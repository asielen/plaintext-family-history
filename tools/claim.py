#!/usr/bin/env python3
"""
claim.py - fha claim: human-directed claim review + minting (AGENTS.md, SPEC §8).

  fha claim <C-id> --status accepted|disputed|rejected|needs-review|superseded
                   [--reviewed DATE] [--value "…"] [--date EDTF]
                   [--type TYPE] [--place L-id | --place-text TEXT]
                   [--persons P-id[,P-id...]] [--confidence high|medium|low]
                   [--root PATH] [--dry-run]

  fha claim new --source S-id --type TYPE --value TEXT [--date DATE]
                [--place L-id | --place-text TEXT] [--persons P-id[,P-id...]]
                [--subtype WORD] [--status suggested|accepted]
                [--confidence high|medium|low] [--dry-run] [--root PATH]

Two verbs share one file because both are surgical `## Claims` block writers on
an existing source record, and the second (`new`) was built by lifting the
first's machinery rather than inventing a second way to edit the block.

**`fha claim <C-id>`** is the deterministic write-back a human reaches for
during review: moving one claim's `status:` out of `suggested` into any of the
SPEC §8.1 review outcomes (`accepted` / `disputed` / `rejected` / `needs-review`
/ `superseded`), and now also correcting any of `value`/`date`/`type`/`place`/
`place_text`/`persons` on a claim that already exists. Today that move is done
by hand-editing the `## Claims` YAML or through the `review-claims` AI skill;
this tool makes it a real, contract-safe CLI action that any front door (chat
now, a UI later) can drive.

**Contract (AGENTS.md / SPEC §8.2):** only the *human* moves a claim to
`accepted`, and an accepted claim must carry a `reviewed:` date (lint E006). The
human satisfies that contract by *directing* this tool - the editing method does
not matter, only that the decision is theirs. So `--status accepted` always
writes a `reviewed:` stamp: the explicit `--reviewed DATE` when given, otherwise
today (forgiving, since a human is at the keyboard). The tool never accepts on
its own. `disputed` / `rejected` / `superseded` change status rather than
deleting, so the research trail is preserved - `disputed` marks a claim that is
actively contested (e.g. a `contradicts:` standoff) rather than settled either
way, distinct from a `rejected` claim the reviewer has ruled out.

**Compatibility change (2026-07, deliberate):** `--status` is now OPTIONAL on
the edit verb. Before this build every call had to move the status; now a
field-only correction (`fha claim <C-id> --place L-baba9801fa`) is legal on its
own, and at least one mutation flag (`--status`/`--value`/`--date`/`--type`/
`--place`/`--place-text`/`--persons`) is required so a bare `fha claim <C-id>`
with nothing to do is refused rather than silently doing nothing. `reviewed:`
is stamped only when `--status` is given, exactly as before - a field-only edit
never touches status or reviewed.

The edit is **surgical**: only the one named claim's entry inside its source
`.md` `## Claims` block is touched - its sibling claims, the block's key order,
and any hand comments are preserved - mirroring `places geocode`'s surgical
`places.yaml` edit and `process --more`'s frontmatter edit. The claim is located
by scanning the `sources/` records directly (the `.md` files are the truth), so
the command works even when `.cache/index.sqlite` is stale or absent. After a
write, re-run `fha index` so the new status enters the query surface.

**`fha claim new`** mints a brand-new claim onto an EXISTING source record - the
CLI-driven counterpart to hand-typing a claim item under `## Claims`. It shares
`_lib.append_claim_to_source` with `fha confirm cooccur` (both mint a claim and
append it to a source's block) rather than a third copy of that machinery.

**Deliberate scope line: no `--type relationship` here, ever - neither to mint
one with `claim new` nor to change an existing claim TO relationship with
`--type` on the edit verb.** SPEC §8.3 requires a `roles:` map on every
relationship claim, and this file's writers only ever set flat scalar keys - no
writer here builds `roles:`. The two sanctioned paths for an actual
relationship tie are unaffected: a hypothesis tie between two people goes
through `fha person relate`; a sourced tie discovered via shared sources goes
through `fha confirm cooccur` (which DOES build the `roles:` map, in its own
file, for exactly this reason). Anything else - a relationship needing a role
vocabulary these two verbs don't cover - is a hand-edit of the claim block,
same as always.

CODE MAP
--------
  Shared
    _today                    - review-stamp default (overridable in tests)
    _ClaimEditRefused         - alias of _lib.ClaimEditRefused (shared with fha confirm)
    _fail / _notfound         - small Result-builders (mirrors confirm.py's helpers)
    _claim_type_problem       - unknown-type / relationship-refusal gate (shared by both verbs)
    _invalid_person_ids       - which --persons entries are not P-id shaped
    _unresolvable_person_ids  - which (shape-valid) P-ids have no person record
    _place_known_in_index     - best-effort L-id existence check (warns, never refuses)

  fha claim <C-id> (review + field edit)
    _find_claim_record        - scan sources/ for the .md holding one C-id
    _own_key_indent / _own_id_key_line - which id: line is an item's OWN key
    _apply_claim_review       - surgical `## Claims` block edit (status/value/date/type/place/persons)
    run_claim                 - validate, locate, edit, return a Result
    _cmd_claim

  fha claim new (mint onto an existing source)
    _render_new_claim_lines   - build one new claim item's YAML lines (SPEC §8.4 field order)
    run_claim_new             - validate, locate source, mint id, append, return a Result
    _cmd_claim_new

  CLI wiring
    _add_arguments / _add_new_arguments
    register / build_claim_new_parser / _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from _lib import (
    CLAIM_TYPES,
    CONFIDENCE_VALUES,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    ClaimEditRefused,
    default_confidence,
    INDEX_SCHEMA_VERSION,
    Result,
    append_claim_to_source,
    claim_item_key_indent,
    claims_edit_problem,
    configure_utf8_stdout,
    find_source_record_path,
    fmt_id_display,
    format_edtf_error,
    id_type_of,
    is_valid_id,
    mint_ids,
    normalize_date,
    normalize_id,
    read_record,
    read_text_exact,
    reapply_newline,
    resolve_root_arg,
    scan_person_record_ids,
    sqlite_cache_schema_status,
    write_text_exact,
    yaml_inline,
)

configure_utf8_stdout()

# The review statuses a human can move a claim into (SPEC §8.1 lifecycle:
# `suggested → needs-review → accepted | disputed | rejected | superseded`).
# `suggested` is deliberately absent: the AI draft pass produces `suggested`;
# review only ever moves *out* of it, never back to it through this tool.
REVIEW_STATUSES = ('accepted', 'disputed', 'rejected', 'needs-review', 'superseded')

# The statuses `fha claim new` can mint a claim at (SPEC §8.1: suggested is the
# AI-draft entry point, accepted is the human-directed one - review only moves
# a claim OUT of suggested through the other statuses, so minting straight to
# disputed/rejected/needs-review/superseded makes no sense and stays refused).
NEW_CLAIM_STATUSES = ('suggested', 'accepted')

# The SHAPE of a claim's `id:` key line (optionally after the list dash).
# Shape alone is not ownership: a block scalar (`notes: |`) can quote an
# `id: C-...` line verbatim, so every consumer must also check the line sits at
# the item's own key column (`_own_id_key_line`). Mirrors confirm.py - KEEP IN SYNC.
_CLAIM_ID_KEY_RE = re.compile(
    r'^\s*(?:-\s+)?id:\s*(C-[0-9a-hjkmnp-tv-z]{10})\b', re.I
)


def _today() -> str:
    return datetime.date.today().isoformat()


def _fail(result: Result, status: str, message: str, *, next_step: str | None = None) -> Result:
    """Build a plain EXIT_FAILURE refusal onto `result` (mirrors confirm.py's `_fail`).

    Small enough, and needed by both `run_claim` and `run_claim_new`, that it
    is worth a local copy rather than reaching into confirm.py - tools never
    import tools (AGENTS_TOOLING.md)."""
    result.ok = False
    result.exit_code = EXIT_FAILURE
    result.data['status'] = status
    result.add('error', message, next_step=next_step)
    return result


def _notfound(result: Result, message: str, *, next_step: str | None = None) -> Result:
    """Build a plain EXIT_WARNINGS not-found onto `result` (mirrors confirm.py's `_notfound`)."""
    result.ok = False
    result.exit_code = EXIT_WARNINGS
    result.data['status'] = 'not-found'
    result.add('warning', message, next_step=next_step)
    return result


def _claim_type_problem(claim_type: str) -> str | None:
    """Validate a `--type` value; return a refusal message, or None if it's fine.

    Two distinct refusals share this one gate, used by both `run_claim_new`
    and `run_claim`'s `--type` field edit so both refuse identically: an
    unrecognised type (not in the SPEC §8.2 vocabulary) and the deliberately
    unsupported `relationship` type. `relationship` needs a `roles:` map
    (SPEC §8.3) that this file's flat-scalar writers do not build - see the
    module docstring's scope note for the two sanctioned paths instead.
    """
    if claim_type not in CLAIM_TYPES:
        return (f'{claim_type!r} is not a claim type. Use one of: '
                f'{", ".join(sorted(CLAIM_TYPES))}.')
    if claim_type == 'relationship':
        return (
            'fha claim does not create or change a claim to type relationship - it needs '
            'a roles: map (SPEC §8.3) this tool does not build. For a hypothesis tie between '
            'two people, use `fha person relate`; for a sourced tie discovered from shared '
            'sources, use `fha confirm cooccur`; otherwise open the source file and add the '
            'roles: map by hand under ## Claims.'
        )
    return None


def _invalid_person_ids(raw_ids: list[str]) -> list[str]:
    """Return the entries of `raw_ids` that are not shaped like a P-id."""
    return [p for p in raw_ids if not (is_valid_id(p) and id_type_of(p) == 'P')]


def _unresolvable_person_ids(archive_root: Path, ids: list[str]) -> list[str]:
    """Return the (already shape-valid) ids in `ids` with no person record file.

    SPEC §9: every referenced P-id must resolve to at least a stub. `persons:`
    is the load-bearing cross-link on a claim, so writing one that names
    nobody is worse than writing none - it leaves an E005 broken reference for
    lint to find later instead of a plain refusal now. Returned display-cased
    (`fmt_id_display`) since the only use is a human-facing message.
    """
    known = {normalize_id(p) for p in scan_person_record_ids(archive_root)}
    return [fmt_id_display(normalize_id(p)) for p in ids if normalize_id(p) not in known]


def _place_known_in_index(archive_root: Path, place_id: str) -> bool:
    """Best-effort check: does `place_id` appear in `.cache/index.sqlite`'s places table?

    Existence is checked against the INDEX, not `places/places.yaml` directly,
    because this is a deliberately soft, forgiving check (AGENTS.md: "forgiving,
    not fussy") - a freshly-registered place may not be reindexed yet, so a miss
    here becomes a WARNING the caller adds, never a refusal. Returns False both
    when the index genuinely lacks the id and when the index can't be trusted at
    all (missing, corrupt, wrong schema) - the caller's warning wording already
    covers both readings ("it may be new, or the index may be stale"), so the
    distinction does not need to leak out through this return value.
    """
    db_path = archive_root / '.cache' / 'index.sqlite'
    status, _detail = sqlite_cache_schema_status(db_path, INDEX_SCHEMA_VERSION, ('places',))
    if status != 'fresh':
        return False
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.DatabaseError:
        return False
    try:
        row = conn.execute('SELECT 1 FROM places WHERE id=?', (normalize_id(place_id),)).fetchone()
        return row is not None
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


# Thin alias: the exception type itself lives in `_lib.ClaimEditRefused` -
# lifted there so `fha claim new`'s call into `_lib.append_claim_to_source`
# (which raises that shared type) can be caught with the same name this file's
# own `_apply_claim_review` refusals already use. Kept as a module-level name
# here so every existing `except _ClaimEditRefused` site, and the tests that
# assert on `claim._ClaimEditRefused`, are unchanged.
_ClaimEditRefused = ClaimEditRefused


# ── Locate the claim's source record ──────────────────────────────────────────

def _find_claim_record(archive_root: Path, claim_id: str) -> tuple[Path, dict] | None:
    """Scan `sources/` for the record whose `## Claims` block holds `claim_id`.

    Returns `(path, claim_dict)` for the first match, or None. The `.md` files
    are archive truth, so this never consults `.cache/index.sqlite` - the write
    must work even when the index is stale or has never been built. Records that
    fail to parse are skipped (the claim isn't in an unparseable block, and lint
    is the tool that reports the parse error).
    """
    target = normalize_id(claim_id)
    sources_dir = archive_root / 'sources'
    if not sources_dir.is_dir():
        return None
    for path in sorted(sources_dir.rglob('*.md')):
        try:
            rec = read_record(path)
        except Exception:  # noqa: BLE001 - a bad record can't hold our claim
            continue
        for claim in rec.get('claims') or []:
            if not isinstance(claim, dict):
                continue
            cid = claim.get('id')
            if cid and normalize_id(str(cid)) == target:
                return path, claim
    return None


# ── Surgical edit of the one claim's YAML entry ───────────────────────────────

def _own_key_indent(item_lines: list[str], base_indent: str) -> str | None:
    """The exact column of one claim item's OWN mapping keys, or None.

    Sibling of `_lib.claim_item_key_indent`, with one deliberate difference:
    no conventional fallback. The write path wants a best-effort column to
    place a new key at (a wrong guess there is caught by the pre-write
    guard), but *ownership* testing wants certainty - a guessed column could
    bless a look-alike line inside quoted scalar content, which is exactly
    the wrong-claim edit this check exists to prevent. So: an inline first
    key on the dash line pins the column; else the first content line after
    the dash does; else the column is unknowable and the item owns nothing.
    Mirrors confirm.py - KEEP IN SYNC.
    """
    first = item_lines[0] if item_lines else ''
    m = re.match(r'^' + re.escape(base_indent) + r'(-[ ]+)[^\s#]', first)
    if m:
        return base_indent + ' ' * len(m.group(1))
    for ln in item_lines[1:]:
        stripped = ln.strip()
        if not stripped or stripped.startswith('#'):
            continue
        indent = re.match(r'^(\s*)', ln).group(1)
        if len(indent) > len(base_indent):
            return indent
        break  # content at or above the dash's own column belongs to no key of this item
    return None


def _own_id_key_line(
    lines: list[str], start: int, end: int, base_indent: str,
) -> tuple[int, str] | None:
    """Find one item's OWN `id:` mapping key; return (line_index, C-id) or None.

    `_CLAIM_ID_KEY_RE` describes the shape of an id key line, but a block
    scalar (`notes: |`) may quote such a line verbatim, and quoted lines sit
    DEEPER than the item's real key column (YAML requires it). Only two
    placements are the item's own key: the inline first key on the dash line
    (`- id: C-...` at the item's dash) and a line at exactly the item's key
    column (`_own_key_indent`). Matching on shape alone edited the first item
    whose quoted evidence mentioned the target id - a wrong refusal here (the
    status guard caught it), a wrong WRITE in confirm.py's twin.
    Mirrors confirm.py - KEEP IN SYNC.
    """
    key_indent = _own_key_indent(lines[start:end], base_indent)
    dash_id_re = re.compile(r'^' + re.escape(base_indent) + r'-\s+id:', re.I)
    key_id_re = None
    if key_indent is not None:
        key_id_re = re.compile(r'^' + re.escape(key_indent) + r'id:', re.I)
    for j in range(start, end):
        m = _CLAIM_ID_KEY_RE.match(lines[j])
        if not m:
            continue
        if j == start:
            if dash_id_re.match(lines[j]):
                return j, m.group(1)
        elif key_id_re is not None and key_id_re.match(lines[j]):
            return j, m.group(1)
    return None


def _apply_claim_review(
    text: str,
    claim_id: str,
    *,
    status: str | None = None,
    reviewed: str | None = None,
    value: str | None = None,
    date: str | None = None,
    type_: str | None = None,
    place: str | None = None,
    place_text: str | None = None,
    persons: list[str] | None = None,
    confidence: str | None = None,
) -> tuple[str, bool]:
    """Surgically set any of status/reviewed/value/date/type/place/persons on one claim.

    Only the claim whose `id:` matches `claim_id` (case-insensitive) inside the
    `## Claims` fenced YAML block is touched; every other line - sibling claims,
    key order, comments - is preserved. Returns `(new_text, changed)`; `changed`
    is False when the block or the claim isn't found (the caller reports a clean
    not-found rather than guessing).

    `status` is optional (2026-07 compatibility change): the caller (`run_claim`)
    guarantees at least one field is being set, but that field need not be
    status any more - a plain field correction is legal on its own. When
    `status` is None the existing `status:` line is located read-only, purely
    to anchor where any NEW keys land (a claim always has one; 0, the item's
    own first line, is the safe fallback if it somehow does not). `reviewed`
    is only ever passed together with `status` - the caller enforces that, so
    this function does not need to.

    `place`/`place_text` are mutually exclusive in a well-formed claim (the
    caller validates that); setting one here removes the other if present, so
    `--place` after an existing `place_text:` leaves the block with exactly
    one place key, never both. `persons` REPLACES the whole `persons:` list
    (not append) - the caller pre-validates every P-id resolves to a record.

    Edits land at the item's OWN key column, derived from its lines
    (`claim_item_key_indent`): a claim legally written `-   value: farmer` keeps
    its keys at column 4, and writing at the conventional column 2 there would
    break the whole block's YAML. The caller re-parses the result before any
    write (`claims_edit_problem`), so a rewrite this function gets wrong is
    refused rather than saved.

    The claim is located by its OWN `id:` key line (`_own_id_key_line`), never
    by an id merely mentioned in its text - a block scalar quoting an
    `id: C-...` line used to draw the edit onto the quoting claim instead.

    Raises `_ClaimEditRefused` when an edit can't be made without risking
    corruption: `--value` against a multi-line block scalar (`value: >` /
    `value: |`), which a human edits by hand, and the belt-and-braces case
    where the located entry does not read back as the target claim when the
    block is parsed.
    """
    target = normalize_id(claim_id)
    lines = text.splitlines()

    # 1. Locate the `## Claims` fenced YAML block.
    heading = None
    for i, ln in enumerate(lines):
        if re.match(r'^##\s+Claims\b', ln):
            heading = i
            break
    if heading is None:
        return text, False

    open_fence = None
    for i in range(heading + 1, len(lines)):
        if lines[i].strip() == '```yaml':
            open_fence = i
            break
        if lines[i].startswith('## '):  # next section before any fence
            return text, False
    if open_fence is None:
        return text, False

    close_fence = None
    for i in range(open_fence + 1, len(lines)):
        if lines[i].strip() == '```':
            close_fence = i
            break
    if close_fence is None:
        return text, False

    content_start, content_end = open_fence + 1, close_fence

    # 2. Split the block into top-level claim items (dashes at the base indent).
    dash_lines = [
        i for i in range(content_start, content_end)
        if re.match(r'^(\s*)-\s', lines[i])
    ]
    if not dash_lines:
        return text, False
    base_indent = re.match(r'^(\s*)-', lines[dash_lines[0]]).group(1)
    item_starts = [
        i for i in dash_lines
        if re.match(r'^' + re.escape(base_indent) + r'-\s', lines[i])
    ]
    bounds = item_starts + [content_end]

    target_span = None
    span_index = None
    for k, start in enumerate(item_starts):
        end = bounds[k + 1]
        own = _own_id_key_line(lines, start, end, base_indent)
        if own is not None and normalize_id(own[1]) == target:
            target_span = (start, end)
            span_index = k
            break
    if target_span is None:
        return text, False

    # Belt and braces: the k-th top-level dash is the k-th parsed list item,
    # so the parsed item's `id` must equal the target. A mismatch means the
    # line-level read and YAML disagree about which claim this is - the edit
    # has no safe landing place, so refuse rather than touch the wrong claim.
    try:
        parsed_items = yaml.safe_load('\n'.join(lines[content_start:content_end]))
    except yaml.YAMLError:
        parsed_items = None
    aligned = (
        isinstance(parsed_items, list) and len(parsed_items) == len(item_starts)
        and isinstance(parsed_items[span_index], dict)
        and normalize_id(str(parsed_items[span_index].get('id') or '')) == target
    )
    if not aligned:
        raise _ClaimEditRefused(
            f'the entry carrying the id line for {fmt_id_display(target)} does not '
            'read back as that claim when the block is parsed, so the edit has no '
            'safe landing place. Open the file, make the change by hand under '
            '## Claims, then run `fha lint` to check it.'
        )

    start, end = target_span
    item = lines[start:end]
    # The item's real key column comes from its own lines, never from a fixed
    # base_indent+2 assumption - see claim_item_key_indent for the why. The
    # dash prefix mirrors it so a first-key rewrite keeps the item's column.
    key_indent = claim_item_key_indent(item, base_indent)
    dash_prefix = base_indent + '-' + ' ' * max(1, len(key_indent) - len(base_indent) - 1)

    def find_key(key: str) -> tuple[int | None, str | None, str | None]:
        """Return (index, kind, raw-value) of a top-level item key, or (None, …)."""
        dash_re = re.compile(r'^' + re.escape(base_indent) + r'-\s+' + re.escape(key) + r':\s*(.*)$')
        key_re = re.compile(r'^' + re.escape(key_indent) + re.escape(key) + r':\s*(.*)$')
        for idx, ln in enumerate(item):
            m = dash_re.match(ln)
            if m:
                return idx, 'dash', m.group(1)
            m = key_re.match(ln)
            if m:
                return idx, 'key', m.group(1)
        return None, None, None

    def set_scalar(key: str, value_text: str, insert_after: int) -> int:
        idx, kind, _ = find_key(key)
        if idx is not None:
            if kind == 'dash':
                item[idx] = f'{dash_prefix}{key}: {value_text}'
            else:
                item[idx] = f'{key_indent}{key}: {value_text}'
            return idx
        pos = insert_after + 1
        item.insert(pos, f'{key_indent}{key}: {value_text}')
        return pos

    def remove_key(key: str) -> None:
        """Delete one top-level item key entirely (dash or indented form), if
        present. Used only for the place/place_text switch below; a claim
        legally has `value:` (never place/place_text) as its first/dash key,
        so this never removes the dash line in practice - and if a hand-edited
        claim somehow did put place/place_text there, the pre-write guard
        (`claims_edit_problem`, run by the caller) would catch the resulting
        malformed block and refuse rather than save it."""
        idx, _kind, _ = find_key(key)
        if idx is not None:
            del item[idx]

    # 3. status (required on every valid claim). status is now OPTIONAL here
    # (2026-07 compat change: a field-only edit is legal on its own) - when
    # given, it is replaced in place as before; when not, the EXISTING status
    # line is located read-only, purely as the anchor other new keys insert
    # after (0, the item's own first line, is the safe fallback).
    if status is not None:
        status_idx = set_scalar('status', status, insert_after=0)
    else:
        found_status_idx, _found_kind, _ = find_key('status')
        status_idx = found_status_idx if found_status_idx is not None else 0

    anchor = status_idx
    if reviewed is not None:
        anchor = set_scalar('reviewed', reviewed, insert_after=status_idx)
    if date is not None:
        date_idx, date_kind, _ = find_key('date')
        if date_idx is not None:
            set_scalar('date', date, insert_after=date_idx)
        else:
            anchor = set_scalar('date', date, insert_after=anchor)
    if value is not None:
        _, _, raw = find_key('value')
        if raw is not None and (raw == '' or raw[:1] in ('>', '|')):
            raise _ClaimEditRefused(
                f'{fmt_id_display(target)} has a multi-line block-scalar value; '
                'edit the value by hand to avoid corrupting it.'
            )
        set_scalar('value', _yaml_inline(value), insert_after=status_idx)
    if type_ is not None:
        type_idx, _type_kind, _ = find_key('type')
        if type_idx is not None:
            set_scalar('type', type_, insert_after=type_idx)
        else:
            anchor = set_scalar('type', type_, insert_after=anchor)
    if place is not None:
        remove_key('place_text')
        place_idx, _place_kind, _ = find_key('place')
        if place_idx is not None:
            set_scalar('place', place, insert_after=place_idx)
        else:
            anchor = set_scalar('place', place, insert_after=anchor)
    if place_text is not None:
        remove_key('place')
        pt_idx, _pt_kind, _ = find_key('place_text')
        if pt_idx is not None:
            set_scalar('place_text', _yaml_inline(place_text), insert_after=pt_idx)
        else:
            anchor = set_scalar('place_text', _yaml_inline(place_text), insert_after=anchor)
    if persons is not None:
        rendered_persons = '[' + ', '.join(fmt_id_display(p) for p in persons) + ']'
        persons_idx, _persons_kind, _ = find_key('persons')
        if persons_idx is not None:
            set_scalar('persons', rendered_persons, insert_after=persons_idx)
        else:
            anchor = set_scalar('persons', rendered_persons, insert_after=anchor)
    if confidence is not None:
        conf_idx, _conf_kind, _ = find_key('confidence')
        if conf_idx is not None:
            set_scalar('confidence', confidence, insert_after=conf_idx)
        else:
            anchor = set_scalar('confidence', confidence, insert_after=anchor)

    new_lines = lines[:start] + item + lines[end:]
    trailing_nl = '\n' if text.endswith('\n') else ''
    return '\n'.join(new_lines) + trailing_nl, True


# Thin alias: the quoting rule itself lives in `_lib.yaml_inline` (shared by
# every surgical claim/frontmatter writer - see its docstring for the why).
# Kept as a module-level name here so existing call sites in this file (and
# any test importing `claim._yaml_inline`) do not need to change.
_yaml_inline = yaml_inline


# ── Top-level operation ───────────────────────────────────────────────────────

def run_claim(
    archive_root: Path,
    *,
    claim_id: str,
    status: str | None = None,
    reviewed: str | None = None,
    value: str | None = None,
    date: str | None = None,
    claim_type: str | None = None,
    place: str | None = None,
    place_text: str | None = None,
    persons: list[str] | None = None,
    confidence: str | None = None,
    dry_run: bool = False,
) -> Result:
    """Edit one claim (status move and/or field corrections); return a Result.

    `data` is {'status': 'ok'|'invalid-id'|'invalid-status'|'invalid-type'|
    'no-op'|'not-found'|'refused'|'failed', 'claim_id', 'before_status',
    'after_status', 'reviewed', 'source'}. On a real write the source `.md` is
    listed in `changed`; `--dry-run` previews the YAML change (a unified diff
    in the messages) and writes nothing. Before any write (or preview) the
    rewritten block is re-parsed (`claims_edit_problem`); a rewrite that would
    corrupt the block is a `refused` failure with nothing written, never a
    saved corruption - and when the problem predates the edit (the claim id
    already appears twice in the file, lint E001) the refusal names that
    repair instead of blaming the edit.

    `status` is OPTIONAL (2026-07 compatibility change - see the module
    docstring): at least one of status/value/date/claim_type/place/
    place_text/persons must be given, or the call is a plain `no-op` refusal.
    `reviewed:` is stamped only together with a `status` move, exactly as
    before - passing `reviewed` without `status` is refused rather than
    silently ignored, so a human is never left wondering why a date they typed
    did not land. `claim_type == 'relationship'` is refused (see
    `_claim_type_problem`); `place`/`place_text` are mutually exclusive and
    each replaces the other's key when switching; `persons` REPLACES the whole
    list and every P-id must already resolve to a record (SPEC §9) or the call
    refuses naming the missing id and the fix. The success message names the
    re-index next step (`fha index`).
    """
    result = Result(data={
        'status': None, 'claim_id': None, 'before_status': None,
        'after_status': status, 'reviewed': None, 'source': None,
    })

    # Validate the C-id shape before touching the archive - a malformed id is a
    # plain refusal, never a traceback.
    if not (is_valid_id(claim_id) and id_type_of(claim_id) == 'C'):
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'invalid-id'
        result.add('error',
                   f'{claim_id!r} is not a valid claim ID. C-ids look like C-fd0000001a - '
                   'a C followed by a dash and 10 characters from the archive alphabet.')
        return result

    cid = normalize_id(claim_id)
    result.data['claim_id'] = fmt_id_display(cid)

    if (status is None and value is None and date is None and claim_type is None
            and place is None and place_text is None and persons is None
            and confidence is None):
        return _fail(result, 'no-op',
                     'Nothing to change - pass at least one of --status, --value, --date, '
                     '--type, --place, --place-text, --persons, or --confidence.')

    if confidence is not None and confidence not in CONFIDENCE_VALUES:
        return _fail(result, 'failed',
                     f'{confidence!r} is not a confidence level. confidence records evidence '
                     'quality (separate from status, the review state) - use one of: '
                     f'{", ".join(CONFIDENCE_VALUES)} (SPEC §8.5).')

    if status is not None and status not in REVIEW_STATUSES:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'invalid-status'
        result.add('error',
                   f'{status!r} is not a review status. Use one of: {", ".join(REVIEW_STATUSES)}.')
        return result

    if reviewed is not None and status is None:
        return _fail(result, 'failed',
                     '--reviewed only takes effect together with --status (it stamps the '
                     'review date on the status you are setting). Add --status, or drop '
                     '--reviewed if you are only editing another field.')

    if place is not None and place_text is not None:
        return _fail(result, 'failed',
                     '--place and --place-text are mutually exclusive - use one or the other.')

    if claim_type is not None:
        problem = _claim_type_problem(claim_type)
        if problem is not None:
            return _fail(result, 'refused' if claim_type == 'relationship' else 'invalid-type',
                         problem)

    if place is not None and not (is_valid_id(place) and id_type_of(place) == 'L'):
        return _fail(result, 'failed',
                     f'--place {place!r} is not a valid place ID. L-ids look like '
                     'L-baba9801fa. For a place written a different way, use --place-text '
                     'instead.')

    persons_norm: list[str] | None = None
    if persons is not None:
        bad_shape = _invalid_person_ids(persons)
        if bad_shape:
            return _fail(result, 'failed',
                         'Not a valid person ID: ' + ', '.join(repr(p) for p in bad_shape)
                         + '. P-ids look like P-de957bcda1.')
        missing = _unresolvable_person_ids(archive_root, persons)
        if missing:
            return _notfound(
                result,
                'No person record for: ' + ', '.join(missing) + '. Mint a stub first with '
                '`fha person new "Name"`, or run `fha stubs --from-names` to create one for '
                'every unresolved name, then retry.',
                next_step='fha person new "Name"')
        persons_norm = [normalize_id(p) for p in persons]

    # `reviewed:` is the human-review stamp, stamped only together with a
    # --status move (the combination without --status was already refused
    # above). The contract *requires* it for an accepted claim (lint E006);
    # for every review move we record when it happened, defaulting to today
    # when the human didn't pass one.
    if status is not None:
        if reviewed is None:
            reviewed = _today()
        else:
            try:
                datetime.date.fromisoformat(reviewed)
            except ValueError:
                return _fail(result, 'failed',
                             f'--reviewed {reviewed!r} is not a calendar date. Use YYYY-MM-DD, '
                             f'e.g. {_today()}.')
    result.data['reviewed'] = reviewed

    normalized_date = None
    if date is not None:
        normalized_date = normalize_date(date)
        if normalized_date is None:
            result.ok = False
            result.exit_code = EXIT_FAILURE
            result.data['status'] = 'failed'
            result.add('error', '--date ' + format_edtf_error(date))
            return result

    found = _find_claim_record(archive_root, cid)
    if found is None:
        result.ok = False
        result.exit_code = EXIT_WARNINGS
        result.data['status'] = 'not-found'
        result.add('warning',
                   f'No claim {fmt_id_display(cid)} found in any source record under '
                   f'{archive_root / "sources"}.',
                   next_step='fha find ' + fmt_id_display(cid))
        return result

    record_path, claim = found
    before_status = str(claim.get('status') or '')
    result.data['before_status'] = before_status
    result.data['source'] = str(record_path)

    try:
        # Exact read/write so a one-line status edit doesn't churn every line
        # ending of a CRLF-authored record on Linux (or an LF one on Windows) -
        # the claims-surgery byte-faithful contract (read_text_exact docstring).
        text = read_text_exact(record_path)
    except OSError as e:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'failed'
        result.add('error', f'cannot read {record_path}: {e}')
        return result

    place_display = fmt_id_display(normalize_id(place)) if place is not None else None

    try:
        new_text, changed = _apply_claim_review(
            text, cid, status=status, reviewed=reviewed,
            value=value, date=normalized_date, type_=claim_type,
            place=place_display, place_text=place_text, persons=persons_norm,
            confidence=confidence,
        )
    except _ClaimEditRefused as e:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'refused'
        result.add('error', str(e))
        return result

    if not changed:
        # The record parsed with the claim but its raw `## Claims` text didn't -
        # an unusual shape lint should surface; refuse rather than guess.
        result.ok = False
        result.exit_code = EXIT_WARNINGS
        result.data['status'] = 'not-found'
        result.add('warning',
                   f'Found {fmt_id_display(cid)} in {record_path} but could not locate its '
                   'entry in the ## Claims block to edit. Check the block by hand.')
        return result

    # Pre-write guard: re-parse the rewritten block and refuse rather than save
    # text that would hide every claim in this source from lint/index/report.
    # Runs before the dry-run preview too, so preview and live run agree.
    # `expect_status=status` is fine when status is None - claims_edit_problem
    # skips that particular check whenever no status change was requested.
    problem = claims_edit_problem(new_text, cid, expect_status=status)
    if problem is not None:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'refused'
        if claims_edit_problem(text, cid) is not None:
            # The same check already fails on the UNEDITED text, so this edit
            # did not cause the problem. The only pre-existing state that can
            # reach this point is a duplicate of this claim id (finding the
            # claim required the block to parse and the id to be present), so
            # the honest advice is the duplicate-id repair, not a warning that
            # this edit would hide claims.
            result.add('error',
                       f'Refusing to change {fmt_id_display(cid)}: this claim id appears '
                       f'more than once in {record_path} - a duplicate-id problem (lint '
                       'E001) that predates this edit. Fix the duplicate first: open the '
                       'file, give one of those claims a fresh id (mint one with '
                       '`fha id mint C`), then retry. Nothing was written.')
        else:
            result.add('error',
                       f'Refusing to change {fmt_id_display(cid)}: {problem}, so saving would '
                       f'hide every claim in {record_path} from the tools. Nothing was written. '
                       'Open that file, edit the claim under ## Claims by hand, then run '
                       '`fha lint` to check it.')
        return result

    changed_bits = []
    if status is not None:
        changed_bits.append(f'status {before_status or "(none)"} -> {status} (reviewed {reviewed})')
    if value is not None:
        changed_bits.append('value updated')
    if normalized_date is not None:
        changed_bits.append(f'date -> {normalized_date}')
    if claim_type is not None:
        changed_bits.append(f'type -> {claim_type}')
    if place is not None:
        changed_bits.append(f'place -> {place_display}')
    if place_text is not None:
        changed_bits.append('place_text updated')
    if persons_norm is not None:
        changed_bits.append('persons -> [' + ', '.join(fmt_id_display(p) for p in persons_norm) + ']')
    if confidence is not None:
        changed_bits.append(f'confidence -> {confidence}')
    summary = f'{fmt_id_display(cid)}: ' + '; '.join(changed_bits)

    if dry_run:
        result.data['status'] = 'ok'
        diff = difflib.unified_diff(
            text.splitlines(), new_text.splitlines(),
            fromfile=f'{record_path} (before)', tofile=f'{record_path} (after)',
            lineterm='',
        )
        result.add('info', f'[dry-run] Would set {summary}')
        for dline in diff:
            result.add('info', dline)
        result.add('info', f'[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    try:
        write_text_exact(record_path, reapply_newline(new_text, text))
    except OSError as e:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'failed'
        result.add('error', f'cannot write {record_path}: {e}')
        return result

    result.data['status'] = 'ok'
    result.note_changed(record_path)
    result.add('info', f'Set {summary}', path=record_path)
    result.add('info', 'Reminder: run `fha index` so the new status enters the query surface.',
               next_step='fha index')
    return result


# ── Top-level operation: fha claim new ──────────────────────────────────────────

def _render_new_claim_lines(
    cid: str, claim_type: str, value: str, *,
    persons: list[str], date: str | None, place: str | None, place_text: str | None,
    subtype: str | None, status: str, reviewed: str | None, confidence: str,
) -> list[str]:
    """Build the YAML lines for one brand-new claim item (SPEC §8.4 field order).

    Field order mirrors SPEC §8.4's illustrative block (value, id, type,
    persons, date, place, status, confidence, reviewed, notes), with `subtype`
    moved into its natural slot right after `type` - SPEC lists subtype among
    the "other optional fields" at the end of the block, but it qualifies
    type, so a human skimming the item reads it more naturally there.

    `confidence` is always written: SPEC §8.5 marks it required on every claim
    (lint E010, the same required set as `persons`), and the same section
    directs tooling to DEFAULT it from source_type rather than leave it
    missing - `run_claim_new` resolves the default (`_lib.default_confidence`)
    or takes the human's --confidence override, so by the time this renderer
    runs the value is settled. `information`/`evidence`/`notes` are the
    SPEC-legal fields this verb deliberately leaves unset (lint only pings
    them informationally); a scope line kept tight to what `run_claim_new`'s
    signature actually takes.
    """
    lines = [
        f'- value: {_yaml_inline(value)}',
        f'  id: {fmt_id_display(cid)}',
        f'  type: {claim_type}',
    ]
    if subtype:
        lines.append(f'  subtype: {_yaml_inline(subtype)}')
    if persons:
        lines.append('  persons: [' + ', '.join(fmt_id_display(p) for p in persons) + ']')
    if date:
        lines.append(f'  date: {date}')
    if place:
        lines.append(f'  place: {fmt_id_display(normalize_id(place))}')
    elif place_text:
        lines.append(f'  place_text: {_yaml_inline(place_text)}')
    lines.append(f'  status: {status}')
    lines.append(f'  confidence: {confidence}')
    if status == 'accepted' and reviewed:
        lines.append(f'  reviewed: {reviewed}')
    return lines


def run_claim_new(
    archive_root: Path,
    *,
    source_id: str,
    claim_type: str,
    value: str,
    date: str | None = None,
    place: str | None = None,
    place_text: str | None = None,
    persons: list[str] | None = None,
    subtype: str | None = None,
    status: str = 'accepted',
    confidence: str | None = None,
    dry_run: bool = False,
) -> Result:
    """Mint a brand-new claim onto an existing source's `## Claims` block.

    `data` is {'status': 'ok'|'invalid-id'|'invalid-type'|'invalid-status'|
    'refused'|'failed'|'not-found', 'claim_id', 'source_id'}. On a real write
    the source `.md` is listed in `changed`; `--dry-run` previews the exact
    block that would be appended and writes nothing.

    Default `status='accepted'` is deliberate, and is NOT the same kind of
    optional as `run_claim`'s status (that one is optional because a
    field-only edit needs no status move at all): here a human typing this
    command IS the review gate AGENTS.md requires, so directing this tool at
    the default needs no extra ceremony - `reviewed:` is stamped to today
    automatically. An AI caller is bound by the same AGENTS.md contract to
    pass `status='suggested'` explicitly; this function cannot tell who is
    calling it, so the default assumes the human case and lets an automated
    caller opt out.

    `--type relationship` is refused (`_claim_type_problem`) - see the module
    docstring's scope note for the two sanctioned paths instead. `--persons`
    is OPTIONAL: a claim may legitimately be minted before its persons are
    linked (a source often names one, then more get attached during review),
    but the result carries a warning naming the follow-up command so the gap
    does not go unnoticed. `confidence` (required on every claim, SPEC §8.5)
    is defaulted from the source's `source_type` when not given - the
    spec-directed behavior ("Tooling defaults confidence from source_type") -
    with a message saying what was chosen and how to override.

    Every write funnels through `_lib.append_claim_to_source`, which raises
    `ClaimEditRefused` (caught here as `_ClaimEditRefused`) rather than ever
    saving a rewrite that would corrupt the block - the same insurance
    `run_confirm_cooccur` relies on for the identical append step.
    """
    result = Result(data={'status': None, 'claim_id': None, 'source_id': None})

    if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
        return _fail(result, 'invalid-id',
                     f'{source_id!r} is not a valid source ID. S-ids look like S-fa1234567b.')

    sid = normalize_id(source_id)
    result.data['source_id'] = fmt_id_display(sid)

    if not value or not value.strip():
        return _fail(result, 'failed',
                     '--value cannot be empty - a claim needs its human-readable summary.')

    problem = _claim_type_problem(claim_type)
    if problem is not None:
        return _fail(result, 'refused' if claim_type == 'relationship' else 'invalid-type',
                     problem)

    if status not in NEW_CLAIM_STATUSES:
        return _fail(result, 'invalid-status',
                     f'{status!r} is not a status `fha claim new` can mint at. Use one of: '
                     f'{", ".join(NEW_CLAIM_STATUSES)}. (Review moves a claim on to disputed/'
                     'rejected/needs-review/superseded later, with `fha claim <C-id> --status …`.)')

    if place is not None and place_text is not None:
        return _fail(result, 'failed',
                     '--place and --place-text are mutually exclusive - use one or the other.')

    if confidence is not None and confidence not in CONFIDENCE_VALUES:
        return _fail(result, 'failed',
                     f'{confidence!r} is not a confidence level. confidence records evidence '
                     'quality (separate from status, the review state) - use one of: '
                     f'{", ".join(CONFIDENCE_VALUES)} (SPEC §8.5).')

    normalized_date = None
    if date is not None:
        normalized_date = normalize_date(date)
        if normalized_date is None:
            return _fail(result, 'failed', '--date ' + format_edtf_error(date))

    if place is not None and not (is_valid_id(place) and id_type_of(place) == 'L'):
        return _fail(result, 'failed',
                     f'--place {place!r} is not a valid place ID. L-ids look like '
                     'L-baba9801fa. For a place written a different way, use --place-text '
                     'instead.')

    persons_norm: list[str] = []
    if persons:
        bad_shape = _invalid_person_ids(persons)
        if bad_shape:
            return _fail(result, 'failed',
                         'Not a valid person ID: ' + ', '.join(repr(p) for p in bad_shape)
                         + '. P-ids look like P-de957bcda1.')
        missing = _unresolvable_person_ids(archive_root, persons)
        if missing:
            return _notfound(
                result,
                'No person record for: ' + ', '.join(missing) + '. Mint a stub first with '
                '`fha person new "Name"`, or run `fha stubs --from-names` to create one for '
                'every unresolved name, then retry.',
                next_step='fha person new "Name"')
        persons_norm = [normalize_id(p) for p in persons]

    source_path = find_source_record_path(archive_root, sid)
    if source_path is None:
        return _notfound(
            result,
            f'No source record {fmt_id_display(sid)} found under {archive_root / "sources"}.',
            next_step='fha find ' + fmt_id_display(sid))

    cid = mint_ids('C', 1, archive_root)[0]
    result.data['claim_id'] = fmt_id_display(cid)

    # Confidence is required on every claim (SPEC §8.5) and the same section
    # directs tooling to default it from the source's type rather than leave
    # the field missing. Read the source's own record for its source_type; a
    # record that will not parse still gets the conservative 'medium'.
    if confidence is None:
        try:
            source_meta = read_record(source_path).get('meta') or {}
        except Exception:
            source_meta = {}
        source_type = source_meta.get('source_type')
        confidence = default_confidence(source_type)
        described = f'from source type {source_type!r}' if source_type else \
            'no source_type on the record, so the conservative middle'
        result.add('info',
                   f'confidence defaulted to {confidence} ({described} - SPEC §8.5 rubric). '
                   'Pass --confidence high|medium|low to override.')

    reviewed = _today() if status == 'accepted' else None
    item_lines = _render_new_claim_lines(
        cid, claim_type, value, persons=persons_norm, date=normalized_date,
        place=place, place_text=place_text, subtype=subtype, status=status, reviewed=reviewed,
        confidence=confidence,
    )

    try:
        before = read_text_exact(source_path)
    except OSError as e:
        return _fail(result, 'failed', f'cannot read {source_path}: {e}')

    try:
        after, changed = append_claim_to_source(before, item_lines)
    except ClaimEditRefused as e:
        return _fail(result, 'refused',
                     f'{fmt_id_display(cid)} in {source_path}: {e} Nothing was written.')

    if not changed:
        return _notfound(
            result,
            f'{source_path} has no `## Claims` block to append to. Add the block by hand '
            '(see SPEC.md §14 for the shape), then retry.')

    if not persons_norm:
        result.add('warning',
                   f'{fmt_id_display(cid)} has no persons: yet - `fha lint` will flag it until '
                   f'you link one: `fha claim {fmt_id_display(cid)} --persons P-id[,P-id...]`.')
    if place is not None and not _place_known_in_index(archive_root, place):
        result.add('warning',
                   f'--place {fmt_id_display(normalize_id(place))} was not found in the place '
                   'index (it may be new, or `.cache/index.sqlite` may be stale) - the claim '
                   'was still created. Run `fha index` to refresh, or check the id with '
                   f'`fha find {fmt_id_display(normalize_id(place))}`.')

    summary = f'{fmt_id_display(cid)}: {claim_type} ({status}) on {fmt_id_display(sid)}'

    if dry_run:
        result.data['status'] = 'ok'
        result.add('info', f'[dry-run] Would mint {summary}')
        for dline in difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=f'{source_path} (before)', tofile=f'{source_path} (after)', lineterm='',
        ):
            result.add('info', dline)
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    try:
        write_text_exact(source_path, reapply_newline(after, before))
    except OSError as e:
        return _fail(result, 'failed', f'cannot write {source_path}: {e}')

    result.data['status'] = 'ok'
    result.note_changed(source_path)
    result.add('info', f'Minted {summary}', path=source_path)
    if status == 'suggested':
        result.add('info',
                   'Minted as `suggested` - review it with '
                   f'`fha claim {fmt_id_display(cid)} --status accepted` before treating it as fact.',
                   next_step=f'fha claim {fmt_id_display(cid)} --status accepted')
    result.add('info', 'Reminder: run `fha index` so the new claim enters the query surface.',
               next_step='fha index')
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cmd_claim(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    persons = None
    if getattr(args, 'persons', None):
        persons = [p.strip() for p in args.persons.split(',') if p.strip()]

    result = run_claim(
        archive_root,
        claim_id=args.claim_id,
        status=getattr(args, 'status', None),
        reviewed=getattr(args, 'reviewed', None),
        value=getattr(args, 'value', None),
        date=getattr(args, 'date', None),
        claim_type=getattr(args, 'claim_type', None),
        place=getattr(args, 'place', None),
        place_text=getattr(args, 'place_text', None),
        persons=persons,
        confidence=getattr(args, 'confidence', None),
        dry_run=bool(getattr(args, 'dry_run', False)),
    )
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def _cmd_claim_new(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    persons = None
    if getattr(args, 'persons', None):
        persons = [p.strip() for p in args.persons.split(',') if p.strip()]

    result = run_claim_new(
        archive_root,
        source_id=args.source_id,
        claim_type=args.claim_type,
        value=args.value,
        date=getattr(args, 'date', None),
        place=getattr(args, 'place', None),
        place_text=getattr(args, 'place_text', None),
        persons=persons,
        subtype=getattr(args, 'subtype', None),
        status=args.status,
        confidence=getattr(args, 'confidence', None),
        dry_run=bool(getattr(args, 'dry_run', False)),
    )
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('claim_id', metavar='C-id', help='The claim to review (e.g. C-fd0000001a).')
    p.add_argument('--status', choices=REVIEW_STATUSES,
                   help='The review status to set. At least one of --status/--value/--date/'
                        '--type/--place/--place-text/--persons/--confidence is required.')
    p.add_argument('--reviewed', metavar='DATE',
                   help='Review date (YYYY-MM-DD); defaults to today. Only takes effect '
                        'together with --status.')
    p.add_argument('--value', metavar='TEXT',
                   help='Optionally correct the claim value (single-line scalar only).')
    p.add_argument('--date', metavar='EDTF',
                   help="Optionally correct the claim's date (EDTF, e.g. 1880 or 1880-06-15).")
    p.add_argument('--type', dest='claim_type', metavar='TYPE', choices=sorted(CLAIM_TYPES),
                   help='Optionally correct the claim type (SPEC §8.2). Changing TO '
                        'relationship is refused - see `fha claim --help`.')
    place_group = p.add_mutually_exclusive_group()
    place_group.add_argument('--place', metavar='L-id',
                             help='Optionally set the place by its registry id - replaces '
                                  'place_text if one is set.')
    place_group.add_argument('--place-text', metavar='TEXT', dest='place_text',
                             help='Optionally set the place as free text - replaces place '
                                  'if one is set.')
    p.add_argument('--persons', metavar='P-id[,P-id...]',
                   help='Optionally REPLACE the whole persons: list, comma-separated P-ids '
                        '(every id must already have a person record).')
    p.add_argument('--confidence', choices=CONFIDENCE_VALUES,
                   help='Optionally set the evidence-quality level (SPEC §8.5; separate '
                        'from --status, the review state).')
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview the YAML change without writing.')


def _add_new_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('--source', required=True, metavar='S-id', dest='source_id',
                   help='The source this claim is cited to (e.g. S-fa1234567b).')
    p.add_argument('--type', required=True, dest='claim_type', metavar='TYPE',
                   choices=sorted(CLAIM_TYPES),
                   help='The claim type (SPEC §8.2). relationship is refused - see below.')
    p.add_argument('--value', required=True, metavar='TEXT',
                   help='The human-readable summary of the assertion.')
    p.add_argument('--date', metavar='DATE',
                   help='When it happened - EDTF (1880-06-15) or plain words like "about 1880".')
    place_group = p.add_mutually_exclusive_group()
    place_group.add_argument('--place', metavar='L-id',
                             help='The place, by its registry id (e.g. L-baba9801fa).')
    place_group.add_argument('--place-text', metavar='TEXT', dest='place_text',
                             help='The place as written in the source, when it has no L-id yet.')
    p.add_argument('--persons', metavar='P-id[,P-id...]',
                   help='Who the claim is about, comma-separated P-ids. Optional, but lint '
                        'will flag the claim until at least one is linked.')
    p.add_argument('--subtype', metavar='WORD',
                   help='Free-text refinement of the type (e.g. deacon for occupation).')
    p.add_argument('--status', choices=NEW_CLAIM_STATUSES, default='accepted',
                   help='Review state to mint at (default: accepted - a human directing this '
                        'IS the review; AI callers must pass --status suggested).')
    p.add_argument('--confidence', choices=CONFIDENCE_VALUES,
                   help='Evidence-quality level (SPEC §8.5). Defaults from the source\'s '
                        'source_type (vital-record: high, interview: low, else medium).')
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview the claim block that would be appended, without writing.')


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Record your verdict on a suggested fact - the human decision point.

  fha claim <C-id> --status accepted   Confirm a fact (stamps today's date)
  fha claim <C-id> --status disputed|rejected|needs-review|superseded
  fha claim <C-id> --place L-baba9801fa   Correct a field without touching status

Only you move a claim to accepted. Nothing becomes a fact until you decide here.
At least one of --status/--value/--date/--type/--place/--place-text/--persons/
--confidence is required. Preview any change first with --dry-run.

To mint a brand-new claim onto a source, use `fha claim new` (`fha claim new --help`)."""

_NEW_CLI_DESCRIPTION = """\
Mint a brand-new claim onto an existing source record.

  fha claim new --source S-… --type occupation --value "Bookkeeper, 1874 directory" \\
      --persons P-… --date 1874 --status accepted

Defaults to --status accepted (typing this command IS the review). Pass
--status suggested for an AI-drafted claim awaiting human review.
--type relationship is refused - it needs a roles: map this command does not
build; use `fha person relate` (hypothesis) or `fha confirm cooccur` (sourced)
instead. Preview any change first with --dry-run."""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'claim' onto the main fha parser.

    Only the flat review verb (`fha claim <C-id> …`) is registered here.
    `fha claim new …` is NOT reachable through this subparser tree - its
    positional-C-id shape would misparse `new` as a claim id - it is routed
    instead by `fha.py`'s `_intercept_claim_new`, the same early-interception
    pattern `fha gedcom import` uses (TOOLING §13a2)."""
    p = subs.add_parser(
        'claim',
        help='Review one claim: set its status and/or correct fields (human-directed write-back)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_cmd_claim)
    return p


def build_claim_new_parser() -> argparse.ArgumentParser:
    """Build the standalone parser for `fha claim new` / `python tools/claim.py new`.

    A separate parser (not a subparser of `register()`'s flat `claim` parser)
    because `fha.py`'s `_intercept_claim_new` builds and runs this BEFORE the
    main dispatcher's flat parser ever sees the argv - the same reason
    `gedcom_import.py` keeps its own standalone parser rather than nesting
    under `gedcom`'s positional-P-id parser.
    """
    parser = argparse.ArgumentParser(
        prog='fha claim new',
        description=_NEW_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_new_arguments(parser)
    parser.set_defaults(func=_cmd_claim_new)
    return parser


def _standalone_main(argv: list[str] | None = None) -> int:
    """Entry point for `python tools/claim.py …`.

    Mirrors `fha.py`'s `_intercept_claim_new`: when the first token is `new`,
    route to `build_claim_new_parser()` instead of the flat review parser, so
    `python tools/claim.py new --source … …` works the same as `fha claim new`.
    """
    argv_list = list(argv) if argv is not None else sys.argv[1:]
    if argv_list and argv_list[0] == 'new':
        args = build_claim_new_parser().parse_args(argv_list[1:])
        return args.func(args)

    parser = argparse.ArgumentParser(
        prog='fha claim',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    args = parser.parse_args(argv_list)
    return _cmd_claim(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
