#!/usr/bin/env python3
"""
process.py - fha process: Stage A of the intake pipeline.

  fha process <file> [--type TYPE] [--title "…"] [--date DATE] [--slug SLUG]
                                                                 Process one asset
  fha process <photo> --more <file> ROLE[:copy]                  Attach a file to its source
  fha process <file> --dry-run                                   Preview, write nothing

This is the *deterministic* stage of processing an original into a Source
(SPEC §12.1, TOOLING §6): it mints an S-id, marks the file's identity, and
scaffolds the §14 source record with an empty `## Claims` block. The AI draft
pass (read the file, resolve names/places, draft `suggested` claims) and the
human review pass are Stages B and C - the `process-source` and `review-claims`
skills - not this tool.

Two roots, two identity rules (the spine of SPEC §12.1):

  * Documents root - the file is RENAMED in place to `{slug}_{S-id}.{ext}`;
    its prior name is preserved as `original_filename` provenance. Filename
    only; never content, never location.
  * Photos root - files are NEVER renamed (a rename breaks the Lightroom
    catalog). Identity travels in the embedded `SOURCE: S-xxxx` keyword
    (written via exiftool) plus the record's `files:` inventory.

Detection is by extension and by the photos-root mapping in `fha.yaml`: a file
with a photo extension, or any file living under the resolved photos root, is a
photo; everything else is a document.

Every mutating path is transactional: each filesystem effect registers an undo,
and any failure unwinds them in reverse so an interrupted run leaves no partial
state (AGENTS.md contract). `--dry-run` performs no effect at all - which means
an inbox relocation is then only *virtual* (the previewed destination does not
exist yet), so every preview read (embedded keywords, the sidecar and its
hints, variation grouping) is threaded back to the file's real pre-move
location via the `real_path`/`real_paths` parameters. Without that, the
preview would describe a different plan than the live run executes.

Passing a *directory* selects one of two folder modes:

  * Bundle folder (M7.4) - a folder containing a bare `notes.md`. It is a
    source-stub *bundle* (SPEC §12.1): one S-id covers every asset inside, each
    filed to its proper root (documents renamed, photos moved but never
    renamed), one record scaffolded from the notes, and the now-empty folder
    deleted. The whole bundle becomes one source.
  * Triage folder (M7.3) - any other folder (typically a `photos/` subfolder).
    Its unprocessed photos are grouped into variation sets, ranked by the same
    evidence signals `fha photoindex triage` uses, and offered for selection;
    the chosen sets are processed one by one.

**Tier-1 variation detection** also runs when a single photo is processed: its
directory is scanned for siblings sharing a filename base_id (front/back, copy
letters, crops, negatives, booklet pages - the TOOLING §6 grammar), and if any
are found the user is asked whether they are *one* source (shared S-id) or
*separate* ones. The grouping grammar is shared with `fha photoindex` through
`_lib` so both tools agree on what counts as a variation set.
"""

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Slug / type derivation
#    _slugify                  - text → lowercase-hyphenated slug
#    _derive_slug              - pick the slug from --slug / --title / filename stem
#
#  Asset classification
#    _is_under                 - is a path inside a (resolved) root directory?
#    classify_asset            - 'photo' | 'document' for a file + fha.yaml
#    _filename_has_source_id   - does a filename already carry _{S-id}? (refuse)
#
#  exiftool seams (monkeypatched in tests - process never imports photoindex)
#    _run_exiftool_read_keywords - read embedded Keywords/Subject of one file
#    _run_exiftool_embed_source  - write `SOURCE: {S-id}` into one file
#    _run_exiftool_remove_source - remove a just-written `SOURCE: {S-id}`
#    _read_source_keyword        - the S-id embedded in a photo, or None
#
#  Record scaffolding
#    _scaffold_text            - the §14 source-record template as text
#    _render_scaffold_file_entry - one files: list item (file/role/copy/…) as lines
#    _find_record_for_sid      - locate sources/**/*_{S-id}.md
#    _append_file_entry        - surgically add a files: list item to a record
#
#  Source-stub sidecar (*.notes.md) + bundle notes.md
#    _find_sidecar             - the {stem}.notes.md beside an asset, if any
#    _companion_for_sidecar    - resolve direct sidecar input to its asset
#    _read_sidecar             - its hint frontmatter + prose body
#    _bundle_file_hints        - bundle notes per-file role/copy/primary hints
#
#  Variation detection (M7.3, shared grammar via _lib)
#    _photo_variation_siblings - photos in a dir sharing one base_id
#    _variation_role_copy      - (role, copy) annotation for a grouped member
#    _batch_type               - A–D label for a multi-image set (informational)
#    _run_exiftool_read_meta   - caption/date/keyword signals for triage scoring
#    _score_photo_group        - TOOLING §15b evidence score (mirrors photoindex)
#
#  Top-level operations
#    process_document          - M7.1: rename + scaffold (transactional)
#    process_photo             - M7.2: keyword + scaffold (transactional)
#    process_photo_group       - M7.3: one S-id over a variation set (transactional)
#    process_folder            - M7.3: triage a folder, process selected groups
#    process_bundle            - M7.4: dissolve a notes.md bundle into one source
#    attach_more               - M7.2: attach a file to an existing source
#
#  CLI
#    _prompt                   - interactive input seam (monkeypatched in tests)
#    _resolve_input_file       - forgiving FILE/--more lookup: as typed, then under the archive root
#    register / _run_process / _standalone_main
#
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    Result,
    PHOTO_EXTENSIONS,
    SOURCE_TYPES,
    FhaConfigError,
    ParsedName,
    configure_utf8_stdout,
    format_edtf_error,
    format_exiftool_error,
    format_source_type_error,
    fmt_id_display,
    grouping_stem,
    id_type_of,
    is_valid_edtf,
    is_valid_id,
    load_fha_yaml,
    mint_ids,
    normalize_date,
    normalize_id,
    parse_media_filename,
    path_to_alias,
    read_record,
    resolve_path,
    resolve_root_arg,
    scan_ids_in_tree,
    scan_person_record_ids,
    select_variation_primary,
    is_working_copy,
    variant_role,
    yaml_inline,
)

configure_utf8_stdout()

# Default source_type for a document when --type is not given. 'other' is in the
# controlled vocabulary (SPEC §14), so the scaffold lints clean; the human (or
# the AI draft pass) refines it during review.
_DEFAULT_DOCUMENT_TYPE = 'other'

# Photos always scaffold to sources/photos/ with source_type 'photo' regardless
# of any --type - the directory is plural by SPEC convention, the type singular.
_PHOTO_DIR = 'photos'
_PHOTO_SOURCE_TYPE = 'photo'

# A filename already carrying an S-id (e.g. a re-run of a processed document).
_FILENAME_SOURCE_ID_RE = re.compile(r'_(S-[0-9a-hjkmnp-tv-z]{10})$', re.I)

# An embedded `SOURCE: S-xxxx` keyword (the photo identity carrier).
_SOURCE_KEYWORD_RE = re.compile(r'^SOURCE:\s*(S-[0-9a-hjkmnp-tv-z]{10})$', re.I)


def _record_subdir(source_type: str) -> str:
    """Map a source_type to its on-disk subdirectory name.

    Two cases differ from the literal type (SPEC §14): the singular `photo`
    type files under the plural `photos/` directory, and `proof-argument`
    authored conclusions file under `proofs/`. Shared by every scaffold path so
    a photo record always lands in `sources/photos/`, never `sources/photo/`.
    """
    if source_type == _PHOTO_SOURCE_TYPE:
        return _PHOTO_DIR
    if source_type == 'proof-argument':
        return 'proofs'
    return source_type


def _today() -> str:
    return datetime.date.today().isoformat()


