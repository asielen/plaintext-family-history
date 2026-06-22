#!/usr/bin/env python3
"""
process.py — fha process: Stage A of the intake pipeline.

  fha process <file> [--type TYPE] [--title "…"] [--slug SLUG]   Process one asset
  fha process <photo> --more <file> ROLE[:copy]                  Attach a file to its source
  fha process <file> --dry-run                                   Preview, write nothing

This is the *deterministic* stage of processing an original into a Source
(SPEC §12.1, TOOLING §6): it mints an S-id, marks the file's identity, and
scaffolds the §14 source record with an empty `## Claims` block. The AI draft
pass (read the file, resolve names/places, draft `suggested` claims) and the
human review pass are Stages B and C — the `process-source` and `review-claims`
skills — not this tool.

Two roots, two identity rules (the spine of SPEC §12.1):

  * Documents root — the file is RENAMED in place to `{slug}_{S-id}.{ext}`;
    its prior name is preserved as `original_filename` provenance. Filename
    only; never content, never location.
  * Photos root — files are NEVER renamed (a rename breaks the Lightroom
    catalog). Identity travels in the embedded `SOURCE: S-xxxx` keyword
    (written via exiftool) plus the record's `files:` inventory.

Detection is by extension and by the photos-root mapping in `fha.yaml`: a file
with a photo extension, or any file living under the resolved photos root, is a
photo; everything else is a document.

Every mutating path is transactional: each filesystem effect registers an undo,
and any failure unwinds them in reverse so an interrupted run leaves no partial
state (AGENTS.md contract). `--dry-run` performs no effect at all.

This milestone (BUILD.md M7.1–M7.2) is single-file / single-photo mode plus
`--more`. Folder triage (M7.3), tier-1 variation grouping (M7.3), and bundle
folder dissolution (M7.4) are later phases; passing a directory here is refused
with a pointer to those phases.
"""

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Slug / type derivation
#    _slugify                  — text → lowercase-hyphenated slug
#    _derive_slug              — pick the slug from --slug / --title / filename stem
#
#  Asset classification
#    _is_under                 — is a path inside a (resolved) root directory?
#    classify_asset            — 'photo' | 'document' for a file + fha.yaml
#    _filename_has_source_id   — does a filename already carry _{S-id}? (refuse)
#
#  exiftool seams (monkeypatched in tests — process never imports photoindex)
#    _run_exiftool_read_keywords — read embedded Keywords/Subject of one file
#    _run_exiftool_embed_source  — write `SOURCE: {S-id}` into one file
#    _run_exiftool_remove_source — remove a just-written `SOURCE: {S-id}`
#    _read_source_keyword        — the S-id embedded in a photo, or None
#
#  Record scaffolding
#    _scaffold_text            — the §14 source-record template as text
#    _find_record_for_sid      — locate sources/**/*_{S-id}.md
#    _append_file_entry        — surgically add a files: list item to a record
#
#  Source-stub sidecar (*.notes.md)
#    _find_sidecar             — the {stem}.notes.md beside an asset, if any
#    _companion_for_sidecar    — resolve direct sidecar input to its asset
#    _read_sidecar             — its hint frontmatter + prose body
#
#  Top-level operations
#    process_document          — M7.1: rename + scaffold (transactional)
#    process_photo             — M7.2: keyword + scaffold (transactional)
#    attach_more               — M7.2: attach a file to an existing source
#
#  CLI
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
    FhaConfigError,
    PHOTO_EXTENSIONS,
    SOURCE_TYPES,
    configure_utf8_stdout,
    fmt_id_display,
    load_fha_yaml,
    mint_ids,
    path_to_alias,
    read_record,
    resolve_path,
    resolve_root_arg,
)

configure_utf8_stdout()

# Default source_type for a document when --type is not given. 'other' is in the
# controlled vocabulary (SPEC §14), so the scaffold lints clean; the human (or
# the AI draft pass) refines it during review.
_DEFAULT_DOCUMENT_TYPE = 'other'

# Photos always scaffold to sources/photos/ with source_type 'photo' regardless
# of any --type — the directory is plural by SPEC convention, the type singular.
_PHOTO_DIR = 'photos'
_PHOTO_SOURCE_TYPE = 'photo'

