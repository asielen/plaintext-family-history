#!/usr/bin/env python3
"""
confirm.py — fha confirm: human-directed write-back for detection candidates
(AGENTS.md, TOOLING §14a / §14a2 / §15a).

  fha confirm xref      <C-a> <C-b> --as corroborates|contradicts [--dry-run]
  fha confirm cooccur   <P-a> <P-b> --source <S-id> --subtype friend|associate|neighbor
                                    [--accept [--reviewed DATE]] [--dry-run]
  fha confirm dismiss   <P-a> <P-b> [--dry-run]
  fha confirm place     <C-id> [<C-id> …] (--name NAME [--hierarchy H] | --into <L-id>) [--dry-run]
  fha confirm discovery "<text>" [--refs S-…,P-…] [--dry-run]
  fha confirm draft     <P-id> [--dry-run]

The deterministic *detection* tools (`fha xref`, `fha cooccur`, `fha places
candidates`) only ever read — they print candidate pairs/clusters for a human to
judge and explicitly leave every write "to a future skill layer." This is that
layer's deterministic floor: once the human has picked a candidate, the
write-back itself is mechanical, so it lives here as a real CLI any front door
(chat now, a UI later) can drive. Keeping the writes in their own tool preserves
the read-only contract the detection tools advertise (a detector that also wrote
would be two owners for one surface).

THE SIX WRITE-BACKS
-------------------
  xref       — confirm an `fha xref` pair: write reciprocal `corroborates:`/
               `contradicts:` claim_links into both claims' source records. A
               contradiction also spawns a templated open question in
               `notes/questions.md` (`origin: tool`, both C-ids referenced) so
               lint E009 ("contradicts: with no open question") stays satisfied —
               the same machinery `fha lint --spawn-questions` uses.
  cooccur    — confirm an `fha cooccur` person-pair: mint a `relationship` claim
               (`subtype: friend|associate|neighbor`, the confirming source
               cited) into that source's `## Claims` block. Minted `suggested`
               by default — the human's confirm proposes the edge; accepting it
               into a load-bearing graph edge still goes through the normal
               step-05 claim review (`fha claim … --status accepted`), which is
               the only place "the human accepts" lives. `--accept` is the
               escape hatch for a human who is treating this confirm *as* the
               review (it stamps `reviewed:` like `fha claim` does).
  dismiss    — record a co-occurrence pair in `.cache/cooccur_dismissed.json`,
               the tombstone `fha cooccur` reads to stop re-proposing a pair.
               Nothing else writes this file; this is its writer.
  place      — register a place-text cluster: mint a new `L-id` place in
               `places/places.yaml` (or merge into an existing one via `--into`)
               and relink the named claims' `place:` to it, so the cluster stops
               surfacing as an unlinked `fha places candidates` group.
  discovery  — append a dated entry (with `[S-]`/`[P-]` refs) to
               `notes/discoveries.md`, the durable research-wins log
               `fha report` §0 leads with.
  draft      — flip a profile's `<!-- AI-DRAFT … -->` markers to
               `<!-- AI-ACCEPTED … (accepted DATE) -->` (AGENTS.md: draft prose
               carries the marker until the human accepts it). Provenance is
               preserved, not erased — the original date/model stay in the marker.

Every verb ships `--dry-run` (previews, writes nothing) and returns a `_lib.Result`
whose `changed[]` lists each file written. Source/registry edits are **surgical**
text edits (not a YAML round-trip) so sibling claims, key order, and hand comments
survive — the same discipline as `fha claim` and `fha places geocode`. The `.md`
files are archive truth, so claims/sources are located by scanning `sources/`
directly; the write works even when `.cache/index.sqlite` is stale or absent.
After a write that changes the query surface, re-run `fha index`.

CODE MAP
--------
  Shared edit helpers
    _today, _EditRefused
    _find_source_path_for_claim   — scan sources/ for the .md holding one C-id
    _find_source_path_by_id       — scan sources/ for one S-id's record
    _find_profile_path            — scan people/ for one P-id's curated profile
    _find_claims_block            — locate the ## Claims ```yaml fence
    _claim_spans / _item_span_for — split the block into claim items, find one
    _parse_inline_list            — read a `key: [a, b]` inline YAML list
    _add_link_to_claim            — append a corroborates/contradicts target
    _set_scalar_on_claim          — set a single scalar key (e.g. place:)
    _append_claim_to_source       — append a whole new claim item to the block

  Verbs (each returns a Result)
    run_confirm_xref, run_confirm_cooccur, run_dismiss,
    run_confirm_place, run_add_discovery, run_accept_draft

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

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    Result,
    configure_utf8_stdout,
    fmt_id_display,
    id_type_of,
    is_valid_id,
    mint_ids,
    normalize_id,
    parse_filename,
    read_record,
    resolve_root_arg,
)

configure_utf8_stdout()

SOCIAL_SUBTYPES = ('friend', 'associate', 'neighbor')
XREF_RELATIONS = ('corroborates', 'contradicts')

# The `id:` key of a claim, anchored as the first key on its line (optionally
# after the list dash). Mirrors claim.py so a C-id mentioned mid-value is never
# mistaken for the claim's own identity.
_CLAIM_ID_KEY_RE = re.compile(r'^\s*(?:-\s+)?id:\s*(C-[0-9a-hjkmnp-tv-z]{10})\b', re.I)


def _today() -> str:
    return datetime.date.today().isoformat()


class _EditRefused(Exception):
    """A surgical edit cannot be performed safely (caller turns into a Result)."""


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
        except Exception:  # noqa: BLE001 — a bad record can't hold our claim
            continue
        for claim in rec.get('claims') or []:
            if isinstance(claim, dict) and claim.get('id') \
                    and normalize_id(str(claim['id'])) == target:
                return path
    return None


def _find_source_path_by_id(archive_root: Path, source_id: str) -> Path | None:
    """Scan `sources/` for the record file whose `_{S-id}.md` suffix matches."""
    target = normalize_id(source_id)
    sources_dir = archive_root / 'sources'
    if not sources_dir.is_dir():
        return None
    for path in sorted(sources_dir.rglob('*.md')):
        parsed = parse_filename(path)
        if parsed and parsed.get('id_str') == target:
            return path
    return None


def _find_profile_path(archive_root: Path, person_id: str) -> Path | None:
    """Scan `people/` for one P-id's *curated profile* (not a companion view)."""
    target = normalize_id(person_id)
    people_dir = archive_root / 'people'
    if not people_dir.is_dir():
        return None
    for path in sorted(people_dir.rglob('*.md')):
        parsed = parse_filename(path)
        if not parsed or parsed.get('id_str') != target:
            continue
        if parsed.get('id_type') == 'P' and not parsed.get('is_companion'):
            return path
    return None


