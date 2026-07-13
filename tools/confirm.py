#!/usr/bin/env python3
"""
confirm.py - fha confirm: human-directed write-back for detection candidates
and confirmed decisions (AGENTS.md, TOOLING §14a / §14a2 / §14a3 / §15a).

  fha confirm xref      <C-a> <C-b> --as corroborates|contradicts [--dry-run]
  fha confirm cooccur   <P-a> <P-b> --source <S-id> --subtype friend|associate|neighbor
                                    [--accept [--reviewed DATE]] [--dry-run]
  fha confirm dismiss   <P-a> <P-b> [--dry-run]
  fha confirm place     <C-id> [<C-id> …] (--name NAME [--hierarchy H] | --into <L-id>) [--dry-run]
  fha confirm discovery "<text>" [--refs S-…,P-…] [--dry-run]
  fha confirm draft     <P-id> [--dry-run]
  fha confirm merge     <P-merged> --into <P-survivor> --reason "<why>" [--dry-run]

The deterministic *detection* tools (`fha xref`, `fha cooccur`, `fha places
candidates`) only ever read - they print candidate pairs/clusters for a human to
judge and explicitly leave every write "to a future skill layer." This is that
layer's deterministic floor: once the human has picked a candidate, the
write-back itself is mechanical, so it lives here as a real CLI any front door
(chat now, a UI later) can drive. Keeping the writes in their own tool preserves
the read-only contract the detection tools advertise (a detector that also wrote
would be two owners for one surface).

THE SEVEN WRITE-BACKS
---------------------
  xref       - confirm an `fha xref` pair: write reciprocal `corroborates:`/
               `contradicts:` claim_links into both claims' source records. A
               contradiction also spawns a templated open question in
               `notes/questions.md` (`origin: tool`, both C-ids referenced) so
               lint E009 ("contradicts: with no open question") stays satisfied -
               the same machinery `fha lint --spawn-questions` uses.
  cooccur    - confirm an `fha cooccur` person-pair: mint a `relationship` claim
               (`subtype: friend|associate|neighbor`, the confirming source
               cited) into that source's `## Claims` block. Minted `suggested`
               by default - the human's confirm proposes the edge; accepting it
               into a load-bearing graph edge still goes through the normal
               step-05 claim review (`fha claim … --status accepted`), which is
               the only place "the human accepts" lives. `--accept` is the
               escape hatch for a human who is treating this confirm *as* the
               review (it stamps `reviewed:` like `fha claim` does).
  dismiss    - record a co-occurrence pair in `.cache/cooccur_dismissed.json`,
               the tombstone `fha cooccur` reads to stop re-proposing a pair.
               Nothing else writes this file; this is its writer.
  place      - register a place-text cluster: mint a new `L-id` place in
               `places/places.yaml` (or merge into an existing one via `--into`)
               and relink the named claims' `place:` to it, so the cluster stops
               surfacing as an unlinked `fha places candidates` group.
  discovery  - append a dated entry (with `[S-]`/`[P-]` refs) to
               `notes/discoveries.md`, the durable research-wins log
               `fha report` §0 leads with.
  draft      - flip a profile's `<!-- AI-DRAFT … -->` markers to
               `<!-- AI-ACCEPTED … (accepted DATE) -->` (AGENTS.md: draft prose
               carries the marker until the human accepts it). Provenance is
               preserved, not erased - the original date/model stay in the marker.
  merge      - enact a human-confirmed identity merge (SPEC §9) in one verb:
               tombstone the merged person (`status: merged`, `merged_into:`,
               `merge_reason:`, `merged_date:`; its `tier:` is stripped - a
               tombstone is a redirect, not a profile), rename its file with the
               `MERGED-INTO-P-survivor__` prefix (kept forever, never deleted),
               fold its name variants / external ids / relationships into the
               survivor, delete its GENERATED companion views (timeline /
               sources-index / draft-queue - the survivor's views carry that
               life now), and relink every claim (ALL statuses - lint E016 has no
               status filter) plus other records' `relationships:`/`people:`
               references from the merged P-id to the survivor. Prose `[[P-…]]`
               mentions are deliberately left for lint W107's gradual-cleanup
               list (counted and reported). The judgment half - is this really
               one person? - stays with the merge-identities skill and the
               human; this verb only enacts a decision already made, and it may
               WARN on evidence (an existing relationship edge between the two)
               but never refuses on evidence grounds.

Every verb ships `--dry-run` (previews, writes nothing) and returns a `_lib.Result`
whose `changed[]` lists each file written. Source/registry edits are **surgical**
text edits (not a YAML round-trip) so sibling claims, key order, and hand comments
survive - the same discipline as `fha claim` and `fha places geocode`. The `.md`
files are archive truth, so claims/sources are located by scanning `sources/`
directly; the write works even when `.cache/index.sqlite` is stale or absent.
After a write that changes the query surface, re-run `fha index`.

CODE MAP
--------
  Shared edit helpers
    _today
    _EditRefused                  - alias of _lib.ClaimEditRefused (shared with fha claim)
    _find_source_path_for_claim   - scan sources/ for the .md holding one C-id
    _find_source_path_by_id       - scan sources/ for one S-id's record
    (person records are located via _lib.find_person_record_path, shared with fha person;
     source records can also be via the newer _lib.find_source_record_path, shared with
     fha claim new - _find_source_path_by_id above predates it and is left as-is)
    _find_claims_block            - alias of _lib._find_claims_block (locate the ## Claims fence)
    _claim_spans                  - split the block into claim items
    _own_key_indent / _own_id_key_line - which id: line is an item's OWN key
    _item_span_for                - find the item that owns one C-id (verified)
    _parse_inline_list            - read a `key: [a, b]` inline YAML list
    _guard_claims_rewrite         - alias of _lib.guard_claims_rewrite (pre-write re-parse guard)
    _add_link_to_claim            - append a corroborates/contradicts target
    _set_scalar_on_claim          - set a single scalar key (e.g. place:)
    _append_claim_to_source       - alias of _lib.append_claim_to_source (append a whole new
                                    claim item to the block - shared with fha claim new)

  Merge machinery (confirm merge)
    _fm_key_span                  - locate one top-level frontmatter key
    (fences via the shared _lib.frontmatter_fence_span, the FRONT_RE grammar)
    _render_scalar / _render_variant_item / _render_relationship_entry
    _split_flow_items / _unquote  - bracket/quote-aware flow-list reading
    _fold_fm_list, _fold_external_ids, _fold_relationship_entries
    _set_fm_scalar, _remove_fm_key, _replace_fm_inline_list,
    _strip_external_id_keys       - tombstone/survivor frontmatter surgery
    _merge_fm_problem             - post-rewrite guard (_lib.frontmatter_edit_problem
                                    with the merge's intended-keys sets)
    _scan_person_profiles         - pid -> (path, meta) map from people/
    _merged_companion_views       - the tombstone's generated views to delete
    _final_survivor               - follow a merged_into chain to its end
    _person_token_re, _resolve_person_item, _rewrite_ref_item,
    _rewrite_person_list_value, _rewrite_span_person_fields,
    _relink_claims_in_source      - claim persons:/roles: relink (guarded)
    _relink_people_frontmatter    - source frontmatter people: relink
    _relink_relationship_targets  - profile relationships: to: relink

  Verbs (each returns a Result)
    run_confirm_xref, run_confirm_cooccur, run_dismiss,
    run_confirm_place, run_add_discovery, run_accept_draft,
    run_confirm_merge

  CLI
    _emit, _cmd_*, register, _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    ClaimEditRefused,
    Result,
    _find_claims_block,
    append_claim_to_source,
    build_alias_map,
    claim_item_key_indent,
    claims_edit_problem,
    configure_utf8_stdout,
    find_person_record_path,
    find_source_record_path,
    fmt_id_display,
    frontmatter_edit_problem,
    frontmatter_fence_span,
    guard_claims_rewrite,
    id_type_of,
    is_generated_file,
    is_merged_meta,
    is_valid_id,
    link_field_refs,
    mint_ids,
    normalize_id,
    parse_filename,
    parse_frontmatter_strict,
    read_record,
    read_text_exact,
    reapply_newline,
    resolve_root_arg,
    result_fail,
    resolve_typed_ref,
    strip_link_wrapper,
    write_text_exact,
    scan_ids_in_tree,
    scan_person_record_ids,
    yaml_inline,
)

configure_utf8_stdout()

SOCIAL_SUBTYPES = ('friend', 'associate', 'neighbor')
XREF_RELATIONS = ('corroborates', 'contradicts')

# The SHAPE of a claim's `id:` key line (optionally after the list dash).
# Shape alone is not ownership: a block scalar (`notes: |`) can quote an
# `id: C-...` line verbatim, so every consumer must also check the line sits at
# the item's own key column (`_own_id_key_line`). Mirrors claim.py - KEEP IN SYNC.
_CLAIM_ID_KEY_RE = re.compile(r'^\s*(?:-\s+)?id:\s*(C-[0-9a-hjkmnp-tv-z]{10})\b', re.I)


def _today() -> str:
    return datetime.date.today().isoformat()


# Thin alias: the exception type itself now lives in `_lib.ClaimEditRefused`
# (moved there when `fha claim new` needed the same claims-append machinery
# as `run_confirm_cooccur` - see its docstring for the why). Kept as a
# module-level name here so every existing `except _EditRefused` site in this
# file, and the tests that assert on `confirm._EditRefused`, are unchanged.
_EditRefused = ClaimEditRefused


# ── Locating records on disk (the .md files are truth, never the index) ─────────

def _find_source_path_for_claim(archive_root: Path, claim_id: str) -> Path | None:
    """Scan `sources/` for the record whose `## Claims` block holds `claim_id`."""
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
            if isinstance(claim, dict) and claim.get('id') \
                    and normalize_id(str(claim['id'])) == target:
                return path
    return None


def _find_source_path_by_id(archive_root: Path, source_id: str) -> Path | None:
    """Scan `sources/` for the record file whose `_{S-id}.md` suffix matches.

    Thin wrapper over the shared `_lib.find_source_record_path` (the same scan
    `fha source note` and `fha claim new` use). The shared version additionally
    requires the matched file's id_type to be `S`; that is a no-op tightening
    here - `parse_filename` only sets a non-`S` id_type for a differently-typed
    id in the suffix, and the sole caller has already validated `source_id` as
    an S-id - so the semantics are identical for every input this receives.
    """
    return find_source_record_path(archive_root, source_id)


# ── Surgical claim-block editing ───────────────────────────────────────────────
#
# `_find_claims_block` used to be defined here; it moved to `_lib.py` (as the
# same private name, imported directly above) when `fha claim new` needed the
# exact same line-precise fence lookup that `append_claim_to_source` also
# needed - see its docstring in `_lib.py` for the why.

def _claim_spans(lines: list[str], open_fence: int, close_fence: int) -> tuple[str, list[tuple[int, int]]]:
    """Split a claims block into (base_indent, [(start, end), …]) per claim item."""
    content_start, content_end = open_fence + 1, close_fence
    dash_lines = [
        i for i in range(content_start, content_end)
        if re.match(r'^(\s*)-\s', lines[i])
    ]
    if not dash_lines:
        return '', []
    base_indent = re.match(r'^(\s*)-', lines[dash_lines[0]]).group(1)
    item_starts = [
        i for i in dash_lines
        if re.match(r'^' + re.escape(base_indent) + r'-\s', lines[i])
    ]
    bounds = item_starts + [content_end]
    return base_indent, [(s, bounds[k + 1]) for k, s in enumerate(item_starts)]


def _own_key_indent(item_lines: list[str], base_indent: str) -> str | None:
    """The exact column of one claim item's OWN mapping keys, or None.

    Sibling of `_lib.claim_item_key_indent`, with one deliberate difference:
    no conventional fallback. The write path wants a best-effort column to
    place a new key at (a wrong guess there is caught by the pre-write
    guard), but *ownership* testing wants certainty - a guessed column could
    bless a look-alike line inside quoted scalar content, which is exactly
    the wrong-claim write this check exists to prevent. So: an inline first
    key on the dash line pins the column; else the first content line after
    the dash does; else the column is unknowable and the item owns nothing.
    Mirrors claim.py - KEEP IN SYNC.
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
    column (`_own_key_indent`). Matching on shape alone sent edits to the
    first item whose quoted evidence mentioned the target id.
    Mirrors claim.py - KEEP IN SYNC.
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


def _item_span_for(
    lines: list[str], block: tuple[int, int], spans: list[tuple[int, int]],
    base_indent: str, claim_id: str,
) -> tuple[int, int] | None:
    """Locate the claim item that OWNS `claim_id`; return its span or None.

    Ownership means the id sits on the item's own `id:` key line
    (`_own_id_key_line`), never merely somewhere in its text. Belt and
    braces: the chosen span is then cross-checked against the parsed block -
    the k-th top-level dash is the k-th parsed list item, so the parsed
    item's `id` must equal the target. A mismatch means the line-level read
    and YAML disagree about which claim this is, so the edit has no safe
    landing place and this raises `_EditRefused` (the callers turn that into
    a refusal with nothing written) rather than risk the wrong claim.
    """
    target = normalize_id(claim_id)
    for k, (start, end) in enumerate(spans):
        own = _own_id_key_line(lines, start, end, base_indent)
        if own is None or normalize_id(own[1]) != target:
            continue
        open_fence, close_fence = block
        try:
            parsed = yaml.safe_load('\n'.join(lines[open_fence + 1:close_fence]))
        except yaml.YAMLError:
            parsed = None
        aligned = (
            isinstance(parsed, list) and len(parsed) == len(spans)
            and isinstance(parsed[k], dict)
            and normalize_id(str(parsed[k].get('id') or '')) == target
        )
        if not aligned:
            raise _EditRefused(
                f'the entry carrying the id line for {fmt_id_display(target)} does not '
                'read back as that claim when the block is parsed, so the edit has no '
                'safe landing place. Open the file, make the change by hand under '
                '## Claims, then run `fha lint` to check it.'
            )
        return start, end
    return None


# Thin alias: the quoting rule itself lives in `_lib.yaml_inline` (shared by
# every surgical claim/frontmatter writer - see its docstring for the why).
# Kept as a module-level name here so existing call sites in this file (and
# any test importing `confirm._yaml_inline`) do not need to change.
_yaml_inline = yaml_inline


def _split_inline_comment(raw: str) -> tuple[str, str]:
    """Split `value  # comment` into (value, '# comment').

    Per YAML, a comment begins at a `#` preceded by whitespace (a `#` flush
    against a token is part of the scalar). Returns an empty comment when there
    is none. Used to carry a hand-written trailing comment through a rewrite of
    an inline link list instead of folding it into the list.
    """
    m = re.search(r'\s#', raw)
    if not m:
        return raw, ''
    return raw[:m.start()], raw[m.start():].strip()


def _parse_inline_list(raw: str) -> list[str]:
    """Parse a `key: [a, b]` inline YAML list (or a bare scalar) into a list.

    Raises `_EditRefused` for forms this text-edit can't safely extend - an
    empty value (a block list `- …` likely follows) or a block scalar (`>`/`|`).
    A human edits those by hand rather than risk corrupting the YAML.
    """
    raw = raw.strip()
    if raw == '' or raw[:1] in ('>', '|'):
        raise _EditRefused(
            'the existing link list is empty or a block scalar; add the link by hand.'
        )
    if raw.startswith('[') and raw.endswith(']'):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [item.strip() for item in inner.split(',') if item.strip()]
    return [raw]  # a single bare scalar (e.g. `corroborates: C-x`)


# Thin alias: `guard_claims_rewrite` moved to `_lib.py` (raising the shared
# `ClaimEditRefused`, aliased above as `_EditRefused`) when `fha claim new`
# needed the exact same pre-write guard `append_claim_to_source` uses. Kept
# as a module-level name here so `_add_link_to_claim`/`_set_scalar_on_claim`
# below - and any test calling `confirm._guard_claims_rewrite` - are unchanged.
_guard_claims_rewrite = guard_claims_rewrite


