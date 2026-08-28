#!/usr/bin/env python3
"""
reorganize.py - fha reorganize: bulk-tidy the documents/ root (#107).

  fha reorganize [--root PATH] [--apply] [--yes] [--dry-run]
                 [--batch-size N] [--group-threshold N] [--limit N]

The story: after a large inbox migration processes a hundred-plus new
sources in one sitting, `documents/` ends up with several hundred files -
some loose at the top level, some one level down - while `sources/` (one
record per source) stays clean throughout, because nothing ever had to
plan or perform the documents-side tidy-up. Before this tool, the fix was
by hand: find each file's owning record, edit its `files:` path, move the
file, and hope nothing was missed - correct but tedious and risky at the
scale of hundreds of files (the filed issue's own case, done once by hand
because no tool existed).

**Is there still a "loose at the root" problem for FRESH processing?** No -
as of the 2026-07-22 owner decision (`process_document`/`process_bundle`/
`process_refile` in `tools/process.py`), a document that lands directly at
the documents root is filed straight into `documents/{type}/`, never left
flat. This tool exists for everything that predates that fix, or that
otherwise never got the type-folder treatment (a hand-dropped file, an
archive migrated from elsewhere, a pre-2026-07-22 inbox run) - a one-time
(or occasional) CLEANUP pass, not a replacement for how new sources are
filed, which already works.

**The one safety property that matters more than any rule below:** a file
already sitting inside ANY folder a human created by hand - even a shallow
one - is presumptively deliberate and is NEVER touched, never even
proposed. The only material this tool will move is a documents-root file
still sitting EXACTLY where `fha process` originally placed it: flat at
the documents root, or one level down in the type folder its OWN record's
`source_type` maps to (`_record_subdir`, the same mapping `sources/{type}/`
already uses). Anything else - a different folder name, a folder within a
folder, a hand-renamed path - reads as "a human already curated this" and
is left alone regardless of how untidy it looks by this tool's own rules.
A false positive here (moving a human's deliberately-organized file) is
far worse than a false negative (leaving something disorganized that a
human would have wanted moved) - see `_plan`'s eligibility check below.

**Reconciling this with SPEC §12.1's "folders are the human's projection,
never the tool's to rearrange"** (the phrase `process.py`'s own filing
logic uses, `_relocate_from_inbox`'s destination comment): SPEC §12.1
itself already draws the distinction this tool relies on - describing
`fha process refile` (the cross-root correction), it says plainly that
moves *within* one asset root "remain free (folders are projection) and
are healed by `fha reconcile`." A within-root move was never the tool's
business to forbid; it is a human's business to WANT, confirm, and preview
first. What this tool automates is only the mechanical half of that
already-sanctioned move (finding the candidates, computing where each one
belongs, keeping the record in sync) for the one narrow case where no
human projection exists yet to respect - material still sitting exactly
where a machine, not a person, put it. The eligibility rule above is what
keeps that true in practice: the instant a file sits anywhere a human
could plausibly have put it on purpose, it is out of scope, full stop.

**The rule set (small and fixed, not a general file-organizer; issue #107
asks for exactly this, no more):**

  1. The base folder for an eligible file is `documents/{type}/`, where
     `{type}` is the OWNING record's own `source_type` mapped through the
     same `_record_subdir` folder-name convention `sources/{type}/` already
     uses (so `documents/` ends up organized the same way `sources/` is -
     one folder per source_type, `proof-argument` under `proofs/` same as
     the records side).
  2. A source with MORE than `--group-threshold` (default 3) ELIGIBLE files
     gets its own subfolder instead, `documents/{type}/{slug}_{S-id}/`, so
     one multi-page source does not turn its type folder into a second
     flat pile.
  3. Nothing eligible is left loose at the bare documents root once this
     tool has run over it - rule 1 alone guarantees that.

**Safety, matching every other mutating `fha` verb:** dry-run is the
default - `fha reorganize` alone only PLANS and PRINTS, nothing is ever
written without the explicit `--apply` flag (stronger than the usual
"pass --dry-run to preview" convention, because this can touch hundreds of
files in one run). Every move updates the owning source's `files:` entry
in the SAME atomic step as the physical move - `_apply_one_record` mirrors
`fha process refile`'s own move+record-update+rollback shape (narrowed to
the same-root case, and to `fha reconcile`'s lighter files:-only mutation:
no Notes provenance paragraph is added, matching reconcile's own precedent
for a same-root path change rather than refile's cross-root one - see
`_apply_one_record`'s docstring). Application runs in bounded batches
(`--batch-size`, default 25 files); after each batch, `fha reconcile` is
re-run (`--dry-run`, so it only ever REPORTS, never itself "fixes"
anything) and its warning/error count is compared against the count just
before this batch started - a batch that leaves MORE for reconcile to
complain about halts the whole run right there, before anything further is
touched, and says so plainly. A batch that leaves the SAME (already
pre-existing, not caused by this run) count alone is not this tool's
business to fix, and the run continues - see `run_reorganize`'s docstring
for exactly how that distinction is drawn.

**Photos are out of scope, on purpose.** SPEC §12.1 identifies a photo by
its embedded `SOURCE:` keyword, never by path, and `fha process refile`'s
own docstring already states the governing principle: "fha never invents
photo-library organization - the folder choice is yours." There is no
"loose vs. organized" ambiguity to resolve for a photo the way there is
for a document - a photo works correctly wherever it sits, as long as the
keyword survives - so there is no analogous problem for this tool to fix.
The issue also names a "related, pre-existing gap": that `fha reconcile`
only covered documents-side paths, not a photos-side re-verification after
a move. That gap is already closed in current code - `fha reconcile`'s
photo pass already calls `photoindex.run_reconcile` (SOURCE:-keyword-based
re-matching, `--with-exif` to read embedded keywords) for every archive
that has a photo catalog (`reconcile.py`'s own docstring: "one command
reconciles every file type"). This tool's own post-batch check runs the
FULL `fha reconcile` (documents AND photos), so an archive's photo catalog
gets the same re-verification as a side effect, even though reorganize
itself never moves a photos-root file.

CODE MAP
--------
  _record_subdir              - source_type -> documents/{type} folder name (twin of process.py's)
  _slugify                    - lowercase-hyphenate text into a slug (twin of process.py's)
  _filename_has_source_id     - the S-id embedded in a filed document's filename (twin of process.py's)
  _canonical_path              - resolve a path for physical-identity comparison (duplicate-ownership guard)
  _machine_generated_names    - every basename fha process could have produced for one files: entry
  _is_under                   - containment check (twin of process.py's)
  _move_file                  - move-with-copy+delete-fallback (twin of process.py's)
  _scalar_value / _rewrite_file_line / _file_line_count
                               - value-exact `files:` line surgery (twin of process.py's)
  _files_shape_error          - is a record's raw files: value the SPEC §13 shape? (mapping /
                               scalar-list detector, folded into the same fail-closed path)
  _iter_source_records        - yield (path, meta) for every parseable sources/ record;
                               collects unparseable-record names for _plan to fail closed on
  _plan                       - compute the full move plan; nothing written (the survey)
  _apply_one_record           - atomic move+record-update+rollback for ONE record's files
  _chunk_records               - group planned records into bounded batches
  _finish_apply                - the shared final-lint-then-finalize every apply return path shares
  run_reorganize               - engine: plan, and (with apply=True) execute in checked batches
  _cmd_reorganize / register / _standalone_main - CLI plumbing
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml

from _lib import (
    EXIT_FAILURE,
    EXIT_WARNINGS,
    SOURCE_TYPES,
    FhaConfigError,
    Result,
    configure_utf8_stdout,
    fmt_id_display,
    format_source_type_error,
    frontmatter_fence_span,
    is_working_copy,
    load_fha_yaml,
    normalize_id,
    read_record,
    read_text_exact,
    reapply_newline,
    resolve_path,
    resolve_root_arg,
    unreadable_dir_recorder,
    walk_files,
    write_text_exact_atomic,
    yaml_inline,
)

# Orchestrator carve-out (TOOLING.md "a tool must never import another tool;
# shared code lives in tools/_lib.py"). reconcile.py's own docstring already
# names the exception it relies on to import photoindex: "a tool whose whole
# job is running other tools' engines does import them; ordinary tools still
# never do" - reconcile completes its "one command reconciles every file
# type" contract that way. This tool's post-batch safety check IS running
# fha reconcile's (and, at the very end, fha lint's) engine in exactly that
# spirit - "confirms nothing broke" is only honest if it is the SAME check a
# human would run by hand, not a hand-rolled approximation of it - so the
# same carve-out is used here, for the same reason.
import lint
import reconcile

configure_utf8_stdout()


_DEFAULT_DOCUMENT_TYPE = 'other'
_DEFAULT_GROUP_THRESHOLD = 3
_DEFAULT_BATCH_SIZE = 25

# A filename already carrying an S-id (`{slug}_{S-id}.ext`, SPEC §13) - the
# same pattern process.py's `_FILENAME_SOURCE_ID_RE` matches, duplicated per
# TOOLING's "tools never import tools".
_FILENAME_SOURCE_ID_RE = re.compile(r'_(S-[0-9a-hjkmnp-tv-z]{10})$', re.I)


def _record_subdir(source_type: str) -> str:
    """Map a source_type to its documents/{type} folder name.

    Twin of `process.py`'s own `_record_subdir` (SPEC §14): `proof-argument`
    files under `proofs/`, matching the records side; every other type is
    the literal type name. `photo` is included for completeness (a hand-
    corrupted record could carry a documents-alias file while still typed
    `photo`) but should never actually occur for a documents-root file in a
    healthy archive - photos live under the photos root, not documents/.
    """
    if source_type == 'photo':
        return 'photos'
    if source_type == 'proof-argument':
        return 'proofs'
    return source_type


def _filename_has_source_id(file_path: Path) -> str | None:
    """Return the S-id embedded in a filed document's filename, or None.

    Twin of `process.py`'s own `_filename_has_source_id`. This is the
    identity-drift guard `fha process refile` already uses (its own P1 audit
    finding): a `files:` entry that resolves to a real file is not
    automatically the RIGHT file - only a filename that carries the same
    record's own S-id proves that.
    """
    m = _FILENAME_SOURCE_ID_RE.search(file_path.stem)
    return m.group(1).lower() if m else None


def _slugify(text: str) -> str:
    """Collapse arbitrary text to a lowercase-hyphenated slug (SPEC §13).

    Twin of `process.py`'s own `_slugify` - needed here only to reproduce a
    ROLE or COPY value's slug form exactly the way `fha process`/`process
    refile`/bundle dissolution already do when they mint a `role`/`copy`
    filename suffix (`_machine_generated_names` below).
    """
    text = (text or '').strip().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return slug or 'source'


def _canonical_path(path: Path) -> Path:
    """`path`, resolved for a PHYSICAL-identity comparison.

    Two lexically different aliases can still name the very same file on
    disk (a redundant `./` segment, a doubled slash, a `sub/../` detour, a
    case difference on a case-insensitive filesystem) - `Path.resolve()`
    collapses all of that (Codex audit finding, PR #188 second pass): the
    duplicate-ownership map below has to be keyed on WHAT a `files:` entry
    physically points at, not on the exact characters it is spelled with,
    or two spellings of the same file slip past the "claimed by more than
    one entry" guard entirely. `resolve()` works fine on a path that does
    not exist (the default `strict=False`), and any resolution failure
    (a symlink loop, a locked component) falls back to the un-resolved
    path - the same "not verifiably resolved is not verifiably different"
    posture `_is_under` already takes for the exact same call.
    """
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _machine_generated_names(slug: str, display_sid: str, entry: dict, ext: str) -> set[str]:
    """Every basename `fha process` (or `process refile`, or bundle
    dissolution) could have machine-generated for THIS ONE `files:` entry
    today - twin of `process.py`'s several filing paths, collapsed into the
    two BASE conventions those paths actually use (P1 audit finding, PR
    #188): a lone processed file (or its `-back` sibling, or a later `--to
    documents` refile) is named off the RECORD's own slug (`process_
    refile`'s own `new_name` computation: `{slug}[-{copy}][-{role}]_{S-id}
    {ext}`); a bundle-dissolved attachment is instead named off ITS OWN
    original filename (`process_bundle`'s per-asset `base = _slugify(asset
    .stem)`, stored back onto the entry as `original_filename` since #59 -
    every filing path since writes it).

    Returning the SET of names either convention could have produced -
    rather than picking one - is deliberate: nothing here can tell, after
    the fact, which path filed a given entry, and treating a real bundle
    attachment as "hand-renamed" merely because its name was never built
    from the record's OWN slug would be exactly the false positive this
    tool exists to avoid (a human's deliberately-organized-or-not file
    mistaken for the other case). A basename outside BOTH candidates is the
    genuinely unambiguous case: nothing `fha process` does today would ever
    have produced it, so it reads as a human rename - excluded into
    `excluded_human`, never touched (`_plan`'s eligibility check).
    """
    suffix = ''
    role = entry.get('role')
    if role and str(role) != 'primary':
        suffix = f'-{_slugify(str(role))}'
    copy = entry.get('copy')
    if copy:
        suffix = f'-{_slugify(str(copy))}{suffix}'
    bases = {slug}
    original_filename = entry.get('original_filename')
    if original_filename:
        bases.add(_slugify(Path(str(original_filename)).stem))
    return {f'{base}{suffix}_{display_sid}{ext}' for base in bases}


def _is_under(path: Path, root: Path) -> bool:
    """True if `path` resolves to somewhere inside `root`.

    Twin of `process.py`'s own `_is_under`: a symlink loop on either side
    resolves to False (not verifiably contained) rather than raising, the
    same posture every containment check in this suite takes.
    """
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError, RuntimeError):
        return False


def _move_file(src: Path, dest: Path) -> None:
    """Move one file, falling back to copy+delete across filesystems.

    Byte-for-byte twin of `process.py`'s own `_move_file` (see its docstring
    for the fallback's exact failure handling) - `Path.rename` is atomic but
    only works within one filesystem, and an archive's documents root can
    live on a different drive than the folder this tool creates for it.
    """
    try:
        src.rename(dest)
        return
    except OSError:
        pass
    try:
        shutil.copy2(src, dest)
    except Exception:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        src.unlink()
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def _scalar_value(raw: str) -> tuple[str, str]:
    """Split a `file:` line's value region into (value, trailing_comment).

    Twin of `process.py`'s own `_scalar_value` (itself a deliberate twin of
    `reconcile.py`'s `_split_file_value`) - quote state is tracked before any
    comment split, so a `#` inside a quoted scalar is never mistaken for a
    comment and truncated.
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
                i += 2
                continue
            if c == quote:
                if quote == "'" and i + 1 < len(s) and s[i + 1] == "'":
                    i += 2
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

    Twin of `process.py`'s own `_rewrite_file_line`: value-exact matching
    (never substring), bounded to the frontmatter fence (so a `## Notes`
    bullet that happens to read `- file: ...` can never be mistaken for the
    inventory), re-emitted through `yaml_inline` so a folder name carrying a
    YAML-hostile character is quoted rather than silently truncated. Returns
    (new_text, match_count); the caller refuses on anything but exactly 1.
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
    new_line = f'{indent}{key} {yaml_inline(new_alias)}'
    if comment:
        new_line = f'{new_line}  {comment}'
    lines[i] = new_line
    return '\n'.join(lines), 1