# A filename already carrying an S-id (e.g. a re-run of a processed document).
_FILENAME_SOURCE_ID_RE = re.compile(r'_(S-[0-9a-hjkmnp-tv-z]{10})$', re.I)

# An embedded `SOURCE: S-xxxx` keyword (the photo identity carrier).
_SOURCE_KEYWORD_RE = re.compile(r'^SOURCE:\s*(S-[0-9a-hjkmnp-tv-z]{10})$', re.I)


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

    A file is a photo if it has a known photo extension OR lives under the
    resolved photos root — the latter catches odd-extension scans filed in the
    photo library. Everything else is a document, including a photo-extension
    file the user deliberately filed under the documents root? No: extension
    wins there, because an embedded-keyword photo must never be renamed even if
    mis-filed. The two rules only ever *add* photos, never reclassify one as a
    document.
    """
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
# photoindex's (tools never import tools — TOOLING §15). Tests monkeypatch these
# two functions to exercise the photo paths without exiftool on PATH.

def _run_exiftool_read_keywords(file_path: Path) -> list[str]:
    """Return the embedded Keywords/Subject of one file (union, order-preserving).

    Used only to detect an already-embedded `SOURCE:` keyword before processing
    a photo. Raises RuntimeError if exiftool is missing — that is an environment
    problem the caller surfaces, distinct from "no keyword present".
    """
    cmd = ['exiftool', '-j', '-Keywords', '-Subject', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError('fha process requires exiftool on PATH for photo files') from e
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


def _run_exiftool_embed_source(file_path: Path, s_id: str) -> str | None:
    """Append `SOURCE: {s_id}` to a photo's Keywords, overwriting in place.

    Uses exiftool's `+=` list-append so existing keywords (DATE:, P-ids) are
    preserved — the only sanctioned write to a photo original (AGENTS.md: photos
    are never renamed, but spec'd keyword writes through fha tools are allowed).
    Returns None on success, the stderr text on a per-file failure; raises
    RuntimeError only when exiftool itself is absent.
    """
    keyword = f'SOURCE: {s_id}'
    cmd = ['exiftool', f'-keywords+={keyword}', '-overwrite_original_in_place', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError('fha process requires exiftool on PATH for photo files') from e
    return None if proc.returncode == 0 else proc.stderr.strip()


def _run_exiftool_remove_source(file_path: Path, s_id: str) -> str | None:
    """Remove a just-added `SOURCE: {s_id}` keyword during rollback.

    The normal photo path writes the keyword before the record, because a
    failed exiftool write must abort without a dangling source record. If the
    later record write fails, this inverse operation restores the photo to its
    pre-run identity state so the command remains transactional.
    """
    keyword = f'SOURCE: {s_id}'
    cmd = ['exiftool', f'-keywords-={keyword}', '-overwrite_original_in_place', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError('fha process requires exiftool on PATH for photo files') from e
    return None if proc.returncode == 0 else proc.stderr.strip()


def _read_source_keyword(file_path: Path) -> str | None:
    """Return the S-id embedded in a photo's `SOURCE:` keyword, or None."""
    for kw in _run_exiftool_read_keywords(file_path):
        m = _SOURCE_KEYWORD_RE.match(kw.strip())
        if m:
            return m.group(1).lower()
    return None


# ── Record scaffolding ────────────────────────────────────────────────────────

def _yaml_inline(value: str) -> str:
    """Render a string as a single-line YAML scalar, quoting only when needed.

    The scaffold is built as text (not `yaml.safe_dump` on the whole record) to
    keep the SPEC §14 field order and the literal ```yaml fence a human reads.
    But free-form values — a `--title`, a filename preserved as
    `original_filename`, a `--more` role, an alias path — can carry
    YAML-significant characters (`: `, a leading `-`, ` #`) that would make the
    frontmatter unparseable, so each is round-tripped through the YAML emitter
    and quoted exactly when the parser needs it. Without this, a title like
    `Letter: Home` silently produces a record `read_record` cannot load.
    """
    rendered = yaml.safe_dump(
        value, default_flow_style=True, allow_unicode=True, width=10 ** 9,
    ).strip()
    if rendered.endswith('...'):
        rendered = rendered[:-3].strip()
    return rendered


def _scaffold_text(
    s_id: str,
    title: str,
    source_type: str,
    file_alias: str,
    *,
    is_photo: bool,
    original_filename: str | None,
    notes_body: str | None,
) -> str:
    """Render a §14 source record as text, ready to write.

    Built by hand (not yaml.safe_dump) so the field order matches the SPEC §14
    template a human reads, and so the `## Claims` fenced block is emitted
    verbatim — `read_record` requires the literal ```yaml fence under the
    heading, and an empty body parses to an empty claims list. The inventory
    records the one file we just processed: `is_primary`/`role: primary` for a
    photo, `original_filename` provenance for a renamed document.
    """
    lines = [
        '---',
        f'id: {s_id}',
        f'title: {_yaml_inline(title)}',
        f'source_type: {source_type}',
        'source_class: original',
        'repository: unknown',
        'citation: >',
        f'  {title}',
        'people: []',
        'files:',
        f'  - file: {_yaml_inline(file_alias)}',
        '    role: primary',
    ]
    if is_photo:
        lines.append('    is_primary: true')
    if original_filename:
        lines.append(f'    original_filename: {_yaml_inline(original_filename)}')
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
        lines.append('*(none yet — drafted in the AI pass)*')
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
        # No inventory yet — create the block immediately before the closing ---.
        insert_at = end
        block = ['files:'] + entry_lines
        return '\n'.join(lines[:insert_at] + block + lines[insert_at:])

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


def _companion_for_sidecar(sidecar: Path) -> Path:
    """Return the single same-stem asset paired with a source-stub sidecar.

    M7.1 documents the convenient entrypoint `fha process sample.notes.md`.
    The sidecar is not the original; it is the notes wrapper around exactly one
    companion asset named `sample.*`. Refusing none-or-many matches prevents the
    tool from minting a source for the wrong file.
    """
    stem = sidecar.name[:-len('.notes.md')]
    candidates = [
        p for p in sidecar.parent.iterdir()
        if p.is_file() and p.name != sidecar.name and p.stem == stem
    ]
    if not candidates:
        raise ProcessError(f'no companion asset found for source stub {sidecar.name}')
    if len(candidates) > 1:
        names = ', '.join(sorted(p.name for p in candidates))
        raise ProcessError(
            f'source stub {sidecar.name} has multiple companion assets: {names}. '
            'Process the intended asset directly.'
        )
    return candidates[0]


def _read_sidecar(sidecar: Path) -> tuple[dict, str]:
    """Parse a stub sidecar into (hint frontmatter, prose body).

    The stub's optional frontmatter seeds record fields (title/source_type/
    repository hints); its prose body flows into the record's `## Notes`, since
    those notes are the starting point a reviewer reads (never accepted facts).
    """
    rec = read_record(sidecar)
    meta = rec.get('meta') or {}
    # Strip the frontmatter off the body; keep the prose the human wrote.
    body = (rec.get('body') or '').strip()
    return meta, body


# ── Top-level operations ──────────────────────────────────────────────────────

class ProcessError(Exception):
    """A user-facing processing failure (refusal or bad input)."""


def process_document(
    archive_root: Path,
    fha_config: dict,
    file_path: Path,
    *,
    source_type: str,
    slug: str | None,
    title: str | None,
    dry_run: bool,
) -> int:
    """M7.1: rename a documents-root original and scaffold its source record.

    Transactional: the rename and the record write each register an undo, and
    any exception unwinds them in reverse, so an interrupted run leaves neither
    a renamed-but-unrecorded file nor a record pointing at a vanished asset.
    """
    if existing := _filename_has_source_id(file_path):
        raise ProcessError(
            f'{file_path.name} already carries an S-id ({existing.upper()}); '
            'it looks already processed. Refusing to mint a second ID.'
        )

    final_title = title or _slugify(file_path.stem).replace('-', ' ')
    sidecar = _find_sidecar(file_path)
    notes_body = None
    sidecar_meta: dict = {}
    if sidecar is not None:
        sidecar_meta, notes_body = _read_sidecar(sidecar)
        # A stub may hint a better title / source_type than the bare filename.
        if title is None and sidecar_meta.get('title'):
            final_title = str(sidecar_meta['title'])
        if source_type == _DEFAULT_DOCUMENT_TYPE and sidecar_meta.get('source_type'):
            hinted = str(sidecar_meta['source_type'])
            if hinted in SOURCE_TYPES:
                source_type = hinted

    sid = _mint_one_source_id(archive_root)
    final_slug = _derive_slug(slug, final_title if title is None else title, file_path)
    ext = file_path.suffix
    new_name = f'{final_slug}_{sid}{ext}'
    new_path = file_path.with_name(new_name)

    record_dir = archive_root / 'sources' / source_type
    record_path = record_dir / f'{final_slug}_{sid}.md'
    file_alias = path_to_alias(new_path, 'documents', fha_config, archive_root)

    if new_path.exists():
        raise ProcessError(f'destination file already exists: {new_path.name}')
    if record_path.exists():
        raise ProcessError(f'record already exists: {_rel(record_path, archive_root)}')

    text = _scaffold_text(
        sid, final_title, source_type, file_alias,
        is_photo=False, original_filename=file_path.name, notes_body=notes_body,
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


def process_photo(
    archive_root: Path,
    fha_config: dict,
    file_path: Path,
    *,
    slug: str | None,
    title: str | None,
    dry_run: bool,
) -> int:
    """M7.2: embed a SOURCE keyword in a photo and scaffold its source record.

    Photos are never renamed. The keyword write is the risky step and happens
    first: if exiftool fails, nothing is scaffolded (TOOLING §6 "abort on
    failure, do not scaffold"). If the scaffold then fails, the just-written
    keyword is removed so the photo is not left half-processed.
    """
    existing = _read_source_keyword(file_path)
    if existing:
        raise ProcessError(
            f'{file_path.name} already carries SOURCE: {existing.upper()}; '
            'it looks already processed. Refusing to mint a second ID.'
        )

    final_title = title or _slugify(file_path.stem).replace('-', ' ')
    sidecar = _find_sidecar(file_path)
    notes_body = None
    if sidecar is not None:
        sidecar_meta, notes_body = _read_sidecar(sidecar)
        if title is None and sidecar_meta.get('title'):
            final_title = str(sidecar_meta['title'])

    sid = _mint_one_source_id(archive_root)
    final_slug = _derive_slug(slug, final_title if title is None else title, file_path)
    record_dir = archive_root / 'sources' / _PHOTO_DIR
    record_path = record_dir / f'{final_slug}_{sid}.md'
    file_alias = path_to_alias(file_path, _PHOTO_DIR, fha_config, archive_root)

    if record_path.exists():
        raise ProcessError(f'record already exists: {_rel(record_path, archive_root)}')

    text = _scaffold_text(
        sid, final_title, _PHOTO_SOURCE_TYPE, file_alias,
        is_photo=True, original_filename=None, notes_body=notes_body,
    )

    if dry_run:
        print(f'[dry-run] Would mint {sid}')
        print(f'[dry-run] Would embed SOURCE: {sid} in {file_path.name} (no rename)')
        print(f'[dry-run] Would scaffold {_rel(record_path, archive_root)}')
        if sidecar is not None:
            print(f'[dry-run] Would delete stub {sidecar.name} (its notes -> ## Notes)')
        return EXIT_CLEAN

    err = _run_exiftool_embed_source(file_path, sid)
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
            rollback_err = _run_exiftool_remove_source(file_path, sid)
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
    print(f'Scaffolded {_rel(record_path, archive_root)}')
    if sidecar is not None:
        print(f'Consumed stub {sidecar.name} (notes -> ## Notes)')
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
) -> int:
    """M7.2 `--more`: attach an additional file to an existing source record.

    The positional `primary_path` is an already-processed asset; its S-id comes
    from the embedded SOURCE keyword (photo) or the `_{S-id}` filename suffix
    (document). The attached file is identity-marked the same way its own root
    demands — keyword for a photo (no rename), `-{role}_{S-id}` rename for a
    document — and a `files:` entry is appended to the located record.
    """
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
        if _read_source_keyword(more_file):
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
        err = _run_exiftool_embed_source(more_file, sid)
        if err is not None:
            print(f'ERROR: exiftool could not embed SOURCE keyword in {more_file.name}: {err}',
                  file=sys.stderr)
            return EXIT_FAILURE
        old_text = record_path.read_text(encoding='utf-8')
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
    old_text = record_path.read_text(encoding='utf-8')
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


def _mint_one_source_id(archive_root: Path) -> str:
    """Mint one fresh S-id through the shared `_lib` ID minter.

    The ID CLI and process tool both call the same `_lib.mint_ids` helper so
    there is one Crockford alphabet and one collision-checking path, while
    still honoring the rule that tools do not import other tools.

    Called in `--dry-run` too: minting is a read-only tree scan (it reserves
    nothing), so previewing the real S-id a live run would assign is safe.
    """
    # mint_ids returns the canonical display form ('S-xxxxxxxxxx', uppercase type
    # prefix) that every on-disk record, filename, and SOURCE keyword uses
    # (SPEC §13, the example archive). Keep it — do not lowercase for writing.
    return mint_ids('S', 1, archive_root)[0]


def _rel(path: Path, archive_root: Path) -> str:
    """Display a path relative to the archive root when possible, else as posix."""
    try:
        return path.resolve().relative_to(archive_root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


# ── CLI ───────────────────────────────────────────────────────────────────────

def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'process',
        help='Process an original asset into a Source (mint + mark + scaffold)',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_run_process)


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('file', metavar='FILE', help='Asset file to process (or the processed asset, with --more)')
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--type', metavar='TYPE', dest='source_type',
                   help=f'Source type for a document (default: {_DEFAULT_DOCUMENT_TYPE}); '
                        'photos are always source_type photo')
    p.add_argument('--title', metavar='TITLE', help='Source title (also seeds the slug)')
    p.add_argument('--slug', metavar='SLUG', help='Explicit filename slug')
    p.add_argument('--more', nargs=2, metavar=('FILE', 'ROLE'),
                   help='Attach FILE to the existing source as ROLE[:copy]')
    p.add_argument('--dry-run', action='store_true', help='Preview without writing')