# ── Surgical claim-block editing ───────────────────────────────────────────────

def _find_claims_block(lines: list[str]) -> tuple[int, int] | None:
    """Return (open_fence, close_fence) of the ## Claims ```yaml block, or None."""
    heading = None
    for i, ln in enumerate(lines):
        if re.match(r'^##\s+Claims\b', ln):
            heading = i
            break
    if heading is None:
        return None

    open_fence = None
    for i in range(heading + 1, len(lines)):
        if lines[i].strip() == '```yaml':
            open_fence = i
            break
        if lines[i].startswith('## '):  # next section before any fence
            return None
    if open_fence is None:
        return None

    for i in range(open_fence + 1, len(lines)):
        if lines[i].strip() == '```':
            return open_fence, i
    return None


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


def _item_span_for(lines: list[str], spans: list[tuple[int, int]], claim_id: str) -> tuple[int, int] | None:
    target = normalize_id(claim_id)
    for start, end in spans:
        for j in range(start, end):
            m = _CLAIM_ID_KEY_RE.match(lines[j])
            if m and normalize_id(m.group(1)) == target:
                return start, end
    return None


def _parse_inline_list(raw: str) -> list[str]:
    """Parse a `key: [a, b]` inline YAML list (or a bare scalar) into a list.

    Raises `_EditRefused` for forms this text-edit can't safely extend — an
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


def _add_link_to_claim(
    text: str, claim_id: str, rel: str, target_id: str,
) -> tuple[str, bool, bool]:
    """Append `target_id` to `claim_id`'s `rel:` (corroborates/contradicts) list.

    Returns (new_text, changed, already_present). The link list is rendered as a
    single-line inline YAML list. If the claim has no such key yet, one is
    inserted right after its `status:` line (falling back to the last line of the
    item). Other lines — sibling keys, comments, key order — are untouched.
    """
    target = normalize_id(target_id)
    target_disp = fmt_id_display(target)
    lines = text.splitlines()

    block = _find_claims_block(lines)
    if block is None:
        return text, False, False
    base_indent, spans = _claim_spans(lines, *block)
    span = _item_span_for(lines, spans, claim_id)
    if span is None:
        return text, False, False
    start, end = span
    key_indent = base_indent + '  '

    dash_re = re.compile(r'^' + re.escape(base_indent) + r'-\s+' + re.escape(rel) + r':\s*(.*)$')
    key_re = re.compile(r'^' + re.escape(key_indent) + re.escape(rel) + r':\s*(.*)$')

    for idx in range(start, end):
        ln = lines[idx]
        m_dash = dash_re.match(ln)
        m_key = m_dash or key_re.match(ln)
        if not m_key:
            continue
        items = _parse_inline_list(m_key.group(1))
        if target in [normalize_id(x) for x in items]:
            return text, False, True
        items.append(target_disp)
        prefix = f'{base_indent}- ' if m_dash else key_indent
        lines[idx] = f'{prefix}{rel}: [{", ".join(items)}]'
        trailing = '\n' if text.endswith('\n') else ''
        return '\n'.join(lines) + trailing, True, False

    # No existing rel key — insert one after `status:`, else at end of item.
    status_idx = None
    for idx in range(start, end):
        if re.match(r'^' + re.escape(key_indent) + r'status:', lines[idx]) \
                or re.match(r'^' + re.escape(base_indent) + r'-\s+status:', lines[idx]):
            status_idx = idx
            break
    insert_at = (status_idx + 1) if status_idx is not None else end
    lines.insert(insert_at, f'{key_indent}{rel}: [{target_disp}]')
    trailing = '\n' if text.endswith('\n') else ''
    return '\n'.join(lines) + trailing, True, False


def _set_scalar_on_claim(text: str, claim_id: str, key: str, value: str) -> tuple[str, bool]:
    """Set a single scalar key (e.g. `place: L-id`) on one claim item in place."""
    lines = text.splitlines()
    block = _find_claims_block(lines)
    if block is None:
        return text, False
    base_indent, spans = _claim_spans(lines, *block)
    span = _item_span_for(lines, spans, claim_id)
    if span is None:
        return text, False
    start, end = span
    key_indent = base_indent + '  '

    dash_re = re.compile(r'^' + re.escape(base_indent) + r'-\s+' + re.escape(key) + r':')
    key_re = re.compile(r'^' + re.escape(key_indent) + re.escape(key) + r':')
    for idx in range(start, end):
        if dash_re.match(lines[idx]):
            lines[idx] = f'{base_indent}- {key}: {value}'
            break
        if key_re.match(lines[idx]):
            lines[idx] = f'{key_indent}{key}: {value}'
            break
    else:
        # Insert after id: (falls back to first line of the item).
        id_idx = start
        for idx in range(start, end):
            if _CLAIM_ID_KEY_RE.match(lines[idx]):
                id_idx = idx
                break
        lines.insert(id_idx + 1, f'{key_indent}{key}: {value}')

    trailing = '\n' if text.endswith('\n') else ''
    return '\n'.join(lines) + trailing, True


def _append_claim_to_source(text: str, item_lines: list[str]) -> tuple[str, bool]:
    """Append one new claim item (its full YAML lines) to the ## Claims block."""
    lines = text.splitlines()
    block = _find_claims_block(lines)
    if block is None:
        return text, False
    open_fence, close_fence = block

    new = lines[:close_fence]
    # Separate from any preceding claim with one blank line, matching the
    # readable spacing the example records use between claim items.
    if close_fence > open_fence + 1 and new and new[-1].strip() != '':
        new.append('')
    new.extend(item_lines)
    new.extend(lines[close_fence:])
    trailing = '\n' if text.endswith('\n') else ''
    return '\n'.join(new) + trailing, True


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
                         'C-ids look like C-fd0000001a — a C, a dash, then 10 archive-alphabet '
                         'characters.')
    if relation not in XREF_RELATIONS:
        return _fail(result, 'invalid-relation',
                     f'{relation!r} is not a link type. Use one of: {", ".join(XREF_RELATIONS)}.')

    ca, cb = normalize_id(claim_a), normalize_id(claim_b)
    if ca == cb:
        return _fail(result, 'same-claim',
                     'A claim cannot link to itself — pass two different C-ids.')
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
    try:
        for path, pairs in edits.items():
            before = path.read_text(encoding='utf-8')
            text = before
            for owner, target in pairs:
                text, changed, already = _add_link_to_claim(text, owner, relation, target)
                if not changed and not already:
                    return _notfound(result,
                                     f'Found {fmt_id_display(owner)} in {path} but could not edit '
                                     'its claims block. Check the block by hand.')
                already_all = already_all and (already or not changed)
            if text != before:
                previews.append((path, before, text))
    except _EditRefused as e:
        return _fail(result, 'refused', f'{fmt_id_display(ca)}/{fmt_id_display(cb)}: {e}')

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

    for path, _before, after in previews:
        try:
            path.write_text(after, encoding='utf-8')
        except OSError as e:
            return _fail(result, 'failed', f'cannot write {path}: {e}')
        result.note_changed(path)
        result.add('info', f'Wrote {relation} link in {path.name}', path=path)

    if relation == 'contradicts':
        q_path = _spawn_contradiction_question(archive_root, ca, cb)
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