def _file_line_count(text: str) -> int:
    """Count `file:` inventory lines - the refuse-rather-than-corrupt guard
    every surgical `files:` writer in this suite shares."""
    return sum(1 for ln in text.split('\n')
               if ln.strip().startswith(('file:', '- file:')))


def _files_shape_error(files_value) -> str | None:
    """Return a human-readable problem description if `files_value` (a
    record's raw `files:` frontmatter value) is not the SPEC §13 shape - a
    list of `{file: ..., ...}` mappings - or None when it is (including
    when the key is simply absent, meta.get('files') is None).

    Syntactically valid YAML can still be shaped wrong two ways a plain
    `read_record` parse never catches (Codex review round 5, PR #188 P1):
    `files:` written as a MAPPING instead of a list, or a list whose
    members are bare SCALARS instead of `{file: ...}` mappings. Both used
    to reach the ownership-building loop and have every one of their
    entries silently skipped one at a time (`if not isinstance(entry,
    dict): continue`, both in Pass 1's `canonical_owners` build and Pass
    2's eligibility loop) rather than refusing the record - so a record
    with this shape never contributed ANY of its own claims to
    `canonical_owners`, even though the physical file(s) it (in spirit)
    names are real and may be claimed by a DIFFERENT, well-formed record
    too. That different record's move then went ahead unrefused, as if
    there were no conflict at all, leaving the malformed record's own
    reference pointing at the file's now-stale old path - the exact
    "torn ownership" hazard `_iter_source_records`'s unparseable-record
    handling already exists to prevent for outright parse failures.
    """
    if files_value is None:
        return None
    if not isinstance(files_value, list):
        return f'files: is a {type(files_value).__name__}, not a list'
    bad = sum(1 for e in files_value if not isinstance(e, dict))
    if bad:
        noun = 'entry' if bad == 1 else 'entries'
        return (f'files: has {bad} {noun} that are not `file: ...` mappings '
                '(a bare scalar in the list)')
    return None


