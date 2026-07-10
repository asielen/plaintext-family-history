#!/usr/bin/env python3
"""
claim.py - fha claim: human-directed claim review write-back (AGENTS.md, SPEC §8).

  fha claim <C-id> --status accepted|disputed|rejected|needs-review|superseded
                   [--reviewed DATE] [--value "…"] [--date EDTF]
                   [--root PATH] [--dry-run]

This is the single deterministic write-back a human reaches for during review:
moving one claim's `status:` out of `suggested` into any of the SPEC §8.1 review
outcomes (`accepted` / `disputed` / `rejected` / `needs-review` / `superseded`)
and stamping `reviewed:`. Today that move is done by hand-editing the `## Claims`
YAML or through the `review-claims` AI skill; this tool makes it a real,
contract-safe CLI action that any front door (chat now, a UI later) can drive.

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

The edit is **surgical**: only the one named claim's entry inside its source
`.md` `## Claims` block is touched - its sibling claims, the block's key order,
and any hand comments are preserved - mirroring `places geocode`'s surgical
`places.yaml` edit and `process --more`'s frontmatter edit. The claim is located
by scanning the `sources/` records directly (the `.md` files are the truth), so
the command works even when `.cache/index.sqlite` is stale or absent. After a
write, re-run `fha index` so the new status enters the query surface.

CODE MAP
--------
  _today                    - review-stamp default (overridable in tests)
  _ClaimEditRefused         - surgical edit declined (e.g. block-scalar value)
  _find_claim_record        - scan sources/ for the .md holding one C-id
  _own_key_indent / _own_id_key_line - which id: line is an item's OWN key
  _apply_claim_review       - surgical `## Claims` block edit (status/reviewed/…)
  run_claim                 - validate, locate, edit, return a Result
  _cmd_claim / register / _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
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
    Result,
    claim_item_key_indent,
    claims_edit_problem,
    configure_utf8_stdout,
    fmt_id_display,
    format_edtf_error,
    id_type_of,
    is_valid_id,
    normalize_date,
    normalize_id,
    read_record,
    read_text_exact,
    reapply_newline,
    resolve_root_arg,
    write_text_exact,
)

configure_utf8_stdout()

# The review statuses a human can move a claim into (SPEC §8.1 lifecycle:
# `suggested → needs-review → accepted | disputed | rejected | superseded`).
# `suggested` is deliberately absent: the AI draft pass produces `suggested`;
# review only ever moves *out* of it, never back to it through this tool.
REVIEW_STATUSES = ('accepted', 'disputed', 'rejected', 'needs-review', 'superseded')

# The SHAPE of a claim's `id:` key line (optionally after the list dash).
# Shape alone is not ownership: a block scalar (`notes: |`) can quote an
# `id: C-...` line verbatim, so every consumer must also check the line sits at
# the item's own key column (`_own_id_key_line`). Mirrors confirm.py - KEEP IN SYNC.
_CLAIM_ID_KEY_RE = re.compile(
    r'^\s*(?:-\s+)?id:\s*(C-[0-9a-hjkmnp-tv-z]{10})\b', re.I
)


def _today() -> str:
    return datetime.date.today().isoformat()


class _ClaimEditRefused(Exception):
    """The surgical edit cannot be performed safely (caller turns into a Result)."""


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
    status: str,
    reviewed: str | None = None,
    value: str | None = None,
    date: str | None = None,
) -> tuple[str, bool]:
    """Surgically set `status:`/`reviewed:`/`value:`/`date:` on one claim block.

    Only the claim whose `id:` matches `claim_id` (case-insensitive) inside the
    `## Claims` fenced YAML block is touched; every other line - sibling claims,
    key order, comments - is preserved. Returns `(new_text, changed)`; `changed`
    is False when the block or the claim isn't found (the caller reports a clean
    not-found rather than guessing).

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

    # 3. status (always present in a valid claim - replaced in place).
    status_idx = set_scalar('status', status, insert_after=0)

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

    new_lines = lines[:start] + item + lines[end:]
    trailing_nl = '\n' if text.endswith('\n') else ''
    return '\n'.join(new_lines) + trailing_nl, True