def _add_link_to_claim(
    text: str, claim_id: str, rel: str, target_id: str,
) -> tuple[str, bool, bool]:
    """Append `target_id` to `claim_id`'s `rel:` (corroborates/contradicts) list.

    Returns (new_text, changed, already_present). The link list is rendered as a
    single-line inline YAML list. If the claim has no such key yet, one is
    inserted right after its `status:` line (falling back to the last line of the
    item). Other lines - sibling keys, comments, key order - are untouched.

    Edits land at the item's OWN key column (`claim_item_key_indent`) - a claim
    legally written `-   value: …` keeps its keys at column 4, and writing at the
    conventional column 2 there would break the whole block. Every changed
    rewrite passes through `_guard_claims_rewrite`, so this raises `_EditRefused`
    (nothing to write) rather than ever returning corrupting text.
    """
    target = normalize_id(target_id)
    target_disp = fmt_id_display(target)
    lines = text.splitlines()

    block = _find_claims_block(lines)
    if block is None:
        return text, False, False
    base_indent, spans = _claim_spans(lines, *block)
    span = _item_span_for(lines, block, spans, base_indent, claim_id)
    if span is None:
        return text, False, False
    start, end = span
    key_indent = claim_item_key_indent(lines[start:end], base_indent)
    dash_prefix = base_indent + '-' + ' ' * max(1, len(key_indent) - len(base_indent) - 1)

    dash_re = re.compile(r'^' + re.escape(base_indent) + r'-\s+' + re.escape(rel) + r':\s*(.*)$')
    key_re = re.compile(r'^' + re.escape(key_indent) + re.escape(rel) + r':\s*(.*)$')

    for idx in range(start, end):
        ln = lines[idx]
        m_dash = dash_re.match(ln)
        m_key = m_dash or key_re.match(ln)
        if not m_key:
            continue
        value_part, comment = _split_inline_comment(m_key.group(1))
        items = _parse_inline_list(value_part)
        if target in [normalize_id(x) for x in items]:
            return text, False, True
        items.append(target_disp)
        prefix = dash_prefix if m_dash else key_indent
        suffix = f'  {comment}' if comment else ''
        lines[idx] = f'{prefix}{rel}: [{", ".join(items)}]{suffix}'
        trailing = '\n' if text.endswith('\n') else ''
        return _guard_claims_rewrite('\n'.join(lines) + trailing, claim_id,
                                     before_text=text), True, False

    # No existing rel key - insert one after `status:`, else at end of item.
    status_idx = None
    for idx in range(start, end):
        if re.match(r'^' + re.escape(key_indent) + r'status:', lines[idx]) \
                or re.match(r'^' + re.escape(base_indent) + r'-\s+status:', lines[idx]):
            status_idx = idx
            break
    insert_at = (status_idx + 1) if status_idx is not None else end
    lines.insert(insert_at, f'{key_indent}{rel}: [{target_disp}]')
    trailing = '\n' if text.endswith('\n') else ''
    return _guard_claims_rewrite('\n'.join(lines) + trailing, claim_id,
                                 before_text=text), True, False


def _set_scalar_on_claim(text: str, claim_id: str, key: str, value: str) -> tuple[str, bool]:
    """Set a single scalar key (e.g. `place: L-id`) on one claim item in place.

    The edit lands at the item's OWN key column (`claim_item_key_indent`), not
    an assumed base+2, so a claim written `-   value: …` (keys at column 4)
    stays valid. The rewrite passes through `_guard_claims_rewrite`, so this
    raises `_EditRefused` rather than ever returning text that would break the
    block's YAML.
    """
    lines = text.splitlines()
    block = _find_claims_block(lines)
    if block is None:
        return text, False
    base_indent, spans = _claim_spans(lines, *block)
    span = _item_span_for(lines, block, spans, base_indent, claim_id)
    if span is None:
        return text, False
    start, end = span
    key_indent = claim_item_key_indent(lines[start:end], base_indent)
    dash_prefix = base_indent + '-' + ' ' * max(1, len(key_indent) - len(base_indent) - 1)

    dash_re = re.compile(r'^' + re.escape(base_indent) + r'-\s+' + re.escape(key) + r':')
    key_re = re.compile(r'^' + re.escape(key_indent) + re.escape(key) + r':')
    for idx in range(start, end):
        if dash_re.match(lines[idx]):
            lines[idx] = f'{dash_prefix}{key}: {value}'
            break
        if key_re.match(lines[idx]):
            lines[idx] = f'{key_indent}{key}: {value}'
            break
    else:
        # Insert after the item's OWN id: line (never a shape-alike inside a
        # block scalar - landing there would split the human's quoted
        # evidence). The span was found via that same line, so it exists.
        own = _own_id_key_line(lines, start, end, base_indent)
        id_idx = own[0] if own is not None else start
        lines.insert(id_idx + 1, f'{key_indent}{key}: {value}')

    trailing = '\n' if text.endswith('\n') else ''
    expect = value if key == 'status' else None
    return _guard_claims_rewrite('\n'.join(lines) + trailing, claim_id,
                                 expect_status=expect, before_text=text), True


# Thin alias: `append_claim_to_source` moved to `_lib.py` (see its docstring)
# when `fha claim new` needed the exact same "mint a claim, append it to an
# existing source's block" step `run_confirm_cooccur` below already does.
# Kept as a module-level name here so `run_confirm_cooccur`'s call site, and
# any test calling `confirm._append_claim_to_source`, are unchanged.
_append_claim_to_source = append_claim_to_source


# ── Verb: confirm xref ──────────────────────────────────────────────────────────

def run_confirm_xref(
    archive_root: Path, *, claim_a: str, claim_b: str, relation: str, dry_run: bool = False,
) -> Result:
    """Write reciprocal corroborates/contradicts links between two claims.

    `data` is {'status', 'claim_a', 'claim_b', 'relation', 'question_spawned'}.
    The link is written on *both* claims (outgoing ↔ incoming symmetry), which
    may touch one or two source files. A contradiction also appends a templated
    open question naming both C-ids, so lint E009 is satisfied immediately.
    """
    result = Result(data={
        'status': None, 'claim_a': None, 'claim_b': None,
        'relation': relation, 'question_spawned': False,
    })

    for label, cid in (('first', claim_a), ('second', claim_b)):
        if not (is_valid_id(cid) and id_type_of(cid) == 'C'):
            return _fail(result, 'invalid-id',
                         f'The {label} argument {cid!r} is not a valid claim ID. '
                         'C-ids look like C-fd0000001a - a C, a dash, then 10 archive-alphabet '
                         'characters.')
    if relation not in XREF_RELATIONS:
        return _fail(result, 'invalid-relation',
                     f'{relation!r} is not a link type. Use one of: {", ".join(XREF_RELATIONS)}.')

    ca, cb = normalize_id(claim_a), normalize_id(claim_b)
    if ca == cb:
        return _fail(result, 'same-claim',
                     'A claim cannot link to itself - pass two different C-ids.')
    result.data['claim_a'] = fmt_id_display(ca)
    result.data['claim_b'] = fmt_id_display(cb)

    path_a = _find_source_path_for_claim(archive_root, ca)
    path_b = _find_source_path_for_claim(archive_root, cb)
    for cid, path in ((ca, path_a), (cb, path_b)):
        if path is None:
            return _notfound(result,
                             f'No source record holds claim {fmt_id_display(cid)} under '
                             f'{archive_root / "sources"}.',
                             next_step='fha find ' + fmt_id_display(cid))

    # Group both directed edges by the file they land in (the pair may share a
    # source if a human links two claims in one record).
    edits: dict[Path, list[tuple[str, str]]] = {}
    edits.setdefault(path_a, []).append((ca, cb))
    edits.setdefault(path_b, []).append((cb, ca))

    previews: list[tuple[Path, str, str]] = []   # (path, before, after)
    already_all = True
    for path, pairs in edits.items():
        try:
            before = read_text_exact(path)
        except OSError as e:
            return _fail(result, 'failed', f'cannot read {path}: {e}')
        text = before
        for owner, target in pairs:
            # A refusal (unextendable link list, or a rewrite the pre-write
            # guard rejects) happens here in the planning pass, before any
            # file is written - so "nothing was written" is always true.
            try:
                text, changed, already = _add_link_to_claim(text, owner, relation, target)
            except _EditRefused as e:
                return _fail(result, 'refused',
                             f'{fmt_id_display(owner)} in {path}: {e} Nothing was written.')
            if not changed and not already:
                return _notfound(result,
                                 f'Found {fmt_id_display(owner)} in {path} but could not edit '
                                 'its claims block. Check the block by hand.')
            already_all = already_all and (already or not changed)
        if text != before:
            previews.append((path, before, text))

    if not previews:
        result.data['status'] = 'already'
        result.add('info',
                   f'{fmt_id_display(ca)} and {fmt_id_display(cb)} are already linked '
                   f'({relation}). Nothing to do.')
        return result

    rel_summary = f'{fmt_id_display(ca)} {relation} {fmt_id_display(cb)} (reciprocal)'

    if dry_run:
        result.data['status'] = 'ok'
        result.add('info', f'[dry-run] Would write {rel_summary}')
        for path, before, after in previews:
            for dline in difflib.unified_diff(
                before.splitlines(), after.splitlines(),
                fromfile=f'{path} (before)', tofile=f'{path} (after)', lineterm='',
            ):
                result.add('info', dline)
        if relation == 'contradicts':
            result.add('info', '[dry-run] Would spawn an open question in notes/questions.md.')
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    written: list[tuple[Path, str]] = []   # (path, pristine text) for rollback

    def _rollback_xref() -> None:
        # Restore every file written so far to its pristine text, so a failure
        # part-way through the reciprocal pair never leaves a one-sided link.
        for p, original in reversed(written):
            try:
                write_text_exact(p, original)
            except OSError:
                pass
        result.changed.clear()
        result.messages.clear()

    for path, before, after in previews:
        try:
            write_text_exact(path, reapply_newline(after, before))
        except OSError as e:
            _rollback_xref()
            return _fail(result, 'failed',
                         f'cannot write {path}: {e}; rolled earlier link writes back. '
                         'Nothing was changed.')
        written.append((path, before))
        result.note_changed(path)
        result.add('info', f'Wrote {relation} link in {path.name}', path=path)

    if relation == 'contradicts':
        # A `contradicts:` link only satisfies E009 once a co-occurring open
        # question exists. If spawning it fails (no notes/ dir, unreadable or
        # unwritable questions.md, disk full), roll the source edits back so we
        # never leave dangling contradicts: links without their question.
        try:
            q_path = _spawn_contradiction_question(archive_root, ca, cb)
        except OSError as e:
            _rollback_xref()
            return _fail(result, 'failed',
                         'linked the sources but could not spawn the required '
                         f'contradiction question ({e}); rolled the link writes back. '
                         'Nothing was changed.')
        result.data['question_spawned'] = True
        result.note_changed(q_path)
        result.add('info', f'Spawned an open question in {q_path}', path=q_path)

    result.data['status'] = 'ok'
    result.add('info', f'Linked {rel_summary}.')
    result.add('info', 'Reminder: run `fha index` so the link enters the query surface.',
               next_step='fha index')
    return result


def _spawn_contradiction_question(archive_root: Path, ca: str, cb: str) -> Path:
    """Append an `origin: tool` open question naming both C-ids (satisfies E009).

    Mirrors `fha lint --spawn-questions`' template: a `## Q:` block whose text
    references both claim IDs in one block, which is exactly what lint's E009
    co-occurrence check looks for. `refs:` carries both ids for tooling.
    """
    notes_dir = archive_root / 'notes'
    notes_dir.mkdir(parents=True, exist_ok=True)
    q_path = notes_dir / 'questions.md'
    existing = q_path.read_text(encoding='utf-8') if q_path.exists() else ''
    a_disp, b_disp = fmt_id_display(ca), fmt_id_display(cb)
    block = (
        f'\n## Q: Contradiction: {a_disp} contradicts {b_disp}\n'
        f'- origin: tool\n- status: open\n- refs: [{a_disp}, {b_disp}]\n'
        f'- context:\n  - (tool, {_today()}) Confirmed via `fha confirm xref`; '
        'resolve which claim stands.\n'
    )
    q_path.write_text(existing + block, encoding='utf-8')
    return q_path


# ── Verb: confirm cooccur (mint a relationship claim) ───────────────────────────

def _existing_pair_claim(source_path: Path, pa: str, pb: str, subtype: str) -> dict | None:
    """Find a live relationship claim in this source already covering the pair.

    This is what makes `confirm cooccur` idempotent: a `suggested` claim derives
    no `relationships` edge, so `fha cooccur` keeps re-proposing the pair and the
    same confirm is easy to run twice - each run used to mint a duplicate claim.
    A claim blocks a re-mint when it is `type: relationship` with the same
    `subtype`, its `persons` cover both P-ids (in either order), and its status
    is anything except `rejected`/`superseded` - a dead claim must NOT block a
    fresh confirm (a human who rejected one bad claim may later confirm the same
    pair for real). Returns the blocking claim dict, or None. An unparseable
    record yields None; the append path's pre-write guard judges it from there.

    `persons:` entries are read the way every other tool reads link fields
    (`link_field_refs` + `resolve_typed_ref`), because hand-written claims
    carry every taught form: bare `P-x`, quoted `"[[P-x]]"` / `"[[P-x|Sam]]"`,
    and the unquoted `[[P-x]]` that YAML parses into a nested list. Comparing
    `normalize_id(str(p))` raw made every wikilink-form claim invisible to
    this gate, so a re-confirm minted a duplicate. A plain-name entry
    (`"[[Sam Rivera]]"`) still cannot block without an alias map; that fails
    toward a duplicate the human can see, never a silently skipped mint.
    """
    try:
        claims = read_record(source_path).get('claims') or []
    except Exception:  # noqa: BLE001 - unreadable record cannot show a duplicate
        return None
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        if str(claim.get('type') or '').strip().lower() != 'relationship':
            continue
        if str(claim.get('subtype') or '').strip().lower() != subtype:
            continue
        if str(claim.get('status') or '').strip().lower() in ('rejected', 'superseded'):
            continue
        norm: set[str] = set()
        for ref in link_field_refs(claim.get('persons')):
            rid = resolve_typed_ref(ref, None, want='P')
            if rid:
                norm.add(rid)
        if pa in norm and pb in norm:
            return claim
    return None


