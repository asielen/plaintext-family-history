#!/usr/bin/env python3
"""
person.py - fha person: deterministic person-field write-backs (TOOLING §3c).

  fha person new "Full Name" [--sex M|F|intersex|unknown] [--gender TEXT]
                 [--birth DATE] [--death DATE] [--dry-run] [--root PATH]
  fha person set-living <P-id> true|false|unknown [--dry-run] [--root PATH]
  fha person relate <P-id> (--parent|--child|--sibling|--spouse) <P-id2>
                     [--subtype WORD] [--reciprocal] [--dry-run]
  fha person estimate <P-id> [--birth DATE|-] [--death DATE|-] [--dry-run]
  fha person edit <P-id> --section biography|stories|research
                   (--text TEXT | --file PATH) [--append] [--dry-run]
  fha person note <P-id> --section stories|research --text TEXT [--dry-run]

The `living:` flag is the switch every privacy decision hangs on: `fha site`,
`fha gedcom`, and `fha packet` all redact (or refuse) around it, and `unknown`
is treated as living - the safe default (SPEC §9, §19). Until this tool, the
flag could only be changed by hand-editing a person record's YAML frontmatter.
`fha person set-living` is the safe one-line switch: when someone dies, or a
"person" born in 1850 is obviously not living, the human (or a skill acting on
the human's explicit yes) flips the flag with one command.

This module opens the `fha person` namespace for every deterministic
person-record write-back that used to require a hand edit: `new` (mint a
brand-new person from nothing), `set-living` (the privacy flag), `relate` (an
unsourced relationships: belief), `estimate` (the provisional birth:/death:
fields), and `edit`/`note` (the curated profile's prose sections). The five
verbs below `new` share one shape: an engine (`run_*`) that validates,
locates the record by scanning `people/` (never the index), performs surgical
text edits, and returns a `_lib.Result`; a thin `_cmd_*` renders it. `new` is
the one exception to "locate": there is no existing record to find, so it
mints a fresh P-id and writes a brand-new stub instead of editing one - see
its own design-rule bullet below.

DESIGN RULES (why the code looks the way it does)
-------------------------------------------------
- **`new` mints, it never locates.** Every verb below it resolves an EXISTING
  record through `_locate_person`; `new` is the one entry point that creates
  a record from nothing, and it is the parity command behind every "+ add
  person" button (plan 17 BUILD §3.3 option b). It shares
  `_lib.render_stub_content`/`stub_filename`/`mint_ids` with `fha stubs`, so
  a human deliberately typing a name here and an automatic scan finding an
  unresolved reference in `fha stubs` produce byte-identical stub records -
  this module invents no parallel rendering logic. Every input (name, sex,
  birth/death) is validated BEFORE `mint_ids` draws an ID, so a bad flag
  never burns one; `--dry-run` still draws a real ID via `mint_ids` (the same
  contract `fha stubs --from-names --dry-run` uses) so the preview's filename
  and content match a live run exactly, even though nothing is persisted -
  the ID is simply never referenced by any file, so it is as good as unminted.
- **Locate by scanning, never the index.** Every verb resolves a P-id by
  scanning `people/` for the `_{P-id}.md` filename suffix
  (`_lib.find_person_record_path`), so a stale or absent `.cache/index.sqlite`
  can never block or misdirect a write - the same rule `fha claim` follows.
  The four verbs below set-living (relate/estimate/edit/note) share one
  `_locate_person` prelude for the id-shape check, the scan, and the
  merged-tombstone refusal. `set-living` is the one exception: it predates
  that helper and keeps its OWN inline prelude, because its refusals name the
  flag specifically - the merged-tombstone message points at
  `fha person set-living <survivor> <value>` (pinned by test), and the
  no-frontmatter message says "nowhere safe to write the flag" - wording the
  shared, verb-agnostic `_locate_person` cannot produce. Folding set-living in
  would either lose that wording or push a flag-specific callback into the
  shared helper, so the duplication is kept deliberately here. Stubs and
  curated profiles are both editable; generated companion files are never
  candidates. (`estimate`'s optional "a sourced claim already covers this"
  warning is the one place a verb *reads* the index - and only as a soft,
  best-effort note; a missing or stale index never blocks the write, it just
  means the warning cannot be offered.)
- **The edit is text surgery, not a YAML round-trip.** Only the touched
  line(s) change; key order, hand comments, and every other byte survive.
  Reading and writing go through `read_text_exact`/`write_text_exact` so a
  CRLF-authored record churns only the edited lines - `set-living`'s pattern,
  reused by every verb below it.
- **Refuse rather than guess.** Before anything is written to frontmatter, the
  rewrite is re-parsed with the shared `_lib.frontmatter_edit_problem`: it
  must still parse, `id` must be unchanged, and no field outside the verb's
  declared `changed_keys` may appear, disappear, or change value. Any
  failure - including a lookalike line the editor cannot own with certainty -
  is a plain refusal with nothing written. Body-section edits (`edit`/`note`)
  have no YAML to re-parse, so their safety comes from construction instead:
  the rewrite is built by slicing `lines[]` around a located span, so bytes
  outside that span are never touched.
- **A merged tombstone is never edited.** Readers resolve through
  `merged_into` (SPEC §9), so writing to either side of a merge would fork the
  truth; `_locate_person` refuses and names the surviving record to edit
  instead. `relate` checks BOTH ends (the target of a new tie can be a
  tombstone too).
- **`relate` records a belief, not a claim.** Every entry it writes carries
  `status: hypothesis` - unconditionally, no `--status` flag. A sourced edge
  only ever arrives the way SPEC §9 describes: an accepted `relationship`
  claim, applied to the person record by hand or by `fha lint --fix-reciprocal`
  for the mirror side. Giving this verb a way to write `status: accepted` (or
  omit `status:`) would let an unsourced guess masquerade as a sourced fact -
  the one thing this whole archive exists to prevent. (BUILD_INTERFACE sketched
  an open `--status hypothesis|...` flag; this is a deliberate deviation.)
- **`estimate` never contests a sourced claim.** SPEC §9's provisional
  `birth:`/`death:` fields are superseded automatically by a matching claim;
  this verb still writes what is asked (the human is the gate) but warns when
  an accepted claim already exists, so the write is never silently pointless.
- **`edit`/`note` never touch frontmatter or another section.** Each locates
  exactly one `## Heading` span (the heading to the next `## ` heading, or
  EOF) and replaces or extends only that slice. `edit` defaults to replace
  (the curated-writing path: a human or `write-biography` has a finished
  paragraph); `note` is always append (the casual, human-typed-a-thought path)
  and refuses outright - rather than silently misplacing text - when the
  section's existing `<!-- private -->` fencing is unclosed.
- **No stdin mode.** `edit` takes `--text` or `--file`, never a bare stdin
  read: an interactive read blocks forever under a harness that does not
  attach a TTY (a real failure mode on Windows), and there is no way for the
  tool to tell "no --text" apart from "waiting on piped input" without one.
  This departs from the BUILD sketch's assumption of a stdin fallback.
- **Nothing flips `living:` automatically, and no verb here re-derives
  `relationships:` from claims.** Those stay human (or reindex) decisions;
  this module only ever performs the one edit it was asked for.
- **Success exits 0; a real but non-blocking issue exits `EXIT_WARNINGS`.**
  `set-living`/`relate`/`estimate` fold every non-refusal outcome into 0 (ok,
  already-recorded, dry-run) - a warning-level MESSAGE is not the same as a
  warning EXIT CODE, and none of their soft notes (an unrecognised subtype
  word, a sourced claim that already covers a provisional date) block or
  contest the write, so the exit code stays clean. `edit` is the one
  exception: dropping a human's `<!-- private -->` redaction by replacing a
  section is a privacy-adjacent change the human could easily miss in a
  chained script, so - and only there - a warning message also raises the
  exit code, in both a live write and its `--dry-run` preview.

CODE MAP
--------
  _normalize_living           - bool/str/None -> 'true'/'false'/'unknown'/other/None
  (fence location and the pre-write guard are the shared _lib helpers:
   frontmatter_fence_span + frontmatter_edit_problem, also used by confirm merge)
  _key_line_indexes           - column-0 `key:` lines between the fence span
  _replace_living_line        - swap only the value, keep any trailing comment
  _frontmatter_edit_problem   - the living-specific wrapper over the shared guard
  run_set_living              - validate, locate, edit; returns a _lib.Result
  -- shared prelude for every verb below set-living --
  _refuse_result / _not_found_result - the two non-happy Result shapes every
                                 verb returns, factored so they cannot drift
  _locate_person               - id-shape check + scan lookup + merged-tombstone
                                 refusal; the one prelude relate/estimate/edit/note share
  -- new --
  _normalize_sex_input        - case-fold a --sex value onto its canonical
                                 PERSON_SEX_VALUES spelling ('m' -> 'M')
  run_new                      - validate, mint a P-id, render + write the
                                 stub; the one verb that mints instead of locating
  -- relate --
  RELATION_TYPES, _RECIPROCAL_RELATION, KIN_SUBTYPES - the closed vocabularies
  _relationship_entry_exists  - idempotency test: same target id + type already there?
  _relationship_item_lines    - the new entry's YAML lines, unindented
  _insert_relationship_entry  - surgical append/create of the relationships: block
  run_relate                  - validate both ends, dedupe, write (+ mirror), Result
  -- estimate --
  _edtf_gloss                  - plain-language gloss for a normalized EDTF value
  _accepted_vital_claim_exists - soft index read: does an accepted claim already win?
  run_estimate                 - validate dates, locate, edit birth:/death:, Result
  -- edit / note (curated prose sections) --
  SECTION_HEADINGS, _PRIVATE_OPEN/_PRIVATE_CLOSE - section keys and redaction markers
  (the '## Heading' locate/append primitives are the shared _lib helpers:
   section_bounds + lines_end_with_newline + create_section_at_eof +
   append_paragraph_to_section, also used by fha source note)
  _locate_section / _ends_with_newline / _create_section_at_eof / _append_to_section
                                 - thin local wrappers over those _lib helpers
  _replace_section               - swap a section's whole content (replace mode)
  run_edit                      - replace/append one section; Result
  run_note                      - append-only note; refuses on an unclosed private fence
  _emit / _cmd_* / _add_*_arguments / register / _standalone_main
"""