def _yaml_inline(value: str) -> str:
    """Render a string as a single-line YAML scalar, quoting only when needed.

    The claims block is edited as text (not round-tripped through the YAML
    emitter) to preserve key order and comments, so a free-form `--value` that
    carries YAML-significant characters (`: `, a leading `-`, ` #`) must be
    quoted exactly when the parser needs it - the same discipline `fha process`
    uses for scaffold scalars.
    """
    rendered = yaml.safe_dump(
        value, default_flow_style=True, allow_unicode=True, width=10 ** 9,
    ).strip()
    if rendered.endswith('...'):
        rendered = rendered[:-3].strip()
    return rendered


# ── Top-level operation ───────────────────────────────────────────────────────

def run_claim(
    archive_root: Path,
    *,
    claim_id: str,
    status: str,
    reviewed: str | None = None,
    value: str | None = None,
    date: str | None = None,
    dry_run: bool = False,
) -> Result:
    """Move one claim's review status (and stamp `reviewed:`); return a Result.

    `data` is {'status': 'ok'|'invalid-id'|'not-found'|'refused'|'failed',
    'claim_id', 'before_status', 'after_status', 'reviewed', 'source'}. On a real
    write the source `.md` is listed in `changed`; `--dry-run` previews the YAML
    change (a unified diff in the messages) and writes nothing. Before any write
    (or preview) the rewritten block is re-parsed (`claims_edit_problem`); a
    rewrite that would corrupt the block is a `refused` failure with nothing
    written, never a saved corruption - and when the problem predates the edit
    (the claim id already appears twice in the file, lint E001) the refusal
    names that repair instead of blaming the edit. The success message names
    the re-index next step (`fha index`).
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

    if status not in REVIEW_STATUSES:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'invalid-status'
        result.add('error',
                   f'{status!r} is not a review status. Use one of: {", ".join(REVIEW_STATUSES)}.')
        return result

    # `reviewed:` is the human-review stamp. The contract *requires* it for an
    # accepted claim (lint E006); for every review move we record when it
    # happened, defaulting to today when the human didn't pass one.
    if reviewed is None:
        reviewed = _today()
    else:
        try:
            datetime.date.fromisoformat(reviewed)
        except ValueError:
            result.ok = False
            result.exit_code = EXIT_FAILURE
            result.data['status'] = 'failed'
            result.add('error',
                       f'--reviewed {reviewed!r} is not a calendar date. Use YYYY-MM-DD, e.g. {_today()}.')
            return result
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

    try:
        new_text, changed = _apply_claim_review(
            text, cid, status=status, reviewed=reviewed,
            value=value, date=normalized_date,
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

    summary = f'{fmt_id_display(cid)}: {before_status or "(none)"} -> {status} (reviewed {reviewed})'

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


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cmd_claim(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    result = run_claim(
        archive_root,
        claim_id=args.claim_id,
        status=args.status,
        reviewed=getattr(args, 'reviewed', None),
        value=getattr(args, 'value', None),
        date=getattr(args, 'date', None),
        dry_run=bool(getattr(args, 'dry_run', False)),
    )
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('claim_id', metavar='C-id', help='The claim to review (e.g. C-fd0000001a).')
    p.add_argument('--status', required=True, choices=REVIEW_STATUSES,
                   help='The review status to set.')
    p.add_argument('--reviewed', metavar='DATE',
                   help='Review date (YYYY-MM-DD); defaults to today.')
    p.add_argument('--value', metavar='TEXT',
                   help='Optionally correct the claim value (single-line scalar only).')
    p.add_argument('--date', metavar='EDTF',
                   help="Optionally correct the claim's date (EDTF, e.g. 1880 or 1880-06-15).")
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview the YAML change without writing.')


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Record your verdict on a suggested fact - the human decision point.

  fha claim <C-id> --status accepted   Confirm a fact (stamps today's date)
  fha claim <C-id> --status disputed|rejected|needs-review|superseded

Only you move a claim to accepted. Nothing becomes a fact until you decide here.
Preview any change first with --dry-run."""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'claim' onto the main fha parser."""
    p = subs.add_parser(
        'claim',
        help='Review one claim: set its status and stamp reviewed: (human-directed write-back)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_cmd_claim)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha claim',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    args = parser.parse_args(argv)
    return _cmd_claim(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