def run_confirm_cooccur(
    archive_root: Path, *, person_a: str, person_b: str, source_id: str, subtype: str,
    accept: bool = False, reviewed: str | None = None, dry_run: bool = False,
) -> Result:
    """Mint a social `relationship` claim into the confirming source's record.

    `data` is {'status', 'claim_id', 'person_a', 'person_b', 'subtype',
    'source', 'source_id', 'claim_status'}. `source` is the record path (kept
    for backward compat); `source_id` is the canonical `S-…` id this confirm
    already knows from its `source_id` argument - the field a downstream
    reindexer (`fha serve`) reads to know which source to refresh. Minted
    `suggested` by default (the human's
    confirm proposes; accepting into a graph edge is the step-05 review). With
    `--accept` the claim is minted `accepted` and stamped `reviewed:` (today
    unless given), treating this confirm as the review - the only way it becomes
    a derived `relationships` edge on the next `fha index`.

    Idempotent, mirroring `confirm xref`'s `already` no-op: when the source
    already holds a live relationship claim for this pair + subtype (any status
    except rejected/superseded), the run reports status `already` - `claim_id`/
    `claim_status` then describe that existing claim - and writes nothing.
    """
    result = Result(data={
        'status': None, 'claim_id': None, 'person_a': None, 'person_b': None,
        'subtype': subtype, 'source': None, 'source_id': None, 'claim_status': None,
    })

    # --reviewed only stamps the review date onto an *accepted* claim; a
    # suggested claim is by definition unreviewed. Passing it without --accept
    # used to be a silent no-op (the date was validated then discarded), so
    # refuse the combination rather than quietly dropping a date the human typed.
    if reviewed is not None and not accept:
        return _fail(result, 'failed',
                     '--reviewed only takes effect with --accept (it stamps the review '
                     'date on the accepted claim). Add --accept, or drop --reviewed to '
                     'mint the claim as suggested.')

    for label, pid in (('first', person_a), ('second', person_b)):
        if not (is_valid_id(pid) and id_type_of(pid) == 'P'):
            return _fail(result, 'invalid-id',
                         f'The {label} argument {pid!r} is not a valid person ID. '
                         'P-ids look like P-de957bcda1.')
    pa, pb = normalize_id(person_a), normalize_id(person_b)
    if pa == pb:
        return _fail(result, 'same-person',
                     'A relationship needs two different people - pass two distinct P-ids.')
    # Both people must have an actual profile record before we mint a claim that
    # names them, else the write leaves an E005 missing-person reference behind.
    known_people = {normalize_id(x) for x in scan_person_record_ids(archive_root)}
    missing_people = [fmt_id_display(p) for p in (pa, pb) if p not in known_people]
    if missing_people:
        return _notfound(result,
                         'No person profile record for: ' + ', '.join(missing_people)
                         + '. Mint the person first (no claim written).',
                         next_step='fha find ' + missing_people[0])
    if subtype not in SOCIAL_SUBTYPES:
        return _fail(result, 'invalid-subtype',
                     f'{subtype!r} is not a social relationship subtype. '
                     f'Use one of: {", ".join(SOCIAL_SUBTYPES)}.')
    if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
        return _fail(result, 'invalid-id',
                     f'--source {source_id!r} is not a valid source ID. S-ids look like S-fa1234567b.')

    result.data['person_a'] = fmt_id_display(pa)
    result.data['person_b'] = fmt_id_display(pb)

    source_path = _find_source_path_by_id(archive_root, source_id)
    if source_path is None:
        return _notfound(result,
                         f'No source record {fmt_id_display(source_id)} found under '
                         f'{archive_root / "sources"}.',
                         next_step='fha find ' + fmt_id_display(source_id))
    result.data['source'] = str(source_path)
    # Publish the canonical `S-…` id (path kept above for backward compat) so a
    # downstream reindexer reads one field. This confirm already validated the
    # `source_id` argument as an S-id, so the display form is authoritative -
    # no filename parse needed, unlike claim.py which had only the record path.
    result.data['source_id'] = fmt_id_display(normalize_id(source_id))

    # Idempotency gate: never mint a second claim for a pair + subtype this
    # source already covers with a live claim (see _existing_pair_claim).
    existing = _existing_pair_claim(source_path, pa, pb, subtype)
    if existing is not None:
        ex_id = str(existing.get('id') or '').strip()
        ex_disp = fmt_id_display(normalize_id(ex_id)) if is_valid_id(ex_id) else None
        ex_status = str(existing.get('status') or '').strip() or 'unknown'
        # Comparisons use the lowercased form (a hand-edited `Status: Suggested`
        # must behave like the canonical spelling); ex_status keeps the
        # author's casing for display.
        ex_status_norm = ex_status.lower()
        # --accept on a pair this source already covers with a still-suggested
        # (or needs-review) claim PROMOTES that claim rather than silently
        # dropping the request: the human directed the accept (TOOLING §3b), and
        # minting a second claim would duplicate. A claim already accepted (or
        # disputed/rejected) is left alone - only forward moves out of suggested.
        if accept and ex_disp and ex_status_norm in ('suggested', 'needs-review'):
            if reviewed is not None:
                try:
                    datetime.date.fromisoformat(reviewed)
                except ValueError:
                    return _fail(result, 'failed',
                                 f'--reviewed {reviewed!r} is not a calendar date. Use '
                                 f'YYYY-MM-DD, e.g. {_today()}.')
            stamp = reviewed or _today()
            try:
                before = read_text_exact(source_path)
                after, _ok = _set_scalar_on_claim(before, ex_id, 'status', 'accepted')
                after, _ok = _set_scalar_on_claim(after, ex_id, 'reviewed', stamp)
            except _EditRefused as e:
                return _fail(result, 'refused',
                             f'{ex_disp} in {source_path}: {e} Nothing was written.')
            result.data['claim_id'] = ex_disp
            result.data['claim_status'] = 'accepted'
            if dry_run:
                result.data['status'] = 'accepted'
                result.add('info',
                           f'[dry-run] Would accept the existing {subtype} claim '
                           f'{ex_disp} in {source_path.name} (reviewed {stamp}).')
                for dline in difflib.unified_diff(
                    before.splitlines(), after.splitlines(),
                    fromfile=f'{source_path} (before)', tofile=f'{source_path} (after)',
                    lineterm=''):
                    result.add('info', dline)
                result.add('info', '[dry-run] No file written.')
                return result
            try:
                write_text_exact(source_path, reapply_newline(after, before))
            except OSError as e:
                return _fail(result, 'failed', f'cannot write {source_path}: {e}')
            result.note_changed(source_path)
            result.data['status'] = 'accepted'
            result.add('info',
                       f'Accepted the existing {subtype} relationship claim {ex_disp} '
                       f'in {source_path.name} (reviewed {stamp}).', path=source_path)
            return result
        result.data['status'] = 'already'
        result.data['claim_id'] = ex_disp
        result.data['claim_status'] = ex_status
        result.add('info',
                   f'{fmt_id_display(pa)} and {fmt_id_display(pb)} already have a '
                   f'{subtype} relationship claim in {source_path.name} '
                   f'({ex_disp or "no id yet"}, status {ex_status}). Nothing to do.')
        if ex_status_norm == 'suggested' and ex_disp:
            result.add('info',
                       f'To accept it, review it with `fha claim {ex_disp} --status accepted`.',
                       next_step=f'fha claim {ex_disp} --status accepted')
        return result

    claim_status = 'accepted' if accept else 'suggested'
    result.data['claim_status'] = claim_status
    if accept and reviewed is None:
        reviewed = _today()
    elif reviewed is not None:
        try:
            datetime.date.fromisoformat(reviewed)
        except ValueError:
            return _fail(result, 'failed',
                         f'--reviewed {reviewed!r} is not a calendar date. Use YYYY-MM-DD, '
                         f'e.g. {_today()}.')

    cid = mint_ids('C', 1, archive_root)[0]
    result.data['claim_id'] = fmt_id_display(cid)
    item_lines = _relationship_claim_lines(
        cid, pa, pb, subtype, claim_status, reviewed,
    )

    try:
        before = read_text_exact(source_path)
    except OSError as e:
        return _fail(result, 'failed', f'cannot read {source_path}: {e}')
    try:
        after, changed = _append_claim_to_source(before, item_lines)
    except _EditRefused as e:
        # The pre-write guard rejected the appended block (e.g. the existing
        # block is hand-indented and the templated item would break its YAML).
        return _fail(result, 'refused',
                     f'{fmt_id_display(cid)} in {source_path}: {e} Nothing was written.')
    if not changed:
        return _notfound(result,
                         f'{source_path} has no `## Claims` block to append to. '
                         'Add the block by hand, then retry.')

    summary = (f'{fmt_id_display(cid)}: {fmt_id_display(pa)} <-> {fmt_id_display(pb)} '
               f'({subtype}, {claim_status}) cited to {fmt_id_display(source_id)}')

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
    if not accept:
        result.add('info',
                   'Minted as `suggested` - review it with '
                   f'`fha claim {fmt_id_display(cid)} --status accepted` to make it a graph edge.',
                   next_step=f'fha claim {fmt_id_display(cid)} --status accepted')
    result.add('info', 'Reminder: run `fha index` so the claim enters the query surface.',
               next_step='fha index')
    return result


def _relationship_claim_lines(
    cid: str, pa: str, pb: str, subtype: str, status: str, reviewed: str | None,
) -> list[str]:
    """Build the YAML lines for one minted social `relationship` claim.

    `roles:` is required for relationship claims (lint E015); a social tie is
    symmetric, so both people sit under one role named for the subtype. The
    derived `relationships` edge (index `_derive_relationships`) pairs every
    person in the claim for social subtypes, so this shape yields the reciprocal
    edge once the claim is accepted.
    """
    a_disp, b_disp = fmt_id_display(pa), fmt_id_display(pb)
    lines = [
        f'- value: "{a_disp} and {b_disp}: {subtype} (co-occurrence confirmed)"',
        f'  id: {fmt_id_display(cid)}',
        '  type: relationship',
        f'  subtype: {subtype}',
        f'  persons: [{a_disp}, {b_disp}]',
        '  roles:',
        f'    {subtype}: [{a_disp}, {b_disp}]',
        f'  status: {status}',
    ]
    if status == 'accepted' and reviewed:
        lines.append(f'  reviewed: {reviewed}')
    lines += [
        '  confidence: medium',
        '  information: secondary',
        '  evidence: indirect',
        '  notes: >',
        f'    Social tie ({subtype}) suggested by co-occurrence in shared sources and',
        '    confirmed by a human from this source.',
    ]
    return lines


# ── Verb: dismiss (cooccur tombstone) ───────────────────────────────────────────

def run_dismiss(
    archive_root: Path, *, person_a: str, person_b: str, dry_run: bool = False,
) -> Result:
    """Record a person-pair in `.cache/cooccur_dismissed.json` so it isn't re-proposed.

    `data` is {'status', 'person_a', 'person_b', 'total'}. The tombstone is the
    exact `{"pairs": [[id, id], …]}` shape `fha cooccur._load_dismissed` reads
    (lowercased ids, deduped by unordered pair). `fha cooccur` reads this file
    directly, so no re-index is needed.
    """
    result = Result(data={'status': None, 'person_a': None, 'person_b': None, 'total': 0})

    for label, pid in (('first', person_a), ('second', person_b)):
        if not (is_valid_id(pid) and id_type_of(pid) == 'P'):
            return _fail(result, 'invalid-id',
                         f'The {label} argument {pid!r} is not a valid person ID. '
                         'P-ids look like P-de957bcda1.')
    pa, pb = normalize_id(person_a), normalize_id(person_b)
    if pa == pb:
        return _fail(result, 'same-person', 'Pass two different P-ids to dismiss a pair.')
    result.data['person_a'] = fmt_id_display(pa)
    result.data['person_b'] = fmt_id_display(pb)

    cache_dir = archive_root / '.cache'
    path = cache_dir / 'cooccur_dismissed.json'

    pairs: list[list[str]] = []
    seen: set[frozenset[str]] = set()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            for pair in (data.get('pairs') or []):
                if isinstance(pair, list) and len(pair) == 2:
                    norm = [normalize_id(str(pair[0])), normalize_id(str(pair[1]))]
                    key = frozenset(norm)
                    if key not in seen:
                        seen.add(key)
                        pairs.append(norm)
        except (OSError, json.JSONDecodeError):
            # A corrupt tombstone is disposable cache, not archive truth - start
            # fresh rather than refuse the dismissal.
            pairs, seen = [], set()

    if frozenset((pa, pb)) in seen:
        result.data['status'] = 'already'
        result.data['total'] = len(pairs)
        result.add('info',
                   f'{fmt_id_display(pa)} <-> {fmt_id_display(pb)} is already dismissed. '
                   'Nothing to do.')
        return result

    pairs.append([pa, pb])
    result.data['total'] = len(pairs)

    if dry_run:
        result.data['status'] = 'ok'
        result.add('info',
                   f'[dry-run] Would dismiss {fmt_id_display(pa)} <-> {fmt_id_display(pb)} '
                   f'({len(pairs)} pair(s) total). No file written.')
        return result

    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps({'pairs': pairs}, indent=2) + '\n', encoding='utf-8')
    except OSError as e:
        return _fail(result, 'failed', f'cannot write {path}: {e}')

    result.data['status'] = 'ok'
    result.note_changed(path)
    result.add('info',
               f'Dismissed {fmt_id_display(pa)} <-> {fmt_id_display(pb)}. '
               f'`fha cooccur` will no longer propose this pair.', path=path)
    return result


# ── Verb: confirm place (register/merge a place-text cluster) ────────────────────

def run_confirm_place(
    archive_root: Path, *, claim_ids: list[str], name: str | None = None,
    hierarchy: str | None = None, into: str | None = None, dry_run: bool = False,
) -> Result:
    """Register a place-text cluster into the registry and relink its claims.

    `data` is {'status', 'place_id', 'name', 'into', 'relinked', 'missing'}.
    With `--into L-id` the named claims are relinked to an existing place; else a
    new `L-id` is minted and a place block appended to `places/places.yaml`.
    Both halves run (registry write + claim relink) so the cluster stops
    surfacing as an unlinked `fha places candidates` group (the symmetric write).
    """
    result = Result(data={
        'status': None, 'place_id': None, 'name': name, 'into': None,
        'relinked': [], 'missing': [],
    })

    if not claim_ids:
        return _fail(result, 'failed', 'Pass at least one claim C-id to relink to the place.')
    norm_claims: list[str] = []
    for cid in claim_ids:
        if not (is_valid_id(cid) and id_type_of(cid) == 'C'):
            return _fail(result, 'invalid-id',
                         f'{cid!r} is not a valid claim ID. C-ids look like C-fd0000001a.')
        norm_claims.append(normalize_id(cid))

    # --into (relink to an existing place) and --name/--hierarchy (mint a new
    # one) are mutually exclusive; accepting both silently took the --into branch
    # and dropped the requested name, relinking to the wrong place.
    if into is not None and (name or hierarchy):
        return _fail(result, 'failed',
                     'Pass either --into (relink to an existing place) or '
                     '--name/--hierarchy (mint a new one), not both.')

    if into is not None:
        if not (is_valid_id(into) and id_type_of(into) == 'L'):
            return _fail(result, 'invalid-id',
                         f'--into {into!r} is not a valid place ID. L-ids look like L-7c1a9f4e22.')
        place_id = normalize_id(into)
        if not _place_exists(archive_root, place_id):
            return _notfound(result,
                             f'No place {fmt_id_display(place_id)} in places/places.yaml. '
                             'Drop --into to mint a new place instead.')
        result.data['into'] = fmt_id_display(place_id)
    else:
        if not (name and name.strip()):
            return _fail(result, 'failed',
                         'Pass --name to mint a new place, or --into L-id to merge into an existing one.')
        place_id = normalize_id(mint_ids('L', 1, archive_root)[0])
    result.data['place_id'] = fmt_id_display(place_id)
    place_disp = fmt_id_display(place_id)

    # Locate each claim's source up front so a missing one is reported, not half-written.
    claim_paths: dict[str, Path] = {}
    for cid in norm_claims:
        path = _find_source_path_for_claim(archive_root, cid)
        if path is None:
            result.data['missing'].append(fmt_id_display(cid))
        else:
            claim_paths[cid] = path
    if result.data['missing']:
        return _notfound(result,
                         'These claims were not found in any source record: '
                         + ', '.join(result.data['missing'])
                         + '. Fix the C-ids and retry (no changes written).')

    places_yaml = archive_root / 'places' / 'places.yaml'
    new_block_lines = _place_block_lines(place_id, name, hierarchy) if into is None else None

    # Plan every edit before writing so a failure leaves nothing half-done.
    # Several claims can live in the same source file; chain each edit onto the
    # accumulated text (not the pristine original) so two C-ids in one record
    # both survive. Building each preview from the original would make the last
    # write for that path clobber the earlier relinks while still reporting
    # every C-id as relinked.
    file_originals: dict[Path, str] = {}     # path -> pristine text (for rollback)
    file_edits: dict[Path, str] = {}         # path -> text after all its claims
    for cid, path in claim_paths.items():
        before = file_edits.get(path)
        if before is None:
            try:
                before = path.read_text(encoding='utf-8')
            except OSError as e:
                return _fail(result, 'failed', f'cannot read {path}: {e}')
            file_originals[path] = before
        try:
            after, changed = _set_scalar_on_claim(before, cid, 'place', place_disp)
        except _EditRefused as e:
            # Raised in the planning pass, before the registry or any source
            # file is written - refusing here leaves the archive untouched.
            return _fail(result, 'refused',
                         f'{fmt_id_display(cid)} in {path}: {e} Nothing was written.')
        if not changed:
            return _notfound(result,
                             f'Found {fmt_id_display(cid)} but could not edit its claims block '
                             f'in {path}. Check the block by hand.')
        file_edits[path] = after

    if dry_run:
        result.data['status'] = 'ok'
        if into is None:
            result.add('info', f'[dry-run] Would register place {place_disp} ({name}) in places.yaml.')
            for ln in new_block_lines:
                result.add('info', '  ' + ln)
        else:
            result.add('info', f'[dry-run] Would relink claims to existing place {place_disp}.')
        for cid, path in claim_paths.items():
            result.add('info', f'[dry-run] Would set place: {place_disp} on {fmt_id_display(cid)} in {path.name}')
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    # Apply every planned write, rolling back on the first failure so the
    # cluster is either fully relinked or left untouched - no new place stranded
    # with only some of its claims relinked. We restore in reverse: source files
    # to their pristine text, then the place registry to its prior state (or
    # remove it if this run created it).
    places_existed = places_yaml.exists()
    places_prior: str | None = None
    written_files: list[Path] = []

    def _rollback() -> None:
        for p in reversed(written_files):
            try:
                p.write_text(file_originals[p], encoding='utf-8')
            except OSError:
                pass
        if into is None:
            try:
                if places_existed:
                    places_yaml.write_text(places_prior or '', encoding='utf-8')
                elif places_yaml.exists():
                    places_yaml.unlink()
            except OSError:
                pass

    # 1. Registry write (new place only).
    if into is None:
        try:
            places_prior = places_yaml.read_text(encoding='utf-8') if places_existed else None
            existing = places_prior or ''
            sep = '' if (not existing or existing.endswith('\n')) else '\n'
            places_yaml.parent.mkdir(parents=True, exist_ok=True)
            places_yaml.write_text(existing + sep + '\n'.join(new_block_lines) + '\n', encoding='utf-8')
        except OSError as e:
            return _fail(result, 'failed', f'cannot write {places_yaml}: {e}')

    # 2. Relink claims - one write per source file (all of a file's claims are
    #    already folded into file_edits[path]).
    for path, after in file_edits.items():
        try:
            path.write_text(after, encoding='utf-8')
        except OSError as e:
            _rollback()
            return _fail(result, 'failed', f'cannot write {path}: {e}')
        written_files.append(path)

    # All writes landed - now it is safe to record what changed.
    if into is None:
        result.note_changed(places_yaml)
        result.add('info', f'Registered place {place_disp} ({name}) in {places_yaml.name}', path=places_yaml)
    for path in file_edits:
        result.note_changed(path)
    for cid in claim_paths:
        result.data['relinked'].append(fmt_id_display(cid))

    result.data['status'] = 'ok'
    result.add('info',
               f'Relinked {len(result.data["relinked"])} claim(s) to {place_disp}: '
               + ', '.join(result.data['relinked']))
    result.add('info', 'Reminder: run `fha index` so the place link enters the query surface.',
               next_step='fha index')
    return result


