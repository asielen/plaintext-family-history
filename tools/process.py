#!/usr/bin/env python3
"""
process.py - fha process: Stage A of the intake pipeline.

  fha process <file> [--type TYPE] [--title "…"] [--date DATE] [--slug SLUG]
                                                                 Process one asset
  fha process <photo> --more <file> ROLE[:copy]                  Attach a file to its source
  fha process refile <S-id> --to photos|documents [--type TYPE] [--dest SUB]
                                                                 Move a filed file across roots
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

Which root a new file belongs in is decided by `classify_asset`, in three
steps: where the file already lives (a file under either root stays under it -
`fha process` never moves a filed original across the roots), then the stated
`--type` (any type but `photo` names a record, and records live in the
documents root - a scanned census is a census whether it arrives as a `.pdf`
or a `.jpg`), then the extension. Before issue #59 only the last of those was
consulted for a new file, so `--type census` on a `.jpg` was accepted and
silently discarded and the sheet went into the family photo library.

**Refile** (`fha process refile <S-id> --to photos|documents`) is the
owner-approved carve-out to SPEC 12.1's one-sanctioned-move rule: the
CROSS-ROOT correction for a filing decision that turned out wrong. It moves
one of a source's files to the other root, re-establishes the destination
root's identity carriers (the last-chance rename + SOURCE: keyword going into
photos; the 13-grammar rename going into documents), and updates the record -
value-exact inventory rewrite plus a dated Notes provenance line - in one
transaction. `--type` carries the source's TYPE across in that same
transaction: `source_type:` is rewritten and the record file itself moves to
`sources/{type}/`, because a scan leaving the photo library is not a family
photo any more and leaving it typed `photo` in `sources/photos/` is a
hand-edit `fha lint` never flags. Within-root moves are NOT refile's business
(free + healed by `fha reconcile`). See `process_refile` for the full
contract.

Every mutating path is transactional: each filesystem effect registers an undo,
and any failure unwinds them in reverse so an interrupted run leaves no partial
state (AGENTS.md contract).  The keyword write goes one step further: before
exiftool touches a photo for the first time, `_lib.OriginalBackup` puts one
pristine copy of it under the `originals_backup:` folder (TOOLING §13f), and a
copy that fails refuses the write rather than proceeding without one.

`--dry-run` performs no effect at all - which means
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
#    classify_asset            - 'photo' | 'document' for a file + fha.yaml + --type
#    _filename_has_source_id   - does a filename already carry _{S-id}? (refuse)
#
#  exiftool seams (monkeypatched in tests - process never imports photoindex)
#    _run_exiftool_read_keywords - read embedded Keywords/Subject of one file
#    _clear_read_only            - make a file writable before exiftool touches it (#110)
#    _cleanup_exiftool_tmp       - delete a stray <file>_exiftool_tmp left by a failed write (#110)
#    _format_exiftool_tmp_error  - name the real fix if a tmp-file collision still surfaces (#110)
#    _run_exiftool_embed_source  - write `SOURCE: {S-id}` into one file
#    _run_exiftool_remove_source - remove a just-written `SOURCE: {S-id}`
#    _open_backup                - the run's safety-copy policy, announced (TOOLING §13f)
#    _flush_backup_messages      - print its notices (warnings to stderr)
#    _read_source_keyword        - the FIRST S-id embedded in a photo, or None
#    _read_all_source_keywords   - every distinct S-id embedded in a photo (refile identity check)
#    ExiftoolUnavailableError    - exiftool itself missing, distinct from a per-file read failure
#
#  Record scaffolding
#    _scaffold_text            - the §14 source-record template as text
#    _render_scaffold_file_entry - one files: list item (file/role/copy/…) as lines
#    _find_record_for_sid      - locate sources/**/*_{S-id}.md
#    _append_file_entry        - surgically add a files: list item to a record
#
#  Source-stub sidecar (*.notes.md) + bundle notes.md
#    _find_sidecar             - the {stem}.notes.md beside an asset, if any
#    _find_back_sibling        - an unambiguous {base_id}-back sibling beside a plain
#                                 scan, pulled in automatically rather than left behind
#                                 (#113); raises on more than one candidate (#145 finding 4)
#    _companion_for_sidecar    - resolve direct sidecar input to its asset
#    _read_sidecar             - its hint frontmatter + prose body
#    _bundle_file_hints        - bundle notes per-file role/copy/primary hints
#
#  Variation detection (M7.3, shared grammar via _lib)
#    _photo_variation_siblings - photos in a dir sharing one base_id
#    _variation_role_copy      - (role, copy) annotation for a grouped member
#    _batch_type               - A–D label for a multi-image set (informational)
#    _photo_meta_from_row      - one exiftool row -> triage signals (date resolved via _lib)
#    _run_exiftool_read_meta   - caption/date/keyword signals for triage scoring
#    _score_photo_group        - TOOLING §15b evidence score (mirrors photoindex)
#
#  Top-level operations
#    process_document          - M7.1: rename + scaffold (transactional)
#    process_photo             - M7.2: keyword + scaffold (transactional)
#    _photo_root_type_note     - the refile next step for a typed photos-root file
#    process_photo_group       - M7.3: one S-id over a variation set (transactional)
#    process_folder            - M7.3: triage a folder, process selected groups
#    process_bundle            - M7.4: dissolve a notes.md bundle into one source
#    attach_more               - M7.2: relocate --more's file out of inbox/ if needed (#111),
#                                 then hand off to the engine below
#    _attach_more_engine       - the attach logic proper: identity-mark + files: entry
#
#  Refile (the sanctioned cross-root correction move)
#    _stdin_is_interactive     - tty seam for the photo-catalog confirm (tests patch it)
#    _move_file                - rename with a copy+delete fallback across drives
#    _rewrite_file_line        - value-exact files: line rewrite (mirrors reconcile.py)
#    _rewrite_source_type_line - value-exact source_type: rewrite (the --type carry)
#    _file_line_count          - refuse-rather-than-corrupt shape guard
#    _validate_dest_subpath    - --dest containment (no absolutes, no '..', inside the root)
#    _refile_pick_entry        - choose which files: entry moves (never guesses)
#    _photo_library_name       - the last-chance rename: restore the pre-processing name
#    process_refile            - the engine: move + rename + keyword + record update
#
#  CLI
#    _prompt                   - interactive input seam (monkeypatched in tests)
#    _resolve_input_file       - forgiving FILE/--more lookup: as typed, then under the archive root
#    build_process_refile_parser / _cmd_refile - the refile subcommand (fha.py intercepts)
#    register / _run_process / _standalone_main
#
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import datetime
import errno
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    Result,
    PHOTO_EXTENSIONS,
    SOURCE_PURPOSE_BLOCK,
    SOURCE_TYPES,
    BackupRefused,
    FhaConfigError,
    OriginalBackup,
    ParsedName,
    append_paragraph_to_section,
    claims_edit_problem,
    configure_utf8_stdout,
    edtf_confidence,
    find_source_record_path,
    format_edtf_error,
    format_exiftool_error,
    format_source_type_error,
    fmt_id_display,
    frontmatter_fence_span,
    grouping_stem,
    id_type_of,
    is_valid_id,
    load_fha_yaml,
    mint_ids,
    normalize_date,
    normalize_id,
    parse_frontmatter_strict,
    parse_media_filename,
    path_to_alias,
    read_record,
    read_text_exact,
    reapply_newline,
    resolve_path,
    resolve_photo_edtf,
    resolve_root_arg,
    scan_ids_in_tree,
    scan_person_record_ids,
    source_type_list,
    select_variation_primary,
    is_working_copy,
    variant_role,
    write_text_exact_atomic,
    yaml_inline,
)

configure_utf8_stdout()

# Default source_type for a document when --type is not given. 'other' is in the
# controlled vocabulary (SPEC §14), so the scaffold lints clean; the human (or
# the AI draft pass) refines it during review.
_DEFAULT_DOCUMENT_TYPE = 'other'

# The photo root's alias and the source_type that names a family photo. The
# directory is plural by SPEC convention, the type singular (see _record_subdir).
_PHOTO_DIR = 'photos'
_PHOTO_SOURCE_TYPE = 'photo'

# #109: where `_relocate_from_inbox` parks the ORIGINAL inbox file after
# filing a copy into its documents/photos root - never deleted, so a human
# processing a large batch can still spot-check the originals afterward.
# Mirrors `fha capture --ingest`'s own park-don't-delete precedent
# (`_INGESTED_DIRNAME` in capture.py), just without the dot-prefix: this one
# sits inside `inbox/` itself (SPEC-visible, not a tool-internal holding pen)
# and the issue's own acceptance test names it literally, `inbox/processed/`.
_INBOX_PROCESSED_DIRNAME = 'processed'

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
    """True if `path` is inside `root` (both resolved); False on unrelated trees.

    Also False - rather than a raised `RuntimeError` - when either side's
    `.resolve()` hits a symlink loop (issue #170 finding 2, round-3 audit):
    `Path.resolve()` raises `RuntimeError` for that case, distinct from the
    `ValueError`/`OSError` an ordinary unresolvable path raises, and every one
    of this function's ~11 call sites already treats a plain False as "not
    verifiably contained" and produces its own clean refusal (or a silent
    skip) - none needs to tell a symlink loop apart from an everyday
    containment failure. Folding RuntimeError in here, once, turns what used
    to be a raw traceback out of `process_refile`'s containment guard (added
    in PR #163's own P1 fix, which only caught ValueError/OSError) into that
    same clean refusal at every call site, live and `--dry-run` alike -
    instead of hand-wrapping each call individually.
    """
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


# Windows has no POSIX `ELOOP`: `errno.ELOOP` there is a repurposed Winsock
# code (WSAELOOP) that a real filesystem loop never raises. What Windows
# actually raises once path resolution exceeds its internal reparse-point
# traversal cap - true whether the reparse chain is a genuine cycle or just
# deep - is a bare `OSError` carrying this `winerror`: `ERROR_CANT_RESOLVE_
# FILENAME`, "The name of the file cannot be resolved by the system".
# Verified empirically against a real, unprivileged, on-disk NTFS junction
# loop (see `tests/test_process.py`'s `_build_real_symlink_loop`) - a real
# symlink loop hits the identical failure, since the OS's traversal cap is
# not specific to symlinks vs. junctions (issue #170 finding 2, round-9
# audit, post-merge Codex review of #176).
_WINDOWS_SYMLINK_LOOP_WINERROR = 1921


def _resolve_hits_symlink_loop(path: Path) -> bool:
    """True if resolving `path` alone hits a symlink loop specifically.

    `_is_under`'s widened except tuple folds a symlink loop into the same
    `False` as an everyday containment miss for all ~11 of its callers
    uniformly - right for most of them, since a plain refusal is enough
    (Codex review, round-5 audit). `process_refile`'s own containment
    refusal was the first exception: its message already NAMES a symlink
    loop as a possible cause alongside a `..` segment or doubled slash, but
    then gives one universal remedy - "fix the files: entry by hand" - that
    is actively wrong when the entry is correct and an on-disk symlink is
    what actually needs repairing. This is the targeted re-check that call
    site uses to tell the two apart and give the right remedy for each, the
    same way `packet.py`'s `_resolve_source_files` already does for its own
    identical containment check.

    `_is_under_strict`/`_require_contained` below reuse this same primitive
    to extend that same distinction to every OTHER user-facing containment
    check in this file (issue #170 finding 2, extended - Codex review
    round-8 audit): `classify_asset`, `process_document`, `process_photo`,
    `process_photo_group`, and `attach_more` all had the identical gap -
    a symlink loop on a configured root silently became a generic "not
    under the root; file it there" refusal, which cannot be acted on when
    the file may already be exactly where it belongs and the real problem
    is a corrupted symlink.

    Detection needs `strict=True` and cannot use the module's usual
    non-strict `.resolve()` (post-merge Codex review, round-9 audit, issue
    #170 finding 2 follow-up): on Python < 3.13, pathlib's own pure-Python
    resolver detects a repeated component itself and raises `RuntimeError`
    for a loop in strict AND non-strict mode alike, which is what this
    function originally (and still) catches - but 3.13 rewrote `resolve()`
    to delegate straight to `os.path.realpath()`, and that implementation's
    NON-strict mode silently swallows any `OSError` a loop raises and
    returns a best-effort (frequently still-unresolved) path instead of
    raising anything at all. On 3.13+ a real loop therefore no longer
    raises `RuntimeError` - non-strict `.resolve()` returns cleanly, and
    this function would wrongly report "not a loop" for a genuine one
    (verified empirically on 3.14: a real on-disk loop's non-strict
    `.resolve()` returns the chain unresolved rather than raising). Only
    `strict=True` still forces the underlying loop failure to surface as an
    exception on every supported version.
    """
    try:
        path.resolve(strict=True)
        return False
    except RuntimeError:
        return True
    except FileNotFoundError:
        # `resolve(strict=True)` raises this for an ordinary missing target
        # too (nothing to do with a loop) - `os.path.realpath` maps a
        # resolvable-but-absent path to the same exception a loop's own
        # "file not found at the resolved name" case can produce, so this
        # must be checked before the broader `OSError` branch below, not
        # folded into it.
        return False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return True
        if os.name == 'nt' and getattr(exc, 'winerror', None) == _WINDOWS_SYMLINK_LOOP_WINERROR:
            return True
        return False
    except ValueError:
        return False


def _containment_loop_error(offending: Path, root_label: str) -> ProcessError:
    """The message for a containment check that could not be verified
    because resolving the candidate path or the root itself hit a symlink
    loop - naming the loop as the thing to fix, not a genuine "elsewhere"
    answer (issue #170 finding 2, extended - Codex review round-8 audit).
    Shared by `_is_under_strict` (and, through it, `_require_contained`) so
    every call site below phrases this the same way `process_refile`'s own
    hand-written version of the same distinction already does."""
    return ProcessError(
        f'{offending.name} could not be checked against the configured '
        f'{root_label} - this looks like a symlink loop, not a misfiled '
        'asset. Find and fix (or remove) the broken symlink, then retry.'
    )


def _is_under_strict(path: Path, root: Path, *, root_label: str) -> bool:
    """Like `_is_under`, but raises `ProcessError` instead of returning
    `False` when a symlink loop - not genuine non-containment - is what
    stopped resolution (issue #170 finding 2, extended - Codex review
    round-8 audit).

    `classify_asset` calls this directly: a plain `_is_under` False there
    is not just "not verifiably contained", it feeds a fallback decision
    (stated `source_type`, then file extension) that can misfile a document
    into the photo library, or vice versa, permanently - silently treating
    "could not verify because of a broken symlink" the same as "genuinely
    elsewhere" is not safe there the way it is for `_is_under`'s other,
    purely refuse-or-continue callers.
    """
    if _is_under(path, root):
        return True
    if _resolve_hits_symlink_loop(path) or _resolve_hits_symlink_loop(root):
        raise _containment_loop_error(path, root_label)
    return False


def _require_contained(path: Path, root: Path, *, root_label: str, message: str) -> None:
    """Raise `ProcessError` unless `path` is verifiably under `root`.

    `message` is the caller's own genuine "not under root" wording - each
    call site phrases the remedy a little differently ('before processing',
    'before attaching it', 'file the whole set there', ...) - this only
    decides WHICH message is right: a symlink loop on either side raises
    `_containment_loop_error` instead (via `_is_under_strict`), since that
    is a different, unrelated failure with a different fix (issue #170
    finding 2, extended - Codex review round-8 audit).
    """
    if _is_under_strict(path, root, root_label=root_label):
        return
    raise ProcessError(message)


def classify_asset(file_path: Path, fha_config: dict, archive_root: Path,
                   *, source_type: str | None = None) -> str:
    """Return 'photo' or 'document' for an asset file (TOOLING §6).

    Read as three questions in order: where does the file already live, what
    did the human say it is, and failing both, what does it look like.

    **Where it lives wins**, because that is the one answer this tool may not
    argue with. A file under the documents root is a document even with a
    photo extension (a scanned record saved as `.jpg`) - the documents-root
    identity rule, rename plus provenance, applies to whatever was
    deliberately filed there. A file under the photos root is a photo, because
    `fha process` may never carry a file out of the photo library at all
    (SPEC §12.1); only `fha process refile` may, and only with the human's yes.

    **Then a stated `source_type`** - the human's `--type`, or a source stub's
    `source_type:` hint - for a file that is not yet in either root (the inbox
    case, which is how new material actually arrives). A stated type other
    than `photo` names a RECORD: SPEC §14's vocabulary is a vocabulary of
    records (census, vital-record, land-record, probate), and records are what
    SPEC §12 puts in the documents root, "scans, clippings, recordings,
    transcripts". A scanned census sheet is a census whether it arrives as a
    `.pdf` or a `.jpg`, so the stated type beats the extension. Before this,
    the extension decided alone and `--type census` on a `.jpg` was accepted
    and silently discarded - the sheet was filed into the family photo library
    and typed `photo` (issue #59). A flag accepted and then ignored is the
    confident-wrong-answer class this project treats as the cardinal sin, so
    the flag wins.

    A stated `photo` does NOT push a file the other way. The photos root is an
    external library another program owns (Lightroom and its like), and
    nothing but a real photograph belongs in it; a `.pdf` labelled
    `--type photo` therefore falls through to the extension test below and is
    filed as a document, with its record still typed as the human asked.

    **Then the file itself:** a known photo extension is a photo, everything
    else a document.

    A symlink loop on either configured root is not treated as "genuinely
    not under this root" - `_is_under_strict` raises instead of falling
    through, because a silent guess here (stated `source_type`, then
    extension) could misfile a document into the photo library, or a photo
    into documents, with no way back short of `fha process refile` (issue
    #170 finding 2, extended - Codex review round-8 audit).
    """
    documents_root = resolve_path('documents', fha_config, archive_root)
    if _is_under_strict(file_path, documents_root, root_label='documents root'):
        return 'document'
    photos_root = resolve_path(_PHOTO_DIR, fha_config, archive_root)
    if _is_under_strict(file_path, photos_root, root_label='photos root'):
        return 'photo'
    stated = str(source_type).strip().lower() if source_type else ''
    if stated and stated != _PHOTO_SOURCE_TYPE:
        return 'document'
    if file_path.suffix.lower() in PHOTO_EXTENSIONS:
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

class ExiftoolUnavailableError(RuntimeError):
    """exiftool itself could not be run at all (missing from PATH).

    Distinct from a per-file read/parse failure (a nonzero exit reading THIS
    file, or invalid JSON on its stdout) - both of those mean exiftool IS
    present and runnable, it just did not like this particular file. A caller
    that soft-fails when the environment lacks exiftool (the refile identity
    check's weaker containment-only fallback) must catch narrowly, only this,
    rather than blanket `RuntimeError` - otherwise a photo that is unreadable
    for some OTHER reason but still carries a conflicting embedded source id
    would silently pass the identity check instead of refusing (P1 audit
    finding on `fha process refile`).
    """


def _run_exiftool_read_keywords(file_path: Path) -> list[str]:
    """Return the embedded Keywords/Subject of one file (union, order-preserving).

    Used only to detect an already-embedded `SOURCE:` keyword before processing
    a photo. Raises `ExiftoolUnavailableError` (a `RuntimeError` subclass) if
    exiftool itself is missing - an environment problem the caller surfaces,
    distinct from "no keyword present"; raises plain `RuntimeError` for every
    other read failure (nonzero exit, invalid JSON), since exiftool ran but
    this specific read did not succeed.
    """
    cmd = ['exiftool', '-j', '-Keywords', '-Subject', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise ExiftoolUnavailableError(format_exiftool_error('fha process')) from e
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


# The literal suffix exiftool's `-overwrite_original_in_place` appends to the
# temp file it writes through before swapping it in for the real one - no dot,
# no configurability, verified against the issue's own reproduction (#110).
_EXIFTOOL_TMP_SUFFIX = '_exiftool_tmp'
_EXIFTOOL_TMP_EXISTS_RE = re.compile(r'temporary file already exists', re.I)


def _clear_read_only(file_path: Path) -> None:
    """Make `file_path` writable before exiftool's own write, best-effort (#110).

    `_relocate_from_inbox` is a plain move/rename, which preserves whatever
    permission bits a file already had - an inbox-staged original that
    arrived read-only (common: copied off read-only media, or a personal
    "protect this" habit) reaches the embed step still locked, and exiftool
    then fails with a write-permission error that names no fix. Nothing in
    this archive's design requires a filed original to stay read-only for
    fha's own subsequent keyword write, so this clears the bit unconditionally
    before every embed/remove attempt. Best-effort and silent on failure: if
    clearing it does not work either, exiftool's own error still surfaces
    exactly as it did before this existed - this is not a new failure mode,
    only a chance to avoid the old one.
    """
    try:
        mode = file_path.stat().st_mode
        file_path.chmod(mode | stat.S_IWRITE)
    except OSError:
        pass


def _cleanup_exiftool_tmp(file_path: Path) -> None:
    """Delete a stray `<file>_exiftool_tmp` left beside `file_path` (#110).

    exiftool's `-overwrite_original_in_place` writes through a temp file
    named literally `<path>_exiftool_tmp` and renames it over the original
    only on success; a write that fails part-way (e.g. the destination was
    read-only) leaves that temp file behind. A later attempt against the same
    destination then trips exiftool's OWN "temporary file already exists"
    guard - a second, different-looking error that names the wrong cause and
    masks that the real one (e.g. read-only) may already be fixed. Called
    both before an embed/remove attempt (clears a stale file left by an
    EARLIER failed run) and after one fails (clears whatever THIS attempt
    left), so a retry only ever meets the real, current cause. Best-effort:
    the rare case a locked temp file cannot be deleted still surfaces via
    `_format_exiftool_tmp_error` naming the manual fix.
    """
    tmp_path = file_path.with_name(file_path.name + _EXIFTOOL_TMP_SUFFIX)
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass


def _format_exiftool_tmp_error(stderr_text: str, file_path: Path) -> str:
    """Rewrite exiftool's "temp file already exists" stderr to name the fix (#110).

    `_cleanup_exiftool_tmp` runs before every embed/remove attempt, so this
    text should be rare - it only reaches a user if the stray temp file could
    not be deleted (e.g. locked by another process). When it does, the raw
    exiftool text alone reads as an unrelated new failure rather than the
    leftover of a previous one; naming the exact file and the delete-then-
    retry fix keeps the next step honest (AGENTS.md: every error names the
    fix, not just the symptom).
    """
    if not _EXIFTOOL_TMP_EXISTS_RE.search(stderr_text):
        return stderr_text
    tmp_path = file_path.with_name(file_path.name + _EXIFTOOL_TMP_SUFFIX)
    return (
        f'{stderr_text} - this is a stray file left by a previous failed attempt, '
        f'not a new problem; delete {tmp_path} and try again'
    )


def _run_exiftool_embed_source(
    file_path: Path, s_id: str, extra_keywords: list[str] | None = None,
    *, backup: OriginalBackup,
) -> str | None:
    """Append `SOURCE: {s_id}` (and any extra keywords) to a photo's Keywords.

    Uses exiftool's `+=` list-append so existing keywords (DATE:, P-ids) are
    preserved - the only sanctioned write to a photo original (AGENTS.md: photos
    are never renamed, but spec'd keyword writes through fha tools are allowed).
    `extra_keywords` carries bare P-id strings (e.g. `['P-de957bcda1']`) added
    in the same call so SOURCE: and people are atomic: one exiftool invocation
    per file, one rollback path if the record scaffold fails.

    `backup` is the run's safety-copy policy (`_lib.OriginalBackup`, TOOLING
    §13f). This is the write that matters most to it: it is the FIRST time fha
    touches the file, so the copy it takes is the photo exactly as the human's
    camera or scanner left it. A copy that fails refuses the write, returned
    through the same per-file error string an exiftool failure uses, so the
    caller's existing rollback runs unchanged.

    Returns None on success, the stderr text on a per-file failure; raises
    RuntimeError only when exiftool itself is absent.

    Before writing: clears any read-only bit on `file_path` and any stray
    `_exiftool_tmp` left beside it by an earlier failed attempt (#110) - both
    are cheap, best-effort, and mean an ordinary retry after fixing the real
    cause (e.g. clearing read-only by hand) just works, instead of tripping a
    second, differently-worded failure that hides the first one was fixed.
    """
    try:
        backup.ensure(file_path)
    except BackupRefused as e:
        return str(e)
    _clear_read_only(file_path)
    _cleanup_exiftool_tmp(file_path)
    keywords = [f'SOURCE: {s_id}'] + (extra_keywords or [])
    kw_args = [f'-keywords+={kw}' for kw in keywords]
    cmd = ['exiftool'] + kw_args + ['-overwrite_original_in_place', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError(format_exiftool_error('fha process')) from e
    if proc.returncode == 0:
        return None
    _cleanup_exiftool_tmp(file_path)
    return _format_exiftool_tmp_error(proc.stderr.strip(), file_path)


def _run_exiftool_remove_source(
    file_path: Path, s_id: str, extra_keywords: list[str] | None = None,
    *, backup: OriginalBackup,
) -> str | None:
    """Remove a just-added SOURCE keyword (and any extra keywords) during rollback.

    The normal photo path writes the keyword before the record, because a
    failed exiftool write must abort without a dangling source record. If the
    later record write fails, this inverse operation restores the photo to its
    pre-run identity state so the command remains transactional. `extra_keywords`
    must match what was passed to `_run_exiftool_embed_source` so the rollback
    removes exactly what was added.

    It takes the same `backup` guard as the forward write, and for the same
    reason: this is a write to an original photo file, and the rule is on the
    act of writing, not on the caller's intention. In practice the copy is
    already there - the forward write made it, or refused and left nothing to
    roll back - so this call costs one `exists()` check and can only refuse in
    the case where the forward write would have refused too.

    Shares the same read-only-clear / stray-tmp-cleanup discipline as
    `_run_exiftool_embed_source` (#110) - the forward write already clears
    both, so in practice this is a no-op here, but the rollback is its own
    exiftool invocation and can fail for the identical reason if something
    else re-locked the file mid-run.
    """
    try:
        backup.ensure(file_path)
    except BackupRefused as e:
        return str(e)
    _clear_read_only(file_path)
    _cleanup_exiftool_tmp(file_path)
    keywords = [f'SOURCE: {s_id}'] + (extra_keywords or [])
    kw_args = [f'-keywords-={kw}' for kw in keywords]
    cmd = ['exiftool'] + kw_args + ['-overwrite_original_in_place', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError(format_exiftool_error('fha process')) from e
    if proc.returncode == 0:
        return None
    _cleanup_exiftool_tmp(file_path)
    return _format_exiftool_tmp_error(proc.stderr.strip(), file_path)


def _open_backup(
    archive_root: Path, fha_config: dict, backup: OriginalBackup | None,
) -> OriginalBackup:
    """The run's safety-copy policy, announced once and reported (TOOLING §13f).

    `fha process` is print-based rather than Result-based, so the notices go
    straight to the terminal here: the warning to stderr with the other
    warnings, the "N originals copied" line to stdout with the rest of the
    run's report. An operation called as part of a larger run (a triage folder
    processing group after group) is handed the outer run's object so the
    "no safety copies" warning is said once, not once per group.
    """
    if backup is None:
        backup = OriginalBackup(archive_root, fha_config)
    backup.announce()
    _flush_backup_messages(backup)
    return backup


def _flush_backup_messages(backup: OriginalBackup) -> None:
    """Print any safety-copy notices not yet reported."""
    for level, text in backup.drain_messages():
        print(text, file=(sys.stderr if level == 'warning' else sys.stdout))


def _read_source_keyword(file_path: Path) -> str | None:
    """Return the FIRST S-id embedded in a photo's `SOURCE:` keyword(s), or None.

    A photo should carry exactly one, but a hand-edited or corrupted
    Keywords/Subject field can carry more than one (e.g. a different value in
    each field) - a caller that must notice every embedded value, not just
    the first, uses `_read_all_source_keywords` instead (the refile identity
    check: a second, conflicting value must refuse the move even when the
    FIRST value matches the expected source - #163 audit finding).
    """
    matches = _read_all_source_keywords(file_path)
    return matches[0] if matches else None


def _read_all_source_keywords(file_path: Path) -> list[str]:
    """Return every distinct S-id embedded in a photo's `SOURCE:` keyword(s).

    Order-preserving, deduplicated, lowercased. See `_read_source_keyword`
    for why this exists as a sibling rather than a changed return shape:
    every other caller wants "the" S-id an asset carries and is unaffected by
    a conflicting second value, so their contract (str | None) is untouched.
    """
    out: list[str] = []
    for kw in _run_exiftool_read_keywords(file_path):
        m = _SOURCE_KEYWORD_RE.match(kw.strip())
        if m:
            sid = m.group(1).lower()
            if sid not in out:
                out.append(sid)
    return out


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

    The body opens with `_lib.SOURCE_PURPOSE_BLOCK` (SPEC §16a, #75) - the
    same visible "who writes this" blockquote every scaffolded record gets,
    right after the frontmatter and before `## Claims`.
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
        SOURCE_PURPOSE_BLOCK,
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
    """Delegates to the shared `_lib.render_file_entry` (moved there so
    `fha source extract` appends its derived-artifact entry through the same
    single renderer; kept as a thin alias for existing call sites/tests)."""
    from _lib import render_file_entry
    return render_file_entry(item)


def _append_file_entry(record_text: str, entry_lines: list[str]) -> str:
    """Delegates to the shared `_lib.append_file_entry_to_record` (moved
    there for `fha source extract`; thin alias for existing call sites)."""
    from _lib import append_file_entry_to_record
    return append_file_entry_to_record(record_text, entry_lines)


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


def _find_back_sibling(file_path: Path) -> Path | None:
    """Return the unambiguous `{base_id}-back`/`_back` sibling beside a plain
    scan, if one exists on disk - or None (#113).

    A trailing copy letter ('portrait_1880b') names a SEPARATE print of the
    same picture (TOOLING §6) - never a back, and never auto-attached here;
    which member of a set is "the" primary is exactly the judgment call the
    photo variation-set prompt exists for (`_process_variation_set`). An
    explicit `-back`/`_back` suffix is different in kind: it names no other
    physical item, so there is no ambiguity to ask a human about, and no
    prompt-driven grouping flow watches for it when `file_path` is a
    DOCUMENT (`process_document` has no sibling awareness at all otherwise).
    Left unattached, a document's back scan - often the only surviving
    caption or provenance note - simply sits on disk, unrecorded and
    unmentioned, until someone happens to notice a name that no longer has a
    record (#113's actual reported case).

    Restricted to `file_path` itself being a PLAIN scan (no variant letter,
    no recognised part-kind, no crop) - the same "plain" test
    `select_variation_primary` uses for its default pick - so processing a
    copy-letter print or a crop on its own never reaches for a shared back
    scan that conceptually belongs with the group's primary, not with it.

    Finds the sibling by SCANNING the directory and parsing every candidate's
    stem with `parse_media_filename` - the same parser this codebase's own
    filename grammar and photo-variation grouping already use - rather than
    constructing one or two hardcoded lowercase `{stem}-back`/`{stem}_back`
    candidate paths and probing each with `.is_file()`. Two gaps that shape
    missed on a real filesystem: an explicitly-named sibling in a DIFFERENT
    case (`record-BACK.PDF`, on a case-sensitive filesystem where a lowercase
    guess simply does not match), and a back scan saved with a DIFFERENT
    extension than the primary (`record.jpg` primary, `record-back.jpeg`
    scan). Both parse to `part_kind == 'back'` with the same `base_id` as the
    primary, so the scan finds them; the two-candidate-path guess could not.
    Requires EXACTLY ONE match (case-insensitive `base_id` compare). A
    genuine zero-match is unambiguous - returns None, nothing to attach - but
    TWO OR MORE candidates is a real ambiguity this function does not resolve
    on its own, and it RAISES rather than silently picking one (#145 finding
    4). Before this, "nothing found" and "found several, pick one for me"
    both returned the very same None, so the caller silently processed the
    primary alone and left EVERY candidate back scan unattached with no
    warning at all: document intake renames the primary at the same moment
    (removing the obvious naming link back to any candidate), and `fha
    doctor`'s orphaned-back-photo check explicitly excludes document-root
    entries (TOOLING §3a), so nothing downstream ever catches it either. Both
    call sites - `process_document`'s own same-folder fallback, and
    `_run_process`'s inbox-aware discovery, which runs before the primary is
    even relocated - already execute inside a ProcessError-handling context,
    so raising here reaches the human refusal the same way every other
    "found more than one, won't guess" case in this module does
    (`_companion_for_sidecar`'s multiple-companion-asset refusal is the twin
    case for a source-stub sidecar).
    """
    parsed = parse_media_filename(file_path.stem)
    if parsed.variant_id is not None or parsed.part_kind != 'none' or parsed.is_crop:
        return None
    base_id = parsed.base_id.lower()
    try:
        siblings = list(file_path.parent.iterdir())
    except OSError:
        return None
    matches = []
    for candidate in siblings:
        if not candidate.is_file():
            continue
        candidate_parsed = parse_media_filename(candidate.stem)
        if candidate_parsed.part_kind != 'back':
            continue
        if candidate_parsed.base_id.lower() == base_id:
            matches.append(candidate)
    if len(matches) > 1:
        names = ', '.join(sorted(m.name for m in matches))
        raise ProcessError(
            f'{file_path.name} has more than one possible back scan beside it: '
            f'{names} - which one is "the" back cannot be guessed. Rename or move '
            'aside all but the intended one, then re-run.'
        )
    return matches[0] if matches else None


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


def _sidecar_hinted_source_type(sidecar: Path | None) -> str | None:
    """The `source_type:` hint a stub sidecar carries, or None.

    A best-effort peek: a sidecar that fails to parse returns None here
    rather than raising, because every call site either has its own explicit
    `--type` to fall back on or re-reads the sidecar itself downstream (where
    the real ProcessError surfaces with the full context). Factored out so
    the CLI's inbox-relocation classification (`_run_process`, #113's
    back-sibling fix) and `_relocate_from_inbox`'s own internal classification
    read the SAME hint the SAME way - two independent re-implementations of
    "peek at source_type:" is exactly how they used to drift (one honoring
    the hint, the other silently falling back to the raw --type/extension).
    """
    if sidecar is None:
        return None
    try:
        sidecar_meta, _ = _read_sidecar(sidecar)
    except ProcessError:
        return None  # downstream re-parse will raise the real error
    hinted = sidecar_meta.get('source_type')
    return str(hinted) if hinted else None


def _relocate_from_inbox(
    archive_root: Path,
    fha_config: dict,
    file_path: Path,
    sidecar: Path | None,
    *,
    source_type: str | None = None,
    dry_run: bool,
) -> tuple[Path, Path | None, object]:
    """File a COPY of an inbox-staged asset (+ sidecar) into its documents/
    photos root, and park the original(s) in `inbox/processed/` rather than
    deleting them.

    `fha capture --asset` (and a hand-dropped file) stage in `inbox/`, but
    `process_document`/`process_photo` require the asset already under the
    configured root - that's the whole point of an inbox: every fha process
    entrypoint should know how to file something out of it rather than making
    the user move it by hand first. A no-op (returns the inputs unchanged, undo
    `None`) when `file_path` isn't under the resolved inbox root.

    Before #109 this was a flat `Path.rename` (same filename, no rename) into
    documents/ or photos/, which left nothing behind in `inbox/` to audit
    afterward - processing "several hundred files" (the filed issue's own
    case) gave the archive's owner no before-and-after view without digging
    through git history. Now the destination gets a byte-for-byte COPY
    (`shutil.copy2`, so timestamps/metadata survive too) under the SAME
    filename `process_document`/`process_photo` expect (`process_document`
    mints its own `{slug}_{S-id}` rename afterward; photos are never renamed
    at all) - and the ORIGINAL is then MOVED, never copied again, to
    `inbox/processed/<the relative path it had inside inbox/>`, preserving
    whatever subfolder structure it was staged under. This mirrors `fha
    capture --ingest`'s own park-don't-delete precedent for `.ingested/`:
    deletion is the human's call, made after a look, not a side effect of
    processing. The copy step reads `file_path`/`sidecar` exactly once; the
    move step never re-reads their bytes at all.

    On `dry_run` nothing is touched and not-yet-existing destination paths are
    returned (including the `inbox/processed/` one, named but not created) so
    the caller's own preview can still report the full plan. Those returned
    paths name where things WOULD land, not where the bytes are: any
    dry-run read (embedded keywords, sidecar discovery, variation grouping)
    must keep using the pre-move path, which the caller threads through as
    `real_path`/`real_paths`.

    A collision at EITHER destination - the documents/photos root, or the
    `inbox/processed/` park spot (e.g. a same-named file was already
    processed once before) - refuses cleanly before anything is written,
    exactly like the existing documents/photos collision check below.

    Returns `(file_path, sidecar, undo)`. This relocation runs *before*
    `process_document`/`process_photo`'s own validation (e.g. the `dna`
    source_type's documents/dna/ requirement) and their own transactions, so a
    refusal downstream would otherwise leave the asset filed out of the inbox
    even though the command failed overall. The caller must call `undo()` (a
    no-arg callable, or `None` for the no-op case) whenever it reports the
    relocated file's command as anything other than success - `undo()`
    reverses BOTH steps: it deletes the destination copy and moves the
    parked original back to its exact starting location in `inbox/`.
    """
    inbox_root = resolve_path('inbox', fha_config, archive_root)
    if not _is_under(file_path, inbox_root):
        return file_path, sidecar, None

    # A stated source_type overrides the extension heuristic: a record image
    # like `census.jpg` has a photo *extension* but is a document-typed source,
    # and filing it under photos/ would put a census sheet in the family photo
    # library. `--type` (source_type) is the human saying so directly; a stub's
    # `source_type:` hint is the recipe or the capture saying so on his behalf,
    # and the explicit flag outranks the hint. classify_asset owns the rule -
    # including that a stated `photo` never pushes a non-photo file into the
    # photo library.
    kind = classify_asset(file_path, fha_config, archive_root,
                          source_type=source_type or _sidecar_hinted_source_type(sidecar))
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

    # file_path is already confirmed under inbox_root above, so this relative
    # path always resolves - it is what lets inbox/processed/ mirror whatever
    # subfolder structure the human (or a recipe) staged the file under.
    processed_root = inbox_root / _INBOX_PROCESSED_DIRNAME
    inbox_rel = file_path.resolve().relative_to(inbox_root.resolve())
    parked_path = processed_root / inbox_rel
    parked_sidecar = (
        processed_root / sidecar.resolve().relative_to(inbox_root.resolve())
        if sidecar is not None else None
    )
    if parked_path.exists():
        raise ProcessError(f'destination already exists: {_rel(parked_path, archive_root)}')
    if parked_sidecar is not None and parked_sidecar.exists():
        raise ProcessError(f'destination already exists: {_rel(parked_sidecar, archive_root)}')

    if dry_run:
        print(f'[dry-run] Would copy {file_path.name} out of inbox/ into '
              f'{_rel(dest_root, archive_root)}/, parking the original at '
              f'{_rel(parked_path, archive_root)}')
        return new_path, new_sidecar, None

    # parked_sidecar always shares parked_path's parent (a sidecar is always a
    # same-directory sibling of its asset - _find_sidecar/_companion_for_sidecar
    # both construct it that way), so only one lineage of processed/ subfolders
    # is ever created here. Track which ones this call creates (deepest-named
    # first) so a full undo below can remove them again - the same created-dirs
    # bookkeeping process_refile's own rollback uses - rather than leaving empty
    # `inbox/processed/...` folders behind once every file that was ever parked
    # in them has been moved back out.
    created_park_dirs: list[Path] = []
    probe = parked_path.parent
    while not probe.exists() and probe != probe.parent:
        created_park_dirs.append(probe)
        probe = probe.parent

    # Nothing has been written yet, so any failure here (e.g. `processed/`
    # already exists as a plain file, not a folder - NotADirectoryError) can
    # refuse cleanly with inbox/ still guaranteed untouched.
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        parked_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ProcessError(
            f'could not create a destination folder for {file_path.name}: {e}. '
            'Nothing was moved out of inbox/.'
        ) from e

    # Step 1: copy the bytes to the destination - read once, never again.
    # A copy that raises, or lands short (e.g. a full disk truncating it),
    # must leave inbox/ completely untouched: clean up any partial copy and
    # refuse before the original is touched at all.
    try:
        shutil.copy2(file_path, new_path)
        if sidecar is not None:
            shutil.copy2(sidecar, new_sidecar)
        if new_path.stat().st_size != file_path.stat().st_size:
            raise OSError(f'{new_path.name} landed at a different size than the original')
        if sidecar is not None and new_sidecar.stat().st_size != sidecar.stat().st_size:
            raise OSError(f'{new_sidecar.name} landed at a different size than the original')
    except OSError as e:
        new_path.unlink(missing_ok=True)
        if new_sidecar is not None:
            new_sidecar.unlink(missing_ok=True)
        raise ProcessError(
            f'could not copy {file_path.name} into {_rel(dest_root, archive_root)}/: '
            f'{e}. Nothing was moved out of inbox/.'
        ) from e

    # Step 2: move (never copy again) the verified-good original into
    # inbox/processed/. _move_file is the same rename-with-copy+delete-
    # fallback helper process_refile uses for its own moves - inbox/processed/
    # always sits inside the same inbox root, so the fallback should never
    # actually trigger, but nothing here depends on it not triggering.
    primary_parked = False
    try:
        _move_file(file_path, parked_path)
        primary_parked = True
        if sidecar is not None:
            _move_file(sidecar, parked_sidecar)
    except OSError as e:
        # The destination copy already landed (step 1). A failed park must not
        # leave it stranded as an orphan with no corresponding original
        # anywhere findable - undo the copy, and if the primary itself had
        # already been parked before the SIDECAR's move failed, move it back
        # to inbox/ too (_move_file's own contract guarantees a failed move
        # never leaves an unparked file's bytes anywhere but its start point,
        # so only the primary-already-parked case needs an explicit undo here).
        recovery_note = ''
        if primary_parked and parked_path.exists():
            try:
                _move_file(parked_path, file_path)
            except OSError as recovery_exc:
                recovery_note = (
                    f' The original also could not be moved back to inbox/ '
                    f'({recovery_exc}) - it is sitting at '
                    f'{_rel(parked_path, archive_root)} instead.'
                )
        new_path.unlink(missing_ok=True)
        if new_sidecar is not None:
            new_sidecar.unlink(missing_ok=True)
        for d in reversed(created_park_dirs):
            try:
                d.rmdir()
            except OSError:
                pass  # not empty (something else parked there already) - leave it
        raise ProcessError(
            f'could not park {file_path.name} in '
            f'{_rel(processed_root, archive_root)}/: {e}.{recovery_note}'
        ) from e

    print(f'Filed {file_path.name} into {_rel(dest_root, archive_root)}/ and parked the '
          f'original at {_rel(parked_path, archive_root)}')

    def undo() -> None:
        if new_path.exists():
            new_path.unlink()
        if new_sidecar is not None and new_sidecar.exists():
            new_sidecar.unlink()
        if parked_path.exists():
            _move_file(parked_path, file_path)
        if sidecar is not None and parked_sidecar is not None and parked_sidecar.exists():
            _move_file(parked_sidecar, sidecar)
        # Best-effort: drop any processed/ subfolders THIS call created, now
        # that the file(s) they held are back in inbox/ - leaves nothing
        # behind when this is the run that undoes it. A folder still holding
        # something else this call didn't park (e.g. a sibling relocation's
        # own file, undone separately) simply fails rmdir and is left alone.
        for d in reversed(created_park_dirs):
            try:
                d.rmdir()
            except OSError:
                pass

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

    A sidecar saved in another encoding (cp1252, a Windows editor's default)
    gets the same clean refusal rather than the `UnicodeDecodeError` traceback
    `read_record` raises by default (#68): every call site here runs well
    before the mkdir/rename/write/unlink sequence that consumes the sidecar,
    so an uncaught crash left the sidecar untouched anyway - but a bare
    traceback is still not the refusal this function's docstring promises,
    and "not valid UTF-8" is a different fix (re-save the file) than
    "malformed frontmatter" (fix the YAML), so it gets its own message.
    """
    rec = read_record(sidecar, on_decode_error=lambda p: None)
    if rec['undecodable']:
        raise ProcessError(
            f'{sidecar.name} is not saved as UTF-8 text, so its hints could not '
            'be read. Nothing was changed or deleted - open it and save it '
            'again choosing UTF-8 (in Notepad: Save As, then pick UTF-8 from '
            'the Encoding menu), then re-run.'
        )
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
#
# "The same signals" has to mean the same code, or the two rankings drift apart
# silently. The date signal is the one that did: it read the DATE: keyword body
# as if it were an EDTF date, which no spec-conformant keyword ever is. So the
# date vocabulary now lives in _lib (resolve_photo_edtf, edtf_confidence) and
# both tools call it (tools never import tools).

# A user_comment that is purely machine-authored is weak evidence (TOOLING §15b);
# mirrors photoindex._AI_COMMENT_RE.
_AI_COMMENT_RE = re.compile(r'^\s*(AI|Model):', re.I)
# The keyword that carries a photo's date precision (SPEC §20); its body is a
# letter pattern, resolved against DateTimeOriginal - never read as a date itself.
_DATE_KEYWORD_RE = re.compile(r'^DATE:\s*(.+)$')


def _photo_meta_from_row(row: dict) -> dict:
    """Map one exiftool JSON row to the triage signals `_score_photo_group` reads.

    Returns {'caption', 'user_comment', 'edtf', 'has_pid_keyword'}, where `edtf`
    is the photo's RESOLVED date - never the raw keyword body. A DATE: keyword
    carries only precision letters ('Y!M!D!', 'Y~'); the date itself is the
    photo's EXIF DateTimeOriginal, and `_lib.resolve_photo_edtf` pairs the two
    (SPEC §20). Reading the keyword body as a date is the bug this replaced: a
    spec-conformant 'Y!M!D!' is not valid EDTF, so the confident-date signal
    could never fire and only the retired digit form ever scored.

    Split out from the exiftool call so a test can drive the real mapping from
    a fake metadata row - the parsing is where the tools have to agree, and the
    subprocess is what a test cannot run.
    """
    keywords: list[str] = []
    for key in ('Keywords', 'Subject'):
        val = row.get(key)
        if val is None:
            continue
        for v in (val if isinstance(val, list) else [val]):
            keywords.append(str(v))

    date_pattern = None
    for kw in keywords:
        m = _DATE_KEYWORD_RE.match(kw.strip())
        if m:
            date_pattern = m.group(1).strip()
            break
    has_pid = any(id_type_of(kw.strip()) == 'P' for kw in keywords)

    return {
        'caption': row.get('Caption-Abstract') or row.get('Description'),
        'user_comment': row.get('UserComment'),
        'edtf': resolve_photo_edtf(date_pattern, row.get('DateTimeOriginal')),
        'has_pid_keyword': has_pid,
    }


def _run_exiftool_read_meta(file_path: Path) -> dict:
    """Read the caption/date/keyword signals one photo contributes to triage.

    Returns `_photo_meta_from_row`'s dict. A separate seam from
    `_run_exiftool_read_keywords` (which reads only Keywords/Subject to detect a
    SOURCE: marker) because triage also needs the caption, description and
    capture-date fields - DateTimeOriginal is requested here because a DATE:
    keyword alone cannot yield a date (SPEC §20 rule 2). Monkeypatched in tests;
    raises RuntimeError when exiftool is absent so the caller can degrade rather
    than fail.
    """
    cmd = ['exiftool', '-j', '-Caption-Abstract', '-XMP-dc:Description',
           '-UserComment', '-DateTimeOriginal', '-Keywords', '-Subject', str(file_path)]
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
    return _photo_meta_from_row(rows[0] if rows else {})


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

    The confident-date signal shares photoindex's actual code, not a
    restatement of it: `_lib.resolve_photo_edtf` resolves the date and
    `_lib.edtf_confidence` scores it, exactly as photoindex does from its
    cached rows. This used to be a local re-expression carrying a comment that
    claimed parity, and it was wrong for every spec-conformant keyword.
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
    if any(m['edtf'] and edtf_confidence(m['edtf'])[1] == 0 for m in metas):
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
    back_sibling: Path | None = None,
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

    `back_sibling` (#113) is an already-resolved `-back`/`_back` companion the
    CALLER found and relocated (`_run_process` runs the same inbox-relocation
    dance on it that it runs on the primary, since the back scan may be
    sitting in the same inbox folder the primary came from). Left `None`
    (direct callers, tests, the `--more`/photo paths that never pass it), this
    function falls back to its own same-folder discovery via
    `_find_back_sibling` - covering the common case of a back scan already
    co-located with its primary, just without inbox-awareness.
    """
    if existing := _filename_has_source_id(file_path):
        raise ProcessError(
            f'{file_path.name} already carries an S-id ({existing.upper()}); '
            'it looks already processed. Refusing to mint a second ID.'
        )

    documents_root = resolve_path('documents', fha_config, archive_root)
    _require_contained(
        file_path, documents_root, root_label='documents root',
        message=(
            f'{file_path.name} is not under the configured documents root '
            f'({_rel(documents_root, archive_root)}); file it there before processing - '
            'a record outside the asset roots cannot be expressed as a portable alias path.'
        ),
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

    # An unambiguous `-back`/`_back` sibling beside a plain scan is pulled in
    # automatically rather than left on disk unrecorded (#113) - see
    # `_find_back_sibling`'s docstring for why this is safe without a human
    # prompt (unlike a copy-letter print, a back names no other physical item).
    # An explicit `back_sibling` (the CLI's own inbox-aware discovery) wins;
    # otherwise fall back to the same-folder check against wherever this
    # file's bytes actually are right now.
    if back_sibling is None:
        back_sibling = _find_back_sibling(real_path if real_path is not None else file_path)

    # DNA sources always carry restricted: true and must live under
    # documents/dna/ (SPEC §8.5.5, lint E017) - refuse before scaffolding a
    # source the linter would immediately flag.
    if source_type == 'dna':
        dna_root = documents_root / 'dna'
        _require_contained(
            file_path, dna_root, root_label='documents/dna root',
            message=(
                f'{file_path.name} is source_type dna but is not under '
                f'{_rel(dna_root, archive_root)}; file DNA originals there before processing.'
            ),
        )

    sid = _mint_one_source_id(archive_root, source_id=source_id)
    if report is not None:
        report['source_id'] = sid
    final_slug = _derive_slug(slug, final_title if title is None else title, file_path)
    ext = file_path.suffix
    new_name = f'{final_slug}_{sid}{ext}'
    # Destination: a file the human pre-filed into their own subfolder keeps
    # its place - the rename is in place (SPEC §12.1: folders are the human's
    # projection, never the tool's to rearrange). A file sitting at the
    # documents root TOP level - the flat relocation _relocate_from_inbox
    # performs, or a hand-drop at the root - files into documents/{type}/
    # instead, matching the bundle path's _record_subdir destination (owner
    # decision 2026-07-22): routine inbox processing must not pile every
    # document into one flat folder. Both sides resolved: the two intake
    # routes disagree on file_path's form (the CLI path is pre-resolved, the
    # inbox relocation is not), and an external or '..'-relative documents
    # root would otherwise never compare equal. DNA never reaches this branch
    # (refused above unless already under documents/dna/).
    if file_path.parent.resolve() == documents_root.resolve():
        new_path = documents_root / _record_subdir(source_type) / new_name
    else:
        new_path = file_path.with_name(new_name)

    # The back sibling (if any) is filed beside its primary, sharing the same
    # slug/S-id with a `-back` role suffix - the same naming grammar `--more`
    # uses for an attached companion (SPEC §12.1: `{slug}[-{role}]_{S-id}.ext`).
    back_new_path = (
        new_path.with_name(f'{final_slug}-back_{sid}{back_sibling.suffix}')
        if back_sibling is not None else None
    )

    # SPEC §14: proof-argument sources live under sources/proofs/, not a
    # sources/proof-argument/ directory matching the source_type literally.
    record_dir = archive_root / 'sources' / _record_subdir(source_type)
    record_path = record_dir / f'{final_slug}_{sid}.md'
    file_alias = path_to_alias(new_path, 'documents', fha_config, archive_root)

    if new_path.exists():
        raise ProcessError(f'destination file already exists: {new_path.name}')
    if back_new_path is not None and back_new_path.exists():
        raise ProcessError(f'destination file already exists: {back_new_path.name}')
    if record_path.exists():
        raise ProcessError(f'record already exists: {_rel(record_path, archive_root)}')

    file_entries = [{'file': file_alias, 'role': 'primary', 'original_filename': file_path.name}]
    if back_sibling is not None:
        back_alias = path_to_alias(back_new_path, 'documents', fha_config, archive_root)
        file_entries.append({'file': back_alias, 'role': 'back',
                             'original_filename': back_sibling.name})

    # The original inbox basename is the human tag the source was known by; keep
    # it as an alias so any `[[old-name]]` reference still resolves once the file
    # is renamed to `{slug}_{S-id}`. _scaffold_text drops it when it matches the
    # slug the filename already carries (no redundant alias).
    text = _scaffold_text(
        sid, final_title, source_type, file_entries,
        notes_body=notes_body,
        restricted=source_type == 'dna' or _sidecar_flag(sidecar_meta, 'restricted'),
        citation=_sidecar_str(sidecar_meta, 'citation'),
        repository=_sidecar_str(sidecar_meta, 'repository'),
        source_date=source_date or _sidecar_source_date(sidecar_meta, sidecar.name if sidecar else file_path.name),
        provenance=_sidecar_str(sidecar_meta, 'provenance'),
        stem=file_path.stem if _slugify(file_path.stem) != final_slug else None,
    )

    # When the destination changed folders (root-top -> documents/{type}/),
    # the preview names the full relative path, not just the new filename.
    dest_display = (new_name if new_path.parent == file_path.parent
                    else _rel(new_path, archive_root))

    if dry_run:
        print(f'[dry-run] Would mint {sid}')
        print(f'[dry-run] Would rename {file_path.name} -> {dest_display}')
        if back_sibling is not None:
            back_display = (back_new_path.name if back_new_path.parent == back_sibling.parent
                            else _rel(back_new_path, archive_root))
            print(f'[dry-run] Would also rename {back_sibling.name} -> {back_display} '
                  f'(role: back - found beside {file_path.name})')
        print(f'[dry-run] Would scaffold {_rel(record_path, archive_root)}')
        if sidecar is not None:
            print(f'[dry-run] Would delete stub {sidecar.name} (its notes -> ## Notes)')
        return EXIT_CLEAN

    # Each undo carries a plain description so a rollback that cannot finish can
    # name exactly what it left behind - a swallowed undo failure that still
    # reported "rolled back" is the whole hazard here.
    undo: list[tuple[str, object]] = []
    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.rename(new_path)
        undo.append((f'move {new_path.name} back to {file_path.name}',
                     lambda: new_path.rename(file_path)))

        if back_sibling is not None:
            back_new_path.parent.mkdir(parents=True, exist_ok=True)
            back_sibling.rename(back_new_path)
            undo.append((f'move {back_new_path.name} back to {back_sibling.name}',
                         lambda: back_new_path.rename(back_sibling)))

        record_dir.mkdir(parents=True, exist_ok=True)
        record_path.write_text(text, encoding='utf-8')
        undo.append((f'delete the half-written record {record_path.name}',
                     lambda: record_path.unlink(missing_ok=True)))

        if sidecar is not None:
            sidecar.unlink()
    except Exception as e:
        failed = _run_undo(undo)
        if failed:
            print(f'ERROR: processing failed, and the rollback could not finish: {e}',
                  file=sys.stderr)
            print('Could not undo: ' + '; '.join(failed) + '.', file=sys.stderr)
            print('The archive may be inconsistent. Run `fha doctor` to see what is '
                  'off; a file left renamed with no record is re-tied with '
                  '`fha reconcile`.', file=sys.stderr)
        else:
            print(f'ERROR: processing failed, rolled back: {e}', file=sys.stderr)
        return EXIT_FAILURE

    print(f'Minted {sid}')
    print(f'Renamed {file_path.name} -> {dest_display}')
    if back_sibling is not None:
        back_display = (back_new_path.name if back_new_path.parent == back_sibling.parent
                        else _rel(back_new_path, archive_root))
        print(f'Also renamed {back_sibling.name} -> {back_display} (role: back - '
              f'found beside {file_path.name} and attached automatically)')
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
    source_type: str | None = None,
    people: list[str] | None = None,
    real_path: Path | None = None,
    source_id: str | None = None,
    report: dict | None = None,
    backup: OriginalBackup | None = None,
) -> int:
    """M7.2: embed a SOURCE keyword in a photo and scaffold its source record.

    Photos are never renamed. The keyword write is the risky step and happens
    first: if exiftool fails, nothing is scaffolded (TOOLING §6 "abort on
    failure, do not scaffold"). If the scaffold then fails, the just-written
    keyword is removed so the photo is not left half-processed.

    `source_type` is reached only by a file that ALREADY lives in the photos
    root and was given a non-photo `--type` (a census sheet the human filed
    into the photo library before deciding what it was). `fha process` may
    never carry a file out of that root - only `fha process refile` may, and
    only with the human's yes (SPEC §12.1) - so the flag is honoured as far as
    the spec allows: the record is typed and filed as he asked, the file stays
    exactly where it is, and the caller is told refile is how it moves.
    Defaults to `photo`, the ordinary case.

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
    _require_contained(
        file_path, photos_root, root_label='photos root',
        message=(
            f'{file_path.name} is not under the configured photos root '
            f'({_rel(photos_root, archive_root)}); file it there before processing - '
            'a record outside the asset roots cannot be expressed as a portable alias path.'
        ),
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
    resolved_type = source_type or _PHOTO_SOURCE_TYPE
    record_dir = archive_root / 'sources' / _record_subdir(resolved_type)
    record_path = record_dir / f'{final_slug}_{sid}.md'
    file_alias = path_to_alias(file_path, _PHOTO_DIR, fha_config, archive_root)

    if record_path.exists():
        raise ProcessError(f'record already exists: {_rel(record_path, archive_root)}')

    text = _scaffold_text(
        sid, final_title, resolved_type,
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
        # Announce-only: says where the safety copy would go (or that none is
        # configured) and writes nothing - a preview must show the guard the
        # live run would apply, or it is not a preview of that run.
        _open_backup(archive_root, fha_config, backup)
        print(f'[dry-run] Would mint {sid}')
        kw_desc = f'SOURCE: {sid}' + (f' + {len(new_people)} P-id keyword(s)' if new_people else '')
        print(f'[dry-run] Would embed {kw_desc} in {file_path.name} (no rename)')
        print(f'[dry-run] Would scaffold {_rel(record_path, archive_root)}')
        if note := _photo_root_type_note(sid, resolved_type):
            print(f'[dry-run] {note}')
        if new_people:
            print(f'[dry-run] people: {", ".join(new_people)}')
        if sidecar is not None:
            print(f'[dry-run] Would delete stub {sidecar.name} (its notes -> ## Notes)')
        return EXIT_CLEAN

    backup = _open_backup(archive_root, fha_config, backup)
    err = _run_exiftool_embed_source(
        file_path, sid, extra_keywords=new_people or None, backup=backup)
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
            rollback_err = _run_exiftool_remove_source(
                file_path, sid, extra_keywords=new_people or None, backup=backup)
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

    _flush_backup_messages(backup)
    print(f'Minted {sid}')
    print(f'Embedded SOURCE: {sid} in {file_path.name} (not renamed)')
    if new_people:
        print(f'Tagged people: {", ".join(new_people)}')
    print(f'Scaffolded {_rel(record_path, archive_root)}')
    if note := _photo_root_type_note(sid, resolved_type):
        print(note)
    if sidecar is not None:
        print(f'Consumed stub {sidecar.name} (notes -> ## Notes)')
    return EXIT_CLEAN


def _photo_root_type_note(sid: str, resolved_type: str) -> str | None:
    """The next step for a non-photo record whose file sits in the photo library.

    `fha process` never carries a file out of the photos root (SPEC §12.1;
    only `fha process refile` may, and only with the human's yes), so a
    photos-root file given a non-photo `--type` gets the record it asked for
    and keeps its place. That is half an answer unless he is told the other
    half, so this names refile - the one verb allowed to move it - with the
    command already filled in. Returns None for an ordinary photo, which needs
    no explanation at all.
    """
    if resolved_type == _PHOTO_SOURCE_TYPE:
        return None
    return (f'This file lives in your photo library, so it stays there and the record '
            f'is typed {resolved_type}. If the scan itself belongs with your records '
            f'instead, run `fha process refile {sid} --to documents '
            f'--type {resolved_type}`.')


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
    source_type: str | None = None,
    people: list[str] | None = None,
    real_paths: dict[Path, Path] | None = None,
    backup: OriginalBackup | None = None,
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

    `source_type` is process_photo's - a non-photo `--type` on a set that
    already lives in the photos root types the record and moves nothing. See
    that function's docstring for why the flag cannot move these files.
    """
    members = sorted(members)
    real_paths = real_paths or {}
    photos_root = resolve_path(_PHOTO_DIR, fha_config, archive_root)
    for m in members:
        _require_contained(
            m, photos_root, root_label='photos root',
            message=(
                f'{m.name} is not under the configured photos root '
                f'({_rel(photos_root, archive_root)}); file the whole set there before processing.'
            ),
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
    resolved_type = source_type or _PHOTO_SOURCE_TYPE
    record_dir = archive_root / 'sources' / _record_subdir(resolved_type)
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
        sid, final_title, resolved_type, file_entries,
        notes_body=notes_body,
        restricted=_sidecar_flag(sidecar_meta, 'restricted'),
        citation=_sidecar_str(sidecar_meta, 'citation'),
        repository=_sidecar_str(sidecar_meta, 'repository'),
        source_date=source_date or _sidecar_source_date(sidecar_meta, sidecar.name if sidecar else primary.name),
        provenance=_sidecar_str(sidecar_meta, 'provenance'),
        people=people or None,
    )

    if dry_run:
        _open_backup(archive_root, fha_config, backup)
        print(f'[dry-run] Would mint {sid} for a {len(members)}-file variation set')
        for m in ordered:
            tag = 'primary' if m == primary else _variation_role_copy(m, False)[0]
            print(f'[dry-run] Would embed SOURCE: {sid} in {m.name} ({tag}, no rename)')
        if people:
            print(f'[dry-run] people: {", ".join(people)} (keyword on every member)')
        print(f'[dry-run] Would scaffold {_rel(record_path, archive_root)}')
        if note := _photo_root_type_note(sid, resolved_type):
            print(f'[dry-run] {note}')
        if sidecar is not None:
            print(f'[dry-run] Would delete stub {sidecar.name} (its notes -> ## Notes)')
        return EXIT_CLEAN

    backup = _open_backup(archive_root, fha_config, backup)
    embedded: list[Path] = []
    try:
        for m in ordered:
            err = _run_exiftool_embed_source(
                m, sid, extra_keywords=per_member_new_people[m] or None, backup=backup)
            if err is not None:
                raise RuntimeError(f'exiftool could not embed SOURCE keyword in {m.name}: {err}')
            embedded.append(m)
        record_dir.mkdir(parents=True, exist_ok=True)
        record_path.write_text(text, encoding='utf-8')
        if sidecar is not None:
            sidecar.unlink()
    except Exception as e:
        # Best-effort unwind that keeps its failures instead of swallowing them:
        # a SOURCE keyword left on a photo after the record is gone is a real
        # inconsistency, and the owner has to be told when one survives.
        failed: list[str] = []
        try:
            record_path.unlink(missing_ok=True)
        except Exception as undo_exc:
            failed.append(f'delete the half-written record {record_path.name} ({undo_exc})')
        for m in reversed(embedded):
            try:
                kw_err = _run_exiftool_remove_source(
                    m, sid, extra_keywords=per_member_new_people[m] or None, backup=backup)
            except RuntimeError as kw_exc:
                kw_err = str(kw_exc)
            if kw_err is not None:
                failed.append(f'remove the SOURCE: {sid} keyword from {m.name} ({kw_err})')
        if failed:
            print(f'ERROR: processing the variation set failed, and the rollback '
                  f'could not finish: {e}', file=sys.stderr)
            print('Could not undo: ' + '; '.join(failed) + '.', file=sys.stderr)
            print(f'The archive may be inconsistent - a photo may still carry a '
                  f'SOURCE: {sid} keyword pointing at a record that is gone. Run '
                  '`fha doctor` to see what is off, then clear the keyword as it '
                  'advises.', file=sys.stderr)
        else:
            print(f'ERROR: processing the variation set failed, rolled back: {e}', file=sys.stderr)
        return EXIT_FAILURE

    _flush_backup_messages(backup)
    print(f'Minted {sid}')
    for m in ordered:
        tag = 'primary' if m == primary else _variation_role_copy(m, False)[0]
        print(f'Embedded SOURCE: {sid} in {m.name} ({tag}, not renamed)')
    if people:
        print(f'Tagged people: {", ".join(people)} (on all {len(members)} files)')
    print(f'Scaffolded {_rel(record_path, archive_root)} with {len(members)} files')
    if note := _photo_root_type_note(sid, resolved_type):
        print(note)
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
    source_type: str | None = None,
    people: list[str] | None = None,
    real_paths: dict[Path, Path] | None = None,
    source_id: str | None = None,
    report: dict | None = None,
    backup: OriginalBackup | None = None,
) -> int:
    """Surface a variation set and process it per the human's one/separate/skip choice.

    A single-member set has no ambiguity and is processed straight through. For a
    real set the TOOLING §6 prompt is shown with the batch-type label, then:
    `one` mints a shared S-id over the whole set (process_photo_group); `separate`
    processes each member as its own source; `skip` (also blank or anything
    unrecognized - never mutate on an unclear answer) defers the set.

    `real_paths` (the process_photo_group contract) maps a virtual dry-run
    inbox destination to its real on-disk location; it is threaded into every
    processing branch so preview reads stay against reality. `source_type` is
    threaded the same way - a non-photo `--type` on files already in the photo
    library types the record wherever the human's one/separate choice lands.

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
                             dry_run=dry_run, source_type=source_type, people=people,
                             real_path=real_paths.get(members[0]),
                             source_id=source_id, report=report, backup=backup)

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
                                   dry_run=dry_run, source_type=source_type,
                                   people=people,
                                   real_paths=real_paths or None, backup=backup)
    if answer.startswith('sep'):
        rc = EXIT_CLEAN
        for m in members:
            rc = max(rc, process_photo(archive_root, fha_config, m,
                                       slug=None, title=None, source_date=source_date,
                                       dry_run=dry_run, source_type=source_type,
                                       people=people,
                                       real_path=real_paths.get(m), backup=backup))
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
    source_type: str | None = None,
    people: list[str] | None = None,
    backup: OriginalBackup | None = None,
) -> int:
    """M7.3: triage a folder's unprocessed photos, then process selected groups.

    The folder's top-level photo files (by extension, excluding sidecars and any
    file already carrying an `_{S-id}` name) are grouped into variation sets by
    the shared `grouping_stem`, ranked by the same evidence signals
    `fha photoindex triage` uses, and listed for selection. The human picks
    groups (numbers, a comma/space list, or `all`); each chosen group is then
    run through the one/separate/skip flow. Non-recursive: a folder *containing*
    a `notes.md` is a bundle (process_bundle), handled before we get here.

    `source_type` is `--type`, threaded down to every selected group so the
    flag is honoured here too rather than quietly dropped on the folder path.
    Nothing moves: a triage folder's files are already filed, so the type
    lands on the records only (see `process_photo`).
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

    # One policy for the whole triage run, built here rather than per group:
    # a folder of twelve sets must not say "no safety copies are being kept"
    # twelve times, and its copy report is the run's total.
    if backup is None:
        backup = OriginalBackup(archive_root, fha_config)
    rc = EXIT_CLEAN
    for idx in chosen:
        members = scored[idx]['members']
        rc = max(rc, _process_variation_set(
            archive_root, fha_config, members, slug=None, title=None,
            source_date=source_date, dry_run=dry_run, source_type=source_type,
            people=people, backup=backup))
    return rc


def process_bundle(
    archive_root: Path,
    fha_config: dict,
    folder: Path,
    *,
    source_date: str | None,
    dry_run: bool,
    source_type: str | None = None,
    backup: OriginalBackup | None = None,
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

    `source_type` is `--type`. It outranks the notes' own `source_type:` hint,
    which outranks the all-photos inference, and it decides each asset's root
    as well as the record's type - so a bundle of census page scans is filed
    as records whatever their suffix (#59).

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
    # The type decides the whole bundle: one S-id, one source_type, and one
    # root per asset. `--type` outranks the notes' `source_type:` hint, which
    # outranks the all-photos inference - the same precedence the single-file
    # path uses, so a bundle of census page scans does not land in the photo
    # library on the strength of their .jpg suffix (#59).
    stated_type = source_type
    if stated_type is None:
        hinted_type = sidecar_meta.get('source_type')
        if hinted_type:
            hinted_type = str(hinted_type)
            if hinted_type not in SOURCE_TYPES:
                raise ProcessError(
                    f"{notes_path.name} hints {format_source_type_error(hinted_type)} "
                    'Fix the notes before dissolving the bundle.'
                )
            stated_type = hinted_type
    asset_kinds = {a: classify_asset(a, fha_config, archive_root, source_type=stated_type)
                   for a in assets}

    source_type = stated_type or _DEFAULT_DOCUMENT_TYPE
    if stated_type is None and asset_kinds \
            and all(kind == 'photo' for kind in asset_kinds.values()):
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
        if any(item['embed'] for item in plan):
            _open_backup(archive_root, fha_config, backup)
        print(f'[dry-run] Would mint {sid} for bundle {folder.name} ({len(assets)} files)')
        for item in plan:
            verb = 'move + embed SOURCE in' if item['kind'] == 'photo' else 'rename + file'
            print(f'[dry-run] Would {verb} {item["src"].name} -> '
                  f'{_rel(item["dest"], archive_root)}')
        print(f'[dry-run] Would scaffold {_rel(record_path, archive_root)}')
        print(f'[dry-run] Would delete the dissolved bundle folder {folder.name}')
        return EXIT_CLEAN

    # Only when the bundle actually holds a photo to keyword: a bundle of
    # documents is renamed and filed, never written into.
    if any(item['embed'] for item in plan):
        backup = _open_backup(archive_root, fha_config, backup)
    undo: list = []
    embedded: list[tuple[Path, str]] = []
    notes_text = notes_path.read_text(encoding='utf-8')
    try:
        for item in plan:
            item['dest'].parent.mkdir(parents=True, exist_ok=True)
            src, dest = item['src'], item['dest']
            src.rename(dest)
            undo.append((f'move {dest.name} back to {src.name}',
                         lambda s=src, d=dest: d.rename(s)))
            if item['embed']:
                err = _run_exiftool_embed_source(dest, sid, backup=backup)
                if err is not None:
                    raise RuntimeError(f'exiftool could not embed SOURCE keyword in {dest.name}: {err}')
                embedded.append((dest, sid))
        record_dir.mkdir(parents=True, exist_ok=True)
        record_path.write_text(text, encoding='utf-8')
        undo.append((f'delete the half-written record {record_path.name}',
                     lambda: record_path.unlink(missing_ok=True)))

        # Dissolve the now-asset-free folder: remove the notes stub, then rmdir.
        notes_path.unlink()
        undo.append((f'restore the bundle notes stub {notes_path.name}',
                     lambda p=notes_path, text=notes_text: p.write_text(text, encoding='utf-8')))
        folder.rmdir()
    except Exception as e:
        # Keyword removals and the file/record undos are all best-effort, and any
        # that fail are reported: a file left in its destination or a keyword left
        # on a photo with no record is a real inconsistency, not a clean rollback.
        failed: list[str] = []
        for dest, dsid in reversed(embedded):
            try:
                kw_err = _run_exiftool_remove_source(dest, dsid, backup=backup)
            except RuntimeError as kw_exc:
                kw_err = str(kw_exc)
            if kw_err is not None:
                failed.append(f'remove the SOURCE: {dsid} keyword from {dest.name} ({kw_err})')
        failed.extend(_run_undo(undo))
        if failed:
            print(f'ERROR: bundle dissolution failed, and the rollback could not '
                  f'finish: {e}', file=sys.stderr)
            print('Could not undo: ' + '; '.join(failed) + '.', file=sys.stderr)
            print('The archive may be inconsistent - a file may still sit in its '
                  'destination or a photo may still carry a SOURCE keyword with no '
                  'record. Run `fha doctor` to see what is off; `fha reconcile` '
                  're-ties a stranded file to its record.', file=sys.stderr)
        else:
            print(f'ERROR: bundle dissolution failed, rolled back: {e}', file=sys.stderr)
        return EXIT_FAILURE

    if backup is not None:
        _flush_backup_messages(backup)
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


# ── DNA restriction on an existing source (Codex PR #145 review, finding 1) ──
# A DNA attachment names the WHOLE source it lands on as DNA material, not
# just the one file: `fha packet` decides what to copy per SOURCE, not per
# file (packet.py's `_source_copy_plan` copies every asset belonging to an
# INCLUDED source). An unrestricted source that gains a DNA attachment via
# `--more --type dna` would therefore ship that file the moment the source
# itself ships - under `--include-restricted` alone, or under no flag at all
# if the source carried no marker before - defeating the no-override
# `restricted: dna` contract (AGENTS.md #6, TOOLING §1, SPEC §19) that every
# other DNA path already keeps (`fha process --type dna` on a NEW primary file
# forces `restricted:` before scaffolding, SPEC §8.5.5, lint E017). These two
# helpers extend that same promise to an EXISTING source: `_attach_more_engine`
# reads the record once up front and calls them from whichever branch (photo
# or document) and whichever exit path (fresh attach, dry-run preview, or the
# document branch's idempotent-retry no-op) ends up writing.

def _restricted_type_of(value) -> str | None:
    """Normalize a raw `restricted:` value to its type, or None when
    unrestricted.

    A local twin of packet.py's own `_restricted_type` (tools never import
    tools, TOOLING §15) - duplicated rather than shared so the two readers
    agree on the SAME contract without a cross-tool import: an absent/false
    value is unrestricted, a plain truthy value is the type 'plain', and any
    other string is its own type ('dna', 'by-request', …), lowercased. Feed it
    a `parse_frontmatter_strict` value (uncoerced YAML) - the same source
    packet.py's `_record_restriction` reads from - not `read_record`'s
    string-coerced meta, or a real YAML `true` would fail the `value in
    (True, 'true')` check.
    """
    if value in (None, False, '', 'false'):
        return None
    if value in (True, 'true'):
        return 'plain'
    return str(value).strip().lower() or 'plain'



# YAML permits the block scalar's chomping indicator (`+`/`-`) and its
# explicit indentation indicator (a digit 1-9) in EITHER order - `|2-` and
# `|-2` are the same header. `[+-]?\d*` alone only accepted chomp-then-digit
# (adversarial review, round-2 audit: `|2-` failed to match, so
# `_frontmatter_key_span` fell back to header-only replacement for that
# order - the original bug, for that narrower shape; caught in practice by
# `_force_dna_restriction_text`'s own re-parse safety net rather than by
# this regex, which is the point of tightening it here rather than relying
# on that net to keep catching it by accident). `(?:[+-]?\d?|\d[+-]?)`
# accepts both orders, each indicator at most once.
#
# YAML also lets a "node property" - an anchor (`&name`) and/or a type tag
# (`!!str`, a bare `!`, or a custom `!tag`) - precede the block indicator on
# the same line, in EITHER order (the YAML grammar allows both
# tag-then-anchor and anchor-then-tag): `restricted: &privacy >-` or
# `restricted: !!str |` are both valid hand-authored YAML (issue #169
# followup, finding 2 - a Codex P2 against this branch's own round-1 fix).
# Without recognizing these, `_frontmatter_key_span` never even detects a
# block scalar is opening, falls back to header-only replacement, and the
# re-parse safety net below correctly refuses rather than corrupt - but that
# means a VALID restricted: shape gets a false "cannot safely rewrite"
# refusal instead of a clean upgrade. `(?:&\S+|!\S*)` matches either
# property form; up to two tokens (one of each kind, in either order) are
# accepted before the indicator. This is deliberately permissive rather than
# a full YAML grammar - anything it does not confidently recognize still
# falls through to the existing safe single-line treatment, and the
# re-parse safety net below still refuses rather than corrupts if this ever
# over-matches.
_NODE_PROPERTY = r'(?:&\S+|!\S*)'
# The indentation digit is captured under two different group names - one
# per order the chomp/indent indicators can appear in (`indent_a` for
# chomp-then-digit, `indent_b` for digit-then-chomp) - rather than one
# shared name, since Python's `re` (unlike the third-party `regex` module)
# rejects a duplicate group name even across alternation branches that can
# never both match. `_explicit_block_indent` reads whichever one matched.
_BLOCK_SCALAR_HEADER_RE = re.compile(
    r'^(?:' + _NODE_PROPERTY + r'\s+){0,2}[|>]'
    r'(?:[+-]?(?P<indent_a>\d)?|(?P<indent_b>\d)[+-]?)'
    r'(?:\s*#.*)?$'
)
_NODE_PROPERTY_TOKEN_RE = re.compile(r'^' + _NODE_PROPERTY + r'$')


def _explicit_block_indent(header_match: re.Match) -> int | None:
    """Return the explicit indentation indicator (the `2` in `|2`, `>2-`, or
    `|-2`) from an already-matched `_BLOCK_SCALAR_HEADER_RE` header, or None
    if the header carries no explicit indentation indicator - in which case
    the content's indentation is established by its own first continuation
    line instead (issue #169 followup review round 2, post-merge Codex pass
    on #175 - distinct from this file's own round-1 "finding N" comments,
    which numbered a different, earlier batch).

    Reading the digit out of the REGEX MATCH - rather than, say, scanning
    `body` for any digit character - matters because a node property token
    can itself legally contain digits (`&privacy2`): a bare digit-scan over
    the whole header body would mistake that anchor's own name for an
    indentation indicator. The match's `indent_a`/`indent_b` groups are
    anchored to the block indicator's own position in the grammar, so they
    can only ever capture the real indentation digit, never a node
    property's text.
    """
    digit = header_match.group('indent_a') or header_match.group('indent_b')
    return int(digit) if digit else None


def _quote_closes_in(text: str, quote: str) -> bool:
    """True if `text` - everything after a YAML quoted scalar's OPENING
    quote character, whether that is the rest of the same physical line or
    an entire later continuation line - contains that scalar's closing
    `quote` character.

    A quoted scalar can wrap across multiple physical lines (issue #169
    followup review round 2, post-merge Codex pass on #175: `restricted:
    "dead` on one line, an indented `name"` closing it on the next - valid
    hand-authored YAML), so knowing the header line's own quote is
    unterminated is not enough; each following line has to be checked the
    same way until the real close is found. The two quote styles disagree
    on how a LITERAL quote character
    is escaped inside the scalar, so both rules are honored here: a
    double-quoted scalar escapes one with a backslash (`\\"`, which does not
    close the scalar - skip both characters), while a single-quoted scalar
    escapes one by doubling it (`''`, also skipped as a pair) and gives
    backslash no special meaning at all.
    """
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if quote == '"' and ch == '\\':
            i += 2
            continue
        if ch == quote:
            if quote == "'" and i + 1 < n and text[i + 1] == "'":
                i += 2
                continue
            return True
        i += 1
    return False


def _leading_yaml_anchor(value_body: str) -> str | None:
    """Return the anchor name (without its leading `&`) if `value_body` -
    the text of a `key:` line after the colon - opens with a YAML anchor
    node property, else None.

    A human can put an anchor on ANY value, not just a block scalar's own
    header (`restricted: &privacy >-`, but just as validly a one-line
    `restricted: &privacy true`) - up to two node properties (an anchor
    and/or a type tag), in either order, may precede the actual scalar per
    the YAML grammar `_BLOCK_SCALAR_HEADER_RE` already leans on. Scanning
    stops at the first token that is not itself a node property, so a plain
    value's own text is never mistaken for one - `_leading_yaml_anchor('true')`
    is None, and `_leading_yaml_anchor('&privacy true')` is `'privacy'`.

    A caller that REPLACES the whole value outright (issue #169 followup
    review, finding 1) needs this to keep any alias ELSEWHERE in the same
    document valid: `note: *privacy` resolves to whatever `&privacy` points
    at, so dropping the anchor along with the old value leaves that alias
    referencing nothing - not a corruption THIS value caused, but one this
    value's replacement would inflict on a different field entirely. Only
    the anchor is returned - a type tag has no alias pointing at it, so
    there is nothing about it that a value replacement needs to preserve.
    """
    anchor = None
    for tok in value_body.split():
        if not _NODE_PROPERTY_TOKEN_RE.match(tok):
            break
        if tok.startswith('&'):
            anchor = tok[1:]
    return anchor


def _frontmatter_key_span(lines: list[str], start: int, end: int, key: str) -> tuple[int, int] | None:
    """Return the (first, last) line indexes - inclusive, within [start, end) -
    of a top-level frontmatter key's FULL value, or None if `key:` is absent.

    Most frontmatter scalars live on one line, but YAML also lets a value
    open a block scalar (`key: >-` / `|` / `|-` / `|+`, or a bare trailing
    colon that opens a nested block) whose real text continues on the
    following more-indented - or blank - lines. A caller that touches only
    the header line and drops the continuation produces frontmatter that
    still STARTS with a valid-looking `key: value` line but is malformed YAML
    underneath (issue #169, finding 1) - exactly the failure this span-aware
    lookup exists to avoid, by handing the caller the whole span to replace
    as one unit. `source_type:` never takes this shape (SOURCE_TYPES is a
    closed set of single tokens a human cannot author as a block scalar), so
    `_rewrite_source_type_line` has no matching gap and does not need this
    helper; `restricted:` does, since a human can hand-author any YAML scalar
    there, block form included.

    A block scalar's content sits at ONE fixed indentation level - established
    by its own first non-blank continuation line, not by the parent `key:`'s
    column - and only lines indented at least that far (or genuinely blank)
    belong to it. A line indented less than that, but still indented more
    than column 0, sits OUTSIDE the scalar: valid YAML, most often a comment
    a human placed there at a shallower indent than the value itself (issue
    #169 followup review, finding 5). The old loop below swallowed every
    whitespace-prefixed line into the span regardless of how little it was
    indented, so replacing the span silently deleted that comment even though
    it was never part of the value - and the belt-and-suspenders re-parse in
    `_force_dna_restriction_text` could not catch the loss, since the
    resulting YAML (comment gone, everything else intact) is perfectly valid.
    Tracking the established content indentation and stopping the moment a
    non-blank line falls short of it keeps that comment (and anything else
    genuinely outside the scalar) out of the deleted span.

    That "established by its own first continuation line" rule has one
    exception the YAML grammar itself carves out: an EXPLICIT indentation
    indicator on the header (`restricted: |2`) declares the content
    indentation outright, rather than leaving it to be inferred from the
    first continuation line (issue #169 followup review round 2, post-merge
    Codex pass on #175). A valid hand-authored scalar can then have its
    first content line indented deeper than the declared value (say, 4
    spaces) with a LATER line sitting exactly at the declared indent (2) -
    both legitimately belong to the scalar, but deriving content_indent from
    the first line's own 4-space indentation instead of the declared 2 made
    the second line look outdented and wrongly ended the span early,
    leaving part of the old value behind after the rewrite. When the header
    carries an explicit indicator, `content_indent` is seeded from THAT
    value before the loop runs, instead of from the first continuation
    line's own indentation.

    A block scalar is not the only way a value can span multiple physical
    lines: a QUOTED scalar (single- or double-quoted) can also wrap, with
    the closing quote arriving on a later, indented line (`restricted:
    "dead` / `name"` - issue #169 followup review round 2). Neither quote
    style was recognized as continuing before, so the span stopped at the
    header line and the rewrite left the dangling continuation behind.
    """
    for i in range(start, end):
        raw = lines[i].rstrip('\r')
        if not raw.startswith(key + ':'):
            continue
        body = raw[len(key) + 1:].strip()
        last = i
        header_match = _BLOCK_SCALAR_HEADER_RE.match(body)
        if body == '' or header_match:
            content_indent = _explicit_block_indent(header_match) if header_match else None
            j = i + 1
            while j < end:
                nxt = lines[j].rstrip('\r')
                if nxt.strip() == '':
                    j += 1
                    continue
                if nxt[:1] not in (' ', '\t'):
                    break
                indent = len(nxt) - len(nxt.lstrip(' \t'))
                if content_indent is None:
                    content_indent = indent
                elif indent < content_indent:
                    break       # outdented relative to the scalar's own content - not part of it
                last = j
                j += 1
        elif body and body[0] in ('"', "'") and not _quote_closes_in(body[1:], body[0]):
            quote = body[0]
            j = i + 1
            while j < end:
                nxt = lines[j].rstrip('\r')
                last = j
                if _quote_closes_in(nxt, quote):
                    break
                j += 1
        return i, last
    return None


def _force_dna_restriction_text(
    record_text: str, *, record_label: str | None = None,
) -> tuple[str, bool]:
    """Set the record's `restricted:` marker to `dna`, unless it already
    carries one that protects at least as well. Returns (new_text, changed).

    Already-sufficient markers are left alone: `dna` needs no upgrade, and
    `by-request` is MORE restrictive (never opens under any flag, where `dna`
    opens with `--include-dna`) - downgrading it to `dna` would be a
    regression dressed up as a fix. Anything else - absent, plain `true`, or a
    different free-text type such as `deadname` - opens under
    `--include-restricted` alone today, which is exactly the gap a DNA
    attachment cannot be allowed to leave open.

    Text surgery, not a YAML round-trip, so a human's field order and
    comments elsewhere in the frontmatter survive untouched - the same
    discipline `_rewrite_source_type_line` and `_lib.append_file_entry_to_record`
    already apply here, including the CRLF-faithful line-ending handling
    (`append_file_entry_to_record`'s docstring): every line this function
    introduces carries the record's own ending, so a CRLF-authored record
    never comes out with a bare-LF island exactly where the edit landed. A
    record with no parseable frontmatter fence is left untouched (nothing
    safe to edit) rather than guessed at.

    The `restricted:` value may itself be a multi-line YAML block scalar
    (e.g. `restricted: >-` with an indented `true` on the next line) -
    `_frontmatter_key_span` locates the WHOLE span so it can be replaced as
    one unit, rather than leaving an orphaned continuation line behind
    (issue #169, finding 1: a Codex P2 from PR #161's review that was
    tracked rather than fixed in the moment). Belt-and-suspenders past that:
    the rewritten text is re-parsed before it is trusted, and this function
    refuses (raises ProcessError, original text untouched) rather than hand
    the caller frontmatter that would not actually read back as
    `restricted: dna` - the same "refuse rather than corrupt" posture
    `process_refile` applies to its own record surgery via its own
    `parse_frontmatter_strict(final_text)` re-parse.

    When the value being replaced carried a YAML anchor (`restricted:
    &privacy >-`), the replacement line keeps it (`restricted: &privacy
    dna`) instead of dropping it (issue #169 followup review, finding 1):
    valid hand-authored YAML can point an alias at that anchor from ANOTHER
    field entirely (`note: *privacy`), and an alias with no matching anchor
    is itself invalid YAML - the belt-and-suspenders re-parse below would
    then refuse a document whose human-authored shape was never wrong in
    the first place. `&privacy dna` still parses to the plain string `dna`
    for THIS field, and now leaves `*privacy` resolving to that same string
    rather than to nothing.
    """
    meta = parse_frontmatter_strict(record_text) or {}
    if _restricted_type_of(meta.get('restricted')) in ('dna', 'by-request'):
        return record_text, False
    lines = record_text.split('\n')
    cr = '\r' if '\r\n' in record_text else ''
    fence_idx = [i for i, ln in enumerate(lines) if ln.rstrip('\r') == '---']
    if len(fence_idx) < 2:
        return record_text, False
    start, end = fence_idx[0], fence_idx[1]
    span = _frontmatter_key_span(lines, start + 1, end, 'restricted')
    if span is None:
        new_lines = list(lines)
        new_lines[end:end] = ['restricted: dna' + cr]
    else:
        first, last = span
        old_body = lines[first].rstrip('\r')[len('restricted') + 1:].strip()
        anchor = _leading_yaml_anchor(old_body)
        replacement = f'restricted: &{anchor} dna' if anchor else 'restricted: dna'
        new_lines = lines[:first] + [replacement + cr] + lines[last + 1:]
    new_text = '\n'.join(new_lines)
    reparsed = parse_frontmatter_strict(new_text)
    if reparsed is None or _restricted_type_of(reparsed.get('restricted')) != 'dna':
        where = f' in {record_label}' if record_label else ''
        raise ProcessError(
            f'refusing: forcing restricted: dna{where} would not read back '
            'cleanly - the restricted: value may use a YAML form this '
            'surgical rewrite could not safely replace. Fix restricted: by '
            'hand to a plain `restricted: dna` line, then re-run. '
            'Nothing was written.'
        )
    return new_text, True


def _preflight_dna_restriction_before_relocation(
    archive_root: Path,
    fha_config: dict,
    primary_path: Path,
    real_path: Path | None,
    *,
    dry_run: bool,
    source_type: str | None,
) -> None:
    """Best-effort preflight for `attach_more`: when `--type dna` is in play,
    validate the record's `restricted:` rewrite BEFORE `_relocate_from_inbox`
    ever moves an inbox-sourced `--more` file out of `inbox/`.

    `_attach_more_engine`'s own photo/document branches already preflight
    `_force_dna_restriction_text` ahead of their own irreversible-ish
    mutations (the exiftool embed / the rename) - but by the time execution
    reaches EITHER branch, `attach_more`'s own wrapper has already relocated
    an inbox-sourced `--more` file into its asset root (issue #169 followup
    review, finding 2). A refusal from `_force_dna_restriction_text` still
    gets the relocation undone - `attach_more`'s `except Exception:` around
    the engine call below already does that - but recovery then depends on
    THAT undo (another filesystem move) succeeding too; an undo that itself
    hits an OSError would leave a failed command's attachment stranded
    outside the inbox regardless of how carefully the engine ordered its own
    mutations. Running this validation-only pass first means the relocation
    - and any dependency on undoing it - never happens at all when the
    rewrite would refuse.

    This resolves the primary's S-id and its record the same way
    `_attach_more_engine` does, but ONLY to learn whether
    `_force_dna_restriction_text` would refuse; the rewritten text is
    discarded, and the engine re-reads the record fresh afterward. Every
    OTHER failure mode this touches - primary not processed, no record
    found, record unreadable - is swallowed here rather than raised: those
    are unrelated to this finding and already get their own undo-on-failure
    handling exactly as before, so raising early for them here would only
    change their existing messages/exit codes for no reason this finding
    asks for. Only `_force_dna_restriction_text`'s own refusal - the one
    mutation-ordering hazard this preflight exists to catch - is allowed to
    propagate out of this function.
    """
    if (source_type or '').strip().lower() != 'dna':
        return
    try:
        primary_on_disk = real_path if real_path is not None else primary_path
        if dry_run and classify_asset(primary_path, fha_config, archive_root) == 'photo':
            try:
                raw_sid = _read_source_keyword(primary_on_disk)
            except RuntimeError:
                return
        else:
            raw_sid = _source_id_of(primary_path, fha_config, archive_root)
        if raw_sid is None:
            return
        sid = fmt_id_display(raw_sid)
        record_path = _find_record_for_sid(archive_root, sid)
        if record_path is None:
            return
        record_text = read_text_exact(record_path)
    except (OSError, UnicodeDecodeError, ProcessError):
        return
    meta = parse_frontmatter_strict(record_text) or {}
    if _restricted_type_of(meta.get('restricted')) in ('dna', 'by-request'):
        return
    # Validation only: the rewritten text is discarded. A refusal here IS the
    # ordering hazard this preflight exists to catch, and propagates to
    # `attach_more`'s caller before anything has been relocated.
    _force_dna_restriction_text(record_text, record_label=_rel(record_path, archive_root))


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
    backup: OriginalBackup | None = None,
    source_type: str | None = None,
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

    `source_type` is the CLI's own validated `--type` (e.g. `fha process
    <primary> --more page-2.jpg page-2 --type census`) - the SAME rule "an
    explicit --type wins" that the primary file's own classification honors
    (`classify_asset`'s docstring). Before this, a `--more` file's root was
    decided purely by extension, so `--type census` on a `.jpg` attachment was
    silently accepted and then ignored: the page landed in `photos/` with a
    PHOTO keyword instead of being filed and renamed as a document page under
    `documents/census/`, the same class of bug issue #59 already fixed for
    the primary file's own classification.

    This is a thin wrapper: `--more`'s FILE argument is documented (and used,
    per the CLI's own example) as a plain path into `inbox/`, but until #111
    every downstream check assumed it was already filed - `process_photo`/
    `process_document` get their own inbox-relocation from `_run_process`
    before they are ever called, and `attach_more` had no equivalent at all.
    Relocating `more_file` HERE, in the one function that owns its whole
    lifecycle, mirrors that same dance (`_relocate_from_inbox`) rather than
    threading it through the CLI dispatcher a second time. Any non-clean
    result from the engine below - a refusal or a raised exception alike -
    undoes the relocation, so a failed attach never leaves the file stranded
    outside the inbox with nothing to show for it (the same "undo the move
    too" rule `_run_process` already applies to the primary).

    `_preflight_dna_restriction_before_relocation` runs BEFORE that
    relocation, not after (issue #169 followup review, finding 2): an
    explicit `--type dna` whose `restricted:` rewrite would refuse must not
    depend on the relocation's own undo succeeding to keep an inbox-sourced
    file from being stranded outside `inbox/` - see that function's
    docstring for why "moved then undone" is not equivalent to "never
    moved" here.
    """
    _preflight_dna_restriction_before_relocation(
        archive_root, fha_config, primary_path, real_path,
        dry_run=dry_run, source_type=source_type)
    pre_move_more = more_file
    more_file, _, more_relocate_undo = _relocate_from_inbox(
        archive_root, fha_config, more_file, None,
        source_type=source_type, dry_run=dry_run)
    more_real_path = pre_move_more if dry_run and more_file != pre_move_more else None
    try:
        rc = _attach_more_engine(
            archive_root, fha_config, primary_path, more_file, role, copy,
            dry_run=dry_run, real_path=real_path, more_real_path=more_real_path,
            backup=backup, source_type=source_type,
        )
    except Exception:
        if more_relocate_undo is not None:
            more_relocate_undo()
        raise
    if rc != EXIT_CLEAN and more_relocate_undo is not None:
        more_relocate_undo()
    return rc


def _attach_more_engine(
    archive_root: Path,
    fha_config: dict,
    primary_path: Path,
    more_file: Path,
    role: str,
    copy: str | None,
    *,
    dry_run: bool,
    real_path: Path | None,
    more_real_path: Path | None,
    backup: OriginalBackup | None,
    source_type: str | None = None,
) -> int:
    """The `--more` attach logic proper, run against `more_file`'s resolved
    location (`attach_more`'s inbox-relocation wrapper has already moved it,
    or confirmed it needs no move). Split out so that wrapper has a single
    call site to undo the relocation on any non-clean result, rather than
    threading the undo through every one of this function's several
    raise/return points.

    `more_real_path` is `more_file`'s pre-move location on a DRY-RUN
    relocation (nothing moved, `more_file` names a destination that does not
    exist yet) - the same `real_path` contract `process_photo` already uses
    for the PRIMARY, extended to the `--more` file. Every on-disk read below
    targets it; every destination-shaped use (alias, rename target, printed
    path) keeps using `more_file`.

    `source_type` is the CLI's own `--type`, passed straight through to
    `classify_asset` for `more_file` - see `attach_more`'s docstring. It plays
    no part in classifying `primary_path` (already a processed source; its
    root was decided when IT was filed).
    """
    more_on_disk = more_real_path if more_real_path is not None else more_file
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

    more_kind = classify_asset(more_file, fha_config, archive_root, source_type=source_type)

    # #1 (Codex PR #145 review, P1): an explicit --type dna on a --more
    # attachment must not leave the SOURCE record itself unrestricted.
    # `fha packet` decides what to copy per SOURCE, not per file
    # (packet.py's `_source_copy_plan` copies every asset belonging to an
    # INCLUDED source), so a source that stayed unrestricted after gaining a
    # DNA attachment would ship that file under `--include-restricted`
    # alone - or under no flag at all - defeating the no-override
    # `restricted: dna` contract (AGENTS.md #6, TOOLING §1, SPEC §19) every
    # other DNA path already keeps (`fha process --type dna` on a NEW
    # primary forces it, SPEC §8.5.5, lint E017). Read the record once, up
    # front, so both branches below - and the document branch's
    # idempotent-retry check - see the same answer; `_restricted_type_of` is
    # a local twin of packet.py's own `_restricted_type` (tools never import
    # tools, TOOLING §15) so the two readers agree on what already counts as
    # sufficiently restricted.
    try:
        record_text_for_check = read_text_exact(record_path)
    except (OSError, UnicodeDecodeError) as e:
        print(f'ERROR: could not read {_rel(record_path, archive_root)}: {e}', file=sys.stderr)
        return EXIT_FAILURE
    meta_for_check = parse_frontmatter_strict(record_text_for_check) or {}
    needs_dna_upgrade = (
        (source_type or '').strip().lower() == 'dna'
        and _restricted_type_of(meta_for_check.get('restricted')) not in ('dna', 'by-request')
    )

    if more_kind == 'photo':
        photos_root = resolve_path(_PHOTO_DIR, fha_config, archive_root)
        _require_contained(
            more_file, photos_root, root_label='photos root',
            message=(
                f'{more_file.name} is not under the configured photos root '
                f'({_rel(photos_root, archive_root)}); file it there before attaching it.'
            ),
        )
        if dry_run:
            try:
                more_existing = _read_source_keyword(more_on_disk)
            except RuntimeError as e:
                print(f'WARNING: could not read existing keywords from {more_file.name}: {e}',
                      file=sys.stderr)
                more_existing = None
        else:
            more_existing = _read_source_keyword(more_on_disk)
        if more_existing:
            raise ProcessError(f'{more_file.name} already carries a SOURCE keyword.')
        new_alias = path_to_alias(more_file, _PHOTO_DIR, fha_config, archive_root)
        entry = [f'  - file: {_yaml_inline(new_alias)}', f'    role: {_yaml_inline(role)}']
        if copy:
            entry.append(f'    copy: {_yaml_inline(copy)}')
        if dry_run:
            _open_backup(archive_root, fha_config, backup)
            print(f'[dry-run] Would embed SOURCE: {sid} in {more_file.name} (no rename)')
            if needs_dna_upgrade:
                _, dna_would_change = _force_dna_restriction_text(
                    record_text_for_check, record_label=_rel(record_path, archive_root))
                if dna_would_change:
                    print(f'[dry-run] Would set restricted: dna on '
                          f'{_rel(record_path, archive_root)} (required for the attached '
                          'DNA file).')
            print(f'[dry-run] Would add files: entry (role: {role}) to '
                  f'{_rel(record_path, archive_root)}')
            return EXIT_CLEAN
        # Read the record before writing the keyword: if the read fails
        # (permission issue, transient I/O error, non-UTF-8 record), nothing
        # has been written to the photo yet, so there's nothing to roll back.
        try:
            old_text = read_text_exact(record_path)
        except (OSError, UnicodeDecodeError) as e:
            print(f'ERROR: could not read {_rel(record_path, archive_root)}: {e}',
                  file=sys.stderr)
            return EXIT_FAILURE
        # Compute and validate the rewritten record BEFORE the exiftool embed
        # below - an irreversible-ish filesystem mutation that would
        # otherwise depend on rollback if the rewrite then turned out to be
        # unsafe. `_force_dna_restriction_text` can refuse (raise
        # ProcessError) on a `restricted:` value it cannot rewrite with
        # confidence (issue #169, finding 2); preflighting it here - before
        # any mutation - means that refusal no longer depends on the
        # exiftool-remove rollback below succeeding (issue #169 followup
        # review, finding 4). Nothing has touched the photo or the record if
        # this raises.
        new_text = old_text
        dna_restriction_set = False
        if needs_dna_upgrade:
            new_text, dna_restriction_set = _force_dna_restriction_text(
                new_text, record_label=_rel(record_path, archive_root))
        try:
            new_text = _append_file_entry(new_text, entry)
        except Exception as e:
            # Also preflighted, ahead of the embed: an unparseable record
            # (e.g. malformed frontmatter) can only be discovered by actually
            # trying to append the entry, but discovering it HERE - before
            # anything has touched the photo - means there is nothing to roll
            # back; the old ordering embedded the keyword first and would
            # have had to undo it for this same failure.
            print(f'ERROR: could not add files: entry to '
                  f'{_rel(record_path, archive_root)}: {e}', file=sys.stderr)
            return EXIT_FAILURE
        backup = _open_backup(archive_root, fha_config, backup)
        err = _run_exiftool_embed_source(more_file, sid, backup=backup)
        if err is not None:
            print(f'ERROR: exiftool could not embed SOURCE keyword in {more_file.name}: {err}',
                  file=sys.stderr)
            return EXIT_FAILURE
        try:
            # Atomic, unlike the scaffolding writes above: those CREATE a record
            # and their undo unlinks the partial, but this one REPLACES a
            # complete source record to add one files: entry. A truncating write
            # here would trade the whole record for a fragment, and the rollback
            # below would be trying to restore a file the failure had already
            # destroyed.
            write_text_exact_atomic(record_path, reapply_newline(new_text, old_text))
        except Exception as e:
            try:
                rollback_err = _run_exiftool_remove_source(more_file, sid, backup=backup)
            except RuntimeError as rollback_exc:
                rollback_err = str(rollback_exc)
            try:
                write_text_exact_atomic(record_path, old_text)
            except Exception:
                pass
            print(f'ERROR: attach failed after keyword write: {e}', file=sys.stderr)
            if rollback_err is None:
                print(f'Rolled back SOURCE: {sid} from {more_file.name}.', file=sys.stderr)
            else:
                print(f'WARNING: could not roll back SOURCE: {sid} from {more_file.name}: '
                      f'{rollback_err}', file=sys.stderr)
            return EXIT_FAILURE
        _flush_backup_messages(backup)
        print(f'Embedded SOURCE: {sid} in {more_file.name} (not renamed)')
        if dna_restriction_set:
            print(f'Set restricted: dna on {_rel(record_path, archive_root)} '
                  '(required for the attached DNA file).')
        print(f'Added files: entry (role: {role}) to {_rel(record_path, archive_root)}')
        return EXIT_CLEAN

    # Document: rename to share the record's S-id with a -role suffix - unless
    # the file already carries THIS source's S-id, which happens whenever it
    # was named on the archive's own recommended companion convention
    # (`<stem>-transcript_S-<id>.md`) before being attached (#108). The guard
    # stays for anything else: a file already bearing a DIFFERENT S-id looks
    # filed for another source, which is exactly the silent-re-processing
    # case it exists to catch.
    existing_doc_sid = _filename_has_source_id(more_file)
    if existing_doc_sid is not None and existing_doc_sid != sid.lower():
        raise ProcessError(
            f'{more_file.name} already carries a different S-id '
            f'({existing_doc_sid.upper()}, not {sid.upper()}); it looks filed for '
            'another source. Attach it to that source, or rename it, before attaching it here.'
        )
    already_named = existing_doc_sid is not None
    documents_root = resolve_path('documents', fha_config, archive_root)
    _require_contained(
        more_file, documents_root, root_label='documents root',
        message=(
            f'{more_file.name} is not under the configured documents root '
            f'({_rel(documents_root, archive_root)}); file it there before attaching it.'
        ),
    )
    if already_named:
        # Already named exactly right (#108) - keep the name as-is rather than
        # recomputing base+suffix+sid, which would either duplicate the role
        # word already baked into the stem or double the S-id suffix.
        new_name = more_file.name
    else:
        base = _slugify(more_file.stem)
        suffix = f'-{_slugify(role)}'
        if copy:
            suffix = f'-{_slugify(copy)}{suffix}'
        new_name = f'{base}{suffix}_{sid}{more_file.suffix}'
    # Same destination rule as process_document (M11.2): a pre-filed
    # attachment renames in place; one sitting at the documents root TOP
    # level files WITH its source - beside a document primary (honoring
    # whatever subfolder the human chose for it, or staying flat beside a
    # legacy flat-filed primary), else into documents/{type}/ (the record
    # dir name IS _record_subdir(source_type) for every scaffold path).
    if more_file.parent.resolve() == documents_root.resolve():
        if _is_under(primary_path, documents_root):
            dest_dir = primary_path.parent
        else:
            dest_dir = documents_root / record_path.parent.name
        new_path = dest_dir / new_name
    else:
        new_path = more_file.with_name(new_name)
    # already_named may still need a MOVE (a file staged in inbox/ and
    # relocated flat to documents/ root, then filed beside its primary) even
    # though it needs no RENAME - the two are independent (#108 + #111).
    needs_move = new_path.resolve() != more_file.resolve()
    new_alias = path_to_alias(new_path, 'documents', fha_config, archive_root)

    # #2 fix: the no-rename path above (#108) accepts ANY file already
    # carrying this source's own S-id - including one that is ALREADY listed
    # in the record's own files: (an already-attached transcript, or even the
    # primary file itself, re-offered under a different role spelling).
    # Without this check, retrying the identical --more command exits clean
    # while silently appending a DUPLICATE alias entry - or, worse, a second
    # entry for the same physical file carrying a CONFLICTING role. Checked
    # here (before any write, and before the dry-run branch below) so a
    # dry-run preview and a live run agree on the outcome, and an idempotent
    # retry never reaches the rename/move logic at all.
    #
    # `record_text_for_check`/`meta_for_check` (read_text_exact +
    # parse_frontmatter_strict, not read_record) come from the shared read
    # near the top of this function (the DNA-restriction check, finding 1,
    # needed the same record read before this function knew which branch it
    # was in) - an unparseable frontmatter there already fell back to `{}`,
    # so the duplicate check below just finds no `files:` list rather than
    # inventing a new refusal class this finding did not ask for.
    existing_entries = [e for e in (meta_for_check.get('files') or [])
                        if isinstance(e, dict) and e.get('file')]
    new_alias_norm = new_alias.replace('\\', '/')
    existing_match = next(
        (e for e in existing_entries
         if str(e['file']).replace('\\', '/') == new_alias_norm),
        None,
    )
    if existing_match is not None:
        existing_role = str(existing_match.get('role') or '').strip() or 'attachment'
        existing_copy = str(existing_match.get('copy') or '').strip() or None
        requested_role = role.strip() or 'attachment'
        requested_copy = (copy or '').strip() or None
        if existing_role == requested_role and existing_copy == requested_copy:
            # #3 (Codex PR #145 review): the alias/role/copy lining up with an
            # already-listed entry is only a genuine repeat of an
            # already-completed command when the supplied file IS ALREADY at
            # the listed destination (`not needs_move`). Before this, ANY
            # alias match short-circuited here, so a record whose listed file
            # had gone missing from disk - with a same-named REPLACEMENT
            # still sitting untouched under its own name - reported "already
            # attached, nothing to do" without ever moving the replacement
            # into place or touching the record: a false success that left
            # the hole exactly as it was.
            if not needs_move:
                if needs_dna_upgrade:
                    new_text, changed = _force_dna_restriction_text(
                        record_text_for_check, record_label=_rel(record_path, archive_root))
                    if dry_run:
                        if changed:
                            print(f'[dry-run] Would set restricted: dna on '
                                  f'{_rel(record_path, archive_root)} (required for the '
                                  'attached DNA file).')
                    elif changed:
                        try:
                            write_text_exact_atomic(
                                record_path, reapply_newline(new_text, record_text_for_check))
                        except OSError as e:
                            print(f'ERROR: could not write '
                                  f'{_rel(record_path, archive_root)}: {e}', file=sys.stderr)
                            return EXIT_FAILURE
                        print(f'Set restricted: dna on {_rel(record_path, archive_root)} '
                              '(required for the attached DNA file).')
                print(f"{more_file.name} is already attached to "
                      f"{_rel(record_path, archive_root)} as role '{existing_role}'"
                      + (f' (copy: {existing_copy})' if existing_copy else '')
                      + ' - nothing to do.')
                return EXIT_CLEAN
            if new_path.exists():
                raise ProcessError(
                    f'destination file already exists: {new_path.name} - '
                    f'{new_alias} is already listed in {_rel(record_path, archive_root)} as '
                    f"role '{existing_role}'"
                    + (f' (copy: {existing_copy})' if existing_copy else '')
                    + ', and a different file already sits at that path. Resolve the '
                    'name collision by hand, then re-run.'
                )
            raise ProcessError(
                f'{new_alias} is already listed in {_rel(record_path, archive_root)} as '
                f"role '{existing_role}'"
                + (f' (copy: {existing_copy})' if existing_copy else '')
                + f', but that file is missing from disk, and {more_file.name} is a '
                'different file still sitting where it was - not yet moved into place. '
                'If it is meant to replace the missing file, remove the stale files: '
                'entry from the record by hand and re-run this command to attach it '
                'fresh; if the original was simply misplaced, put it back at the '
                'listed path instead.'
            )
        raise ProcessError(
            f'{new_alias} is already listed in {_rel(record_path, archive_root)} as '
            f"role '{existing_role}'"
            + (f' (copy: {existing_copy})' if existing_copy else '')
            + f", which conflicts with the requested role '{requested_role}'"
            + (f' (copy: {requested_copy})' if requested_copy else '')
            + '. Edit the record by hand if this is intentional, or attach a different file.'
        )

    entry = [f'  - file: {_yaml_inline(new_alias)}', f'    role: {_yaml_inline(role)}']
    if copy:
        entry.append(f'    copy: {_yaml_inline(copy)}')
    entry.append(f'    original_filename: {_yaml_inline(more_file.name)}')

    if needs_move and new_path.exists():
        raise ProcessError(f'destination file already exists: {new_path.name}')

    more_dest_display = (new_name if new_path.parent == more_file.parent
                         else _rel(new_path, archive_root))

    if dry_run:
        if needs_move:
            print(f'[dry-run] Would rename {more_file.name} -> {more_dest_display}')
        else:
            print(f"[dry-run] {more_file.name} already carries this source's S-id; keeping its name.")
        if needs_dna_upgrade:
            # Validate the rewrite here too, not just on the live path
            # (issue #169 followup review, finding 3): `_force_dna_
            # restriction_text` can refuse (raise ProcessError) on a
            # `restricted:` value it cannot rewrite with confidence, and
            # running that validation only live let `--dry-run` promise an
            # update it could not actually deliver - the live run would
            # begin the rename, then fail and roll it back, while the
            # preview had reported clean. This call is pure text surgery (no
            # filesystem side effects), so calling it here for its outcome
            # is safe during a dry run; a refusal now shows up identically
            # in preview and in the live run.
            _, dna_would_change = _force_dna_restriction_text(
                record_text_for_check, record_label=_rel(record_path, archive_root))
            if dna_would_change:
                print(f'[dry-run] Would set restricted: dna on {_rel(record_path, archive_root)} '
                      '(required for the attached DNA file).')
        print(f'[dry-run] Would add files: entry (role: {role}) to '
              f'{_rel(record_path, archive_root)}')
        return EXIT_CLEAN

    undo: list = []
    try:
        old_text = read_text_exact(record_path)
    except (OSError, UnicodeDecodeError) as e:
        print(f'ERROR: could not read {_rel(record_path, archive_root)}: {e}', file=sys.stderr)
        return EXIT_FAILURE
    # Compute and validate the rewritten record BEFORE the rename below - the
    # documents-root counterpart to the photo branch's own preflight above
    # (issue #169 followup review, finding 4): a refusal from
    # `_force_dna_restriction_text` must not depend on the rename-undo below
    # succeeding. Nothing on disk has moved yet if this raises.
    new_text = old_text
    dna_restriction_set = False
    if needs_dna_upgrade:
        new_text, dna_restriction_set = _force_dna_restriction_text(
            new_text, record_label=_rel(record_path, archive_root))
    try:
        new_text = _append_file_entry(new_text, entry)
    except Exception as e:
        # Also preflighted, ahead of the rename: an unparseable record (e.g.
        # malformed frontmatter) can only be discovered by actually trying to
        # append the entry, but discovering it HERE - before anything on
        # disk has moved - means there is nothing to roll back; the old
        # ordering renamed the file first and would have had to undo that
        # for this same failure.
        print(f'ERROR: could not add files: entry to '
              f'{_rel(record_path, archive_root)}: {e}', file=sys.stderr)
        return EXIT_FAILURE
    try:
        if needs_move:
            new_path.parent.mkdir(parents=True, exist_ok=True)
            more_file.rename(new_path)
            undo.append((f'move {new_path.name} back to {more_file.name}',
                         lambda: new_path.rename(more_file)))
        # Atomic for the same reason as the photos branch above: an existing
        # source record is being replaced, not created.
        write_text_exact_atomic(record_path, reapply_newline(new_text, old_text))
    except Exception as e:
        # Undo the record entry and the rename best-effort, but keep any failure:
        # a file left renamed while the record no longer lists it is inconsistent,
        # so the owner is told rather than shown a false "rolled back".
        failed: list[str] = []
        try:
            write_text_exact_atomic(record_path, old_text)
        except Exception as rec_exc:
            failed.append(f'restore {record_path.name} ({rec_exc})')
        failed.extend(_run_undo(undo))
        if failed:
            print(f'ERROR: attach failed, and the rollback could not finish: {e}',
                  file=sys.stderr)
            print('Could not undo: ' + '; '.join(failed) + '.', file=sys.stderr)
            print('The archive may be inconsistent. Run `fha doctor` to see what is '
                  'off; `fha reconcile` re-ties a renamed file to its record.',
                  file=sys.stderr)
        else:
            print(f'ERROR: attach failed, rolled back: {e}', file=sys.stderr)
        return EXIT_FAILURE
    if needs_move:
        print(f'Renamed {more_file.name} -> {more_dest_display}')
    else:
        print(f'Kept {more_file.name} (already named for this source)')
    if dna_restriction_set:
        print(f'Set restricted: dna on {_rel(record_path, archive_root)} '
              '(required for the attached DNA file).')
    print(f'Added files: entry (role: {role}) to {_rel(record_path, archive_root)}')
    return EXIT_CLEAN


# ── Refile (the sanctioned cross-root correction move) ───────────────────────
#
# SPEC §12.1's law is that filing out of the inbox is the ONE sanctioned move
# of an original, and §13 adds that a filed documents-root file is renamed
# exactly once while a photos-root file is never renamed at all. `fha process
# refile` is the owner-approved carve-out (usage feedback item 3, 2026-07-23):
# the sanctioned CROSS-ROOT correction for a filing decision that turned out
# wrong - a scan processed as a document that belongs in the photo library, or
# a photo that is really a record. Within-root reorganization is NOT this verb:
# that stays free (folders are projection) and `fha reconcile` heals it.

def _stdin_is_interactive() -> bool:
    """Whether a [y/N] confirmation can actually be answered (tests patch this).

    A seam rather than a bare `sys.stdin.isatty()` call at the use site so the
    photos->documents confirm gate can be exercised both ways without a TTY.
    """
    return sys.stdin.isatty()


def _run_undo(undo: list) -> list[str]:
    """Run undo steps in reverse, best-effort, and return what could NOT be undone.

    Rollback has to be best-effort - a later step still runs after an earlier one
    fails - but it must never claim more than it actually did. Each entry is a
    (description, callable) pair, and the description is written for the archive's
    owner, so a rollback that cannot finish names exactly what it left behind (a
    file still renamed, a record still on disk) instead of the older silent
    'rolled back' that hid a half-undone, inconsistent archive from him.
    """
    failed: list[str] = []
    for desc, fn in reversed(undo):
        try:
            fn()
        except Exception as undo_exc:
            failed.append(f'{desc} ({undo_exc})')
    return failed


def _move_file(src: Path, dest: Path) -> None:
    """Move one file, falling back to copy+delete across filesystems.

    `Path.rename` is atomic but only works within one filesystem, and refile
    exists precisely for archives whose documents and photos roots live on
    different drives (SPEC §12.4 external roots). The fallback copies bytes and
    timestamps then removes the source; a failed copy removes the partial
    destination first so an interrupted move never leaves two half-files.
    """
    try:
        src.rename(dest)
        return
    except OSError:
        pass
    try:
        shutil.copy2(src, dest)
    except Exception:
        # Best-effort: drop the partial copy, but never let a failure to remove
        # it (a locked handle on Windows) MASK the real copy error - re-raise the
        # copy failure. A partial that survives is caught by the caller's
        # residual-partial guard, which checks `dest` after a failed forward move
        # and names the stray so no rollback is reported as clean.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        src.unlink()
    except Exception:
        # The copy landed but the source could not be removed (a locked handle
        # on Windows, say). Keep the ORIGINAL as the sole copy and drop the
        # partial destination, so an interrupted move never leaves two files
        # and the caller's rollback can still remove any folders it created.
        dest.unlink(missing_ok=True)
        raise


def _scalar_value(raw: str) -> tuple[str, str]:
    """Split a `file:` line's value region into (value, trailing_comment).

    Quote state is tracked BEFORE any comment split, because a '#' inside a
    quoted scalar is data, not a comment - the reverse order truncated a
    legitimately quoted `"documents/x #1.jpg"` at the '#' and made it
    unmatchable. For a bare scalar the comment must be whitespace-preceded
    (YAML's own rule), so a '#' touching non-space text stays part of the path.

    The comment region is returned untouched, and NOT because the matcher needs
    it - matching ignores it - but because refile's `_rewrite_file_line` has to
    re-append a hand-written inventory note (`# fragile original`) onto the
    rewritten line; dropping it silently deletes the owner's annotation on a
    "successful" refile. A deliberate twin of reconcile.py's `_split_file_value`
    so the two surgical writers read `file:` lines the same way.
    """
    s = raw.strip()
    if not s:
        return '', ''
    if s[0] in ('"', "'"):
        quote = s[0]
        i = 1
        while i < len(s):
            c = s[i]
            if quote == '"' and c == '\\':
                i += 2                      # backslash escape in a double quote
                continue
            if c == quote:
                if quote == "'" and i + 1 < len(s) and s[i + 1] == "'":
                    i += 2                  # '' is an escaped single quote
                    continue
                i += 1
                break
            i += 1
        value_repr, trailing = s[:i], s[i:]
    else:
        cut = len(s)
        for j in range(1, len(s)):
            if s[j] == '#' and s[j - 1] in (' ', '\t'):
                cut = j
                break
        value_repr, trailing = s[:cut].rstrip(), s[cut:]
    try:
        value = yaml.safe_load(value_repr)
    except Exception:
        value = value_repr
    if not isinstance(value, str):
        value = value_repr
    return value, trailing.strip()


def _rewrite_file_line(text: str, old_alias: str, new_alias: str) -> tuple[str, int]:
    """Rewrite the ONE `file:` line whose VALUE equals `old_alias` exactly.

    A deliberate twin of `reconcile.py`'s private `_rewrite_entry` (see its P2
    history): the match is value-exact after stripping quotes and trailing
    comments - never substring containment, which would let a stale 'x.jpg'
    rewrite grab a sibling 'x.jpg.txt' entry and corrupt it while reporting
    success. Duplicated here rather than extracted to _lib because tools never
    import tools and reconcile keeps its own copy; if a third caller appears,
    extract the shared matcher then.

    Three disciplines beyond value-exact matching, each closing a corruption
    class a lens found: the scan is bounded to the frontmatter fence, so a body
    line that happens to read `- file: <alias>` (a Notes bullet) can never be
    mistaken for the inventory; the replacement is re-emitted through
    `_yaml_inline`, so a new alias carrying a YAML-hostile character (a photo
    named `Scan #12.jpg`, or a `--dest` like `Box #3`) is quoted rather than
    silently truncated at the ' #'; and an alias listed on more than one line
    refuses rather than guessing which entry to touch.

    The matched line's trailing comment is carried onto the rewrite verbatim -
    a hand-written note like `# fragile original` is the owner's annotation, and
    a refile that silently dropped it would be a quiet data loss reported as a
    success (reconcile.py's `_rewrite_entry` keeps it the same way). Returns
    (new_text, match_count): the caller refuses on 0 (not found) or >1
    (ambiguous).
    """
    lines = text.split('\n')
    span = frontmatter_fence_span(lines)
    if span is None:
        return text, 0
    hits: list[int] = []
    for i in range(span[0] + 1, span[1]):
        stripped = lines[i].strip()
        if stripped.startswith('- file:'):
            value = stripped[len('- file:'):]
        elif stripped.startswith('file:'):
            value = stripped[len('file:'):]
        else:
            continue
        if _scalar_value(value)[0] == old_alias:
            hits.append(i)
    if len(hits) != 1:
        return text, len(hits)
    i = hits[0]
    line = lines[i]
    indent = line[:len(line) - len(line.lstrip())]
    key = '- file:' if line.lstrip().startswith('- file:') else 'file:'
    _, comment = _scalar_value(line.lstrip()[len(key):])
    new_line = f'{indent}{key} {_yaml_inline(new_alias)}'
    if comment:
        new_line = f'{new_line}  {comment}'
    lines[i] = new_line
    return '\n'.join(lines), 1


def _file_line_count(text: str) -> int:
    """Count `file:` inventory lines - the refuse-rather-than-corrupt guard.

    The rewritten record must carry exactly as many `file:` lines as before
    (reconcile.py's `_apply` discipline); any drift means the surgery touched
    more than the one entry and the write must be refused, file untouched.
    """
    return sum(1 for ln in text.split('\n')
               if ln.strip().startswith(('file:', '- file:')))


def _validate_dest_subpath(root: Path, dest: str, root_label: str) -> Path:
    """Validate a `--dest SUBPATH` and return the directory it names under `root`.

    Mirrors person.py's `_validate_into_folder` containment discipline: no
    absolute paths, no '.'/'..' steps, and the resolved result must sit INSIDE
    the resolved root - the resolve() check is what stops a Windows
    drive-relative spelling ('C:1880s') and any other escape the string tests
    cannot see. A leading '{root_label}/' is forgiven (the natural spelling
    when copying a path out of a record). The directory need not exist yet -
    refile creates it.
    """
    example = '--dest 1880s' if root_label == _PHOTO_DIR else '--dest census'
    raw = str(dest).strip().replace('\\', '/')
    if not raw:
        raise ProcessError(
            f'--dest needs a folder path under the {root_label} root, e.g. {example}.')
    if Path(raw).is_absolute() or (len(raw) > 1 and raw[1] == ':'):
        raise ProcessError(
            f'--dest {dest!r} is an absolute path - name a folder under the '
            f'{root_label} root instead, e.g. {example}.')
    if raw.lower().startswith(root_label + '/'):
        raw = raw[len(root_label) + 1:]
    parts = [p for p in raw.strip('/').split('/') if p]
    if not parts or any(p in ('.', '..') for p in parts):
        raise ProcessError(
            f'--dest {dest!r} does not name a folder under the {root_label} root - '
            f'no ".." or "." steps, e.g. {example}.')
    if any(p != p.rstrip('. ') for p in parts):
        raise ProcessError(
            f'--dest {dest!r} has a folder name ending in a dot or space - '
            'Windows silently strips those on disk, so the record would name a '
            f'folder that does not exist. Drop the trailing character, e.g. {example}.')
    dest_dir = root.joinpath(*parts)
    try:
        dest_dir.resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        # RuntimeError joins ValueError/OSError here for the same reason as
        # `_is_under` above (issue #170 finding 2): a symlink loop under
        # --dest raises RuntimeError from `.resolve()`, not ValueError/
        # OSError, and without it here this refusal was a raw traceback
        # instead of the clean message every other containment failure
        # already gets.
        raise ProcessError(
            f'--dest {dest!r} does not resolve to a folder inside the '
            f'{root_label} root - name a plain subfolder, e.g. {example}.')
    return dest_dir


def _refile_pick_entry(entries: list[dict], file_name: str | None, record_name: str) -> dict:
    """Choose which `files:` inventory entry refile moves - never by guessing.

    With `--file NAME`: the entry whose basename (or full stored path) matches,
    refused with the candidate list when nothing (or more than one thing)
    matches. Without it: the single non-derived entry, the overwhelmingly
    common case. Anything else - several files, or only `derived: true`
    artifacts - refuses and lists the names, because moving the wrong file
    quietly would be worse than asking. A derived entry (an extracted-text
    companion, a corrected transcript) may legitimately be refiled, but only
    when named explicitly; it is never the default pick.
    """
    def _posix(entry: dict) -> str:
        # Normalize a stored alias to POSIX separators before deriving its
        # basename. Aliases are forward-slash by contract, but a record written
        # on Windows may carry backslashes ('documents\\letters\\scan.pdf'); on
        # POSIX, Path('documents\\letters\\scan.pdf').name is the whole string,
        # so an un-normalized `--file scan.pdf` lookup would miss a listed file
        # and refuse. Mirrors the reconcile refile selector's normalization.
        return str(entry['file']).replace('\\', '/')

    def _names() -> str:
        return ', '.join(PurePosixPath(_posix(e)).name for e in entries)

    if file_name:
        wanted = str(file_name).replace('\\', '/')
        matches = [
            e for e in entries
            if PurePosixPath(_posix(e)).name == wanted
            or _posix(e) == wanted
        ]
        if not matches:
            raise ProcessError(
                f'--file {file_name!r} does not match any file listed by '
                f'{record_name}. Its files are: {_names()}.')
        if len(matches) > 1:
            raise ProcessError(
                f'--file {file_name!r} matches more than one entry in '
                f'{record_name} - name the full stored path instead, e.g. '
                f'--file "{matches[0]["file"]}".')
        return matches[0]

    candidates = [e for e in entries if not e.get('derived')]
    if len(candidates) == 1:
        return candidates[0]
    raise ProcessError(
        f'{record_name} lists {len(entries)} files, so the one to move must be '
        f'named: add --file NAME. Its files are: {_names()}.')


def _photo_library_name(src: Path, entry: dict) -> str:
    """The last-chance rename for a documents->photos crossing.

    After the move the file is a photos-root file and is NEVER renamed again
    (SPEC §13), so this is the one legal moment to shed the processing name.
    The recorded `original_filename` wins when present - the exact name the
    human filed it under. Otherwise the `_{S-id}` suffix is stripped, along
    with the entry's own `-{role}`/`-{copy}` suffixes per the §13 grammar
    (`{slug}[-{copy}][-{role}]_{S-id}`), so no S-id jargon lands in the photo
    library. Stripping is entry-driven, not guessed from the stem, so a slug
    that legitimately ends in a role-like word is never mangled.
    """
    original = entry.get('original_filename')
    if original:
        name = str(original)
        # original_filename is human-editable data used verbatim as a write
        # target, so it gets the same containment discipline as --dest: a bare
        # filename only. PureWindowsPath('.name') catches '/', '\\', '..' steps,
        # and drive-relative 'C:' spellings in one comparison.
        if not name or PureWindowsPath(name).name != name:
            raise ProcessError(
                f'original_filename {name!r} in the record is not a bare '
                'filename - refile will not follow path separators or drive '
                'prefixes. Fix the entry by hand to the plain name, then re-run.')
        return name
    stem = src.stem
    m = _FILENAME_SOURCE_ID_RE.search(stem)
    if m:
        stem = stem[:m.start()]
    role = entry.get('role')
    if role and str(role) != 'primary':
        suffix = '-' + _slugify(str(role))
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
    copy = entry.get('copy')
    if copy:
        suffix = '-' + _slugify(str(copy))
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
    return f'{stem or "photo"}{src.suffix}'


def _rewrite_source_type_line(text: str, old_type: str, new_type: str) -> tuple[str, int]:
    """Rewrite the frontmatter's `source_type:` value, value-exactly.

    The same discipline `_rewrite_file_line` applies to a `files:` entry, and
    for the same reason: a substring replace would also hit the word inside a
    citation, a note, or a claim value. The scan is confined to the
    frontmatter fence and matches only a top-level `source_type:` line whose
    value is the one the record was read as - a quoted or extra-spaced
    spelling still matches, a trailing YAML comment is carried across
    untouched. Returns (new_text, matches); the caller refuses on anything but
    exactly one, and re-parses the result before writing.
    """
    lines = text.split('\n')
    bounds = frontmatter_fence_span(lines)
    if bounds is None:
        return text, 0
    start, end = bounds
    matched = 0
    out = list(lines)
    for i in range(start + 1, end):
        line = lines[i]
        if not line.startswith('source_type:'):
            continue
        body = line[len('source_type:'):].rstrip('\r')
        cr = '\r' if line.endswith('\r') else ''
        # A ' #' opens a YAML comment; keep the human's note exactly as written.
        hash_at = body.find(' #')
        value, comment = (body, '') if hash_at == -1 else (body[:hash_at], body[hash_at:])
        if value.strip().strip('\'"') != old_type:
            continue
        matched += 1
        out[i] = f'source_type: {new_type}{comment}{cr}'
    return '\n'.join(out), matched


def process_refile(
    archive_root: Path,
    fha_config: dict,
    source_id: str,
    *,
    to: str,
    file_name: str | None = None,
    dest: str | None = None,
    new_type: str | None = None,
    dry_run: bool = False,
    assume_yes: bool = False,
    backup: OriginalBackup | None = None,
) -> int:
    """Move one of a source's files to the other asset root - the correction verb.

    The sanctioned CROSS-ROOT move (SPEC §12.1 carve-out): a file filed to the
    wrong root at processing is moved to the right one, its identity carriers
    re-established for the destination root's rules, and the record updated in
    the same transaction. Refuses when the file is already under the target
    root - within-root moves are free and healed by `fha reconcile`, not this.

    documents -> photos (`--to photos`): `--dest` is REQUIRED (the tool never
    invents photo-library organization); the file is renamed back to its
    photo-library name (`_photo_library_name` - the one legal rename moment,
    since photos-root files are never renamed after the crossing) and the
    `SOURCE:` keyword is embedded where the format supports it. A missing
    exiftool or an unsupported format WARNS and proceeds - the inventory still
    carries identity, and refusing would strand the correction.

    photos -> documents (`--to documents`): the move breaks the external photo
    catalog's knowledge of the file, so it confirms first ([y/N], or --yes);
    the file is renamed INTO the §13 grammar `{slug}[-{copy}][-{role}]_{S-id}`
    and lands in `--dest` or `documents/{type}/` (the M11.2 convention). The
    embedded SOURCE keyword is deliberately NOT stripped - it is harmless in
    the documents root and stripping would be a second risky write for zero
    correctness gain. `original_filename` is NOT added to the entry (that
    would need multi-key entry surgery, out of scope for v1); the dated Notes
    line carries the old name instead.

    Record updates are one atomic text write: the value-exact `file:` line
    rewrite (never substring - sibling entries must survive) plus a dated
    provenance paragraph appended to `## Notes` via the shared
    `append_paragraph_to_section` engine, both computed BEFORE anything moves
    so an unmatchable line refuses up front instead of rolling back. Line
    endings are preserved exactly (read_text_exact/write_text_exact_atomic).

    Deliberately out of scope for v1: re-typing (`refile` moves location,
    never `source_type`), same-root moves (reconcile's domain), and moving the
    RECORD file between sources/{type}/ directories.

    Returns 0 on success or dry-run, 1 when the record is not found, 3 on any
    refusal or rolled-back failure (raised as ProcessError for the CLI layer
    where convenient). Transactional: every filesystem effect registers an
    undo and any failure unwinds them in reverse.
    """
    if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
        raise ProcessError(
            f'{source_id!r} is not a valid source ID. S-ids look like '
            'S-2b3c4d5e6f - an S followed by a dash and 10 characters from the '
            'archive alphabet (0-9 and lowercase a-z, except i, l, o, u). '
            'Run `fha find <a word from the title>` to look one up.')
    sid = fmt_id_display(normalize_id(source_id))
    if new_type is not None:
        # Pure argument validation, so it belongs beside the S-id check rather
        # than down among the record-aware refusals.
        new_type = str(new_type).strip().lower()
        if new_type not in SOURCE_TYPES:
            raise ProcessError(
                f'{format_source_type_error(new_type, where="--type")} Nothing was moved.')

    record_path = find_source_record_path(archive_root, sid)
    if record_path is None:
        print(f'No source record found for {sid} under sources/ - check the id '
              f'with `fha find {sid}`.', file=sys.stderr)
        return EXIT_WARNINGS

    rec = read_record(record_path, on_decode_error=lambda p: None)
    if rec['undecodable']:
        raise ProcessError(
            f'{record_path.name} is not saved as UTF-8 text, so its files: '
            'inventory could not be read. Nothing was moved - open it and save '
            'it again choosing UTF-8 (in Notepad: Save As, then pick UTF-8 from '
            'the Encoding menu), then re-run.')
    if rec.get('parse_errors'):
        raise ProcessError(
            f'{record_path.name} has malformed frontmatter, so its files: '
            'inventory cannot be trusted. Run `fha lint` and fix it, then re-run.')
    meta = rec.get('meta') or {}
    entries = [e for e in (meta.get('files') or []) if isinstance(e, dict) and e.get('file')]
    if not entries:
        raise ProcessError(
            f'{record_path.name} lists no files - nothing to refile. '
            'A pointer-only source has no asset in the archive.')

    entry = _refile_pick_entry(entries, file_name, record_path.name)
    stored_alias = str(entry['file'])
    alias_norm = stored_alias.replace('\\', '/')
    alias_root = alias_norm.split('/', 1)[0]
    if alias_root not in ('documents', _PHOTO_DIR):
        raise ProcessError(
            f'{record_path.name} stores {stored_alias!r}, which is not under the '
            'photos or documents root - fix the entry by hand, then re-run.')
    if alias_root == to:
        raise ProcessError(
            f'{Path(alias_norm).name} is already under the {to} root. Refile only '
            'moves a file ACROSS roots (photos <-> documents); to reorganize '
            'within a root, move the file in your file browser and run '
            '`fha reconcile` to re-tie it to its record.')

    src = resolve_path(stored_alias, fha_config, archive_root)

    # P1 (audit finding, mirrors the #147-review fix to `fha source
    # clear-keyword`): the `alias_root` check above only confirms the alias
    # STARTS WITH 'documents'/'photos' AS TEXT - a hand-edited or corrupted
    # entry like 'documents/../../outside.tif' passes that check, but
    # `resolve_path` joins the alias onto the configured root with plain path
    # arithmetic and does not itself guard against a '..' segment (or a
    # doubled separator) carrying the result outside that root. Resolve both
    # sides and refuse before touching the filesystem at all if the target
    # does not actually land beneath the root it claims to be under -
    # otherwise refile would move (or, on the copy+unlink fallback, delete)
    # a file that was never part of this archive.
    #
    # The symlink-loop check runs FIRST, unconditionally - not only inside
    # the `not _is_under(...)` branch below - as of round-11 audit
    # (post-merge Codex review of #197, finding 5): on Python 3.13+,
    # `Path.resolve()`'s non-strict mode (what `_is_under` uses) stopped
    # raising for a genuine symlink loop at all - it silently returns a
    # best-effort, still-unresolved path instead (see
    # `_resolve_hits_symlink_loop`'s own docstring). A broken inventory
    # symlink can leave that best-effort path textually unchanged and
    # still sitting under `claimed_root`, so `_is_under` reports it
    # contained (True) even though nothing was actually resolved through
    # the loop - and since `not _is_under(...)` is then False, the loop
    # check that used to live inside this branch never ran at all. Control
    # fell through to `if not src.is_file():` below, whose `.is_file()`
    # DOES follow the loop for real (a genuine OS-level stat) and fails,
    # producing "is not on disk" - true only in the sense that nothing
    # could be reached, but the wrong diagnosis and the wrong remedy: the
    # file is likely sitting exactly where it belongs, and what needs
    # fixing is the broken symlink, not a `files:` entry or a missing
    # drive. Checking the loop before ever consulting `_is_under`'s result
    # - success or failure alike - closes that gap for both Python's
    # resolve() eras at once.
    claimed_root = resolve_path(alias_root, fha_config, archive_root)
    if _resolve_hits_symlink_loop(src) or _resolve_hits_symlink_loop(claimed_root):
        raise ProcessError(
            f'{stored_alias} could not be checked against the configured '
            f'{alias_root} root - this looks like a symlink loop, most '
            'likely from a corrupted or maliciously crafted filesystem '
            f'entry, not a mistyped files: entry in {record_path.name}. '
            'Find and fix or remove the offending symlink, then retry. '
            'Nothing was moved.')
    if not _is_under(src, claimed_root):
        raise ProcessError(
            f'{stored_alias} resolves outside the configured {alias_root} '
            f'folder ({claimed_root}) - this looks like a hand-edited files: '
            f'entry gone wrong (a `..` segment or a doubled slash). Fix the '
            f'entry in {record_path.name} by hand, then retry. Nothing was '
            'moved.')

    if not src.is_file():
        raise ProcessError(
            f'{stored_alias} is not on disk. If the {alias_root} folder lives on '
            'an external drive, plug it in; if the file was moved, run '
            '`fha reconcile` first so the record points at its real location, '
            'then re-run.')

    # P1 (audit finding, same source as above): confirm the file this would
    # move actually IS the requested source's own asset before touching it.
    # Inventory drift - a hand-edited or stale files: entry that still names a
    # file since reassigned to (or always belonging to) a DIFFERENT source -
    # can point one source's refile at another source's file, silently
    # relocating it (and rewriting the WRONG record's files: entry to claim
    # it) while the other source's own record is left pointing at nothing.
    # documents-root identity rides in the filename's own `_{S-id}` suffix
    # (SPEC §13 - the same convention `_filename_has_source_id`/`--more`
    # already check), and every `fha process`-produced documents-root file
    # carries it unconditionally, so its absence is refused exactly like
    # `fha source clear-keyword`'s own identity check treats a missing
    # marker. `fha reconcile` cannot repair THIS drift, though (round-2 audit
    # finding): it only re-links a `files:` entry whose stored path no longer
    # resolves on disk (TOOLING §9), and `src.is_file()` above already
    # confirmed this one does resolve - the entry is not stale, it is simply
    # wrong. The message below names the exact hand-edit instead of a command
    # that would run clean and change nothing, leaving the human to retry
    # forever.
    #
    # photos-root files are never renamed, so there is no filename signal
    # there - identity rides in the embedded SOURCE: keyword(s) instead, read
    # via `_read_all_source_keywords` (not the single-value
    # `_read_source_keyword`) so a SECOND, conflicting value - e.g. one in
    # Keywords, a different one in Subject, from a hand-edited or corrupted
    # field - is never missed just because the FIRST value happened to match
    # (round-2 audit finding: accepting on the first match let refile move an
    # ambiguous file while the OTHER named source's own inventory silently
    # kept pointing at nothing). ANY present-and-different value is refused
    # (unambiguous drift: this file is on record, at least in part, as
    # belonging to another source); a photo with no keyword at all is left to
    # the weaker (but still-fixed) containment check above rather than
    # refused on an inference this verb cannot confirm - the same "verify
    # what can actually be verified" posture refile already takes for the
    # keyword it embeds at the destination, so this verb gains no hard new
    # dependency.
    #
    # A read FAILURE is not the same as "no keyword" and must not be treated
    # as one (the P1 half of the round-2 audit finding): only
    # `ExiftoolUnavailableError` - exiftool itself missing from the machine -
    # earns the soft-fail down to the containment check, because that machine
    # truly cannot learn anything about the file. Every OTHER read failure
    # (exiftool present but this file rejected as corrupt/unsupported, or
    # invalid JSON on its stdout) means exiftool DID run and simply could not
    # confirm this file's identity; treating that the same as "no keyword
    # present" let a photo that actually carries another source's embedded id
    # slip through unverified and get moved/relabelled as this source's
    # asset. Refuse instead - the human can inspect the file (or fix whatever
    # exiftool choked on) and retry.
    if alias_root == 'documents':
        filename_sid = _filename_has_source_id(src)
        if filename_sid != sid.lower():
            carried = fmt_id_display(filename_sid) if filename_sid else 'no source id at all'
            raise ProcessError(
                f"{stored_alias}'s own filename carries {carried}, not {sid} - "
                f"{record_path.name}'s files: entry looks like inventory drift "
                f'(it names a file that belongs to a different source), and '
                f'moving it would relocate the WRONG document. `fha reconcile` '
                'cannot fix this: it only re-links a files: entry whose path no '
                f'longer resolves on disk, and {stored_alias} still resolves - '
                'it is simply the wrong file for this record. Fix it by hand: '
                f'open {record_path.name}, find the `files:` entry `file: '
                f'{stored_alias}`, and either point it at the correct on-disk '
                f'file for this source (one whose own filename ends `_{sid}`) '
                'or delete the entry if no such file exists. Nothing was moved.')
    else:
        try:
            embedded_sids = _read_all_source_keywords(src)
        except ExiftoolUnavailableError:
            embedded_sids = []
        except RuntimeError as e:
            raise ProcessError(
                f'{stored_alias} could not be read to confirm which source it '
                f'belongs to ({e}) - refusing to move it without checking, '
                'since a hidden identity conflict would relocate the WRONG '
                'photo. Make sure exiftool can open the file (it may be '
                'corrupt or an unsupported format), then retry. Nothing was '
                'moved.') from e
        conflicting = sorted({s for s in embedded_sids if s != sid.lower()})
        if conflicting:
            # Same dead-end shape as the documents-root branch above (issue
            # #170 finding 3, round-3 audit): `fha reconcile`'s document pass
            # only considers documents-alias entries (its own docstring:
            # "the photos side has its own [machinery]"), and its photo pass
            # only re-ties `fha photoindex`'s own catalog to files on disk -
            # neither one touches THIS record's `files:` entry. Suggesting it
            # here would run clean and change nothing, leaving the human to
            # retry forever. Name the exact entry to fix by hand instead.
            carried = ', '.join(fmt_id_display(s) for s in conflicting)
            raise ProcessError(
                f"{stored_alias}'s own embedded SOURCE keyword(s) carry "
                f'{carried}, not {sid} - '
                f"{record_path.name}'s files: entry looks like inventory "
                f'drift (it names a file that belongs to a different '
                f'source), and moving it would relocate the WRONG photo. '
                '`fha reconcile` cannot fix this: its document pass ignores '
                'photos/ aliases, and its photo pass only re-ties the photo '
                f'catalog, not this record. Fix it by hand: open '
                f'{record_path.name}, find the `files:` entry `file: '
                f'{stored_alias}`, and either point it at the correct '
                f'on-disk photo for this source (untagged, or whose '
                f'embedded SOURCE keyword already names {sid} - either is '
                f'fine) or delete the entry if no such photo exists. '
                'Nothing was moved.')

    # The source's type, resolved. A type that is wrong for the destination
    # root is part of the misfiling this verb corrects, so refile carries the
    # type across with the file rather than leaving it as a hand-edit
    # `fha lint` never flags (#59). Placed after the refusals above so
    # "already under the documents root" and "not on disk" keep their own,
    # better-fitting messages.
    current_type = str(meta.get('source_type') or _DEFAULT_DOCUMENT_TYPE).strip()
    current_type = current_type or _DEFAULT_DOCUMENT_TYPE
    if new_type is None and to == 'documents' and current_type == _PHOTO_SOURCE_TYPE:
        # The junk-folder case: with the record still typed `photo`, the default
        # destination would be a `documents/photos/` folder invented for the
        # purpose, and the record would stay in `sources/photos/` describing
        # something that is no longer in the photo library. What it is instead
        # is not derivable - a census sheet and a deed scan look identical to a
        # tool - so ask rather than guess, in the only way a command line can:
        # name the flag and a real example.
        raise ProcessError(
            f'{record_path.name} is still typed photo, and a scan that leaves the '
            'photo library is not a family photo any more. Say what kind of record '
            'it is and refile will carry the type across with the file, e.g. '
            f'`fha process refile {sid} --to documents --type census`. '
            f'Valid types: {source_type_list()}. Nothing was moved.')
    resolved_type = new_type or current_type

    # Plan the destination. Both directions compute everything - destination
    # directory, final name, record surgery - before any byte moves, so every
    # refusal happens with the archive untouched.
    if to == _PHOTO_DIR:
        photos_root = resolve_path(_PHOTO_DIR, fha_config, archive_root)
        if not photos_root.is_dir():
            # The destination root must be REACHABLE before planning. Without this,
            # the later dest_dir.mkdir(parents=True) would recreate an unplugged
            # external drive's mount path on the local disk and move the original
            # into it - hiding the file under the real drive when it reconnects.
            raise ProcessError(
                f'the photos root is not reachable at {photos_root} - if it lives '
                'on an external drive, plug it in, then re-run. (Refusing rather '
                'than recreating the mount path on the local disk, which would '
                'strand the file when the drive reconnects.)')
        if not dest:
            raise ProcessError(
                '--dest is required when refiling into the photo library: fha '
                'never invents photo-library organization - the folder choice is '
                'yours. Name the subfolder the photo belongs in, e.g. '
                '--dest 1880s or --dest "Family/Hartley".')
        dest_dir = _validate_dest_subpath(photos_root, dest, _PHOTO_DIR)
        new_name = _photo_library_name(src, entry)
    else:
        documents_root = resolve_path('documents', fha_config, archive_root)
        if not documents_root.is_dir():
            # Same reachability guard as the photos direction: never recreate an
            # offline root's mount path on the local disk under the moved file.
            raise ProcessError(
                f'the documents root is not reachable at {documents_root} - if it '
                'lives on an external drive, plug it in, then re-run. (Refusing '
                'rather than recreating the mount path on the local disk, which '
                'would strand the file when the drive reconnects.)')
        if dest:
            dest_dir = _validate_dest_subpath(documents_root, dest, 'documents')
        else:
            dest_dir = documents_root / _record_subdir(resolved_type)
        rec_stem = record_path.stem
        m = _FILENAME_SOURCE_ID_RE.search(rec_stem)
        slug = rec_stem[:m.start()] if m else _slugify(rec_stem)
        suffix = ''
        role = entry.get('role')
        if role and str(role) != 'primary':
            suffix = f'-{_slugify(str(role))}'
        copy = entry.get('copy')
        if copy:
            suffix = f'-{_slugify(str(copy))}{suffix}'
        new_name = f'{slug}{suffix}_{sid}{src.suffix}'

    dest_path = dest_dir / new_name
    if dest_path.exists():
        raise ProcessError(
            f'destination already exists: {_rel(dest_path, archive_root)} - '
            'move or rename that file first, then re-run.')
    new_alias = path_to_alias(dest_path, to, fha_config, archive_root)

    # Record surgery, computed up front: the value-exact file: line rewrite
    # plus the dated Notes provenance paragraph, as ONE new text.
    old_text = read_text_exact(record_path)
    rewritten, matched = _rewrite_file_line(old_text, stored_alias, new_alias)
    if matched == 0:
        raise ProcessError(
            f'{record_path.name}: could not find the files: line for '
            f'{stored_alias!r} - the entry may be spelled differently in the '
            'raw text. Fix it by hand or run `fha lint`, then re-run. '
            'Nothing was moved.')
    if matched > 1:
        raise ProcessError(
            f'{record_path.name} lists {stored_alias!r} on more than one files: '
            'line, so the rewrite cannot know which entry to touch - refusing. '
            'Fix the duplicate entry by hand (`fha lint` names the spot), then '
            're-run. Nothing was moved.')
    if _file_line_count(rewritten) != _file_line_count(old_text):
        raise ProcessError(
            f'{record_path.name}: rewriting the files: entry would change the '
            'shape of its files: list - refusing. Fix the entry by hand '
            '(`fha lint` names the spot). Nothing was moved.')

    # A re-type is two writes that must not come apart: the frontmatter field,
    # and the record file's own folder (SPEC §14 files a source at
    # `sources/{type}/`). Both are planned here, before any byte moves, so a
    # collision refuses with the archive untouched.
    new_record_path: Path | None = None
    if resolved_type != current_type:
        rewritten, type_matched = _rewrite_source_type_line(
            rewritten, current_type, resolved_type)
        if type_matched != 1:
            raise ProcessError(
                f'{record_path.name}: could not find its single `source_type: '
                f'{current_type}` line to rewrite - the record may spell it '
                'differently, or list it twice. Fix it by hand (`fha lint` names '
                'the spot), then re-run. Nothing was moved.')
        new_record_path = (archive_root / 'sources' / _record_subdir(resolved_type)
                           / record_path.name)
        if new_record_path.exists():
            raise ProcessError(
                f'the record cannot move to {_rel(new_record_path, archive_root)} - '
                'a file is already there. Move or rename it first, then re-run. '
                'Nothing was moved.')

    if to == _PHOTO_DIR:
        note = (f'Refiled {_today()}: {stored_alias} -> {new_alias} '
                '(fha process refile). The file was living in the records '
                'drawer but belongs with the photo library.')
    else:
        note = (f'Refiled {_today()}: {stored_alias} -> {new_alias} '
                '(fha process refile). The photo was living in the photo '
                'library but belongs with the records; it was previously '
                f'named {src.name}.')

    if resolved_type != current_type:
        note += (f' Its type was corrected from {current_type} to {resolved_type}, '
                 f'so the record moved to sources/{_record_subdir(resolved_type)}/ '
                 'with it.')

    rew_lines = rewritten.split('\n')
    bounds = frontmatter_fence_span(rew_lines)
    body_start = (bounds[1] + 1) if bounds is not None else 0
    cr = '\r' if '\r\n' in old_text else ''
    new_lines, _created, _old = append_paragraph_to_section(
        rew_lines, body_start, 'Notes', note, cr)
    final_text = '\n'.join(new_lines)

    # Belt-and-braces guards (the run_source_note / claim-writer posture): the
    # edit must leave a sound Claims block sound, and the rewritten frontmatter
    # must round-trip to the NEW alias exactly. A value-check, not a parse-check:
    # a YAML-hostile path (a ' #' that opens a comment) can still PARSE while
    # silently truncating the value, so the guard confirms the parsed files:
    # list actually carries new_alias once and no longer carries the old one.
    if claims_edit_problem(old_text) is None and claims_edit_problem(final_text) is not None:
        raise ProcessError(
            f'refusing: the edit would leave {record_path.name}\'s ## Claims '
            'block broken. Nothing was moved or written - open the record and '
            'check it by hand, then run `fha lint`.')
    reparsed = parse_frontmatter_strict(final_text)
    reparsed_files = ([str(e.get('file', '')) for e in (reparsed.get('files') or [])
                       if isinstance(e, dict)] if reparsed else [])
    if reparsed is None or reparsed_files.count(new_alias) != 1 or stored_alias in reparsed_files:
        raise ProcessError(
            f'refusing: the new path {new_alias!r} would not survive cleanly in '
            f'{record_path.name} - use a simpler --dest (letters, digits, '
            'hyphens). Nothing was moved or written.')
    # Only when this run rewrote the type: a record that never carried a
    # `source_type:` line is a lint problem, not a reason to refuse a move
    # this verb was not asked to re-type.
    if new_record_path is not None and str(reparsed.get('source_type') or '') != resolved_type:
        raise ProcessError(
            f'refusing: {record_path.name} would not read back as source_type '
            f'{resolved_type} after the edit. Open the record and check its '
            'source_type line by hand, then re-run. Nothing was moved or written.')

    embed_keyword = to == _PHOTO_DIR
    keyword_supported = dest_path.suffix.lower() in PHOTO_EXTENSIONS

    if dry_run:
        if embed_keyword and keyword_supported:
            _open_backup(archive_root, fha_config, backup)
        if to == 'documents':
            print('[dry-run] Note: your photo tool (Lightroom) would show this '
                  'photo as missing after the move - removing it from the '
                  'catalog stays your job; fha never touches the catalog.')
        if not dest_dir.exists():
            print(f'[dry-run] Would create {_rel(dest_dir, archive_root)}/')
        print(f'[dry-run] Would move {stored_alias} -> {new_alias}')
        if src.name != new_name:
            why = ('restoring its photo-library name' if to == _PHOTO_DIR
                   else 'renaming it into the {slug}_{S-id} grammar')
            print(f'[dry-run] Would rename {src.name} -> {new_name} at the crossing ({why})')
        if embed_keyword:
            if keyword_supported:
                print(f'[dry-run] Would embed the SOURCE: {sid} keyword in {new_name}')
            else:
                print(f'[dry-run] Keywords are not supported for {src.suffix} files - '
                      'the record\'s files: inventory would carry the identity alone.')
        print(f'[dry-run] Would rewrite the files: entry in '
              f'{_rel(record_path, archive_root)}: {stored_alias} -> {new_alias}')
        if new_record_path is not None:
            print(f'[dry-run] Would retype the record {current_type} -> {resolved_type} '
                  f'and move it to {_rel(new_record_path, archive_root)}')
        print(f'[dry-run] Would add a Notes line: {note}')
        return EXIT_CLEAN

    # photos -> documents breaks the external catalog's knowledge of the file:
    # warn, then require a human yes (or --yes) before anything moves.
    if to == 'documents':
        print('Your photo tool (Lightroom) will show this photo as missing '
              'after the move - remove it from your catalog yourself; fha '
              'never touches the catalog.')
        if not assume_yes:
            if not _stdin_is_interactive():
                raise ProcessError(
                    'moving a photo out of the photo library needs a '
                    'confirmation, and there is no one here to ask. Re-run '
                    'with --yes to confirm.')
            try:
                answer = _prompt('Move it out of the photo library? [y/N] ').strip().lower()
            except EOFError:
                # No answer is reachable (a closed stdin, or a scheduler run
                # where isatty() lies). That is truly non-interactive: refuse
                # with the same crafted cause and next step, not the generic
                # catch-all's misleading "run fha lint".
                raise ProcessError(
                    'moving a photo out of the photo library needs a '
                    'confirmation, and there is no one here to ask. Re-run '
                    'with --yes to confirm.')
            if answer not in ('y', 'yes'):
                print('Not refiled - nothing changed.')
                return EXIT_CLEAN

    # Explicit transactional state, not a list of opaque undo lambdas: a rollback
    # that hits a snag (the asset drive unplugged mid-undo) has to tell the owner
    # WHERE the file ended up and WHERE the record points, and only named state
    # can say that. The order below is the fix for the reviewer-named hazard - the
    # file is moved back BEFORE the record is rewritten, and the record is then
    # pointed at the file's REAL location, so a rollback that cannot finish still
    # leaves the file and the record agreeing (a consistent archive) instead of a
    # record pointing at a now-missing old path while the file sits in the
    # destination root - a split a file browser never surfaces.
    created_dirs: list[Path] = []
    record_moved = False
    file_moved = False
    keyword_warning: str | None = None
    keyword_embedded = False
    # One policy object for the whole transaction: the forward keyword write and
    # the rollback that strips it must share it, or the run would take two
    # safety copies and say "no safety copies are being kept" twice. Opened only
    # when this refile actually writes into the file - a move to the documents
    # root renames and never touches the file's contents, so warning about
    # safety copies there would be about a risk this run does not run.
    if embed_keyword and keyword_supported:
        backup = _open_backup(archive_root, fha_config, backup)
    try:
        # Track which destination folders this run creates, so a rollback can
        # remove them again (deepest-first, once the file has moved back out).
        probe = dest_dir
        while not probe.exists() and probe != probe.parent:
            created_dirs.append(probe)
            probe = probe.parent
        dest_dir.mkdir(parents=True, exist_ok=True)

        _move_file(src, dest_path)
        file_moved = True

        if embed_keyword:
            if not keyword_supported:
                keyword_warning = (
                    f'WARNING: keywords are not supported for {src.suffix} files, '
                    f'so the SOURCE: {sid} keyword was not embedded in {new_name}. '
                    'The record\'s files: inventory still carries the identity.')
            else:
                try:
                    err = _run_exiftool_embed_source(
                        dest_path, sid, backup=backup)
                except RuntimeError as e:
                    keyword_warning = (
                        f'WARNING: {e}\nThe SOURCE: {sid} keyword was not '
                        f'embedded in {new_name} - the record\'s files: '
                        'inventory still carries the identity. Install '
                        'exiftool and re-process the keyword later.')
                else:
                    if err is not None:
                        keyword_warning = (
                            f'WARNING: exiftool could not embed the SOURCE '
                            f'keyword in {new_name}: {err}. The record\'s '
                            'files: inventory still carries the identity.')
                    else:
                        keyword_embedded = True

        # Atomic (temp + os.replace): refile is documented as one atomic
        # transaction (BUILD M11.6), so a mid-write failure after the asset has
        # moved must leave the record fully written or untouched, never torn.
        write_text_exact_atomic(record_path, reapply_newline(final_text, old_text))

        # The record's own move comes last: it is the cheapest step to undo
        # (a rename back), and doing it after the text write means the write
        # above always has one, known target.
        if new_record_path is not None:
            probe = new_record_path.parent
            while not probe.exists() and probe != probe.parent:
                created_dirs.append(probe)
                probe = probe.parent
            new_record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.rename(new_record_path)
            record_moved = True
    except Exception as e:
        # Best-effort rollback that never claims more than it did. Every step is
        # attempted even when an earlier one fails, failures are collected rather
        # than swallowed, and the record is written LAST, pointing at wherever the
        # file actually is now.
        undone: list[str] = []
        move_back_error: Exception | None = None
        keyword_cleanup_error: str | None = None

        # 1. Strip the SOURCE keyword this run embedded, targeting the destination
        #    where the file still sits (before the move-back), so the removal lands
        #    on the file's current path and undoes exactly what the embed added.
        if keyword_embedded:
            try:
                kw_err = _run_exiftool_remove_source(
                    dest_path, sid, backup=backup)
            except RuntimeError as kw_exc:
                kw_err = str(kw_exc)
            if kw_err is None:
                undone.append(f'removed the SOURCE: {sid} keyword from {new_name}')
            else:
                # The embed landed but its inverse failed: the file still carries
                # metadata THIS run added. Keep the error - a rollback that moved
                # the file home and restored the record is NOT clean while that
                # keyword remains, and the clean-branch report below must say so
                # rather than claim "nothing changed".
                keyword_cleanup_error = kw_err

        # 2. Move the file home. If the asset drive vanished this fails here, and
        #    file_home stays at the destination so step 3 points the record at the
        #    file's real location instead of the now-missing old one.
        file_home = src
        if file_moved:
            try:
                _move_file(dest_path, src)
                undone.append(f'moved {new_name} back to {_rel(src, archive_root)}')
            except Exception as move_exc:
                file_home = dest_path
                move_back_error = move_exc

        # 2b. Put the record file back in its own folder before step 3 writes to
        #     it, so the restore lands on the record's real path either way. If
        #     even that fails, step 3 writes wherever the record actually is.
        record_home = record_path
        if record_moved and new_record_path is not None and new_record_path.exists():
            try:
                new_record_path.rename(record_path)
                undone.append(f'moved the record back to {_rel(record_path, archive_root)}')
            except OSError:
                record_home = new_record_path

        # 3. Point the record at the file's real location. Move-back done -> old
        #    alias (a full undo); move-back failed -> the file is still in the
        #    destination, so the record must point THERE to keep the archive
        #    consistent. Writing the record last means a failed move-back can never
        #    strand the record on the missing old path.
        if file_home == src:
            record_text = old_text
        else:
            record_text = reapply_newline(final_text, old_text)
        record_consistent = True
        try:
            # Atomic, same as the forward write: the rollback restore must not
            # be able to tear the record it is trying to make whole.
            write_text_exact_atomic(record_home, record_text)
        except Exception:
            record_consistent = False

        # 3b. A forward move that failed mid-copy can leave a partial destination
        #     file that `_move_file` could not remove (a locked handle on Windows).
        #     It is not `file_moved`, so steps 1-3 never touched it; remove it now,
        #     and if that still fails, name it so no report below claims a clean
        #     rollback while an orphan sits in the destination root. Done before
        #     step 4 so clearing the stray lets a created folder rmdir cleanly.
        stray_partial: str | None = None
        if not file_moved and dest_path.exists():
            try:
                dest_path.unlink()
            except OSError:
                stray_partial = _rel(dest_path, archive_root)

        # 4. Drop any now-empty folders this run created (deepest first). One that
        #    still holds the stranded file simply stays - it is reported below.
        for d in reversed(created_dirs):
            try:
                d.rmdir()
            except OSError:
                pass

        rel_record = _rel(record_home, archive_root)
        if move_back_error is None and record_consistent:
            # File is home AND the record was restored to its pre-refile text:
            # the only state that is truly a clean, complete rollback. Both
            # conditions matter - a successful move-back with a FAILED record
            # restore (below) leaves the record truncated, and calling that
            # "nothing changed" would send the owner away from a damaged file.
            print(f'ERROR: refile failed, rolled back: {e}', file=sys.stderr)
            if undone:
                print('Undone: ' + '; '.join(undone) + '.', file=sys.stderr)
            if stray_partial is not None:
                # The record is back to its original text, but the failed copy's
                # partial destination could not be removed - name it and the
                # manual step so this is not read as "nothing changed".
                print(f'{rel_record} is unchanged, but a partial copy was left at '
                      f'{stray_partial} and could not be removed automatically - '
                      f'remove it by hand (e.g. `rm {stray_partial}`), then re-run.',
                      file=sys.stderr)
            elif keyword_cleanup_error is not None:
                # Record and file location are restored, but the SOURCE: keyword
                # this run embedded could not be stripped - the file at its home
                # path still carries metadata the failed command added. Name it
                # and the exact exiftool cleanup instead of "nothing changed".
                rel_home = _rel(file_home, archive_root)
                print(f'{rel_record} is unchanged and the file is back at {rel_home}, '
                      f'but the SOURCE: {sid} keyword this run embedded could not be '
                      f'removed ({keyword_cleanup_error}) - the file still carries it. '
                      f'Remove it by hand: `exiftool -keywords-="SOURCE: {sid}" '
                      f'-overwrite_original_in_place "{rel_home}"`, then re-run.',
                      file=sys.stderr)
            else:
                print(f'Nothing was left changed in {rel_record}.', file=sys.stderr)
            return EXIT_FAILURE

        if move_back_error is None:
            # The file is back home, but rewriting the record to its original
            # text failed, so the record on disk is truncated or half-written
            # while the file sits at its original location. Never report this as
            # a clean rollback: name the damaged record and the one recovery that
            # makes it whole, restoring the pre-refile text from version control
            # or a backup.
            rel_src = _rel(src, archive_root)
            print(f'ERROR: refile failed and the record could not be restored: {e}',
                  file=sys.stderr)
            if undone:
                print('Done during rollback: ' + '; '.join(undone) + '.', file=sys.stderr)
            print(f'The file is back at its original location ({rel_src}), but '
                  f'{rel_record} is damaged: its original text could not be written '
                  'back and the record may be truncated.', file=sys.stderr)
            print(f'Next: restore {rel_record} from git or a backup (e.g. '
                  f'`git checkout -- {rel_record}`), then run `fha lint` to confirm '
                  'the record is whole again.', file=sys.stderr)
            return EXIT_FAILURE

        # The move-back could not finish. Report the true on-disk state plainly.
        rel_dest = _rel(dest_path, archive_root)
        rel_src = _rel(src, archive_root)
        print(f'ERROR: refile failed, and the file could not be moved back: {e}',
              file=sys.stderr)
        print(f'The {alias_root} location did not answer while moving the file '
              f'home: {move_back_error}', file=sys.stderr)
        if undone:
            print('Done during rollback: ' + '; '.join(undone) + '.', file=sys.stderr)
        if record_consistent:
            # Healed to a consistent "still refiled" state: file at the destination,
            # record points at the destination. Honest - the refile stands - and
            # the reverse command is the one clean way to finish undoing it later.
            print(f'To keep the archive consistent, the file was LEFT at {rel_dest} '
                  f'and {rel_record} now points there. Nothing is broken, but the '
                  'refile you were undoing is still in place.', file=sys.stderr)
            reverse = f'fha process refile {sid} --to {alias_root}'
            if alias_root == _PHOTO_DIR:
                reverse += ' --dest <folder>'
            print(f'Next: reconnect the {alias_root} location, then run '
                  f'`{reverse}` to move it back.', file=sys.stderr)
        else:
            # Could not even re-point the record: a genuine split. Spell out both
            # halves and the manual repair so the owner is never left guessing.
            print(f'WARNING: the archive is now INCONSISTENT: the file is at '
                  f'{rel_dest}, but {rel_record} still lists {stored_alias} '
                  f'({rel_src}), which is not where the file is.', file=sys.stderr)
            print(f'Next: reconnect the {alias_root} location, move {rel_dest} back '
                  f'to {rel_src} by hand, then run `fha reconcile` to re-tie the '
                  'record to the file. Run `fha doctor` to confirm it is clean '
                  'again.', file=sys.stderr)
        return EXIT_FAILURE

    if backup is not None:
        _flush_backup_messages(backup)
    print(f'Refiled {stored_alias} -> {new_alias}')
    if src.name != new_name:
        print(f'Renamed {src.name} -> {new_name} at the crossing')
    if keyword_embedded:
        print(f'Embedded SOURCE: {sid} in {new_name}')
    if keyword_warning:
        print(keyword_warning, file=sys.stderr)
    final_record_path = new_record_path if record_moved else record_path
    print(f'Updated the files: entry and added a Notes line in '
          f'{_rel(final_record_path, archive_root)}')
    if record_moved and new_record_path is not None:
        print(f'Retyped the source {current_type} -> {resolved_type} and moved its '
              f'record to {_rel(new_record_path, archive_root)}')
    print('Next: run `fha index` so searches see the new location, then '
          '`fha photoindex` to refresh the photo catalog'
          + (' (this file is new to it).' if to == _PHOTO_DIR
             else ' (its old row clears on the next scan).'))
    if to == _PHOTO_DIR:
        print(f'Your photo tool (Lightroom) does not know about this file yet - '
              f'import/synchronize {_rel(dest_dir, archive_root)}/ there.')
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
    """Display a path relative to the archive root when possible, else as posix.

    Also falls back on `RuntimeError` (issue #170 finding 2, extended -
    Codex review round-8 audit), not just `ValueError`/`OSError`: every
    "not under the configured root" `ProcessError` message in this file
    calls `_rel(root, archive_root)` to show the root's location, and that
    call is built EAGERLY as part of the message string - even when the
    containment check that triggered it (`_require_contained`/
    `_is_under_strict`) is about to prefer a different, symlink-specific
    error instead. Before this, a symlink loop on `root` itself made this
    display call raise an uncaught `RuntimeError` while constructing the
    (ultimately discarded) genuine-failure message, crashing with a raw
    traceback instead of ever reaching the clean symlink-loop refusal -
    the exact failure mode `_is_under` was already fixed to avoid (round-3
    audit), reached here through a different function. A path that cannot
    be resolved for display, loop or otherwise, is shown as-is.
    """
    try:
        return path.resolve().relative_to(archive_root.resolve()).as_posix()
    except (ValueError, OSError, RuntimeError):
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

    Raises `ProcessError` instead when RESOLVING the path itself - not
    finding it - is what fails: a symlink loop under either candidate
    location (adversarial review of PR #170, extended - every other
    containment/resolution check in this file already distinguishes "not
    there" from "could not even be checked because of a broken symlink" via
    `_resolve_hits_symlink_loop`; this is the one caller of a bare
    `Path.resolve()` in the whole module that still did not, so a symlink
    loop under the very first file a user names crashed with a raw,
    unhelpful `RuntimeError` before any of that hardening ever ran).
    """
    def found(p: Path) -> bool:
        return p.is_file() if require_file else p.exists()

    def resolved(p: Path, *, where: str) -> Path:
        if _resolve_hits_symlink_loop(p):
            raise ProcessError(
                f'{what} could not be resolved {where}: {raw} - this looks '
                'like a symlink loop, not a missing file. Find and fix (or '
                'remove) the broken symlink, then retry.'
            )
        return p.resolve()

    raw_path = Path(raw)
    primary = resolved(raw_path, where='as typed')
    if found(primary):
        return primary, None
    if not raw_path.is_absolute():
        retry = resolved(archive_root / raw_path, where='inside your archive')
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
  fha process refile <S-id> --to photos|documents [--dest SUB]
                                            Move a filed file to the other root
  fha process <file> --dry-run              Preview, write nothing

Documents are renamed with their new ID (the old name is kept as provenance);
photos are never renamed. This is the deterministic step; drafting claims and
reviewing them come after, through the process-source and review-claims skills."""


_REFILE_CLI_DESCRIPTION = """\
Move a filed source file to the other asset root - the filing correction.

  fha process refile S-2b3c4d5e6f --to photos --dest 1880s    records drawer -> photo library
  fha process refile S-2b3c4d5e6f --to documents --type census   photo library -> records
  fha process refile S-2b3c4d5e6f --file scan-back.jpg --to photos --dest 1880s

Fixes a filing decision after the fact: a scan processed as a document that
belongs in the photo library, or a photo that is really a record. The file is
moved, renamed for its new root's rules, and the source record updated, all in
one transaction. Reorganizing WITHIN a root is not this command - move files
freely and run `fha reconcile`. Preview with --dry-run first."""


def build_process_refile_parser() -> argparse.ArgumentParser:
    """The `fha process refile` parser, exposed for fha.py's early interception.

    `fha process` takes a positional FILE, so its parser would read 'refile' as
    a file path and choke on the S-id that follows. The dispatcher intercepts
    `fha process refile …` before argparse ever sees it and routes it here -
    the same mechanism `fha claim new` and `fha gedcom import` use
    (`fha.py::_intercept_process_refile`). `python tools/process.py refile …`
    reaches it through `_standalone_main`'s own leading-token check.
    """
    p = argparse.ArgumentParser(
        prog='fha process refile',
        description=_REFILE_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('source_id', metavar='S-ID',
                   help='The source whose file is filed in the wrong root, e.g. S-2b3c4d5e6f')
    p.add_argument('--to', required=True, choices=('photos', 'documents'),
                   help='Which root the file belongs in')
    p.add_argument('--file', metavar='NAME', dest='file_name',
                   help='Which of the record\'s files to move, by name - needed '
                        'only when the record lists more than one')
    p.add_argument('--dest', metavar='SUBPATH',
                   help='Destination folder under the target root. Required with '
                        '--to photos (e.g. "1880s" or "Family/Hartley"); with '
                        '--to documents it defaults to documents/{type}/')
    p.add_argument('--type', metavar='TYPE', dest='new_type',
                   help='What kind of source this really is, e.g. census. Rewrites '
                        'source_type: and moves the record to sources/{type}/ in the '
                        'same transaction. Required with --to documents when the '
                        'record is still typed photo - a scan leaving the photo '
                        'library is not a family photo any more')
    p.add_argument('--yes', action='store_true',
                   help='Skip the photo-catalog confirmation (--to documents)')
    p.add_argument('--dry-run', action='store_true', help='Preview without writing')
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.set_defaults(func=_cmd_refile)
    return p


def _cmd_refile(args: argparse.Namespace) -> int:
    """CLI wrapper for `process_refile`: root/config/working-copy gates + rendering.

    Exit codes (the refile contract, narrower than the main process command's):
    0 success or dry-run, 1 record-not-found or working-copy mode, 3 every
    refusal and any rolled-back failure. All exceptions are translated to plain
    text here - no traceback ever reaches the user.
    """
    archive_root = resolve_root_arg(args, command='fha process refile')
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    if is_working_copy(archive_root):
        print('fha process refile is not available in working-copy mode - '
              'the photo and document files are on the main machine. '
              'Run this command there.', file=sys.stderr)
        return EXIT_WARNINGS
    try:
        return process_refile(
            archive_root, fha_config, args.source_id,
            to=args.to,
            file_name=getattr(args, 'file_name', None),
            dest=getattr(args, 'dest', None),
            new_type=getattr(args, 'new_type', None),
            dry_run=bool(getattr(args, 'dry_run', False)),
            assume_yes=bool(getattr(args, 'yes', False)),
        )
    except (ProcessError, RuntimeError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    except Exception as e:
        # Planning is read-only, so an unexpected failure here changed nothing;
        # the transactional block inside process_refile handles its own rollback.
        print(f'ERROR: refile hit an unexpected problem: {e}. '
              'Run `fha lint` to check the record, then re-run.', file=sys.stderr)
        return EXIT_FAILURE


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
                   help=f'What kind of source this is, e.g. census (default: '
                        f'{_DEFAULT_DOCUMENT_TYPE}). Any type but photo files the '
                        'asset in the documents root whatever its extension, so a '
                        'scanned record supplied as a .jpg is filed as a record. A '
                        'file already in the photo library is never moved out of it: '
                        'the record is typed as asked and `fha process refile` is '
                        'named as the way to move the file')
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
    try:
        file_path, path_error = _resolve_input_file(args.file, archive_root)
    except ProcessError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_ERRORS
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

    # Validate --type once, before any file I/O and before any branch. It used
    # to be checked only inside the document branch, so an unknown --type on an
    # image was accepted in silence along with everything else the flag said
    # (#59); the vocabulary is the same one whatever the file turns out to be
    # (SPEC §14), so the check belongs here.
    source_type = getattr(args, 'source_type', None)
    if source_type is not None:
        source_type = str(source_type).strip().lower()
        if source_type not in SOURCE_TYPES:
            print(f'ERROR: {format_source_type_error(source_type, where="--type")}',
                  file=sys.stderr)
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
                    source_type=source_type,
                )
            return process_folder(
                archive_root, fha_config, file_path,
                source_date=source_date, dry_run=dry_run,
                source_type=source_type,
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
                    source_type=source_type, slug=args.slug, title=args.title,
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
        # Resolve --type/the sidecar hint to the SAME effective classification
        # _relocate_from_inbox would derive internally, and pass it explicitly
        # rather than let the primary and (below) any back-sibling relocation
        # each re-derive their own answer. Before this, a back sibling's
        # relocation call had no sidecar to hint from (a back scan carries no
        # sidecar of its own) and fell back to the raw --type (often None),
        # so a sidecar-hinted document primary (e.g. `census.jpg` + a stub
        # saying `source_type: census`) could route to documents/ while its
        # `-back` sibling, seeing no hint, routed to photos/ instead - two
        # different roots for one physical item (#113 follow-up).
        relocate_type = source_type or _sidecar_hinted_source_type(sidecar_path)
        file_path, sidecar_path, relocate_undo = _relocate_from_inbox(
            archive_root, fha_config, file_path, sidecar_path,
            source_type=relocate_type, dry_run=dry_run,
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

    # An unambiguous `-back`/`_back` sibling (#113) may still be sitting
    # wherever the PRIMARY itself was found - its own inbox folder, if that is
    # where it came from - so it needs the identical inbox-relocation dance
    # the primary just got, run here rather than inside process_document,
    # which has no way to know the primary's pre-move location once it has
    # already been relocated. Set (and undone) alongside relocate_undo below;
    # stays None whenever nothing is found (the common case, and every path
    # other than a plain single document).
    back_relocate_undo = None
    back_sibling = None

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
                                  role, copy, dry_run=dry_run, real_path=real_path,
                                  source_type=source_type)
        else:
            kind = classify_asset(file_path, fha_config, archive_root,
                                  source_type=source_type)
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
                    dry_run=dry_run, source_type=source_type, people=people or None,
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
                    back_src = _find_back_sibling(pre_move_path)
                    if back_src is not None:
                        # Same resolved classification as the primary's own
                        # relocation just above (relocate_type, not the raw
                        # --type) - see that call's comment. A back sibling
                        # carries no sidecar of its own to hint from, so
                        # without this it silently lost the primary's hint
                        # and could land in the wrong root (#1 above).
                        back_sibling, _, back_relocate_undo = _relocate_from_inbox(
                            archive_root, fha_config, back_src, None,
                            source_type=relocate_type, dry_run=dry_run,
                        )
                    rc = process_document(
                        archive_root, fha_config, file_path,
                        source_type=source_type or _DEFAULT_DOCUMENT_TYPE,
                        slug=args.slug, title=args.title,
                        source_date=source_date, dry_run=dry_run,
                        real_path=real_path,
                        source_id=source_id_override, report=mint_report,
                        back_sibling=back_sibling,
                    )
        if rc != EXIT_CLEAN and relocate_undo is not None:
            relocate_undo()
        if rc != EXIT_CLEAN and back_relocate_undo is not None:
            back_relocate_undo()
        args.result_source_id = mint_report.get('source_id')
        return rc
    except ProcessError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        if relocate_undo is not None:
            relocate_undo()
        if back_relocate_undo is not None:
            back_relocate_undo()
        return EXIT_ERRORS
    except RuntimeError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        if relocate_undo is not None:
            relocate_undo()
        if back_relocate_undo is not None:
            back_relocate_undo()
        return EXIT_FAILURE


# ── Standalone ────────────────────────────────────────────────────────────────

def _standalone_main(argv: list[str] | None = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    # 'refile' leads its own subcommand (the S-id follows it), so a leading
    # token check is enough here; the full flag-tolerant routing for
    # `fha process [--root …] refile …` lives in fha.py's interceptor.
    if argv and argv[0] == 'refile':
        args = build_process_refile_parser().parse_args(argv[1:])
        return _cmd_refile(args)
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