def _iter_source_records(archive_root: Path, result: Result, *,
                          unparseable: list[str] | None = None):
    """Yield (record_path, meta) for every parseable record under sources/.

    An unparseable record's NAME is appended to `unparseable` (when the
    caller passes a list) rather than merely warned-and-skipped (P1 audit
    finding, PR #188, third pass): `_plan`'s duplicate-ownership guard
    (Pass 1, `canonical_owners`) depends on seeing EVERY record's `files:`
    claims to catch two records fighting over the same physical file - a
    broken record silently skipped here means its OWN alias-ownership
    claims never enter that map, so a DIFFERENT, parseable record that
    happens to list the same physical file moves it as if there were no
    conflict at all, leaving the broken record's `files:` entry pointing
    at a file that is no longer there. `_plan` fails the WHOLE survey
    closed the instant this list is non-empty - see its Pass 0 - so the
    per-record `continue` below never has to be safe on its own; it only
    has to not crash the generator. Twin of `reconcile.py`'s own
    `_iter_source_records`, narrowed to what this tool needs (no reverse
    "unlisted" pass here) and diverging from it on exactly this point -
    reconcile's job is to REPORT every problem it can find in one pass, so
    a broken record there is one more finding among many; this tool's job
    is to decide what is SAFE TO MOVE, and an incomplete ownership map can
    never be trusted for that.

    A record that PARSES fine but whose `files:` value is the wrong SHAPE
    (a mapping, or a list of bare scalars - `_files_shape_error`) is folded
    into this SAME `unparseable` path (Codex review round 5, PR #188 P1):
    it is just as invisible to `canonical_owners` as an outright parse
    failure, so it must fail the whole survey closed the same way, not
    merely skip its own malformed entries one at a time.
    """
    sources_dir = archive_root / 'sources'
    if not sources_dir.is_dir():
        return
    for rec_path in sorted(sources_dir.rglob('*.md')):
        try:
            rec = read_record(rec_path)
        except Exception:
            if unparseable is not None:
                unparseable.append(
                    f'{rec_path.name} could not be parsed. Run `fha lint` for the '
                    'specifics.')
            continue
        if rec.get('parse_errors'):
            detail = '; '.join(msg for _, msg in rec['parse_errors'])
            if unparseable is not None:
                unparseable.append(
                    f'{rec_path.name} has malformed YAML ({detail}).')
            continue
        meta = rec.get('meta') or {}
        shape_error = _files_shape_error(meta.get('files'))
        if shape_error:
            if unparseable is not None:
                unparseable.append(f'{rec_path.name} has a malformed files: entry ({shape_error}).')
            continue
        yield rec_path, meta


def _display_path(path: Path, archive_root: Path) -> str:
    """A path the way the human sees it, relative to the archive root when
    possible (mirrors `reconcile.py`'s own `_display_dir`)."""
    try:
        return path.resolve().relative_to(archive_root.resolve()).as_posix()
    except ValueError:
        return str(path).replace('\\', '/')