def _run_process(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    file_path = Path(args.file)
    if not file_path.exists():
        print(f'ERROR: file not found: {args.file}', file=sys.stderr)
        return EXIT_ERRORS
    if file_path.is_dir():
        print('ERROR: folder mode (triage, variation grouping, bundle dissolution) is not '
              'in this milestone — BUILD.md M7.3/M7.4. Pass a single file.', file=sys.stderr)
        return EXIT_ERRORS
    if not file_path.is_file():
        print(f'ERROR: not a regular file: {args.file}', file=sys.stderr)
        return EXIT_ERRORS

    try:
        if _is_sidecar_path(file_path):
            file_path = _companion_for_sidecar(file_path)
    except ProcessError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_ERRORS

    dry_run = bool(getattr(args, 'dry_run', False))

    try:
        if args.more:
            more_file = Path(args.more[0])
            role_spec = args.more[1]
            if not more_file.is_file():
                print(f'ERROR: --more file not found: {args.more[0]}', file=sys.stderr)
                return EXIT_ERRORS
            role, _, copy = role_spec.partition(':')
            role = role.strip() or 'attachment'
            copy = copy.strip() or None
            return attach_more(archive_root, fha_config, file_path, more_file,
                               role, copy, dry_run=dry_run)

        kind = classify_asset(file_path, fha_config, archive_root)
        if kind == 'photo':
            return process_photo(
                archive_root, fha_config, file_path,
                slug=args.slug, title=args.title, dry_run=dry_run,
            )
        source_type = (args.source_type or _DEFAULT_DOCUMENT_TYPE).strip().lower()
        if source_type not in SOURCE_TYPES:
            print(f'ERROR: unknown source type {source_type!r}.', file=sys.stderr)
            return EXIT_ERRORS
        return process_document(
            archive_root, fha_config, file_path,
            source_type=source_type, slug=args.slug, title=args.title, dry_run=dry_run,
        )
    except ProcessError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_ERRORS
    except RuntimeError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE


# ── Standalone ────────────────────────────────────────────────────────────────

def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha process',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    args = parser.parse_args(argv)
    return _run_process(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