def run_confirm_cooccur(
    archive_root: Path, *, person_a: str, person_b: str, source_id: str, subtype: str,
    accept: bool = False, reviewed: str | None = None, dry_run: bool = False,
) -> Result:
    """Mint a social `relationship` claim into the confirming source's record.

    `data` is {'status', 'claim_id', 'person_a', 'person_b', 'subtype',
    'source', 'claim_status'}. Minted `suggested` by default (the human's
    confirm proposes; accepting into a graph edge is the step-05 review). With
    `--accept` the claim is minted `accepted` and stamped `reviewed:` (today
    unless given), treating this confirm as the review — the only way it becomes
    a derived `relationships` edge on the next `fha index`.
    """
    result = Result(data={
        'status': None, 'claim_id': None, 'person_a': None, 'person_b': None,
        'subtype': subtype, 'source': None, 'claim_status': None,
    })

    for label, pid in (('first', person_a), ('second', person_b)):
        if not (is_valid_id(pid) and id_type_of(pid) == 'P'):
            return _fail(result, 'invalid-id',
                         f'The {label} argument {pid!r} is not a valid person ID. '
                         'P-ids look like P-de957bcda1.')
    pa, pb = normalize_id(person_a), normalize_id(person_b)
    if pa == pb:
        return _fail(result, 'same-person',
                     'A relationship needs two different people — pass two distinct P-ids.')
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
        before = source_path.read_text(encoding='utf-8')
    except OSError as e:
        return _fail(result, 'failed', f'cannot read {source_path}: {e}')
    after, changed = _append_claim_to_source(before, item_lines)
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
        source_path.write_text(after, encoding='utf-8')
    except OSError as e:
        return _fail(result, 'failed', f'cannot write {source_path}: {e}')

    result.data['status'] = 'ok'
    result.note_changed(source_path)
    result.add('info', f'Minted {summary}', path=source_path)
    if not accept:
        result.add('info',
                   'Minted as `suggested` — review it with '
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
            # A corrupt tombstone is disposable cache, not archive truth — start
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
    relink_previews: list[tuple[Path, str, str, str]] = []   # (path, cid, before, after)
    for cid, path in claim_paths.items():
        before = path.read_text(encoding='utf-8')
        after, changed = _set_scalar_on_claim(before, cid, 'place', place_disp)
        if not changed:
            return _notfound(result,
                             f'Found {fmt_id_display(cid)} but could not edit its claims block '
                             f'in {path}. Check the block by hand.')
        relink_previews.append((path, cid, before, after))

    if dry_run:
        result.data['status'] = 'ok'
        if into is None:
            result.add('info', f'[dry-run] Would register place {place_disp} ({name}) in places.yaml.')
            for ln in new_block_lines:
                result.add('info', '  ' + ln)
        else:
            result.add('info', f'[dry-run] Would relink claims to existing place {place_disp}.')
        for path, cid, before, after in relink_previews:
            result.add('info', f'[dry-run] Would set place: {place_disp} on {fmt_id_display(cid)} in {path.name}')
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    # 1. Registry write (new place only).
    if into is None:
        try:
            existing = places_yaml.read_text(encoding='utf-8') if places_yaml.exists() else ''
            sep = '' if (not existing or existing.endswith('\n')) else '\n'
            places_yaml.parent.mkdir(parents=True, exist_ok=True)
            places_yaml.write_text(existing + sep + '\n'.join(new_block_lines) + '\n', encoding='utf-8')
        except OSError as e:
            return _fail(result, 'failed', f'cannot write {places_yaml}: {e}')
        result.note_changed(places_yaml)
        result.add('info', f'Registered place {place_disp} ({name}) in {places_yaml.name}', path=places_yaml)

    # 2. Relink claims.
    for path, cid, _before, after in relink_previews:
        try:
            path.write_text(after, encoding='utf-8')
        except OSError as e:
            return _fail(result, 'failed', f'cannot write {path}: {e}')
        result.note_changed(path)
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
    """Build a minimal new place record for places.yaml (no coords — geocode later)."""
    lines = [
        f'- id: {fmt_id_display(place_id)}',
        f'  name: {name.strip()}',
    ]
    if hierarchy and hierarchy.strip():
        lines.append(f'  hierarchy: {hierarchy.strip()}')
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
    for ref in (refs or []):
        if not is_valid_id(ref):
            return _fail(result, 'invalid-id',
                         f'{ref!r} is not a valid archive ID. Refs are S-/P-/C-/L-/H- ids, '
                         'e.g. S-fa1234567b or P-de957bcda1.')
        ref_tokens.append(f'[{fmt_id_display(normalize_id(ref))}]')

    suffix = (' ' + ' '.join(ref_tokens)) if ref_tokens else ''
    entry = f'- {_today()}: {text}{suffix}'
    result.data['entry'] = entry

    path = archive_root / 'notes' / 'discoveries.md'

    if dry_run:
        result.data['status'] = 'ok'
        result.add('info', f'[dry-run] Would append to notes/discoveries.md:\n{entry}')
        result.add('info', '[dry-run] No file written.')
        return result

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding='utf-8')
    else:
        existing = '# Discoveries Log\n'
    sep = '' if existing.endswith('\n') else '\n'
    try:
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
    (AI provenance is never erased — AGENTS.md §20). The drafted prose itself is
    untouched; only the marker's state flips, so the human-readable body is
    unchanged apart from the comment.
    """
    result = Result(data={'status': None, 'person_id': None, 'profile': None, 'count': 0})

    if not (is_valid_id(person_id) and id_type_of(person_id) == 'P'):
        return _fail(result, 'invalid-id',
                     f'{person_id!r} is not a valid person ID. P-ids look like P-2b3c4d5e6f.')
    pid = normalize_id(person_id)
    result.data['person_id'] = fmt_id_display(pid)

    profile = _find_profile_path(archive_root, pid)
    if profile is None:
        return _notfound(result,
                         f'No curated profile found for {fmt_id_display(pid)} under '
                         f'{archive_root / "people"}.',
                         next_step='fha find ' + fmt_id_display(pid))
    result.data['profile'] = str(profile)

    try:
        before = profile.read_text(encoding='utf-8')
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
        profile.write_text(after, encoding='utf-8')
    except OSError as e:
        return _fail(result, 'failed', f'cannot write {profile}: {e}')

    result.data['status'] = 'ok'
    result.note_changed(profile)
    result.add('info', f'Accepted {count} AI-DRAFT marker(s) in {profile.name}.', path=profile)
    return result


# ── Result helpers ──────────────────────────────────────────────────────────────

def _fail(result: Result, status: str, message: str) -> Result:
    result.ok = False
    result.exit_code = EXIT_FAILURE
    result.data['status'] = status
    result.add('error', message)
    return result


def _notfound(result: Result, message: str, next_step: str | None = None) -> Result:
    result.ok = False
    result.exit_code = EXIT_WARNINGS
    result.data['status'] = 'not-found'
    result.add('warning', message, next_step=next_step)
    return result


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


def _add_subcommands(subs: argparse._SubParsersAction, *, suppress_root: bool) -> None:
    """Build the six `confirm` subparsers (shared by main fha + standalone)."""
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


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'confirm' onto the main fha parser."""
    p = subs.add_parser(
        'confirm',
        help='Write back a detection candidate the human picked (xref/cooccur/place/discovery/draft)',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = p.add_subparsers(dest='confirm_command', metavar='SUBCOMMAND')
    _add_subcommands(sub, suppress_root=True)
    p.set_defaults(func=lambda a: p.print_help() or EXIT_FAILURE)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha confirm',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest='confirm_command', metavar='SUBCOMMAND')
    _add_subcommands(sub, suppress_root=False)
    args = parser.parse_args(argv)
    if not getattr(args, 'func', None):
        parser.print_help()
        return EXIT_FAILURE
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