def _plan(archive_root: Path, fha_config: dict, documents_root: Path,
          result: Result, *, group_threshold: int) -> dict:
    """Compute the full reorganize plan. Nothing is written here - the survey.

    Returns:
      {'moves': {record_path: [(old_alias, new_alias, old_abs, new_abs), ...]},
       'no_op_count': int,          # eligible files already exactly where they belong
       'excluded_human': [(alias, record_name), ...],  # presumptively human-organized
       'problems': [str, ...],      # pre-existing drift/corruption needing a human
       'unreadable_dirs': [Path, ...],
       'unparseable_records': [str, ...]}  # malformed frontmatter/Claims - survey refused

    Three passes, in order, each closing a hazard a plain "walk and move"
    would miss:

    Pass 0 - fail closed on any folder (under `documents/` OR `sources/`)
    that will not list (mirrors `reconcile.py`'s own `_disk_index`/`_plan`
    posture): every conclusion below comes from a full read of both trees,
    and a hole in either one can hide a second record claiming the same
    file, or hide the human-organized folder a file actually lives in. When
    either walk is incomplete, the whole plan comes back empty with the
    unreadable folders named, and NOTHING is proposed - not even the files
    this run could see fine. The same fail-closed posture covers a record
    that LISTS fine but does not PARSE (malformed frontmatter or Claims
    YAML, P1 audit finding PR #188 third pass): `_iter_source_records`
    used to warn and skip just that one record, but Pass 1's
    duplicate-ownership map is only trustworthy when it has seen EVERY
    record's `files:` claims - a broken record's own alias claims silently
    missing from that map means a DIFFERENT, parseable record listing the
    SAME physical file gets waved through as if there were no conflict,
    and the broken record is left with a `files:` entry pointing at a file
    that just moved out from under it. So ANY unparseable record also
    empties the whole plan (the unparseable records named) rather than
    proceeding on an incomplete map.

    Pass 1 - build canonical-physical-path -> owning (record, alias) map
    across every parseable record's documents-alias `files:` entries, keyed
    by the RESOLVED path so two lexically different aliases naming the same
    real file (a redundant `./`, a `sub/../` detour) still collide (see
    `_canonical_path`). A path claimed by MORE than one entry is
    pre-existing corruption (adversarial case: two records - or two aliases
    in the SAME record - pointing at the same physical path) - every claim
    on it is refused into `problems`, and the file is never moved on the
    strength of any one of them, rather than silently picking a side.

    Pass 2 - for each record, first two whole-record identity guards run
    before any of its files are looked at: the record's frontmatter `id:`
    must agree with the S-id its OWN filename carries (a filename/frontmatter
    drift, `fha lint`'s E003, means this record's identity cannot be
    trusted from the filename alone - mirrors `site.py`'s
    `_origin_frontmatter_id_mismatches`), and its `source_type` must be a
    real entry in `_lib.SOURCE_TYPES` (an unvalidated value is a path
    component moments later - `_record_subdir` - and a hostile or typo'd
    one could walk a destination outside the documents root entirely).
    Either failing refuses the WHOLE record into `problems`, nothing of its
    planned. For each surviving record's own documents-alias entries:
    resolve it, confirm it is really on disk, confirm its OWN filename
    still carries this record's S-id (the same identity-drift guard `fha
    process refile` uses - a `files:` entry that resolves cleanly is not
    automatically pointing at the right file), then judge ELIGIBILITY by
    path SHAPE (depth 0 under documents/, or depth 1 in this record's OWN
    `_record_subdir(source_type)` folder) AND by basename: the file's
    actual name must match one of the shapes `fha process` itself could
    have produced for this entry (`_machine_generated_names` - a lone file
    is named off the record's own slug, a bundle attachment off its own
    `original_filename`). Failing either check reads as "a human already
    curated this" and goes to `excluded_human`, never touched, never even
    proposed. Eligible files are grouped per record; a destination is
    computed only once eligibility is settled, checked that it still lands
    inside the documents root (belt-and-braces, given the source_type guard
    above), and a proposed destination that already exists on disk
    (adversarial case: a name collision for an unrelated reason) is refused
    into `problems` rather than silently overwritten or merged.
    """
    unreadable: list[Path] = []
    for _ in walk_files(documents_root, on_error=unreadable_dir_recorder(unreadable)):
        pass
    sources_dir = archive_root / 'sources'
    for _ in walk_files(sources_dir, suffix='.md', on_error=unreadable_dir_recorder(unreadable)):
        pass
    if unreadable:
        return {'moves': {}, 'no_op_count': 0, 'excluded_human': [],
                'problems': [], 'unreadable_dirs': unreadable,
                'unparseable_records': []}

    # Fail closed on ANY unparseable record (P1 audit finding, PR #188 third
    # pass) - see `_iter_source_records`'s own docstring and Pass 0 above for
    # why an incomplete `canonical_owners` map below can never be trusted:
    # the whole plan comes back empty, with every unparseable record named,
    # rather than silently planning moves against a partial ownership
    # picture.
    unparseable: list[str] = []
    records = list(_iter_source_records(archive_root, result, unparseable=unparseable))
    if unparseable:
        return {'moves': {}, 'no_op_count': 0, 'excluded_human': [],
                'problems': [], 'unreadable_dirs': [],
                'unparseable_records': unparseable}

    # Pass 1: canonical (resolved) physical path -> owning (record, alias)
    # pair(s), documents-alias entries only. Keyed by the RESOLVED path, not
    # the raw alias text (Codex audit finding, PR #188 second pass): two
    # lexically different aliases that name the SAME file (a redundant
    # `./`, a `sub/../` detour, a case difference) must still collide here,
    # or the second one slips through unrefused while the first one's move
    # quietly stranded it - see `_canonical_path`.
    canonical_owners: dict[Path, list[tuple[Path, str]]] = {}
    for rec_path, meta in records:
        for entry in (meta.get('files') or []):
            if not isinstance(entry, dict):
                continue
            alias = str(entry.get('file', '') or '').replace('\\', '/')
            if not alias or alias.split('/', 1)[0] != 'documents':
                continue
            canonical = _canonical_path(resolve_path(alias, fha_config, archive_root))
            canonical_owners.setdefault(canonical, []).append((rec_path, alias))

    problems: list[str] = []
    excluded_human: list[tuple[str, str]] = []
    moves: dict[Path, list[tuple[str, str, Path, Path]]] = {}
    no_op_count = 0
    planned_destinations: set[Path] = set()

    for rec_path, meta in records:
        rec_stem = rec_path.stem
        m = _FILENAME_SOURCE_ID_RE.search(rec_stem)
        if not m:
            problems.append(
                f'{rec_path.name}: the record filename does not carry an S-id - '
                'skipped (`fha lint` flags this separately).')
            continue
        # Two forms, deliberately kept apart: `rec_sid` (fully lowercase) is
        # for EQUALITY checks against `_filename_has_source_id`'s own
        # lowercased return; `display_sid` (capital prefix, e.g. 'S-2b3c...')
        # is the `fmt_id_display` convention every OTHER filename/folder
        # `fha` constructs already uses (process_refile's own `new_name`) -
        # reusing the fully-lowercased form to NAME a folder would produce
        # `thing_s-2b3c...` next to every other tool's `thing_S-2b3c...`.
        rec_sid = m.group(1).lower()
        display_sid = m.group(1)[0].upper() + m.group(1)[1:].lower()
        slug = rec_stem[:m.start()]

        # Identity guard (P1 audit finding, PR #188): `rec_sid` above comes
        # ONLY from the record's own FILENAME - a record whose filename and
        # frontmatter `id:` have drifted apart (a real, lint-flagged E003
        # state) must not be trusted just because its filename LOOKS right.
        # Same posture as `site.py`'s `_origin_frontmatter_id_mismatches`
        # (#117 audit): fail closed and refuse every one of this record's
        # files rather than silently proceeding under whichever identity
        # turns out to be wrong.
        frontmatter_id = normalize_id(str(meta.get('id') or ''))
        if frontmatter_id != rec_sid:
            problems.append(
                f'{rec_path.name}: the filename names {display_sid} but its own `id:` '
                f'says {fmt_id_display(frontmatter_id) if frontmatter_id else "(none)"} '
                "- `fha lint`'s E003 flags this mismatch. Refusing to move any of its "
                'files until the identity is fixed by hand (`fha lint` names the spot), '
                'then re-run. Not touched.')
            continue

        source_type = str(meta.get('source_type') or _DEFAULT_DOCUMENT_TYPE).strip()
        source_type = source_type or _DEFAULT_DOCUMENT_TYPE
        # Path-traversal guard (P1 audit finding, PR #188): `source_type` is
        # a hand-editable frontmatter field, trusted below as a PATH
        # COMPONENT (`_record_subdir`) with no other check in between. An
        # unknown value could be a typo (an unsupported folder) or, worse,
        # something like `../family` that walks `dest_dir` straight out of
        # the configured documents root. Refuse the WHOLE record before any
        # destination is computed from it - never after.
        if source_type not in SOURCE_TYPES:
            problems.append(
                f'{rec_path.name}: {format_source_type_error(source_type)} Refusing to '
                'plan any move for this record until its source_type is fixed. Not '
                'touched.')
            continue
        type_dir = _record_subdir(source_type)

        eligible: list[tuple[str, Path]] = []
        for entry in (meta.get('files') or []):
            if not isinstance(entry, dict):
                continue
            alias = str(entry.get('file', '') or '').replace('\\', '/')
            if not alias or alias.split('/', 1)[0] != 'documents':
                continue
            if str(entry.get('status', '')) == 'missing-fixture':
                continue

            abs_path = resolve_path(alias, fha_config, archive_root)
            canonical = _canonical_path(abs_path)
            owners = canonical_owners.get(canonical, [])
            if len(owners) > 1:
                owner_names = sorted(set(p.name for p, _a in owners))
                if len(owner_names) == 1:
                    other_aliases = sorted(set(a for _p, a in owners if a != alias))
                    problems.append(
                        f'{rec_path.name}: {alias!r} and {other_aliases!r} both resolve '
                        'to the same file, listed more than once - fix the duplicate '
                        'files: entry by hand, then re-run. Not touched.')
                else:
                    problems.append(
                        f'{alias}: listed by more than one source record '
                        f'({", ".join(owner_names)}) - fix the duplicate files: entry by '
                        'hand, then re-run. Not touched.')
                continue

            if not _is_under(abs_path, documents_root):
                problems.append(
                    f'{rec_path.name}: {alias!r} does not resolve inside the '
                    'documents root (a `..` segment or a doubled slash?) - fix '
                    'the entry by hand, then re-run. Not touched.')
                continue
            if not abs_path.is_file():
                problems.append(
                    f'{rec_path.name}: {alias} is not on disk - run `fha reconcile` '
                    'first, then re-run. Not touched.')
                continue

            filename_sid = _filename_has_source_id(abs_path)
            if filename_sid != rec_sid:
                carried = filename_sid.upper() if filename_sid else 'no source id at all'
                problems.append(
                    f"{rec_path.name}: {alias}'s own filename carries {carried}, not "
                    f'{rec_sid.upper()} - this looks like inventory drift, not a plain '
                    'reorganize candidate. Fix it by hand, then re-run. Not touched.')
                continue

            rel = PurePosixPath(alias[len('documents/'):])
            parts = rel.parts[:-1]
            depth = len(parts)
            shape_ok = depth == 0 or (depth == 1 and parts[0] == type_dir)
            # Hand-renamed guard (P1 audit finding, PR #188): path SHAPE
            # alone is not enough - a human can rename a file in place,
            # keeping its `_S-id` suffix and its eligible folder, and that
            # is JUST as much "a human already curated this" as moving it
            # would be. Compare the actual basename against every shape
            # `fha process` itself could have produced for this entry; only
            # an EXACT match is still "sitting exactly where a machine put
            # it" (`_machine_generated_names`).
            if shape_ok and abs_path.name in _machine_generated_names(
                    slug, display_sid, entry, abs_path.suffix):
                eligible.append((alias, abs_path))
            else:
                excluded_human.append((alias, rec_path.name))

        if not eligible:
            continue

        if len(eligible) > group_threshold:
            dest_dir = documents_root / type_dir / f'{slug}_{display_sid}'
        else:
            dest_dir = documents_root / type_dir

        record_pairs: list[tuple[str, str, Path, Path]] = []
        for alias, abs_path in eligible:
            new_abs = dest_dir / abs_path.name
            if new_abs == abs_path:
                no_op_count += 1
                continue
            if not _is_under(new_abs, documents_root):
                # Belt-and-braces: `source_type` is already validated above,
                # so `type_dir` can never itself carry a `..` segment - but
                # a computed destination is cheap insurance against this
                # exact hazard (moving a real file OUTSIDE the documents
                # root) never being silently reintroduced by a future edit.
                problems.append(
                    f'{rec_path.name}: {alias} would compute a destination outside the '
                    'documents root - refusing. Not touched.')
                continue
            if new_abs.exists() or new_abs in planned_destinations:
                problems.append(
                    f'{rec_path.name}: {alias} would move to '
                    f'{_display_path(new_abs, archive_root)}, but something is already '
                    'there - skipped. Move or rename it by hand, then re-run.')
                continue
            new_alias = 'documents/' + new_abs.relative_to(documents_root).as_posix()
            planned_destinations.add(new_abs)
            record_pairs.append((alias, new_alias, abs_path, new_abs))

        if record_pairs:
            moves[rec_path] = record_pairs

    return {'moves': moves, 'no_op_count': no_op_count,
            'excluded_human': excluded_human, 'problems': problems,
            'unreadable_dirs': [], 'unparseable_records': []}