def _place_exists(archive_root: Path, place_id: str) -> bool:
    """Whether `place_id` already appears as a `- id:` entry in places.yaml."""
    places_yaml = archive_root / 'places' / 'places.yaml'
    if not places_yaml.exists():
        return False
    target = normalize_id(place_id)
    id_re = re.compile(r'^\s*-\s+id:\s*(L-[0-9a-hjkmnp-tv-z]{10})\b', re.I)
    for line in places_yaml.read_text(encoding='utf-8').splitlines():
        m = id_re.match(line)
        if m and normalize_id(m.group(1)) == target:
            return True
    return False


def _place_block_lines(place_id: str, name: str, hierarchy: str | None) -> list[str]:
    """Build a minimal new place record for places.yaml (no coords - geocode later)."""
    lines = [
        f'- id: {fmt_id_display(place_id)}',
        f'  name: {_yaml_inline(name.strip())}',
    ]
    if hierarchy and hierarchy.strip():
        lines.append(f'  hierarchy: {_yaml_inline(hierarchy.strip())}')
    lines.append('  notes: registered from a place-text cluster via `fha confirm place`')
    return lines


# ── Verb: discovery (append to the research-wins log) ────────────────────────────

def run_add_discovery(
    archive_root: Path, *, text: str, refs: list[str] | None = None, dry_run: bool = False,
) -> Result:
    """Append a dated entry (with `[S-]`/`[P-]` refs) to `notes/discoveries.md`.

    `data` is {'status', 'entry'}. The entry is `- YYYY-MM-DD: <text> [refs]`,
    appended to the durable log `fha report` §0 leads with. Refs are validated as
    archive IDs and rendered as `[S-…]`/`[P-…]` tokens.
    """
    result = Result(data={'status': None, 'entry': None})

    text = (text or '').strip()
    if not text:
        return _fail(result, 'failed', 'Pass the discovery text (what was found).')

    ref_tokens: list[str] = []
    norm_refs: list[str] = []
    for ref in (refs or []):
        if not is_valid_id(ref):
            return _fail(result, 'invalid-id',
                         f'{ref!r} is not a valid archive ID. Refs are S-/P-/C-/L-/H- ids, '
                         'e.g. S-fa1234567b or P-de957bcda1.')
        norm_refs.append(normalize_id(ref))
        ref_tokens.append(f'[{fmt_id_display(normalize_id(ref))}]')

    # A syntactically valid but mistyped ref (e.g. S-0000000000) would land an
    # E004 orphan reference in the log, so verify each ref names something that
    # actually exists in the archive before appending. scan_ids_in_tree is a
    # superset of every real record's id (each record carries its own id), so an
    # id missing from it appears nowhere and is certainly an orphan.
    known_ids = scan_ids_in_tree(archive_root) if norm_refs else set()
    missing_refs = [fmt_id_display(r) for r in norm_refs if r.lower() not in known_ids]
    if missing_refs:
        return _notfound(result,
                         'These refs name nothing in the archive: '
                         + ', '.join(missing_refs)
                         + '. Fix the IDs and retry (nothing written).')

    suffix = (' ' + ' '.join(ref_tokens)) if ref_tokens else ''
    entry = f'- {_today()}: {text}{suffix}'
    result.data['entry'] = entry

    path = archive_root / 'notes' / 'discoveries.md'

    if dry_run:
        result.data['status'] = 'ok'
        result.add('info', f'[dry-run] Would append to notes/discoveries.md:\n{entry}')
        result.add('info', '[dry-run] No file written.')
        return result

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding='utf-8') if path.exists() else '# Discoveries Log\n'
        sep = '' if existing.endswith('\n') else '\n'
        path.write_text(existing + sep + entry + '\n', encoding='utf-8')
    except OSError as e:
        return _fail(result, 'failed', f'cannot write {path}: {e}')

    result.data['status'] = 'ok'
    result.note_changed(path)
    result.add('info', f'Appended discovery to {path.name}: {entry}', path=path)
    return result


# ── Verb: accept AI-DRAFT prose ─────────────────────────────────────────────────

_AI_DRAFT_RE = re.compile(r'<!--(\s*)AI-DRAFT\b(.*?)-->', re.S)


def run_accept_draft(
    archive_root: Path, *, person_id: str, dry_run: bool = False,
) -> Result:
    """Flip a profile's `<!-- AI-DRAFT … -->` markers to accepted.

    `data` is {'status', 'person_id', 'profile', 'count'}. Each marker becomes
    `<!-- AI-ACCEPTED … (accepted DATE) -->`, preserving the original date/model
    (AI provenance is never erased - AGENTS.md §20). The drafted prose itself is
    untouched; only the marker's state flips, so the human-readable body is
    unchanged apart from the comment.
    """
    result = Result(data={'status': None, 'person_id': None, 'profile': None, 'count': 0})

    if not (is_valid_id(person_id) and id_type_of(person_id) == 'P'):
        return _fail(result, 'invalid-id',
                     f'{person_id!r} is not a valid person ID. P-ids look like P-2b3c4d5e6f.')
    pid = normalize_id(person_id)
    result.data['person_id'] = fmt_id_display(pid)

    profile = find_person_record_path(archive_root, pid)
    if profile is None:
        return _notfound(result,
                         f'No curated profile found for {fmt_id_display(pid)} under '
                         f'{archive_root / "people"}.',
                         next_step='fha find ' + fmt_id_display(pid))
    result.data['profile'] = str(profile)

    # A merged tombstone is never edited (the fha person set-living posture):
    # readers resolve THROUGH merged_into (SPEC §9), so accepting draft prose
    # onto the tombstone would grow a fork of the truth no reader ever sees.
    meta = read_record(profile).get('meta') or {}
    if is_merged_meta(meta):
        name = str(meta.get('name') or '').strip()
        label = f'{fmt_id_display(pid)} ({name})' if name else fmt_id_display(pid)
        survivor = normalize_id(str(meta.get('merged_into') or ''))
        if survivor and is_valid_id(survivor):
            return _fail(result, 'merged',
                         f'{label} was merged into {fmt_id_display(survivor)} - this '
                         'record is a tombstone that readers resolve through, so '
                         'draft prose belongs on the surviving record. Accept it '
                         f'there: `fha confirm draft {fmt_id_display(survivor)}`. '
                         'Nothing was written.')
        return _fail(result, 'merged',
                     f'{label} is a merged tombstone, but its merged_into: pointer '
                     'is missing or malformed, so the surviving record cannot be '
                     f'named. Find it with `fha find {fmt_id_display(pid)}`, then '
                     'accept the draft on the survivor. Nothing was written.')

    try:
        before = read_text_exact(profile)
    except OSError as e:
        return _fail(result, 'failed', f'cannot read {profile}: {e}')

    today = _today()

    def _flip(m: re.Match) -> str:
        inner = m.group(2).rstrip()
        return f'<!--{m.group(1)}AI-ACCEPTED{inner} (accepted {today}) -->'

    after, count = _AI_DRAFT_RE.subn(_flip, before)
    result.data['count'] = count

    if count == 0:
        result.data['status'] = 'none'
        result.exit_code = EXIT_WARNINGS
        result.ok = False
        result.add('warning',
                   f'No <!-- AI-DRAFT … --> markers found in {profile.name}. '
                   'Nothing to accept.')
        return result

    if dry_run:
        result.data['status'] = 'ok'
        result.add('info', f'[dry-run] Would accept {count} AI-DRAFT marker(s) in {profile.name}.')
        for dline in difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=f'{profile} (before)', tofile=f'{profile} (after)', lineterm='',
        ):
            result.add('info', dline)
        result.add('info', '[dry-run] No file written.')
        return result

    try:
        write_text_exact(profile, reapply_newline(after, before))
    except OSError as e:
        return _fail(result, 'failed', f'cannot write {profile}: {e}')

    result.data['status'] = 'ok'
    result.note_changed(profile)
    result.add('info', f'Accepted {count} AI-DRAFT marker(s) in {profile.name}.', path=profile)
    return result


# ── Verb: confirm merge (enact a human-confirmed identity merge, SPEC §9) ───────
#
# The merge write is the highest-blast-radius operation in the suite: it edits
# the survivor, tombstones and renames the merged record, and rewrites person
# references across every source and profile. The discipline that contains it:
#   1. plan EVERYTHING in memory first (all validation and every rewritten text
#      exists before the first byte is written);
#   2. every rewritten file passes a re-parse guard in the planning pass, and a
#      guard failure is a refusal naming the file with nothing written anywhere;
#   3. apply with an undo journal (the convert_mining pattern: register the
#      cleanup BEFORE each write) so any mid-apply failure rolls the whole
#      archive back to its pre-merge bytes;
#   4. the rename happens LAST, after every content write has landed.

# The frontmatter surgery below assumes SPEC-shaped person records: top-level
# keys at column zero between `---` fences. Anything weirder fails the
# `_merge_fm_problem` re-parse guard and becomes a refusal, never a bad write.

_TOMBSTONE_KIN_TYPES = ('relationship', 'marriage')


def _person_token_re(person_id: str) -> re.Pattern:
    """A finder for one P-id used as a reference token in record text.

    The lookarounds keep the match honest at both ends: no alphabet character
    (or hyphen) may precede - so the survivor id inside an existing
    `MERGED-INTO-P-…__` filename never reads as a reference to it - and no
    alphabet character may follow, so a 10-char id never matches inside a
    longer id-shaped string. Case-insensitive because display form uppercases
    the type prefix.
    """
    return re.compile(
        r'(?<![0-9a-hjkmnp-tv-z-])' + re.escape(person_id) + r'(?![0-9a-hjkmnp-tv-z])',
        re.I,
    )


def _fm_key_span(lines: list[str], fm_open: int, fm_close: int, key: str) -> tuple[int, int] | None:
    """Locate one TOP-LEVEL frontmatter key; return (key_line, end) or None.

    A top-level key sits at column zero; its block extends through every
    following line that is blank or indented (nested list items, mapping
    children, block-scalar continuations). A column-zero comment ends the
    block rather than joining it, so a human's comment ahead of the NEXT key
    is never swept up when a span is removed or replaced.
    """
    key_re = re.compile(r'^' + re.escape(key) + r'\s*:')
    for i in range(fm_open + 1, fm_close):
        if key_re.match(lines[i]):
            end = i + 1
            while end < fm_close and (not lines[end].strip() or lines[end][:1] in (' ', '\t')):
                end += 1
            return i, end
    return None


def _render_scalar(value) -> str:
    """Render any YAML scalar/flow value on one line, quoting only when needed.

    The sibling of `_yaml_inline` for non-string values: folded external ids
    keep their type (an unquoted number stays a number, a quoted numeric
    string stays quoted), and a `{value:, restricted: true}` mapping keeps its
    key order (`sort_keys=False`) so the folded form reads like the original.

    `read_record` coerces YAML booleans to the strings 'true'/'false'
    (`_coerce_yaml`); a folded `restricted:` flag is written back as the real
    boolean so the mapping round-trips verbatim. Only that key is un-coerced -
    a display name that literally reads "true" stays the string it was.
    """
    if isinstance(value, dict):
        value = {
            k: (v == 'true' if str(k) == 'restricted' and v in ('true', 'false') else v)
            for k, v in value.items()
        }
    if isinstance(value, str):
        return _yaml_inline(value)
    rendered = yaml.safe_dump(
        value, default_flow_style=True, allow_unicode=True, width=10 ** 9,
        sort_keys=False,
    ).strip()
    if rendered.endswith('...'):
        rendered = rendered[:-3].strip()
    return rendered


def _variant_value(variant) -> str | None:
    """The display string of one `name_variants` entry.

    Mirrors index.py's unwrap of the `{value:, restricted: true}` mapping form
    (SPEC §18): the mapping's `value` is the name; `str()` on the dict would
    yield a Python repr that matches nothing.
    """
    if isinstance(variant, dict):
        val = variant.get('value')
        return str(val) if val else None
    return str(variant) if variant else None


def _split_flow_items(inner: str) -> list[str]:
    """Split the inside of a YAML flow list on top-level commas.

    Bracket- and quote-aware so `[[P-x|Smith, John]]` and nested flow forms
    stay one item; each returned item keeps its own quoting verbatim, which is
    what lets a rewrite preserve untouched siblings byte-for-byte.
    """
    items: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            continue
        if ch in '[{':
            depth += 1
        elif ch in ']}':
            depth -= 1
        if ch == ',' and depth == 0:
            items.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    tail = ''.join(buf).strip()
    if tail:
        items.append(tail)
    return items