# ── Slug / type derivation ────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Collapse arbitrary text to a lowercase-hyphenated slug (SPEC §13).

    Slugs are mutable and human-facing; only the trailing `_{S-id}` carries
    machine meaning. We keep ASCII letters and digits, turn every other run of
    characters into a single hyphen, and trim hyphens off the ends. An empty
    result (e.g. a filename of only punctuation) falls back to 'source' so a
    record is always nameable.
    """
    text = (text or '').strip().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return slug or 'source'


def _derive_slug(slug: str | None, title: str | None, file_path: Path) -> str:
    """Choose the record slug: explicit --slug, else --title, else the filename stem.

    The filename stem is the common case for hand-filed assets ('1880-census.pdf'
    → '1880-census'); --title gives a readable slug when the filename is opaque
    ('scan0007.jpg' with --title "Wedding portrait" → 'wedding-portrait').
    """
    if slug:
        return _slugify(slug)
    if title:
        return _slugify(title)
    return _slugify(file_path.stem)


# ── Asset classification ──────────────────────────────────────────────────────

def _is_under(path: Path, root: Path) -> bool:
    """True if `path` is inside `root` (both resolved); False on unrelated trees."""
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def classify_asset(file_path: Path, fha_config: dict, archive_root: Path) -> str:
    """Return 'photo' or 'document' for an asset file (TOOLING §6).

    The documents root takes precedence: a file filed there is a document even
    if it has a photo extension (a scanned record saved as `.jpg`) - the
    documents-root identity rule (rename + provenance) applies to whatever was
    deliberately filed there. Otherwise a file is a photo if it has a known
    photo extension OR lives under the resolved photos root - the latter
    catches odd-extension scans filed in the photo library. Everything else is
    a document.
    """
    documents_root = resolve_path('documents', fha_config, archive_root)
    if _is_under(file_path, documents_root):
        return 'document'
    if file_path.suffix.lower() in PHOTO_EXTENSIONS:
        return 'photo'
    photos_root = resolve_path(_PHOTO_DIR, fha_config, archive_root)
    if _is_under(file_path, photos_root):
        return 'photo'
    return 'document'


def _filename_has_source_id(file_path: Path) -> str | None:
    """Return the S-id already embedded in the filename, or None.

    A processed documents-root file carries `_{S-id}` in its name; re-processing
    it would mint a second ID for the same source, so the caller refuses.
    """
    m = _FILENAME_SOURCE_ID_RE.search(file_path.stem)
    return m.group(1).lower() if m else None


# ── exiftool seams ────────────────────────────────────────────────────────────
#
# process.py keeps its own thin exiftool wrappers rather than importing
# photoindex's (tools never import tools - TOOLING §15). Tests monkeypatch these
# two functions to exercise the photo paths without exiftool on PATH.

def _run_exiftool_read_keywords(file_path: Path) -> list[str]:
    """Return the embedded Keywords/Subject of one file (union, order-preserving).

    Used only to detect an already-embedded `SOURCE:` keyword before processing
    a photo. Raises RuntimeError if exiftool is missing - that is an environment
    problem the caller surfaces, distinct from "no keyword present".
    """
    cmd = ['exiftool', '-j', '-Keywords', '-Subject', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError(format_exiftool_error('fha process')) from e
    if proc.returncode != 0:
        raise RuntimeError(f'exiftool failed reading {file_path.name}: {proc.stderr.strip()}')
    try:
        rows = json.loads(proc.stdout or '[]')
    except json.JSONDecodeError as e:
        raise RuntimeError(f'exiftool returned invalid JSON: {e}') from e
    if not rows:
        return []
    row = rows[0]
    out: list[str] = []
    for key in ('Keywords', 'Subject'):
        val = row.get(key)
        if val is None:
            continue
        for v in (val if isinstance(val, list) else [val]):
            s = str(v)
            if s not in out:
                out.append(s)
    return out


def _run_exiftool_embed_source(
    file_path: Path, s_id: str, extra_keywords: list[str] | None = None
) -> str | None:
    """Append `SOURCE: {s_id}` (and any extra keywords) to a photo's Keywords.

    Uses exiftool's `+=` list-append so existing keywords (DATE:, P-ids) are
    preserved - the only sanctioned write to a photo original (AGENTS.md: photos
    are never renamed, but spec'd keyword writes through fha tools are allowed).
    `extra_keywords` carries bare P-id strings (e.g. `['P-de957bcda1']`) added
    in the same call so SOURCE: and people are atomic: one exiftool invocation
    per file, one rollback path if the record scaffold fails.
    Returns None on success, the stderr text on a per-file failure; raises
    RuntimeError only when exiftool itself is absent.
    """
    keywords = [f'SOURCE: {s_id}'] + (extra_keywords or [])
    kw_args = [f'-keywords+={kw}' for kw in keywords]
    cmd = ['exiftool'] + kw_args + ['-overwrite_original_in_place', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError(format_exiftool_error('fha process')) from e
    return None if proc.returncode == 0 else proc.stderr.strip()


def _run_exiftool_remove_source(
    file_path: Path, s_id: str, extra_keywords: list[str] | None = None
) -> str | None:
    """Remove a just-added SOURCE keyword (and any extra keywords) during rollback.

    The normal photo path writes the keyword before the record, because a
    failed exiftool write must abort without a dangling source record. If the
    later record write fails, this inverse operation restores the photo to its
    pre-run identity state so the command remains transactional. `extra_keywords`
    must match what was passed to `_run_exiftool_embed_source` so the rollback
    removes exactly what was added.
    """
    keywords = [f'SOURCE: {s_id}'] + (extra_keywords or [])
    kw_args = [f'-keywords-={kw}' for kw in keywords]
    cmd = ['exiftool'] + kw_args + ['-overwrite_original_in_place', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError(format_exiftool_error('fha process')) from e
    return None if proc.returncode == 0 else proc.stderr.strip()


def _read_source_keyword(file_path: Path) -> str | None:
    """Return the S-id embedded in a photo's `SOURCE:` keyword, or None."""
    for kw in _run_exiftool_read_keywords(file_path):
        m = _SOURCE_KEYWORD_RE.match(kw.strip())
        if m:
            return m.group(1).lower()
    return None


# ── Record scaffolding ────────────────────────────────────────────────────────

# Thin alias: the quoting rule itself lives in `_lib.yaml_inline` (shared by
# every surgical claim/frontmatter writer - see its docstring for the why).
# Kept as a module-level name here so existing call sites in this file (and
# any test importing `process._yaml_inline`) do not need to change.
_yaml_inline = yaml_inline


def _render_scaffold_file_entry(entry: dict) -> list[str]:
    """Render one `files:` inventory item as block-style YAML lines.

    `entry` keys: `file` (alias path, required), `role` (required), and the
    optional `copy`, `is_primary` (bool), and `original_filename`. The field
    order - file, role, copy, is_primary, original_filename - is fixed so a
    single-photo record (file/role/is_primary), a renamed document
    (file/role/original_filename), and a grouped variation member (which may
    carry all of them) all read consistently.
    """
    lines = [
        f'  - file: {_yaml_inline(entry["file"])}',
        f'    role: {_yaml_inline(entry["role"])}',
    ]
    if entry.get('copy'):
        lines.append(f'    copy: {_yaml_inline(entry["copy"])}')
    if entry.get('is_primary'):
        lines.append('    is_primary: true')
    if entry.get('original_filename'):
        lines.append(f'    original_filename: {_yaml_inline(entry["original_filename"])}')
    return lines


def _scaffold_text(
    s_id: str,
    title: str,
    source_type: str,
    file_entries: list[dict],
    *,
    notes_body: str | None,
    restricted: bool = False,
    citation: str | None = None,
    repository: str | None = None,
    source_date: str | None = None,
    provenance: str | None = None,
    external_links: list[dict] | None = None,
    people: list[str] | None = None,
    stem: str | None = None,
) -> str:
    """Render a §14 source record as text, ready to write.

    Built by hand (not yaml.safe_dump) so the field order matches the SPEC §14
    template a human reads, and so the `## Claims` fenced block is emitted
    verbatim - `read_record` requires the literal ```yaml fence under the
    heading, and an empty body parses to an empty claims list. The inventory
    lists every file the source covers: a single document or photo is one
    entry; a variation group or dissolved bundle is many, with the primary
    carrying `is_primary: true` (photos) and each renamed document carrying its
    `original_filename` provenance. `file_entries` is empty for a TOOLING §13b
    "pointer-only" source (no asset, `external_links` only), in which case the
    `files:` block is omitted rather than written empty.

    `restricted`/`citation`/`repository`/`source_date`/`provenance`/
    `external_links` are §14 fields a source-stub sidecar may hint (or, for
    `restricted`, that a `dna` source_type always forces) - without passing
    them through, capture-written metadata in the stub would be silently
    dropped when the stub is consumed.

    `people` is a validated list of P-ids from `--people`; they land in the
    record's `people:` field so `fha index` indexes the photo-to-person link
    and `fha find --related P-xxx` surfaces the photo source without requiring
    any face-region placement (the "photos, no photo manager" path, TOOLING §FAQ).

    `aliases:` ships from birth carrying the canonical S-id - the one line that
    makes a bare `[[S-…]]` cite click through in Obsidian. A `stem` (a human tag
    the source was known by before it had an ID - the inbox basename or a notes
    hint) is preserved as a second alias so old `[[stem]]` references keep
    resolving after processing.
    """
    aliases = [s_id]
    if stem:
        stem_alias = _slugify(stem)
        if stem_alias and stem_alias != s_id.lower():
            aliases.append(stem_alias)
    lines = [
        '---',
        f'id: {s_id}',
        f'aliases: [{", ".join(aliases)}]',
        f'title: {_yaml_inline(title)}',
        f'source_type: {source_type}',
    ]
    if source_date:
        lines.append(f'source_date: {_yaml_inline(source_date)}')
    # Proof-argument sources are authored conclusions, not captured originals
    # (SPEC §14: source_class: authored, filed under sources/proofs/).
    lines.append(f'source_class: {"authored" if source_type == "proof-argument" else "original"}')
    lines.append(f'repository: {_yaml_inline(repository) if repository else "unknown"}')
    lines.append('citation: >')
    citation_text = citation if citation else title
    lines += [f'  {line}' for line in (citation_text.splitlines() or [''])]
    if people:
        lines.append('people:')
        for pid in people:
            lines.append(f'  - {_yaml_inline(pid)}')
    else:
        lines.append('people: []')
    if restricted:
        lines.append('restricted: true')
    if provenance:
        lines.append(f'provenance: {_yaml_inline(provenance)}')
    if external_links:
        lines.append('external_links:')
        for link in external_links:
            url = link.get('url') if isinstance(link, dict) else str(link)
            if not url:
                continue
            lines.append(f'  - url: {_yaml_inline(str(url))}')
            accessed = link.get('accessed') if isinstance(link, dict) else None
            if accessed:
                lines.append(f'    accessed: {_yaml_inline(str(accessed))}')
    if file_entries:
        lines.append('files:')
        for entry in file_entries:
            lines += _render_scaffold_file_entry(entry)
    lines += [
        f'created: {_today()}',
        '---',
        '',
        '## Claims',
        '```yaml',
        '```',
        '',
        '## Notes',
    ]
    if notes_body:
        lines.append(notes_body.rstrip())
    else:
        lines.append('*(none yet - drafted in the AI pass)*')
    lines.append('')
    return '\n'.join(lines)


def _find_record_for_sid(archive_root: Path, s_id: str) -> Path | None:
    """Locate the scaffolded source record carrying `s_id`, or None.

    Globs sources/ for `*_{S-id}.md`. The filename is the durable carrier for
    source *records* (unlike photo asset files), so a filename glob is reliable
    here even though it never is for photos.
    """
    sources_dir = archive_root / 'sources'
    if not sources_dir.is_dir():
        return None
    # Match the S-id case-insensitively: filenames carry the uppercase-prefix
    # form ('…_S-xxxx.md') but a caller may pass either casing, and only the
    # 10-char body is identity.
    sid_norm = s_id.lower()
    for p in sources_dir.rglob('*.md'):
        m = _FILENAME_SOURCE_ID_RE.search(p.stem)
        if m and m.group(1).lower() == sid_norm:
            return p
    return None


def _render_file_entry(item: dict) -> list[str]:
    """Render a parsed files: list item dict as block-style lines.

    Used to re-emit an existing inline-list item (`files: [{file: ..., role:
    primary}]`) in the same two-space block style a freshly appended entry
    uses, so converting the key from inline to block form doesn't drop it.
    """
    keys = list(item.keys())
    first, *rest = keys
    lines = [f'  - {first}: {_yaml_inline(item[first])}']
    lines += [f'    {k}: {_yaml_inline(item[k])}' for k in rest]
    return lines


def _append_file_entry(record_text: str, entry_lines: list[str]) -> str:
    """Insert a files: list item into a record's frontmatter (text surgery).

    We edit the text rather than round-tripping the YAML so human comments and
    field order in an existing record survive untouched (the same discipline as
    `fha places geocode`'s surgical edits). The new item is appended after the
    last line of the existing `files:` block; if the record somehow has no
    `files:` block, one is created just before the closing frontmatter `---`.
    `entry_lines` are already indented (two spaces for the `- file:` line).
    """
    lines = record_text.split('\n')

    # Find frontmatter bounds: first '---' and the next '---'.
    try:
        start = lines.index('---')
        end = lines.index('---', start + 1)
    except ValueError:
        raise ValueError('record has no parseable frontmatter')

    files_idx = None
    for i in range(start + 1, end):
        if lines[i].rstrip() == 'files:' or lines[i].rstrip().startswith('files:'):
            files_idx = i
            break

    if files_idx is None:
        # No inventory yet - create the block immediately before the closing ---.
        insert_at = end
        block = ['files:'] + entry_lines
        return '\n'.join(lines[:insert_at] + block + lines[insert_at:])

    if lines[files_idx].rstrip() != 'files:':
        # An inline value ('files: []', 'files: [{file: ..., role: primary}]', …)
        # is valid YAML but has no block underneath it to append to. Parse out
        # any existing entries before normalizing to a bare key, so a non-empty
        # inline list's items are preserved as block items rather than dropped.
        inline_value = lines[files_idx].split(':', 1)[1].strip()
        existing_items = yaml.safe_load(inline_value) if inline_value not in ('', '~', 'null') else None
        lines[files_idx] = 'files:'
        if existing_items:
            preserved_lines = []
            for item in existing_items:
                preserved_lines.extend(_render_file_entry(item))
            lines[files_idx + 1:files_idx + 1] = preserved_lines
            end += len(preserved_lines)

    # Find the end of the files: block: the first line at/after files_idx+1 that
    # is not indented (a new top-level key) or the closing ---.
    block_end = end
    for i in range(files_idx + 1, end):
        stripped = lines[i]
        if stripped and not stripped[0].isspace():
            block_end = i
            break
    return '\n'.join(lines[:block_end] + entry_lines + lines[block_end:])


# ── Source-stub sidecar (*.notes.md) ──────────────────────────────────────────

def _find_sidecar(file_path: Path) -> Path | None:
    """Return the `{stem}.notes.md` sidecar beside an asset, if it exists.

    A lone sidecar (SPEC §12.1) is a hand- or capture-written stub paired with a
    single asset by basename: `photo.jpg` ↔ `photo.notes.md`. Bundle folders
    (multiple files + one notes.md) are M7.4, not handled here.
    """
    sidecar = file_path.with_name(file_path.stem + '.notes.md')
    return sidecar if sidecar.is_file() else None


def _is_sidecar_path(file_path: Path) -> bool:
    """True when `file_path` is a source-stub sidecar (`{stem}.notes.md`)."""
    return file_path.name.lower().endswith('.notes.md')


def _companion_for_sidecar(sidecar: Path) -> Path | None:
    """Return the single same-stem asset paired with a source-stub sidecar.

    M7.1 documents the convenient entrypoint `fha process sample.notes.md`.
    The sidecar is not the original; it is the notes wrapper around exactly one
    companion asset named `sample.*`. Refusing none-or-many matches prevents the
    tool from minting a source for the wrong file.

    Returns None, rather than raising, when the stub explicitly flags
    `asset_elsewhere: true` - TOOLING §13b case (c), the "pointer-only" source
    (no asset, citation + `external_links` only, flagged for later retrieval).
    Any other no-companion case still refuses: an unflagged missing asset is
    far more likely a mistake than a deliberate pointer-only capture.
    """
    stem = sidecar.name[:-len('.notes.md')]
    candidates = [
        p for p in sidecar.parent.iterdir()
        if p.is_file() and p.name != sidecar.name and p.stem == stem
    ]
    if not candidates:
        meta, _ = _read_sidecar(sidecar)
        if _sidecar_flag(meta, 'asset_elsewhere'):
            return None
        raise ProcessError(f'no companion asset found for source stub {sidecar.name}')
    if len(candidates) > 1:
        names = ', '.join(sorted(p.name for p in candidates))
        raise ProcessError(
            f'source stub {sidecar.name} has multiple companion assets: {names}. '
            'Process the intended asset directly.'
        )
    return candidates[0]


def _relocate_from_inbox(
    archive_root: Path,
    fha_config: dict,
    file_path: Path,
    sidecar: Path | None,
    *,
    dry_run: bool,
) -> tuple[Path, Path | None, object]:
    """Move an inbox-staged asset (+ sidecar) into its documents/photos root.

    `fha capture --asset` (and a hand-dropped file) stage in `inbox/`, but
    `process_document`/`process_photo` require the asset already under the
    configured root - that's the whole point of an inbox: every fha process
    entrypoint should know how to file something out of it rather than making
    the user move it by hand first. A no-op (returns the inputs unchanged, undo
    `None`) when `file_path` isn't under the resolved inbox root.

    The move is flat (same filename, no rename) into documents/ or photos/ -
    `process_document` mints its own `{slug}_{S-id}` rename afterward; photos
    are never renamed at all. Real moves happen with `Path.rename`, which is
    atomic on the same filesystem; on `dry_run` nothing is touched and a
    not-yet-existing destination path is returned so the caller's own preview
    can still report the post-move root. That returned path names where things
    WOULD land, not where the bytes are: any dry-run read (embedded keywords,
    sidecar discovery, variation grouping) must keep using the pre-move path,
    which the caller threads through as `real_path`/`real_paths`.

    Returns `(file_path, sidecar, undo)`. This relocation runs *before*
    `process_document`/`process_photo`'s own validation (e.g. the `dna`
    source_type's documents/dna/ requirement) and their own transactions, so a
    refusal downstream would otherwise leave the asset filed out of the inbox
    even though the command failed overall. The caller must call `undo()` (a
    no-arg callable, or `None` for the no-op case) whenever it reports the
    relocated file's command as anything other than success.
    """
    inbox_root = resolve_path('inbox', fha_config, archive_root)
    if not _is_under(file_path, inbox_root):
        return file_path, sidecar, None

    # A sidecar's `source_type` hint (e.g. `census`, `vital-record`) overrides
    # the extension heuristic: a record image like `census.jpg` is a photo
    # *extension* but the recipe/stub already knows it's a document-typed
    # source, and filing it under photos/ would scaffold the wrong record type.
    hinted_type = None
    if sidecar is not None:
        try:
            sidecar_meta, _ = _read_sidecar(sidecar)
            hinted_type = sidecar_meta.get('source_type')
        except ProcessError:
            pass  # downstream re-parse will raise the real error
    if hinted_type:
        kind = _PHOTO_SOURCE_TYPE if str(hinted_type) == _PHOTO_SOURCE_TYPE else 'document'
    else:
        kind = classify_asset(file_path, fha_config, archive_root)
    dest_root = (
        resolve_path(_PHOTO_DIR, fha_config, archive_root) if kind == 'photo'
        else resolve_path('documents', fha_config, archive_root)
    )
    new_path = dest_root / file_path.name
    new_sidecar = dest_root / sidecar.name if sidecar is not None else None
    if new_path.exists():
        raise ProcessError(f'destination already exists: {_rel(new_path, archive_root)}')
    if new_sidecar is not None and new_sidecar.exists():
        raise ProcessError(f'destination already exists: {_rel(new_sidecar, archive_root)}')

    if dry_run:
        print(f'[dry-run] Would move {file_path.name} out of inbox/ into '
              f'{_rel(dest_root, archive_root)}/')
        return new_path, new_sidecar, None

    dest_root.mkdir(parents=True, exist_ok=True)
    file_path.rename(new_path)
    if sidecar is not None:
        sidecar.rename(new_sidecar)
    print(f'Moved {file_path.name} out of inbox/ into {_rel(dest_root, archive_root)}/')

    def undo() -> None:
        if new_path.exists():
            new_path.rename(file_path)
        if sidecar is not None and new_sidecar is not None and new_sidecar.exists():
            new_sidecar.rename(sidecar)

    return new_path, new_sidecar, undo


def _read_sidecar(sidecar: Path) -> tuple[dict, str]:
    """Parse a stub sidecar into (hint frontmatter, prose body).

    The stub's optional frontmatter seeds record fields (title/source_type/
    repository hints); its prose body flows into the record's `## Notes`, since
    those notes are the starting point a reviewer reads (never accepted facts).
    A `people:` hint (names the captured page showed, not yet resolved to
    P-ids) has nowhere else to land in a §14 record, so it is folded into that
    same prose rather than silently dropped when the sidecar is consumed.

    Raises ProcessError on malformed frontmatter rather than silently dropping
    it: the sidecar is consumed (deleted) on a successful run, so any citation/
    title/source-type hints in unparseable frontmatter would otherwise be lost
    instead of surfaced for the user to fix.
    """
    rec = read_record(sidecar)
    if rec.get('parse_errors'):
        errors = '; '.join(msg for _, msg in rec['parse_errors'])
        raise ProcessError(f'{sidecar.name} has malformed frontmatter: {errors}')
    meta = rec.get('meta') or {}
    # Strip the frontmatter off the body; keep the prose the human wrote.
    body = (rec.get('body') or '').strip()
    names = [str(n) for n in (meta.get('people') or []) if str(n).strip()]
    if names:
        hint = 'Captured people mentioned on source page (unreconciled): ' + ', '.join(names)
        body = f'{body}\n\n{hint}' if body else hint
    return meta, body


def _sidecar_str(sidecar_meta: dict, key: str) -> str | None:
    """A sidecar hint field as a string, or None - feeds an optional §14 field."""
    val = sidecar_meta.get(key)
    return str(val) if val not in (None, '') else None


def _sidecar_source_date(sidecar_meta: dict, sidecar_name: str) -> str | None:
    """Return a sidecar `source_date:` hint normalized to EDTF, or None.

    Mirrors `fha capture`'s `--date` handling: loose but clear human dates
    ("about 1880", "1870s") are translated before writing the §14 record, while
    genuinely unclear dates still stop before the stub is consumed.
    """
    source_date = _sidecar_str(sidecar_meta, 'source_date')
    if source_date is None:
        return None
    normalized = normalize_date(source_date)
    if normalized is None:
        raise ProcessError(
            f'{sidecar_name} hints {format_edtf_error(source_date, field="source_date")} '
            'Fix the sidecar before processing.'
        )
    return normalized


def _sidecar_flag(sidecar_meta: dict, key: str) -> bool:
    """A sidecar hint field as a bool - feeds an optional §14 flag field."""
    return sidecar_meta.get(key) in (True, 'true')


def _sidecar_external_links(sidecar_meta: dict) -> list[dict]:
    """A sidecar's `external_links:` hint as a list of `{url, accessed}` dicts.

    Mirrors `capture.py`'s `RecipeResult.external_links` shape so a captured
    stub's links survive unchanged into the §14 record.
    """
    raw = sidecar_meta.get('external_links')
    if not isinstance(raw, list):
        return []
    links = []
    for item in raw:
        if isinstance(item, dict) and item.get('url'):
            links.append({'url': str(item['url']), 'accessed': item.get('accessed')})
        elif isinstance(item, str) and item:
            links.append({'url': item})
    return links


def _bundle_file_hints(sidecar_meta: dict) -> dict[str, dict]:
    """Return per-file hints from a bundle `notes.md`, keyed by filename.

    SPEC 12.1 keeps source stubs deliberately light, but allows bundle notes
    to carry per-file role hints such as `recording` or `transcript`. Capture
    tools and humans tend to write those hints in two natural shapes, both
    accepted here:

      roles:
        interview.mp3: recording

      files:
        - file: interview.mp3
          role: recording

    The tool refuses malformed hint structures before moving anything. A typo
    in pre-source metadata should be fixed in the stub, not silently flattened
    into generic `attachment` roles and then deleted with the consumed stub.
    """
    hints: dict[str, dict] = {}

    roles = sidecar_meta.get('roles')
    if roles is not None:
        if not isinstance(roles, dict):
            raise ProcessError('bundle notes field `roles` must be a filename -> role mapping.')
        for filename, role in roles.items():
            if role in (None, ''):
                continue
            hints[Path(str(filename)).name] = {'role': str(role)}

    files = sidecar_meta.get('files')
    if files is None:
        return hints

    if isinstance(files, dict):
        iterable = []
        for filename, data in files.items():
            if isinstance(data, dict):
                item = dict(data)
                item.setdefault('file', filename)
            else:
                item = {'file': filename, 'role': data}
            iterable.append(item)
    elif isinstance(files, list):
        iterable = files
    else:
        raise ProcessError('bundle notes field `files` must be a list or filename mapping.')

    for item in iterable:
        if not isinstance(item, dict):
            raise ProcessError('bundle notes `files` entries must be mappings.')
        filename = item.get('file') or item.get('name') or item.get('path')
        if not filename:
            raise ProcessError('bundle notes `files` entry is missing `file`.')
        key = Path(str(filename)).name
        hint = hints.setdefault(key, {})
        if item.get('role') not in (None, ''):
            hint['role'] = str(item['role'])
        if item.get('copy') not in (None, ''):
            hint['copy'] = str(item['copy'])
        if item.get('is_primary') in (True, 'true', 'yes', '1'):
            hint['is_primary'] = True
    return hints


# ── Variation detection (M7.3) ────────────────────────────────────────────────
#
# Variation siblings (front/back, copy letters, crops, negatives, booklet
# pages) share a filename base_id. The grouping grammar (`grouping_stem`,
# `variant_role`, `select_variation_primary`) lives in _lib so this tool and
# `fha photoindex` agree on what counts as one physical photo - a folder must
# group identically no matter which tool looks at it. Tools never import tools.

def _is_photo_ext(file_path: Path) -> bool:
    """True if the filename has a known photo extension (TOOLING §6 grammar)."""
    return file_path.suffix.lower() in PHOTO_EXTENSIONS


def _photo_variation_siblings(file_path: Path) -> list[Path]:
    """Return the photo files in `file_path`'s directory that share its base_id.

    The result always includes `file_path` itself and is sorted, so a length of
    one means "no siblings - process normally" and a length >1 means a candidate
    variation set the caller should surface with the one/separate/skip prompt.

    The directory listing cannot always yield `file_path` itself, so it is
    added back explicitly. Two real cases: a dry-run inbox relocation hands us
    the *virtual* post-move destination (`_relocate_from_inbox` moved nothing,
    the destination directory may not even exist yet), and an odd-extension
    file under the photos root is a photo by location, not by extension, so
    the `_is_photo_ext` filter skips it. Either way an empty result would send
    `select_variation_primary` an empty set and crash; the file being
    processed is by definition a member of its own group.

    Matching is purely by filename grammar (`grouping_stem`) - cheap, no
    exiftool, no disk reads beyond a directory listing - so the common
    single-photo case never pays for variation detection. Files already carrying
    an `_{S-id}` in the name are excluded: a processed document-style name is not
    an unprocessed sibling. (A photo already carrying a SOURCE: keyword can only
    be detected with exiftool; that check happens later, in process_photo_group,
    where it can refuse the whole set cleanly.)
    """
    stem_key = grouping_stem(parse_media_filename(file_path.stem))
    siblings = []
    if file_path.parent.is_dir():
        for p in file_path.parent.iterdir():
            if not p.is_file() or not _is_photo_ext(p):
                continue
            if _is_sidecar_path(p) or _filename_has_source_id(p):
                continue
            if grouping_stem(parse_media_filename(p.stem)) == stem_key:
                siblings.append(p)
    if file_path not in siblings:
        siblings.append(file_path)
    return sorted(siblings)


def _variation_role_copy(file_path: Path, is_primary: bool) -> tuple[str, str | None]:
    """Return the (role, copy) `files:` annotation for one variation member.

    The primary always gets `role: primary`. A non-primary member's role comes
    from `variant_role` (back, front, page-3, negative, bw, a freeform suffix,
    or crop); when the filename encodes only a bare copy letter ('portrait_1880b')
    there is no part-kind, so the role falls back to 'variant' and the letter is
    recorded in `copy:`. A negative is source material for the root image rather
    than an A/B print, so its copy letter (if any) is dropped - mirroring how
    `fha photoindex` files negatives at the stem level.
    """
    parsed = parse_media_filename(file_path.stem)
    if is_primary:
        return 'primary', None
    role = variant_role(parsed) or 'variant'
    copy = None if parsed.part_kind == 'negative' else parsed.variant_id
    return role, copy


def _batch_type(members: list[Path]) -> tuple[str, str]:
    """Classify a multi-image set as TOOLING §6 batch type A–D (informational).

    The label is shown to the human as context for the one/separate decision; it
    drives no behavior. Precedence matches the table: multi-page booklets (C)
    and helper crops (D) are the most specific, then front/back pairs (B), with
    plain variant scans (A) as the default.
    """
    parsed = [parse_media_filename(p.stem) for p in members]
    if any(p.part_kind == 'page' for p in parsed):
        return 'C', 'multi-page document set'
    if any(p.is_crop for p in parsed) and any(not p.is_crop for p in parsed):
        return 'D', 'helper crops of a parent image'
    if any(p.part_kind in ('front', 'back') for p in parsed):
        return 'B', 'front/back of one physical item'
    return 'A', 'variant scans of one image'


# ── Triage scoring (M7.3 folder mode) ─────────────────────────────────────────
#
# Folder mode ranks unprocessed photo groups by the same evidence signals
# `fha photoindex triage` uses (TOOLING §15b) so the two tools order the same
# folder the same way. photoindex scores from its cached SQLite rows; here we
# read the few needed fields straight off the files via exiftool, degrading to
# filename-only signals (back-variant) when exiftool is unavailable so a triage
# still ranks rather than crashing on a machine without the binary.

# A user_comment that is purely machine-authored is weak evidence (TOOLING §15b);
# mirrors photoindex._AI_COMMENT_RE.
_AI_COMMENT_RE = re.compile(r'^\s*(AI|Model):', re.I)
_DATE_KEYWORD_RE = re.compile(r'^DATE:\s*(.+)$')


def _run_exiftool_read_meta(file_path: Path) -> dict:
    """Read the caption/date/keyword signals one photo contributes to triage.

    Returns {'caption', 'user_comment', 'edtf', 'has_pid_keyword'}. A separate
    seam from `_run_exiftool_read_keywords` (which reads only Keywords/Subject
    to detect a SOURCE: marker) because triage also needs the caption and
    description fields. Monkeypatched in tests; raises RuntimeError when exiftool
    is absent so the caller can degrade rather than fail.
    """
    cmd = ['exiftool', '-j', '-Caption-Abstract', '-XMP-dc:Description',
           '-UserComment', '-Keywords', '-Subject', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError(format_exiftool_error('fha process folder triage')) from e
    if proc.returncode != 0:
        raise RuntimeError(f'exiftool failed reading {file_path.name}: {proc.stderr.strip()}')
    try:
        rows = json.loads(proc.stdout or '[]')
    except json.JSONDecodeError as e:
        raise RuntimeError(f'exiftool returned invalid JSON: {e}') from e
    row = rows[0] if rows else {}

    keywords: list[str] = []
    for key in ('Keywords', 'Subject'):
        val = row.get(key)
        if val is None:
            continue
        for v in (val if isinstance(val, list) else [val]):
            keywords.append(str(v))

    edtf = None
    for kw in keywords:
        m = _DATE_KEYWORD_RE.match(kw.strip())
        if m:
            edtf = m.group(1).strip()
            break
    has_pid = any(id_type_of(kw.strip()) == 'P' for kw in keywords)

    return {
        'caption': row.get('Caption-Abstract') or row.get('Description'),
        'user_comment': row.get('UserComment'),
        'edtf': edtf,
        'has_pid_keyword': has_pid,
    }


def _score_photo_group(members: list[Path]) -> tuple[int, list[str]]:
    """Score one candidate group by TOOLING §15b signals; return (score, signals).

    Point values mirror `photoindex._score_group` so the two tools agree on
    ranking: +3 a human caption, +2 a bare P-id keyword, +1 a confident date
    (year-precise with no ~/? marker), +1 a back variant in the set, -2 an
    AI-only user_comment with no caption. Signals are evaluated across every
    member (a caption on the back of a print counts for the whole physical
    photo). Per-file metadata is read best-effort; a member whose metadata can't
    be read (no exiftool, unreadable file) contributes only its filename-derived
    back-variant signal.
    """
    metas = []
    for p in members:
        try:
            metas.append(_run_exiftool_read_meta(p))
        except RuntimeError:
            metas.append({'caption': None, 'user_comment': None,
                          'edtf': None, 'has_pid_keyword': False})

    score = 0
    signals: list[str] = []

    has_caption = any(m['caption'] for m in metas)
    if has_caption:
        score += 3
        signals.append('caption')
    if any(m['has_pid_keyword'] for m in metas):
        score += 2
        signals.append('pid-keyword')
    # A confident date is year-precise (or finer) with no approximation marker -
    # photoindex's _edtf_confidence marker_rank 0 condition, expressed directly.
    if any(m['edtf'] and is_valid_edtf(m['edtf']) and '~' not in m['edtf'] and '?' not in m['edtf']
           for m in metas):
        score += 1
        signals.append('date:Y!+')
    if any(parse_media_filename(p.stem).part_kind == 'back' for p in members):
        score += 1
        signals.append('back-variant')
    if (not has_caption) and any(
            m['user_comment'] and _AI_COMMENT_RE.match(m['user_comment']) for m in metas):
        score -= 2
        signals.append('ai-only')

    return score, signals


# ── Top-level operations ──────────────────────────────────────────────────────

class ProcessError(Exception):
    """A user-facing processing failure (refusal or bad input)."""


def _parse_people_ids(raw: str | None, archive_root: Path) -> list[str]:
    """Parse `--people` into known person IDs before any photo write.

    `--people` accepts a comma/space/semicolon-separated list of bare P-ids
    (e.g. 'P-de957bcda1, P-ab3c8f0e12'). Each token is checked against the
    Crockford ID format and the archive's person records. A typo that still
    looks like a P-id must fail before exiftool writes to original photo
    metadata; `fha photoindex tag-person` follows the same known-person rule.
    """
    if not raw:
        return []
    known_people = scan_person_record_ids(archive_root)
    tokens = re.split(r'[,;\s]+', raw.strip())
    ids: list[str] = []
    for tok in tokens:
        if not tok:
            continue
        if not is_valid_id(tok) or id_type_of(tok) != 'P':
            raise ProcessError(
                f'{tok!r} is not a valid P-id. P-ids look like P-de957bcda1 - '
                'a P followed by a dash and 10 characters from the archive alphabet '
                '(0-9 and lowercase a-z, except i, l, o, u). '
                'Run `fha find <name>` to look up the right P-id.'
            )
        normalized = normalize_id(tok)
        if normalized not in known_people:
            raise ProcessError(
                f'{fmt_id_display(normalized)} is not a known person in this archive. '
                'Run `fha find <name>` to look up the right P-id, or create the person '
                'record before tagging the photo.'
            )
        ids.append(fmt_id_display(normalized))
    return ids


def process_document(
    archive_root: Path,
    fha_config: dict,
    file_path: Path,
    *,
    source_type: str,
    slug: str | None,
    title: str | None,
    source_date: str | None,
    dry_run: bool,
    real_path: Path | None = None,
    source_id: str | None = None,
    report: dict | None = None,
) -> int:
    """M7.1: rename a documents-root original and scaffold its source record.

    Transactional: the rename and the record write each register an undo, and
    any exception unwinds them in reverse, so an interrupted run leaves neither
    a renamed-but-unrecorded file nor a record pointing at a vanished asset.

    `real_path` is set only on a dry-run inbox relocation, where `file_path`
    is the virtual post-move destination (nothing was moved). The sidecar and
    its hints still sit beside the real file, so discovery targets `real_path`;
    otherwise the preview would miss the stub, scaffold under the wrong
    source_type directory, and hide the stub deletion the live run performs.
    Everything destination-shaped (rename target, alias, record path) keeps
    using `file_path` - those name what live WOULD create.

    `source_id` is `_mint_one_source_id`'s override (see its docstring);
    `report`, when given, is filled with `{'source_id': sid}` for the caller
    (`fha serve`'s process.file verb) to read back - the id used is reported
    on BOTH a dry-run preview and a live apply, so the two can be compared or
    threaded together, the same round-trip person.new/claim.new already have.
    """
    if existing := _filename_has_source_id(file_path):
        raise ProcessError(
            f'{file_path.name} already carries an S-id ({existing.upper()}); '
            'it looks already processed. Refusing to mint a second ID.'
        )

    documents_root = resolve_path('documents', fha_config, archive_root)
    if not _is_under(file_path, documents_root):
        raise ProcessError(
            f'{file_path.name} is not under the configured documents root '
            f'({_rel(documents_root, archive_root)}); file it there before processing - '
            'a record outside the asset roots cannot be expressed as a portable alias path.'
        )

    final_title = title or _slugify(file_path.stem).replace('-', ' ')
    sidecar = _find_sidecar(real_path if real_path is not None else file_path)
    notes_body = None
    sidecar_meta: dict = {}
    if sidecar is not None:
        sidecar_meta, notes_body = _read_sidecar(sidecar)
        # A stub may hint a better title / source_type than the bare filename.
        if title is None and sidecar_meta.get('title'):
            final_title = str(sidecar_meta['title'])
        if source_type == _DEFAULT_DOCUMENT_TYPE and sidecar_meta.get('source_type'):
            hinted = str(sidecar_meta['source_type'])
            if hinted not in SOURCE_TYPES:
                raise ProcessError(
                    f'{sidecar.name} hints {format_source_type_error(hinted)} '
                    'Fix the sidecar, or pass --type with one of those values.'
                )
            source_type = hinted

    # DNA sources always carry restricted: true and must live under
    # documents/dna/ (SPEC §8.5.5, lint E017) - refuse before scaffolding a
    # source the linter would immediately flag.
    if source_type == 'dna':
        dna_root = documents_root / 'dna'
        if not _is_under(file_path, dna_root):
            raise ProcessError(
                f'{file_path.name} is source_type dna but is not under '
                f'{_rel(dna_root, archive_root)}; file DNA originals there before processing.'
            )

    sid = _mint_one_source_id(archive_root, source_id=source_id)
    if report is not None:
        report['source_id'] = sid
    final_slug = _derive_slug(slug, final_title if title is None else title, file_path)
    ext = file_path.suffix
    new_name = f'{final_slug}_{sid}{ext}'
    new_path = file_path.with_name(new_name)

    # SPEC §14: proof-argument sources live under sources/proofs/, not a
    # sources/proof-argument/ directory matching the source_type literally.
    record_dir = archive_root / 'sources' / _record_subdir(source_type)
    record_path = record_dir / f'{final_slug}_{sid}.md'
    file_alias = path_to_alias(new_path, 'documents', fha_config, archive_root)

    if new_path.exists():
        raise ProcessError(f'destination file already exists: {new_path.name}')
    if record_path.exists():
        raise ProcessError(f'record already exists: {_rel(record_path, archive_root)}')

    # The original inbox basename is the human tag the source was known by; keep
    # it as an alias so any `[[old-name]]` reference still resolves once the file
    # is renamed to `{slug}_{S-id}`. _scaffold_text drops it when it matches the
    # slug the filename already carries (no redundant alias).
    text = _scaffold_text(
        sid, final_title, source_type,
        [{'file': file_alias, 'role': 'primary', 'original_filename': file_path.name}],
        notes_body=notes_body,
        restricted=source_type == 'dna' or _sidecar_flag(sidecar_meta, 'restricted'),
        citation=_sidecar_str(sidecar_meta, 'citation'),
        repository=_sidecar_str(sidecar_meta, 'repository'),
        source_date=source_date or _sidecar_source_date(sidecar_meta, sidecar.name if sidecar else file_path.name),
        provenance=_sidecar_str(sidecar_meta, 'provenance'),
        stem=file_path.stem if _slugify(file_path.stem) != final_slug else None,
    )

    if dry_run:
        print(f'[dry-run] Would mint {sid}')
        print(f'[dry-run] Would rename {file_path.name} -> {new_name}')
        print(f'[dry-run] Would scaffold {_rel(record_path, archive_root)}')
        if sidecar is not None:
            print(f'[dry-run] Would delete stub {sidecar.name} (its notes -> ## Notes)')
        return EXIT_CLEAN

    undo: list = []
    try:
        file_path.rename(new_path)
        undo.append(lambda: new_path.rename(file_path))

        record_dir.mkdir(parents=True, exist_ok=True)
        record_path.write_text(text, encoding='utf-8')
        undo.append(lambda: record_path.unlink(missing_ok=True))

        if sidecar is not None:
            sidecar.unlink()
    except Exception as e:
        for fn in reversed(undo):
            try:
                fn()
            except Exception:
                pass
        print(f'ERROR: processing failed, rolled back: {e}', file=sys.stderr)
        return EXIT_FAILURE

    print(f'Minted {sid}')
    print(f'Renamed {file_path.name} -> {new_name}')
    print(f'Scaffolded {_rel(record_path, archive_root)}')
    if sidecar is not None:
        print(f'Consumed stub {sidecar.name} (notes -> ## Notes)')
    return EXIT_CLEAN


def _pointer_provenance(sidecar_meta: dict) -> str | None:
    """Provenance text for a case-(c) pointer-only source, folding in the
    human's own `asset_path` shorthand when the sidecar carries one
    (`fha capture --path`, TOOLING_INGESTION §2.6) alongside any hand-written
    `provenance:` note - concatenated, not overwritten, so a human note
    survives being processed alongside a captured location hint.

    Uses `asset_path` (the location exactly as the human typed it - "their
    own shorthand may be meaningful to them", `capture.py`'s
    `run_capture_path` docstring) rather than `asset_path_absolute`: the
    absolute form is a machine-specific path, and a source record is a
    long-lived file that may travel in a packet or export, where a local
    absolute path has no business appearing (AGENTS_TOOLING's privacy rule
    against local absolute paths in exported/committed output).
    """
    existing = _sidecar_str(sidecar_meta, 'provenance')
    asset_path = _sidecar_str(sidecar_meta, 'asset_path')
    if not asset_path:
        return existing
    location_note = f'Original not copied into the archive - last known location: {asset_path}.'
    return f'{existing}\n{location_note}' if existing else location_note


def process_pointer_only(
    archive_root: Path,
    fha_config: dict,
    sidecar: Path,
    *,
    source_type: str | None = None,
    slug: str | None = None,
    title: str | None = None,
    source_date: str | None = None,
    dry_run: bool,
    source_id: str | None = None,
    report: dict | None = None,
) -> int:
    """TOOLING §13b case (c): mint a source record with no asset.

    Only reached when `_companion_for_sidecar` found no same-stem file *and*
    the stub explicitly flags `asset_elsewhere: true`. Two pointer-only
    shapes are accepted: citation + `external_links` (the page merely says
    "record held at the county courthouse"), or `asset_path` (a
    `fha capture --path` stub - a specific asset known to exist but that
    must never be copied/moved; TOOLING_INGESTION §2.6). Either is enough to
    mint; a stub with neither refuses, naming the fix. Every other
    no-companion case still refuses in `_companion_for_sidecar`.

    `source_id`/`report` are `process_document`'s same mint-override/
    report-back pair - see its docstring.
    """
    sidecar_meta, notes_body = _read_sidecar(sidecar)
    resolved_type = source_type or _DEFAULT_DOCUMENT_TYPE
    if source_type is None and sidecar_meta.get('source_type'):
        resolved_type = str(sidecar_meta['source_type'])
    if resolved_type not in SOURCE_TYPES:
        raise ProcessError(
            f'{format_source_type_error(resolved_type, where="--type" if source_type else "source_type")} '
            'Fix the sidecar, or pass --type with one of those values.'
        )
    source_type = resolved_type

    final_title = title or (
        str(sidecar_meta['title']) if sidecar_meta.get('title')
        else _slugify(sidecar.stem).replace('-', ' ')
    )
    external_links = _sidecar_external_links(sidecar_meta)
    asset_path = _sidecar_str(sidecar_meta, 'asset_path')
    if not external_links and not asset_path:
        raise ProcessError(
            f'{sidecar.name} flags asset_elsewhere but has neither external_links '
            'nor asset_path; add at least one before processing.'
        )

    # DNA sources always carry restricted: true (SPEC §8.5.5, lint E017),
    # same as process_document - a missing asset doesn't relax that rule.
    restricted = source_type == 'dna' or _sidecar_flag(sidecar_meta, 'restricted')

    sid = _mint_one_source_id(archive_root, source_id=source_id)
    if report is not None:
        report['source_id'] = sid
    final_slug = _derive_slug(slug, final_title, sidecar)
    record_dir = archive_root / 'sources' / _record_subdir(source_type)
    record_path = record_dir / f'{final_slug}_{sid}.md'
    if record_path.exists():
        raise ProcessError(f'record already exists: {_rel(record_path, archive_root)}')

    text = _scaffold_text(
        sid, final_title, source_type, [],
        notes_body=notes_body,
        restricted=restricted,
        citation=_sidecar_str(sidecar_meta, 'citation'),
        repository=_sidecar_str(sidecar_meta, 'repository'),
        source_date=source_date or _sidecar_source_date(sidecar_meta, sidecar.name),
        provenance=_pointer_provenance(sidecar_meta),
        external_links=external_links,
    )

    if dry_run:
        print(f'[dry-run] Would mint {sid}')
        print(f'[dry-run] Would scaffold {_rel(record_path, archive_root)} (no asset - asset-elsewhere)')
        print(f'[dry-run] Would delete stub {sidecar.name} (its notes -> ## Notes)')
        return EXIT_CLEAN

    try:
        record_dir.mkdir(parents=True, exist_ok=True)
        record_path.write_text(text, encoding='utf-8')
        sidecar.unlink()
    except Exception as e:
        record_path.unlink(missing_ok=True)
        print(f'ERROR: processing failed, rolled back: {e}', file=sys.stderr)
        return EXIT_FAILURE

    print(f'Minted {sid}')
    print(f'Scaffolded {_rel(record_path, archive_root)} (asset-elsewhere; no companion file)')
    print(f'Consumed stub {sidecar.name} (notes -> ## Notes)')
    return EXIT_CLEAN


def process_photo(
    archive_root: Path,
    fha_config: dict,
    file_path: Path,
    *,
    slug: str | None,
    title: str | None,
    source_date: str | None,
    dry_run: bool,
    people: list[str] | None = None,
    real_path: Path | None = None,
    source_id: str | None = None,
    report: dict | None = None,
) -> int:
    """M7.2: embed a SOURCE keyword in a photo and scaffold its source record.

    Photos are never renamed. The keyword write is the risky step and happens
    first: if exiftool fails, nothing is scaffolded (TOOLING §6 "abort on
    failure, do not scaffold"). If the scaffold then fails, the just-written
    keyword is removed so the photo is not left half-processed.

    `people` is a validated list of P-ids from `--people`; each is written as
    a bare keyword in the same exiftool call as SOURCE: (one atomic write, one
    rollback path), and also lands in the source record's `people:` field so
    `fha index` + `fha find --related P-xxx` work without any face-region
    placement (the no-photo-manager path, TOOLING §FAQ).

    `real_path` is set only on a dry-run inbox relocation, where `file_path`
    is the virtual post-move destination (nothing was moved). The photo's
    bytes - its embedded keywords - and any sidecar still live at `real_path`,
    so those reads target it: the preview then refuses an already-processed
    photo and carries the stub's hints exactly as the live run would. All
    destination-shaped output (alias, record path, the embed line) keeps using
    `file_path` - the live run's post-move reality.

    `source_id`/`report` are `process_document`'s same mint-override/
    report-back pair - see its docstring.
    """
    photos_root = resolve_path(_PHOTO_DIR, fha_config, archive_root)
    if not _is_under(file_path, photos_root):
        raise ProcessError(
            f'{file_path.name} is not under the configured photos root '
            f'({_rel(photos_root, archive_root)}); file it there before processing - '
            'a record outside the asset roots cannot be expressed as a portable alias path.'
        )
    on_disk = real_path if real_path is not None else file_path

    # Read all keywords at once: one exiftool call detects a pre-existing SOURCE:
    # keyword (refuses re-processing) and identifies which P-ids from --people are
    # already present. Only the absent ones are embedded and rolled back; ExifTool's
    # -= operator removes every occurrence of a value, so rolling back a P-id that
    # predated this run would delete it permanently.
    if dry_run:
        try:
            raw_kws = _run_exiftool_read_keywords(on_disk)
        except RuntimeError as e:
            print(f'WARNING: could not read existing keywords from {on_disk.name}: {e}',
                  file=sys.stderr)
            raw_kws = []
    else:
        raw_kws = _run_exiftool_read_keywords(on_disk)
    existing = next(
        (mo.group(1).lower() for kw in raw_kws if (mo := _SOURCE_KEYWORD_RE.match(kw.strip()))),
        None,
    )
    if existing:
        raise ProcessError(
            f'{file_path.name} already carries SOURCE: {existing.upper()}; '
            'it looks already processed. Refusing to mint a second ID.'
        )
    existing_pids = {kw.strip() for kw in raw_kws if id_type_of(kw.strip()) == 'P'}
    new_people = [p for p in (people or []) if p not in existing_pids]

    final_title = title or _slugify(file_path.stem).replace('-', ' ')
    sidecar = _find_sidecar(on_disk)
    notes_body = None
    sidecar_meta: dict = {}
    if sidecar is not None:
        sidecar_meta, notes_body = _read_sidecar(sidecar)
        if title is None and sidecar_meta.get('title'):
            final_title = str(sidecar_meta['title'])

    sid = _mint_one_source_id(archive_root, source_id=source_id)
    if report is not None:
        report['source_id'] = sid
    final_slug = _derive_slug(slug, final_title if title is None else title, file_path)
    record_dir = archive_root / 'sources' / _PHOTO_DIR
    record_path = record_dir / f'{final_slug}_{sid}.md'
    file_alias = path_to_alias(file_path, _PHOTO_DIR, fha_config, archive_root)

    if record_path.exists():
        raise ProcessError(f'record already exists: {_rel(record_path, archive_root)}')

    text = _scaffold_text(
        sid, final_title, _PHOTO_SOURCE_TYPE,
        [{'file': file_alias, 'role': 'primary', 'is_primary': True}],
        notes_body=notes_body,
        restricted=_sidecar_flag(sidecar_meta, 'restricted'),
        citation=_sidecar_str(sidecar_meta, 'citation'),
        repository=_sidecar_str(sidecar_meta, 'repository'),
        source_date=source_date or _sidecar_source_date(sidecar_meta, sidecar.name if sidecar else file_path.name),
        provenance=_sidecar_str(sidecar_meta, 'provenance'),
        people=people or None,
    )

    if dry_run:
        print(f'[dry-run] Would mint {sid}')
        kw_desc = f'SOURCE: {sid}' + (f' + {len(new_people)} P-id keyword(s)' if new_people else '')
        print(f'[dry-run] Would embed {kw_desc} in {file_path.name} (no rename)')
        print(f'[dry-run] Would scaffold {_rel(record_path, archive_root)}')
        if new_people:
            print(f'[dry-run] people: {", ".join(new_people)}')
        if sidecar is not None:
            print(f'[dry-run] Would delete stub {sidecar.name} (its notes -> ## Notes)')
        return EXIT_CLEAN

    err = _run_exiftool_embed_source(file_path, sid, extra_keywords=new_people or None)
    if err is not None:
        print(f'ERROR: exiftool could not embed SOURCE keyword in {file_path.name}: {err}',
              file=sys.stderr)
        print('Nothing was scaffolded.', file=sys.stderr)
        return EXIT_FAILURE

    try:
        record_dir.mkdir(parents=True, exist_ok=True)
        record_path.write_text(text, encoding='utf-8')
        if sidecar is not None:
            sidecar.unlink()
    except Exception as e:
        try:
            record_path.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            rollback_err = _run_exiftool_remove_source(file_path, sid, extra_keywords=new_people or None)
        except RuntimeError as rollback_exc:
            rollback_err = str(rollback_exc)
        print(f'ERROR: SOURCE keyword was embedded in {file_path.name} but the record '
              f'could not be written: {e}', file=sys.stderr)
        if rollback_err is None:
            print(f'Rolled back SOURCE: {sid} from {file_path.name}.', file=sys.stderr)
        else:
            print(f'WARNING: could not roll back SOURCE: {sid} from {file_path.name}: '
                  f'{rollback_err}', file=sys.stderr)
        return EXIT_FAILURE

    print(f'Minted {sid}')
    print(f'Embedded SOURCE: {sid} in {file_path.name} (not renamed)')
    if new_people:
        print(f'Tagged people: {", ".join(new_people)}')
    print(f'Scaffolded {_rel(record_path, archive_root)}')
    if sidecar is not None:
        print(f'Consumed stub {sidecar.name} (notes -> ## Notes)')
    return EXIT_CLEAN


def _read_existing_source_keyword(file_path: Path, dry_run: bool) -> tuple[str | None, bool]:
    """Read a photo's embedded SOURCE: keyword, degrading on dry-run.

    Returns (s_id_or_None, readable). On a live run a read failure propagates
    (RuntimeError); on dry-run it is downgraded to a warning and reported as
    "not readable" so a machine without exiftool still gets a preview, matching
    the single-photo dry-run contract.
    """
    if dry_run:
        try:
            return _read_source_keyword(file_path), True
        except RuntimeError as e:
            print(f'WARNING: could not read existing keywords from {file_path.name}: {e}',
                  file=sys.stderr)
            return None, False
    return _read_source_keyword(file_path), True


def process_photo_group(
    archive_root: Path,
    fha_config: dict,
    members: list[Path],
    *,
    slug: str | None,
    title: str | None,
    source_date: str | None,
    dry_run: bool,
    people: list[str] | None = None,
    real_paths: dict[Path, Path] | None = None,
) -> int:
    """M7.3: process a variation set as ONE source sharing a single S-id.

    Every member is a photo under the photos root; none is renamed. The chosen
    primary (the plain scan - `select_variation_primary`) carries `is_primary:
    true`, the rest carry their role/copy annotation derived from the filename
    grammar. The keyword writes happen before the record (the process_photo
    discipline) and the whole set is transactional: if any embed fails, the
    keywords already written are removed; if the record write fails, both the
    keywords and the record are rolled back, so an interrupted run never leaves
    a half-tagged set. `people` (P-ids from `--people`) are written as bare
    keywords on every member of the group and land in the source record's
    `people:` list - same atomic-write discipline as for a single photo.

    `real_paths` maps a member that is a virtual dry-run inbox destination
    (nothing was moved) to the file's real on-disk location, so keyword reads
    and sidecar discovery run against reality - the process_photo `real_path`
    contract, extended to a set where at most one member (the relocated file)
    is virtual. Members not in the map are on disk where they claim to be.
    """
    members = sorted(members)
    real_paths = real_paths or {}
    photos_root = resolve_path(_PHOTO_DIR, fha_config, archive_root)
    for m in members:
        if not _is_under(m, photos_root):
            raise ProcessError(
                f'{m.name} is not under the configured photos root '
                f'({_rel(photos_root, archive_root)}); file the whole set there before processing.'
            )

    # Refuse the set if any member is already processed, and collect per-member
    # existing P-id keywords so rollback only removes the ones this run added.
    # ExifTool's -= operator removes every occurrence of a value, so rolling back
    # a P-id keyword that predated this run would delete it permanently.
    per_member_new_people: dict[Path, list[str]] = {}
    for m in members:
        m_on_disk = real_paths.get(m, m)
        if dry_run:
            try:
                raw_kws = _run_exiftool_read_keywords(m_on_disk)
            except RuntimeError as e:
                print(f'WARNING: could not read existing keywords from {m_on_disk.name}: {e}',
                      file=sys.stderr)
                raw_kws = []
        else:
            raw_kws = _run_exiftool_read_keywords(m_on_disk)
        existing_source = next(
            (mo.group(1).lower() for kw in raw_kws if (mo := _SOURCE_KEYWORD_RE.match(kw.strip()))),
            None,
        )
        if existing_source:
            raise ProcessError(
                f'{m.name} already carries SOURCE: {existing_source.upper()}; the set looks '
                'partly processed. Attach the rest with --more instead.'
            )
        existing_pids = {kw.strip() for kw in raw_kws if id_type_of(kw.strip()) == 'P'}
        per_member_new_people[m] = [p for p in (people or []) if p not in existing_pids]

    primary = select_variation_primary(members, lambda p: parse_media_filename(p.stem))
    ordered = [primary] + [m for m in members if m != primary]

    final_title = title or _slugify(primary.stem).replace('-', ' ')
    sidecar = _find_sidecar(real_paths.get(primary, primary))
    notes_body = None
    sidecar_meta: dict = {}
    if sidecar is not None:
        sidecar_meta, notes_body = _read_sidecar(sidecar)
        if title is None and sidecar_meta.get('title'):
            final_title = str(sidecar_meta['title'])

    sid = _mint_one_source_id(archive_root)
    final_slug = _derive_slug(slug, final_title if title is None else title, primary)
    record_dir = archive_root / 'sources' / _PHOTO_DIR
    record_path = record_dir / f'{final_slug}_{sid}.md'
    if record_path.exists():
        raise ProcessError(f'record already exists: {_rel(record_path, archive_root)}')

    file_entries = []
    for m in ordered:
        is_primary = m == primary
        role, copy = _variation_role_copy(m, is_primary)
        entry = {
            'file': path_to_alias(m, _PHOTO_DIR, fha_config, archive_root),
            'role': role,
            'is_primary': is_primary,
        }
        if copy:
            entry['copy'] = copy
        file_entries.append(entry)

    text = _scaffold_text(
        sid, final_title, _PHOTO_SOURCE_TYPE, file_entries,
        notes_body=notes_body,
        restricted=_sidecar_flag(sidecar_meta, 'restricted'),
        citation=_sidecar_str(sidecar_meta, 'citation'),
        repository=_sidecar_str(sidecar_meta, 'repository'),
        source_date=source_date or _sidecar_source_date(sidecar_meta, sidecar.name if sidecar else primary.name),
        provenance=_sidecar_str(sidecar_meta, 'provenance'),
        people=people or None,
    )

    if dry_run:
        print(f'[dry-run] Would mint {sid} for a {len(members)}-file variation set')
        for m in ordered:
            tag = 'primary' if m == primary else _variation_role_copy(m, False)[0]
            print(f'[dry-run] Would embed SOURCE: {sid} in {m.name} ({tag}, no rename)')
        if people:
            print(f'[dry-run] people: {", ".join(people)} (keyword on every member)')
        print(f'[dry-run] Would scaffold {_rel(record_path, archive_root)}')
        if sidecar is not None:
            print(f'[dry-run] Would delete stub {sidecar.name} (its notes -> ## Notes)')
        return EXIT_CLEAN

    embedded: list[Path] = []
    try:
        for m in ordered:
            err = _run_exiftool_embed_source(m, sid, extra_keywords=per_member_new_people[m] or None)
            if err is not None:
                raise RuntimeError(f'exiftool could not embed SOURCE keyword in {m.name}: {err}')
            embedded.append(m)
        record_dir.mkdir(parents=True, exist_ok=True)
        record_path.write_text(text, encoding='utf-8')
        if sidecar is not None:
            sidecar.unlink()
    except Exception as e:
        try:
            record_path.unlink(missing_ok=True)
        except Exception:
            pass
        for m in reversed(embedded):
            try:
                _run_exiftool_remove_source(m, sid, extra_keywords=per_member_new_people[m] or None)
            except RuntimeError:
                pass
        print(f'ERROR: processing the variation set failed, rolled back: {e}', file=sys.stderr)
        return EXIT_FAILURE

    print(f'Minted {sid}')
    for m in ordered:
        tag = 'primary' if m == primary else _variation_role_copy(m, False)[0]
        print(f'Embedded SOURCE: {sid} in {m.name} ({tag}, not renamed)')
    if people:
        print(f'Tagged people: {", ".join(people)} (on all {len(members)} files)')
    print(f'Scaffolded {_rel(record_path, archive_root)} with {len(members)} files')
    if sidecar is not None:
        print(f'Consumed stub {sidecar.name} (notes -> ## Notes)')
    return EXIT_CLEAN


def _process_variation_set(
    archive_root: Path,
    fha_config: dict,
    members: list[Path],
    *,
    slug: str | None,
    title: str | None,
    source_date: str | None,
    dry_run: bool,
    people: list[str] | None = None,
    real_paths: dict[Path, Path] | None = None,
    source_id: str | None = None,
    report: dict | None = None,
) -> int:
    """Surface a variation set and process it per the human's one/separate/skip choice.

    A single-member set has no ambiguity and is processed straight through. For a
    real set the TOOLING §6 prompt is shown with the batch-type label, then:
    `one` mints a shared S-id over the whole set (process_photo_group); `separate`
    processes each member as its own source; `skip` (also blank or anything
    unrecognized - never mutate on an unclear answer) defers the set.

    `real_paths` (the process_photo_group contract) maps a virtual dry-run
    inbox destination to its real on-disk location; it is threaded into every
    processing branch so preview reads stay against reality.

    `source_id`/`report` (`process_document`'s mint-override/report-back
    pair) are only meaningful for the single-member fast path below - a real
    variation set always mints through the interactive one/separate/skip
    choice, which `fha serve` cannot drive (TOOLING §6's prompt needs a
    human), so no caller ever has a previewed id to pass for those branches.
    """
    members = sorted(members)
    real_paths = real_paths or {}
    if len(members) == 1:
        return process_photo(archive_root, fha_config, members[0],
                             slug=slug, title=title, source_date=source_date,
                             dry_run=dry_run, people=people,
                             real_path=real_paths.get(members[0]),
                             source_id=source_id, report=report)

    primary = select_variation_primary(members, lambda p: parse_media_filename(p.stem))
    letter, desc = _batch_type(members)
    print(f'Found {len(members)} files that appear to be variations of the same photo '
          f'(batch type {letter} - {desc}):')
    for m in members:
        if m == primary:
            label = '[primary]'
        else:
            role, copy = _variation_role_copy(m, False)
            label = f'[role: {role}{f", copy {copy}" if copy else ""}]'
        print(f'  {m.name}  {label}')

    answer = _prompt('Process as ONE source (shared S-id) or separately? '
                     '[one / separate / skip]: ').strip().lower()
    if answer.startswith('one') or answer == 'o':
        return process_photo_group(archive_root, fha_config, members,
                                   slug=slug, title=title, source_date=source_date,
                                   dry_run=dry_run, people=people,
                                   real_paths=real_paths or None)
    if answer.startswith('sep'):
        rc = EXIT_CLEAN
        for m in members:
            rc = max(rc, process_photo(archive_root, fha_config, m,
                                       slug=None, title=None, source_date=source_date,
                                       dry_run=dry_run, people=people,
                                       real_path=real_paths.get(m)))
        return rc
    print('Skipped - deferred to a later session.')
    return EXIT_CLEAN


def _parse_selection(text: str, count: int) -> list[int]:
    """Parse a triage selection ("all", "1,3", "2 4") into 0-based indices.

    Out-of-range or non-numeric tokens are dropped with a warning rather than
    aborting the whole selection - a fat-fingered "1, 9" on a 3-group list still
    processes group 1. Returns indices in input order, de-duplicated.
    """
    text = text.strip().lower()
    if not text:
        return []
    if text == 'all':
        return list(range(count))
    out: list[int] = []
    for token in re.split(r'[,\s]+', text):
        if not token:
            continue
        if not token.isdigit():
            print(f'WARNING: ignoring non-numeric selection {token!r}', file=sys.stderr)
            continue
        idx = int(token) - 1
        if idx < 0 or idx >= count:
            print(f'WARNING: ignoring out-of-range selection {token!r}', file=sys.stderr)
            continue
        if idx not in out:
            out.append(idx)
    return out


def process_folder(
    archive_root: Path,
    fha_config: dict,
    folder: Path,
    *,
    source_date: str | None,
    dry_run: bool,
    people: list[str] | None = None,
) -> int:
    """M7.3: triage a folder's unprocessed photos, then process selected groups.

    The folder's top-level photo files (by extension, excluding sidecars and any
    file already carrying an `_{S-id}` name) are grouped into variation sets by
    the shared `grouping_stem`, ranked by the same evidence signals
    `fha photoindex triage` uses, and listed for selection. The human picks
    groups (numbers, a comma/space list, or `all`); each chosen group is then
    run through the one/separate/skip flow. Non-recursive: a folder *containing*
    a `notes.md` is a bundle (process_bundle), handled before we get here.
    """
    photo_files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and _is_photo_ext(p)
        and not _is_sidecar_path(p) and not _filename_has_source_id(p)
    )
    if not photo_files:
        print(f'No unprocessed photo files found in {folder.name}.')
        return EXIT_CLEAN

    groups: dict[str, list[Path]] = {}
    for p in photo_files:
        groups.setdefault(grouping_stem(parse_media_filename(p.stem)), []).append(p)

    scored = []
    for members in groups.values():
        primary = select_variation_primary(members, lambda p: parse_media_filename(p.stem))
        score, signals = _score_photo_group(members)
        scored.append({'members': sorted(members), 'primary': primary,
                       'score': score, 'signals': signals})
    scored.sort(key=lambda c: (-c['score'], c['primary'].name))

    print(f'{len(scored)} unprocessed photo group(s) in {folder.name}, by triage score:')
    for i, c in enumerate(scored, 1):
        signals = ', '.join(c['signals']) if c['signals'] else 'no signals'
        extra = f' (+{len(c["members"]) - 1} variant)' if len(c['members']) > 1 else ''
        print(f'  {i:>2}. {c["primary"].name}{extra}  score={c["score"]:+d}  [{signals}]')

    answer = _prompt('Select groups to process (numbers, comma-list, or "all"; blank to skip): ')
    chosen = _parse_selection(answer, len(scored))
    if not chosen:
        print('Nothing selected.')
        return EXIT_CLEAN

    rc = EXIT_CLEAN
    for idx in chosen:
        members = scored[idx]['members']
        rc = max(rc, _process_variation_set(
            archive_root, fha_config, members, slug=None, title=None,
            source_date=source_date, dry_run=dry_run, people=people))
    return rc


def process_bundle(
    archive_root: Path,
    fha_config: dict,
    folder: Path,
    *,
    source_date: str | None,
    dry_run: bool,
) -> int:
    """M7.4: dissolve a `notes.md` bundle folder into one source (SPEC §12.1).

    A bundle is a folder of related assets plus a bare `notes.md` stub - e.g. a
    recording and its transcript, or a photo and its document of provenance. One
    S-id covers the whole set. Each asset is filed to its proper root: documents
    are renamed `{slug}[-{role}]_{S-id}.{ext}` and moved under the documents root
    (provenance kept as `original_filename`); photos are moved under the photos
    root **without renaming** and carry the SOURCE: keyword. One record is
    scaffolded from the notes (frontmatter hints → §14 fields, prose → ## Notes),
    its `files:` lists every asset, and the emptied bundle folder is deleted.

    Destination convention: documents land in `documents/{subdir}/` (the same
    plural/`proofs` mapping `_record_subdir` gives the record), photos at the top
    of the photos root. SPEC §12 treats asset subfolders as free projection
    ("folders are projection"), so the exact subfolder is an implementation
    choice, not spec law; what SPEC §12.1 pins down - shared S-id, the `[-role]`
    filename grammar for documents, the SOURCE: keyword for photos, notes →
    ## Notes, and the folder dissolving - is honored exactly. The bundle folder
    itself carries no durable meaning; the minted S-id binds the assets.

    Transactional: every move/rename/keyword-embed registers an undo and the
    record write is last; any failure unwinds everything so a failed dissolution
    leaves the bundle exactly as it was.
    """
    notes_path = folder / 'notes.md'
    sidecar_meta, notes_body = _read_sidecar(notes_path)

    unsupported = sorted(
        p.name for p in folder.iterdir()
        if not p.is_file() and p.name.lower() != 'notes.md'
    )
    if unsupported:
        names = ', '.join(unsupported)
        raise ProcessError(
            f'bundle folder {folder.name} contains unsupported non-file entries: {names}. '
            'Move nested folders out before dissolving the bundle.'
        )

    assets = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.name.lower() != 'notes.md'
    )
    if not assets:
        raise ProcessError(f'bundle folder {folder.name} has a notes.md but no asset files.')

    file_hints = _bundle_file_hints(sidecar_meta)
    missing_hints = sorted(name for name in file_hints if not (folder / name).is_file())
    if missing_hints:
        names = ', '.join(missing_hints)
        raise ProcessError(f'bundle notes contain file hints for missing assets: {names}.')

    final_title = str(sidecar_meta['title']) if sidecar_meta.get('title') \
        else _slugify(folder.name).replace('-', ' ')
    final_slug = _derive_slug(None, final_title, folder)

    photos_root = resolve_path(_PHOTO_DIR, fha_config, archive_root)
    documents_root = resolve_path('documents', fha_config, archive_root)
    asset_kinds = {a: classify_asset(a, fha_config, archive_root) for a in assets}

    source_type = _DEFAULT_DOCUMENT_TYPE
    hinted_type = sidecar_meta.get('source_type')
    if hinted_type:
        hinted_type = str(hinted_type)
        if hinted_type not in SOURCE_TYPES:
            raise ProcessError(
                f"{notes_path.name} hints {format_source_type_error(hinted_type)} "
                'Fix the notes before dissolving the bundle.'
            )
        source_type = hinted_type
    elif asset_kinds and all(kind == 'photo' for kind in asset_kinds.values()):
        source_type = _PHOTO_SOURCE_TYPE

    hinted_primary = [
        a for a in assets
        if file_hints.get(a.name, {}).get('is_primary')
        or file_hints.get(a.name, {}).get('role') == 'primary'
    ]
    if len(hinted_primary) > 1:
        names = ', '.join(a.name for a in hinted_primary)
        raise ProcessError(f'bundle notes mark multiple primary files: {names}.')

    # Primary: honor an explicit stub hint, else prefer the plain photo scan,
    # otherwise the first asset (sorted).
    photo_assets = [a for a in assets if asset_kinds[a] == 'photo']
    if hinted_primary:
        primary = hinted_primary[0]
    elif photo_assets:
        primary = select_variation_primary(photo_assets, lambda p: parse_media_filename(p.stem))
    else:
        primary = assets[0]

    sid = _mint_one_source_id(archive_root)

    # Plan every asset's destination + inventory entry before touching disk, so a
    # collision is caught (and previewed) before any move happens.
    plan = []  # each: {src, kind, dest, embed(bool), entry}
    for asset in assets:
        kind = asset_kinds[asset]
        is_primary = asset == primary
        hint = file_hints.get(asset.name, {})
        if kind == 'photo':
            role, copy = _variation_role_copy(asset, is_primary)
            role = hint.get('role') or role
            copy = hint.get('copy') or copy
            dest = photos_root / asset.name  # photos are never renamed
            entry = {'file': path_to_alias(dest, _PHOTO_DIR, fha_config, archive_root),
                     'role': role, 'is_primary': is_primary}
            if copy:
                entry['copy'] = copy
            plan.append({'src': asset, 'kind': 'photo', 'dest': dest, 'embed': True, 'entry': entry})
        else:
            role = hint.get('role') or (
                'primary' if is_primary else (variant_role(parse_media_filename(asset.stem))
                                              or 'attachment')
            )
            copy = hint.get('copy')
            base = _slugify(asset.stem)
            suffix = '' if role == 'primary' else f'-{_slugify(role)}'
            if copy:
                suffix = f'-{_slugify(copy)}{suffix}'
            new_name = f'{base}{suffix}_{sid}{asset.suffix}'
            dest = documents_root / _record_subdir(source_type) / new_name
            entry = {'file': path_to_alias(dest, 'documents', fha_config, archive_root),
                     'role': role, 'original_filename': asset.name}
            if copy:
                entry['copy'] = copy
            if hint.get('is_primary'):
                entry['is_primary'] = True
            plan.append({'src': asset, 'kind': 'document', 'dest': dest, 'embed': False, 'entry': entry})

    for item in plan:
        if item['dest'].exists():
            raise ProcessError(f'destination already exists: {item["dest"].name}')
        if item['kind'] == 'photo':
            existing, _ = _read_existing_source_keyword(item['src'], dry_run)
            if existing:
                raise ProcessError(
                    f'{item["src"].name} already carries SOURCE: {existing.upper()}; '
                    'the bundle looks partly processed. Attach remaining files with --more '
                    'or remove the stale bundle before processing.'
                )

    record_dir = archive_root / 'sources' / _record_subdir(source_type)
    record_path = record_dir / f'{final_slug}_{sid}.md'
    if record_path.exists():
        raise ProcessError(f'record already exists: {_rel(record_path, archive_root)}')

    text = _scaffold_text(
        sid, final_title, source_type, [item['entry'] for item in plan],
        notes_body=notes_body,
        restricted=source_type == 'dna' or _sidecar_flag(sidecar_meta, 'restricted'),
        citation=_sidecar_str(sidecar_meta, 'citation'),
        repository=_sidecar_str(sidecar_meta, 'repository'),
        source_date=source_date or _sidecar_source_date(sidecar_meta, notes_path.name),
        provenance=_sidecar_str(sidecar_meta, 'provenance'),
    )

    if dry_run:
        print(f'[dry-run] Would mint {sid} for bundle {folder.name} ({len(assets)} files)')
        for item in plan:
            verb = 'move + embed SOURCE in' if item['kind'] == 'photo' else 'rename + file'
            print(f'[dry-run] Would {verb} {item["src"].name} -> '
                  f'{_rel(item["dest"], archive_root)}')
        print(f'[dry-run] Would scaffold {_rel(record_path, archive_root)}')
        print(f'[dry-run] Would delete the dissolved bundle folder {folder.name}')
        return EXIT_CLEAN

    undo: list = []
    embedded: list[tuple[Path, str]] = []
    notes_text = notes_path.read_text(encoding='utf-8')
    try:
        for item in plan:
            item['dest'].parent.mkdir(parents=True, exist_ok=True)
            src, dest = item['src'], item['dest']
            src.rename(dest)
            undo.append(lambda s=src, d=dest: d.rename(s))
            if item['embed']:
                err = _run_exiftool_embed_source(dest, sid)
                if err is not None:
                    raise RuntimeError(f'exiftool could not embed SOURCE keyword in {dest.name}: {err}')
                embedded.append((dest, sid))
        record_dir.mkdir(parents=True, exist_ok=True)
        record_path.write_text(text, encoding='utf-8')
        undo.append(lambda: record_path.unlink(missing_ok=True))

        # Dissolve the now-asset-free folder: remove the notes stub, then rmdir.
        notes_path.unlink()
        undo.append(lambda p=notes_path, text=notes_text: p.write_text(text, encoding='utf-8'))
        folder.rmdir()
    except Exception as e:
        for dest, dsid in reversed(embedded):
            try:
                _run_exiftool_remove_source(dest, dsid)
            except RuntimeError:
                pass
        for fn in reversed(undo):
            try:
                fn()
            except Exception:
                pass
        print(f'ERROR: bundle dissolution failed, rolled back: {e}', file=sys.stderr)
        return EXIT_FAILURE

    print(f'Minted {sid} for bundle {folder.name}')
    for item in plan:
        if item['kind'] == 'photo':
            print(f'Filed {item["src"].name} -> {_rel(item["dest"], archive_root)} '
                  f'(SOURCE: {sid}, not renamed)')
        else:
            print(f'Filed {item["src"].name} -> {_rel(item["dest"], archive_root)}')
    print(f'Scaffolded {_rel(record_path, archive_root)} with {len(assets)} files')
    print(f'Dissolved bundle folder {folder.name}')
    return EXIT_CLEAN