def _apply_one_record(
    record_path: Path, pairs: list[tuple[str, str, Path, Path]],
) -> tuple[bool, str, bool]:
    """Move every planned file for ONE record and rewrite its files: entries,
    or leave both completely untouched. Never a partial result.

    Mirrors `fha process refile`'s (`process.py::process_refile`) own
    move+record-update+rollback shape, narrowed two ways: same-root (no
    keyword embedding, no photo-catalog confirmation - those are refile's
    cross-root concerns) and files:-only (no Notes provenance paragraph -
    `fha reconcile` sets the precedent for a same-root path fix touching
    only the files: line, and reorganize is that same class of change,
    just tool-planned instead of human-moved-then-healed).

    Order matters and is deliberate: the text rewrite is fully computed and
    validated FIRST, in memory, refusing on any mismatch before a single
    byte moves; then every physical move for this record happens, tracked
    one at a time, with a mid-way failure rolling back every move already
    done for THIS record (never touching the record's text, since it has
    not been written yet); only once every file for this record is safely
    at its new home does the record get ONE atomic write covering every
    pair; a failure at that step rolls every file straight back too. A
    final re-read confirms the write means what it says (the same
    round-trip guard `reconcile.py`'s own `_apply` uses) and restores both
    the text and the files on any mismatch. Every rollback is best-effort
    and, on the rare case it cannot fully finish (e.g. a drive that went
    read-only mid-move), says exactly what is left inconsistent and where,
    rather than claiming a clean rollback that did not happen.

    Returns (ok, description, unrecoverable). `ok=True` is success; the
    third element only ever matters when `ok=False` and is the return
    contract's own answer to "was this a CLEAN failure, or one that left
    the archive genuinely inconsistent" (P1 audit finding, PR #188) - both
    used to come back as an indistinguishable `(False, message)`, which let
    the caller treat a rollback that could not fully finish as an ordinary
    skip-and-continue instead of the whole-run halt it actually demands.
    `unrecoverable=True` only when a best-effort rollback (`_undo_moves`, or
    the final text restore) did not fully finish - the archive is left
    genuinely inconsistent for THIS record and the caller must stop the
    whole run, not just skip to the next one; every other `ok=False` path
    is a clean "nothing changed" the caller may treat as an ordinary,
    contained failure.
    """
    try:
        before = read_text_exact(record_path)
    except OSError as e:
        return False, f'{record_path.name}: could not be read ({e}) - skipped, nothing changed.', False

    after = before
    for old_alias, new_alias, _old_abs, _new_abs in pairs:
        after, count = _rewrite_file_line(after, old_alias, new_alias)
        if count != 1:
            what = 'not found' if count == 0 else 'listed on more than one line'
            return False, (
                f'{record_path.name}: the files: line for {old_alias!r} was {what} - '
                'skipped, nothing changed. Fix it by hand or run `fha lint`, then re-run.'
            ), False
    if _file_line_count(after) != _file_line_count(before):
        return False, (
            f'{record_path.name}: rewriting its files: entries would change the shape '
            'of the list - skipped, nothing changed. Fix it by hand (`fha lint` names '
            'the spot), then re-run.'
        ), False

    moved: list[tuple[Path, Path]] = []
    created_dirs: list[Path] = []
    try:
        for _old_alias, _new_alias, old_abs, new_abs in pairs:
            probe = new_abs.parent
            while not probe.exists() and probe != probe.parent:
                created_dirs.append(probe)
                probe = probe.parent
            new_abs.parent.mkdir(parents=True, exist_ok=True)
            if new_abs.exists():
                # TOCTOU guard (P1 audit finding, PR #188): `_plan` checked
                # this destination was free only ONCE, well before this
                # batch actually runs - real time passes for the [y/N]
                # confirmation, for earlier batches, for `fha reconcile`
                # shelling out between them. Re-check immediately before
                # THIS move, right next to where `_move_file`'s
                # `Path.rename` would otherwise silently clobber whatever
                # showed up there since - raising here (caught by the
                # `except OSError` below) refuses and rolls back cleanly,
                # the same as any other mid-move problem, rather than
                # overwriting a file this run never planned to touch.
                raise OSError(
                    f'{new_abs.name} now exists at the planned destination (it did not '
                    'when this run was planned) - refusing to overwrite it')
            _move_file(old_abs, new_abs)
            moved.append((old_abs, new_abs))
    except OSError as e:
        undo_failed = _undo_moves(moved, created_dirs)
        if undo_failed:
            return False, (
                f'{record_path.name}: moving its files failed ({e}), and the rollback '
                f'could not fully finish: {"; ".join(undo_failed)}. The archive is '
                f'INCONSISTENT for this record - run `fha doctor`, then `fha reconcile` '
                'to see exactly what is where.'
            ), True
        return False, f'{record_path.name}: moving its files failed ({e}) - rolled back, nothing changed.', False

    try:
        write_text_exact_atomic(record_path, reapply_newline(after, before))
    except OSError as e:
        undo_failed = _undo_moves(moved, created_dirs)
        if undo_failed:
            return False, (
                f'{record_path.name}: its files were moved but the record could not be '
                f'written ({e}), and the rollback could not fully finish: '
                f'{"; ".join(undo_failed)}. The archive is INCONSISTENT for this record - '
                'run `fha doctor`, then fix it by hand before continuing.'
            ), True
        return False, (
            f'{record_path.name}: could not be written ({e}) - files moved back, '
            'nothing changed.'
        ), False

    reparsed = read_record(record_path)
    reparsed_files = {
        str(e.get('file', '')).replace('\\', '/')
        for e in (reparsed.get('meta', {}).get('files') or [])
        if isinstance(e, dict)
    }
    intended = {new for _old, new, _oa, _na in pairs}
    stale = {old for old, _new, _oa, _na in pairs}
    if reparsed.get('parse_errors') or not intended <= reparsed_files or (stale & reparsed_files):
        undo_failed = _undo_moves(moved, created_dirs)
        restore_failed = False
        try:
            write_text_exact_atomic(record_path, before)
        except OSError:
            restore_failed = True
        if undo_failed or restore_failed:
            parts = list(undo_failed)
            if restore_failed:
                parts.append('the record could not be restored to its original text')
            return False, (
                f'{record_path.name}: the rewritten files: entry did not read back '
                f'correctly, and the rollback could not fully finish ({"; ".join(parts)}). '
                'The archive is INCONSISTENT for this record - run `fha doctor`.'
            ), True
        return False, (
            f'{record_path.name}: the rewritten files: entry did not read back correctly '
            '(likely a folder name with a stray "#" or ":") - rolled back, nothing '
            'changed. Fix the folder name, then re-run.'
        ), False

    names = '; '.join(f'{old} -> {new}' for old, new, _oa, _na in pairs)
    return True, f'{record_path.name}: {names}', False


def _undo_moves(moved: list[tuple[Path, Path]], created_dirs: list[Path]) -> list[str]:
    """Best-effort reverse of `_apply_one_record`'s physical moves.

    Every entry is attempted even after an earlier one fails (rollback must
    never stop partway and leave later moves un-attempted); returns what
    could NOT be undone, plainly, rather than claiming a clean rollback that
    did not happen.
    """
    failed: list[str] = []
    for old_abs, new_abs in reversed(moved):
        try:
            _move_file(new_abs, old_abs)
        except OSError as e:
            failed.append(f'{new_abs.name} could not be moved back ({e})')
    for d in reversed(created_dirs):
        try:
            d.rmdir()
        except OSError:
            pass  # not empty (something else landed there) - leave it
    return failed


def _chunk_records(
    record_order: list[Path], moves: dict[Path, list], batch_size: int,
) -> list[list[Path]]:
    """Group planned records into batches of at most `batch_size` FILES.

    A single record's moves are never split across two batches (its own
    atomicity is already per-record; splitting would gain nothing and would
    make the post-batch reconcile check's "this batch's own work" framing
    ambiguous). A record that alone owns MORE files than `batch_size` is
    no longer handed to this function at all (P2 audit finding, Codex
    review round 5, PR #188): `run_reorganize` filters it out of
    `record_order` before calling this, reporting it as skipped rather
    than letting it silently exceed the advertised bound in a batch of its
    own. If one somehow still reached here, the loop below would still
    give it a batch entirely its own rather than crash or split it - cheap
    belt-and-braces, not the sanctioned way to exceed the bound.
    """
    batches: list[list[Path]] = []
    current: list[Path] = []
    current_count = 0
    for rp in record_order:
        n = len(moves[rp])
        if current and current_count + n > batch_size:
            batches.append(current)
            current = []
            current_count = 0
        current.append(rp)
        current_count += n
    if current:
        batches.append(current)
    return batches


def _issue_count(r: Result) -> int:
    """How many things `fha reconcile` had to complain about, as one number.

    Counts warning/error-level messages only - a dry-run heal's own preview
    lines print at 'info' level, so a healable-but-harmless pre-existing
    drift elsewhere in the archive never inflates this count or trips the
    halt check below on its own.
    """
    return sum(1 for m in r.messages if m.level in ('warning', 'error'))


def _finalize(result: Result) -> Result:
    """Set the exit code from the collected messages (mirrors reconcile.py's
    own `_finalize`): any error is 3, anything left needing a human is 1,
    else 0."""
    if any(m.level == 'error' for m in result.messages):
        result.ok = False
        result.exit_code = EXIT_FAILURE
    elif any(m.level == 'warning' for m in result.messages):
        result.exit_code = EXIT_WARNINGS
    return result


def _reindex_reminder(result: Result, moved_total: int) -> None:
    """Add the "go run `fha index`" reminder if this run has moved ANY file
    so far - the one thing every halt branch below must do, not just the
    normal-completion path (P2 audit finding, Codex review round 5, PR
    #188).

    `source_files` rows stay keyed to a moved document's OLD alias until
    `fha index` re-reads the record that now names its new one, so search
    and site builds answer from a stale path until a human runs it -
    exactly as true after a HALTED run as after a clean one. Before this
    fix, only the normal-completion path (the bottom of `run_reorganize`)
    said so; every halt branch above it (an unrecoverable rollback, a
    post-batch reconcile crash, a post-batch reconcile error, a post-batch
    issue-count regression) told the human only to run `fha reconcile` and
    retry `fha reorganize` - but a retry right after a halt on the FINAL
    planned batch can legitimately find nothing left to move (everything
    already moved) and finish "cleanly" without ever reaching the one
    branch that used to say this. Called with `moved_total == 0` this is a
    silent no-op - nothing moved, nothing to reindex.
    """
    if not moved_total:
        return
    result.add('info',
               f'{moved_total} file(s) already moved this run - run `fha index` so '
               'search and site builds see their new location(s), even though the rest '
               'of the plan was not attempted.')