def _unquote(text: str) -> str:
    """Strip one layer of surrounding YAML quotes from a flow-list item."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1]
    return text


def _resolve_person_item(item: str, alias_map: dict[str, str] | None) -> str | None:
    """Resolve one raw persons/roles/to item (with its quoting) to a P-id."""
    return resolve_typed_ref(_unquote(item.strip()), alias_map, want='P')


# Pulls target and optional |display out of a wikilink, so a name link being
# pinned to the survivor keeps the human-readable display text.
_WIKI_PARTS_RE = re.compile(r'^\[\[(?P<target>[^\]|#]+)(?:#[^\]|]*)?(?:\|(?P<display>[^\]]+))?\]\]$')


def _rewrite_ref_item(item: str, token_re: re.Pattern, survivor_disp: str) -> str:
    """Rewrite ONE list item that resolves to the merged person.

    Two shapes arrive here. An item carrying the merged P-id as a token (bare,
    `[[P-id]]`, `[[P-id|Name]]`, quoted or not) keeps its exact wrapper and
    display - only the id substring changes, so the edit is minimal. An item
    that resolved through a NAME alias carries no id token to substitute; it
    is pinned to the survivor as `"[[P-survivor|<display>]]"` - the standard
    clash-pinning form (SPEC §9/§7) - keeping the name the human wrote as the
    display half.
    """
    if token_re.search(item):
        return token_re.sub(survivor_disp, item)
    core = _unquote(item.strip()).strip()
    m = _WIKI_PARTS_RE.match(core)
    if m:
        display = (m.group('display') or m.group('target')).strip()
    else:
        display = core
    # `display` came from the item's OWN existing text (a hand-typed alias
    # name, not something this merge validated) - escape it for the
    # double-quoted YAML scalar it lands in rather than splice it in raw
    # (PR #30 review sweep; same class as person.py's relationship-mirror fix).
    escaped_display = display.replace('\\', '\\\\').replace('"', '\\"')
    return f'"[[{survivor_disp}|{escaped_display}]]"'


def _rewrite_person_list_value(
    stripped: str, merged_id: str, survivor_id: str,
    alias_map: dict[str, str] | None, token_re: re.Pattern,
) -> tuple[str, bool]:
    """Rewrite an inline persons/roles/people value (`[a, b]`, `{…}`, or scalar).

    Items resolving to the merged person are rewritten to the survivor; items
    whose post-rewrite id duplicates one already in the same list are dropped
    (the survivor-already-listed dedupe). Unresolvable items are never touched
    and never dropped. A flow MAPPING (`roles: {head: P-x}`) gets plain token
    substitution only - good enough for id forms; a name-alias hiding inside
    one is caught by the caller's whole-block re-parse guard and becomes a
    refusal rather than a miss.

    Raises `_EditRefused` for a block scalar, which this line-level rewrite
    cannot safely restructure.
    """
    survivor_disp = fmt_id_display(survivor_id)
    if stripped[:1] in ('>', '|'):
        raise _EditRefused(
            'the person list is written as a block scalar this tool cannot '
            'rewrite; update it by hand'
        )
    if stripped.startswith('[') and stripped.endswith(']'):
        items = _split_flow_items(stripped[1:-1])
        out: list[str] = []
        seen: set[str] = set()
        changed = False
        for item in items:
            rid = _resolve_person_item(item, alias_map)
            if rid == merged_id:
                item = _rewrite_ref_item(item, token_re, survivor_disp)
                rid = survivor_id
                changed = True
            if rid is not None:
                if rid in seen:
                    changed = True
                    continue
                seen.add(rid)
            out.append(item)
        return '[' + ', '.join(out) + ']', changed
    if stripped.startswith('{'):
        new = token_re.sub(survivor_disp, stripped)
        return new, new != stripped
    rid = _resolve_person_item(stripped, alias_map)
    if rid == merged_id:
        return _rewrite_ref_item(stripped, token_re, survivor_disp), True
    return stripped, False


def _rewrite_span_person_fields(
    span_lines: list[str], base_indent: str, merged_id: str, survivor_id: str,
    alias_map: dict[str, str] | None, token_re: re.Pattern,
) -> tuple[list[str], bool]:
    """Rewrite persons:/roles: person references inside ONE claim item's lines.

    Only the item's OWN `persons:` and `roles:` keys are touched (the same
    ownership discipline as `_own_id_key_line`: the key must sit on the dash
    line or at the item's key column, so a look-alike line quoted inside a
    block scalar is never edited). Handles inline values, block-list items,
    and role sub-keys; a block-list item whose post-rewrite id duplicates one
    already present in the same list is dropped. Every other line - `value:`,
    `notes:`, comments, sibling keys - passes through byte-identical.
    """
    key_indent = claim_item_key_indent(span_lines, base_indent)
    field_re = re.compile(
        r'^(?:' + re.escape(base_indent) + r'-\s+|' + re.escape(key_indent) + r')'
        r'(persons|roles)(\s*):(\s*)(.*)$'
    )
    item_re = re.compile(r'^(\s*-\s+)(.*)$')
    out: list[str] = []
    changed = False
    i = 0
    n = len(span_lines)

    def rewrite_inline(line: str, m: re.Match) -> tuple[str, bool]:
        raw = m.group(4)
        value, comment = _split_inline_comment(raw)
        stripped = value.strip()
        if not stripped:
            return line, False
        new_val, ch = _rewrite_person_list_value(
            stripped, merged_id, survivor_id, alias_map, token_re)
        if not ch:
            return line, False
        suffix = f'  {comment}' if comment else ''
        return line[:m.start(4)] + new_val + suffix, True

    def rewrite_block_items(j: int, parent_col: int, seen: set[str]) -> int:
        """Consume `- item` lines deeper than parent_col; rewrite/drop them."""
        nonlocal changed
        while j < n:
            nxt = span_lines[j]
            if not nxt.strip():
                out.append(nxt)
                j += 1
                continue
            ind = len(nxt) - len(nxt.lstrip())
            if ind <= parent_col:
                break
            mi = item_re.match(nxt)
            if not mi:
                out.append(nxt)
                j += 1
                continue
            ival, icom = _split_inline_comment(mi.group(2))
            istr = ival.strip()
            rid = _resolve_person_item(istr, alias_map)
            new_item = istr
            if rid == merged_id:
                new_item = _rewrite_ref_item(istr, token_re, fmt_id_display(survivor_id))
                rid = survivor_id
                changed = True
            if rid is not None and rid in seen:
                changed = True
                j += 1
                continue          # duplicate of an id already listed: drop the line
            if rid is not None:
                seen.add(rid)
            suffix = f'  {icom}' if icom else ''
            out.append(mi.group(1) + new_item + suffix)
            j += 1
        return j

    while i < n:
        ln = span_lines[i]
        m = field_re.match(ln)
        if not m:
            out.append(ln)
            i += 1
            continue
        field = m.group(1)
        key_col = m.start(1)
        raw = m.group(4)
        if raw.strip():
            new_line, ch = rewrite_inline(ln, m)
            out.append(new_line)
            changed = changed or ch
            i += 1
            continue
        out.append(ln)
        if field == 'persons':
            i = rewrite_block_items(i + 1, key_col, set())
            continue
        # roles: nested role keys, each with a scalar/flow value or its own
        # block list. Track a seen-set per role so dedupe stays per-list.
        j = i + 1
        role_re = re.compile(r'^(\s+)([^\s#-][^:]*)(\s*):(\s*)(.*)$')
        while j < n:
            nxt = span_lines[j]
            if not nxt.strip():
                out.append(nxt)
                j += 1
                continue
            ind = len(nxt) - len(nxt.lstrip())
            if ind <= key_col:
                break
            mr = role_re.match(nxt)
            if mr:
                rraw = mr.group(5)
                rval, rcom = _split_inline_comment(rraw)
                rstr = rval.strip()
                if rstr:
                    new_val, ch = _rewrite_person_list_value(
                        rstr, merged_id, survivor_id, alias_map, token_re)
                    if ch:
                        suffix = f'  {rcom}' if rcom else ''
                        nxt = nxt[:mr.start(5)] + new_val + suffix
                        changed = True
                    out.append(nxt)
                    j += 1
                else:
                    out.append(nxt)
                    j = rewrite_block_items(j + 1, len(mr.group(1)), set())
                continue
            out.append(nxt)
            j += 1
        i = j
    return out, changed


def _claim_person_refs(claim: dict) -> list[str]:
    """Every raw person reference a claim carries: `persons:` plus role values."""
    refs = list(link_field_refs(claim.get('persons')))
    roles = claim.get('roles')
    if isinstance(roles, dict):
        for value in roles.values():
            refs.extend(link_field_refs(value))
    return refs


def _claim_resolved_persons(claim: dict, alias_map: dict[str, str] | None) -> set[str]:
    """The set of P-ids a claim's persons/roles resolve to (names via aliases)."""
    out: set[str] = set()
    for ref in _claim_person_refs(claim):
        rid = resolve_typed_ref(ref, alias_map, want='P')
        if rid:
            out.add(rid)
    return out


def _relink_claims_in_source(
    text: str, merged_id: str, survivor_id: str,
    alias_map: dict[str, str] | None, token_re: re.Pattern,
) -> tuple[str, list[str], list[str]]:
    """Relink every claim in one source's ## Claims block, ALL statuses.

    Returns (new_text, edited_claim_ids, both_named_relationship_claim_ids).
    E016 fires on ANY claim referencing a merged person - suggested, accepted,
    disputed, rejected, superseded, needs-review alike - so no status is
    skipped. Spans are rewritten bottom-up so earlier spans' line indexes stay
    valid when a duplicate list item is dropped.

    Safety is layered: the block must parse and align with the text spans
    before any span is touched (a file that does not read back cleanly AND
    mentions the merged id is a refusal, never a guess); afterwards the whole
    rewritten block is re-parsed and must show the same claim count, the same
    id in every position, and NO remaining reference resolving to the merged
    person - so a form the line-level rewriter missed becomes a refusal
    naming the file instead of a silent leftover E016.

    Raises `_EditRefused` on any of those failures; the caller refuses the
    whole merge with nothing written.
    """
    lines = text.splitlines()
    block = _find_claims_block(lines)
    if block is None:
        return text, [], []
    open_fence, close_fence = block
    base_indent, spans = _claim_spans(lines, open_fence, close_fence)
    block_text = '\n'.join(lines[open_fence + 1:close_fence])
    try:
        parsed = yaml.safe_load(block_text)
    except yaml.YAMLError:
        parsed = None
    if parsed is None:
        parsed = []
    if not isinstance(parsed, list) or len(parsed) != len(spans):
        if token_re.search(block_text):
            raise _EditRefused(
                'its ## Claims block does not read back cleanly, so the claim '
                'relink has no safe landing place. Run `fha lint`, repair this '
                'file by hand, then re-run the merge'
            )
        return text, [], []

    edited: list[str] = []
    both_named: list[str] = []
    changed_any = False
    for k in range(len(spans) - 1, -1, -1):
        claim = parsed[k]
        if not isinstance(claim, dict):
            continue
        resolved = _claim_resolved_persons(claim, alias_map)
        if merged_id not in resolved:
            continue
        label = str(claim.get('id') or f'claim {k + 1}')
        if survivor_id in resolved and \
                str(claim.get('type') or '').strip().lower() in _TOMBSTONE_KIN_TYPES:
            both_named.append(label)
        start, end = spans[k]
        new_span, ch = _rewrite_span_person_fields(
            lines[start:end], base_indent, merged_id, survivor_id, alias_map, token_re)
        if ch:
            lines[start:end] = new_span
            changed_any = True
        edited.append(label)
    edited.reverse()
    both_named.reverse()
    if not changed_any:
        if edited:
            # The parse says these claims name the merged person, yet the
            # line-level rewrite found nothing to change - an exotic form.
            raise _EditRefused(
                f'claim(s) {", ".join(edited)} reference the merged person in a '
                'form this tool cannot rewrite; update them by hand, then re-run'
            )
        return text, [], both_named

    new_text = '\n'.join(lines) + ('\n' if text.endswith('\n') else '')
    problem = claims_edit_problem(new_text, None)
    if problem is not None:
        raise _EditRefused(f'{problem} after the relink, so the edit was abandoned')
    new_lines = new_text.splitlines()
    new_block = _find_claims_block(new_lines)
    reparsed = None
    if new_block is not None:
        try:
            reparsed = yaml.safe_load('\n'.join(new_lines[new_block[0] + 1:new_block[1]]))
        except yaml.YAMLError:
            reparsed = None
    if not isinstance(reparsed, list) or len(reparsed) != len(parsed):
        raise _EditRefused('the relink would change the number of claims in the block')
    for old, new in zip(parsed, reparsed):
        if not isinstance(old, dict):
            continue
        if not isinstance(new, dict) or \
                normalize_id(str(old.get('id') or '')) != normalize_id(str(new.get('id') or '')):
            raise _EditRefused('the relink would disturb a claim id in the block')
        if merged_id in _claim_resolved_persons(new, alias_map):
            raise _EditRefused(
                f'claim {new.get("id", "?")} still references the merged person '
                'after the rewrite (a form this tool cannot rewrite); update it '
                'by hand, then re-run'
            )
    return new_text, edited, both_named


def _relink_people_frontmatter(
    text: str, merged_id: str, survivor_id: str,
    alias_map: dict[str, str] | None, token_re: re.Pattern,
) -> tuple[str, bool]:
    """Repoint a source record's frontmatter `people:` list to the survivor.

    A `people:` entry naming the merged person is a W107 source after the
    merge; the list is the human-maintained "this source shows these people"
    statement, so it must follow the identity. Inline and block-list forms
    both handled; an entry that becomes a duplicate of the survivor already
    listed is dropped. Raises `_EditRefused` if the rewritten frontmatter no
    longer parses.
    """
    lines = text.splitlines()
    fm = frontmatter_fence_span(lines)
    if fm is None:
        return text, False
    span = _fm_key_span(lines, fm[0], fm[1], 'people')
    if span is None:
        return text, False
    key_line, end = span
    m = re.match(r'^(people\s*:\s*)(.*)$', lines[key_line])
    raw = m.group(2)
    value, comment = _split_inline_comment(raw)
    stripped = value.strip()
    changed = False
    if stripped:
        new_val, ch = _rewrite_person_list_value(
            stripped, merged_id, survivor_id, alias_map, token_re)
        if ch:
            suffix = f'  {comment}' if comment else ''
            lines[key_line] = m.group(1) + new_val + suffix
            changed = True
    else:
        item_re = re.compile(r'^(\s*-\s+)(.*)$')
        seen: set[str] = set()
        out: list[str] = []
        for j in range(key_line + 1, end):
            ln = lines[j]
            mi = item_re.match(ln)
            if not mi:
                out.append(ln)
                continue
            ival, icom = _split_inline_comment(mi.group(2))
            istr = ival.strip()
            rid = _resolve_person_item(istr, alias_map)
            new_item = istr
            if rid == merged_id:
                new_item = _rewrite_ref_item(istr, token_re, fmt_id_display(survivor_id))
                rid = survivor_id
                changed = True
            if rid is not None and rid in seen:
                changed = True
                continue
            if rid is not None:
                seen.add(rid)
            if new_item == istr:
                out.append(ln)
            else:
                suffix = f'  {icom}' if icom else ''
                out.append(mi.group(1) + new_item + suffix)
        if changed:
            lines[key_line + 1:end] = out
    if not changed:
        return text, False
    new_text = '\n'.join(lines) + ('\n' if text.endswith('\n') else '')
    problem = _fm_parse_problem(new_text)
    if problem is not None:
        raise _EditRefused(f'{problem} after the people: relink, so the edit was abandoned')
    return new_text, True


def _relink_relationship_targets(
    text: str, merged_id: str, survivor_id: str,
    alias_map: dict[str, str] | None, token_re: re.Pattern,
) -> tuple[str, int]:
    """Repoint `relationships:` entries' `to:` fields to the survivor.

    A `to:` left naming the merged person trips W115 (the entry stops
    reconciling with its backing claim, which this same merge relinks to the
    survivor) and keeps the human-facing relationship section naming a
    tombstone. Only the `to:` value changes; `claim:`/`source:`/`type:` and
    everything else pass through untouched. Returns (new_text, entries_changed).
    """
    lines = text.splitlines()
    fm = frontmatter_fence_span(lines)
    if fm is None:
        return text, 0
    span = _fm_key_span(lines, fm[0], fm[1], 'relationships')
    if span is None:
        return text, 0
    to_re = re.compile(r'^(\s+(?:-\s+)?to\s*:\s*)(.*)$')
    count = 0
    for j in range(span[0] + 1, span[1]):
        m = to_re.match(lines[j])
        if not m:
            continue
        value, comment = _split_inline_comment(m.group(2))
        stripped = value.strip()
        if not stripped:
            continue
        rid = _resolve_person_item(stripped, alias_map)
        if rid != merged_id:
            continue
        new_item = _rewrite_ref_item(stripped, token_re, fmt_id_display(survivor_id))
        suffix = f'  {comment}' if comment else ''
        lines[j] = m.group(1) + new_item + suffix
        count += 1
    if count == 0:
        return text, 0
    new_text = '\n'.join(lines) + ('\n' if text.endswith('\n') else '')
    problem = _fm_parse_problem(new_text)
    if problem is not None:
        raise _EditRefused(f'{problem} after the relationships relink, so the edit was abandoned')
    return new_text, count


def _fm_parse_problem(text: str) -> str | None:
    """Vet a rewritten record's frontmatter: it must still parse as a mapping."""
    lines = text.splitlines()
    fm = frontmatter_fence_span(lines)
    if fm is None:
        return 'the record would lose its frontmatter'
    try:
        meta = yaml.safe_load('\n'.join(lines[1:fm[1]]))
    except yaml.YAMLError:
        return 'the frontmatter would no longer read as YAML'
    if not isinstance(meta, dict):
        return 'the frontmatter would no longer read as a mapping'
    return None


# The merge's declared frontmatter intent, per record (the changed_keys
# contract of `_lib.frontmatter_edit_problem`): the survivor gains folds in
# exactly these four keys; the tombstone additionally gains the four SPEC §9
# tombstone fields and loses `tier:` (a redirect is not a curated profile, so
# keeping the tier would keep regenerating companion views for it). Any other
# field appearing, disappearing, or changing value is a refusal - the same
# strictness `fha person set-living` applies to its one-key edit.
_MERGE_SURVIVOR_KEYS = frozenset(
    {'name_variants', 'aliases', 'external_ids', 'relationships'})
_MERGE_TOMBSTONE_KEYS = _MERGE_SURVIVOR_KEYS | frozenset(
    {'status', 'merged_into', 'merge_reason', 'merged_date', 'tier'})