def attach_more(
    archive_root: Path,
    fha_config: dict,
    primary_path: Path,
    more_file: Path,
    role: str,
    copy: str | None,
    *,
    dry_run: bool,
    real_path: Path | None = None,
) -> int:
    """M7.2 `--more`: attach an additional file to an existing source record.

    The positional `primary_path` is an already-processed asset; its S-id comes
    from the embedded SOURCE keyword (photo) or the `_{S-id}` filename suffix
    (document). The attached file is identity-marked the same way its own root
    demands - keyword for a photo (no rename), `-{role}_{S-id}` rename for a
    document - and a `files:` entry is appended to the located record.

    `real_path` is the primary's real on-disk location when a dry-run inbox
    relocation made `primary_path` virtual (the process_photo contract). Only
    the keyword read below uses it; an inbox-staged primary is unprocessed, so
    the preview then refuses with the same "not a processed source" answer the
    live run gives, instead of a spurious read failure.
    """
    # _source_id_of reads the primary's embedded SOURCE keyword via exiftool when
    # it's a photo. The documented dry-run contract is "no exiftool call" - a
    # machine without exiftool on PATH must still get a preview here, so only
    # dry-run degrades that read failure to a warning and stops the preview
    # rather than raising; the live path still needs the real read.
    primary_on_disk = real_path if real_path is not None else primary_path
    if dry_run and classify_asset(primary_path, fha_config, archive_root) == 'photo':
        try:
            raw_sid = _read_source_keyword(primary_on_disk)
        except RuntimeError as e:
            print(f'WARNING: could not read existing keywords from {primary_on_disk.name}: {e}',
                  file=sys.stderr)
            print('[dry-run] Cannot determine the existing S-id without exiftool; '
                  'nothing more to preview.')
            return EXIT_CLEAN
    else:
        raw_sid = _source_id_of(primary_path, fha_config, archive_root)
    if raw_sid is None:
        raise ProcessError(
            f'{primary_path.name} is not a processed source (no S-id in keyword or '
            'filename). Process it first, then attach more files to it.'
        )
    # _source_id_of returns the lowercase body form; normalize to the canonical
    # display form ('S-xxxx') so the keyword we embed / filename we rename to
    # matches the casing the primary already carries.
    sid = fmt_id_display(raw_sid)

    record_path = _find_record_for_sid(archive_root, sid)
    if record_path is None:
        raise ProcessError(f'no source record found for {sid.upper()} under sources/.')

    more_kind = classify_asset(more_file, fha_config, archive_root)

    if more_kind == 'photo':
        photos_root = resolve_path(_PHOTO_DIR, fha_config, archive_root)
        if not _is_under(more_file, photos_root):
            raise ProcessError(
                f'{more_file.name} is not under the configured photos root '
                f'({_rel(photos_root, archive_root)}); file it there before attaching it.'
            )
        if dry_run:
            try:
                more_existing = _read_source_keyword(more_file)
            except RuntimeError as e:
                print(f'WARNING: could not read existing keywords from {more_file.name}: {e}',
                      file=sys.stderr)
                more_existing = None
        else:
            more_existing = _read_source_keyword(more_file)
        if more_existing:
            raise ProcessError(f'{more_file.name} already carries a SOURCE keyword.')
        new_alias = path_to_alias(more_file, _PHOTO_DIR, fha_config, archive_root)
        entry = [f'  - file: {_yaml_inline(new_alias)}', f'    role: {_yaml_inline(role)}']
        if copy:
            entry.append(f'    copy: {_yaml_inline(copy)}')
        if dry_run:
            print(f'[dry-run] Would embed SOURCE: {sid} in {more_file.name} (no rename)')
            print(f'[dry-run] Would add files: entry (role: {role}) to '
                  f'{_rel(record_path, archive_root)}')
            return EXIT_CLEAN
        # Read the record before writing the keyword: if the read fails
        # (permission issue, transient I/O error, non-UTF-8 record), nothing
        # has been written to the photo yet, so there's nothing to roll back.
        try:
            old_text = record_path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as e:
            print(f'ERROR: could not read {_rel(record_path, archive_root)}: {e}',
                  file=sys.stderr)
            return EXIT_FAILURE
        err = _run_exiftool_embed_source(more_file, sid)
        if err is not None:
            print(f'ERROR: exiftool could not embed SOURCE keyword in {more_file.name}: {err}',
                  file=sys.stderr)
            return EXIT_FAILURE
        try:
            new_text = _append_file_entry(old_text, entry)
            record_path.write_text(new_text, encoding='utf-8')
        except Exception as e:
            try:
                rollback_err = _run_exiftool_remove_source(more_file, sid)
            except RuntimeError as rollback_exc:
                rollback_err = str(rollback_exc)
            try:
                record_path.write_text(old_text, encoding='utf-8')
            except Exception:
                pass
            print(f'ERROR: attach failed after keyword write: {e}', file=sys.stderr)
            if rollback_err is None:
                print(f'Rolled back SOURCE: {sid} from {more_file.name}.', file=sys.stderr)
            else:
                print(f'WARNING: could not roll back SOURCE: {sid} from {more_file.name}: '
                      f'{rollback_err}', file=sys.stderr)
            return EXIT_FAILURE
        print(f'Embedded SOURCE: {sid} in {more_file.name} (not renamed)')
        print(f'Added files: entry (role: {role}) to {_rel(record_path, archive_root)}')
        return EXIT_CLEAN

    # Document: rename to share the record's S-id with a -role suffix.
    if _filename_has_source_id(more_file):
        raise ProcessError(f'{more_file.name} already carries an S-id.')
    documents_root = resolve_path('documents', fha_config, archive_root)
    if not _is_under(more_file, documents_root):
        raise ProcessError(
            f'{more_file.name} is not under the configured documents root '
            f'({_rel(documents_root, archive_root)}); file it there before attaching it.'
        )
    base = _slugify(more_file.stem)
    suffix = f'-{_slugify(role)}'
    if copy:
        suffix = f'-{_slugify(copy)}{suffix}'
    new_name = f'{base}{suffix}_{sid}{more_file.suffix}'
    new_path = more_file.with_name(new_name)
    new_alias = path_to_alias(new_path, 'documents', fha_config, archive_root)
    entry = [f'  - file: {_yaml_inline(new_alias)}', f'    role: {_yaml_inline(role)}']
    if copy:
        entry.append(f'    copy: {_yaml_inline(copy)}')
    entry.append(f'    original_filename: {_yaml_inline(more_file.name)}')

    if new_path.exists():
        raise ProcessError(f'destination file already exists: {new_path.name}')

    if dry_run:
        print(f'[dry-run] Would rename {more_file.name} -> {new_name}')
        print(f'[dry-run] Would add files: entry (role: {role}) to '
              f'{_rel(record_path, archive_root)}')
        return EXIT_CLEAN

    undo: list = []
    try:
        old_text = record_path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as e:
        print(f'ERROR: could not read {_rel(record_path, archive_root)}: {e}', file=sys.stderr)
        return EXIT_FAILURE
    try:
        more_file.rename(new_path)
        undo.append(lambda: new_path.rename(more_file))
        new_text = _append_file_entry(old_text, entry)
        record_path.write_text(new_text, encoding='utf-8')
    except Exception as e:
        try:
            record_path.write_text(old_text, encoding='utf-8')
        except Exception:
            pass
        for fn in reversed(undo):
            try:
                fn()
            except Exception:
                pass
        print(f'ERROR: attach failed, rolled back: {e}', file=sys.stderr)
        return EXIT_FAILURE
    print(f'Renamed {more_file.name} -> {new_name}')
    print(f'Added files: entry (role: {role}) to {_rel(record_path, archive_root)}')
    return EXIT_CLEAN