def _finish_apply(result: Result, archive_root: Path, fha_config: dict) -> Result:
    """The one finalization every `--apply` return path must go through:
    the promised final `fha lint` pass, then `_finalize`.

    TOOLING §9a: "A final `fha lint` pass after the whole run (or after a
    halt) reports its error/warning counts for the human's awareness." The
    normal-completion path always reached this; every halt branch (a
    reconcile regression, a reconcile crash, an unrecoverable rollback) used
    to `return _finalize(result)` directly instead, so a halted run's own
    summary silently dropped the one promised check that would tell the
    human what state the rest of the archive is in (P2 audit finding, PR
    #188) - routing every one of those returns through here closes that gap
    once, rather than in each branch separately.

    The `lint.run_lint` call itself is caught (P2 audit finding, Codex
    review round 5, PR #188): before this fix, a crash inside `fha lint`
    (e.g. a record that turned unreadable partway through this very run)
    escaped straight past `_finish_apply` - and therefore past every caller
    of it, `run_reorganize` AND `_cmd_reorganize` - as a raw exception. The
    accumulated `result` this function was about to render (every batch's
    moved-file count and paths, the halt reason if this was a halt, the
    specific INCONSISTENT warning after an unrecoverable rollback) was lost
    with it: the CLI showed only a generic top-level traceback, and a human
    reading it had no way to tell how much of the run had already
    succeeded. A crashed final lint pass is folded into the SAME `result`
    as a warning instead, so everything collected so far still prints.
    """
    try:
        final_lint = lint.run_lint(archive_root, fha_config)
    except Exception as e:
        result.add('warning',
                   f'fha lint crashed while running its final pass after reorganizing '
                   f'({e}) - the report above (what moved, and why this run stopped, if '
                   'it did) is still accurate; only this last health check could not '
                   'finish. Run `fha lint` yourself to see the archive\'s current state.')
        return _finalize(result)
    n_err = int((final_lint.data or {}).get('n_errors') or 0)
    n_warn = int((final_lint.data or {}).get('n_warnings') or 0)
    result.add('info',
               f'fha lint after reorganizing: {n_err} error(s), {n_warn} warning(s) '
               '(run `fha lint` for the detail).')
    return _finalize(result)


def _stdin_is_interactive() -> bool:
    """Whether a [y/N] confirmation can actually be answered (tests patch
    this) - the same seam `process.py` uses for its own confirm gate."""
    return sys.stdin.isatty()


def _prompt(message: str) -> str:
    """Read one line of interactive input (monkeypatched in tests)."""
    return input(message)