def _merge_fm_problem(
    before_text: str, new_text: str, changed_keys: frozenset[str],
) -> str | None:
    """Vet one rewritten person record against the merge's declared intent.

    Wraps the shared `_lib.frontmatter_edit_problem` with the strict-parsed
    original (`parse_frontmatter_strict` - read_record's coerced meta would
    false-flag booleans): the rewrite must still parse as a mapping, keep its
    id, and touch ONLY `changed_keys`. Replaces the old parse+id-only check,
    which let a surgery bug rewrite any unrelated field unnoticed.
    """
    before_meta = parse_frontmatter_strict(before_text)
    if before_meta is None:
        return ('the record\'s current frontmatter could not be re-read for '
                'the pre-write check')
    return frontmatter_edit_problem(
        new_text, before_meta=before_meta, changed_keys=changed_keys)


def _fold_fm_list(text: str, key: str, rendered_items: list[str],
                  insert_after: tuple[str, ...] = ('name', 'id')) -> str:
    """Append rendered items to a top-level frontmatter LIST key.

    Extends an inline list in place (mapping items render as flow mappings, so
    `[T. E. Hartley, {value: X, restricted: true}]` stays one line), appends
    `- item` lines to a block list at its own indent, and creates the key as
    an inline list after the first `insert_after` anchor found when absent.
    Raises `_EditRefused` when the existing value is a form this text edit
    cannot extend (a block scalar, a non-list scalar).
    """
    if not rendered_items:
        return text
    lines = text.splitlines()
    fm = frontmatter_fence_span(lines)
    if fm is None:
        raise _EditRefused('the record has no frontmatter to edit')
    span = _fm_key_span(lines, fm[0], fm[1], key)
    if span is None:
        pos = fm[1]
        for anchor in insert_after:
            a_span = _fm_key_span(lines, fm[0], fm[1], anchor)
            if a_span is not None:
                pos = a_span[1]
                break
        lines.insert(pos, f'{key}: [{", ".join(rendered_items)}]')
    else:
        key_line, end = span
        m = re.match(r'^(' + re.escape(key) + r'\s*:\s*)(.*)$', lines[key_line])
        value, comment = _split_inline_comment(m.group(2))
        stripped = value.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            inner = stripped[1:-1].strip()
            items = _split_flow_items(inner) if inner else []
            items.extend(rendered_items)
            suffix = f'  {comment}' if comment else ''
            lines[key_line] = m.group(1) + '[' + ', '.join(items) + ']' + suffix
        elif not stripped:
            item_indent = '  '
            for j in range(key_line + 1, end):
                mi = re.match(r'^(\s*)-\s', lines[j])
                if mi:
                    item_indent = mi.group(1)
                    break
            insert_at = end
            while insert_at > key_line + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines[insert_at:insert_at] = [f'{item_indent}- {item}' for item in rendered_items]
        else:
            raise _EditRefused(
                f'the {key}: value is not a list this tool can extend; add the '
                'folded entries by hand'
            )
    return '\n'.join(lines) + ('\n' if text.endswith('\n') else '')


def _fold_external_ids(text: str, pairs: list[tuple[str, object]]) -> str:
    """Append external-id keys the survivor lacks to its `external_ids:` mapping.

    Extends a block mapping at its child indent, extends an inline flow
    mapping in place, and creates the key (block form) before the closing
    fence when absent. Values render through `_render_scalar` so a quoted
    numeric string stays a string.
    """
    if not pairs:
        return text
    lines = text.splitlines()
    fm = frontmatter_fence_span(lines)
    if fm is None:
        raise _EditRefused('the record has no frontmatter to edit')
    span = _fm_key_span(lines, fm[0], fm[1], 'external_ids')
    rendered = [f'{k}: {_render_scalar(v)}' for k, v in pairs]
    if span is None:
        lines[fm[1]:fm[1]] = ['external_ids:'] + [f'  {r}' for r in rendered]
    else:
        key_line, end = span
        m = re.match(r'^(external_ids\s*:\s*)(.*)$', lines[key_line])
        value, comment = _split_inline_comment(m.group(2))
        stripped = value.strip()
        if stripped.startswith('{') and stripped.endswith('}'):
            inner = stripped[1:-1].strip()
            items = _split_flow_items(inner) if inner else []
            items.extend(rendered)
            suffix = f'  {comment}' if comment else ''
            lines[key_line] = m.group(1) + '{' + ', '.join(items) + '}' + suffix
        elif not stripped:
            child_indent = '  '
            for j in range(key_line + 1, end):
                if lines[j].strip():
                    child_indent = re.match(r'^(\s*)', lines[j]).group(1)
                    break
            insert_at = end
            while insert_at > key_line + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines[insert_at:insert_at] = [f'{child_indent}{r}' for r in rendered]
        else:
            raise _EditRefused(
                'the external_ids: value is not a mapping this tool can extend; '
                'add the folded ids by hand'
            )
    return '\n'.join(lines) + ('\n' if text.endswith('\n') else '')


def _render_relationship_entry(entry: dict, item_indent: str) -> list[str]:
    """Render one folded `relationships:` entry as block-list YAML lines.

    Folding moves an entry between files, so it is re-rendered from its parsed
    dict (insertion order preserved - the author's key order survives; a hand
    comment inside the entry does not, which the fold reports honestly by
    listing the tombstone in `changed[]`). Scalars quote only when YAML needs
    it; nested values render flow-style on one line.
    """
    lines: list[str] = []
    first = True
    for k, v in entry.items():
        prefix = f'{item_indent}- ' if first else f'{item_indent}  '
        lines.append(f'{prefix}{k}: {_render_scalar(v)}')
        first = False
    return lines


def _fold_relationship_entries(text: str, entries: list[dict]) -> str:
    """Append folded relationship entries to the survivor's `relationships:` block."""
    if not entries:
        return text
    lines = text.splitlines()
    fm = frontmatter_fence_span(lines)
    if fm is None:
        raise _EditRefused('the record has no frontmatter to edit')
    span = _fm_key_span(lines, fm[0], fm[1], 'relationships')
    if span is None:
        new_lines = ['relationships:']
        for entry in entries:
            new_lines.extend(_render_relationship_entry(entry, '  '))
        lines[fm[1]:fm[1]] = new_lines
    else:
        key_line, end = span
        m = re.match(r'^(relationships\s*:\s*)(.*)$', lines[key_line])
        value, _comment = _split_inline_comment(m.group(2))
        stripped = value.strip()
        if stripped and stripped != '[]':
            raise _EditRefused(
                'the relationships: value is not a block list this tool can '
                'extend; add the folded entries by hand'
            )
        if stripped == '[]':
            lines[key_line] = m.group(1).rstrip()
        item_indent = '  '
        for j in range(key_line + 1, end):
            mi = re.match(r'^(\s*)-\s', lines[j])
            if mi:
                item_indent = mi.group(1)
                break
        insert_at = end
        while insert_at > key_line + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        new_lines = []
        for entry in entries:
            new_lines.extend(_render_relationship_entry(entry, item_indent))
        lines[insert_at:insert_at] = new_lines
    return '\n'.join(lines) + ('\n' if text.endswith('\n') else '')


def _set_fm_scalar(text: str, key: str, rendered_value: str) -> str:
    """Set one top-level frontmatter key to a scalar (replace or append).

    An existing key's whole span (including any nested block) is replaced by
    the single scalar line; a missing key is appended at the bottom of the
    frontmatter, so the four tombstone fields land together in call order.
    """
    lines = text.splitlines()
    fm = frontmatter_fence_span(lines)
    if fm is None:
        raise _EditRefused('the record has no frontmatter to edit')
    span = _fm_key_span(lines, fm[0], fm[1], key)
    new_line = f'{key}: {rendered_value}'
    if span is None:
        lines.insert(fm[1], new_line)
    else:
        lines[span[0]:span[1]] = [new_line]
    return '\n'.join(lines) + ('\n' if text.endswith('\n') else '')


def _remove_fm_key(text: str, key: str) -> str:
    """Remove one top-level frontmatter key and its nested block, if present."""
    lines = text.splitlines()
    fm = frontmatter_fence_span(lines)
    if fm is None:
        return text
    span = _fm_key_span(lines, fm[0], fm[1], key)
    if span is None:
        return text
    del lines[span[0]:span[1]]
    return '\n'.join(lines) + ('\n' if text.endswith('\n') else '')


def _replace_fm_inline_list(text: str, key: str, rendered_items: list[str]) -> str:
    """Replace an existing top-level list key with a one-line inline list.

    Used to reduce the tombstone's `aliases:` to the bare P-id so the merged
    person's name-aliases resolve at the survivor rather than clashing. A
    record without the key is left alone (there is nothing to reduce; its
    `name:` stays, as SPEC §9's tombstone keeps its identity readable).
    """
    lines = text.splitlines()
    fm = frontmatter_fence_span(lines)
    if fm is None:
        return text
    span = _fm_key_span(lines, fm[0], fm[1], key)
    if span is None:
        return text
    lines[span[0]:span[1]] = [f'{key}: [{", ".join(rendered_items)}]']
    return '\n'.join(lines) + ('\n' if text.endswith('\n') else '')


def _strip_external_id_keys(text: str, keys: set[str]) -> str:
    """Remove folded keys from the tombstone's `external_ids:`, keeping conflicts.

    A key whose value folded to (or already matched) the survivor is stripped;
    a CONFLICTING key stays on the tombstone so the losing value is never
    silently destroyed - the warning names both values and the tombstone
    keeps the evidence. When nothing remains, the whole key goes.
    """
    if not keys:
        return text
    lines = text.splitlines()
    fm = frontmatter_fence_span(lines)
    if fm is None:
        return text
    span = _fm_key_span(lines, fm[0], fm[1], 'external_ids')
    if span is None:
        return text
    key_line, end = span
    m = re.match(r'^(external_ids\s*:\s*)(.*)$', lines[key_line])
    value, comment = _split_inline_comment(m.group(2))
    stripped = value.strip()
    if stripped.startswith('{') and stripped.endswith('}'):
        inner = stripped[1:-1].strip()
        items = _split_flow_items(inner) if inner else []
        kept = [it for it in items
                if _unquote(it.split(':', 1)[0].strip()) not in keys]
        if kept:
            suffix = f'  {comment}' if comment else ''
            lines[key_line] = m.group(1) + '{' + ', '.join(kept) + '}' + suffix
        else:
            del lines[key_line:end]
    else:
        kept_lines: list[str] = []
        remaining = False
        for j in range(key_line + 1, end):
            ln = lines[j]
            mk = re.match(r'^\s+([^\s#][^:]*?)\s*:', ln)
            if mk and _unquote(mk.group(1).strip()) in keys:
                continue
            if mk:
                remaining = True
            kept_lines.append(ln)
        if remaining:
            lines[key_line + 1:end] = kept_lines
        else:
            del lines[key_line:end]
    return '\n'.join(lines) + ('\n' if text.endswith('\n') else '')


def _scan_person_profiles(archive_root: Path) -> dict[str, tuple[Path, dict]]:
    """Map every P-id under `people/` to its profile (path, frontmatter meta).

    One scan feeds everything the merge needs - locating both records, the
    merged_into chain walk, and the archive-wide alias map - so the verb works
    with a stale or absent index (the §14a3 contract) and never scans twice.
    Companion views are excluded; a record that will not parse is skipped
    (it cannot be merged or resolve names, and lint owns reporting it).
    """
    people_dir = archive_root / 'people'
    out: dict[str, tuple[Path, dict]] = {}
    if not people_dir.is_dir():
        return out
    for path in sorted(people_dir.rglob('*.md')):
        parsed = parse_filename(path)
        if not parsed or parsed.get('id_type') != 'P' or parsed.get('is_companion'):
            continue
        try:
            rec = read_record(path)
        except Exception:  # noqa: BLE001 - unreadable record cannot take part
            continue
        meta = rec.get('meta') or {}
        if not meta:
            # read_record reports junk via parse_errors rather than raising, so
            # a file with no parseable frontmatter must be skipped here, not
            # caught above - otherwise a lookalike filename ending in the same
            # _{P-id}.md suffix can shadow the real record, and which of the
            # two wins depends on Path sort order (case-insensitive on Windows
            # only).
            continue
        pid = normalize_id(parsed['id_str'])
        if pid not in out:
            out[pid] = (path, meta)
    return out


# The GENERATED companion view kinds the merge cleans up. `research` is a
# companion by filename grammar too, but it is human-authored - never deleted.
_GENERATED_VIEW_KINDS = frozenset({'timeline', 'sources-index', 'draft-queue'})


def _merged_companion_views(
    archive_root: Path, person_id: str,
) -> tuple[list[Path], list[Path]]:
    """The merged person's companion VIEW files: (generated, lookalikes).

    A merge leaves the tombstone with no curated life to view, so its
    timeline / sources-index / draft-queue files are deleted with the merge
    (regeneration is prevented by the tombstone's tier strip - `fha views
    refresh` only generates for curated persons). Only files that carry the
    GENERATED header are deletable; a human file parked at a companion
    filename is never touched (AGENTS.md: never delete without instruction)
    and is returned separately so the merge can name it as a loose end.
    """
    people_dir = archive_root / 'people'
    generated: list[Path] = []
    lookalike: list[Path] = []
    if not people_dir.is_dir():
        return generated, lookalike
    for path in sorted(people_dir.rglob('*.md')):
        parsed = parse_filename(path)
        if not parsed or parsed.get('id_type') != 'P' or not parsed.get('is_companion'):
            continue
        if normalize_id(parsed['id_str']) != person_id:
            continue
        if parsed.get('kind') not in _GENERATED_VIEW_KINDS:
            continue
        (generated if is_generated_file(path) else lookalike).append(path)
    return generated, lookalike


def _final_survivor(profiles: dict[str, tuple[Path, dict]], person_id: str) -> str:
    """Follow a `merged_into` chain to its living end (cycle-safe).

    SPEC §9: tools resolve references *through* merged_into. Used to name the
    chain's final survivor when someone tries to merge INTO a tombstone - the
    refusal tells them where the merge should actually land.
    """
    seen: set[str] = set()
    current = person_id
    while current not in seen:
        seen.add(current)
        entry = profiles.get(current)
        if entry is None:
            return current
        meta = entry[1]
        if not is_merged_meta(meta):
            return current
        nxt = normalize_id(str(meta.get('merged_into') or ''))
        if not nxt:
            return current
        current = nxt
    return current


def _relationship_edge_key(entry: dict, alias_map: dict[str, str] | None) -> tuple:
    """The dedupe identity of one relationships entry: (to, type, subtype).

    `to:` compares by resolved P-id when it resolves, else by its normalized
    text; a missing `subtype` means `biological` (the SPEC §8.2 default), so
    an explicit `subtype: biological` and an omitted one are the same edge.
    """
    to_raw = str(entry.get('to') or '')
    to_key = resolve_typed_ref(to_raw, alias_map, want='P') \
        or strip_link_wrapper(to_raw).strip().lower()
    type_key = str(entry.get('type') or '').strip().lower()
    subtype_key = str(entry.get('subtype') or 'biological').strip().lower()
    return to_key, type_key, subtype_key