def _source_id_of(file_path: Path, fha_config: dict, archive_root: Path) -> str | None:
    """The S-id naming an already-processed asset: keyword (photo) or filename (doc)."""
    if classify_asset(file_path, fha_config, archive_root) == 'photo':
        return _read_source_keyword(file_path)
    return _filename_has_source_id(file_path)


def _mint_one_source_id(archive_root: Path, source_id: str | None = None) -> str:
    """Mint one fresh S-id through the shared `_lib` ID minter, or reuse an
    already-minted one.

    The ID CLI and process tool both call the same `_lib.mint_ids` helper so
    there is one Crockford alphabet and one collision-checking path, while
    still honoring the rule that tools do not import other tools.

    Called in `--dry-run` too: minting is a read-only tree scan (it reserves
    nothing), so previewing the real S-id a live run would assign is safe.

    `source_id` is NOT a CLI flag - `fha process` always mints its own id,
    same as before. It exists for a caller (`fha serve`'s process.file verb)
    that already ran this SAME function once as a dry run and is now
    re-running it live: reusing that earlier call's minted id here means
    Apply commits exactly the source the human previewed, instead of
    `mint_ids` drawing a fresh random id on the second call (the same
    preview/apply mismatch already fixed for person.new/claim.new - P2 codex
    finding, round 7, PR #30). Still collision-checked against the whole
    tree, same as a freshly-minted id would be - a stale preview (something
    else changed the archive in between) is refused, not silently reused.
    """
    if source_id:
        if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
            raise ProcessError(f'{source_id!r} is not a valid S-id.')
        sid = normalize_id(source_id)
        if sid in scan_ids_in_tree(archive_root):
            raise ProcessError(
                f'{fmt_id_display(sid)} already exists in the archive - the earlier '
                'preview is stale (something else changed since). Preview again, '
                'then apply.')
        return fmt_id_display(sid)
    # mint_ids returns the canonical display form ('S-xxxxxxxxxx', uppercase type
    # prefix) that every on-disk record, filename, and SOURCE keyword uses
    # (SPEC §13, the example archive). Keep it - do not lowercase for writing.
    return mint_ids('S', 1, archive_root)[0]


