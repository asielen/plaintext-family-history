#!/usr/bin/env python3
"""
normalize_links.py - fha normalize-links: settle citations to their canonical form.

  fha normalize-links            Preview the rewrites (dry run, writes nothing)
  fha normalize-links --dry-run  The same preview, named explicitly (the flag the
                                 operating instructions tell agents to always pass)
  fha normalize-links --write    Apply them, showing the same diff

`--dry-run --write` together is refused (exit 2): pick one - preview or write.

This is the ONE explicit, previewed rewrite pass over a human's citation prose
(SPEC §3 "resolve always; rewrite only on purpose"). It is deliberately separate
from the Formatter, which never rewrites prose beyond trailing whitespace
(TOOLING §3) - and citations are prose. Nothing here ever runs silently: the
default is a dry run, a real write needs `--write`, and a human stem is never
dropped (it stays in the record's `aliases:`, which is exactly what lets the
shortened link keep resolving).

Three rewrites, all toward the stable, rename-proof, ID-carrying form:
  (a) a legacy single-bracket `[S-…]` prose cite        → `[[S-…]]`
  (b) a resolved human stem/name in prose `[[grandmas-album]]` / `[[Ken Smith]]`
      → `[[S-…|grandmas-album]]` / `[[P-…|Ken Smith]]`  (ID load-bearing, the
      human's text preserved as display)
  (c) a resolved frontmatter name-link `people: ["[[Ken Smith]]"]`
      → `["[[P-…|Ken Smith]]"]`                          (the human graph surface,
      settled to a stable P-id target)

An AMBIGUOUS name (two "John Smith"s) is never guessed - it is reported and left
exactly as written for the human (or Obsidian autocomplete) to pin to an ID.

Two more settle the lists a reader scans to see who a record is about to
the written form SPEC §8.7 and §14 give them:
  (d) a source's frontmatter `people:` / `places:` - whatever form they were
      written in (a bare ID, `[[Ken Smith]]`)
      → one `- "[[p-…|Name]]"` line per entry
  (e) a claim's `persons:` → the same one-per-line `- "[[p-…|Name]]"` form
Nothing a person wrote is lost: a display they chose (`[[p-…|Maggie]]`,
`[[Maggie]]`) is kept, the record's name is added only where there is none,
and a trailing `# note` stays on its line. A record with no usable name is written as its bare ID, never
guessed; an entry that does not resolve is left as written. Nothing else in the claims
```yaml block is touched - `roles:` and `place:` stay bare IDs (SPEC §8.7).

Follows the headless-core Result contract: `run_normalize_links` computes and
returns a `Result`; `_cmd_normalize_links` renders it and returns the exit code.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FRONT_RE,
    LEGACY_TOKEN_RE,
    WIKILINK_RE,
    FhaConfigError,
    Result,
    alias_clashes,
    build_alias_map,
    fmt_id_display,
    format_yaml_dependency_error,
    yaml_link_item,
    split_yaml_comment,
    written_link_display,
    id_type_of,
    is_generated_file,
    is_placeholder_name,
    strip_link_wrapper,
    yaml_inline,
    is_template_file,
    link_entry,
    link_parts,
    load_fha_yaml,
    normalize_id,
    read_record,
    read_text_exact,
    resolve_ref,
    resolve_root_arg,
    undecodable_file_recorder,
    write_text_exact_atomic,
    safe_load_yaml,
)

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# Fenced code blocks (the ```yaml claims block, any ``` example) are structured
# data, not prose - split them out so a prose rewrite never edits a bare ID
# inside them.
import re

_FENCE_RE = re.compile(r'(```.*?```)', re.S)


# ── Record scan → alias map ───────────────────────────────────────────────────

def _scan_records(
    archive_root: Path,
    *,
    on_decode_error=None,
) -> list[dict]:
    """Collect the identity + alias surface of every record, for the resolve map
    and clash check: persons (id/name/variants/stems), sources (id/stems), and
    places (id/name/alt_names).

    A whole-archive scan, same shape as `stubs.py`'s unresolved-reference walk:
    a file saved in another encoding (cp1252, a Windows editor's default) must
    not blind the rest of the scan (#68). `on_decode_error`, when given, is
    handed to every `read_record` call here (people and sources both - the
    same `undecodable_file_recorder` callback for both loops, so one file
    touched by more than one pass earns one report, not two) and the file is
    skipped rather than crashed on; the caller (`run_normalize_links`) is the
    one with somewhere to put the aggregated report, so it owns the list this
    callback feeds. Omitted, the old crash-on-decode behaviour is unchanged -
    matching `read_record`'s own opt-in contract."""
    records: list[dict] = []

    people_root = archive_root / 'people'
    if people_root.is_dir():
        for path in people_root.rglob('*.md'):
            if is_template_file(path):
                continue
            rec = read_record(path, on_decode_error=on_decode_error)
            if rec['undecodable']:
                continue
            meta = rec['meta']
            rid = normalize_id(str(meta.get('id', '')))
            if rid.startswith('p-'):
                records.append({
                    'id': rid,
                    'name': meta.get('name'),
                    'name_variants': meta.get('name_variants') or [],
                    'aliases': meta.get('aliases') or [],
                    'status': meta.get('status'),
                })

    sources_root = archive_root / 'sources'
    if sources_root.is_dir():
        for path in sources_root.rglob('*.md'):
            if is_template_file(path):
                continue
            rec = read_record(path, on_decode_error=on_decode_error)
            if rec['undecodable']:
                continue
            meta = rec['meta']
            rid = normalize_id(str(meta.get('id', '')))
            if rid.startswith('s-'):
                records.append({'id': rid, 'aliases': meta.get('aliases') or []})

    places_path = archive_root / 'places' / 'places.yaml'
    if places_path.exists() and yaml is not None:
        try:
            places = safe_load_yaml(places_path.read_text(encoding='utf-8'))
        except Exception:
            places = None
        for place in places or []:
            if not isinstance(place, dict):
                continue
            rid = normalize_id(str(place.get('id', '')))
            if rid.startswith('l-'):
                records.append({
                    'id': rid,
                    'name': place.get('name'),
                    'alt_names': place.get('alt_names') or [],
                })

    return records


# ── Rewriting ─────────────────────────────────────────────────────────────────

def _rewrite_wikilinks(
    text: str,
    alias_map: dict[str, str],
    clashes: dict[str, list[str]],
) -> tuple[str, int, list[str]]:
    """Rewrite resolved name/stem `[[ ]]` links to the canonical `[[ID|display]]`
    form. ID-target links are already canonical and left alone, and so is a
    file-stem target (`[[hartley__thomas_edward_p-…|Tom]]`), which carries its
    ID at the end; an ambiguous name is collected (for the caller to report)
    and left exactly as written."""
    edits = 0
    ambiguous: list[str] = []

    def repl(m: re.Match) -> str:
        nonlocal edits
        target, _display = link_parts(m.group(1), m.group(3))
        if id_type_of(target):
            return m.group(0)   # already an ID or file-stem link: leave it
        resolved = resolve_ref(target, alias_map)
        if resolved:
            display = (m.group(3) or target).strip()
            edits += 1
            return f'[[{fmt_id_display(resolved)}|{display}]]'
        if target.lower() in clashes:
            ambiguous.append(target)
        return m.group(0)

    return WIKILINK_RE.sub(repl, text), edits, ambiguous


def _rewrite_prose(
    text: str,
    alias_map: dict[str, str],
    clashes: dict[str, list[str]],
) -> tuple[str, int, list[str]]:
    """Prose rewrites: resolved name/stem links, then legacy single-bracket IDs."""
    text, edits, ambiguous = _rewrite_wikilinks(text, alias_map, clashes)

    def upgrade_legacy(m: re.Match) -> str:
        return f'[[{fmt_id_display(normalize_id(m.group(1)))}]]'

    new_text, n = LEGACY_TOKEN_RE.subn(upgrade_legacy, text)
    edits += n
    return new_text, edits, ambiguous


# Matches a `people:` or `places:` YAML key plus all its continuation lines
# (both flow-style `people: ["[[X]]"]` and block-style `  - "[[X]]"` items).
# Only these two fields carry name-links that should be normalised; text fields
# like citation:, provenance:, title: must never be rewritten.
_FM_PERSON_PLACE_RE = re.compile(
    r'^((?:people|places):[^\n]*(?:\n[ \t]+[^\n]*)*)',
    re.M,
)


def _rewrite_frontmatter(
    fm_text: str,
    alias_map: dict[str, str],
    clashes: dict[str, list[str]],
) -> tuple[str, int, list[str]]:
    """Rewrite wikilinks inside `people:` and `places:` frontmatter blocks only.
    All other fields (citation:, title:, aliases:, provenance: …) are untouched."""
    total_edits = 0
    all_ambiguous: list[str] = []

    def repl(m: re.Match) -> str:
        nonlocal total_edits
        new_block, e, a = _rewrite_wikilinks(m.group(1), alias_map, clashes)
        total_edits += e
        all_ambiguous.extend(a)
        return new_block

    new_fm = _FM_PERSON_PLACE_RE.sub(repl, fm_text)
    return new_fm, total_edits, all_ambiguous


# ── Settling people:/places:/persons: to "[[id|Name]]" ───────────────────────

_LINK_LIST_WANT = {'people': 'P', 'places': 'L', 'persons': 'P'}
_ITEM_LINE_RE = re.compile(r'^(\s*)-\s+(.*)$')
_UNPARSED = object()


def _yaml_value(text: str):
    """`text` parsed as YAML, or `_UNPARSED` when it does not parse."""
    try:
        return safe_load_yaml(text)
    except Exception:
        return _UNPARSED


_split_comment = split_yaml_comment


def _resolve_list_item(item, want: str, alias_map: dict[str, str]) -> str | None:
    """The lowercase ID one people:/places:/persons: entry names, or None.
    An ID is kept as written (never redirected to a merge survivor - that is
    `fha confirm merge`'s job); a name resolves through the alias map."""
    if isinstance(item, list):           # an unquoted [[X]] reads as a nested list
        item = '[[' + ' '.join(str(x) for x in item) + ']]'
    if item is None or isinstance(item, dict):
        return None
    target = strip_link_wrapper(str(item))
    if not target:
        return None
    if id_type_of(target):
        rid = normalize_id(target)
    else:
        rid = resolve_ref(target, alias_map)
    if rid and id_type_of(rid) == want:
        return normalize_id(rid)
    return None


def _written_stem(item) -> str | None:
    """The file stem a `[[stem|…]]` list entry targets, as written, or None
    when the entry targets an ID or a name."""
    if not isinstance(item, str):
        return None
    m = WIKILINK_RE.fullmatch(item.strip())
    if not m:
        return None
    written = m.group(1).strip()
    target, _display = link_parts(written)
    return written if target != written and id_type_of(target) else None


def _settle_list_segment(
    seg: list[str], key: str, col: int, head_prefix: str,
    alias_map: dict[str, str], names: dict[str, str],
) -> list[str] | None:
    """Rewrite one `key:` segment (its key line plus continuation lines) to
    the one-per-line `- "[[id|Name]]"` form. Returns None to leave it alone:
    it does not parse, is empty, or holds a comment this rewrite could not
    keep in its place."""
    want = _LINK_LIST_WANT[key]
    head = seg[0]
    after = head[head.index(':', len(head_prefix)) + 1:]
    inline, head_comment = _split_comment(after.strip()) if after.strip() else ('', '')
    if any(ln.strip().startswith('#') for ln in seg[1:]):
        return None
    body = [ln[col:] if ln[:col].strip() == '' else ln for ln in seg[1:]]
    parsed = _yaml_value('\n'.join([f'{key}: {inline}'] + body))
    if parsed is _UNPARSED or not isinstance(parsed, dict):
        return None
    value = parsed.get(key)
    if value in (None, [], ''):
        return None
    items = value if isinstance(value, list) else [value]
    comments: list[str] = [''] * len(items)
    if not inline:
        item_lines = [ln for ln in seg[1:] if ln.strip()]
        if len(item_lines) == len(items) and all(_ITEM_LINE_RE.match(ln) for ln in item_lines):
            comments = [_split_comment(_ITEM_LINE_RE.match(ln).group(2))[1] for ln in item_lines]
        elif any('#' in ln for ln in item_lines):
            return None                  # cannot place its comments: leave it
    elif '#' in inline and not isinstance(value, list):
        return None
    pad = ' ' * col
    out = [f'{head_prefix}{key}:' + (f'  {head_comment}' if head_comment else '')]
    for item, comment in zip(items, comments):
        rid = _resolve_list_item(item, want, alias_map)
        if rid:
            # A display a person wrote (`[[p-…|Maggie]]`, `[[Maggie]]`) is
            # kept; the record's name is added only where there is none.
            written = written_link_display(item)
            name = written or names.get(rid)
            stem = _written_stem(item)
            if stem:
                # A file-stem link is already settled: keep the stem the
                # person (or Obsidian) wrote, adding only a missing name.
                entry = link_entry(rid, name)
                shown = entry[2:-2].split('|', 1)[1] if entry.startswith('[[') else ''
                rendered = json.dumps(f'[[{stem}|{shown}]]' if shown else f'[[{stem}]]',
                                      ensure_ascii=False)
            else:
                rendered = yaml_link_item(rid, name)
        elif isinstance(item, list):
            rendered = json.dumps('[[' + ' '.join(str(x) for x in item) + ']]', ensure_ascii=False)
        elif isinstance(item, str):
            rendered = json.dumps(item, ensure_ascii=False) if item.startswith('[') else yaml_inline(item)
        else:
            return None
        out.append(f'{pad}  - {rendered}' + (f'  {comment}' if comment else ''))
    return None if out == seg else out


def _segment_end(lines: list[str], start: int, col: int) -> int:
    """Exclusive end of the value lines owned by the key at `lines[start]`
    (key column `col`): deeper-indented lines, `- ` items at the key's own
    column, and blank lines between them."""
    j = start + 1
    end = j
    while j < len(lines):
        ln = lines[j]
        if not ln.strip():
            j += 1
            continue
        ind = len(ln) - len(ln.lstrip(' '))
        if ind > col or (ind == col and ln.lstrip(' ').startswith('- ')):
            j += 1
            end = j
            continue
        break
    return end


def _settle_frontmatter_lists(fm_lines: list[str], alias_map, names) -> tuple[list[str], int]:
    """people:/places: in one frontmatter (its lines, fences excluded)."""
    out = list(fm_lines)
    edits = 0
    i = 0
    while i < len(out):
        m = re.match(r'^(people|places)\s*:', out[i])
        if not m:
            i += 1
            continue
        end = _segment_end(out, i, 0)
        new = _settle_list_segment(out[i:end], m.group(1), 0, '', alias_map, names)
        if new is not None:
            out[i:end] = new
            edits += 1
            end = i + len(new)
        i = end
    return out, edits


_PERSONS_KEY_RE = re.compile(r'^(\s*(?:-\s+)?)persons\s*:')


def _settle_claims_block(block: list[str], alias_map, names) -> tuple[list[str], int]:
    """persons: in one fenced claims block. The rewrite is kept only when the
    block still reads as the same claims naming the same people, so a
    `persons:` line quoted inside a `notes:` block scalar is never edited."""
    out = list(block)
    edits = 0
    i = 0
    while i < len(out):
        m = _PERSONS_KEY_RE.match(out[i])
        if not m:
            i += 1
            continue
        col = len(m.group(1))
        end = _segment_end(out, i, col)
        new = _settle_list_segment(out[i:end], 'persons', col, m.group(1), alias_map, names)
        if new is not None:
            out[i:end] = new
            edits += 1
            end = i + len(new)
        i = end
    if not edits:
        return block, 0
    before = _yaml_value('\n'.join(block))
    after = _yaml_value('\n'.join(out))
    if not (isinstance(before, list) and isinstance(after, list) and len(before) == len(after)):
        return block, 0

    def people(claim: dict) -> list:
        raw = claim.get('persons')
        return [_resolve_list_item(x, 'P', alias_map) or x
                for x in (raw if isinstance(raw, list) else [raw])]

    for a, b in zip(before, after):
        if not (isinstance(a, dict) and isinstance(b, dict)) or set(a) != set(b):
            return block, 0
        if any(a[k] != b[k] for k in a if k != 'persons') or people(a) != people(b):
            return block, 0
    return out, edits


def settle_link_lists(text: str, alias_map: dict[str, str],
                      names: dict[str, str]) -> tuple[str, int]:
    """Settle a record's people:/places: and its claims' persons: (rewrites
    (d) and (e) of the module docstring). The record's line endings are kept."""
    crlf = '\r\n' in text
    lines = text.replace('\r\n', '\n').split('\n')
    edits = 0
    body_start = 0
    if lines and lines[0].lstrip('﻿') == '---':
        close = next((k for k in range(1, len(lines)) if lines[k] == '---'), None)
        if close is not None:
            new_fm, e = _settle_frontmatter_lists(lines[1:close], alias_map, names)
            if e:
                lines[1:close] = new_fm
                edits += e
                close = 1 + len(new_fm)
            body_start = close + 1
    i = body_start
    while i < len(lines):
        if re.match(r'^\s*```\s*ya?ml\s*$', lines[i]):
            j = next((k for k in range(i + 1, len(lines))
                      if re.match(r'^\s*```\s*$', lines[k])), None)
            if j is None:
                break
            new_block, e = _settle_claims_block(lines[i + 1:j], alias_map, names)
            if e:
                lines[i + 1:j] = new_block
                edits += e
                j = i + 1 + len(new_block)
            i = j + 1
            continue
        i += 1
    if not edits:
        return text, 0
    out = '\n'.join(lines)
    return (out.replace('\n', '\r\n') if crlf else out), edits


def normalize_text(
    text: str,
    alias_map: dict[str, str],
    clashes: dict[str, list[str]],
    names: dict[str, str] | None = None,
) -> tuple[str, int, list[str]]:
    """Normalize one record's text, region by region:

      - frontmatter: only the `people:` and `places:` list blocks (so text fields
        like citation:, provenance:, title:, aliases: are never touched, and
        bare-ID `people: [P-…]` lists without wikilinks are left alone);
      - body prose: name-link upgrade + legacy single-bracket upgrade, but NOT
        inside ```fenced``` blocks - the claims YAML stays bare.
    """
    settled = 0
    if names is not None:
        text, settled = settle_link_lists(text, alias_map, names)
    fm = FRONT_RE.match(text)
    if fm:
        fm_text, body = text[:fm.end()], text[fm.end():]
        new_fm, e_fm, a_fm = _rewrite_frontmatter(fm_text, alias_map, clashes)
    else:
        new_fm, body, e_fm, a_fm = '', text, 0, []

    out: list[str] = []
    edits = e_fm + settled
    ambiguous = list(a_fm)
    for i, part in enumerate(_FENCE_RE.split(body)):
        if i % 2 == 1:          # odd parts are fenced blocks - structured data
            out.append(part)
            continue
        new_part, e, a = _rewrite_prose(part, alias_map, clashes)
        out.append(new_part)
        edits += e
        ambiguous += a

    return new_fm + ''.join(out), edits, ambiguous


def _record_files(archive_root: Path):
    """Yield every prose-bearing record file (people/, sources/, notes/),
    skipping `_TEMPLATE.*` teaching templates (not records) and GENERATED
    companion files (those must only be regenerated by tools, AGENTS.md §4).

    GENERATED ownership is judged by `_lib.is_generated_file` - the first
    NON-BLANK line, BOM tolerated - the same rule lint and views apply. The
    old byte-0 check here rewrote prose inside any generated file that merely
    began with a blank line (round-2 finding 12)."""
    for sub in ('people', 'sources', 'notes'):
        base = archive_root / sub
        if base.is_dir():
            for path in sorted(base.rglob('*.md')):
                if is_template_file(path):
                    continue
                if is_generated_file(path):
                    continue
                yield path


# ── Run / compute ─────────────────────────────────────────────────────────────

def run_normalize_links(
    archive_root: Path,
    fha_config: dict,
    *,
    write: bool = False,
) -> Result:
    """Compute (and, with write=True, apply) the citation normalization, returning
    a Result. `data` carries `files_changed`, `edits`, the per-file unified
    `diffs`, and the `ambiguous` names that were left for a human to pin."""
    undecodable: list[Path] = []
    on_decode_error = undecodable_file_recorder(undecodable)
    records = _scan_records(archive_root, on_decode_error=on_decode_error)
    alias_map = build_alias_map(records)
    clashes = alias_clashes(records)
    # The display added to a people:/places:/persons: entry that has none:
    # the record's own name (a display a person wrote is always kept). A
    # record with no usable name is written as its bare ID.
    names = {r['id']: ' '.join(str(r['name']).split()) for r in records
             if r.get('name') and not is_placeholder_name(r['name'])
             and r['id'][:2] in ('p-', 'l-')}

    result = Result()
    result.data['diffs'] = {}
    files_changed = 0
    total_edits = 0
    ambiguous_seen: set[str] = set()

    for path in _record_files(archive_root):
        try:
            # Byte-faithful read: the default translates CRLF to LF, and the
            # matching default write would then re-translate on the way out, so
            # tidying one citation in a CRLF-authored record rewrote every line
            # ending in it. This verb touches every record in the archive at
            # once, so that churn is archive-wide.
            original = read_text_exact(path)
        except OSError:
            continue
        except UnicodeDecodeError:
            # A second #68 site beyond the two `_scan_records` was built for
            # (found while testing this fix, not in the original two-site
            # list): `_record_files` walks people/, sources/ AND notes/ for
            # the actual rewrite, wider than `_scan_records`'s alias-map scan
            # (people/sources only). `read_text_exact` has no
            # `on_decode_error` seam (it is byte-preserving, not the
            # frontmatter parser), so the decode is caught here directly and
            # fed the SAME recorder `_scan_records` used above - a file that
            # both scans touch (any people/sources record) earns one report,
            # not two, matching the shared-recorder contract `undecodable_
            # file_recorder` documents.
            on_decode_error(path)
            continue
        new_text, edits, ambiguous = normalize_text(original, alias_map, clashes, names)
        rel = str(path.relative_to(archive_root)).replace('\\', '/')

        for name in ambiguous:
            if name.lower() not in ambiguous_seen:
                ambiguous_seen.add(name.lower())
                ids = ', '.join(fmt_id_display(i) for i in clashes.get(name.lower(), []))
                result.add('warning', f"{rel}: '{name}' is ambiguous - it names {ids}. "
                           'Left unchanged; pin it to one ID by hand.')

        if new_text == original:
            continue
        files_changed += 1
        total_edits += edits
        diff = ''.join(difflib.unified_diff(
            original.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=rel, tofile=rel,
        ))
        result.data['diffs'][rel] = diff
        result.add('info', f'{rel}: {edits} citation(s) to normalize', path=path)
        if write:
            # Atomic: --write walks every record in the archive, so a failure
            # midway (disk full on a large tree) would otherwise truncate
            # whichever record it happened to be holding open. No
            # `reapply_newline` needed - `normalize_text` is regex substitution
            # over the untranslated text, so `new_text` already carries the
            # record's own line endings verbatim.
            write_text_exact_atomic(path, new_text)
            result.note_changed(path)

    result.data['files_changed'] = files_changed
    result.data['edits'] = total_edits
    result.data['written'] = write

    if files_changed == 0:
        result.add('info', 'All citations are already in canonical form - nothing to normalize.')
    elif write:
        result.add('info', f'Normalized {total_edits} citation(s) across {files_changed} file(s).')
    else:
        result.add('info',
                   f'{total_edits} citation(s) across {files_changed} file(s) can be normalized. '
                   'Re-run with --write to apply.',
                   next_step='fha normalize-links --write')

    if undecodable:
        shown = ', '.join(
            str(p.relative_to(archive_root)).replace('\\', '/') for p in undecodable[:5])
        if len(undecodable) > 5:
            shown += f' and {len(undecodable) - 5} more'
        result.add(
            'warning',
            f'{len(undecodable)} file(s) are not saved as UTF-8 text, so they were '
            f'skipped rather than crashing this run: {shown}. A person or source '
            "record's aliases in that file will not be considered for normalization, "
            'so a citation that should resolve through it may be reported as '
            'unrecognized/ambiguous instead - and if the file itself has citations to '
            'normalize, they are left exactly as written this run, same as any other '
            'skipped file. The file itself is fine and nothing was changed - it is '
            'only saved in an older encoding (a Windows editor defaults to one, '
            'commonly cp1252). Open it and save it again choosing UTF-8 (in Notepad: '
            'Save As, then pick UTF-8 from the Encoding menu), '
            'then run `fha normalize-links` again.')

    if ambiguous_seen or undecodable:
        result.ok = False
        result.exit_code = EXIT_WARNINGS
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _emit(result: Result, show_diff: bool) -> int:
    if show_diff:
        for rel, diff in result.data.get('diffs', {}).items():
            if diff:
                sys.stdout.write(diff)
                if not diff.endswith('\n'):
                    sys.stdout.write('\n')
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
        if msg.next_step:
            print(f'  next: {msg.next_step}', file=stream)
    return result.exit_code


def _cmd_normalize_links(args: argparse.Namespace) -> int:
    # `--dry-run` names the default preview explicitly (AGENTS.md tells agents
    # to always pass it before any mutating operation, so it must parse here);
    # combined with `--write` the request is contradictory - refuse plainly.
    if getattr(args, 'dry_run', False) and getattr(args, 'write', False):
        print('ERROR: --dry-run and --write cannot be combined - pick one: '
              '--dry-run previews the changes (the default), --write applies them.',
              file=sys.stderr)
        return EXIT_ERRORS
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    result = run_normalize_links(archive_root, fha_config, write=bool(getattr(args, 'write', False)))
    return _emit(result, show_diff=not bool(getattr(args, 'quiet', False)))


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Tidy the citations and cross-links in your prose to the archive's [[ ]] form,
and write each source's people:/places: and each claim's persons: one per line
with the name beside the ID, so a record says who it is about.

  fha normalize-links            Preview the rewrites (dry run, writes nothing)
  fha normalize-links --write    Apply them, showing the same diff

Runs as a preview by default; a real write needs --write. Your original names
are kept as aliases, so the shortened links still resolve."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'normalize-links',
        help='Settle prose citations to the canonical [[ID]] / [[ID|name]] form',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--write', action='store_true',
                   help='Apply the rewrites (default is a dry-run preview)')
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview without writing (already the default; accepted '
                        'so the always-preview habit works here too)')
    p.add_argument('--quiet', action='store_true',
                   help='Suppress the per-file diff (show only the summary)')
    p.set_defaults(func=_cmd_normalize_links)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha normalize-links',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--dry-run', action='store_true', dest='dry_run')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    return _cmd_normalize_links(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