def run_confirm_merge(
    archive_root: Path, *, person_merged: str, into: str, reason: str,
    dry_run: bool = False,
) -> Result:
    """Enact a human-confirmed identity merge - the full SPEC §9 write.

    `data` is {'status', 'merged', 'survivor', 'folded': {counts},
    'relinked_claims', 'relinked_profiles', 'prose_refs_remaining',
    'renamed_to', 'deleted_views'}. The plan is built completely in memory
    (validation, folds, tombstone, every relink, the generated companion
    views to delete) before anything is written; `--dry-run` prints that
    plan as per-file unified diffs plus the pending rename/deletions and
    writes nothing. Live mode applies with an undo journal and rolls
    everything back on any failure. Exit: 0 clean or idempotent `already`;
    1 when the merge landed but carries warnings (an external-id conflict,
    evidence of a relationship between the two); 3 for a refusal or a
    rolled-back failure.

    Deliberately out of scope: the split (`confirm separate` stays a guided
    human task, SPEC §9) and evidence judgment - the merge-identities skill
    lays out the case and the human decides; this verb enacts, warning (never
    refusing) when the records themselves hint the two may be different
    people.
    """
    result = Result(data={
        'status': None, 'merged': None, 'survivor': None, 'folded': {},
        'relinked_claims': 0, 'relinked_profiles': 0,
        'prose_refs_remaining': 0, 'renamed_to': None, 'deleted_views': [],
    })

    # ── Validation: everything refusable is refused before any write ────────
    for label, pid in (('person to merge', person_merged), ('--into survivor', into)):
        if not (is_valid_id(pid) and id_type_of(pid) == 'P'):
            return _fail(result, 'invalid-id',
                         f'The {label} argument {pid!r} is not a valid person ID. '
                         'P-ids look like P-de957bcda1.')
    pm, ps = normalize_id(person_merged), normalize_id(into)
    pm_disp, ps_disp = fmt_id_display(pm), fmt_id_display(ps)
    result.data['merged'] = pm_disp
    result.data['survivor'] = ps_disp
    if pm == ps:
        return _fail(result, 'same-person',
                     'A person cannot be merged into themselves - pass two '
                     'different P-ids.')
    if not (reason or '').strip():
        return _fail(result, 'failed',
                     'Pass --reason "<why you decided these are one person>" - it '
                     'is kept on the tombstone as merge_reason: so the decision '
                     'is never lost.')
    reason = reason.strip()

    profiles = _scan_person_profiles(archive_root)
    for label, pid in (('person to merge', pm), ('--into survivor', ps)):
        if pid not in profiles:
            # A not-found here is exit 3 (via _fail), NOT the exit-1 posture the
            # other `confirm` verbs use for a missing target. Deliberate: merge
            # reserves exit 1 for "the merge LANDED but carries warnings", so a
            # not-found returning 1 would be indistinguishable from a successful
            # merge-with-homework - the one confusion this verb must not create
            # (audit flag 14; TOOLING §14a3).
            return _fail(result, 'not-found',
                         f'No person record for {fmt_id_display(pid)} (the {label}) '
                         f'under {archive_root / "people"}. Check the ID with '
                         f'`fha find {fmt_id_display(pid)}`.')
    merged_path, merged_meta = profiles[pm]
    survivor_path, survivor_meta = profiles[ps]

    # Idempotence and chain checks come before anything else so a re-run of a
    # finished merge is a clean no-op, matching confirm cooccur's `already`.
    if is_merged_meta(merged_meta):
        existing_target = normalize_id(str(merged_meta.get('merged_into') or ''))
        if existing_target == ps:
            result.data['status'] = 'already'
            result.add('info',
                       f'{pm_disp} is already merged into {ps_disp}. Nothing to do.')
            return result
        return _fail(result, 'already-merged-elsewhere',
                     f'{pm_disp} is already merged into '
                     f'{fmt_id_display(existing_target) or "another person"}. To '
                     f'move it to {ps_disp} instead, undo the earlier merge by '
                     'hand first (edit the tombstone record), then re-run.')
    if is_merged_meta(survivor_meta):
        final = _final_survivor(profiles, ps)
        final_disp = fmt_id_display(final)
        return _fail(result, 'merged-survivor',
                     f'{ps_disp} is itself merged (a tombstone). Merge into the '
                     f'chain\'s final survivor instead: fha confirm merge '
                     f'{pm_disp} --into {final_disp} --reason "..."')

    dest = merged_path.with_name(f'MERGED-INTO-{ps_disp}__{merged_path.name}')
    if dest.exists():
        return _fail(result, 'rename-collision',
                     f'Cannot rename the merged record: {dest.name} already exists '
                     f'in {dest.parent}. Move that file aside, then retry.')

    # The merged person's generated companion views (timeline, sources-index,
    # draft-queue) go with the merge: they describe a life the survivor now
    # owns, and left behind they sit beside the tombstone forever (refresh
    # only regenerates for curated persons, and the tombstone's tier is
    # stripped below). A companion-NAMED file without the GENERATED header is
    # human-owned: it is left in place and named as a loose end.
    view_files, view_lookalikes = _merged_companion_views(archive_root, pm)
    result.data['deleted_views'] = [str(p) for p in view_files]
    if view_lookalikes:
        names = ', '.join(p.name for p in view_lookalikes)
        result.add('warning',
                   f'{names}: named like a generated companion view of the '
                   'merged person but missing the GENERATED header, so it is '
                   'human-written and was left in place. Review it and remove '
                   'or rename it by hand if it is stale.')

    # Names resolve through the archive-wide alias map (built from every person
    # record), so an ambiguous name - two people sharing it - never resolves
    # and is never relinked by guess; that is lint's clash to surface.
    alias_map = build_alias_map(
        dict(meta, id=pid) for pid, (_p, meta) in profiles.items()
    )
    token_re = _person_token_re(pm)

    # ── Plan: survivor folds ────────────────────────────────────────────────
    folded = {'name_variants': 0, 'aliases': 0, 'external_ids': 0,
              'external_id_conflicts': 0, 'relationships': 0}
    try:
        survivor_before = read_text_exact(survivor_path)
        merged_before = read_text_exact(merged_path)
    except OSError as e:
        return _fail(result, 'failed', f'cannot read the person records: {e}')

    survivor_text = survivor_before

    # Fold names: merged name + variants into the survivor's name_variants,
    # deduped against everything the survivor already displays. A restricted
    # `{value:, restricted: true}` mapping folds as a mapping (SPEC §18) so
    # the privacy flag travels with the name.
    survivor_known = set()
    if survivor_meta.get('name'):
        survivor_known.add(str(survivor_meta['name']).strip().lower())
    survivor_variants = survivor_meta.get('name_variants') or []
    if isinstance(survivor_variants, (list, tuple)):
        for v in survivor_variants:
            val = _variant_value(v)
            if val:
                survivor_known.add(val.strip().lower())
    # Snapshot the PRE-fold display names now: the variant-fold loop below
    # grows survivor_known as it dedupes, and the aliases fold must compare
    # against what the survivor knew before - the freshly folded names are
    # exactly what the aliases mirror needs to gain.
    survivor_prefold_known = set(survivor_known)
    fold_variant_items: list[str] = []
    merged_name_candidates: list[object] = []
    if merged_meta.get('name'):
        merged_name_candidates.append(str(merged_meta['name']))
    merged_variants = merged_meta.get('name_variants') or []
    if isinstance(merged_variants, (list, tuple)):
        merged_name_candidates.extend(merged_variants)
    for cand in merged_name_candidates:
        val = _variant_value(cand)
        if not val or val.strip().lower() in survivor_known:
            continue
        survivor_known.add(val.strip().lower())
        fold_variant_items.append(_render_scalar(cand))
        folded['name_variants'] += 1

    # Fold alias stems. The survivor's aliases: is the tool-maintained mirror
    # (id + name + variants + human stems); when it exists, the folded names
    # join it so the mirror stays complete. When it does not exist, only the
    # merged record's HUMAN STEMS force its creation - name/variants already
    # resolve through the name_variants fold above. A RESTRICTED variant's
    # value never enters aliases: as plain text - it would strip the privacy
    # marker from a name someone no longer uses; it resolves through the
    # folded `{value:, restricted: true}` mapping instead (SPEC §18).
    def _alias_strings(meta: dict) -> list[str]:
        return [strip_link_wrapper(str(a)).strip() for a in (meta.get('aliases') or [])]

    def _is_restricted_variant(variant) -> bool:
        return isinstance(variant, dict) and \
            variant.get('restricted') not in (None, False, '', 'false')

    survivor_alias_known = {a.lower() for a in _alias_strings(survivor_meta) if a}
    survivor_alias_known |= survivor_prefold_known
    survivor_alias_known.add(ps)
    merged_variant_values = {v.strip().lower() for v in
                             (_variant_value(x) for x in merged_name_candidates) if v}
    merged_public_names = [
        v for v in (_variant_value(x) for x in merged_name_candidates
                    if not _is_restricted_variant(x)) if v
    ]
    merged_stems: list[str] = []
    fold_alias_items: list[str] = []
    for a in _alias_strings(merged_meta):
        if not a or normalize_id(a) == pm or id_type_of(a):
            continue
        if a.lower() not in merged_variant_values:
            merged_stems.append(a)
    survivor_has_aliases = bool(survivor_meta.get('aliases'))
    alias_candidates: list[str] = []
    if survivor_has_aliases:
        alias_candidates = merged_public_names + merged_stems
    elif merged_stems:
        alias_candidates = merged_stems
    for a in alias_candidates:
        if a.strip().lower() in survivor_alias_known:
            continue
        survivor_alias_known.add(a.strip().lower())
        fold_alias_items.append(_yaml_inline(a))
        folded['aliases'] += 1
    if fold_alias_items and not survivor_has_aliases:
        # The fold is about to CREATE the survivor's aliases: list. A
        # non-empty aliases: list must carry the record's own ID (lint W111 -
        # the line that makes [[P-...]] click through in Obsidian), so the
        # merge writes it first rather than minting an instant warning. Not
        # counted in folded['aliases']: it is the survivor's own id, nothing
        # folded from the merged record.
        fold_alias_items.insert(0, ps_disp)

    # Fold external ids; a same-key different-value conflict keeps the
    # survivor's value and is NEVER silently resolved (owner decision) - the
    # warning names both values and the tombstone keeps the losing one.
    merged_ext = merged_meta.get('external_ids')
    survivor_ext = survivor_meta.get('external_ids')
    merged_ext = merged_ext if isinstance(merged_ext, dict) else {}
    survivor_ext = survivor_ext if isinstance(survivor_ext, dict) else {}
    fold_ext_pairs: list[tuple[str, object]] = []
    conflict_keys: set[str] = set()
    strip_ext_keys: set[str] = set()
    for key, value in merged_ext.items():
        key_s = str(key)
        if key_s not in {str(k) for k in survivor_ext}:
            fold_ext_pairs.append((key_s, value))
            strip_ext_keys.add(key_s)
            folded['external_ids'] += 1
        elif str(survivor_ext.get(key_s)).strip() == str(value).strip():
            strip_ext_keys.add(key_s)
        else:
            conflict_keys.add(key_s)
            folded['external_id_conflicts'] += 1
            result.add('warning',
                       f'external_ids conflict on {key_s!r}: the survivor has '
                       f'{str(survivor_ext.get(key_s))!r}, the merged record had '
                       f'{str(value)!r}. Kept the survivor\'s; the merged '
                       'record\'s value stays on its tombstone - reconcile by '
                       'hand if the survivor\'s is the wrong one.')

    # Fold relationships entries not already present (same to+type+subtype);
    # an edge pointing AT the survivor is evidence the two may be different
    # people, so it is skipped and warned about, never copied as a self-edge.
    def _rel_entries(meta: dict) -> list[dict]:
        block = meta.get('relationships')
        return [e for e in block if isinstance(e, dict)] if isinstance(block, list) else []

    survivor_edges = {_relationship_edge_key(e, alias_map) for e in _rel_entries(survivor_meta)}
    survivor_block_claims = {
        normalize_id(strip_link_wrapper(str(e.get('claim'))))
        for e in _rel_entries(survivor_meta) if e.get('claim')
    }
    fold_rel_entries: list[dict] = []
    skipped_self_types: list[str] = []
    for entry in _rel_entries(merged_meta):
        to_resolved = resolve_typed_ref(str(entry.get('to') or ''), alias_map, want='P')
        if to_resolved in (ps, pm):
            skipped_self_types.append(str(entry.get('type') or 'relationship'))
            continue
        edge_key = _relationship_edge_key(entry, alias_map)
        if edge_key in survivor_edges:
            continue
        survivor_edges.add(edge_key)
        fold_rel_entries.append(entry)
        if entry.get('claim'):
            survivor_block_claims.add(normalize_id(strip_link_wrapper(str(entry.get('claim')))))
        folded['relationships'] += 1
    if skipped_self_types:
        result.add('warning',
                   'The record being merged lists a relationship to the survivor '
                   f'({", ".join(sorted(set(skipped_self_types)))}) - evidence the '
                   'two may be different people. Re-check the decision if in '
                   'doubt; that edge was not copied onto the survivor.')

    try:
        survivor_text = _fold_fm_list(survivor_text, 'name_variants', fold_variant_items)
        survivor_text = _fold_fm_list(survivor_text, 'aliases', fold_alias_items,
                                      insert_after=('name_variants', 'name', 'id'))
        survivor_text = _fold_external_ids(survivor_text, fold_ext_pairs)
        survivor_text = _fold_relationship_entries(survivor_text, fold_rel_entries)
        # The survivor's own relationships may name the merged person; those
        # entries repoint like any other profile's (the self-edge they become
        # is covered by the evidence warning above).
        survivor_text, survivor_self_relinks = _relink_relationship_targets(
            survivor_text, pm, ps, alias_map, token_re)
        problem = _merge_fm_problem(survivor_before, survivor_text,
                                    _MERGE_SURVIVOR_KEYS)
    except _EditRefused as e:
        return _fail(result, 'refused',
                     f'{survivor_path.name}: {e}. Nothing was written.')
    if problem is not None:
        return _fail(result, 'refused',
                     f'{survivor_path.name}: {problem}, so the merge was '
                     'abandoned. Nothing was written.')
    if survivor_self_relinks:
        result.add('warning',
                   f'The survivor\'s own relationships: block listed the merged '
                   f'person {survivor_self_relinks} time(s) - those entries now '
                   'point at the survivor themselves. Evidence the two may be '
                   'different people; review (and likely remove) those entries.')

    # ── Plan: tombstone ─────────────────────────────────────────────────────
    # Folds and strip land together: a folded name or sourced relationship
    # left on the tombstone would clash as an alias or trip W115 (the
    # reconciliation check has no merged filter), so steps 2 and 3 of SPEC §9
    # are one atomic plan.
    try:
        tombstone_text = _remove_fm_key(merged_before, 'name_variants')
        tombstone_text = _remove_fm_key(tombstone_text, 'relationships')
        # The tier goes too: a tombstone is a redirect, not a profile (SPEC
        # §9), and a `tier: curated` left behind would keep `fha views
        # refresh` regenerating companion views for a person who no longer
        # exists as an identity. Absent tier reads as stub everywhere.
        tombstone_text = _remove_fm_key(tombstone_text, 'tier')
        tombstone_text = _strip_external_id_keys(tombstone_text, strip_ext_keys)
        tombstone_text = _replace_fm_inline_list(tombstone_text, 'aliases', [pm_disp])
        tombstone_text = _set_fm_scalar(tombstone_text, 'status', 'merged')
        tombstone_text = _set_fm_scalar(tombstone_text, 'merged_into', ps_disp)
        tombstone_text = _set_fm_scalar(tombstone_text, 'merge_reason', _yaml_inline(reason))
        tombstone_text = _set_fm_scalar(tombstone_text, 'merged_date', _today())
        problem = _merge_fm_problem(merged_before, tombstone_text,
                                    _MERGE_TOMBSTONE_KEYS)
    except _EditRefused as e:
        return _fail(result, 'refused',
                     f'{merged_path.name}: {e}. Nothing was written.')
    if problem is not None:
        return _fail(result, 'refused',
                     f'{merged_path.name}: {problem}, so the merge was abandoned. '
                     'Nothing was written.')

    # ── Plan: relink claims + source frontmatter, then other profiles ───────
    planned: dict[Path, tuple[str, str]] = {}
    if survivor_text != survivor_before:
        planned[survivor_path] = (survivor_before, survivor_text)
    planned[merged_path] = (merged_before, tombstone_text)

    text_cache: dict[Path, str] = {
        survivor_path: survivor_text, merged_path: tombstone_text,
    }
    relinked_claims = 0
    relinked_profiles = 0
    both_named_claims: list[str] = []
    edited_claim_meta: list[dict] = []
    sources_dir = archive_root / 'sources'
    if sources_dir.is_dir():
        for path in sorted(sources_dir.rglob('*.md')):
            try:
                before = read_text_exact(path)
            except OSError as e:
                return _fail(result, 'failed', f'cannot read {path}: {e}')
            try:
                rec = read_record(path)
            except Exception:  # noqa: BLE001 - handled by the token check below
                rec = None
            if rec is not None and rec.get('unfenced_claims'):
                if any(pm in _claim_resolved_persons(c, alias_map)
                       for c in rec.get('claims') or [] if isinstance(c, dict)):
                    return _fail(result, 'refused',
                                 f'{path} keeps its claims outside a ```yaml fence '
                                 'and they reference the person being merged. Wrap '
                                 'the block first (`fha lint --fix-claims-fence` '
                                 'offers to), then re-run. Nothing was written.')
            try:
                text, edited, both = _relink_claims_in_source(
                    before, pm, ps, alias_map, token_re)
                text, people_changed = _relink_people_frontmatter(
                    text, pm, ps, alias_map, token_re)
            except _EditRefused as e:
                return _fail(result, 'refused',
                             f'{path}: {e}. Nothing was written.')
            relinked_claims += len(edited)
            both_named_claims.extend(both)
            if rec is not None and edited:
                edited_ids = {normalize_id(str(x)) for x in edited if is_valid_id(str(x))}
                for c in rec.get('claims') or []:
                    if isinstance(c, dict) and \
                            normalize_id(str(c.get('id') or '')) in edited_ids:
                        edited_claim_meta.append(c)
            if people_changed:
                relinked_profiles += 1
            if text != before:
                planned[path] = (before, text)
                text_cache[path] = text
            else:
                text_cache[path] = before

    for pid, (path, _meta) in profiles.items():
        if path in (merged_path, survivor_path):
            continue
        try:
            before = read_text_exact(path)
        except OSError as e:
            return _fail(result, 'failed', f'cannot read {path}: {e}')
        try:
            text, n_changed = _relink_relationship_targets(
                before, pm, ps, alias_map, token_re)
        except _EditRefused as e:
            return _fail(result, 'refused', f'{path}: {e}. Nothing was written.')
        if n_changed:
            relinked_profiles += 1
            planned[path] = (before, text)
        text_cache[path] = text

    result.data['folded'] = folded
    result.data['relinked_claims'] = relinked_claims
    result.data['relinked_profiles'] = relinked_profiles
    result.data['renamed_to'] = str(dest)

    if both_named_claims:
        ids = ', '.join(dict.fromkeys(both_named_claims))
        result.add('warning',
                   f'Relationship claim(s) {ids} recorded an edge between these '
                   'two people - evidence they may be two different people. '
                   'Review those claims after the merge (they now name only the '
                   'survivor).')

    # W115 heads-up: the reverse reconciliation check expects an opted-in
    # relationships: block to APPLY every accepted kin claim naming its
    # person. Relinked accepted kin claims the survivor's block does not link
    # will surface as W115 - warn now so the human is not surprised by lint.
    survivor_opted_in = bool(_rel_entries(survivor_meta)) or bool(fold_rel_entries)
    if survivor_opted_in:
        uncovered = []
        for c in edited_claim_meta:
            if str(c.get('status') or '').strip().lower() != 'accepted':
                continue
            if str(c.get('type') or '').strip().lower() not in _TOMBSTONE_KIN_TYPES:
                continue
            cid = normalize_id(str(c.get('id') or ''))
            if cid and cid not in survivor_block_claims:
                uncovered.append(fmt_id_display(cid))
        if uncovered:
            result.add('warning',
                       f'The survivor\'s relationships: block does not yet apply '
                       f'accepted claim(s) {", ".join(uncovered)}, which now name '
                       'them - `fha lint` will list this (W115). Add the entries '
                       'to the block, or remove the block if it is not meant to '
                       'be complete.')

    # Prose mentions are deliberately left for W107's gradual-cleanup list
    # (SPEC §9); count them against the PLANNED text state so the number is
    # what lint will actually see after the merge. Generated views are
    # skipped - they are rebuilt, not cleaned up.
    prose_refs = 0
    for tree in ('people', 'sources', 'notes', 'places'):
        tree_dir = archive_root / tree
        if not tree_dir.is_dir():
            continue
        for path in sorted(tree_dir.rglob('*')):
            if not path.is_file() or path.suffix.lower() not in ('.md', '.yaml', '.yml'):
                continue
            if path == merged_path:
                continue
            text = text_cache.get(path)
            if text is None:
                try:
                    text = path.read_text(encoding='utf-8', errors='ignore')
                except OSError:
                    continue
            if '<!-- GENERATED' in text[:400]:
                continue
            prose_refs += len(token_re.findall(text))
    result.data['prose_refs_remaining'] = prose_refs

    has_warnings = any(m.level == 'warning' for m in result.messages)

    # ── Dry-run: the complete, honest preview ───────────────────────────────
    if dry_run:
        result.data['status'] = 'ok'
        result.add('info', f'[dry-run] Would merge {pm_disp} into {ps_disp}.')
        for path, (before, after) in planned.items():
            for dline in difflib.unified_diff(
                before.splitlines(), after.splitlines(),
                fromfile=f'{path} (before)', tofile=f'{path} (after)', lineterm='',
            ):
                result.add('info', dline)
        result.add('info', f'[dry-run] Would rename {merged_path.name} -> {dest.name}')
        for p in view_files:
            result.add('info',
                       f'[dry-run] Would delete the generated companion view '
                       f'{p.name} (the merged life now belongs to the survivor).')
        result.add('info',
                   f'[dry-run] Would relink {relinked_claims} claim(s) and '
                   f'{relinked_profiles} other record(s); {prose_refs} prose '
                   f'mention(s) of {pm_disp} would remain for gradual cleanup (W107).')
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        if has_warnings:
            result.exit_code = EXIT_WARNINGS
        return result

    # ── Apply: undo journal, rename last ────────────────────────────────────
    undo: list = []

    def _rollback_merge() -> None:
        for fn in reversed(undo):
            try:
                fn()
            except OSError:
                pass
        result.changed.clear()

    for path, (before, after) in planned.items():
        # Register the restore BEFORE writing (the convert_mining pattern): a
        # write that fails partway can still leave a half-written file, and
        # restoring the pristine text covers that too.
        undo.append(lambda p=path, t=before: write_text_exact(p, t))
        try:
            write_text_exact(path, reapply_newline(after, before))
        except OSError as e:
            _rollback_merge()
            return _fail(result, 'failed',
                         f'cannot write {path}: {e}; every earlier write was '
                         'rolled back - nothing to clean up.')

    # Companion-view deletion sits between the content writes and the rename
    # (rename stays LAST): each file's bytes are captured for the undo journal
    # before the unlink, so a mid-apply failure restores them verbatim.
    for path in view_files:
        try:
            original_bytes = path.read_bytes()
        except OSError as e:
            _rollback_merge()
            return _fail(result, 'failed',
                         f'cannot read {path} before deleting it: {e}; every '
                         'earlier write was rolled back - nothing to clean up.')
        undo.append(lambda p=path, b=original_bytes: p.write_bytes(b))
        try:
            path.unlink()
        except OSError as e:
            _rollback_merge()
            return _fail(result, 'failed',
                         f'cannot delete the generated view {path}: {e}; every '
                         'earlier write was rolled back - nothing to clean up.')

    try:
        undo.append(lambda src=dest, back=merged_path: src.rename(back))
        merged_path.rename(dest)
    except OSError as e:
        _rollback_merge()
        return _fail(result, 'failed',
                     f'cannot rename {merged_path.name} -> {dest.name}: {e}; '
                     'every content write was rolled back - nothing to clean up.')

    for path in planned:
        if path != merged_path:
            result.note_changed(path)
    for path in view_files:
        result.note_changed(path)
    result.note_changed(dest)

    result.data['status'] = 'ok'
    result.add('info',
               f'Merged {pm_disp} into {ps_disp}. The old record is kept forever '
               f'as a tombstone, renamed to {dest.name}.', path=dest)
    if view_files:
        result.add('info',
                   f'Deleted {len(view_files)} generated companion view(s) of '
                   f'the merged person ({", ".join(p.name for p in view_files)}) '
                   '- that life now shows on the survivor\'s views instead.')
    result.add('info',
               f'Folded into the survivor: {folded["name_variants"]} name '
               f'variant(s), {folded["aliases"]} alias(es), '
               f'{folded["external_ids"]} external id(s), '
               f'{folded["relationships"]} relationship entr(y/ies).')
    result.add('info',
               f'Relinked {relinked_claims} claim(s) and {relinked_profiles} '
               f'other record(s) to {ps_disp}.')
    if prose_refs:
        result.add('info',
                   f'{prose_refs} prose mention(s) of {pm_disp} remain - they '
                   'still resolve through the tombstone, and `fha lint` lists '
                   'them (W107) for gradual cleanup.')
    result.add('info', 'Next: run `fha index` so the merge enters the query surface.',
               next_step='fha index')
    if str(survivor_meta.get('tier') or '').strip().lower() == 'curated':
        result.add('info',
                   f'Then refresh the survivor\'s views - `fha views timeline '
                   f'{ps_disp}`, `fha views sources-index {ps_disp}`, and '
                   f'`fha views draft-queue {ps_disp}` - so the relinked life '
                   'shows up there.',
                   next_step=f'fha views timeline {ps_disp}')
    result.add('info',
               'Finish with `fha lint` to confirm nothing new points at the '
               'merged id.', next_step='fha lint')
    if has_warnings:
        result.exit_code = EXIT_WARNINGS
    return result