from __future__ import annotations

import argparse
import difflib
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from _lib import (
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FRONT_RE,
    INDEX_SCHEMA_VERSION,
    PERSON_SEX_VALUES,
    Result,
    append_paragraph_to_section,
    configure_utf8_stdout,
    create_section_at_eof,
    extract_bare_ids,
    find_person_record_path,
    fmt_id_display,
    format_edtf_error,
    format_person_sex_error,
    frontmatter_edit_problem,
    frontmatter_fence_span,
    id_type_of,
    is_merged_meta,
    is_valid_edtf,
    is_valid_id,
    lines_end_with_newline,
    mint_ids,
    normalize_date,
    normalize_id,
    read_text_exact,
    reapply_newline,
    render_stub_content,
    resolve_root_arg,
    result_fail,
    section_bounds,
    sqlite_cache_schema_status,
    stub_filename,
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

    The shared guard `_lib.frontmatter_edit_problem` (the frontmatter sibling
    of `_lib.claims_edit_problem`, also used by `fha confirm merge`) carries
    the general contract: the rewrite must parse as a mapping, `id` must be
    unchanged, and no field outside the declared intent - here exactly
    `{'living'}` - may appear, disappear, or change value. The value-identity
    check is deliberately stronger than a key-set compare: it catches a
    `living:` lookalike inside a multi-line quoted scalar, where replacing
    the line silently rewrites ANOTHER field's value. This wrapper adds the
    one field-specific check the shared guard cannot know: `living` must
    equal the target after normalization.

    Returns None when the rewrite is sound, else a short plain-language
    description of what would break; the caller refuses and writes nothing.
    """
    problem = frontmatter_edit_problem(
        new_text, before_meta=before_meta, changed_keys={'living'})
    if problem is not None:
        return problem
    meta = yaml.safe_load(FRONT_RE.match(new_text).group(1))
    if _normalize_living(meta.get('living')) != expect_living:
        return (f'the living flag would read {meta.get("living")!r} '
                f'instead of {expect_living!r}')
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
        return _refuse('refused', f'cannot read {path}: {e}. Check the file is '
                       'not open in another program and try again.')

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
    # tombstone and the surviving record. is_merged_meta normalizes, so a
    # hand-edited `status: Merged` cannot slip past the guard.
    if is_merged_meta(before_meta):
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
    bounds = frontmatter_fence_span(lines)
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


# ── Shared prelude for every verb below set-living ─────────────────────────────

def _refuse_result(
    result: Result, status: str, message: str, *, next_step: str | None = None,
) -> Result:
    """Mark `result` a hard refusal (exit 3): nothing was written.

    A thin alias over the shared `_lib.result_fail` (exit 3 / error-level) so
    relate/estimate/edit/note - which each refuse from more than one call
    site - share exactly one builder with confirm/claim/source instead of
    each carrying a near-copy that could drift (AGENTS_TOOLING's symmetry
    rule). `next_step` carries a plain recovery action when there is one.
    """
    return result_fail(result, status, message, next_step=next_step)


def _not_found_result(result: Result, archive_root: Path, pid: str) -> Result:
    """Mark `result` the standard not-found warning (exit 1) with a `fha find`
    next step - the same shape set-living uses, shared here across the four
    verbs below it (delegates to `_lib.result_fail`)."""
    return result_fail(
        result, 'not-found',
        f'No person record found for {fmt_id_display(pid)} under '
        f'{archive_root / "people"} - check the id with '
        f'`fha find {fmt_id_display(pid)}`.',
        exit_code=EXIT_WARNINGS, level='warning',
        next_step='fha find ' + fmt_id_display(pid))


def _locate_person(
    archive_root: Path, person_id: str, result: Result,
) -> tuple[Path, str, dict, str] | None:
    """Validate a P-id, locate its record, and refuse a merged tombstone.

    The prelude every verb below set-living needs before it can safely edit
    anything: the ID-shape check, the not-found warning (with the `fha find`
    next step), the exact-bytes read, the frontmatter parse, and the
    merged-tombstone refusal all repeated per verb during the first draft of
    this module, so this is the one place they now live. On success returns
    `(path, text, before_meta, normalized_pid)` - `text` is the exact on-disk
    bytes (`read_text_exact`, CRLF-faithful) and `before_meta` is the STRICT
    `yaml.safe_load` of the frontmatter (no scalar coercion), which is what
    `frontmatter_edit_problem` needs to compare against a rewrite value-for-
    value. On failure, `result` is mutated into the right refusal/warning and
    this returns None - callers just `return result` when they see that.
    """
    if not (is_valid_id(person_id) and id_type_of(person_id) == 'P'):
        _refuse_result(
            result, 'refused',
            f'{person_id!r} is not a valid person ID. P-ids look like P-2b3c4d5e6f '
            '- a P followed by a dash and 10 characters from the archive alphabet.')
        return None
    pid = normalize_id(person_id)

    path = find_person_record_path(archive_root, pid)
    if path is None:
        _not_found_result(result, archive_root, pid)
        return None

    try:
        text = read_text_exact(path)
    except OSError as e:
        _refuse_result(
            result, 'refused', f'cannot read {path}: {e}',
            next_step='Check the file is not open in another program and try again.')
        return None

    fm = FRONT_RE.match(text)
    if fm is None:
        _refuse_result(
            result, 'refused',
            f'{path.name} has no frontmatter block (the header between --- lines '
            f'at the top of the file), so there is nowhere safe to write. Open '
            f'{path} and add the header by hand, then run `fha lint`. '
            'Nothing was written.')
        return None
    try:
        before_meta = yaml.safe_load(fm.group(1))
    except yaml.YAMLError:
        before_meta = None
    if not isinstance(before_meta, dict):
        _refuse_result(
            result, 'refused',
            f'the header of {path.name} does not read as YAML, so editing it '
            f'automatically could make things worse. Open {path}, fix the header '
            'by hand (run `fha lint` to see the problem line), then retry. '
            'Nothing was written.')
        return None

    # A merged tombstone is never edited - same rationale as set-living: readers
    # resolve THROUGH merged_into (SPEC §9), so writing here would fork the
    # truth between the tombstone and the record everyone actually reads.
    if is_merged_meta(before_meta):
        name = str(before_meta.get('name') or '').strip()
        label = f'{fmt_id_display(pid)} ({name})' if name else fmt_id_display(pid)
        survivor = normalize_id(str(before_meta.get('merged_into') or ''))
        if survivor and is_valid_id(survivor):
            _refuse_result(
                result, 'merged',
                f'{label} was merged into {fmt_id_display(survivor)} - this record '
                'is a tombstone that readers resolve through, so edit the '
                f'surviving record instead: {fmt_id_display(survivor)}.')
        else:
            _refuse_result(
                result, 'merged',
                f'{label} is a merged tombstone, but its merged_into: pointer is '
                'missing or malformed, so the surviving record cannot be named. '
                f'Find it with `fha find {fmt_id_display(pid)}`, then edit the '
                'survivor. Nothing was written.')
        return None

    return path, text, before_meta, pid


# ── new ──────────────────────────────────────────────────────────────────────

def _normalize_sex_input(value: str | None) -> str | None:
    """Case-fold a --sex value onto its exact PERSON_SEX_VALUES spelling.

    `PERSON_SEX_VALUES` is the small closed set {'M', 'F', 'intersex',
    'unknown'} - a human (or a "+ add person" form) most often reaches for the
    single-letter values in lowercase ('m'/'f'), so this folds any
    case-insensitive match against the whole vocabulary onto its canonical
    spelling ('m' -> 'M', 'Unknown' -> 'unknown'). A value that matches
    NOTHING in the vocabulary (a typo, 'Male', a blank) passes through
    UNCHANGED, so `run_new`'s vocabulary check can quote exactly what the
    human typed in the refusal instead of silently rewriting it into
    something they didn't ask for.
    """
    if value is None:
        return None
    stripped = str(value).strip()
    for canonical in PERSON_SEX_VALUES:
        if stripped.lower() == canonical.lower():
            return canonical
    return stripped


def run_new(
    archive_root: Path, name: str, sex: str | None = None, gender: str | None = None,
    birth: str | None = None, death: str | None = None, dry_run: bool = False,
) -> Result:
    """Mint one P-id, render its stub, and write it under people/stubs/;
    return a Result.

    The one-command mint for a brand-new person - the parity command behind
    every "+ add person" button (plan 17 BUILD §3.3 option b), and the
    deliberate counterpart to `fha stubs`: that tool mints stubs FOR
    references already sitting unresolved in claims or a `--from-names` list;
    this one mints a stub a human is DELIBERATELY starting from nothing, with
    whatever they already know. Both end up calling the exact same
    `_lib.render_stub_content`/`stub_filename` renderers, so a stub reads
    identically no matter which tool wrote it.

    Every input is validated BEFORE `mint_ids` is ever called - a bad `--sex`
    or an unreadable date must never burn a freshly-drawn ID on a write that
    was always going to be refused. `--sex` is checked against
    `PERSON_SEX_VALUES` (via `_normalize_sex_input`'s case fold) and
    `--gender` is passed straight through as free text (SPEC §9: "identity,
    free text; omit unless there is something to record") - neither
    `render_stub_content` validation is re-derived here, only relied on.
    `--birth`/`--death` follow `estimate`'s exact date rule: strict EDTF is
    accepted as-is, loose human wording is translated by `normalize_date`
    with a plain gloss of what was recorded, and anything neither reads
    refuses with the two-example date error - nothing is written or minted
    on that path either. The written vitals are PROVISIONAL, unsourced
    estimates (SPEC §9), exactly like a stub `fha stubs` would mint - never
    confused with a sourced claim.

    `data` is {'status': 'ok'|'dry-run'|'refused', 'person_id', 'path',
    'name'}; `changed` names the new file on a live write. `--dry-run` still
    draws a real ID from `mint_ids` (matching the "preview shows a real
    minted-but-unwritten id" contract `fha stubs --from-names --dry-run`
    already uses) so the preview's filename and content are exactly what a
    live run would produce; the ID is simply never written to any file, so it
    is as good as never minted. The never-overwrite guard below `mint_ids` is
    a belt-and-braces check: `mint_ids` collision-scans the whole tree before
    handing back an ID, so reaching an existing file at the fresh ID's target
    path should be next to impossible - but "next to impossible" still gets a
    plain refusal instead of a silent overwrite.
    """
    result = Result(data={'status': None, 'person_id': None, 'path': None, 'name': None})

    clean_name = str(name or '').strip()
    if not clean_name:
        return _refuse_result(
            result, 'refused',
            'a name is required to mint a new person - e.g. '
            '`fha person new "Jane Doe"`. Nothing was minted.')
    result.data['name'] = clean_name

    sex_clean = _normalize_sex_input(sex)
    if sex_clean is not None and sex_clean not in PERSON_SEX_VALUES:
        return _refuse_result(result, 'refused', format_person_sex_error(sex))

    fields: dict[str, str] = {}   # field -> target EDTF ('birth'/'death' only ever added)
    gloss: dict[str, str] = {}    # field -> plain-language note (only when normalized)
    for field, raw in (('birth', birth), ('death', death)):
        if raw is None:
            continue
        raw_str = str(raw).strip()
        if not raw_str:
            continue   # a blank flag value is the same as not giving the flag
        if is_valid_edtf(raw_str):
            fields[field] = raw_str
            continue
        normalized = normalize_date(raw_str)
        if normalized is None:
            return _refuse_result(result, 'refused', format_edtf_error(raw_str, field=field))
        fields[field] = normalized
        if normalized != raw_str:
            gloss[field] = _edtf_gloss(normalized)

    stubs_dir = archive_root / 'people' / 'stubs'
    pid = mint_ids('P', 1, archive_root)[0].lower()
    filename = stub_filename(clean_name, pid)
    path = stubs_dir / filename
    result.data['person_id'] = fmt_id_display(pid)
    result.data['path'] = str(path)

    if path.exists():
        return _refuse_result(
            result, 'refused',
            f'{filename} already exists at {path}, and a freshly minted id '
            'should never collide with an existing file. Run '
            f'`fha find {fmt_id_display(pid)}` to see what is there, then '
            'try again. Nothing was written.')

    content = render_stub_content(
        pid, clean_name, sex=sex_clean, gender=gender,
        birth=fields.get('birth'), death=fields.get('death'))

    def _add_new_messages() -> None:
        for field, note in gloss.items():
            result.add('info', f'recorded {field} as {fields[field]} - {note}.')
        if fields:
            described = ' and '.join(f'{f}: {fields[f]}' for f in fields)
            result.add('info',
                       f'{described} - recorded as unsourced family knowledge '
                       'until a record backs it up; `fha lint` will keep '
                       'listing it as needing a source until an accepted '
                       'birth/death claim supersedes it.')
        result.add('info',
                   f'Next: `fha person relate {fmt_id_display(pid)} '
                   '--parent|--child|--sibling|--spouse <P-id2>` to tie '
                   f'{clean_name} to family.')
        result.add('info',
                   f'Next: `fha find {fmt_id_display(pid)}` to confirm the new record.',
                   next_step=f'fha find {fmt_id_display(pid)}')

    if dry_run:
        result.data['status'] = 'dry-run'
        result.add('info',
                   f'[dry-run] Would create {fmt_id_display(pid)} ({clean_name}) '
                   f'- people/stubs/{filename}.')
        _add_new_messages()
        for dline in difflib.unified_diff(
            [], content.splitlines(), fromfile='(new file)',
            tofile=f'{path} (after)', lineterm='',
        ):
            result.add('info', dline)
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    try:
        stubs_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
    except OSError as e:
        return _refuse_result(
            result, 'refused',
            f'cannot write {path}: {e}. Check the folder is writable, then retry.')

    result.data['status'] = 'ok'
    result.note_changed(path)
    result.add('info',
               f'Created {fmt_id_display(pid)} ({clean_name}) - people/stubs/{filename}.',
               path=path)
    _add_new_messages()
    return result


# ── relate ───────────────────────────────────────────────────────────────────

# The four family-tie roles this verb records (SPEC §9's relationships: type).
RELATION_TYPES = ('parent', 'child', 'sibling', 'spouse')

# entry `type` -> the reciprocal `type` the mirror entry uses on the other
# person's record (SPEC §9: "a child edge here implies a parent edge there").
_RECIPROCAL_RELATION = {'parent': 'child', 'child': 'parent',
                        'sibling': 'sibling', 'spouse': 'spouse'}

# SPEC §8.2's kin subtype vocabulary - the natures that make sense on a
# parent/child/sibling/spouse edge. (The non-kin words in §8.2 - enslaver,
# employer, member-of, ... - describe ties this verb does not record; relate
# only writes the four family roles above.) An unrecognised word is still
# written (never rejected - "forgiving, not fussy", AGENTS.md), just flagged.
KIN_SUBTYPES = frozenset({
    'biological', 'adoptive', 'step', 'foster', 'guardian',
    'surrogate-gestational', 'surrogate-genetic', 'donor-sperm', 'donor-egg',
    'social',
})


def _relationship_entry_exists(meta: dict, target_pid: str, relation_type: str) -> bool:
    """True when `meta`'s relationships: list already names target_pid at this type.

    Matches the `to:` field by the P-id it CONTAINS (`extract_bare_ids`), not
    by exact string, so `to: "[[P-xxxx|Name]]"` and a bare `to: P-xxxx` both
    dedupe the same way - the forgiving match the task spec calls for. Subtype
    is deliberately NOT part of the match: two entries naming the same person
    at the same role are "the same tie" for idempotency even if one carries a
    subtype and the other doesn't.
    """
    for entry in (meta.get('relationships') or []):
        if not isinstance(entry, dict):
            continue
        to_ids = extract_bare_ids(str(entry.get('to') or ''))
        entry_type = str(entry.get('type') or '').strip().lower()
        if target_pid in to_ids and entry_type == relation_type:
            return True
    return False


def _relationship_item_lines(
    target_pid: str, target_name: str, relation_type: str, subtype: str | None,
) -> list[str]:
    """The new relationships: list item's lines, UNINDENTED.

    `_insert_relationship_entry` applies the block's own indentation (or the
    SPEC default) on top of these, so this function only owns the field
    order and content: a pinned `[[P-id|Name]]` target (readable AND
    resolvable even if the name changes later), the role, the nature when
    given, and `status: hypothesis` - always, per this verb's one job
    (recording a belief, never a sourced fact; see the module docstring).
    """
    lines = [
        f'- to: "[[{fmt_id_display(target_pid)}|{target_name}]]"',
        f'  type: {relation_type}',
    ]
    if subtype:
        lines.append(f'  subtype: {subtype}')
    lines.append('  status: hypothesis')
    return lines


def _insert_relationship_entry(text: str, item_lines: list[str]) -> str | None:
    """Append one relationships: list item to `text`'s frontmatter, creating
    the key if absent. Returns the rewritten text, or None when the field is
    written in a form this cannot safely extend.

    Text surgery on `text.split('\\n')`, the same pattern `run_set_living`
    uses: key order and hand comments outside the touched lines survive
    untouched. `item_lines` are UNINDENTED ('- to: ...', '  type: ...', ...);
    this locates an existing block's own list-item indent (falling back to
    the SPEC default, two spaces) so an append matches whatever a human
    already typed instead of assuming one true indentation. A `relationships:
    [...]` FLOW form is refused (returns None) rather than risk creating a
    second `relationships:` key or corrupting the inline list; the caller
    turns that into a plain refusal.
    """
    lines = text.split('\n')
    bounds = frontmatter_fence_span(lines)
    if bounds is None:
        return None
    start, end = bounds
    cr = '\r' if lines[start].endswith('\r') else ''
    new_lines = list(lines)

    key_lines = _key_line_indexes(lines, start + 1, end, 'relationships')
    if len(key_lines) > 1:
        return None

    if key_lines:
        idx = key_lines[0]
        rest = lines[idx].split(':', 1)[1].strip()
        if rest and not rest.startswith('#'):
            return None   # flow/inline form - refuse rather than risk corrupting it
        block_end = idx + 1
        while block_end < end:
            line = lines[block_end]
            if line.startswith(' ') or line.startswith('\t'):
                block_end += 1
                continue
            if line.strip() == '':
                # A blank line is part of the list ONLY if the list continues
                # after it - a later still-indented entry. A blank run followed
                # by a non-indented line (or the closing --- fence at `end`) is
                # the TRUE end of the block. Without this lookahead the scan
                # stopped at the first in-list blank line and spliced the new
                # entry mid-list (two entries separated by a blank line got the
                # third wedged between them). A blank in CRLF frontmatter is a
                # lone '\r', which `.strip()` also reads as empty.
                look = block_end + 1
                while look < end and lines[look].strip() == '':
                    look += 1
                if look < end and (lines[look].startswith(' ')
                                   or lines[look].startswith('\t')):
                    block_end = look
                    continue
            break
        indent = '  '
        for ln in lines[idx + 1:block_end]:
            m = re.match(r'^(\s*)-', ln)
            if m:
                indent = m.group(1)
                break
        new_lines[block_end:block_end] = [f'{indent}{ln}{cr}' for ln in item_lines]
    else:
        created_lines = _key_line_indexes(lines, start + 1, end, 'created')
        insert_at = created_lines[0] if created_lines else end
        new_lines[insert_at:insert_at] = (
            [f'relationships:{cr}'] + [f'  {ln}{cr}' for ln in item_lines])

    return '\n'.join(new_lines)


def run_relate(
    archive_root: Path, person_id: str, relation_type: str, target_id: str,
    subtype: str | None = None, reciprocal: bool = False, dry_run: bool = False,
) -> Result:
    """Record an unsourced family-tie belief on `person_id` (and, with
    `reciprocal`, its mirror on `target_id`); return a Result.

    `data` is {'status': 'ok'|'already'|'dry-run'|'not-found'|'merged'|
    'refused', 'person_id', 'target_id', 'relation', 'subtype'}; `changed`
    lists every file actually written (0, 1, or 2 depending on what was
    already present). Idempotency and the reciprocal write are each checked
    INDEPENDENTLY per side, so re-running with `--reciprocal` after a
    non-reciprocal call fills in just the missing mirror rather than
    refusing outright or duplicating the forward entry.
    """
    result = Result(data={
        'status': None, 'person_id': None, 'target_id': None,
        'relation': relation_type, 'subtype': subtype or None,
    })

    if relation_type not in RELATION_TYPES:
        return _refuse_result(
            result, 'refused',
            f'{relation_type!r} is not a relation this tool records. Use one '
            'of: ' + ', '.join(RELATION_TYPES) +
            ' - e.g. `fha person relate P-... --parent P-...`.')

    owner = _locate_person(archive_root, person_id, result)
    if owner is None:
        return result
    owner_path, owner_text, owner_meta, pid = owner
    result.data['person_id'] = fmt_id_display(pid)

    if not (is_valid_id(target_id) and id_type_of(target_id) == 'P'):
        return _refuse_result(
            result, 'refused',
            f'{target_id!r} is not a valid person ID. P-ids look like '
            'P-2b3c4d5e6f - a P followed by a dash and 10 characters from '
            'the archive alphabet.')
    tid_check = normalize_id(target_id)
    if tid_check == pid:
        return _refuse_result(
            result, 'refused',
            f'{fmt_id_display(pid)} cannot be related to themselves. Pick a '
            f'different person for --{relation_type}.')

    target = _locate_person(archive_root, target_id, result)
    if target is None:
        return result
    target_path, target_text, target_meta, tid = target
    result.data['target_id'] = fmt_id_display(tid)

    owner_name = str(owner_meta.get('name') or '').strip() or fmt_id_display(pid)
    target_name = str(target_meta.get('name') or '').strip() or fmt_id_display(tid)
    subtype_clean = (subtype or '').strip() or None
    warn_subtype = bool(subtype_clean) and subtype_clean.lower() not in KIN_SUBTYPES
    mirror_type = _RECIPROCAL_RELATION[relation_type]

    owner_has = _relationship_entry_exists(owner_meta, tid, relation_type)
    target_has = reciprocal and _relationship_entry_exists(target_meta, pid, mirror_type)

    if owner_has and (not reciprocal or target_has):
        result.data['status'] = 'already'
        result.add('info',
                   f'{owner_name} already has a {relation_type} entry for '
                   f'{target_name} - nothing to change.')
        return result

    # (path, old_text, new_text, human-readable description of the write)
    writes: list[tuple[Path, str, str, str]] = []

    if not owner_has:
        item_lines = _relationship_item_lines(tid, target_name, relation_type, subtype_clean)
        new_owner_text = _insert_relationship_entry(owner_text, item_lines)
        if new_owner_text is None:
            return _refuse_result(
                result, 'refused',
                f"{owner_path.name}'s relationships: field isn't written as a "
                'plain list, so a new entry cannot be added safely. Open '
                f'{owner_path} and add it by hand, then run `fha lint`. '
                'Nothing was written.')
        problem = frontmatter_edit_problem(
            new_owner_text, before_meta=owner_meta, changed_keys={'relationships'})
        if problem is not None:
            return _refuse_result(
                result, 'refused',
                f'Refusing to update {owner_path.name}: {problem}, so saving '
                f'could corrupt the record. Nothing was written. Open '
                f'{owner_path} and add the relationships: entry by hand, then '
                'run `fha lint`.')
        writes.append((owner_path, owner_text, new_owner_text,
                       f'{owner_name} -> {relation_type} -> {target_name}'))

    if reciprocal and not target_has:
        item_lines = _relationship_item_lines(pid, owner_name, mirror_type, subtype_clean)
        new_target_text = _insert_relationship_entry(target_text, item_lines)
        if new_target_text is None:
            return _refuse_result(
                result, 'refused',
                f"{target_path.name}'s relationships: field isn't written as a "
                'plain list, so the mirror entry cannot be added safely. Open '
                f'{target_path} and add it by hand, then run `fha lint`. '
                'Nothing was written.')
        problem = frontmatter_edit_problem(
            new_target_text, before_meta=target_meta, changed_keys={'relationships'})
        if problem is not None:
            return _refuse_result(
                result, 'refused',
                f'Refusing to update {target_path.name}: {problem}, so saving '
                f'could corrupt the record. Nothing was written. Open '
                f'{target_path} and add the mirror entry by hand, then run '
                '`fha lint`.')
        writes.append((target_path, target_text, new_target_text,
                       f'{target_name} -> {mirror_type} -> {owner_name}'))

    if not writes:
        result.data['status'] = 'already'
        result.add('info',
                   f'{owner_name} and {target_name} already record this tie - '
                   'nothing to change.')
        return result

    if warn_subtype:
        result.add('warning',
                   f'{subtype_clean!r} is not one of the archive\'s kin '
                   'subtypes (SPEC §8.2): biological, adoptive, step, foster, '
                   'guardian, surrogate-gestational, surrogate-genetic, '
                   'donor-sperm, donor-egg, social. Recorded as written - fix '
                   'it by hand if it was a typo.')

    if dry_run:
        result.data['status'] = 'dry-run'
        for path, old_text, new_text, label in writes:
            result.add('info', f'[dry-run] Would record: {label}.')
            for dline in difflib.unified_diff(
                old_text.splitlines(), new_text.splitlines(),
                fromfile=f'{path} (before)', tofile=f'{path} (after)', lineterm='',
            ):
                result.add('info', dline)
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    for path, old_text, new_text, label in writes:
        try:
            write_text_exact(path, reapply_newline(new_text, old_text))
        except OSError as e:
            return _refuse_result(
                result, 'refused',
                f'cannot write {path}: {e}. Check the file is not open '
                'elsewhere and the folder is writable, then retry.')
        result.note_changed(path)
        result.add('info',
                   f'Recorded: {label} (an unsourced belief - `fha claim` '
                   'records a sourced tie).', path=path)

    result.data['status'] = 'ok'
    return result


# ── estimate ─────────────────────────────────────────────────────────────────

def _edtf_gloss(edtf: str) -> str:
    """Plain-language gloss for a canonical EDTF value ('1870~' -> 'about 1870').

    Used only to explain a NORMALIZED date back to the human in the same
    words `fha lint` already uses for its own date-normalization suggestions
    (a small, intentionally duplicated pure function - tools never import
    tools, and this task's scope excludes `_lib.py`, so it lives here too
    rather than being hoisted to the one shared home it would otherwise earn).
    """
    if '/' in edtf:
        a, b = edtf.split('/', 1)
        return f'between {a} and {b}'
    before_m = re.match(r'^\[\.{2}(.+)\]$', edtf)
    if before_m:
        return f'on or before {before_m.group(1)}'
    decade_m = re.match(r'^(\d{3})X$', edtf)
    if decade_m:
        return f'the {decade_m.group(1)}0s'
    if edtf.endswith('~'):
        return f'about {edtf[:-1]}'
    if edtf.endswith('?'):
        return f'{edtf[:-1]}, uncertain'
    return edtf


def _accepted_vital_claim_exists(archive_root: Path, pid: str, field: str) -> bool:
    """True when a FRESH `.cache/index.sqlite` shows an accepted `field`
    (birth/death) claim for this person; False for every other case -
    absent, stale-schema, unreadable, or genuinely no such claim.

    Deliberately a soft, best-effort read: `estimate` must keep working the
    way `fha claim` and `set-living` do, without depending on `fha index`
    having been run recently (SPEC's provisional-estimate flow explicitly
    starts before any source, let alone an index rebuild, exists). When the
    index CAN answer, this lets the write add one honest warning - a
    provisional estimate a sourced claim already supersedes is still fine to
    record (SPEC §8.6: recording what you know before you can prove it is a
    legitimate starting state), but the human should know the claim, not
    this field, is what exports and timelines actually show.
    """
    db_path = archive_root / '.cache' / 'index.sqlite'
    status, _ = sqlite_cache_schema_status(
        db_path, INDEX_SCHEMA_VERSION, ('claims', 'claim_persons'))
    if status != 'fresh':
        return False
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            'SELECT 1 FROM claims c JOIN claim_persons cp ON cp.claim_id = c.id '
            'WHERE cp.person_id = ? AND c.type = ? AND c.status = ? LIMIT 1',
            (pid, field, 'accepted'),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def run_estimate(
    archive_root: Path, person_id: str, birth: str | None = None,
    death: str | None = None, dry_run: bool = False,
) -> Result:
    """Write the provisional, unsourced birth:/death: estimates (SPEC §9);
    return a Result.

    `birth`/`death` are None when the flag was not given at all (nothing to
    do for that field), or the literal string `'-'` to CLEAR the field
    (remove the line), or a date string to set - accepted as strict EDTF
    (`is_valid_edtf`) or loose human wording (`normalize_date`, "circa 1870"
    -> "1870~"). At least one of the two must be given. Both dates are
    validated BEFORE any file is touched, so a bad second date never leaves
    the first one written - `estimate --birth 1870 --death nonsense` writes
    nothing and explains only the date that failed.

    `data` is {'status': 'ok'|'already'|'dry-run'|'not-found'|'merged'|
    'refused', 'person_id', 'path', 'birth', 'death'}. A field already
    recording the requested value (or already absent, for a clear) is a
    silent no-op for that field - same idempotence rule as `set-living`.
    """
    result = Result(data={
        'status': None, 'person_id': None, 'path': None, 'birth': None, 'death': None,
    })

    if birth is None and death is None:
        return _refuse_result(
            result, 'refused',
            'estimate needs at least one date to record - add --birth DATE, '
            '--death DATE, or both. Use `-` to clear a field instead of a '
            'date, e.g. `fha person estimate P-... --birth -`.')

    fields: dict[str, str | None] = {}   # field -> target EDTF, or None to clear
    gloss: dict[str, str] = {}           # field -> plain-language note (only when normalized)
    for field, raw in (('birth', birth), ('death', death)):
        if raw is None:
            continue
        raw = str(raw).strip()
        if raw == '-':
            fields[field] = None
            continue
        if is_valid_edtf(raw):
            fields[field] = raw
            continue
        normalized = normalize_date(raw)
        if normalized is None:
            return _refuse_result(result, 'refused', format_edtf_error(raw, field=field))
        fields[field] = normalized
        if normalized != raw:
            gloss[field] = _edtf_gloss(normalized)

    owner = _locate_person(archive_root, person_id, result)
    if owner is None:
        return result
    path, text, before_meta, pid = owner
    result.data['person_id'] = fmt_id_display(pid)
    result.data['path'] = str(path)
    name = str(before_meta.get('name') or '').strip()
    label = f'{fmt_id_display(pid)} ({name})' if name else fmt_id_display(pid)

    to_write: dict[str, str | None] = {}
    for field, target in fields.items():
        current = before_meta.get(field)
        current_norm = str(current).strip() if current is not None else None
        if target is None:
            if current_norm is None:
                continue   # already absent - nothing to clear
        elif current_norm == target:
            continue       # already recorded
        to_write[field] = target

    if not to_write:
        result.data['status'] = 'already'
        described = ' and '.join(
            f'{f}: {fields[f]}' if fields[f] is not None else f'no {f}' for f in fields)
        result.add('info', f'{label} already records {described} - nothing to change.')
        return result

    lines = text.split('\n')
    bounds = frontmatter_fence_span(lines)
    if bounds is None:
        return _refuse_result(
            result, 'refused',
            f'could not locate the frontmatter fences in {path.name} to edit '
            f'safely. Open {path} and set the date(s) by hand, then run '
            '`fha lint`. Nothing was written.')
    start, end = bounds
    cr = '\r' if lines[start].endswith('\r') else ''
    new_lines = list(lines)
    fresh_insert_at: int | None = None   # shared anchor so birth+death insert in order
    warn_claim_wins: list[str] = []

    for field, target in to_write.items():
        real_lines = _key_line_indexes(new_lines, start + 1, end, field)
        commented = [i for i in range(start + 1, end)
                     if re.match(rf'^#\s*{re.escape(field)}:(?=\s|$)', new_lines[i])]
        if len(real_lines) > 1 or (not real_lines and len(commented) > 1):
            return _refuse_result(
                result, 'refused',
                f'{path.name} has more than one {field}: line in its header, '
                'so the right one to edit cannot be chosen safely. Open '
                f'{path} and fix the duplicate by hand, then run `fha lint`. '
                'Nothing was written.')

        if target is None:
            # Clearing only ever reaches here when a REAL line exists (the
            # idempotence check above already skipped "clear an absent
            # field", and only a real line can make before_meta non-None).
            if real_lines:
                del new_lines[real_lines[0]]
                end -= 1
                if fresh_insert_at is not None and real_lines[0] < fresh_insert_at:
                    fresh_insert_at -= 1
            continue

        if real_lines:
            new_lines[real_lines[0]] = f'{field}: {target}{cr}'
        elif commented:
            new_lines[commented[0]] = f'{field}: {target}{cr}'
        else:
            if fresh_insert_at is None:
                living_lines = _key_line_indexes(new_lines, start + 1, end, 'living')
                fresh_insert_at = (living_lines[0] + 1) if living_lines else end
            new_lines.insert(fresh_insert_at, f'{field}: {target}{cr}')
            end += 1
            fresh_insert_at += 1

        if _accepted_vital_claim_exists(archive_root, pid, field):
            warn_claim_wins.append(field)

    new_text = '\n'.join(new_lines)
    problem = frontmatter_edit_problem(
        new_text, before_meta=before_meta, changed_keys=set(to_write.keys()))
    if problem is not None:
        return _refuse_result(
            result, 'refused',
            f'Refusing to update {label}: {problem}, so saving could corrupt '
            f'the record. Nothing was written. Open {path} and set the '
            'date(s) by hand, then run `fha lint` to check it.')

    def _add_field_messages() -> None:
        for field, target in to_write.items():
            shown = target if target is not None else '(cleared)'
            result.add('info', f'{label} {field}: {shown}.')
        for field, note in gloss.items():
            if field in to_write:
                result.add('info', f'{field} recorded as {fields[field]} - {note}.')
        for field in warn_claim_wins:
            result.add('warning',
                       f'{label} already has an accepted {field} claim - the '
                       'sourced claim wins everywhere exports and timelines '
                       'read from; this estimate is kept as a fallback note only.')

    if dry_run:
        result.data['status'] = 'dry-run'
        result.add('info', f'[dry-run] Would update {label}.')
        _add_field_messages()
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
        return _refuse_result(
            result, 'refused',
            f'cannot write {path}: {e}. Check the file is not open elsewhere '
            'and the folder is writable, then retry.')

    result.data['status'] = 'ok'
    result.note_changed(path)
    _add_field_messages()
    # `to_write[f]` and `fields[f]` are always the same value when f was
    # written (to_write is copied straight from fields) - so the effective
    # value is just fields.get(f), and None when that field's flag was never
    # given at all.
    result.data['birth'] = fields.get('birth') if birth is not None else None
    result.data['death'] = fields.get('death') if death is not None else None
    return result


# ── edit / note (curated prose sections) ────────────────────────────────────

# section flag -> the `## Heading` it targets in the curated profile's body
# (the same file `run_set_living` etc. locate; SPEC §16's profile structure,
# extended in practice - and in archive-template/example-archive - with a
# `## Research Notes` section between Stories and Friends & Family).
SECTION_HEADINGS = {
    'biography': 'Biography',
    'stories': 'Stories',
    'research': 'Research Notes',
}

# The redaction fence a human wraps around text meant to stay out of shared
# exports (archive-template's own example). `edit`/`note` never interpret
# what's inside it - they only check whether the markers are present/balanced.
_PRIVATE_OPEN = '<!-- private -->'
_PRIVATE_CLOSE = '<!-- /private -->'

# The prose-section locate/append machinery moved to _lib.py (`section_bounds`,
# `lines_end_with_newline`, `create_section_at_eof`, `append_paragraph_to_section`)
# when `fha source note` needed the same CRLF-safe bounded '## Heading' edit and
# had grown its own copy of it. These stay as thin local wrappers so this file's
# own readers, and `_replace_section` below, keep their historic names.

def _locate_section(
    lines: list[str], body_start: int, heading_text: str,
) -> tuple[int, int, int] | None:
    """Thin wrapper over `_lib.section_bounds` (kept for this file's callers)."""
    return section_bounds(lines, body_start, heading_text)


def _ends_with_newline(lines: list[str]) -> bool:
    """Thin wrapper over `_lib.lines_end_with_newline`."""
    return lines_end_with_newline(lines)


def _create_section_at_eof(
    lines: list[str], heading_text: str, body_text: str, cr: str,
) -> list[str]:
    """Thin wrapper over `_lib.create_section_at_eof`."""
    return create_section_at_eof(lines, heading_text, body_text, cr)


def _replace_section(
    lines: list[str], body_start: int, heading_text: str, new_text: str, cr: str,
) -> tuple[list[str], bool, str]:
    """Return `(new_lines, created, old_content)` - replace mode.

    `created` is True when the heading did not exist and had to be added
    (appended at EOF, per the task spec, with one blank-line separator from
    whatever came before). When the heading already existed, its ENTIRE
    content span (heading to next `## ` heading, or EOF) is replaced with
    `new_text` - this is the "bounded" edit: nothing outside that span is
    touched, and a single blank line separates the new prose from a
    following heading (normalizing whatever spacing was there before, which
    is within bounds since that spacing WAS part of the replaced span).
    """
    located = _locate_section(lines, body_start, heading_text)
    body_text = new_text.strip('\n')
    if located is None:
        return _create_section_at_eof(lines, heading_text, body_text, cr), True, ''

    _, content_start, content_end = located
    old_content = '\n'.join(lines[content_start:content_end])
    has_next = content_end < len(lines)
    new_lines = list(lines[:content_start])
    new_lines.extend(f'{ln}{cr}' for ln in body_text.split('\n'))
    if has_next:
        new_lines.append(cr)             # a real blank-line separator - more follows
    elif _ends_with_newline(lines):
        new_lines.append('')             # the file's own end-of-file sentinel, restored
    new_lines.extend(lines[content_end:])
    return new_lines, False, old_content


def _append_to_section(
    lines: list[str], body_start: int, heading_text: str, new_text: str, cr: str,
) -> tuple[list[str], bool, str]:
    """Thin wrapper over `_lib.append_paragraph_to_section` (append mode).

    Adds `new_text` as a new, blank-line-separated paragraph at the END of
    the section, never touching what was already there (the contract `note`
    depends on: it is the human-written, nothing-ever-lost path). The shared
    engine also treats a lone `*(none yet)*` placeholder as empty and creates
    the heading at EOF when absent - see its docstring in `_lib.py`.
    """
    return append_paragraph_to_section(lines, body_start, heading_text, new_text, cr)


def run_edit(
    archive_root: Path, person_id: str, section: str, text: str | None = None,
    file_path: str | None = None, append: bool = False, dry_run: bool = False,
) -> Result:
    """Replace (default) or append to one curated-profile prose section;
    return a Result.

    Exactly one of `text`/`file_path` must be given (no stdin mode - see the
    module docstring). `data` is {'status': 'ok'|'already'|'dry-run'|
    'not-found'|'merged'|'refused', 'person_id', 'path', 'section'}.

    Two soft notes, neither a refusal - "the human is the gate" (module
    docstring): (1) replacing a section whose OLD text had a balanced
    `<!-- private -->`/`<!-- /private -->` fence with NEW text that has none
    still writes, but warns AND raises the exit code to EXIT_WARNINGS (a live
    write or a --dry-run preview both) - dropping someone's redaction is easy
    to miss in a scripted call, so it is the one place this module escalates
    a warning past a plain message. (2) writing prose onto a `tier: stub`
    record (SPEC's frontmatter-only tier) is noted, not warned - a gentle
    status update, since a stub gaining its first prose is normal, expected
    progress, not a problem.
    """
    result = Result(data={'status': None, 'person_id': None, 'path': None, 'section': section})

    if section not in SECTION_HEADINGS:
        return _refuse_result(
            result, 'refused',
            f'{section!r} is not a section this tool edits. Use one of: '
            + ', '.join(SECTION_HEADINGS) + '.')
    if (text is None) == (file_path is None):
        return _refuse_result(
            result, 'refused',
            'give exactly one of --text or --file - the prose to write, or a '
            'file that already holds it. Neither (or both) was given.')

    if file_path is not None:
        try:
            new_text = Path(file_path).read_text(encoding='utf-8')
        except OSError as e:
            return _refuse_result(
                result, 'refused',
                f'cannot read {file_path}: {e}. Check the path and try again.')
    else:
        new_text = str(text)

    if not new_text.strip():
        return _refuse_result(
            result, 'refused',
            'there is no text to write - --text/--file was empty. Nothing was changed.')

    owner = _locate_person(archive_root, person_id, result)
    if owner is None:
        return result
    path, old_text, before_meta, pid = owner
    result.data['person_id'] = fmt_id_display(pid)
    result.data['path'] = str(path)
    name = str(before_meta.get('name') or '').strip()
    label = f'{fmt_id_display(pid)} ({name})' if name else fmt_id_display(pid)

    lines = old_text.split('\n')
    bounds = frontmatter_fence_span(lines)
    if bounds is None:
        return _refuse_result(
            result, 'refused',
            f'could not locate the frontmatter fences in {path.name} to edit '
            f'safely. Open {path} and edit the ## {SECTION_HEADINGS[section]} '
            'section by hand. Nothing was written.')
    _, fence_end = bounds
    cr = '\r' if lines[0].endswith('\r') else ''
    heading_text = SECTION_HEADINGS[section]

    if append:
        new_lines, created, old_content = _append_to_section(
            lines, fence_end + 1, heading_text, new_text, cr)
    else:
        new_lines, created, old_content = _replace_section(
            lines, fence_end + 1, heading_text, new_text, cr)
    new_full_text = '\n'.join(new_lines)

    warn_private_dropped = False
    if not append:
        had_private = _PRIVATE_OPEN in old_content and _PRIVATE_CLOSE in old_content
        keeps_private = _PRIVATE_OPEN in new_text and _PRIVATE_CLOSE in new_text
        warn_private_dropped = had_private and not keeps_private

    warn_stub = str(before_meta.get('tier') or '').strip().lower() == 'stub'
    verb = 'append to' if append else 'replace'

    if dry_run:
        result.data['status'] = 'dry-run'
        if created:
            result.add('info',
                       f'[dry-run] Would create ## {heading_text} in {path.name} '
                       'and write the text.')
        else:
            result.add('info', f'[dry-run] Would {verb} ## {heading_text} in {path.name}.')
        for dline in difflib.unified_diff(
            old_text.splitlines(), new_full_text.splitlines(),
            fromfile=f'{path} (before)', tofile=f'{path} (after)', lineterm='',
        ):
            result.add('info', dline)
        if warn_private_dropped:
            result.add('warning',
                       f"{label}'s current ## {heading_text} has a "
                       '<!-- private --> section the new text does not '
                       'repeat, so it would be dropped. Include it in '
                       '--text/--file if you want it kept.')
            result.exit_code = EXIT_WARNINGS
        if warn_stub:
            result.add('info',
                       f'{label} is a stub (frontmatter only, SPEC tier: '
                       'stub) - this write begins its curated prose.')
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    try:
        write_text_exact(path, reapply_newline(new_full_text, old_text))
    except OSError as e:
        return _refuse_result(
            result, 'refused',
            f'cannot write {path}: {e}. Check the file is not open elsewhere '
            'and the folder is writable, then retry.')

    result.data['status'] = 'ok'
    result.note_changed(path)
    if created:
        result.add('info',
                   f'Created ## {heading_text} in {label} and wrote the text.', path=path)
    else:
        verb_done = 'Appended to' if append else 'Replaced'
        result.add('info', f'{verb_done} ## {heading_text} in {label}.', path=path)
    if warn_private_dropped:
        result.add('warning',
                   f"{label}'s old ## {heading_text} had a <!-- private --> "
                   'section that is not in the new text, so it is now gone. '
                   'Nothing else was touched.')
        result.exit_code = EXIT_WARNINGS
    if warn_stub:
        result.add('info',
                   f'{label} was a stub (frontmatter only) - it now carries '
                   'curated prose too.')
    return result


def run_note(
    archive_root: Path, person_id: str, section: str, text: str, dry_run: bool = False,
) -> Result:
    """Append TEXT as a new paragraph at the end of a Stories/Research Notes
    section, creating the section if missing; return a Result.

    Append-only and never a replace - this is the casual, human-typed-a-
    thought path (the module docstring), so there is no --append flag to
    turn off (unlike `edit`). Refuses outright, before writing anything, when
    the section's EXISTING text has an unclosed `<!-- private -->` fence: an
    unequal open/close count means this module cannot tell which side of the
    redaction boundary the new paragraph would land on, and guessing wrong
    would either leak something meant to stay private or bury new text inside
    a fence that was never meant to hold it.
    """
    result = Result(data={'status': None, 'person_id': None, 'path': None, 'section': section})

    if section not in ('stories', 'research'):
        return _refuse_result(
            result, 'refused',
            f'{section!r} is not a section note can add to. Use stories or research.')
    if not str(text or '').strip():
        return _refuse_result(
            result, 'refused',
            'there is no text to add - --text was empty. Nothing was changed.')

    owner = _locate_person(archive_root, person_id, result)
    if owner is None:
        return result
    path, old_text, before_meta, pid = owner
    result.data['person_id'] = fmt_id_display(pid)
    result.data['path'] = str(path)
    name = str(before_meta.get('name') or '').strip()
    label = f'{fmt_id_display(pid)} ({name})' if name else fmt_id_display(pid)

    lines = old_text.split('\n')
    bounds = frontmatter_fence_span(lines)
    if bounds is None:
        return _refuse_result(
            result, 'refused',
            f'could not locate the frontmatter fences in {path.name} to edit '
            f'safely. Open {path} and add the note by hand. Nothing was written.')
    _, fence_end = bounds
    cr = '\r' if lines[0].endswith('\r') else ''
    heading_text = SECTION_HEADINGS[section]

    new_lines, created, old_content = _append_to_section(
        lines, fence_end + 1, heading_text, text, cr)

    opens = old_content.count(_PRIVATE_OPEN)
    closes = old_content.count(_PRIVATE_CLOSE)
    if opens != closes:
        return _refuse_result(
            result, 'refused',
            f"{path.name}'s ## {heading_text} has an unclosed <!-- private "
            '--> marker, so appending here could land the new text on the '
            f'wrong side of the redaction fence. Open {path}, add the '
            'matching <!-- /private --> (or remove the stray marker), then '
            'retry. Nothing was written.')

    new_full_text = '\n'.join(new_lines)

    if dry_run:
        result.data['status'] = 'dry-run'
        if created:
            result.add('info',
                       f'[dry-run] Would create ## {heading_text} in {path.name} '
                       'and add the note.')
        else:
            result.add('info',
                       f'[dry-run] Would append a paragraph to ## {heading_text} '
                       f'in {path.name}.')
        for dline in difflib.unified_diff(
            old_text.splitlines(), new_full_text.splitlines(),
            fromfile=f'{path} (before)', tofile=f'{path} (after)', lineterm='',
        ):
            result.add('info', dline)
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    try:
        write_text_exact(path, reapply_newline(new_full_text, old_text))
    except OSError as e:
        return _refuse_result(
            result, 'refused',
            f'cannot write {path}: {e}. Check the file is not open elsewhere '
            'and the folder is writable, then retry.')

    result.data['status'] = 'ok'
    result.note_changed(path)
    if created:
        result.add('info',
                   f'Created ## {heading_text} in {label} and added the note.', path=path)
    else:
        result.add('info', f'Added a note to ## {heading_text} in {label}.', path=path)
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _emit(result: Result) -> int:
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def _cmd_new(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha person new')
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_new(
        archive_root, name=args.name, sex=args.sex, gender=args.gender,
        birth=args.birth, death=args.death,
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_set_living(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha person set-living')
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_set_living(
        archive_root, person_id=args.person_id, value=args.value,
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_relate(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha person relate')
    if archive_root is None:
        return EXIT_FAILURE
    relation_type = target_id = None
    for rel in RELATION_TYPES:
        value = getattr(args, rel, None)
        if value:
            relation_type, target_id = rel, value
            break
    if relation_type is None:
        # argparse's required mutually-exclusive group should make this
        # unreachable; kept as a plain refusal rather than a crash in case a
        # headless caller builds a Namespace by hand.
        print('ERROR: pick exactly one of --parent, --child, --sibling, --spouse.',
              file=sys.stderr)
        return EXIT_FAILURE
    return _emit(run_relate(
        archive_root, person_id=args.person_id, relation_type=relation_type,
        target_id=target_id, subtype=args.subtype, reciprocal=bool(args.reciprocal),
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_estimate(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha person estimate')
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_estimate(
        archive_root, person_id=args.person_id, birth=args.birth, death=args.death,
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_edit(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha person edit')
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_edit(
        archive_root, person_id=args.person_id, section=args.section,
        text=args.text, file_path=args.file, append=bool(args.append),
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_note(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha person note')
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_note(
        archive_root, person_id=args.person_id, section=args.section,
        text=args.text, dry_run=bool(getattr(args, 'dry_run', False))))


def _make_group_help(parser: argparse.ArgumentParser):
    """Bare `fha person` prints the group help and exits 2 (a verb is required)."""
    def _cmd(args: argparse.Namespace) -> int:
        parser.print_help()
        return 2
    return _cmd


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Update a person's record directly - the deterministic person-field write-backs.

  fha person new "Full Name" [--sex M|F|intersex|unknown] [--gender TEXT]
  fha person set-living <P-id> true|false|unknown
  fha person relate <P-id> --parent|--child|--sibling|--spouse <P-id2>
  fha person estimate <P-id> --birth DATE --death DATE
  fha person edit <P-id> --section biography|stories|research --text TEXT
  fha person note <P-id> --section stories|research --text TEXT

new mints a brand-new person and writes their stub record. set-living marks
a person as living, passed away, or unknown (drives export privacy). relate
jots an unsourced family-tie belief. estimate writes a provisional, unsourced
birth/death date. edit and note add or replace prose in the curated
profile's Biography, Stories, and Research Notes sections."""

_NEW_DESCRIPTION = """\
Mint a brand-new person - the one-command "+ add person" mint.

  fha person new "Jane Doe"
  fha person new "Jane Doe" --sex F --birth 1870 --death 1940
  fha person new "Cortez" --gender "two-spirit"

Mints one P-id, writes a stub under people/stubs/, and reports where it
landed. --sex accepts M, F, intersex, or unknown (case-insensitive for M/F).
--gender is free text - omit it unless there is something to record.
--birth/--death record PROVISIONAL, unsourced estimates: the archive's exact
date form (1870, 1870-06) or plain words ("circa 1870", "before 1940") -
loose wording is translated for you. A sourced birth/death claim always
supersedes these later (`fha claim`). Use `fha person relate` next to tie
the new person to family, and `fha find` to confirm the record."""

_SET_LIVING_DESCRIPTION = """\
Mark one person as living, passed away, or unknown - the privacy switch.

  fha person set-living P-2b3c4d5e6f false    Passed away - exports may include them
  fha person set-living P-2b3c4d5e6f true     Living - redacted from every export
  fha person set-living P-2b3c4d5e6f unknown  Not sure - treated as living (safe default)

Changes exactly one line of the person's record and touches nothing else.
Preview the change first with --dry-run."""


def _add_new_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the new verb on a group subparser (shared by both mains)."""
    nw = sub.add_parser(
        'new',
        help='Mint a brand-new person and write their stub record.',
        description=_NEW_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    nw.add_argument('name', metavar='NAME',
                    help='The person\'s full name, e.g. "Jane Doe".')
    # No choices= here (unlike set-living's `value`): an unrecognised sex
    # should refuse with run_new's plain, gender-glossed message
    # (_lib.format_person_sex_error), not argparse's bare "invalid choice" text.
    nw.add_argument('--sex', metavar='M|F|intersex|unknown',
                    help='Birth-assigned sex, if known (case-insensitive for M/F).')
    nw.add_argument('--gender', metavar='TEXT',
                    help='Free-text gender/identity - omit unless there is something to record.')
    nw.add_argument('--birth', metavar='DATE',
                    help='A provisional, unsourced birth date or estimate.')
    nw.add_argument('--death', metavar='DATE',
                    help='A provisional, unsourced death date or estimate.')
    nw.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                    help='Archive root (auto-detected if omitted).')
    nw.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the new record; write and mint nothing persistent.')
    nw.set_defaults(func=_cmd_new)


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


_RELATE_DESCRIPTION = """\
Jot an unsourced family-tie belief - always recorded as a hypothesis.

  fha person relate P-2b3c4d5e6f --parent P-de957bcda1
  fha person relate P-2b3c4d5e6f --spouse P-cd795c61e0 --reciprocal
  fha person relate P-2b3c4d5e6f --child P-c4b26bb4bc --subtype adoptive

Appends one entry to the first person's relationships: list, naming the
second person and the role (parent/child/sibling/spouse). --subtype names
the nature of the bond (biological is the default; SPEC §8.2 also lists
adoptive, step, foster, guardian, and more). --reciprocal also writes the
mirrored entry on the second person (parent<->child flips; sibling/spouse
mirror as themselves). This is always an unsourced belief - a sourced tie
comes from an accepted relationship claim, filed with `fha claim`."""


def _add_relate_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the relate verb on a group subparser (shared by both mains)."""
    rl = sub.add_parser(
        'relate',
        help='Jot an unsourced family-tie belief (parent/child/sibling/spouse).',
        description=_RELATE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    rl.add_argument('person_id', metavar='P-id',
                    help='The person whose relationships: entry gets the new tie.')
    group = rl.add_mutually_exclusive_group(required=True)
    group.add_argument('--parent', metavar='P-id2',
                       help='P-id2 is this person\'s parent.')
    group.add_argument('--child', metavar='P-id2',
                       help='P-id2 is this person\'s child.')
    group.add_argument('--sibling', metavar='P-id2',
                       help='P-id2 is this person\'s sibling.')
    group.add_argument('--spouse', metavar='P-id2',
                       help='P-id2 is this person\'s spouse.')
    rl.add_argument('--subtype', metavar='WORD',
                    help='The nature of the bond (SPEC §8.2), e.g. biological '
                         '(default), adoptive, step, foster, guardian.')
    rl.add_argument('--reciprocal', action='store_true',
                    help="Also write the mirrored entry on P-id2's record.")
    rl.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                    help='Archive root (auto-detected if omitted).')
    rl.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the change(s) without writing.')
    rl.set_defaults(func=_cmd_relate)


_ESTIMATE_DESCRIPTION = """\
Write a provisional, unsourced birth/death estimate - a starting guess.

  fha person estimate P-2b3c4d5e6f --birth 1870
  fha person estimate P-2b3c4d5e6f --birth "circa 1870" --death "before 1940"
  fha person estimate P-2b3c4d5e6f --birth -             Clear the birth estimate

DATE accepts the archive's exact date form (1870, 1870-06, 188X for "the
1880s") or plain words ("circa 1870", "before 1940", "the 1880s") - loose
wording is translated for you. Use - by itself to clear a field. A sourced
birth/death claim always supersedes this estimate; run `fha claim` once you
have a source. At least one of --birth/--death is required."""


def _add_estimate_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the estimate verb on a group subparser (shared by both mains)."""
    es = sub.add_parser(
        'estimate',
        help='Write a provisional, unsourced birth/death estimate.',
        description=_ESTIMATE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    es.add_argument('person_id', metavar='P-id',
                    help='The person to update (e.g. P-2b3c4d5e6f).')
    es.add_argument('--birth', metavar='DATE',
                    help='A birth date/estimate, or - to clear it.')
    es.add_argument('--death', metavar='DATE',
                    help='A death date/estimate, or - to clear it.')
    es.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                    help='Archive root (auto-detected if omitted).')
    es.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the change without writing.')
    es.set_defaults(func=_cmd_estimate)


_EDIT_DESCRIPTION = """\
Replace (or append to) one prose section of a curated profile.

  fha person edit P-2b3c4d5e6f --section biography --text "..."
  fha person edit P-2b3c4d5e6f --section stories --file story.txt --append

--section picks the section: biography (## Biography), stories
(## Stories), or research (## Research Notes). Give the text with --text or
--file (exactly one - no reading from standard input). Replaces the whole
section by default; --append adds the text as a new paragraph at the end
instead. The section is created if it does not exist yet. Frontmatter and
every other section are left byte-for-byte alone."""


def _add_edit_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the edit verb on a group subparser (shared by both mains)."""
    ed = sub.add_parser(
        'edit',
        help='Replace or append to a curated profile section (Biography/Stories/Research Notes).',
        description=_EDIT_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ed.add_argument('person_id', metavar='P-id',
                    help='The person whose profile gets edited.')
    ed.add_argument('--section', required=True,
                    choices=('biography', 'stories', 'research'),
                    help='Which section to edit.')
    group = ed.add_mutually_exclusive_group(required=True)
    group.add_argument('--text', metavar='TEXT', help='The prose to write.')
    group.add_argument('--file', metavar='PATH', help='A file already holding the prose.')
    ed.add_argument('--append', action='store_true',
                    help='Add as a new paragraph at the end instead of replacing.')
    ed.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                    help='Archive root (auto-detected if omitted).')
    ed.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the change without writing.')
    ed.set_defaults(func=_cmd_edit)


_NOTE_DESCRIPTION = """\
Append a quick note to Stories or Research Notes - never replaces anything.

  fha person note P-2b3c4d5e6f --section research --text "Check the 1880 census."

Adds TEXT as a new paragraph at the end of the section (creating it if
missing). Plain markdown, human-written - existing text is never rewritten
or removed. Refuses if the section's <!-- private --> fencing is left
unclosed, since it could not tell which side of it the new text belongs on."""


def _add_note_arguments(sub: argparse._SubParsersAction) -> None:
    """Register the note verb on a group subparser (shared by both mains)."""
    nt = sub.add_parser(
        'note',
        help='Append a quick note to Stories or Research Notes.',
        description=_NOTE_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    nt.add_argument('person_id', metavar='P-id',
                    help='The person whose profile gets the note.')
    nt.add_argument('--section', required=True, choices=('stories', 'research'),
                    help='Which section to append to.')
    nt.add_argument('--text', metavar='TEXT', required=True, help='The note to add.')
    nt.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                    help='Archive root (auto-detected if omitted).')
    nt.add_argument('--dry-run', action='store_true', dest='dry_run',
                    help='Preview the change without writing.')
    nt.set_defaults(func=_cmd_note)


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'person' onto the main fha parser."""
    p = subs.add_parser(
        'person',
        help='Person-record write-backs: set-living, relate, estimate, edit, note',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = p.add_subparsers(dest='person_command', metavar='SUBCOMMAND')
    _add_new_arguments(sub)
    _add_set_living_arguments(sub)
    _add_relate_arguments(sub)
    _add_estimate_arguments(sub)
    _add_edit_arguments(sub)
    _add_note_arguments(sub)
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
    _add_new_arguments(sub)
    _add_set_living_arguments(sub)
    _add_relate_arguments(sub)
    _add_estimate_arguments(sub)
    _add_edit_arguments(sub)
    _add_note_arguments(sub)
    parser.set_defaults(func=_make_group_help(parser))
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == '__main__':
    sys.exit(_standalone_main())