def _rel(path: Path, archive_root: Path) -> str:
    """Display a path relative to the archive root when possible, else as posix."""
    try:
        return path.resolve().relative_to(archive_root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _prompt(message: str) -> str:
    """Read one line of interactive input (monkeypatched in tests).

    Folder triage and the variation one/separate/skip choice both go through
    this single seam so tests can drive the interactive flows without a TTY, and
    so there is one place that owns reading from the human.
    """
    return input(message)


def _resolve_input_file(
    raw: str,
    archive_root: Path,
    *,
    require_file: bool = False,
    what: str = 'file',
) -> tuple[Path | None, str | None]:
    """Resolve a user-typed asset path: as typed first, then under the archive root.

    The docs tell the user to run commands from the workshop folder (the
    PARENT of the archive), so the path they naturally type is the one they
    see inside the archive - "inbox/scan.jpg" - which misses relative to where
    the command actually runs. Forgiving-input doctrine (AGENTS.md): the path as
    typed always wins when it exists; a relative path that misses is retried
    under the resolved archive root before erroring, so the natural spelling
    works from the workshop folder, from inside the archive, and from anywhere
    --root points home. An absolute path is never retried - it can only mean
    one place.

    `require_file` is for --more, which attaches one regular file, so only a
    file satisfies its lookup; the FILE positional also accepts folders
    (triage and bundle modes) and checks plain existence. `what` names the
    argument in the error ('file' or '--more file').

    Returns (resolved_path, None) on a hit, or (None, message) on a miss; the
    message names every location searched plus the next step, because a bare
    "file not found" leaves a non-technical user nowhere to go.
    """
    def found(p: Path) -> bool:
        return p.is_file() if require_file else p.exists()

    raw_path = Path(raw)
    primary = raw_path.resolve()
    if found(primary):
        return primary, None
    if not raw_path.is_absolute():
        retry = (archive_root / raw_path).resolve()
        # retry == primary when the command already runs from the archive root
        # itself; a second look at the same spot would name one place twice.
        if retry != primary:
            if found(retry):
                return retry, None
            return None, (
                f'{what} not found: {raw}\n'
                f'  Looked here: {primary}\n'
                f'  and inside your archive: {retry}\n'
                '  Try the path as you see it inside your archive folder, '
                'e.g. inbox/scan.jpg.'
            )
    return None, f'{what} not found: {raw}'


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
File a new document or photo into the archive with a permanent ID.

  fha process <file>                        File one document or photo
  fha process <photo> --more <file> ROLE    Attach another file to its source
  fha process <file> --dry-run              Preview, write nothing

Documents are renamed with their new ID (the old name is kept as provenance);
photos are never renamed. This is the deterministic step; drafting claims and
reviewing them come after, through the process-source and review-claims skills."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'process',
        help='Process an original asset into a Source (mint + mark + scaffold)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_run_process)


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('file', metavar='FILE',
                   help='Asset file to process (or the processed asset, with --more); '
                        'a folder triages its photos, or dissolves a notes.md bundle')
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--type', metavar='TYPE', dest='source_type',
                   help=f'Source type for a document (default: {_DEFAULT_DOCUMENT_TYPE}); '
                        'photos are always source_type photo')
    p.add_argument('--title', metavar='TITLE', help='Source title (also seeds the slug)')
    p.add_argument('--slug', metavar='SLUG', help='Explicit filename slug')
    p.add_argument('--date', metavar='DATE', dest='source_date',
                   help="Source date, e.g. 1880, 1880-06-15, or 'about 1880'")
    p.add_argument('--more', nargs=2, metavar=('FILE', 'ROLE'),
                   help='Attach FILE to the existing source as ROLE[:copy]')
    p.add_argument('--people', metavar='P-IDS',
                   help='Comma-separated P-ids of people in this photo - e.g. '
                        '"P-de957bcda1,P-ab3c8f0e12". Writes each as a bare keyword '
                        'in the photo file and populates the source record\'s people: '
                        'list. Photos only; use `fha photoindex tag-person` to tag '
                        'photos already processed.')
    p.add_argument('--dry-run', action='store_true', help='Preview without writing')