# ── Result helpers ──────────────────────────────────────────────────────────────

def _fail(result: Result, status: str, message: str) -> Result:
    """A plain hard refusal (exit 3, error-level) - delegates to _lib.result_fail."""
    return result_fail(result, status, message)


def _notfound(result: Result, message: str, next_step: str | None = None) -> Result:
    """A not-found warning (exit 1) - delegates to _lib.result_fail."""
    return result_fail(result, 'not-found', message,
                       exit_code=EXIT_WARNINGS, level='warning', next_step=next_step)


# ── CLI ─────────────────────────────────────────────────────────────────────────

def _emit(result: Result) -> int:
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def _cmd_xref(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_confirm_xref(
        archive_root, claim_a=args.claim_a, claim_b=args.claim_b,
        relation=args.relation, dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_cooccur(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_confirm_cooccur(
        archive_root, person_a=args.person_a, person_b=args.person_b,
        source_id=args.source, subtype=args.subtype,
        accept=bool(getattr(args, 'accept', False)),
        reviewed=getattr(args, 'reviewed', None),
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_dismiss(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_dismiss(
        archive_root, person_a=args.person_a, person_b=args.person_b,
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_place(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_confirm_place(
        archive_root, claim_ids=args.claim_ids, name=getattr(args, 'name', None),
        hierarchy=getattr(args, 'hierarchy', None), into=getattr(args, 'into', None),
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_discovery(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    refs = []
    if getattr(args, 'refs', None):
        refs = [r.strip() for r in args.refs.split(',') if r.strip()]
    return _emit(run_add_discovery(
        archive_root, text=args.text, refs=refs,
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_draft(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_accept_draft(
        archive_root, person_id=args.person_id,
        dry_run=bool(getattr(args, 'dry_run', False))))


def _cmd_merge(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    return _emit(run_confirm_merge(
        archive_root, person_merged=args.person_merged, into=args.into,
        reason=args.reason, dry_run=bool(getattr(args, 'dry_run', False))))


def _add_subcommands(subs: argparse._SubParsersAction, *, suppress_root: bool) -> None:
    """Build the seven `confirm` subparsers (shared by main fha + standalone)."""
    def root_arg(p: argparse.ArgumentParser) -> None:
        if suppress_root:
            p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                           help='Archive root (auto-detected if omitted).')
        else:
            p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')

    xref_p = subs.add_parser('xref', help='Confirm an xref pair: write corroborates/contradicts links')
    xref_p.add_argument('claim_a', metavar='C-id-a', help='First claim (e.g. C-fd0000001a).')
    xref_p.add_argument('claim_b', metavar='C-id-b', help='Second claim.')
    xref_p.add_argument('--as', dest='relation', required=True, choices=XREF_RELATIONS,
                        help='The link to write between the two claims.')
    xref_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                        help='Preview the change without writing.')
    root_arg(xref_p)
    xref_p.set_defaults(func=_cmd_xref)

    co_p = subs.add_parser('cooccur', help='Confirm a co-occurrence pair: mint a relationship claim')
    co_p.add_argument('person_a', metavar='P-id-a', help='First person.')
    co_p.add_argument('person_b', metavar='P-id-b', help='Second person.')
    co_p.add_argument('--source', metavar='S-id', required=True,
                      help='The source that supports this connection (cited on the claim).')
    co_p.add_argument('--subtype', required=True, choices=SOCIAL_SUBTYPES,
                      help='The social relationship subtype.')
    co_p.add_argument('--accept', action='store_true',
                      help='Mint the claim accepted (treat this confirm as the review) rather than suggested.')
    co_p.add_argument('--reviewed', metavar='DATE',
                      help='Review date for --accept (YYYY-MM-DD); defaults to today.')
    co_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                      help='Preview the change without writing.')
    root_arg(co_p)
    co_p.set_defaults(func=_cmd_cooccur)

    dis_p = subs.add_parser('dismiss', help='Dismiss a co-occurrence pair (tombstone, not re-proposed)')
    dis_p.add_argument('person_a', metavar='P-id-a', help='First person.')
    dis_p.add_argument('person_b', metavar='P-id-b', help='Second person.')
    dis_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                       help='Preview the change without writing.')
    root_arg(dis_p)
    dis_p.set_defaults(func=_cmd_dismiss)

    pl_p = subs.add_parser('place', help='Register a place-text cluster and relink its claims')
    pl_p.add_argument('claim_ids', metavar='C-id', nargs='+', help='Claim(s) to relink to the place.')
    pl_p.add_argument('--name', metavar='NAME', help='Name for a new place (required unless --into).')
    pl_p.add_argument('--hierarchy', metavar='TEXT', help='Optional hierarchy, e.g. "Topeka, Kansas, USA".')
    pl_p.add_argument('--into', metavar='L-id', help='Merge into this existing place instead of minting.')
    pl_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                      help='Preview the change without writing.')
    root_arg(pl_p)
    pl_p.set_defaults(func=_cmd_place)

    dv_p = subs.add_parser('discovery', help='Append a dated entry to notes/discoveries.md')
    dv_p.add_argument('text', metavar='TEXT', help='What was discovered.')
    dv_p.add_argument('--refs', metavar='IDS', help='Comma-separated S-/P-/C-/L-/H- refs.')
    dv_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                      help='Preview the change without writing.')
    root_arg(dv_p)
    dv_p.set_defaults(func=_cmd_discovery)

    dr_p = subs.add_parser('draft', help='Accept AI-DRAFT prose markers in a person profile')
    dr_p.add_argument('person_id', metavar='P-id', help='The person whose profile to accept drafts in.')
    dr_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                      help='Preview the change without writing.')
    root_arg(dr_p)
    dr_p.set_defaults(func=_cmd_draft)

    mg_p = subs.add_parser(
        'merge',
        help='Enact a human-confirmed identity merge: tombstone + rename + fold + relink (SPEC §9)')
    mg_p.add_argument('person_merged', metavar='P-merged',
                      help='The duplicate record that becomes the tombstone.')
    mg_p.add_argument('--into', metavar='P-survivor', required=True,
                      help='The record that keeps the canonical home.')
    mg_p.add_argument('--reason', metavar='TEXT', required=True,
                      help='Why these are one person; stored as merge_reason: on the tombstone.')
    mg_p.add_argument('--dry-run', action='store_true', dest='dry_run',
                      help='Preview every edit and the rename without writing.')
    root_arg(mg_p)
    mg_p.set_defaults(func=_cmd_merge)


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Write down a suggestion you've decided to accept.

  fha confirm xref <C-a> <C-b> --as corroborates|contradicts
  fha confirm cooccur <P-a> <P-b> --source <S-id> --subtype friend|associate|neighbor
  fha confirm dismiss <P-a> <P-b>
  fha confirm place <C-id...> (--name NAME | --into <L-id>)
  fha confirm discovery "<text>"
  fha confirm draft <P-id>
  fha confirm merge <P-merged> --into <P-survivor> --reason "<why>"

Each verb turns one kind of decision you have made - a link, a connection, a
place, a note, a draft, an identity merge - into the records. Every verb
previews with --dry-run first."""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'confirm' onto the main fha parser."""
    p = subs.add_parser(
        'confirm',
        help='Write back a decision the human made (xref/cooccur/dismiss/place/discovery/draft/merge)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = p.add_subparsers(dest='confirm_command', metavar='SUBCOMMAND')
    _add_subcommands(sub, suppress_root=True)
    # Bare `fha confirm` (no verb) is a usage error, not a tool failure: exit 2,
    # matching `fha person`/`fha wikitree` (audit flag 15).
    p.set_defaults(func=lambda a: p.print_help() or EXIT_ERRORS)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha confirm',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest='confirm_command', metavar='SUBCOMMAND')
    _add_subcommands(sub, suppress_root=False)
    args = parser.parse_args(argv)
    if not getattr(args, 'func', None):
        parser.print_help()
        return EXIT_ERRORS
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