def run_reorganize(
    archive_root: Path, fha_config: dict, *,
    apply: bool = False,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    group_threshold: int = _DEFAULT_GROUP_THRESHOLD,
    limit: int | None = None,
    assume_yes: bool = False,
) -> Result:
    """Engine: survey the documents root, and (with `apply=True`) execute the
    plan in re-checked batches. `apply=False` (the default) only plans and
    reports - nothing is ever written.

    The Result's `data` carries `{'status', 'planned', 'no_op',
    'excluded_human', 'problems', 'moved', 'moved_records', 'failed',
    'halted'}` - `moved` is a FILE count, `moved_records` the (never
    larger) count of records that actually succeeded, distinct from how
    many were merely attempted. Exit code follows the messages
    (`_finalize`): a clean plan or clean apply is 0; pre-existing problems
    or a mid-run halt are 1; a record left in a non-obvious inconsistent
    state after a rollback that could not fully finish is 3.

    **How a batch halt is decided** (the tool's own adversarial-review
    answer to "what if fha reconcile finds a problem this run did not
    cause"): before the FIRST batch, `fha reconcile --dry-run` is run once
    to count how many things it already has to complain about (its own
    warning/error message count, `_issue_count`) - call it the baseline.
    After EVERY batch, reconcile is re-run and counted again. If the count
    went UP compared to just before that batch, THIS batch's own moves are
    the only thing that could have caused it (nothing else was running -
    TOOLING.md's "single writer, no lock"), so the run halts immediately,
    reports exactly what changed, and does not touch anything further. If
    the count stayed the same, whatever reconcile is still unhappy about
    was already true before this run started - not this tool's business to
    fix, and not a reason to stop making the rest of the requested progress
    either. Files already moved before a halt are NOT rolled back: each one
    was its own atomic, verified transaction (`_apply_one_record`), so the
    archive is self-consistent up to the halt point - halting stops FURTHER
    change, it does not undo correct completed work.

    **`--batch-size` is a real bound, not a suggestion** (P2 audit finding,
    Codex review round 5, PR #188): a record that alone owns more eligible
    files than `--batch-size` is skipped this run - reported by name, never
    silently moved in one oversized batch - rather than exceeding the
    number of files a human actually asked to be moved between reconcile
    checks. Raising `--batch-size` (or simply re-running later) includes it.

    **A halt after the moves reminds the human to reindex too** (P2 audit
    finding, Codex review round 5, PR #188): every halt branch below - not
    just the normal-completion path - adds the `fha index` reminder the
    instant `moved_total` is nonzero, because a retry immediately after a
    halt can legitimately find nothing left to plan (everything already
    moved) and complete "cleanly" without ever reaching the normal path's
    own reminder.
    """
    result = Result(data={'status': 'ok', 'planned': 0, 'moved': 0, 'moved_records': 0,
                          'failed': 0, 'no_op': 0, 'excluded_human': 0, 'problems': 0,
                          'halted': False})

    if is_working_copy(archive_root):
        result.data['status'] = 'working-copy'
        result.add('info',
                   'This is a working copy - documents live on the main machine, so '
                   'there is nothing to reorganize here. Run `fha reorganize` there.')
        return _finalize(result)

    documents_root = resolve_path('documents', fha_config, archive_root)
    if not documents_root.is_dir():
        result.add('warning',
                   f'The documents folder is not reachable at {documents_root} - if it '
                   'lives on an external drive, plug it in; if the location changed, '
                   'update roots: in fha.yaml.')
        return _finalize(result)

    # False-success guard (P2 audit finding, PR #188): `sources/` missing
    # entirely used to fall all the way through to "nothing to reorganize -
    # every eligible document is already tidy" (exit 0) - `walk_files` and
    # `_iter_source_records` both silently yield nothing for a root that
    # will not even list, rather than treating it as the unreachable-root
    # problem it actually is. Refuse plainly instead, mirroring how the
    # documents root's own unreachability is handled just above.
    sources_dir = archive_root / 'sources'
    if not sources_dir.is_dir():
        result.add('warning',
                   f'The sources folder is not reachable at {sources_dir} - every record '
                   'there has to be read to know what can safely move; without it, '
                   'nothing can be planned. If it lives on an external drive, plug it in; '
                   'if the location changed, check fha.yaml. Nothing was planned.')
        return _finalize(result)

    plan = _plan(archive_root, fha_config, documents_root, result,
                 group_threshold=group_threshold)
    if plan['unreadable_dirs']:
        shown = ', '.join(sorted(_display_path(d, archive_root)
                                  for d in plan['unreadable_dirs'])[:5])
        result.add('warning',
                   f'{len(plan["unreadable_dirs"])} folder(s) could not be opened, so '
                   f'nothing was planned: {shown}. Reconnect the drive (or restore your '
                   'access to the folder), then re-run.')
        return _finalize(result)

    # Fail closed on any unparseable record (P1 audit finding, PR #188 third
    # pass): `_plan`'s duplicate-ownership guard can only be trusted when it
    # has seen every record's own files: claims, so a record that will not
    # even parse must block planning for EVERY record, not just itself - see
    # `_iter_source_records`'s docstring for the concrete failure this
    # prevents (a broken record's ownership claim silently vanishing from
    # `canonical_owners` while a different record moves the file it shares).
    if plan['unparseable_records']:
        shown = '; '.join(sorted(plan['unparseable_records'])[:5])
        more = len(plan['unparseable_records']) - 5
        if more > 0:
            shown += f'; and {more} more'
        result.add('warning',
                   f'{len(plan["unparseable_records"])} source record(s) could not be '
                   f'parsed, so nothing was planned for ANY record: {shown}. A broken '
                   "record's own files: claims would be invisible to the duplicate-"
                   'ownership check that keeps two records from fighting over the same '
                   'file, so the whole survey refuses until every record parses cleanly. '
                   'Fix the record(s) by hand (`fha lint` names the spot), then re-run.')
        return _finalize(result)

    for alias, rec_name in plan['excluded_human']:
        result.add('info',
                   f'Left in place (already organized by hand): {alias} ({rec_name})')
    for p in plan['problems']:
        result.add('warning', p)

    total_moves = sum(len(v) for v in plan['moves'].values())
    result.data.update({'no_op': plan['no_op_count'],
                        'excluded_human': len(plan['excluded_human']),
                        'problems': len(plan['problems'])})

    if plan['no_op_count']:
        result.add('info',
                   f'{plan["no_op_count"]} eligible file(s) are already exactly where '
                   'they belong - nothing to do for them.')

    record_order = sorted(plan['moves'].keys())
    limit_emptied_a_real_plan = False
    if limit is not None:
        original_order = record_order
        trimmed: list[Path] = []
        count = 0
        for rp in record_order:
            # A hard file cap (P2 audit finding, PR #188): checking `count`
            # only BEFORE appending a whole record let a single multi-file
            # record push well past `--limit` (`--limit 1` still planned
            # every file of a ten-file first record). `_apply_one_record`'s
            # per-record atomicity means a record's files always move
            # together or not at all, so honoring the cap exactly means
            # skipping the WHOLE record that would cross it, never
            # splitting it - not silently exceeding the number the human
            # asked for.
            #
            # `continue`, not `break` (P2 audit finding, PR #188 third
            # pass): a `break` stopped scanning the instant ONE record did
            # not fit, so a big record sorted before a small one emptied
            # the whole plan even when the smaller, later record would fit
            # exactly under the same cap (three-file record first, one-file
            # record second, `--limit 1` used to plan nothing at all).
            # Keep scanning past an oversized record so a later, smaller
            # one still gets its chance.
            n = len(plan['moves'][rp])
            if count + n > limit:
                continue
            trimmed.append(rp)
            count += n
        if not trimmed and original_order:
            limit_emptied_a_real_plan = True
            smallest = min(len(plan['moves'][rp]) for rp in original_order)
            result.add('info',
                       f'--limit {limit} is smaller than the smallest remaining record\'s '
                       f'own file count ({smallest}) - a record\'s files always move '
                       'together, so nothing fits under the cap without splitting one. '
                       'Raise --limit (or drop it) to include at least one record, then '
                       're-run.')
        elif len(trimmed) < len(original_order):
            result.add('info',
                       f'--limit {limit} applied: only the first {count} file(s) across '
                       f'{len(trimmed)} record(s) are planned this run (a record whose '
                       'own files would cross the limit is skipped whole, never split).')
        record_order = trimmed

    # Enforce the advertised batch-size bound (P2 audit finding, Codex
    # review round 5, PR #188): `--batch-size`'s own CLI help text, TOOLING
    # §9a, and tools/README.md all promise "N files per batch" - but a
    # single record that OWNS more eligible files than `--batch-size` used
    # to get a batch entirely its own anyway, silently exceeding the bound
    # a human actually asked for and delaying the post-batch `fha
    # reconcile` safety re-check until MORE files than that bound had
    # already moved. `_chunk_records`'s per-record atomicity (a record's
    # files always move together, SPEC-consistent with `_apply_one_record`)
    # means the fix cannot be "split it" - so it is skipped instead, same
    # "skip the record that does not fit, keep scanning for ones that do"
    # posture `--limit` already takes just above (a different guard: this
    # one is a stated numeric promise, not the ownership-map integrity
    # concern finding 1's fail-closed refusal protects) - reported plainly
    # so the human can raise --batch-size (or just re-run later) to include
    # it, rather than the bound being silently violated.
    batch_oversized = [rp for rp in record_order if len(plan['moves'][rp]) > batch_size]
    if batch_oversized:
        for rp in sorted(batch_oversized):
            n = len(plan['moves'][rp])
            result.add('info',
                       f'{rp.name}: this record owns {n} eligible file(s), more than '
                       f'--batch-size {batch_size} - a record\'s files always move '
                       'together, so it is skipped this run rather than moved in one '
                       f'oversized batch. Raise --batch-size to at least {n} (or just '
                       're-run later) to include it.')
        record_order = [rp for rp in record_order if rp not in batch_oversized]
    total_moves = sum(len(plan['moves'][rp]) for rp in record_order)

    result.data['planned'] = total_moves
    if not total_moves:
        if not limit_emptied_a_real_plan and not batch_oversized:
            result.add('info', 'Nothing to reorganize - every eligible document is already tidy.')
        return _finalize(result)

    for rp in record_order:
        for old_alias, new_alias, _oa, _na in plan['moves'][rp]:
            verb = 'Would move' if not apply else 'Will move'
            result.add('info', f'[{"dry-run" if not apply else "plan"}] {verb} '
                               f'{old_alias} -> {new_alias} ({rp.name})')

    if not apply:
        result.add('info',
                   f'[dry-run] {total_moves} file(s) across {len(record_order)} '
                   'record(s) would move. Nothing was written. Re-run with --apply to '
                   'do it.')
        return _finalize(result)

    if not assume_yes:
        if not _stdin_is_interactive():
            result.add('error',
                       'fha reorganize --apply needs a confirmation, and there is no '
                       'one here to ask. Re-run with --yes to confirm.')
            return _finalize(result)
        try:
            answer = _prompt(
                f'Move {total_moves} file(s) across {len(record_order)} source '
                'record(s)? [y/N] ').strip().lower()
        except EOFError:
            result.add('error',
                       'fha reorganize --apply needs a confirmation, and there is no '
                       'one here to ask. Re-run with --yes to confirm.')
            return _finalize(result)
        if answer not in ('y', 'yes'):
            result.add('info', 'Not reorganized - nothing changed.')
            return _finalize(result)

    try:
        baseline = reconcile.run_reconcile(archive_root, fha_config, dry_run=True)
    except Exception as e:
        result.add('error',
                   f'fha reconcile crashed while establishing a baseline before this run '
                   f'started ({e}) - fix that first, then re-run fha reorganize. Nothing '
                   'was moved. Run `fha reconcile` yourself to see the detail.')
        return _finalize(result)
    if any(m.level == 'error' for m in baseline.messages):
        # Surface the real reason (P2 audit finding, PR #188): this used to
        # point at "its own messages" without ever rendering any of them,
        # and named no concrete next command - fixed by inlining reconcile's
        # own error text AND naming `fha reconcile` explicitly.
        detail = '; '.join(m.text for m in baseline.messages if m.level == 'error')
        result.add('error',
                   f'fha reconcile could not run cleanly before this run started '
                   f'({detail}) - fix that first, then re-run fha reorganize. Nothing '
                   'was moved. Run `fha reconcile` yourself to see the full detail.')
        return _finalize(result)
    prev_issue_count = _issue_count(baseline)

    batches = _chunk_records(record_order, plan['moves'], batch_size)
    moved_total = 0
    moved_records = 0
    failed_total = 0
    for batch_idx, batch in enumerate(batches, start=1):
        for rp in batch:
            ok, msg, unrecoverable = _apply_one_record(rp, plan['moves'][rp])
            if ok:
                moved_total += len(plan['moves'][rp])
                moved_records += 1
                result.note_changed(rp)
                # Every moved DOCUMENT, not just the rewritten record (P2
                # audit finding, PR #188 third pass): `Result.changed` is
                # this codebase's established contract for "which paths did
                # this operation create/write/rename/embed into" (see e.g.
                # `confirm.py`'s dismiss-pair rename noting both old and new
                # path, `source.py extract`'s asset+record pair) - a
                # headless/structured caller reading `as_dict()` needs to
                # know which actual asset files moved, not just that some
                # record's text changed.
                for _old_alias, _new_alias, _old_abs, new_abs in plan['moves'][rp]:
                    result.note_changed(new_abs)
                result.add('info', f'Moved: {msg}')
            elif unrecoverable:
                # An unrecoverable rollback (P1 audit finding, PR #188) must
                # stop the WHOLE run right here - no further records in
                # this batch, no further batches - not be folded into an
                # ordinary warning-and-continue the way a clean failure is:
                # this one record is left genuinely inconsistent, and
                # letting the run keep changing OTHER records while that is
                # true is exactly the hazard the audit named. 'error' (not
                # 'warning') is what makes `_finalize` return exit code 3,
                # the documented "left inconsistent" contract.
                failed_total += 1
                result.data.update({'moved': moved_total, 'moved_records': moved_records,
                                    'failed': failed_total, 'halted': True})
                result.add('error', msg)
                _reindex_reminder(result, moved_total)
                return _finish_apply(result, archive_root, fha_config)
            else:
                failed_total += 1
                result.add('warning', msg)

        try:
            check = reconcile.run_reconcile(archive_root, fha_config, dry_run=True)
        except Exception as e:
            # A crashed verification step must not swallow the report of
            # what already moved (P2 audit finding, PR #188, second pass):
            # halt exactly like a reconcile-detected regression below, but
            # keep the accumulated moved/failed counts instead of letting
            # the exception escape uncaught past this whole function.
            #
            # 'warning' (exit 1), not 'error' (exit 3) - P2 audit finding,
            # PR #188 third pass: TOOLING §9a and tools/README.md both
            # reserve exit 3 for a record left genuinely INCONSISTENT after
            # an unrecoverable rollback, or for `fha reconcile` failing to
            # run BEFORE the first batch (nothing moved yet). A crash HERE
            # happens AFTER a batch that itself completed - every file move
            # in it was its own verified atomic transaction
            # (`_apply_one_record`) - so the archive is self-consistent;
            # verification just could not finish checking it. That is the
            # documented exit-1 "mid-run halt (the archive is left
            # consistent either way)" case, the same as the reconcile-
            # regression halt just below it, not the worst-case exit-3
            # honesty a genuinely broken record deserves.
            result.data.update({'moved': moved_total, 'moved_records': moved_records,
                                'failed': failed_total, 'halted': True})
            result.add('warning',
                       f'fha reconcile crashed while re-checking after batch {batch_idx} '
                       f'({e}) - halting here. {moved_total} file(s) moved so far are '
                       'safe (each record was updated atomically); the rest of the plan '
                       'was not attempted. Run `fha reconcile` yourself to see what is '
                       'happening, fix it, then re-run `fha reorganize`.')
            _reindex_reminder(result, moved_total)
            return _finish_apply(result, archive_root, fha_config)
        if any(m.level == 'error' for m in check.messages):
            # 'warning' (exit 1), not 'error' (exit 3) - same reasoning as
            # the crash branch just above: this batch itself already
            # completed safely, so the archive is self-consistent even
            # though reconcile could not finish confirming it.
            result.data.update({'moved': moved_total, 'moved_records': moved_records,
                                'failed': failed_total, 'halted': True})
            result.add('warning',
                       f'fha reconcile could not run cleanly after batch {batch_idx} - '
                       f'halting here. {moved_total} file(s) moved so far are safe (each '
                       'record was updated atomically); the rest of the plan was not '
                       'attempted. Run `fha reconcile` to see the detail, fix it, then '
                       're-run `fha reorganize`.')
            _reindex_reminder(result, moved_total)
            return _finish_apply(result, archive_root, fha_config)
        cur_issue_count = _issue_count(check)
        if cur_issue_count > prev_issue_count:
            result.data.update({'moved': moved_total, 'moved_records': moved_records,
                                'failed': failed_total, 'halted': True})
            result.add('warning',
                       f'fha reconcile found {cur_issue_count - prev_issue_count} new '
                       f'issue(s) after batch {batch_idx} - halting here so nothing else '
                       f'changes. {moved_total} file(s) moved so far are safe; the rest '
                       'of the plan was not attempted. Run `fha reconcile` for the '
                       'detail, then re-run `fha reorganize` once it is clear.')
            _reindex_reminder(result, moved_total)
            return _finish_apply(result, archive_root, fha_config)
        prev_issue_count = cur_issue_count

    result.data.update({'moved': moved_total, 'moved_records': moved_records,
                        'failed': failed_total})
    if moved_total:
        # Only records that actually succeeded (P2 audit finding, PR #188):
        # `len(record_order)` counted every record ATTEMPTED, including any
        # that failed and were cleanly rolled back - a partial run's own
        # summary must not claim a failed-and-reverted record as "moved".
        result.add('info',
                   f'Moved {moved_total} file(s) across {moved_records} record(s). '
                   'Run `fha index` so searches see the new locations.')
    return _finish_apply(result, archive_root, fha_config)