def run_process(args: argparse.Namespace) -> Result:
    """Structured entry point for `fha process`; returns a Result.

    `fha process` is an interactive intake flow - it prints its plan and prompts
    the human inline (the `_prompt`/variation-set seams), and the asset
    relocate/rename operations register their own undo callbacks and roll back on
    failure (e.g. `relocate_undo()` above). Per the structured-result contract,
    those prompts, their narration, and the rollback machinery stay exactly where
    they are (a deferred Phase-3 concern). This wraps the flow's exit code into a
    Result (Result == int, so callers/tests comparing against EXIT_* keep
    working); the per-file rename/undo detail is reported inline by the flow.

    `data` is {'status': 'working-copy'} on that one refusal, else
    {'source_id': str | None} - the S-id `_run_process` minted (or reused via
    a `fha serve`-only `args.source_id` override; see `_mint_one_source_id`)
    on the branch that actually ran, or None for a mode that mints nothing
    (--more, a folder/bundle, a real photo variation set - TOOLING §6's
    interactive one/separate/skip prompt, which `fha serve` cannot drive).
    """
    archive_root = resolve_root_arg(args)
    if archive_root is not None and is_working_copy(archive_root):
        # A working-copy refusal is a warning-level Result, not a failure: it
        # succeeded at the only thing it can do here (declining safely and
        # pointing at the main archive), so ok stays True and the exit is clean.
        # data.status='working-copy' is the machine discriminator for headless
        # callers that need to know nothing was filed (TOOLING §13d).
        return Result(
            ok=True,
            exit_code=EXIT_CLEAN,
            data={'status': 'working-copy'},
        ).add(
            'warning',
            'fha process is not available in working-copy mode - '
            'the photo and document files are on the main machine. '
            'Run this command there.',
        )
    exit_code = _run_process(args)
    return Result(
        ok=(exit_code not in (EXIT_ERRORS, EXIT_FAILURE)), exit_code=exit_code,
        data={'source_id': getattr(args, 'result_source_id', None)},
    )


