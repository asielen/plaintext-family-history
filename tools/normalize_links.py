#!/usr/bin/env python3
"""
normalize_links.py — fha normalize-links: settle citations to their canonical form.

  fha normalize-links            Preview the rewrites (dry run — writes nothing)
  fha normalize-links --write    Apply them, showing the same diff

This is the ONE explicit, previewed rewrite pass over a human's citation prose
(SPEC §3 "resolve always; rewrite only on purpose"). It is deliberately separate
from the Formatter, which never rewrites prose beyond trailing whitespace
(TOOLING §3) — and citations are prose. Nothing here ever runs silently: the
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

An AMBIGUOUS name (two "John Smith"s) is never guessed — it is reported and left
exactly as written for the human (or Obsidian autocomplete) to pin to an ID.

The claims ```yaml block and bare-ID frontmatter lists are NEVER touched: those
are structured data, not prose (SPEC §8/§14 — "the claims block stays bare").

Follows the headless-core Result contract: `run_normalize_links` computes and
returns a `Result`; `_cmd_normalize_links` renders it and returns the exit code.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
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
    id_type_of,
    is_template_file,
    load_fha_yaml,
    normalize_id,
    read_record,
    resolve_ref,
    resolve_root_arg,
)

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# Fenced code blocks (the ```yaml claims block, any ``` example) are structured
# data, not prose — split them out so a prose rewrite never edits a bare ID
# inside them.
import re

_FENCE_RE = re.compile(r'(```.*?```)', re.S)


# ── Record scan → alias map ───────────────────────────────────────────────────

def _scan_records(archive_root: Path) -> list[dict]:
    """Collect the identity + alias surface of every record, for the resolve map
    and clash check: persons (id/name/variants/stems), sources (id/stems), and
    places (id/name/alt_names)."""
    records: list[dict] = []

    people_root = archive_root / 'people'
    if people_root.is_dir():
        for path in people_root.rglob('*.md'):
            if is_template_file(path):
                continue
            meta = read_record(path)['meta']
            rid = normalize_id(str(meta.get('id', '')))
            if rid.startswith('p-'):
                records.append({
                    'id': rid,
                    'name': meta.get('name'),
                    'name_variants': meta.get('name_variants') or [],
                    'aliases': meta.get('aliases') or [],
                })

    sources_root = archive_root / 'sources'
    if sources_root.is_dir():
        for path in sources_root.rglob('*.md'):
            if is_template_file(path):
                continue
            meta = read_record(path)['meta']
            rid = normalize_id(str(meta.get('id', '')))
            if rid.startswith('s-'):
                records.append({'id': rid, 'aliases': meta.get('aliases') or []})

    places_path = archive_root / 'places' / 'places.yaml'
    if places_path.exists() and yaml is not None:
        try:
            places = yaml.safe_load(places_path.read_text(encoding='utf-8'))
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
    form. ID-target links are already canonical and left alone; an ambiguous name
    is collected (for the caller to report) and left exactly as written."""
    edits = 0
    ambiguous: list[str] = []

    def repl(m: re.Match) -> str:
        nonlocal edits
        target = m.group(1).strip()
        if id_type_of(target):
            return m.group(0)   # already an ID link — canonical, leave it
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


def normalize_text(
    text: str,
    alias_map: dict[str, str],
    clashes: dict[str, list[str]],
) -> tuple[str, int, list[str]]:
    """Normalize one record's text, region by region:

      - frontmatter: only the name-link upgrade (so a bare-ID `people: [P-…]`
        list is never wrapped, and an `aliases:`/`title:` value is never touched);
      - body prose: name-link upgrade + legacy single-bracket upgrade, but NOT
        inside ```fenced``` blocks — the claims YAML stays bare.
    """
    fm = FRONT_RE.match(text)
    if fm:
        fm_text, body = text[:fm.end()], text[fm.end():]
        new_fm, e_fm, a_fm = _rewrite_wikilinks(fm_text, alias_map, clashes)
    else:
        new_fm, body, e_fm, a_fm = '', text, 0, []

    out: list[str] = []
    edits = e_fm
    ambiguous = list(a_fm)
    for i, part in enumerate(_FENCE_RE.split(body)):
        if i % 2 == 1:          # odd parts are fenced blocks — structured data
            out.append(part)
            continue
        new_part, e, a = _rewrite_prose(part, alias_map, clashes)
        out.append(new_part)
        edits += e
        ambiguous += a

    return new_fm + ''.join(out), edits, ambiguous


def _record_files(archive_root: Path):
    """Yield every prose-bearing record file (people/, sources/, notes/),
    skipping `_TEMPLATE.*` teaching templates (not records)."""
    for sub in ('people', 'sources', 'notes'):
        base = archive_root / sub
        if base.is_dir():
            for path in sorted(base.rglob('*.md')):
                if not is_template_file(path):
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
    records = _scan_records(archive_root)
    alias_map = build_alias_map(records)
    clashes = alias_clashes(records)

    result = Result()
    result.data['diffs'] = {}
    files_changed = 0
    total_edits = 0
    ambiguous_seen: set[str] = set()

    for path in _record_files(archive_root):
        try:
            original = path.read_text(encoding='utf-8')
        except OSError:
            continue
        new_text, edits, ambiguous = normalize_text(original, alias_map, clashes)
        rel = str(path.relative_to(archive_root)).replace('\\', '/')

        for name in ambiguous:
            if name.lower() not in ambiguous_seen:
                ambiguous_seen.add(name.lower())
                ids = ', '.join(fmt_id_display(i) for i in clashes.get(name.lower(), []))
                result.add('warning', f"{rel}: '{name}' is ambiguous — it names {ids}. "
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
            path.write_text(new_text, encoding='utf-8')
            result.note_changed(path)

    result.data['files_changed'] = files_changed
    result.data['edits'] = total_edits
    result.data['written'] = write

    if files_changed == 0:
        result.add('info', 'All citations are already in canonical form — nothing to normalize.')
    elif write:
        result.add('info', f'Normalized {total_edits} citation(s) across {files_changed} file(s).')
    else:
        result.add('info',
                   f'{total_edits} citation(s) across {files_changed} file(s) can be normalized. '
                   'Re-run with --write to apply.',
                   next_step='fha normalize-links --write')

    if ambiguous_seen:
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


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'normalize-links',
        help='Settle prose citations to the canonical [[ID]] / [[ID|name]] form',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--write', action='store_true',
                   help='Apply the rewrites (default is a dry-run preview)')
    p.add_argument('--quiet', action='store_true',
                   help='Suppress the per-file diff (show only the summary)')
    p.set_defaults(func=_cmd_normalize_links)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha normalize-links',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH')
    parser.add_argument('--write', action='store_true')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)
    return _cmd_normalize_links(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