# -- CLI ----------------------------------------------------------------------

_CLI_DESCRIPTION = (
    "Tidy the documents/ root: group loose, machine-filed documents into "
    "documents/{type}/ (mirroring how sources/{type}/ is already organized), "
    "and give a source with many files its own subfolder. Every source's "
    "files: entry is updated in the same step as the physical move, so "
    "nothing ever points at the wrong place.\n\n"
    "SAFETY - read this before running with --apply: a file already sitting "
    "inside ANY folder a human created by hand - even a shallow one - is "
    "never touched, never even proposed as a candidate. Only material still "
    "sitting exactly where `fha process` originally filed it (flat at the "
    "documents root, or one level down in its own type folder) is eligible. "
    "If you (or anyone) moved something by hand, this tool leaves it alone "
    "on purpose - moving a person's deliberately-organized file would be far "
    "worse than leaving something merely untidy.\n\n"
    "Always previews first: run with no flags (or --dry-run) to see the "
    "full plan and change nothing. Add --apply to actually move files, in "
    "bounded batches, re-checked with fha reconcile after each one - if "
    "anything looks wrong afterward, the run stops right there and reports "
    "it, without touching anything further. Photos are out of scope: SPEC "
    "identifies a photo by its embedded keyword, never by its path, so "
    "there is no folder to tidy - fha reconcile's own photo pass already "
    "re-verifies photo identity after any move you make by hand."
)


def _cmd_reorganize(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha reorganize')
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        print('Fix fha.yaml (or run `fha doctor` for a check), then re-run '
              '`fha reorganize`.', file=sys.stderr)
        return EXIT_FAILURE

    # Reject rather than silently rewrite (P2 audit finding, PR #188 third
    # pass): `--batch-size 0` used to become the default 25, a negative
    # batch size became 1, and `--group-threshold 0` became the default 3 -
    # even though 0 has a coherent, DIFFERENT meaning a human might
    # genuinely type: every non-empty source group exceeds the threshold,
    # i.e. "always give a source its own subfolder, never leave 2-3 files
    # loose in the type folder". Silently substituting the default there
    # applies a materially different folder layout than what was actually
    # requested, with no warning. `--group-threshold` therefore honors 0 as
    # a real, meaningful value (only negative is rejected); `--batch-size`
    # has no such reading for 0 or below (a batch of zero files can never
    # make progress), so any non-positive value is rejected outright - same
    # CLI-boundary posture as `--limit`'s own negative-value refusal below.
    batch_size = getattr(args, 'batch_size', None)
    batch_size = _DEFAULT_BATCH_SIZE if batch_size is None else int(batch_size)
    if batch_size < 1:
        print(f'ERROR: --batch-size must be a positive number of files (got {batch_size}).',
              file=sys.stderr)
        return EXIT_FAILURE
    group_threshold = getattr(args, 'group_threshold', None)
    group_threshold = (_DEFAULT_GROUP_THRESHOLD if group_threshold is None
                        else int(group_threshold))
    if group_threshold < 0:
        print('ERROR: --group-threshold must be zero or a positive number of files '
              f'(got {group_threshold}).', file=sys.stderr)
        return EXIT_FAILURE
    limit = getattr(args, 'limit', None)
    if limit is not None and limit < 0:
        # A mistyped `--limit -1` used to exit cleanly and claim "every
        # eligible document is already tidy" (P2 audit finding, PR #188,
        # second pass): the trimming loop's `count` starts at 0, so a
        # negative limit made its own boundary check true immediately,
        # trimming the plan to nothing without ever saying why. Reject it
        # here instead, at the CLI boundary, with a plain correction.
        print(f'ERROR: --limit must be zero or a positive number of files (got {limit}).',
              file=sys.stderr)
        return EXIT_FAILURE
    dry_run = bool(getattr(args, 'dry_run', False))
    apply_ = bool(getattr(args, 'apply', False)) and not dry_run

    result = run_reorganize(
        archive_root, fha_config,
        apply=apply_,
        batch_size=batch_size,
        group_threshold=group_threshold,
        limit=limit,
        assume_yes=bool(getattr(args, 'yes', False)),
    )
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'reorganize',
        help="Bulk-tidy the documents/ root (never touches a human-organized folder)",
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root')
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview the plan only and change nothing (also the default '
                        'with no --apply)')
    p.add_argument('--apply', action='store_true',
                   help='Actually move files and update records (previews only without this)')
    p.add_argument('--yes', action='store_true',
                   help='Skip the [y/N] confirmation before applying')
    p.add_argument('--batch-size', type=int, default=_DEFAULT_BATCH_SIZE, metavar='N',
                   help=f'Files per batch before re-checking with fha reconcile '
                        f'(default {_DEFAULT_BATCH_SIZE})')
    p.add_argument('--group-threshold', type=int, default=_DEFAULT_GROUP_THRESHOLD, metavar='N',
                   help='A source with more than N eligible files gets its own '
                        f'subfolder (default {_DEFAULT_GROUP_THRESHOLD}; 0 means every '
                        'non-empty source gets its own subfolder, never a shared type '
                        'folder)')
    p.add_argument('--limit', type=int, default=None, metavar='N',
                   help='Only plan/apply the first N eligible files this run, whole '
                        'records only - a record whose own files would cross N is '
                        'skipped, never split (default: no cap)')
    p.set_defaults(func=_cmd_reorganize)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha reorganize',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root')
    parser.add_argument('--dry-run', action='store_true', dest='dry_run')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--batch-size', type=int, default=_DEFAULT_BATCH_SIZE, metavar='N')
    parser.add_argument('--group-threshold', type=int, default=_DEFAULT_GROUP_THRESHOLD, metavar='N')
    parser.add_argument('--limit', type=int, default=None, metavar='N')
    args = parser.parse_args(argv)
    return _cmd_reorganize(args)


if __name__ == '__main__':
    raise SystemExit(_standalone_main())