def _run_process(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    if is_working_copy(archive_root):
        print(
            'fha process is not available in working-copy mode - '
            'the photo and document files are on the main machine. '
            'Run this command there.',
            file=sys.stderr,
        )
        return EXIT_CLEAN

    # Resolve to an absolute path before any alias derivation: a relative path
    # run from inside an asset subdirectory (`cd documents/census && fha
    # process deed.pdf`) can't otherwise be related back to the resolved
    # documents/photos roots, and path_to_alias() would fall back to storing
    # the bare relative name instead of the real alias-form path. The lookup
    # is forgiving: a relative path that misses from here is retried under the
    # archive root, so the cheat-sheet spelling ("inbox/scan.jpg" typed from
    # the workshop folder) just works.
    file_path, path_error = _resolve_input_file(args.file, archive_root)
    if file_path is None:
        print(f'ERROR: {path_error}', file=sys.stderr)
        return EXIT_ERRORS

    dry_run = bool(getattr(args, 'dry_run', False))
    # Not a CLI flag (`_add_arguments` never registers it) - only `fha serve`'s
    # process.file verb sets it on the Namespace it builds by hand, threading
    # back an earlier dry-run's minted id so Apply commits exactly the source
    # previewed (see `_mint_one_source_id`). `mint_report` is the matching
    # output side: filled with the id actually used by whichever of the three
    # single-file branches below runs, then copied onto `args` just before
    # returning so `run_process` can read it into `Result.data` without
    # `_run_process` itself changing its plain-int return contract (two
    # existing tests call it directly and expect a bare int back).
    source_id_override = getattr(args, 'source_id', None)
    mint_report: dict = {}
    source_date = getattr(args, 'source_date', None)
    normalized_source_date = normalize_date(source_date) if source_date else None
    if source_date and normalized_source_date is None:
        print(f'ERROR: {format_edtf_error(source_date, field="--date")}', file=sys.stderr)
        return EXIT_ERRORS
    source_date = normalized_source_date
    if source_date and args.more:
        print(
            'ERROR: --date sets the source date while processing a new source. '
            'With --more, edit the existing source record instead.',
            file=sys.stderr,
        )
        return EXIT_ERRORS

    # Parse --people early so a bad P-id fails fast (before any file I/O).
    try:
        people = _parse_people_ids(getattr(args, 'people', None), archive_root)
    except ProcessError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_ERRORS
    if people and args.more:
        print(
            'ERROR: --people is for new sources. With --more, use '
            '`fha photoindex tag-person` to add people to already-processed photos.',
            file=sys.stderr,
        )
        return EXIT_ERRORS

    # Folder modes (BUILD.md M7.3/M7.4): a folder holding a bare notes.md is a
    # source-stub bundle that dissolves into one source; any other folder is a
    # triage target whose unprocessed photos are ranked and offered for
    # selection. --more attaches a single file, so it does not pair with a folder.
    if file_path.is_dir():
        if args.more:
            print('ERROR: --more attaches a single file, not a folder.', file=sys.stderr)
            return EXIT_ERRORS
        if people:
            print('ERROR: --people targets a specific photo. For a folder, process each '
                  'photo individually with --people, or tag after processing with '
                  '`fha photoindex tag-person`.', file=sys.stderr)
            return EXIT_ERRORS
        try:
            if (file_path / 'notes.md').is_file():
                return process_bundle(
                    archive_root, fha_config, file_path,
                    source_date=source_date, dry_run=dry_run,
                )
            return process_folder(
                archive_root, fha_config, file_path,
                source_date=source_date, dry_run=dry_run,
            )
        except ProcessError as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return EXIT_ERRORS
        except RuntimeError as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return EXIT_FAILURE

    if not file_path.is_file():
        print(f'ERROR: not a regular file: {args.file}', file=sys.stderr)
        return EXIT_ERRORS

    sidecar_path: Path | None = None
    try:
        if _is_sidecar_path(file_path):
            companion = _companion_for_sidecar(file_path)
            if companion is None:
                if args.more:
                    print('ERROR: --more attaches a file to a record with an asset; '
                          'a pointer-only source has none.', file=sys.stderr)
                    return EXIT_ERRORS
                rc = process_pointer_only(
                    archive_root, fha_config, file_path,
                    source_type=args.source_type, slug=args.slug, title=args.title,
                    source_date=source_date, dry_run=dry_run,
                    source_id=source_id_override, report=mint_report,
                )
                args.result_source_id = mint_report.get('source_id')
                return rc
            sidecar_path = file_path
            file_path = companion
        else:
            sidecar_path = _find_sidecar(file_path)
        pre_move_path = file_path
        file_path, sidecar_path, relocate_undo = _relocate_from_inbox(
            archive_root, fha_config, file_path, sidecar_path, dry_run=dry_run,
        )
    except ProcessError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_ERRORS

    # A dry-run relocation is virtual: nothing moved, and file_path now names a
    # destination that does not exist yet. Every preview read below (embedded
    # keywords, sidecar hints, variation grouping) must therefore target the
    # file's real pre-move location, or the preview describes a different plan
    # than the live run executes. real_path stays None on a live relocation
    # (the bytes really are at file_path now) and when no relocation happened.
    real_path = pre_move_path if dry_run and file_path != pre_move_path else None

    # The relocation above runs before process_document/process_photo's own
    # validation (e.g. dna's documents/dna/ requirement) and transactions, so
    # any non-clean outcome below - refusal or rollback alike - must undo the
    # move too, or a failed command would still leave the asset filed out of
    # the inbox.
    try:
        if args.more:
            # Same forgiving lookup as the FILE positional, narrowed to regular
            # files - --more attaches exactly one file to an existing source.
            more_file, more_error = _resolve_input_file(
                args.more[0], archive_root, require_file=True, what='--more file')
            role_spec = args.more[1]
            if more_file is None:
                print(f'ERROR: {more_error}', file=sys.stderr)
                rc = EXIT_ERRORS
            else:
                role, _, copy = role_spec.partition(':')
                role = role.strip() or 'attachment'
                copy = copy.strip() or None
                rc = attach_more(archive_root, fha_config, file_path, more_file,
                                  role, copy, dry_run=dry_run, real_path=real_path)
        else:
            kind = classify_asset(file_path, fha_config, archive_root)
            if kind == 'photo':
                # Tier-1 variation detection (M7.3): a single photo may have
                # front/back/crop/copy siblings sitting beside it.
                # _process_variation_set processes a lone photo straight
                # through and only prompts when the directory actually holds
                # a sibling set. On a dry-run relocation the scan runs over
                # the destination directory (the same one live would scan
                # after moving) and file_path stands in for the moved file.
                siblings = _photo_variation_siblings(file_path)
                rc = _process_variation_set(
                    archive_root, fha_config, siblings,
                    slug=args.slug, title=args.title, source_date=source_date,
                    dry_run=dry_run, people=people or None,
                    real_paths={file_path: real_path} if real_path is not None else None,
                    source_id=source_id_override, report=mint_report,
                )
            else:
                if people:
                    print('ERROR: --people is for photo sources only. '
                          'To record people in a document source, edit its `people:` '
                          'field directly after processing.',
                          file=sys.stderr)
                    rc = EXIT_ERRORS
                else:
                    source_type = (args.source_type or _DEFAULT_DOCUMENT_TYPE).strip().lower()
                    if source_type not in SOURCE_TYPES:
                        print(f'ERROR: {format_source_type_error(source_type, where="--type")}', file=sys.stderr)
                        rc = EXIT_ERRORS
                    else:
                        rc = process_document(
                            archive_root, fha_config, file_path,
                            source_type=source_type, slug=args.slug, title=args.title,
                            source_date=source_date, dry_run=dry_run,
                            real_path=real_path,
                            source_id=source_id_override, report=mint_report,
                        )
        if rc != EXIT_CLEAN and relocate_undo is not None:
            relocate_undo()
        args.result_source_id = mint_report.get('source_id')
        return rc
    except ProcessError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        if relocate_undo is not None:
            relocate_undo()
        return EXIT_ERRORS
    except RuntimeError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        if relocate_undo is not None:
            relocate_undo()
        return EXIT_FAILURE


# ── Standalone ────────────────────────────────────────────────────────────────

def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha process',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    args = parser.parse_args(argv)
    return _run_process(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
