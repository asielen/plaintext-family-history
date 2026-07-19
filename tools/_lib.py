"""
_lib.py - shared library for all fha tools.

This is the foundation every other tool builds on.  Tools never import each
other - _lib.py is the only shared dependency (TOOLING §15 build rule).

What lives here:
  - ID grammar and validation  (Crockford Base32, SPEC §10)
  - EDTF date parsing and bounds computation  (TOOLING §1)
  - Record file parsing  (frontmatter + fenced claims block + body)
  - Path and alias resolution  (fha.yaml roots mapping)
  - Filename grammar parsing  (person and source naming conventions, SPEC §13)
  - Shared constants: claim types, source types, COMPANION_KINDS, significance
  - The Finding class and exit-code constants shared by lint and other tools
  - The Result contract (see below) every tool's run_* function returns

THE STRUCTURED-RESULT CONTRACT (the rule every `run_*` follows)
--------------------------------------------------------------
Every operation a tool performs is split in two:

  - `run_*` **computes** and **returns a `Result`** - a small, JSON-serializable
    record of what happened.  It does NOT print human-facing report text and does
    NOT call `sys.exit`.  (File side effects and interactive prompts are out of
    scope for this rule: a tool that must write `report_2026.md` or ask the human
    a yes/no question still does so inside `run_*`.  The rule governs return
    values and human-text *printing*, not side effects.)
  - `_cmd_*` is the **only** layer that renders a `Result` to stdout/stderr and
    returns the process exit code.

A `Result` carries:
  - `ok`        - did the operation succeed (no error-level messages)?
  - `exit_code` - the process exit code the CLI should return (EXIT_* constants).
  - `data`      - the structured payload: whatever a consumer would want as data
                  (matched records, per-check rows, counts, a rendered string …).
  - `messages`  - human-facing lines, each a `Message{level, text, next_step,
                  code, path}`.  A lint `Finding` folds into one of these:
                  severity → level, its E/W code → code, the file → path.
  - `changed`   - paths this operation created, wrote, renamed, or embedded into
                  (empty under --dry-run).

`lint` is the reference implementation: `run_lint` returns a `Result`; `_cmd_lint`
renders the existing human text and `--json` payload from it (TOOLING §3).
"""

from __future__ import annotations

import calendar
import dataclasses
import datetime
import itertools
import os
import re
import secrets
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised by fha.py import-path tests
    yaml = None  # type: ignore[assignment]

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Constants and patterns
#    CROCKFORD_ALPHA           - the 32-char ID alphabet (i l o u omitted)
#    ID_RE                     - bare ID pattern (SPEC §10)
#    TOKEN_RE, LEGACY_TOKEN_RE - [[ID]] / [[ID|display]] / [[ID#frag]] citation
#                                 tokens (superset incl. legacy [ID]) (SPEC §10)
#    FRONT_RE, CLAIMS_RE       - frontmatter and fenced claims block patterns
#    SIGNIFICANCE              - claim type → 'vital'/'substantive'/'incidental'
#    CLAIM_TYPES, VITAL_TYPES  - frozensets derived from SIGNIFICANCE
#    SOURCE_TYPES              - controlled vocabulary for source_type field
#    PERSON_SEX_VALUES         - controlled vocabulary for a person record's sex field (SPEC §9)
#    PHOTO_EXTENSIONS          - recognised photo/scan file extensions (photoindex + process)
#    COMPANION_KINDS           - generated file kinds that share a P-id with their profile
#
#  Archive configuration
#    find_archive_root         - walk up from CWD to find fha.yaml
#    archive_root_missing_message - one plain recovery message for missing roots
#    resolve_root_arg          - CLI --root flag (validated: must carry fha.yaml),
#                                 else find_archive_root(); one shared refusal message
#    load_fha_yaml             - parse fha.yaml into a dict
#    format_*_error            - shared teaching messages for CLI refusals
#    get_roots                 - extract roots mapping from config
#    resolve_path              - alias path ('photos/…') → absolute Path via fha.yaml
#    path_to_alias             - absolute Path → alias path ('photos/…'), the inverse
#
#  Index database access
#    db_mtime                  - mtime of a cache db file, or None if absent/unreadable
#    probe_sqlite              - does this db open and run this one probe query?
#    open_index_db             - open .cache/index.sqlite with the freshness check +
#                                 required-table probe every index-reading tool needs
#    photoindex_status         - classify .cache/photos.sqlite freshness for find/doctor
#
#  Record parsing
#    read_text_exact / write_text_exact - newline-exact record IO (no CRLF/LF translation)
#    reapply_newline           - restore a record's CRLF/LF convention after a text edit
#    yaml_inline                - single-line quoted YAML scalar (every surgical writer's rule)
#    _coerce_yaml              - normalise YAML scalar types for consistent comparisons
#    read_record               - parse frontmatter + claims + body from a .md file
#    claim_item_key_indent     - one claim item's real mapping-key column (surgical edits)
#    claims_edit_problem       - pre-write re-parse guard for surgical claims-block edits
#    ClaimEditRefused          - shared "surgical claims edit unsafe" exception
#    _find_claims_block        - locate the ## Claims ```yaml fence (line indices)
#    guard_claims_rewrite      - claims_edit_problem wrapped as a raise-on-problem guard
#    append_claim_to_source    - append a whole new claim item to a source's ## Claims block
#    is_merged_meta            - normalized SPEC §9 tombstone test (status: merged)
#    frontmatter_fence_span    - the ONE `---` fence grammar (exact, FRONT_RE-matched)
#    parse_frontmatter_strict  - frontmatter mapping via plain yaml.safe_load (no coercion)
#    frontmatter_edit_problem  - pre-write guard for surgical frontmatter edits
#    section_bounds            - locate one `## Heading` prose section's line span
#    lines_end_with_newline    - did this split('\n') list end in the EOF sentinel?
#    create_section_at_eof     - shared "heading missing, append it at EOF" tail
#    append_paragraph_to_section - add a paragraph at a `## Heading`'s end (shared by
#                                 person edit/note + source note; CRLF-safe, bounded)
#    split_log_entries         - an append-log section's entries (paragraph runs)
#    replace_paragraph_in_section - swap ONE entry of an append-log section (the
#                                 workbench's per-entry edit; matched by exact text)
#    parse_filename            - decompose filename into {id_str, kind, is_companion}
#    ParsedName, parse_media_filename - decompose an unprocessed photo/scan filename
#                                 into base_id + variant/part-kind/page/crop (TOOLING §6/§9)
#
#  EDTF handling
#    is_valid_edtf             - validate an EDTF string against this project's subset
#    normalize_date            - loose human date ("circa 1870", "1870s") → canonical EDTF
#    edtf_bounds               - compute (date_min, date_max) ISO strings
#    _pad_date, _last_day      - internal date-padding helpers
#
#  ID utilities
#    mint_ids                  - mint collision-checked Crockford IDs
#    normalize_id              - lowercase for consistent set/dict keying
#    is_valid_id               - syntactic validity check
#    id_type_of                - extract P/S/C/L/H type prefix
#    fmt_id_display            - uppercase the type prefix for display (p-xxx → P-xxx)
#    scan_ids_in_tree          - full-tree scan used by id mint for collision checking
#
#  Filename / path helpers
#    is_working_copy           - WORKING_COPY marker present at archive root?
#    is_fixture_path           - path under example-archive/ or tests/fixtures/?
#    find_person_record_path   - scan people/ for one P-id's record file (never the index)
#    find_source_record_path   - scan sources/ for one S-id's record file (never the index)
#    stub_slug_name            - display name → (surname_slug, given_slug) for a filename
#    stub_filename             - {surname}__{given}_{P-id}.md, the stub naming grammar
#    render_stub_content       - the stub frontmatter text `fha stubs`/`fha person new` write
#    extract_tokens            - (id, display, fragment, span) per citation token
#    extract_token_ids         - the IDs of all citation tokens in a text block
#    extract_bare_ids          - all bare IDs from a text block
#    normalize_place_text      - lowercase/collapse-whitespace key for comparing
#                                 free-text place names without a shared place_id
#
#  Alias resolution / publication guards
#    resolve_typed_ref         - structured-field ref → typed canonical ID (K4 shared home)
#    strip_unaccepted_drafts   - drop AI-DRAFT prose + AI markers pre-publication (fail-closed)
#    GENERATED_PREFIX, is_generated_text, is_generated_file - GENERATED-header ownership test
#
#  Archive freshness
#    newest_record_mtime       - max mtime of sources/people/notes .md + places.yaml
#    newest_source_record_mtime - max mtime of source .md records only
#    newest_person_record_mtime - max mtime of people/*.md only
#    configure_utf8_stdout     - reconfigure stdout to UTF-8 (Windows cp1252 compat)
#
#  Output helpers
#    EXIT_CLEAN / EXIT_WARNINGS / EXIT_ERRORS / EXIT_FAILURE  - shared exit codes
#    Finding                   - one lint finding: severity + code + path + message
#    emit_findings             - print findings list and return exit code
#    Message                   - one human-facing line: level/text/next_step (+code/path)
#    Result                    - the structured-result contract every run_* returns
#    finding_to_message        - fold a lint Finding into a Result Message
#    result_fail               - the shared refusal/not-found Result builder every
#                                 write-back engine delegates to (confirm/claim/person/source)
#    load_site_module          - import tools/site.py under the private `fha_site`
#                                 name (shared by the fha + serve front doors)
#
# ─────────────────────────────────────────────────────────────────────────────


# ── Regex patterns (TOOLING.md §1) ───────────────────────────────────────────

# Crockford Base32 alphabet - lowercase, omitting i l o u
CROCKFORD_ALPHA = '0123456789abcdefghjkmnpqrstvwxyz'

# Matches any bare ID in text (case-insensitive)
ID_RE = re.compile(r'\b([PSCLH])-([0-9a-hjkmnp-tv-z]{10})\b', re.I)

# The bare ID sub-pattern shared by every bracketed-token regex below.  Kept in
# one place so the token grammar and the ID grammar can never drift apart; it is
# exactly the `ID_RE` body without word boundaries or the split type/body groups.
_TOKEN_ID = r'[PSCLH]-[0-9a-hjkmnp-tv-z]{10}'

# Matches in-prose citation/cross-link tokens.  This is the single chokepoint
# every consumer (index, find, wikitree, site, packet, report, lint) resolves
# through, so it is deliberately a *superset*:
#
#   [[S-…]]                 canonical wikilink
#   [[P-…|Margaret Cole]]   …with a |display alias (renderer text; ignored here)
#   [[S-…#Claims]]          …with an Obsidian #heading fragment (parse-only)
#   [[C-…#^x|note]]         …with a #^block fragment and a display alias
#   [S-…]                   legacy single-bracket form (still resolved, forgivingly)
#
# Exactly ONE capturing group - the load-bearing ID - so the historical
# `TOKEN_RE.findall(text)` / `m.group(1)` consumers keep returning the ID and
# nothing else.  The |display and #fragment are matched but NOT captured here;
# the renderers that must re-emit display text use `extract_tokens()` instead.
# The optional second bracket on each side (`\[?` / `\]?`) is what makes the
# single-bracket legacy form resolve through the same pattern.
TOKEN_RE = re.compile(
    r'\[\[?'                # one or two opening brackets
    rf'({_TOKEN_ID})'       # 1: the ID (the only captured, load-bearing group)
    r'(?:#[^|\]]*)?'        # optional #heading / #^block fragment (parse-only)
    r'(?:\|[^\]]*)?'        # optional |display alias
    r'\]\]?',               # one or two closing brackets
    re.I,
)

# The same grammar as TOKEN_RE, but capturing the fragment and display so the
# renderers (wikitree, site) can re-emit a human's chosen display text.  Powers
# `extract_tokens()`; consumers that only need IDs stay on TOKEN_RE so their
# `findall`/`group(1)` contract is untouched.
_TOKEN_PARTS_RE = re.compile(
    r'\[\[?'
    rf'({_TOKEN_ID})'       # 1: ID (load-bearing)
    r'(?:#([^|\]]*))?'      # 2: #fragment (parse-only; no tool ever emits one)
    r'(?:\|([^\]]*))?'      # 3: |display alias
    r'\]\]?',
    re.I,
)

# The legacy single-bracket form on its own, used by the explicit normalize pass
# to find `[ID]` tokens worth upgrading to `[[ID]]`.  The lookbehind/lookahead
# keep it from matching the inner brackets of an already-canonical `[[ID]]`, so a
# normalize sweep never double-counts or re-wraps a token that is already double.
LEGACY_TOKEN_RE = re.compile(
    rf'(?<!\[)\[({_TOKEN_ID})\](?!\])',
    re.I,
)

# Any double-bracket Obsidian wikilink, whose target may be an ID *or* a human
# name/stem (`[[Ken Smith]]`, `[[grandmas-album]]`, `[[P-…|Ken Smith]]`). Looser
# than TOKEN_RE - it does not require an ID body - so the citation indexer and
# `fha normalize-links` can find name/stem links that resolve through the alias
# map. Captures: 1 target, 2 #fragment, 3 |display.
WIKILINK_RE = re.compile(
    r'\[\['
    r'([^\[\]|#]+?)'        # 1: target (id, name, or stem) - no brackets/pipe/hash
    r'(?:#([^\[\]|]*))?'    # 2: optional #heading / #^block fragment
    r'(?:\|([^\[\]]*))?'    # 3: optional |display alias
    r'\]\]'
)

# Extracts YAML frontmatter (between first --- pair)
FRONT_RE = re.compile(r'\A---\r?\n(.*?)\r?\n---\r?\n', re.S)

# Extracts fenced YAML claims block under ## Claims
CLAIMS_RE = re.compile(r'^## Claims.*?```yaml\r?\n(.*?)```', re.S | re.M)

# ── Significance table (SPEC §8.2) ────────────────────────────────────────────

SIGNIFICANCE: dict[str, str] = {
    'birth': 'vital', 'death': 'vital', 'marriage': 'vital',
    'baptism': 'vital', 'burial': 'vital',
    'residence': 'substantive', 'census': 'substantive',
    'occupation': 'substantive', 'education': 'substantive',
    'military': 'substantive', 'immigration': 'substantive',
    'divorce': 'substantive', 'name': 'substantive',
    'relationship': 'substantive',
    'event': 'incidental', 'note': 'incidental',
}

CLAIM_TYPES: frozenset[str] = frozenset(SIGNIFICANCE.keys())

VITAL_TYPES: frozenset[str] = frozenset(
    t for t, sig in SIGNIFICANCE.items() if sig == 'vital'
)

# Optional, UNSOURCED person-record fields: an honest estimate of current
# knowledge ("Grandpa, b. 1923") a hand-author may jot down long before any
# source exists. They are explicitly non-load-bearing - like the §8.6 convenience
# flags - and a real `birth`/`death` claim supersedes them the moment it exists.
# Tools must never count a provisional date as a satisfied vital for completeness
# scoring; the linter only *tracks* it on a gentle needs-sourcing worklist.
PROVISIONAL_VITAL_FIELDS: frozenset[str] = frozenset({'birth', 'death'})

# Bloodline-aware Ahnentafel (SPEC §12.2). A parent/child relationship carries a
# `subtype` naming the *nature* of the bond (§8.2). The pedigree NUMBERING follows
# only the genetic edges; the social/legal kinds below are shown in the bracket
# lists and relationship views but never numbered into the pedigree.
GENETIC_PARENT_SUBTYPES: frozenset[str] = frozenset({
    'biological', 'surrogate-genetic', 'donor-sperm', 'donor-egg',
})
SOCIAL_PARENT_SUBTYPES: frozenset[str] = frozenset({
    'adoptive', 'step', 'foster', 'guardian', 'surrogate-gestational', 'social',
})
# How a non-birth child reads in a couple-folder bracket list (`Ruth (adopted)`).
_NONBIRTH_BRACKET_LABEL: dict[str, str] = {
    'adoptive': 'adopted', 'step': 'step', 'foster': 'foster',
    'guardian': 'guardian', 'surrogate-gestational': 'surrogate', 'social': 'social',
}


def is_genetic_parent_subtype(subtype: Any) -> bool:
    """Does a parent edge of this nature count toward the genetic pedigree?

    Genetic UNLESS the nature is an explicit social/legal kind (adoptive, step,
    foster, guardian, surrogate-gestational, social). An unset, legacy (`child-of`),
    or unrecognised subtype defaults to genetic, so a legacy archive numbers
    exactly as it did before bloodline awareness (SPEC §12.2 back-compat)."""
    return str(subtype or '').strip().lower() not in SOCIAL_PARENT_SUBTYPES


def nonbirth_bracket_label(subtype: Any) -> str | None:
    """The bracket annotation for a non-birth child ('adopted', 'step', …), or
    None for a genetic/birth edge that needs no mark."""
    return _NONBIRTH_BRACKET_LABEL.get(str(subtype or '').strip().lower())


def format_bracket_child(given_name: str, label: str | None) -> str:
    """One child's bracket entry: a bare given name, or `Given (label)` when the
    child joined other than by birth. Shared by lint (W103) and views (W103) so
    both derive byte-identical bracket lists (SPEC §12.2, TOOLING §7)."""
    return f'{given_name} ({label})' if label else given_name

# The keys that mark a YAML mapping as a claim, used to recognise hand-written
# claims a human typed under `## Claims` but forgot to fence (read_record reads
# them anyway so they are never silently lost; lint offers to wrap the fence).
_CLAIM_MARKER_KEYS: frozenset[str] = frozenset({'id', 'type', 'value', 'persons', 'status'})

SOURCE_TYPES: frozenset[str] = frozenset({
    'census', 'vital-record', 'newspaper', 'photo', 'interview', 'letter',
    'military-record', 'land-record', 'probate', 'directory', 'dna', 'book',
    'website', 'artifact', 'proof-argument', 'other',
})

EDTF_EXAMPLE_TEXT = 'like 1880, 1880-06-15, or 188X for "the 1880s"'


def source_type_list() -> str:
    """Return the controlled source_type vocabulary in a stable display order.

    The same list appears in CLI refusals, lint findings, and docs. Keeping the
    formatting here prevents one tool from teaching a shorter or stale version
    of the vocabulary than another.
    """
    return ', '.join(sorted(SOURCE_TYPES))


def format_source_type_error(value: object, *, where: str = 'source_type') -> str:
    """Explain an unknown source type with the valid list and a concrete fix.

    `source_type` is archive jargon, so every hard refusal that names it must
    also say what it means: the source category stored on a source record. The
    caller supplies `where` when the bad value came from a flag or sidecar file.
    """
    return (
        f'unknown {where} {value!r}. source_type means the source category, '
        f'for example census or photo. Use one of: {source_type_list()}.'
    )


# A person's birth-assigned sex (SPEC §9): optional, and distinct from the
# free-text `gender` field beside it. Kept as a small controlled vocabulary
# (like SOURCE_TYPES) rather than free text so `fha person new` and any future
# validator can catch a typo ("m" vs "M", "male") before it lands in a record.
PERSON_SEX_VALUES: frozenset[str] = frozenset({'M', 'F', 'intersex', 'unknown'})


def format_person_sex_error(value: object) -> str:
    """Explain an unrecognised `sex` value with the valid list and a plain gloss.

    `sex` is optional and easy to confuse with `gender` (the free-text identity
    field beside it, SPEC §9), so the refusal spells out the distinction rather
    than just naming the field.
    """
    return (
        f'unrecognised sex {value!r}. sex records birth-assigned sex where a '
        f"record states it (separate from gender, which is free text) - use one "
        f'of: {", ".join(sorted(PERSON_SEX_VALUES))}.'
    )


# Claim confidence (SPEC §8.5): evidence quality, required on every claim and
# distinct from status (review state). Kept here as the one canonical
# vocabulary + defaulting rubric so every claim-minting path (fha claim new
# today, any future drafting pass) defaults identically. lint.py's
# VALID_CONFIDENCE mirrors the same three values for validation.
CONFIDENCE_VALUES: tuple[str, ...] = ('high', 'medium', 'low')

_CONFIDENCE_BY_SOURCE_TYPE: dict[str, str] = {
    'vital-record': 'high',
    'interview': 'low',
}


def default_confidence(source_type: object) -> str:
    """Default a new claim's confidence from its source's source_type.

    SPEC §8.5 (locked): "Tooling defaults confidence from source_type
    (vital-record -> high, census/newspaper -> medium, interview hearsay ->
    low) and only asks the human when the source class is ambiguous." The
    rubric's named anchors are mapped explicitly; every other source type
    lands on 'medium' ("single source with moderate specificity") - the
    conservative middle, never silently wrong in the dangerous direction.
    The human always overrides with --confidence.
    """
    return _CONFIDENCE_BY_SOURCE_TYPE.get(str(source_type or ''), 'medium')


# The asset-root aliases a workbench front door may expose over HTTP and the
# only roots its confinement checks accept (photos/documents may be remapped
# outside the archive by fha.yaml `roots:`; inbox is the drop folder). serve.py
# uses this for /root/<alias>/ confinement and site.py mirrors it when writing
# workbench-mode hrefs - one constant so the two can never drift apart.
ASSET_ROOT_ALIASES: tuple[str, ...] = ('photos', 'documents', 'inbox')


def format_edtf_error(value: object, *, field: str = 'date') -> str:
    """Explain an unreadable date with examples the human can copy.

    EDTF is the archive's compact date form. As of PR 05 the tools first try to
    READ loose human input (`normalize_date`: "circa 1870" → "1870~", "1870s" →
    "187X") and only fall back to this hard message when no clear reading exists -
    so this is reserved for genuinely ambiguous values, and it teaches the
    accepted shapes (including the natural phrasings now understood) rather than
    stopping at the acronym.
    """
    return (
        f'{field} {value!r} is not a date the archive can read. '
        f'Write it {EDTF_EXAMPLE_TEXT}, or in plain words like '
        f'"about 1880", "before 1880", or "the 1880s".'
    )


def format_exiftool_error(command: str = 'fha process') -> str:
    """Explain that photo features need exiftool and name the recovery command.

    `exiftool` is an external program used for the only sanctioned photo writes:
    reading and adding metadata keywords. A missing binary is not a data error,
    so the message tells the user what capability is blocked and where to check
    the archive after installation.
    """
    return (
        f'{command} needs exiftool for photo metadata. Install exiftool and make '
        f'sure the `exiftool` command works, then run `{command}` again. '
        'Run `fha doctor` to check your archive.'
    )


def format_yaml_dependency_error() -> str:
    """Return the central missing-PyYAML message used before config parsing.

    Most tools read `fha.yaml`, source records, or claims through PyYAML. Import
    failure used to surface as a Python traceback; this text gives the install
    line and a verification command instead.
    """
    return (
        'This tool needs PyYAML to read archive YAML files. Install it with '
        '`python -m pip install pyyaml`, then run `fha doctor` to check your archive.'
    )


def archive_root_missing_message() -> str:
    """Return the one archive-root recovery message shared by every entry point."""
    return (
        'cannot find archive root (no fha.yaml found). Run this from inside the '
        'archive, or add `--root PATH` with the folder that contains fha.yaml.'
    )

# Common raster and camera-raw extensions a personal photo library mixes in.
# Canonical home for the set so that `photoindex` (cataloguing) and `process`
# (document-vs-photo intake detection) agree on what counts as a photo without
# either tool importing the other (tools never import tools - TOOLING §15).
PHOTO_EXTENSIONS: frozenset[str] = frozenset({
    '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.gif', '.heic', '.heif',
    '.cr2', '.nef', '.dng', '.arw', '.orf', '.rw2',
})

# Companion file kinds: generated view files that share a P-id with their profile
# and live in the same folder.  Enumerated here so that parse_filename (kind
# detection) and index.py (person_files.kind column) stay in sync when new view
# types are added - add the kind here, and both consumers pick it up automatically.
COMPANION_KINDS: frozenset[str] = frozenset({'research', 'timeline', 'sources-index', 'draft-queue'})

# Disposable cache schema versions. These are deliberately small integers stored
# in both a meta row and PRAGMA user_version so humans and SQLite tools can see
# which cache shape a file was built with.
# v2: rights.publication_ok is now stored three-state (1/0/NULL) instead of
# folding explicit false to NULL. Exporters redact on `COALESCE(publication_ok,
# 1) = 0`, which only fires on a stored 0 - so a v1 index (false → NULL) would
# silently under-redact publication_ok:false sources. Bumping forces `fha index`
# to rebuild before the redaction-critical consumers (site/gedcom/wikitree) trust it.
# v3: adds the `aliases` table (the resolution surface - record IDs, human
# stems, on-demand C-ids, person/place names) and the `source_places` edge.
# A v2 index lacks both, so name-first cross-links and stem citations would
# silently fail to resolve until a rebuild; bumping forces `fha index` to run.
# v4: adds the provisional `birth`/`death` person columns (unsourced estimates
# the needs-sourcing backlog reads) - a v3 index lacks them, so bump to rebuild.
# v5: typed `restricted:` values (`dna`, `by-request`, `deadname`, ...) now
# index as restricted = 1. A v4 index stores 0 for them - the strongest
# privacy markers reading as unrestricted in every SQL prefilter and count
# built on the column - so bump to force `fha index` to rebuild before
# doctor/find/exporter queries trust it (same rationale as v2).
# 6: places.notes column (place research notes rendered on place pages).
INDEX_SCHEMA_VERSION = 6
PHOTOINDEX_SCHEMA_VERSION = 1
CACHE_SCHEMA_KEY = 'schema_version'

# ── fha.yaml loading ──────────────────────────────────────────────────────────

def find_archive_root(start: str | Path | None = None) -> Path | None:
    """Walk upward from `start` (or CWD) to find a directory containing fha.yaml."""
    p = Path(start or os.getcwd()).resolve()
    while True:
        if (p / 'fha.yaml').exists():
            return p
        parent = p.parent
        if parent == p:
            return None
        p = parent


def resolve_root_arg(args: Any, command: str | None = None) -> Path | None:
    """
    Resolve the archive root from a parsed CLI namespace: its own `--root`
    flag if given, else walk up from CWD via `find_archive_root()`.

    Every subcommand defines its own `--root` (TOOLING §1 - argparse doesn't
    propagate parent-parser flags into subparsers), so every tool used to
    re-implement this same five-line lookup. Centralized here so there's one
    error message and one behavior to keep correct.

    An explicit `--root` must point at a real archive: the folder must carry
    an `fha.yaml` FILE at its top. This validation lives here, at the one
    chokepoint every tool resolves through, because a typo'd --root used to
    make mutating tools fabricate an archive skeleton anywhere on disk -
    `fha report` minted a .cache and printed a healthy-empty report with
    exit 0, `fha capture` staged stubs into `<typo>/inbox` - and the three
    guards hand-copied into index/find/id-check had already diverged
    (`.is_file()` vs `.exists()`). The refusal fires before the caller does
    any work, so nothing is ever created in the wrong folder. The no---root
    path needs no such check: `find_archive_root()` only returns a folder
    that already contains fha.yaml.

    `command` names the command in the refusal ('fha index'); when omitted,
    the phrase is derived from `args.command` (set by fha.py's dispatcher
    for every subcommand), and a namespace with neither - a tool's
    standalone `python tools/x.py` parser - gets generic wording.

    `fha install` and `fha update-tools` legitimately target folders that
    are not archives yet; they do not call this helper (scaffold.py owns
    its own root handling, with update-tools carrying its own equivalent
    guard), so no opt-out parameter is needed here.

    Prints an ERROR to stderr and returns None when the root is missing or
    fails validation; the caller decides the exit code (the tools return
    EXIT_FAILURE).
    """
    root = getattr(args, 'root', None)
    if root:
        archive_root = Path(root).resolve()
        if not (archive_root / 'fha.yaml').is_file():
            phrase = command
            if not phrase:
                sub = getattr(args, 'command', None)
                phrase = f'fha {sub}' if sub else None
            run_hint = (
                f'Run `{phrase}` from inside your archive'
                if phrase else 'Run the command from inside your archive'
            )
            print(
                f'ERROR: {archive_root} does not look like an archive (no '
                f'fha.yaml there) - is this the right folder? An archive has '
                f'fha.yaml at its top folder. {run_hint}, or point --root at '
                f'the folder that contains fha.yaml. Nothing was changed or '
                f'created.',
                file=sys.stderr,
            )
            return None
        return archive_root
    detected = find_archive_root()
    if detected is None:
        print(f'ERROR: {archive_root_missing_message()}', file=sys.stderr)
        return None
    return detected


class FhaConfigError(Exception):
    """Raised by load_fha_yaml(strict=True) when fha.yaml is malformed.

    A silent empty-dict fallback can make tools ignore external documents/photos
    roots without telling the user, quietly changing which files are considered
    truth - strict mode surfaces that instead.
    """


def _require_yaml() -> None:
    """Raise a friendly dependency error before any PyYAML API is used."""
    if yaml is None:
        raise FhaConfigError(format_yaml_dependency_error())


def _yaml_problem_location(exc: object) -> str:
    """Return a plain line/column locator for PyYAML exceptions when available."""
    mark = getattr(exc, 'problem_mark', None)
    if mark is None:
        return ''
    return f' on line {mark.line + 1}, column {mark.column + 1}'


def format_fha_config_error(path: str | Path, detail: object) -> str:
    """Explain a bad fha.yaml in plain language with a minimal valid example.

    `fha.yaml` is the file that tells the tools where archive folders live. A
    YAML parser message alone is not actionable for the target user, so this
    wrapper gives the line location when PyYAML provides it and a tiny shape
    the file can be repaired toward.
    """
    path = Path(path)
    loc = _yaml_problem_location(detail)
    return (
        f'{path.name} has a problem{loc}. It should be a small YAML settings file, '
        'for example:\n'
        'roots:\n'
        '  documents: documents\n'
        '  photos: photos\n'
        f'Original parser note: {detail}'
    )


def format_record_yaml_error(path: str | Path, detail: object, *, section: str) -> str:
    """Explain malformed YAML inside an archive record or sidecar.

    Source/person records and inbox sidecars are not `fha.yaml`, so their
    repair hint should point at the section being edited: frontmatter is the
    key/value block between `---` lines, while claims are a YAML list under
    `## Claims`. Keeping this separate prevents config examples from leaking
    into record-editing errors.
    """
    path = Path(path)
    loc = _yaml_problem_location(detail)
    if section == 'claims':
        example = (
            'Claims should be a YAML list, for example:\n'
            '- id: C-0123456789\n'
            '  type: birth\n'
            '  persons: [P-0123456789]\n'
            '  value: born about 1880\n'
            '  status: suggested'
        )
    else:
        example = (
            'Frontmatter should be key/value lines between --- markers, for example:\n'
            '---\n'
            'title: Family census page\n'
            'source_type: census\n'
            '---'
        )
    return (
        f'{path.name} has a YAML problem in its {section}{loc}. {example}\n'
        f'Original parser note: {detail}'
    )


def load_fha_yaml(archive_root: str | Path, *, strict: bool = False) -> dict:
    """Load fha.yaml and return the parsed dict.

    A missing file returns {} (running without fha.yaml on default roots is
    legitimate).  A *malformed* file is handled per `strict`:
      - strict=False (default): return {} (permissive/legacy behavior).
      - strict=True: raise FhaConfigError so the caller can fail loudly rather
        than silently dropping configured roots.
    """
    path = Path(archive_root) / 'fha.yaml'
    if not path.exists():
        return {}
    try:
        _require_yaml()
    except FhaConfigError:
        if strict:
            raise
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = yaml.safe_load(f)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise FhaConfigError(
                f'{path.name} must be a YAML mapping: key/value lines like '
                '`roots:` followed by indented entries. Example:\n'
                'roots:\n'
                '  documents: documents\n'
                '  photos: photos'
            )
        return data
    except FhaConfigError:
        if strict:
            raise
        return {}
    except Exception as e:
        if strict:
            raise FhaConfigError(format_fha_config_error(path, e)) from e
        return {}


def get_roots(fha_config: dict) -> dict[str, str]:
    """Extract the roots mapping from fha.yaml config."""
    return fha_config.get('roots', {})


def resolve_path(
    record_path: str,
    fha_config: dict,
    archive_root: str | Path,
) -> Path:
    """
    Resolve a record-relative alias path like 'photos/1880/foo.jpg' to an absolute Path.
    Alias is the first path segment; mapped through fha.yaml roots:
      - absolute value → used as-is
      - relative value → joined to archive_root
      - missing alias → internal directory of that name under archive_root
    """
    record_path = record_path.replace('\\', '/')
    parts = record_path.split('/', 1)
    alias = parts[0]
    rest = parts[1] if len(parts) > 1 else ''

    roots = get_roots(fha_config)
    archive_root = Path(archive_root)

    if alias in roots:
        root_val = str(roots[alias])
        if os.path.isabs(root_val):
            base = Path(root_val)
        else:
            base = archive_root / root_val
    else:
        base = archive_root / alias

    return (base / rest) if rest else base


def path_to_alias(path: str | Path, alias: str, fha_config: dict, archive_root: str | Path) -> str:
    """
    Inverse of resolve_path: turn an absolute Path under `alias`'s root back into
    the stored alias-form path ('photos/1880/foo.jpg', forward slashes - TOOLING
    "All stored paths are alias-form with forward slashes").

    Falls back to the absolute path's forward-slash form if `path` isn't under the
    alias's resolved root (e.g. an absolute root configured outside archive_root).
    """
    # Resolve both sides: a relative root containing '..' (an external asset
    # root like 'documents: ../family-docs') stays lexically distinct from a
    # caller's already-resolved file path even though they name the same
    # directory, which would otherwise send every file under it to the
    # non-portable absolute-path fallback below.
    root = resolve_path(alias, fha_config, archive_root).resolve()
    path = Path(path).resolve()
    try:
        rel = path.relative_to(root)
    except ValueError:
        return path.as_posix()
    return f'{alias}/{rel.as_posix()}' if str(rel) != '.' else alias


def db_mtime(db_path: Path) -> float | None:
    """Return the mtime of db_path, or None if it is absent/unreadable."""
    try:
        return db_path.stat().st_mtime
    except OSError:
        return None


def probe_sqlite(db_path: str | Path, probe_sql: str) -> bool:
    """Return True if db_path opens and probe_sql executes without error."""
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(probe_sql)
        finally:
            conn.close()
        return True
    except Exception:
        return False


def sqlite_cache_schema_status(
    db_path: str | Path,
    expected_version: int,
    required_tables: tuple[str, ...],
) -> tuple[str, str]:
    """
    Classify a disposable SQLite cache before any caller trusts its rows.

    Returns (status, detail):
      'absent'     -> no DB file exists
      'unreadable' -> SQLite cannot open/query it at all
      'old-schema' -> readable, but missing/wrong schema_version or tables
      'fresh'      -> version marker and required tables are present
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return ('absent', '')

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        meta_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if meta_exists is None:
            return ('old-schema', 'schema version is missing')

        row = conn.execute(
            'SELECT value FROM meta WHERE key=?', (CACHE_SCHEMA_KEY,)
        ).fetchone()
        if row is None:
            return ('old-schema', 'schema version is missing')
        try:
            actual_version = int(row[0])
        except (TypeError, ValueError):
            return ('old-schema', f"schema version {row[0]!r} is not readable")
        if actual_version != expected_version:
            return (
                'old-schema',
                f'schema version {actual_version} does not match expected {expected_version}',
            )

        user_version = conn.execute('PRAGMA user_version').fetchone()[0]
        if int(user_version or 0) != expected_version:
            return (
                'old-schema',
                f'SQLite user_version {user_version} does not match expected {expected_version}',
            )

        for table in required_tables:
            conn.execute(f'SELECT 1 FROM {table} LIMIT 1')
        return ('fresh', '')
    except sqlite3.DatabaseError as exc:
        return ('unreadable', str(exc))
    except Exception as exc:
        return ('unreadable', str(exc))
    finally:
        if conn is not None:
            conn.close()


def open_index_db(
    archive_root: str | Path,
    required_tables: tuple[str, ...],
    *,
    strict: bool = False,
) -> sqlite3.Connection | None:
    """
    Open `.cache/index.sqlite` for reading, with the freshness check and
    table probe every index-reading tool needs before it starts querying.

    Returns None (after printing an explanatory message to stderr) when:
      - the file doesn't exist (run `fha index` first)
      - it's stale and `strict=True` (generating/mutating commands can't
        safely act on stale data; strict=False - read-only commands - only
        warns and still returns the connection, since a slightly stale
        answer beats no answer)
      - it exists but fails the table probe (corrupt or pre-this-schema)

    `required_tables` lets each caller ask for exactly the tables its
    queries touch (e.g. `cooccur` needs `relationships`, plain `find`
    lookups only need `persons`) so a partial/older schema fails fast here
    rather than raising mid-query.

    The connection opened during the probe is always closed before
    returning None - a probe failure used to leak the connection in three
    different copies of this function across the tool files.
    """
    archive_root = Path(archive_root)
    db_path = archive_root / '.cache' / 'index.sqlite'
    if not db_path.exists():
        print(
            'ERROR: .cache/index.sqlite not found - run `fha index` first '
            'then re-run this command.',
            file=sys.stderr,
        )
        return None

    schema_status, schema_detail = sqlite_cache_schema_status(
        db_path, INDEX_SCHEMA_VERSION, required_tables,
    )
    if schema_status in {'unreadable', 'old-schema'}:
        suffix = f' ({schema_detail})' if schema_detail else ''
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema; '
            'your search index is out of date or unreadable'
            f'{suffix}. Run `fha index` to rebuild it.',
            file=sys.stderr,
        )
        return None

    mtime = db_mtime(db_path)
    stale = mtime is not None and newest_record_mtime(archive_root) > mtime
    if stale:
        if strict:
            print(
                "ERROR: index is stale; run 'fha index' before generating views.",
                file=sys.stderr,
            )
            return None
        print(
            'WARNING: index may be stale - a record file is newer than '
            '.cache/index.sqlite. Run `fha index` to refresh.',
            file=sys.stderr,
        )

    conn: sqlite3.Connection | None = None
    try:
        # sqlite3.connect() itself can raise (path is a directory, permission
        # denied, locked, etc.) - keep it inside the guard so callers see the
        # documented unreadable-index error and exit 3 instead of a traceback.
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        for table in required_tables:
            conn.execute(f'SELECT 1 FROM {table} LIMIT 1')
        return conn
    except Exception:
        if conn is not None:
            conn.close()
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema; '
            'your search index is out of date or unreadable. Run `fha index` to rebuild it.',
            file=sys.stderr,
        )
        return None


def photoindex_status(archive_root: str | Path, fha_config: dict) -> tuple[str, float]:
    """Classify the photo index (.cache/photos.sqlite) for find/doctor.

    Returns (status, lag_seconds):
      'absent'     → no photos.sqlite               (lag 0.0)
      'unreadable' → exists but fails a basic schema query - corrupt/incompatible (lag 0.0)
      'stale'      → older than the newest file in the photos root (lag = seconds behind)
      'fresh'      → schema OK and not older than the photos root (lag 0.0)

    The schema is probed *before* the empty/missing-photo-root short-circuit, so a
    corrupt database is never reported fresh just because there are no photos to
    compare against.  Shared by `find --text` (caption search gating) and
    `doctor` (freshness report) so both agree on whether photos.sqlite is usable.
    """
    archive_root = Path(archive_root)
    db_path = archive_root / '.cache' / 'photos.sqlite'
    mtime = db_mtime(db_path)
    if mtime is None:
        return ('absent', 0.0)

    # Probe required tables.  `photo_face_regions` is part of the scrape cache,
    # not just a derived query table; an older cache missing it needs a refresh
    # before doctor/find should call the photoindex fresh.
    schema_status, _schema_detail = sqlite_cache_schema_status(
        db_path,
        PHOTOINDEX_SCHEMA_VERSION,
        (
            'photos', 'photo_face_regions', 'photo_fts', 'photo_groups',
            'photo_keywords', 'photo_people',
        ),
    )
    if schema_status in {'unreadable', 'old-schema'}:
        return (schema_status, 0.0)

    # photo_people is derived from both .cache/index.sqlite
    # (face_tags/name_variants) and source record `people:` lists. Edits in
    # either place make photos.sqlite stale even though no photo file changed.
    index_mtime = db_mtime(archive_root / '.cache' / 'index.sqlite')
    max_mtime = index_mtime if index_mtime is not None else 0.0

    # The index.sqlite mtime only catches a person edit that has already been
    # folded into a rebuilt index. If a profile's face_tags/name_variants changed
    # but `fha index` has NOT been rerun, index.sqlite (and the photo_people rows
    # derived from it) is stale even though its mtime looks current. Fold the
    # person-record watermark in directly - mirroring photoindex._index_is_fresh -
    # so find/doctor flag the cache stale instead of serving outdated weak matches.
    record_mtime = newest_person_record_mtime(archive_root)
    if record_mtime > max_mtime:
        max_mtime = record_mtime
    source_mtime = newest_source_record_mtime(archive_root, subdir='photos')
    if source_mtime > max_mtime:
        max_mtime = source_mtime

    photos_root = resolve_path('photos', fha_config, archive_root)
    if photos_root.is_dir():
        # Directory mtimes are included (not just file mtimes) so that a deletion
        # or rename - which bumps the parent directory's mtime but touches no
        # remaining file - still makes the index look stale instead of silently
        # staying 'fresh' with photo_fts rows pointing at files that no longer exist.
        for p in photos_root.rglob('*'):
            if p.is_file() or p.is_dir():
                try:
                    m = p.stat().st_mtime
                    if m > max_mtime:
                        max_mtime = m
                except OSError:
                    pass
        try:
            root_mtime = photos_root.stat().st_mtime
            if root_mtime > max_mtime:
                max_mtime = root_mtime
        except OSError:
            pass

    if max_mtime == 0.0 or mtime >= max_mtime:
        return ('fresh', 0.0)          # empty root, or db newer than newest photo/index
    return ('stale', max_mtime - mtime)


# ── Record parsing ────────────────────────────────────────────────────────────

def read_text_exact(path: str | Path) -> str:
    """Read a record keeping its line endings exactly as authored.

    Why this exists: `Path.read_text()` opens in universal-newline mode, which
    translates every CRLF to LF on read, and the default write mode translates
    LF back to `os.linesep`. Any read/modify/write round-trip through those
    defaults therefore rewrites EVERY line ending of a record whose endings
    differ from the current platform's (an LF archive edited on Windows, a
    CRLF-authored record on Linux) - churn that buries the one intended edit
    and breaks the surgical editors' byte-faithful contract (packet redaction,
    claims surgery). `newline=''` disables translation in both directions, so
    the only differences after a round-trip are the edits the caller made.
    Mirror: `write_text_exact`."""
    with Path(path).open('r', encoding='utf-8', newline='') as f:
        return f.read()


def write_text_exact(path: str | Path, text: str) -> None:
    """Write text with no newline translation (the mirror of read_text_exact).

    Without `newline=''`, Windows would CRLF-ify an LF-authored record on the
    write half of a round-trip even when the read half preserved it."""
    with Path(path).open('w', encoding='utf-8', newline='') as f:
        f.write(text)


def reapply_newline(text: str, like: str) -> str:
    """Give `text` the newline convention of `like` before a byte-faithful write.

    The claim/profile surgical editors rebuild their output by `str.splitlines()`
    + `'\n'.join(...)`, which normalizes to LF regardless of the record's own
    endings. Paired with `read_text_exact`/`write_text_exact`, this restores a
    CRLF record's endings so the write churns only the line the edit touched, not
    every line. A no-op when `like` is LF, or when `text` already carries CRLF
    (an edit path that operated on the untranslated text directly - e.g. a regex
    substitution - so its endings are already faithful)."""
    if '\r\n' in like and '\r\n' not in text:
        return text.replace('\n', '\r\n')
    return text


def yaml_inline(value: str) -> str:
    """Render a value as a single-line YAML scalar, quoting only when needed.

    Every surgical writer that edits a record as text (not round-tripped
    through the YAML emitter, so key order, comments, and the fenced ```yaml
    block survive untouched) shares this one quoting rule: `fha claim`'s
    `--value` edits, `fha confirm`'s `places.yaml` writes, `fha process`'s
    scaffold scalars, and `fha stubs`' record scaffold all pass free-form
    strings (a source title, a place hierarchy entry, a filename) that may
    carry YAML-significant characters (`: `, a leading `-`, a ` #` comment
    marker) - unquoted, those would corrupt the surrounding hand-edited
    document or make `read_record` fail to parse it back. Routing every one
    of those values through `yaml.safe_dump`'s flow style gets exactly the
    quoting a plain `yaml.safe_load` needs, with no line breaks, and
    `width=10**9` keeps a long string on one line rather than folded.

    `safe_dump` also always terminates a bare scalar document with `...`
    (the YAML end-of-document marker) - harmless in a full document, but
    wrong to splice into the middle of a line the caller is building, so it
    is stripped here once, in the one place every caller relies on."""
    rendered = yaml.safe_dump(
        value, default_flow_style=True, allow_unicode=True, width=10 ** 9,
    ).strip()
    if rendered.endswith('...'):          # safe_dump tags a bare scalar document
        rendered = rendered[:-3].strip()
    return rendered


def _coerce_yaml(obj: Any) -> Any:
    """Recursively coerce YAML scalars to types the index expects."""
    if isinstance(obj, dict):
        return {k: _coerce_yaml(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_coerce_yaml(v) for v in obj]
    if isinstance(obj, bool):
        return str(obj).lower()          # True → 'true', False → 'false'
    if isinstance(obj, datetime.date):
        return obj.isoformat()
    return obj


def read_record(path: str | Path) -> dict:
    """
    Parse a markdown archive record file.

    Returns:
        {
            'meta': dict,           frontmatter (scalars coerced)
            'claims': list,         parsed claim dicts (empty on failure)
            'stories': str | None,  ## Stories section body
            'body': str,            full body text (after frontmatter)
            'unfenced_claims': bool, claims were read from an UNfenced `## Claims`
                                     section (a human forgot the ```yaml fence);
                                     lint offers to wrap it. False normally.
            'parse_errors': list,   [(code, message), ...]
        }
    """
    path = Path(path)
    errors: list[tuple[str, str]] = []

    try:
        text = path.read_text(encoding='utf-8')
    except OSError as e:
        return {
            'meta': {}, 'claims': [], 'stories': None, 'body': '',
            'unfenced_claims': False,
            'parse_errors': [('E010', f'Cannot read file: {e}')],
        }

    # Frontmatter
    meta: dict = {}
    body = text
    fm_match = FRONT_RE.match(text)
    if fm_match:
        try:
            _require_yaml()
            raw_meta = yaml.safe_load(fm_match.group(1)) or {}
            meta = _coerce_yaml(raw_meta)
        except FhaConfigError as e:
            errors.append(('E010', str(e)))
        except yaml.YAMLError as e:
            errors.append(('E010', f'Frontmatter YAML error: {format_record_yaml_error(path, e, section="frontmatter")}'))
        body = text[fm_match.end():]

    # Claims block
    claims: list[dict] = []
    unfenced_claims = False
    cm_match = CLAIMS_RE.search(body)
    if cm_match:
        try:
            _require_yaml()
            raw_claims = yaml.safe_load(cm_match.group(1))
            if raw_claims is None:
                raw_claims = []
            if isinstance(raw_claims, list):
                claims = [_coerce_yaml(c) for c in raw_claims if c is not None]
            else:
                errors.append(('E010', 'Claims block is not a YAML list'))
        except FhaConfigError as e:
            errors.append(('E010', str(e)))
        except yaml.YAMLError as e:
            errors.append(('E010', f'Claims YAML error: {format_record_yaml_error(path, e, section="claims")}'))

    # Forgiving-input (boomer-durable-05): a hand-author may type claims under
    # `## Claims` but forget the ```yaml fence. Rather than let those claims be
    # silently invisible (a data-loss trap), read them when the section content
    # UNMISTAKABLY parses as a YAML list of claim-like mappings. Conservative:
    # arbitrary prose under the heading is never force-read as claims.
    # Guard: only check for unfenced claims when there was no fenced block at all
    # (cm_match is None) - not when the fenced block merely had malformed YAML,
    # which would leave claims=[] and trigger a false W114 + double-wrap.
    if not claims and cm_match is None:
        unfenced = _read_unfenced_claims(body)
        if unfenced:
            claims = [_coerce_yaml(c) for c in unfenced]
            unfenced_claims = True

    # Stories section
    stories: str | None = None
    sm = re.search(r'^## Stories\s*\r?\n(.*?)(?=^## |\Z)', body, re.S | re.M)
    if sm:
        content = sm.group(1).strip()
        if content and content not in ('*(none yet)*', '(none yet)'):
            stories = content

    return {
        'meta': meta,
        'claims': claims,
        'stories': stories,
        'body': body,
        'unfenced_claims': unfenced_claims,
        'parse_errors': errors,
    }


# The text of a `## Claims` section, up to the next `##` heading or EOF.
_CLAIMS_SECTION_RE = re.compile(r'^##\s+Claims\s*\r?\n(.*?)(?=^##\s|\Z)', re.S | re.M)
_FENCE_LINE_RE = re.compile(r'^\s*```[a-zA-Z]*\s*$')


def _read_unfenced_claims(body: str) -> list[dict] | None:
    """Return a list of claim mappings written under `## Claims` without a fence,
    or None when the section is absent, empty, or not unmistakably a claim list.

    Conservative on purpose (the section is the structured-data layer): the
    content must parse as a non-empty YAML list whose every item is a mapping
    carrying at least one claim key (id/type/value/persons/status).

    Strict first, forgiving second. The section is parsed exactly as typed;
    only when that fails is it re-tried with ```-lookalike lines removed.
    The old always-drop order silently deleted evidence AS READ: a claim
    quoting ``` inside a `value: |` scalar lost those lines from every
    in-memory consumer (index, report, packet) even though the text on disk
    was fine - and lint's --fix-claims-fence had already been taught to
    REFUSE such files rather than drop the lines on disk, so the reader was
    quietly doing what the fixer refuses to. Strict-first preserves the
    author's bytes whenever they parse; the retry keeps the original
    forgiveness for a genuinely half-typed fence (an opening ``` with no
    close breaks the strict parse, so only then are fence lines dropped).
    When the retry is what succeeds there is no per-record warning channel
    to note it on (read_record's `parse_errors` is the E010 error channel,
    which lint renders as errors) - accepted as silent here because lint
    already surfaces the situation: the file draws W114 (unfenced claims)
    and --fix-claims-fence names the stray ``` line when asked to wrap it."""
    if yaml is None:
        return None
    m = _CLAIMS_SECTION_RE.search(body)
    if not m:
        return None
    raw_lines = m.group(1).splitlines()

    def _parse(lines: list[str]) -> list[dict] | None:
        text = '\n'.join(lines).strip()
        if not text:
            return None
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
        if not isinstance(parsed, list) or not parsed:
            return None
        if not all(isinstance(item, dict) for item in parsed):
            return None
        if not all(_CLAIM_MARKER_KEYS & set(item.keys()) for item in parsed):
            return None
        return parsed

    strict = _parse(raw_lines)
    if strict is not None:
        return strict
    return _parse([ln for ln in raw_lines if not _FENCE_LINE_RE.match(ln)])


def claim_item_key_indent(item_lines: list[str], base_indent: str) -> str:
    """Return the indent (a whitespace string) of one claim item's mapping keys.

    YAML fixes a list item's mapping column at its first key, wherever the
    author put it: `-   value: farmer` owns column 4, so that item's `id:` and
    `status:` lines must also sit at column 4 - and all of it is valid YAML
    that the archive's readers parse happily. The surgical claim editors used
    to assume the one true indent `base_indent + '  '`, so an edit against a
    wider item landed at a column the mapping does not own and broke the whole
    block (every claim in the source vanished from lint/index/report). This
    derives the real column from the item's own lines instead:

      1. an inline first key on the dash line pins it (the dash plus the
         author's spacing) - preferred, because later lines may be block-scalar
         continuations at a deeper, unrelated indent;
      2. else the first following content line (skipping blanks and comments)
         is the item's first key, so its indent is the column;
      3. else fall back to the conventional two spaces past the dash.
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
    return base_indent + '  '


def claims_edit_problem(
    text: str,
    claim_id: str | None = None,
    *,
    expect_status: str | None = None,
) -> str | None:
    """Vet a rewritten source text's `## Claims` block BEFORE it is written.

    The claim editors (`fha claim`, `fha confirm xref/place/cooccur`) rewrite
    the block as text to preserve key order and hand comments; the price is
    that a bad rewrite can leave YAML that no longer parses, which silently
    hides EVERY claim in that source from lint/index/report until a human
    repairs the file. This guard is the cheap insurance: re-parse the
    rewritten text with the same patterns `read_record` uses and confirm
    (a) the block still reads as a YAML list, (b) `claim_id` (when given)
    still appears exactly once, and (c) when a status change was requested
    via `expect_status`, it actually landed on that claim.

    Returns None when the rewrite is sound, else a short plain-language
    description of what would break - the caller folds it into a refusal and
    writes nothing, so even a future editing bug becomes a clean refusal
    instead of a corrupted archive record.
    """
    if yaml is None:
        return format_yaml_dependency_error()
    body = text
    fm = FRONT_RE.match(text)
    if fm:
        body = text[fm.end():]
    cm = CLAIMS_RE.search(body)
    if cm is None:
        return 'the ## Claims block (its ```yaml fence) would be missing'
    try:
        parsed = yaml.safe_load(cm.group(1))
    except yaml.YAMLError as e:
        return f'the ## Claims block would no longer read as YAML{_yaml_problem_location(e)}'
    if parsed is None:
        parsed = []
    if not isinstance(parsed, list):
        return 'the ## Claims block would no longer read as a list of claims'
    if claim_id is None:
        return None
    target = normalize_id(claim_id)
    matches = [
        c for c in parsed
        if isinstance(c, dict) and c.get('id') is not None
        and normalize_id(str(c['id'])) == target
    ]
    if not matches:
        return f'claim {fmt_id_display(target)} would no longer appear in the block'
    if len(matches) > 1:
        return f'claim {fmt_id_display(target)} would appear {len(matches)} times in the block'
    if expect_status is not None:
        actual = matches[0].get('status')
        if str(actual) != expect_status:
            return (f'the claim status would read {actual!r} '
                    f'instead of {expect_status!r}')
    return None


class ClaimEditRefused(Exception):
    """A surgical ``## Claims`` edit cannot be performed safely.

    Every claims-block writer across `fha claim` and `fha confirm` raises this
    one exception when a rewrite would corrupt the block's YAML or land on
    the wrong claim; the caller turns it into a plain refusal `Result` with
    nothing written (AGENTS_TOOLING's "no traceback ever reaches the user"
    rule). Before `fha claim new` needed the same append machinery as
    `fha confirm cooccur`, claim.py and confirm.py each carried their own
    same-shaped class (`_ClaimEditRefused` / `_EditRefused`) - tools never
    import tools, so the moment a SECOND tool needed `append_claim_to_source`
    and `guard_claims_rewrite`, those private types had to become one shared
    type here. Both files keep their historic private name as a plain alias
    (`_ClaimEditRefused = ClaimEditRefused`, `_EditRefused = ClaimEditRefused`)
    so every existing `except` site - and the tests that assert on those
    names - keep working unchanged.
    """


def _find_claims_block(lines: list[str]) -> tuple[int, int] | None:
    """Return (open_fence, close_fence) line indices of the ``## Claims`` block.

    A line-precise counterpart to `CLAIMS_RE`: that regex reads the block's
    text for parsing, but a surgical writer needs the exact line indices to
    splice new lines into, and a whole-text regex match cannot hand those
    back cleanly (the fence text can repeat, and mapping a character offset
    back to a line index invites off-by-one bugs). Returns None when the file
    has no `## Claims` heading, or the heading is not followed by a
    ` ```yaml ` / `` ``` `` fence pair before the next `##` heading.

    Shared by every text-splicing claims writer: `append_claim_to_source`
    below, and confirm.py's `_add_link_to_claim`/`_set_scalar_on_claim`
    (which import this rather than keep their own copy - it moved here
    alongside `append_claim_to_source` since both need it).
    """
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


def guard_claims_rewrite(
    new_text: str, claim_id: str | None, *, expect_status: str | None = None,
    before_text: str | None = None,
) -> str:
    """Re-parse a rewritten claims block; raise `ClaimEditRefused` on any problem.

    Moved here from confirm.py alongside `append_claim_to_source`: every
    claims-block writer (confirm.py's `_add_link_to_claim`/
    `_set_scalar_on_claim`, this module's `append_claim_to_source`, and any
    future one - including a second tool file) funnels its rewrite through
    here before returning it, because a rewrite that breaks the block's YAML
    hides EVERY claim in that source from lint/index/report - a false
    success far worse than a refusal. The check itself is
    `claims_edit_problem`; this wrapper just turns a problem into the
    refusal exception every caller already handles, keeping each writer's
    happy path readable.

    `before_text` (the text the writer started from) keeps the refusal
    honest about whose fault the problem is. When the same check already
    fails on that starting text, this edit did not cause the problem - and
    the only pre-existing state that can reach this guard is a duplicate of
    `claim_id` (locating the claim required the block to parse and the id to
    be present, so parse failures and absences are ruled out). That case is
    the human's duplicate-id repair (lint E001), so the refusal says so
    instead of accusing this edit of hiding claims. Writers that mint a
    brand-new id (`append_claim_to_source`) must NOT pass `before_text`: the
    new id is legitimately absent from the starting text, which would trip
    this probe.
    """
    problem = claims_edit_problem(new_text, claim_id, expect_status=expect_status)
    if problem is None:
        return new_text
    if (before_text is not None and claim_id is not None
            and claims_edit_problem(before_text, claim_id) is not None):
        raise ClaimEditRefused(
            f'claim id {fmt_id_display(normalize_id(claim_id))} appears more than once '
            'in this file - a duplicate-id problem (lint E001) that predates this edit. '
            'Fix the duplicate first: open the file, give one of those claims a fresh '
            'id (mint one with `fha id mint C`), then retry.'
        )
    raise ClaimEditRefused(
        f'{problem}, so saving this edit would hide every claim in the file '
        'from the tools. Open the claim under ## Claims in the source file, '
        'make the change by hand, then run `fha lint` to check it.'
    )


# The SHAPE of a claim's `id:` key line (optionally after the list dash), used
# only to pull the newly-minted claim's own id out of `append_claim_to_source`'s
# `item_lines` for the pre-write guard's `claim_id` argument. claim.py and
# confirm.py keep their own copies of this same pattern for their line-ownership
# checks (`_own_id_key_line`, a stricter test than this shape match alone) -
# KEEP IN SYNC if the id grammar ever changes.
_CLAIM_ID_KEY_RE = re.compile(
    r'^\s*(?:-\s+)?id:\s*(C-[0-9a-hjkmnp-tv-z]{10})\b', re.I
)


def append_claim_to_source(text: str, item_lines: list[str]) -> tuple[str, bool]:
    """Append one new claim item (its full YAML lines) to the ## Claims block.

    Moved here from confirm.py: `fha confirm cooccur` and `fha claim new`
    both mint a brand-new claim and append it to an existing source's block,
    and tools never import tools, so the moment a second tool needed this
    exact append it could no longer stay confirm.py-private. The appended
    item is templated at column 0; against a hand-indented block (items at a
    deeper column) that would break the block's YAML, so the result passes
    through `guard_claims_rewrite` (keyed on the new item's own C-id) - a
    mismatch raises `ClaimEditRefused` instead of writing a block no tool can
    read.
    """
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

    new_cid = None
    for ln in item_lines:
        m = _CLAIM_ID_KEY_RE.match(ln)
        if m:
            new_cid = m.group(1)
            break
    return guard_claims_rewrite('\n'.join(new) + trailing, new_cid), True


def is_merged_meta(meta: dict | None) -> bool:
    """True when a record's frontmatter marks it a merged tombstone (SPEC §9).

    The one merged-status test every tool shares. Comparison is normalized
    (strip + lowercase) because tombstones can be hand-edited: a
    `status: Merged` or a value with a stray trailing space must trip the
    same guards a canonical `status: merged` does - a guard that only one
    byte-exact spelling can arm is not a guard. Works on both parse shapes
    (read_record's coerced meta and a plain yaml.safe_load mapping); a
    non-mapping or absent meta is simply not merged.
    """
    if not isinstance(meta, dict):
        return False
    return str(meta.get('status') or '').strip().lower() == 'merged'


# One frontmatter fence line: exactly `---` at column zero. The optional
# trailing `\r` mirrors FRONT_RE's `\r?\n` (callers may split with
# `text.split('\n')`, which leaves the `\r` of a CRLF line in place).
_FENCE_LINE_EXACT_RE = re.compile(r'---\r?')


def frontmatter_fence_span(lines: list[str]) -> tuple[int, int] | None:
    """Return (open, close) line indexes of the frontmatter `---` pair, or None.

    The ONE fence grammar every surgical frontmatter editor shares, matched to
    `FRONT_RE` (what `read_record` actually parses): each fence is exactly
    `---` at column zero - no indent, no trailing spaces. Anything looser lets
    an editor operate on a region the readers treat as prose (an indented or
    trailing-space `---` never opens frontmatter for `read_record`), so the
    edit would land where no tool ever looks. `lines` may come from
    `text.split('\\n')` (CRLF lines keep their `\\r` - tolerated, as FRONT_RE
    tolerates it) or `text.splitlines()`.
    """
    if not lines or not _FENCE_LINE_EXACT_RE.fullmatch(lines[0]):
        return None
    for i in range(1, len(lines)):
        if _FENCE_LINE_EXACT_RE.fullmatch(lines[i]):
            return 0, i
    return None


def parse_frontmatter_strict(text: str) -> dict | None:
    """The frontmatter mapping exactly as YAML reads it, or None.

    Parses with FRONT_RE + plain yaml.safe_load and NO scalar coercion -
    unlike `read_record`, which coerces booleans/dates to strings for
    cross-record comparisons. `frontmatter_edit_problem` compares a rewrite
    against its original value-by-value, so both sides must come from the
    same parse; feeding it read_record's coerced meta would false-flag every
    boolean (`living: false` reads False on one side, 'false' on the other).
    Returns None when there is no frontmatter or it does not read as a
    mapping.
    """
    if yaml is None:
        return None
    fm = FRONT_RE.match(text)
    if fm is None:
        return None
    try:
        meta = yaml.safe_load(fm.group(1))
    except yaml.YAMLError:
        return None
    return meta if isinstance(meta, dict) else None


def frontmatter_edit_problem(
    new_text: str,
    *,
    before_meta: dict,
    changed_keys: frozenset[str] | set[str] = frozenset(),
) -> str | None:
    """Vet a surgically rewritten record's frontmatter BEFORE it is written.

    The frontmatter sibling of `claims_edit_problem`, shared by every tool
    that edits person frontmatter as text (`fha person set-living`,
    `fha confirm merge`). Text surgery preserves key order and hand comments;
    the price is that a bad rewrite could leave YAML that no longer parses,
    or silently rewrite a field the edit never meant to touch (a key
    lookalike inside a multi-line quoted scalar). Re-parse and require:

      (a) the frontmatter still parses as a mapping (fences per FRONT_RE);
      (b) `id:` still names the same record (normalized comparison);
      (c) every key OUTSIDE `changed_keys` is present and value-identical,
          and no key outside the set appears or disappears.

    `changed_keys` is the caller's declared intent - `{'living'}` for the
    one-key flip, the tombstone/fold key set for the merge - so a multi-key
    rewrite gets the same appear/disappear/change-value discipline as a
    single-key edit, scoped to what it meant to touch. Anything beyond that
    intent is a refusal, never a write.

    `before_meta` must be the strict-parsed original frontmatter
    (`parse_frontmatter_strict` or an equivalent plain yaml.safe_load), so
    value comparisons see the same types on both sides. Returns None when
    the rewrite is sound, else a short plain-language description of what
    would break; the caller refuses and writes nothing.
    """
    if yaml is None:
        return format_yaml_dependency_error()
    fm = FRONT_RE.match(new_text)
    if fm is None:
        return 'the frontmatter block (its --- fences) would be missing'
    try:
        meta = yaml.safe_load(fm.group(1))
    except yaml.YAMLError:
        return 'the frontmatter would no longer read as YAML'
    if not isinstance(meta, dict):
        return 'the frontmatter would no longer read as a set of fields'
    before_id = normalize_id(str(before_meta.get('id') or ''))
    after_id = normalize_id(str(meta.get('id') or ''))
    if before_id != after_id:
        return 'the id: field would change'
    ignore = set(changed_keys)
    before_keys = set(before_meta) - ignore
    after_keys = set(meta) - ignore
    if before_keys != after_keys:
        return 'another frontmatter field would appear or disappear'
    for key in before_keys:
        if meta.get(key) != before_meta.get(key):
            return f'the {key!r} field would change value'
    return None


# ── Prose-section locate/append (## Heading bodies) ───────────────────────────
#
# The one CRLF-safe, bounded '## Heading' locate/append every prose-section
# writer shares: `fha person edit`/`note` (Biography / Stories / Research Notes)
# and `fha source note` (## Notes). Both tools independently grew a copy of this
# text surgery - and independently fixed the same EOF-newline and CRLF-sentinel
# bugs in it - so it is unified here. All of it operates on `text.split('\n')`
# line lists (never a YAML round-trip), so key order, hand comments, and every
# byte outside the touched section survive.

_SECTION_HEADING_RE = re.compile(r'^##\s+\S')


def section_bounds(
    lines: list[str], body_start: int, heading_text: str,
) -> tuple[int, int, int] | None:
    """Find one `## {heading_text}` section's bounds, or None if absent.

    Returns `(heading_idx, content_start, content_end)`: the section's prose
    spans `lines[content_start:content_end]` - everything from just after the
    heading line up to the next level-2 (`## `) heading, or EOF. Searching
    only from `body_start` (just past the frontmatter's closing fence) means
    a `## Biography`-shaped line could never be mistaken for one written
    inside the frontmatter (which cannot happen anyway, but the boundary is
    explicit rather than assumed). The `\\r?$` in the heading match lets a
    CRLF-authored record's `## Notes\\r` line match the same as a plain one.
    """
    heading_re = re.compile(rf'^##\s+{re.escape(heading_text)}\s*\r?$')
    for i in range(body_start, len(lines)):
        if heading_re.match(lines[i]):
            content_end = len(lines)
            for j in range(i + 1, len(lines)):
                if _SECTION_HEADING_RE.match(lines[j]):
                    content_end = j
                    break
            return i, i + 1, content_end
    return None


def lines_end_with_newline(lines: list[str]) -> bool:
    """True when `lines` (from `text.split('\\n')`) came from text ending in a
    trailing newline - `split` leaves a trailing empty element in that case.
    Checked whenever a section edit touches EOF (the section is the last one,
    or is being newly created) so the file's own convention is restored
    afterward instead of silently losing - or gaining - its final newline."""
    return bool(lines) and lines[-1] == ''


def create_section_at_eof(
    lines: list[str], heading_text: str, body_text: str, cr: str,
) -> list[str]:
    """The shared "heading does not exist yet" tail for the section writers:
    append `## {heading_text}` plus `body_text` at EOF, with exactly one
    blank-line separator from whatever came before.

    A file's trailing element from `text.split('\\n')` (when the text ends in
    a newline) is a bare `''` SENTINEL - split attaches `\\r` to the line
    BEFORE a newline, never to this final placeholder - so it means "nothing
    after the last newline," not a real blank line. Reusing it as a mid-file
    separator is exactly what produced two real CRLF bugs during this
    function's own tests: a stray bare `\\n` appearing mid-file, and a
    dangling `\\r` at EOF with no following `\\n`. The fix: strip that
    sentinel BEFORE deciding on spacing, add a genuine (`cr`-valued)
    separator only if one is actually needed, then restore exactly one
    proper end-of-file sentinel (a bare `''`, never `cr`) afterward.
    """
    ends_nl = lines_end_with_newline(lines)
    base = list(lines[:-1]) if ends_nl else list(lines)
    if base and base[-1].strip() != '':
        base.append(cr)
    base.append(f'## {heading_text}{cr}')
    base.extend(f'{ln}{cr}' for ln in body_text.split('\n'))
    if ends_nl:
        base.append('')
    return base


def append_paragraph_to_section(
    lines: list[str], body_start: int, heading_text: str, paragraph: str, cr: str,
) -> tuple[list[str], bool, str]:
    """Append `paragraph` to a `## {heading_text}` section; the shared engine.

    Returns `(new_lines, created, old_content)`. `paragraph` lands as a new,
    blank-line-separated paragraph at the END of the section, never touching
    what was already there (the nothing-ever-lost contract `fha person note`
    and `fha source note` both depend on). A section holding only a
    `*(none yet)*` / `(none yet)` placeholder is treated as empty - the
    placeholder is replaced outright rather than kept alongside real prose,
    since it means exactly "nothing here yet" and leaving it in would read as
    a second, contradictory sentence. When the heading is absent it is created
    at EOF (`created` True) via `create_section_at_eof`.

    `cr` is `'\\r'` for a CRLF-authored record, else `''` - applied to every
    NEWLY inserted line so a CRLF file gains no stray bare-LF line, with the
    EOF sentinel (`lines_end_with_newline`) restored so the file's
    trailing-newline state is preserved either way.
    """
    located = section_bounds(lines, body_start, heading_text)
    body_text = paragraph.strip('\n')
    if located is None:
        return create_section_at_eof(lines, heading_text, body_text, cr), True, ''

    _, content_start, content_end = located
    old_content_lines = lines[content_start:content_end]
    old_content = '\n'.join(old_content_lines)
    trimmed = list(old_content_lines)
    while trimmed and trimmed[-1].strip() == '':
        trimmed.pop()
    is_placeholder = len(trimmed) == 1 and trimmed[0].strip() in ('*(none yet)*', '(none yet)')
    has_next = content_end < len(lines)

    new_lines = list(lines[:content_start])
    if trimmed and not is_placeholder:
        new_lines.extend(trimmed)
        new_lines.append(cr)
    new_lines.extend(f'{ln}{cr}' for ln in body_text.split('\n'))
    if has_next:
        new_lines.append(cr)             # a real blank-line separator - more follows
    elif lines_end_with_newline(lines):
        new_lines.append('')             # the file's own end-of-file sentinel, restored
    new_lines.extend(lines[content_end:])
    return new_lines, False, old_content


def split_log_entries(text: str) -> list[str]:
    """Split an append-log section's text into its entries (paragraph runs).

    An entry is what one `fha person note` / `fha source note` append wrote:
    a run of non-blank lines separated from its neighbors by blank lines.
    Two consumers MUST split identically - the workbench (one edit button
    per entry) and `replace_paragraph_in_section` (finding the entry that
    button targets) - or a button would name text the engine cannot find,
    so this is their single shared home. Lines keep their own text exactly
    (indentation included); only the blank separators are consumed."""
    entries: list[str] = []
    current: list[str] = []
    for line in (text or '').split('\n'):
        if line.strip() == '':
            if current:
                entries.append('\n'.join(current))
                current = []
        else:
            current.append(line.rstrip('\r'))
    if current:
        entries.append('\n'.join(current))
    return entries


def replace_paragraph_in_section(
    lines: list[str], body_start: int, heading_text: str,
    old_paragraph: str, new_paragraph: str, cr: str,
) -> tuple[list[str] | None, str | None]:
    """Replace ONE existing entry of a `## {heading_text}` append-log section.

    The surgical sibling of `append_paragraph_to_section`: where that one
    only ever adds at the end (the nothing-ever-lost note path), this one
    swaps a single existing entry for new text and leaves every other line
    of the file untouched - the workbench's per-entry edit button.

    `old_paragraph` identifies the entry BY ITS EXACT TEXT, not by position:
    positions shift whenever drafts or private fences are display-stripped,
    so a rendered entry's index is not trustworthy across the render/disk
    boundary, but its text is. Both sides are compared with per-line `\\r`
    stripped so a CRLF-authored record matches the browser's LF copy.
    Returns `(new_lines, None)` on the single match, else `(None, reason)` -
    absent section, entry not found (stale page), or the same text appearing
    more than once (ambiguous; the file edit is the honest fallback). `cr`
    follows the same convention as `append_paragraph_to_section`.
    """
    located = section_bounds(lines, body_start, heading_text)
    if located is None:
        return None, (f'this record has no ## {heading_text} section - '
                      'the entry may have been removed. Reload the page.')
    _, content_start, content_end = located

    spans: list[tuple[int, int]] = []
    run_start: int | None = None
    for i in range(content_start, content_end):
        if lines[i].strip() == '':
            if run_start is not None:
                spans.append((run_start, i))
                run_start = None
        elif run_start is None:
            run_start = i
    if run_start is not None:
        spans.append((run_start, content_end))

    def norm(text: str) -> str:
        return '\n'.join(ln.rstrip('\r') for ln in text.strip('\n').split('\n'))

    target = norm(old_paragraph)
    matches = [(s, e) for s, e in spans
               if '\n'.join(ln.rstrip('\r') for ln in lines[s:e]) == target]
    if not matches:
        return None, (f'that entry was not found in ## {heading_text} - it may '
                      'have been edited since this page was loaded. Reload the '
                      'page and try again.')
    if len(matches) > 1:
        return None, (f'that exact entry appears {len(matches)} times in '
                      f'## {heading_text}, so this edit cannot tell which one '
                      'you meant. Edit the record file directly for this one.')
    start, end = matches[0]
    new_lines = list(lines[:start])
    new_lines.extend(f'{ln}{cr}' for ln in norm(new_paragraph).split('\n'))
    new_lines.extend(lines[end:])
    return new_lines, None


def parse_filename(path: str | Path) -> dict | None:
    """
    Parse a record filename into its components.

    Person files:  {surname}__{given}[_{kind}]_{P-id}.md
    Source records: {slug}_{S-id}.md
    Source files:  {slug}[-{copy}][-{role}]_{S-id}.{ext}

    Returns dict with keys: id_str, id_type, kind (for persons), is_companion
    Returns None if filename doesn't match any expected pattern.
    """
    name = Path(path).stem          # filename without extension
    ext = Path(path).suffix.lower()

    # Look for a trailing ID: -{10 crockford chars} after the last underscore
    id_match = re.search(r'_([PSCLH]-[0-9a-hjkmnp-tv-z]{10})$', name, re.I)
    if not id_match:
        return None

    id_str = id_match.group(1).lower()
    id_type = id_str[0].upper()
    before_id = name[:id_match.start()]   # everything before _{id}

    result = {
        'id_str': id_str,
        'id_type': id_type,
        'kind': None,
        'is_companion': False,
    }

    if id_type == 'P' and ext == '.md':
        # Person file - check for companion kind suffix
        # pattern: {surname}__{given}[_{kind}]_{P-id}
        # kind is one of: research, timeline, sources-index
        for kind in sorted(COMPANION_KINDS, key=len, reverse=True):
            suffix = f'_{kind}'
            if before_id.endswith(suffix):
                result['kind'] = kind
                result['is_companion'] = True
                break
        if result['kind'] is None:
            result['kind'] = 'profile'
        # Verify double-underscore surname separator
        if '__' not in before_id.split('_research')[0].split('_timeline')[0].split('_sources-index')[0]:
            # May be a source file accidentally named with P-id - not valid person filename
            pass

    return result


# ── Media filename grammar (TOOLING.md §6, §9) ───────────────────────────────
#
# Unprocessed photos/scans in a mixed folder carry no S-id yet, but variation
# siblings (different scans of one physical photo, front/back pairs, pages of
# a booklet) share a filename "base_id" with only a suffix distinguishing
# them.  This parser recovers that structure so `fha photoindex` (grouping)
# and `fha process` (variation-detection prompt) can both recognise siblings
# without either tool importing the other (shared code lives only in _lib).

@dataclasses.dataclass(frozen=True)
class ParsedName:
    """One filename stem decomposed per the TOOLING §6 suffix grammar.

    base_id    - the stem with all recognised suffixes stripped; the grouping key.
    variant_id - trailing copy letter ('a', 'b', 'c', …) if present, else None.
    part_kind  - 'front' | 'back' | 'page' | 'negative' | 'bw' | 'freeform' | 'none'.
    page_num   - integer page number when part_kind == 'page', else None.
    freeform_role - unrecognised suffix kept as a role, per TOOLING §6.
    is_crop    - True if a '-crop' derivative-detail suffix was stripped.
    """
    base_id: str
    variant_id: str | None
    part_kind: str
    page_num: int | None
    freeform_role: str | None
    is_crop: bool


_CROP_SUFFIX_RE = re.compile(r'[-_]crop$', re.I)
_NEGATIVE_SUFFIX_RE = re.compile(r'[-_]negative$', re.I)
_BACK_SUFFIX_RE = re.compile(r'[-_]back$', re.I)
_FRONT_SUFFIX_RE = re.compile(r'[-_]front$', re.I)
_BW_SUFFIX_RE = re.compile(r'[-_]bw$', re.I)
_PAGE_SUFFIX_RE = re.compile(r'[-_]page[-_]?(\d+)$', re.I)
_VARIANT_DASH_RE = re.compile(r'-([a-z])$', re.I)
_VARIANT_BARE_RE = re.compile(r'(?<=[0-9])([a-z])$', re.I)
_FREEFORM_ROLE_RE = re.compile(r'[-_]([a-z][a-z0-9-]*)$', re.I)


def parse_media_filename(stem: str) -> ParsedName:
    """
    Decompose a photo/scan filename stem into base_id + variation metadata.

    Suffixes are stripped in a fixed priority order (TOOLING §6) because the
    grammar is ambiguous if read in any other sequence - e.g. 'portrait_1880b'
    must lose the bare trailing letter only after confirming no dash-suffix
    role applies first:
      1. '-crop'                         (stacks on any other suffix)
      2. part-kind: '-negative' before '-back'/'-front'/'-page[-]N'/'-bw'
      3. trailing variant letter: '-b' (dash) or bare 'b' right after a digit
      4. whatever remains is base_id.

    A '-negative' filename may still carry a variant letter (e.g.
    'portrait_1880b-negative') - the parser records it in variant_id, but
    TOOLING §9 directs the *grouper* to file negatives at the stem level
    regardless of that letter, since a negative is source material for the
    root image, not an A/B print variant. That grouping decision lives in
    photoindex.py, not here - this function only reports what the filename
    literally encodes.
    """
    remaining = stem
    is_crop = bool(_CROP_SUFFIX_RE.search(remaining))
    if is_crop:
        remaining = _CROP_SUFFIX_RE.sub('', remaining)

    part_kind = 'none'
    page_num: int | None = None
    freeform_role: str | None = None
    page_m = _PAGE_SUFFIX_RE.search(remaining)
    if page_m:
        part_kind = 'page'
        page_num = int(page_m.group(1))
        remaining = _PAGE_SUFFIX_RE.sub('', remaining)
    elif _NEGATIVE_SUFFIX_RE.search(remaining):
        part_kind = 'negative'
        remaining = _NEGATIVE_SUFFIX_RE.sub('', remaining)
    elif _BACK_SUFFIX_RE.search(remaining):
        part_kind = 'back'
        remaining = _BACK_SUFFIX_RE.sub('', remaining)
    elif _FRONT_SUFFIX_RE.search(remaining):
        part_kind = 'front'
        remaining = _FRONT_SUFFIX_RE.sub('', remaining)
    elif _BW_SUFFIX_RE.search(remaining):
        part_kind = 'bw'
        remaining = _BW_SUFFIX_RE.sub('', remaining)
    else:
        freeform_m = _FREEFORM_ROLE_RE.search(remaining)
        # A single trailing letter is never a freeform role - it's either a
        # documented copy variant ('-b', '034b') or, for an undocumented form
        # like '_a', not a suffix at all (TOOLING §6: only dash or
        # bare-after-digit is copy-variant grammar; underscore-letter must
        # stay part of base_id rather than being swallowed as a "role").
        if freeform_m and len(freeform_m.group(1)) > 1:
            part_kind = 'freeform'
            freeform_role = freeform_m.group(1).lower()
            remaining = _FREEFORM_ROLE_RE.sub('', remaining)

    variant_id: str | None = None
    dash_m = _VARIANT_DASH_RE.search(remaining)
    if dash_m:
        variant_id = dash_m.group(1).lower()
        remaining = _VARIANT_DASH_RE.sub('', remaining)
    else:
        bare_m = _VARIANT_BARE_RE.search(remaining)
        if bare_m:
            variant_id = bare_m.group(1).lower()
            remaining = remaining[:-1]

    return ParsedName(
        base_id=remaining, variant_id=variant_id, part_kind=part_kind,
        page_num=page_num, freeform_role=freeform_role, is_crop=is_crop,
    )


def grouping_stem(parsed: ParsedName) -> str:
    """The base_id to group variation siblings by (TOOLING §6/§9).

    The recognised suffix grammar (copy letter, negative/back/front/page-N/bw,
    crop) is stripped so different scans of one physical photo collapse to one
    key, but an *unrecognised* freeform suffix is folded back in: two unrelated
    files like 'smith-family.jpg' and 'smith-house.jpg' must not merge into one
    group just because both end in '-word'.

    Lives in _lib (not photoindex) because two tools must agree on what counts
    as a variation group: `fha photoindex` caches the grouping, and `fha
    process` re-derives it to surface the one/separate/skip prompt. If the two
    used different rules, a folder would group differently depending on which
    tool looked at it (AGENTS_TOOLING symmetry: photoindex grouping ↔ process
    variation detection). Tools never import tools, so the shared rule lives
    here.
    """
    if parsed.part_kind == 'freeform':
        return f'{parsed.base_id}-{parsed.freeform_role}'
    return parsed.base_id


def variant_role(parsed: ParsedName) -> str | None:
    """Compound role string for a non-primary variation member (TOOLING §6/§9).

    Returns None for a plain scan (no recognised suffix) - the caller treats a
    None role as the primary. 'page' carries its number ('page-3'); a freeform
    suffix becomes the role verbatim; '-crop' stacks onto whatever part-kind it
    accompanies ('back-crop') or stands alone ('crop'). Shared by `fha
    photoindex` (the cached `variant_role` column) and `fha process` (the
    `files:` role annotation written on a grouped source), so both label the
    same physical relationship identically.
    """
    if parsed.part_kind == 'page':
        base = f'page-{parsed.page_num}'
    elif parsed.part_kind == 'freeform':
        base = parsed.freeform_role
    elif parsed.part_kind != 'none':
        base = parsed.part_kind
    else:
        base = None
    if parsed.is_crop:
        return f'{base}-crop' if base else 'crop'
    return base


def select_variation_primary(members: list, parsed_of) -> object:
    """Pick the primary member of a variation group (TOOLING §6/§9).

    `members` is any list of comparable keys (Paths or path strings) and
    `parsed_of(member) -> ParsedName` maps each to its parsed filename. The
    primary is, in priority order: a plain scan (no variant letter, no
    part-kind, no crop); else a front scan of copy a/none; else the
    lexicographically-first member. Min() over the candidate set makes the
    choice deterministic when several qualify (e.g. two plain scans).

    Shared so `fha process` flags the same file as `is_primary: true` that
    `fha photoindex` records in `photo_groups.primary_path`.
    """
    plain = [
        m for m in members
        if parsed_of(m).variant_id is None
        and parsed_of(m).part_kind == 'none'
        and not parsed_of(m).is_crop
    ]
    if plain:
        return min(plain)
    fronts = [
        m for m in members
        if parsed_of(m).variant_id in (None, 'a')
        and parsed_of(m).part_kind == 'front'
        and not parsed_of(m).is_crop
    ]
    if fronts:
        return min(fronts)
    return min(members)


# ── EDTF handling (TOOLING.md §1) ────────────────────────────────────────────

# Validation regex for the EDTF subset this system uses.
# Both tilde-before-component (1850-~05) and tilde-at-end (1880-06~) are valid
# EDTF Level 1 syntax for approximate dates.
_EDTF_PATTERNS = [
    re.compile(r'^\d{4}[~?]?$'),                              # 1850, 1850~, 1850?
    re.compile(r'^\d{3}X$'),                                  # 185X (decade)
    re.compile(r'^\d{4}-~?\d{2}[~?]?$'),                     # 1850-05, 1850-~05, 1850-05~
    re.compile(r'^\d{4}-~?\d{2}-~?\d{2}[~?]?$'),             # 1850-05-20 and approximate variants
    re.compile(r'^\[\.{2}\d{4}(?:-\d{2})?(?:-\d{2})?\]$'),   # [..1920]
]


def is_valid_edtf(s: str | None) -> bool:
    """Return True if s is a valid EDTF date per TOOLING.md §1."""
    if not s or not isinstance(s, str):
        return False
    s = s.strip()
    if '/' in s:
        parts = s.split('/', 1)
        return is_valid_edtf(parts[0]) and is_valid_edtf(parts[1])
    if not any(p.match(s) for p in _EDTF_PATTERNS):
        return False
    try:
        edtf_bounds(s)
    except ValueError:
        return False
    return True


# Loose human date forms the archive understands and the canonical EDTF they map
# to.  The agent is taught to write canonical EDTF directly (AGENTS.md), so these
# exist for the OTHER path: a human hand-edits a claim and types "circa 1870" or
# "1870s".  That is the normal condition of this work, not an error - so the tools
# translate the meaning instead of refusing it ("forgiving, not fussy").
#
# Each prefix must be followed by whitespace so a bare word never swallows a year
# that happens to start with the same letters.  "circa"/"about" → approximate (~);
# "before"/"by" → the EDTF before-form ([..YYYY]); "maybe"/"possibly" → uncertain (?).
_APPROX_PREFIX_RE = re.compile(
    r'^(?:c|ca|circa|abt|about|around|approx|approximately|roughly|est|estimated)\.?\s+',
    re.I,
)
_BEFORE_PREFIX_RE = re.compile(r'^(?:before|bef|prior to|by)\.?\s+', re.I)
_UNCERTAIN_PREFIX_RE = re.compile(r'^(?:maybe|possibly|perhaps|probably)\.?\s+', re.I)
_MONTH_NAMES = {
    'jan': 1, 'january': 1,
    'feb': 2, 'february': 2,
    'mar': 3, 'march': 3,
    'apr': 4, 'april': 4,
    'may': 5,
    'jun': 6, 'june': 6,
    'jul': 7, 'july': 7,
    'aug': 8, 'august': 8,
    'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10,
    'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}


def normalize_date(s: str | None) -> str | None:
    """Translate a loose, human-written date into canonical EDTF, or None if its
    meaning is genuinely unclear.

    Returns the input unchanged when it is ALREADY valid EDTF (the common case),
    so callers can use this as a cheap "is this fine, and if not what did they
    mean?" check.  Returns None only when no clear reading exists - that is the
    one case a tool should fall back to asking the human a plain question.

    Recognised loose forms (everything else → None):
      circa/ca/c./abt/about/around/approx/est 1870, ~1870  → 1870~  (approximate)
      maybe/possibly/perhaps 1870                          → 1870?  (uncertain)
      before/bef/prior to/by 1920                          → [..1920]
      1870s, 1870's, 187x                                  → 187X   (decade)
      between 1870 and 1875, 1870 to 1875, 1870-1875       → 1870/1875 (interval)
      a bare year/month/day already shaped like EDTF       → itself

    Month names such as "June 1923", "June 14 1923", and "14 June 1923"
    are parsed because they carry a clear calendar meaning. The result is always
    re-validated against is_valid_edtf before being returned, so this never emits
    a string the rest of the toolchain can't read.
    """
    if not s or not isinstance(s, str):
        return None
    raw = s.strip()
    if not raw:
        return None
    if is_valid_edtf(raw):
        return raw

    # Work on a lowercased, whitespace-collapsed copy stripped of trailing
    # sentence punctuation ("circa 1870." → "circa 1870").  Canonical forms with
    # meaningful punctuation (~, ?, [..], /) are already handled by the early
    # return above, so stripping '.,' here only removes human noise.
    text = re.sub(r'\s+', ' ', raw.lower()).strip().strip('.,')
    text = re.sub(r'^the\s+', '', text)

    # A leading approximate tilde ("~1870") folds into the approximate path.
    if text.startswith('~'):
        text = 'circa ' + text[1:].strip()

    approx = before = uncertain = False
    m = _APPROX_PREFIX_RE.match(text)
    if m:
        approx, text = True, text[m.end():].strip()
    elif (m := _BEFORE_PREFIX_RE.match(text)):
        before, text = True, text[m.end():].strip()
    elif (m := _UNCERTAIN_PREFIX_RE.match(text)):
        uncertain, text = True, text[m.end():].strip()

    candidate: str | None = None

    range_m = re.match(r'^(?:between\s+)?(\d{4})\s*(?:to|and|-|–|/)\s*(\d{4})$', text)
    decade_word_m = re.match(r"^(\d{3})0(?:'s|s)$", text)
    decade_x_m = re.match(r'^(\d{3})x$', text)
    date_m = re.match(r'^(\d{4})(?:-\d{2})?(?:-\d{2})?$', text)
    month_year_m = re.match(r'^([a-z]{3,9})\.?\s+(\d{4})$', text)
    month_day_year_m = re.match(
        r'^([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?[,]?\s+(\d{4})$',
        text,
    )
    day_month_year_m = re.match(
        r'^(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,9})\.?[,]?\s+(\d{4})$',
        text,
    )

    if range_m:
        candidate = f'{range_m.group(1)}/{range_m.group(2)}'
    elif decade_word_m:
        candidate = f'{decade_word_m.group(1)}X'
    elif decade_x_m:
        candidate = f'{decade_x_m.group(1)}X'
    elif month_year_m and month_year_m.group(1) in _MONTH_NAMES:
        candidate = f'{month_year_m.group(2)}-{_MONTH_NAMES[month_year_m.group(1)]:02d}'
    elif month_day_year_m and month_day_year_m.group(1) in _MONTH_NAMES:
        day = int(month_day_year_m.group(2))
        candidate = (
            f'{month_day_year_m.group(3)}-'
            f'{_MONTH_NAMES[month_day_year_m.group(1)]:02d}-{day:02d}'
        )
    elif day_month_year_m and day_month_year_m.group(2) in _MONTH_NAMES:
        day = int(day_month_year_m.group(1))
        candidate = (
            f'{day_month_year_m.group(3)}-'
            f'{_MONTH_NAMES[day_month_year_m.group(2)]:02d}-{day:02d}'
        )
    elif date_m:
        base = date_m.group(0)
        candidate = base

    if candidate and (date_m or month_year_m or month_day_year_m or day_month_year_m):
        if before:
            candidate = f'[..{candidate}]'
        elif approx:
            candidate = f'{candidate}~'
        elif uncertain:
            candidate = f'{candidate}?'

    if candidate and is_valid_edtf(candidate):
        return candidate
    return None


def edtf_bounds(s: str | None) -> tuple[str, str]:
    """
    Return (date_min, date_max) ISO strings for an EDTF date.

    These bounds serve two purposes:
      - Sorting: date_min is the ORDER BY column for chronological claim ordering
      - Windowing: tools can filter claims to a date range with string comparison

    Approximate dates are deliberately widened: '1840~' (about 1840) becomes
    date_min='1839-01-01', date_max='1841-12-31'.  This reflects the uncertainty.

    IMPORTANT: do not use date_min as the display year for an approximate date.
    '1840~' has date_min=1839, but the correct decade is 1840s, not 1830s.
    Always use the EDTF string directly for display and decade grouping, stripping
    the qualifier yourself.  (See views.py _decade_from_edtf for exactly this.)

    Implements the bounds table from TOOLING.md §1.
    """
    if not s or not isinstance(s, str):
        return ('0001-01-01', '9999-12-31')
    s = s.strip()

    # Interval A/B
    if '/' in s:
        parts = s.split('/', 1)
        mn = edtf_bounds(parts[0])[0]
        mx = edtf_bounds(parts[1])[1]
        return (mn, mx)

    # Before: [..YYYY] or [..YYYY-MM] or [..YYYY-MM-DD]
    before_m = re.match(r'^\[\.{2}(\d{4}(?:-\d{2})?(?:-\d{2})?)\]$', s)
    if before_m:
        return ('0001-01-01', _pad_date(before_m.group(1), 'max'))

    # Decade: 185X
    decade_m = re.match(r'^(\d{3})X$', s)
    if decade_m:
        d = decade_m.group(1)
        return (f'{d}0-01-01', f'{d}9-12-31')

    # Year only (possibly approximate)
    year_m = re.match(r'^(\d{4})([~?])?$', s)
    if year_m:
        year = int(year_m.group(1))
        if year_m.group(2):   # approximate: widen ±1 year
            return (f'{year - 1}-01-01', f'{year + 1}-12-31')
        return (f'{year}-01-01', f'{year}-12-31')

    # Year-month: 1850-05, 1850-~05, or 1850-05~ (trailing tilde also valid EDTF)
    ym_m = re.match(r'^(\d{4})-~?(\d{2})[~?]?$', s)
    if ym_m:
        year, month = int(ym_m.group(1)), int(ym_m.group(2))
        if not (1 <= month <= 12):
            raise ValueError(f'invalid month {month} in EDTF date: {s}')
        if '~' in s or '?' in s:
            mn_m = month - 1 if month > 1 else 12
            mn_y = year if month > 1 else year - 1
            mx_m = month + 1 if month < 12 else 1
            mx_y = year if month < 12 else year + 1
            return (f'{mn_y}-{mn_m:02d}-01', _last_day(mx_y, mx_m))
        return (f'{year}-{month:02d}-01', _last_day(year, month))

    # Year-month-day (possibly with ~ on components)
    ymd_m = re.match(r'^(\d{4})-~?(\d{2})-~?(\d{2})[~?]?$', s)
    if ymd_m:
        year = int(ymd_m.group(1))
        month = int(ymd_m.group(2))
        day = int(ymd_m.group(3))
        calendar.monthrange(year, month)
        if day < 1 or day > calendar.monthrange(year, month)[1]:
            raise ValueError(f'invalid day in EDTF date: {s}')
        iso = f'{year}-{month:02d}-{day:02d}'
        return (iso, iso)

    # Nothing structured matched.  Before giving up to the widest-possible window,
    # try reading it as a loose human form ("circa 1870" → "1870~") so the index
    # and timeline sort it correctly instead of dumping it at the 0001..9999 floor.
    # normalize_date never returns a loose form back, so this recurses at most once.
    normalized = normalize_date(s)
    if normalized and normalized != s:
        return edtf_bounds(normalized)

    return ('0001-01-01', '9999-12-31')


def _pad_date(s: str, mode: str) -> str:
    parts = s.split('-')
    if mode == 'max':
        if len(parts) == 1:
            return f'{parts[0]}-12-31'
        if len(parts) == 2:
            return _last_day(int(parts[0]), int(parts[1]))
        return s
    else:
        if len(parts) == 1:
            return f'{parts[0]}-01-01'
        if len(parts) == 2:
            return f'{parts[0]}-{parts[1]}-01'
        return s


def _last_day(year: int, month: int) -> str:
    last = calendar.monthrange(year, month)[1]
    return f'{year}-{month:02d}-{last:02d}'


# ── ID utilities ──────────────────────────────────────────────────────────────

ID_TYPES: frozenset[str] = frozenset('PSCLH')


def _mint_candidate(prefix: str) -> str:
    """Draw one Crockford ID candidate with the canonical uppercase type prefix."""
    body = ''.join(secrets.choice(CROCKFORD_ALPHA) for _ in range(10))
    return f'{prefix.upper()}-{body}'


def mint_ids(prefix: str, count: int, archive_root: str | Path) -> list[str]:
    """Mint fresh IDs of one type, collision-checked against the archive tree.

    ID minting is shared archive infrastructure, so it lives in `_lib.py`
    rather than in the `id` CLI module. That keeps later tools such as
    `fha process` inside the project rule that tools do not import other tools
    while still using the same Crockford alphabet and collision scan everywhere.
    """
    prefix = prefix.upper()
    if prefix not in ID_TYPES:
        raise ValueError(f'Unknown ID type: {prefix!r}. Must be one of P S C L H.')
    if count < 1:
        raise ValueError('count must be at least 1')

    existing = scan_ids_in_tree(archive_root)
    result: list[str] = []
    while len(result) < count:
        candidate = _mint_candidate(prefix)
        if candidate.lower() not in existing:
            result.append(candidate)
            existing.add(candidate.lower())
    return result


def normalize_id(id_str: str) -> str:
    """Normalize an ID to lowercase."""
    return id_str.strip().lower() if id_str else ''


def is_valid_id(id_str: str) -> bool:
    """Return True if id_str is a syntactically valid archive ID."""
    if not id_str:
        return False
    return bool(ID_RE.fullmatch(id_str.strip()))


def id_type_of(id_str: str) -> str | None:
    """Return the type prefix (P/S/C/L/H) of a valid ID, else None."""
    if is_valid_id(id_str):
        return id_str.strip()[0].upper()
    return None


def fmt_id_display(id_str: str) -> str:
    """
    Return an ID string with its type prefix uppercased (p-xxx -> P-xxx).

    The index stores all IDs lowercase (normalize_id); display output across
    the CLI uses the uppercase-prefix convention instead, so every command
    that prints an ID runs it through this first.
    """
    if not id_str:
        return id_str
    return id_str[0].upper() + id_str[1:]


def normalize_place_text(text: str | None) -> str:
    """
    Collapse a free-text place name to a comparable key: lowercase, trimmed,
    internal whitespace collapsed to single spaces.

    Used wherever two claims' `place_text` values need to be compared for
    "same place" without a shared `place_id` - e.g. "Topeka,  Kansas" and
    "topeka, kansas" should match.
    """
    return ' '.join((text or '').strip().lower().split())


def scan_ids_in_tree(archive_root: str | Path) -> set[str]:
    """
    Scan the archive tree for all ID strings (case-normalized).
    Used by id mint to verify non-existence without a built index.
    """
    root = Path(archive_root)
    found: set[str] = set()
    for path in root.rglob('*'):
        if path.is_file() and path.suffix in ('.md', '.yaml', '.yml', '.txt'):
            try:
                text = path.read_text(encoding='utf-8', errors='ignore')
                for m in ID_RE.finditer(text):
                    found.add(m.group(0).lower())
            except OSError:
                pass
    return found


# ── Filename grammar helpers ──────────────────────────────────────────────────

def is_working_copy(archive_root: str | Path) -> bool:
    """Return True if the archive is in working-copy mode.

    Working-copy mode is flagged by the presence of a WORKING_COPY marker file
    at the archive root.  The marker is git-ignored (machine-local) so it never
    syncs back to the main archive.  When active, absent asset files are treated
    as assumed-present-elsewhere, not missing.
    """
    return (Path(archive_root) / 'WORKING_COPY').exists()


def is_fixture_path(path: str | Path) -> bool:
    """
    Return True if the path is under example-archive/ or tests/fixtures/.
    Files there may use status: missing-fixture (W-level, not E-level).

    Only an actual `tests/fixtures/` prefix qualifies - an arbitrary directory
    named `tests` elsewhere in a real archive is NOT fixture space.
    """
    parts = Path(path).parts
    if 'example-archive' in parts:
        return True
    return any(
        parts[i] == 'tests' and parts[i + 1] == 'fixtures'
        for i in range(len(parts) - 1)
    )


def is_template_file(path: str | Path) -> bool:
    """Return True for a copy-paste template (`_TEMPLATE.*`) that ships in the
    archive to teach the by-hand record forms (SPEC §5.2).

    Templates live alongside real records (`sources/_TEMPLATE.source.md`,
    `people/_TEMPLATE.person.md`, …) but are NOT records - they carry placeholder
    IDs and commented examples. Every record walk (lint, index, views, normalize)
    skips them so a template is never parsed as a malformed record or indexed."""
    return Path(path).name.startswith('_TEMPLATE')


def extract_tokens(text: str) -> list[tuple[str, str | None, str | None, tuple[int, int]]]:
    """Return one (id, display, fragment, span) tuple per citation token.

    Recognises every form the grammar accepts - canonical `[[ID]]`,
    `[[ID|display]]`, `[[ID#fragment]]`, `[[ID#^block|display]]`, and the legacy
    single-bracket `[ID]` - in document order, non-overlapping.

      - `id`        the resolved ID, lowercased.  This is the only load-bearing
                    value; display and fragment NEVER alter it.
      - `display`   the `|alias` text a human typed, stripped, or None.  Renderers
                    (wikitree, site) re-emit this; everyone else ignores it.
      - `fragment`  a tolerated Obsidian `#heading` / `#^block` anchor, stripped of
                    its leading `#`, or None.  Parse-only: no tool ever emits a
                    fragment, and it is dropped from the resolved ID by design.
      - `span`      the (start, end) offsets of the whole token in `text`, for a
                    renderer that rewrites it in place.

    `extract_token_ids` is the simple ID list built on top of this; reach for the
    tuples only when you need the display text or the span.
    """
    tokens: list[tuple[str, str | None, str | None, tuple[int, int]]] = []
    for m in _TOKEN_PARTS_RE.finditer(text):
        fragment = m.group(2)
        if fragment is not None:
            fragment = fragment.strip() or None
        display = m.group(3)
        if display is not None:
            display = display.strip() or None
        tokens.append((m.group(1).lower(), display, fragment, m.span()))
    return tokens


def extract_token_ids(text: str) -> list[str]:
    """Return the canonical ID of every citation token in text (lowercased).

    One entry per token occurrence, in document order, regardless of bracket
    count, `|display`, or `#fragment` - `[[S-…|Name]]`, `[[S-…#Claims]]`, and a
    legacy `[S-…]` all reduce to the same `s-…`.
    """
    return [tok[0] for tok in extract_tokens(text)]


def extract_bare_ids(text: str) -> list[str]:
    """Return all bare ID values found in text (lowercased)."""
    return [m.group(0).lower() for m in ID_RE.finditer(text)]


# ── Alias resolution layer ────────────────────────────────────────────────────
#
# The `aliases:` field on every record is the universal resolution surface: it
# carries the record's own canonical ID (so a bare `[[S-…]]` clicks through in
# Obsidian), any human stem the owner typed (`grandmas-album`), on-demand C-ids,
# and - for people and places - the display `name` and its variants, so a
# hand-typed `[[Ken Smith]]` or `[[Fairview]]` resolves to the right record.
#
# These helpers are the read-time, NON-mutating resolver every front door shares.
# Resolution order is: exact canonical ID → alias string → unresolved (None). An
# alias that names ≥2 distinct records is a CLASH: it is kept out of the resolve
# map entirely (so a bare ambiguous name never silently picks a record - a
# data-integrity rule, SPEC §7) and surfaced separately for the linter to flag.

# A wikilink wrapper around a reference, with optional #fragment and |display.
# The target may be an ID *or* a human name/stem, so this is looser than
# TOKEN_RE (which requires an ID body): it just unwraps `[[ … ]]` / `[ … ]`.
_WIKILINK_WRAP_RE = re.compile(r'^\[\[(?P<inner>.*)\]\]$|^\[(?P<inner1>[^\[\]]*)\]$', re.S)


def strip_link_wrapper(ref: str) -> str:
    """Reduce a reference to its bare target: unwrap `[[ ]]`/`[ ]`, drop any
    `|display` and `#fragment`, and trim. `[[Ken Smith]]` → `Ken Smith`,
    `[[P-x|Name]]` → `P-x`, `[[S-x#Claims]]` → `S-x`, `grandmas-album` → itself.

    The load-bearing target is whatever a human would expect the link to point
    at; display text and heading anchors are presentation only and never alter
    resolution (mirrors the `[[ ]]` token grammar's treatment of them)."""
    if ref is None:
        return ''
    s = str(ref).strip()
    m = _WIKILINK_WRAP_RE.match(s)
    if m:
        s = (m.group('inner') if m.group('inner') is not None else m.group('inner1')).strip()
    s = s.split('|', 1)[0]          # drop |display
    s = s.split('#', 1)[0]          # drop #fragment / #^block
    return s.strip()


def link_field_refs(value: Any) -> list[str]:
    """Extract reference strings from a link-valued frontmatter field.

    A source's `people:`/`places:` (and a note's `persons:`/`sources:`) may be
    authored in any of the forgiving forms a hand-editor (often in Obsidian, no
    code editor) produces:
      - bare IDs:                 `[P-x, P-y]`              → ['P-x', 'P-y']
      - quoted wikilinks:         `["[[Ken Smith]]"]`      → ['Ken Smith']
      - quoted ID+display:        `["[[P-x|Ken Smith]]"]`  → ['P-x']
      - an UNquoted `[[Name]]`, which YAML parses as a nested list
        (`people: [[Ken Smith]]` → [['Ken Smith']])        → ['Ken Smith']

    Returns the bare target strings (wrappers/display/fragment stripped); the
    caller resolves each via `resolve_ref`. Empty entries are dropped."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    refs: list[str] = []
    for item in items:
        if isinstance(item, list):
            # An unquoted `[[X]]` reached us as a YAML nested sequence; rejoin
            # its tokens and unwrap as a wikilink target.
            inner = ' '.join(str(x) for x in item).strip()
            target = strip_link_wrapper(f'[[{inner}]]')
        else:
            target = strip_link_wrapper(str(item))
        if target:
            refs.append(target)
    return refs


def _record_alias_strings(rec: dict) -> list[str]:
    """Every string that should resolve to a record: its ID, its `aliases:`
    entries, and (people/places) the display `name` plus name/alt variants.

    Tolerant of the field names both record types use, so one helper feeds both
    the resolve map and the clash check.

    A merged tombstone (`status: merged`, SPEC §9) registers ONLY its bare
    canonical ID - the one alias the merge leaves in its `aliases:` list.
    Its `name:` stays on the record for human readability, but the name (and
    any variant or stem) now belongs to the survivor, where the merge folded
    it; letting the tombstone register it too would make every folded name a
    two-record clash, dropped from every resolve map - so the very merge
    that moved a name would break every `[[Name]]` link to it (plus a fresh
    W112 per merge). Readers resolve the bare ID through `merged_into`."""
    out: list[str] = []
    rid = rec.get('id')
    if rid:
        out.append(str(rid))
    if is_merged_meta(rec):
        return out
    for a in rec.get('aliases') or []:
        out.append(str(a))
    if rec.get('name'):
        out.append(str(rec['name']))
    for v in rec.get('name_variants') or []:
        # A name variant may be a plain string or a {value:, restricted: true}
        # mapping (SPEC §18 deadname). Use the value either way; str() on the
        # dict would make the literal repr an alias key, so the real prior name
        # would neither resolve internally nor be seen by the clash check.
        if isinstance(v, dict):
            val = v.get('value')
            if val:
                out.append(str(val))
        elif v:
            out.append(str(v))
    for v in rec.get('alt_names') or []:
        out.append(str(v))
    return out


def _alias_index(records: Any) -> dict[str, set[str]]:
    """alias_lower → {canonical_id, …}. A multi-id set is a clash."""
    idx: dict[str, set[str]] = {}
    for rec in records:
        cid = normalize_id(str(rec.get('id', '')))
        if not cid:
            continue
        for s in _record_alias_strings(rec):
            key = strip_link_wrapper(s).lower()
            if key:
                idx.setdefault(key, set()).add(cid)
    return idx


def build_alias_map(records: Any) -> dict[str, str]:
    """Build the resolve map `alias_lower → canonical_id` from record dicts.

    Each record is a dict with at least `id`; optional `aliases`, `name`,
    `name_variants`, `alt_names`, and `status` (pass it through: a
    `status: merged` tombstone contributes only its bare ID - see
    `_record_alias_strings`). Only UNAMBIGUOUS aliases are included - a
    string naming ≥2 records (two "John Smith"s, or a stem colliding with another
    record) is omitted so `resolve_ref` returns None rather than guessing. Use
    `alias_clashes` to enumerate the omitted ambiguous strings."""
    return {a: next(iter(ids)) for a, ids in _alias_index(records).items() if len(ids) == 1}


def alias_clashes(records: Any) -> dict[str, list[str]]:
    """alias_lower → sorted list of the ≥2 canonical IDs that share it.

    Same input as `build_alias_map`. These are the strings a bare reference must
    never silently resolve (SPEC §7: same-name people are normal; the link has to
    be pinned to an ID). The linter turns each into a latent or active finding."""
    return {a: sorted(ids) for a, ids in _alias_index(records).items() if len(ids) > 1}


def resolve_ref(ref: str, alias_map: dict[str, str]) -> str | None:
    """Resolve one reference (an ID, a human stem, or a name) to a canonical ID.

    `ref` may carry a wikilink wrapper, a `|display`, or a `#fragment`; all are
    stripped before lookup. Returns the canonical ID, or None when the reference
    matches no alias OR is ambiguous (clashing aliases are absent from the map by
    construction). Always read-only - never mutates anything."""
    key = strip_link_wrapper(ref).lower()
    if not key:
        return None
    return alias_map.get(key)


def resolve_typed_ref(
    raw: object,
    alias_map: dict[str, str] | None,
    want: str | None = None,
) -> str | None:
    """Resolve one structured-field reference (a claim's `persons:`/`roles:`
    entry, its `place:` field, a cooccur pair member) to a canonical ID, with
    the same tolerance the source frontmatter link fields get (TOOLING §2
    step 4a / §3 E004).

    The quickstart teaches claims written with name links (`persons:
    ["[[Sam Rivera]]"]`), so a bare `normalize_id(str(...))` would store the
    literal `[[sam rivera]]` and break every downstream join. Instead:
      - the `[[ ]]` wrapper, `|display`, and `#fragment` are stripped;
      - an ID-shaped target is kept as-is, even when dangling - integrity is
        lint's job (E005), not the resolver's;
      - a name resolves through the alias map, but only to the record type the
        field means (`want`: 'P' for persons/roles, 'L' for place), so a name
        clash across types never yields a cross-type edge;
      - an unknown or ambiguous name returns None - per TOOLING §3, "an
        unresolved non-ID `[[stem]]` is an inert note-link, not a finding" -
        so nothing garbage ever lands in an index row or an idempotency key.

    Shared home for the identical per-tool resolvers (round-2 cleanup K4).
    Live consumers: confirm.py's cooccur idempotency gate (round-2 finding 6)
    and index.py's claim persons/roles/place resolution (its local
    `_resolve_claim_ref` copy was retired in the round-2 finding-8 wave).
    # TODO(K4): lint.py's `_resolve_person_ref` (plus its inline place
    # variant) still holds a local copy - re-point it here in the cleanup wave."""
    ref = strip_link_wrapper(str(raw)) if raw is not None else ''
    if not ref:
        return None
    if id_type_of(ref):
        return normalize_id(ref)
    resolved = resolve_ref(ref, alias_map) if alias_map else None
    if resolved and (want is None or id_type_of(resolved) == want):
        return resolved
    return None


def extract_wikilinks(text: str) -> list[tuple[str, str | None, str | None, tuple[int, int]]]:
    """Return one (target, display, fragment, span) tuple per `[[ ]]` wikilink.

    Unlike `extract_tokens` (ID tokens only), this also yields name/stem links
    like `[[Ken Smith]]` whose target is not an ID - the citation indexer and
    `fha normalize-links` resolve those through the alias map. `target` is
    returned trimmed but with original case (a name lookup lowercases itself)."""
    out: list[tuple[str, str | None, str | None, tuple[int, int]]] = []
    for m in WIKILINK_RE.finditer(text):
        target = m.group(1).strip()
        frag = m.group(2)
        disp = m.group(3)
        if frag is not None:
            frag = frag.strip() or None
        if disp is not None:
            disp = disp.strip() or None
        if target:
            out.append((target, disp, frag, m.span()))
    return out


# ── AI-draft prose exclusion (the AGENTS.md AI-pass contract) ─────────────────
# THE one implementation for every publication path (fha site, fha wikitree;
# fha packet is a planned consumer - round-2 finding S1). The marker grammar
# mirrors confirm.py's `_AI_DRAFT_RE` exactly: `<!--` + optional whitespace +
# the word + anything up to the first `-->` (DOTALL - a marker comment may
# span lines). KEEP IN SYNC with confirm.py: that regex is the flip grammar
# `fha confirm draft` uses to accept a draft in place, and the two must agree
# on what a complete marker is - a marker this stripper reports as damaged is
# also one confirm cannot flip, so the human hears the same "repair the
# marker" story from both ends.

_AI_DRAFT_MARK_RE = re.compile(r'<!--\s*AI-DRAFT\b.*?-->', re.S)
_AI_ACCEPTED_MARK_RE = re.compile(r'<!--\s*AI-ACCEPTED\b.*?-->', re.S)
# A draft block's upper boundary: the end of the previous AI marker (either
# state - an accepted block ends where its own marker sits) or a section
# heading (`#`/`##`; profile sections are `##`, and a draft never crosses
# one). Deeper headings (###+) are prose the drafter may itself have written,
# so they stay INSIDE the block - treating them as boundaries could publish
# the top of an unaccepted draft. The heading arms use `[ \t]`, never `\s`:
# `\s` also matches the newline, which let a bare `##` line swallow the whole
# next line into the "heading" and publish one line of unaccepted draft
# (round-2 finding 17/X2).
_AI_BLOCK_BOUNDARY_RE = re.compile(
    r'<!--\s*AI-(?:DRAFT|ACCEPTED)\b.*?-->|^#{1,2}[ \t][^\n]*$', re.S | re.M)
_SECTION_HEADING_RE = re.compile(r'^#{1,2}[ \t][^\n]*$', re.M)
_BLANK_RUN_RE = re.compile(r'\n{3,}')


def strip_unaccepted_drafts(text: str) -> tuple[str, str | None]:
    """Remove unaccepted AI draft prose - and every AI provenance marker -
    from prose that is about to be published. Returns `(text, problem)`.

    The contract (AGENTS.md): prose an AI drafts into a profile "goes inside
    `<!-- AI-DRAFT ... -->` markers until the human accepts it"; acceptance is
    `fha confirm draft`, which flips the marker to AI-ACCEPTED in place (the
    prose itself never moves). The write-biography skill places the marker at
    the END of the block it drafted, so the drafted span is everything between
    the previous boundary (an earlier AI marker of either state, or a `#`/`##`
    section heading) and the marker itself. That span, marker included, is
    dropped here; AI-ACCEPTED prose is published with its marker removed (the
    marker is a provenance comment - left in, the export pipelines would
    render it as visible text).

    The block START is not syntactically encoded, so prose sitting directly
    above a draft run with no marker or heading between is withheld too -
    deliberately fail-closed: over-excluding until `fha confirm draft` runs
    can never leak an unaccepted draft, and the withheld prose comes back the
    moment the draft is accepted. A `#`/`##` heading whose section the cut
    leaves empty is dropped with it, so an all-draft section publishes like a
    section that was never written (no stray heading).

    FAIL-CLOSED SIGNALING (round-2 finding 18/X1). A DAMAGED marker - an
    unterminated `<!-- AI-DRAFT` with no `-->`, an orphan wrap-style
    `<!-- /AI-DRAFT -->` closer, or any stray `AI-DRAFT`/`AI-ACCEPTED` text
    the complete-marker grammar cannot account for (a bare prose mention
    included: cheaper to over-withhold than to guess) - means draft can no
    longer be told from accepted prose. The old behavior published the draft.
    Now the function returns `('', problem)`: `problem` is a plain sentence
    naming the damage, and the returned text is EMPTY, so even a consumer
    that ignores `problem` publishes nothing rather than the draft. A tuple
    was chosen over a dedicated exception because a damaged marker is an
    expected authoring state on a publication path, not exceptional control
    flow: site keeps building the other pages, wikitree renders a refusal
    Result - neither wants an unwind - and returning the safe empty string in
    the problem arm makes the API impossible to fail open with. On success
    the function returns `(cleaned_text, None)`."""
    if 'AI-DRAFT' not in text and 'AI-ACCEPTED' not in text:
        return text, None

    if 'AI-DRAFT' not in text:
        cleaned = _AI_ACCEPTED_MARK_RE.sub('', text)
    else:
        boundaries = list(_AI_BLOCK_BOUNDARY_RE.finditer(text))
        headings = list(_SECTION_HEADING_RE.finditer(text))

        # One cut per draft marker: [end of the nearest boundary above it, end
        # of the marker). Cuts come out in ascending, non-overlapping order
        # because a draft marker is itself a boundary for the next one.
        cuts: list[tuple[int, int]] = []
        for marker in _AI_DRAFT_MARK_RE.finditer(text):
            start = 0
            for b in boundaries:
                if b.end() <= marker.start():
                    start = b.end()
                else:
                    break
            cuts.append((start, marker.end()))

        def _surviving(lo: int, hi: int) -> str:
            """Text of [lo, hi) that no cut removes - the empty-section probe."""
            kept: list[str] = []
            pos = lo
            for cs, ce in cuts:
                if ce <= lo or cs >= hi:
                    continue
                kept.append(text[pos:max(lo, cs)])
                pos = min(hi, ce)
            kept.append(text[pos:hi])
            return ''.join(kept)

        # Drop the heading of any section the cuts emptied. Accepted markers
        # do not count as surviving content (they are removed below anyway).
        heading_cuts: list[tuple[int, int]] = []
        for cs, _ce in cuts:
            h_prev = None
            h_next_start = len(text)
            for h in headings:
                if h.end() <= cs:
                    h_prev = h
                elif h.start() > cs:
                    h_next_start = h.start()
                    break
            if h_prev is None:
                continue
            remainder = _AI_ACCEPTED_MARK_RE.sub('', _surviving(h_prev.end(), h_next_start))
            if not remainder.strip():
                heading_cuts.append((h_prev.start(), h_prev.end()))

        out: list[str] = []
        pos = 0
        for cs, ce in sorted(set(cuts + heading_cuts)):
            if cs > pos:
                out.append(text[pos:cs])
            pos = max(pos, ce)
        out.append(text[pos:])
        cleaned = _AI_ACCEPTED_MARK_RE.sub('', ''.join(out))

    # The fail-closed accounting: every marker word must be gone once all
    # complete markers were cut/removed. Anything left is a damaged marker
    # (or an unmarked mention the grammar cannot distinguish from one).
    for word in ('AI-DRAFT', 'AI-ACCEPTED'):
        if word in cleaned:
            return '', (
                f'"{word}" text remains after every complete '
                f'"<!-- {word} ... -->" marker was handled - '
                'usually a marker missing its closing "-->"'
            )

    # Cutting a block leaves the blank lines that framed it; collapse the
    # leftovers so paragraph spacing stays normal.
    return _BLANK_RUN_RE.sub('\n\n', cleaned), None


# ── Private-content fence (publication guard) ─────────────────────────────────
# A general `<!-- private -->…<!-- /private -->` fence hides author-marked prose
# (research hunches, notes touching living kin) from any shared/standalone output
# while keeping it in the `--linked` working preview. Companion to
# strip_unaccepted_drafts; usable on every publication path.
_PRIVATE_MARK_RE = re.compile(r'<!--\s*/?\s*private\s*-->', re.I)
_PRIVATE_BLOCK_RE = re.compile(
    r'<!--\s*private\s*-->.*?(?:<!--\s*/\s*private\s*-->|\Z)', re.S | re.I)


def apply_private_fence(text: str, *, drop: bool) -> str:
    """Resolve `<!-- private -->…<!-- /private -->` fences in prose.

    `drop=True` (a public/standalone build) removes the fenced content entirely;
    `drop=False` (the linked working preview) keeps the content but strips the
    marker comments so they never render as stray blank lines. FAIL-CLOSED: an
    unterminated `<!-- private -->` (no closing marker) drops to the end of the
    text rather than risk publishing what was meant to stay hidden."""
    if '<!--' not in text:
        return text
    if drop:
        text = _PRIVATE_BLOCK_RE.sub('', text)
    text = _PRIVATE_MARK_RE.sub('', text)
    return _BLANK_RUN_RE.sub('\n\n', text) if drop else text


# ── GENERATED-file ownership ──────────────────────────────────────────────────
# The header contract between the generators (views, lint --fix reports, site
# never - it owns a whole directory instead) and every tool that must not
# rewrite, must overwrite, or may delete a generated file.

# Tool-agnostic header prefix. Generators append their own name after it
# ('<!-- GENERATED by fha views timeline ...'); pass that longer string as
# `prefix` to test ownership by one specific tool.
GENERATED_PREFIX = '<!-- GENERATED'

# The UTF-8 byte-order mark an editor re-save may prepend; named because an
# invisible literal in source is unreadable and easy to break in edits.
_BOM = chr(0xfeff)


def is_generated_text(text: str, prefix: str = GENERATED_PREFIX) -> bool:
    """True when `text` is a tool-generated file body: its first NON-BLANK
    line starts with `prefix`.

    Why first-non-blank rather than byte 0: a leading blank line or a UTF-8
    BOM (an editor re-save) must not flip a file's ownership. lint and views
    already judged by the first non-blank line while normalize-links checked
    byte 0, and that split let normalize-links rewrite prose inside a
    generated file that merely began with a blank line (round-2 finding 12).
    The BOM is stripped both at text start and at line start because
    `str.strip()` does not treat U+FEFF as whitespace."""
    for line in text.lstrip(_BOM).splitlines():
        if line.strip():
            return line.lstrip(_BOM).startswith(prefix)
    return False


def is_generated_file(path: str | Path, prefix: str = GENERATED_PREFIX) -> bool:
    """True when the file at `path` carries the GENERATED header
    (`is_generated_text` over its content, BOM tolerated via utf-8-sig).

    An unreadable file returns False - i.e. "not generated". Every caller is
    deciding whether it may skip, overwrite, or delete a tool-owned file, and
    a file that cannot be read must be treated as human-owned (never touched);
    the read failure resurfaces with its own message wherever the caller next
    reads the file for real."""
    try:
        text = Path(path).read_text(encoding='utf-8-sig', errors='ignore')
    except OSError:
        return False
    return is_generated_text(text, prefix)


class GeneratedFileRefused(Exception):
    """Raised when a generated-file write would clobber a file it does not own.

    A file already at the target whose first non-blank line is not the writer's
    GENERATED marker is treated as human-authored and must never be overwritten
    (the archive contract, AGENTS.md). Carries the offending path - `str(exc)`
    is that path - so each tool's CLI can format its own plain-language refusal
    and next step (views points at "move or delete it"; the gallery also offers
    `--out`), which is why the message is NOT baked in here.
    """

    def __init__(self, path: str | Path):
        super().__init__(str(path))
        self.path = Path(path)


class GeneratedFileParentMissing(Exception):
    """Raised when a generated file's parent folder does not exist and the
    caller has not opted into creating it.

    A views companion (.md timeline/sources-index/draft-queue) lives beside an
    existing person profile - its parent folder must already be there, because
    it is the profile's own folder. If it is missing, the index is pointing at
    a folder that moved or was deleted since the last `fha index`; silently
    `mkdir`-ing it back into existence would resurrect a stray folder built
    from stale cache state instead of failing safely (AGENTS.md: never leave
    the archive in an inconsistent state). Only artifact writers whose parent
    is allowed to not exist yet (generated/gallery/, generated/views/) pass
    `create_parents=True` to `write_generated_file` and never hit this path.
    """

    def __init__(self, path: str | Path):
        super().__init__(str(path))
        self.path = Path(path)


def write_generated_file(
    out_path: Path, content: str, marker_prefix: str, create_parents: bool = False,
) -> Path:
    """Write a GENERATED file, refusing to clobber a file it does not own.

    The one guard shared by every fha single-file writer (views companions and
    the photoindex gallery): a file already at `out_path` whose first non-blank
    line is not `marker_prefix` is human-authored and raises
    GeneratedFileRefused rather than being overwritten. A marker-owned or absent
    target is (over)written silently, so every run regenerates in place with no
    --overwrite flag. Returns out_path.

    `create_parents` defaults to False: a companion file's parent is the
    person's own folder, which must already exist, so a missing parent raises
    GeneratedFileParentMissing rather than being silently recreated from a
    stale index. Callers whose target lives under a disposable top-level
    folder that may legitimately not exist yet (generated/gallery/) pass
    create_parents=True; generated/views/ callers never need it - _html_out_path
    already creates that folder before this runs.

    Lifted from the byte-identical guards that used to live in views.py and
    photoindex.py; keeping one copy here (tools never import tools, so _lib is
    the only legal shared home) means the ownership rule can never drift between
    the two writers.
    """
    if out_path.exists():
        try:
            existing = out_path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            existing = ''
        # Ownership = the writer's marker on the first non-blank line, via the
        # shared predicate (which also tolerates a leading blank line or UTF-8
        # BOM from an editor re-save). The per-tool prefix keeps one tool's
        # GENERATED file protected from another tool's overwrite.
        if not is_generated_text(existing, prefix=marker_prefix):
            raise GeneratedFileRefused(out_path)
    if not out_path.parent.is_dir():
        if not create_parents:
            raise GeneratedFileParentMissing(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding='utf-8')
    return out_path


# ── Single-file HTML rendering (views companions + photoindex gallery) ─────────
# The standalone-page shell is shared by `fha views --format html` and `fha
# photoindex gallery`: both inline the same design/view.css subset, both load a
# Jinja2 template from tools/templates/ with autoescape on, and both title their
# masthead from fha.yaml. These three helpers are the one parameterized copy of
# that infra so the two writers cannot drift.

# Per-process caches: the CSS text and each template are identical for every
# file rendered in one process (a bulk `views refresh --format both`, or any
# gallery build), and re-reading them per file would dominate render time.
# _VIEW_CSS_CACHE holds (css_text, missing_bool); None means "not read yet".
_VIEW_CSS_CACHE: tuple[str, bool] | None = None
_JINJA_ENV = None
_TEMPLATE_CACHE: dict[str, object] = {}


def load_view_css(artifact_label: str) -> tuple[str, str | None]:
    """Return (design/view.css text, warning_or_None) for inlining.

    Resolved exactly as `fha site` resolves its design package: design/ sits
    beside tools/ in both this repo and an installed archive (the manifest ships
    it), so a tools-relative path works in both. Styling is never load-bearing,
    so a missing file degrades to ('' , warning) - the page is still complete,
    just unstyled - and the warning names the fix. `artifact_label` fills the
    one word that differs between callers ('the HTML view' vs 'the gallery') so
    each keeps its exact wording.

    The warning is RETURNED, not printed: this is engine-layer code, and the
    engine/interface split (AGENTS_TOOLING) puts all human-facing output in the
    _cmd_* layer. The caller threads it into its Result or prints it there.
    """
    global _VIEW_CSS_CACHE
    if _VIEW_CSS_CACHE is None:
        css_path = Path(__file__).resolve().parent.parent / 'design' / 'view.css'
        try:
            _VIEW_CSS_CACHE = (css_path.read_text(encoding='utf-8'), False)
        except OSError:
            _VIEW_CSS_CACHE = ('', True)
    css, missing = _VIEW_CSS_CACHE
    if not missing:
        return css, None
    warning = (
        f'WARNING: design/view.css is missing - {artifact_label} will be '
        f'unstyled (its content is still complete). Restore the design/ '
        f'folder next to tools/ (re-run your tools install/update) for '
        f'the styled version.'
    )
    return css, warning


def load_template(template_name: str):
    """Return a cached Jinja2 template from tools/templates/, autoescape ON.

    Autoescape escapes every piece of record text a template interpolates
    (paths, captions, labels, file:// hrefs); only pre-built fragments the
    caller has already made safe pass through `| safe`. Raises ImportError when
    Jinja2 is unavailable - callers translate that into a plain install hint
    (Jinja2 is already a suite dependency via `fha site` / `fha views`). Cached
    by template name so a bulk render reads each template from disk once.
    """
    global _JINJA_ENV
    if template_name not in _TEMPLATE_CACHE:
        import jinja2
        if _JINJA_ENV is None:
            _JINJA_ENV = jinja2.Environment(
                loader=jinja2.FileSystemLoader(
                    str(Path(__file__).resolve().parent / 'templates')
                ),
                autoescape=True,
            )
        _TEMPLATE_CACHE[template_name] = _JINJA_ENV.get_template(template_name)
    return _TEMPLATE_CACHE[template_name]


def render_template(template_name: str, **context) -> str:
    """Load and render a tools/templates/ Jinja2 template, returning the text.

    Both loading (a missing template file) and rendering (a bad expression, an
    undefined variable) can raise a jinja2.TemplateError subclass; translated
    here into a plain RuntimeError naming the exact folder to restore, so
    neither `fha views --format html` nor `fha photoindex gallery` ever leaks a
    raw Jinja traceback for a broken or reinstalled tools/templates/ folder.
    ImportError (Jinja2 itself not installed) is deliberately left to propagate
    unchanged, matching load_template's contract - callers already translate
    that into their own install hint.
    """
    import jinja2
    try:
        return load_template(template_name).render(**context)
    except jinja2.TemplateError as e:
        raise RuntimeError(
            f'the {template_name} template is missing or broken (expected at '
            f'tools/templates/{template_name}) - {e}. Restore the tools/templates '
            f'folder (reinstall the tools package or restore it from git), then '
            f're-run.'
        ) from e


def archive_title(cfg: dict) -> str:
    """The archive's display name for a masthead/page title, from fha.yaml.

    Reads `site: archive_name:` (the `fha site` key) with a legacy top-level
    `archive_name:` fallback, then a plain default. A hand-edited scalar `site:`
    (a string, not a mapping) must not crash a render, so a non-dict `site` is
    ignored. Takes the already-loaded config dict rather than re-reading the
    file, since every caller has just loaded it.
    """
    site_cfg = cfg.get('site')
    if not isinstance(site_cfg, dict):
        site_cfg = {}
    return (
        str(site_cfg.get('archive_name') or cfg.get('archive_name') or '').strip()
        or 'Family History Archive'
    )


# ── Archive freshness ─────────────────────────────────────────────────────────

def newest_record_mtime(archive_root: Path) -> float:
    """Max mtime (epoch seconds) across sources/people/notes .md files and places/places.yaml.

    Used as the freshness baseline for index.sqlite and photos.sqlite: if the
    cache is older than this, it is stale.  Returns 0.0 on a brand-new archive
    that has no record files yet (trivially up-to-date).
    """
    max_mtime = 0.0
    dirs = [archive_root / d for d in ('sources', 'people', 'notes')]
    for p in itertools.chain.from_iterable(d.rglob('*.md') for d in dirs if d.is_dir()):
        try:
            mtime = p.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            pass
    for extra in (
        archive_root / 'places' / 'places.yaml',
        archive_root / 'fha.yaml',
    ):
        try:
            mtime = extra.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            pass
    return max_mtime


def newest_source_record_mtime(archive_root: Path, subdir: str | None = None) -> float:
    """Max mtime (epoch seconds) across source records only.

    `photoindex` re-reads source `people:` lists to create the authoritative
    `source-people` tier, so an edit under sources/ must stale photos.sqlite
    even when no original photo file changed. Kept separate from
    newest_record_mtime so photo freshness does not react to unrelated notes or
    generated views.

    Pass `subdir` to limit the scan to a specific subdirectory under sources/
    (e.g. `'photos'`), which avoids false staleness when unrelated source types
    such as census records are edited.
    """
    max_mtime = 0.0
    sources_dir = archive_root / 'sources'
    if subdir:
        sources_dir = sources_dir / subdir
    if not sources_dir.is_dir():
        return max_mtime
    for p in sources_dir.rglob('*.md'):
        try:
            mtime = p.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            pass
    return max_mtime


def newest_person_record_mtime(archive_root: Path) -> float:
    """Max mtime (epoch seconds) across person *profile* records only.

    Narrower than `newest_record_mtime`: face-tag/name matching only reads
    `face_tags`/`name_variants` from profile records, so generated companion
    files (research/timeline/sources-index/draft-queue) and folder-level
    `sources-index.md` files under people/ must not bust this freshness
    check just because `fha views refresh` touched them.
    Returns 0.0 on a brand-new archive that has no person records yet.
    """
    max_mtime = 0.0
    people_dir = archive_root / 'people'
    if not people_dir.is_dir():
        return max_mtime
    for p in people_dir.rglob('*.md'):
        parsed = parse_filename(p)
        if parsed is None or parsed['id_type'] != 'P' or parsed['kind'] != 'profile':
            continue
        try:
            mtime = p.stat().st_mtime
            if mtime > max_mtime:
                max_mtime = mtime
        except OSError:
            pass
    return max_mtime


def scan_person_record_ids(archive_root: str | Path) -> set[str]:
    """
    Return the P-id of every actual person *profile* record under people/
    (case-normalized), excluding companion files (research/timeline/
    sources-index/draft-queue) and any P-id token that merely appears in
    body text elsewhere in the archive.

    Narrower than `scan_ids_in_tree`, which matches any bare ID-shaped token
    anywhere under .md/.yaml/.yml/.txt - fine for `id mint` collision checks,
    but too permissive for validating that an ID a mutating command is about
    to write actually names a person record (a typo'd or placeholder P-id
    mentioned in a note would otherwise pass).
    """
    root = Path(archive_root)
    people_dir = root / 'people'
    if not people_dir.is_dir():
        return set()
    found: set[str] = set()
    for p in people_dir.rglob('*.md'):
        parsed = parse_filename(p)
        if parsed is not None and parsed['id_type'] == 'P' and parsed['kind'] == 'profile':
            found.add(parsed['id_str'])
    return found


def find_person_record_path(archive_root: str | Path, person_id: str) -> Path | None:
    """Scan `people/` for one P-id's primary person record (not a companion view).

    The `.md` files are archive truth, so this never consults
    `.cache/index.sqlite` - a stale or absent index must never block or mislead
    a write aimed at a person record (the `fha claim` locate-by-scanning rule).
    Matches stubs, curated profiles, and merged tombstones alike: identity is
    the `_{P-id}.md` filename suffix (`parse_filename`), so the folder, slug,
    and any `MERGED-INTO-…` prefix are irrelevant. Companion files (research/
    timeline/sources-index/draft-queue) share the P-id but are generated views,
    never the record itself, so they are excluded.

    Shared here because several tools need the same lookup (`fha confirm draft`,
    `fha person set-living`, `fha confirm merge`) - the same
    shared-infrastructure rationale as `mint_ids`.
    """
    target = normalize_id(person_id)
    people_dir = Path(archive_root) / 'people'
    if not people_dir.is_dir():
        return None
    for path in sorted(people_dir.rglob('*.md')):
        parsed = parse_filename(path)
        if not parsed or parsed.get('id_str') != target:
            continue
        if parsed.get('id_type') == 'P' and not parsed.get('is_companion'):
            return path
    return None


def find_source_record_path(archive_root: str | Path, source_id: str) -> Path | None:
    """Scan `sources/` for one S-id's record file, or None.

    The source sibling of `find_person_record_path`: identity is the
    `_{S-id}.md` filename suffix (`parse_filename`), so a stale or absent
    index never blocks or misdirects a write aimed at a source record. Source
    filenames carry no companion-kind suffix the way person profiles do
    (`parse_filename` only ever sets `is_companion` for `P`-typed `.md`
    files), so every `_{S-id}.md` match under `sources/` is the record itself.

    Two tools had already re-implemented this exact scan privately
    (`fha confirm`'s `_find_source_path_by_id`, `fha source`'s
    `_find_source_record_path`) before `fha claim new` needed it too; this is
    the shared home going forward - the same shared-infrastructure rationale
    as `mint_ids` and `find_person_record_path`. The two existing private
    copies are left as-is (out of scope for this change) rather than churned
    just to call through here.
    """
    target = normalize_id(source_id)
    sources_dir = Path(archive_root) / 'sources'
    if not sources_dir.is_dir():
        return None
    for path in sorted(sources_dir.rglob('*.md')):
        parsed = parse_filename(path)
        if not parsed or parsed.get('id_str') != target:
            continue
        if parsed.get('id_type') == 'S':
            return path
    return None


def stub_slug_name(name: str) -> tuple[str, str]:
    """Parse a display name into (surname_slug, given_slug) for a stub filename.

    Best effort, not a real name-parsing engine: the last word is taken as the
    surname and everything before it as given names, because that is right
    often enough for the filename to be recognisable, and a stub filename is
    provisional anyway (renamed by hand once a human files the person
    properly). Sanitised to `[a-z0-9_]` so the slug is always a safe filename
    component regardless of what punctuation the display name carries.

    A SINGLE-token name is a surname-less person - a mononym (`Cher`), an
    enslaved ancestor recorded only by a given name, a patronymic. SPEC §13
    files those with the sort-name slot EMPTY, so the filename leads with the
    double underscore (`__cher_P-….md`), a distinct no-surname sort group -
    hence the empty surname slug here, not the literal 'unknown'. 'unknown'
    stays reserved for the genuinely nameless fallback below (a blank or
    whitespace-only display name), which is a missing name, not a mononym.
    """
    parts = name.strip().split()
    if not parts:
        return ('unknown', 'unknown')
    if len(parts) == 1:
        return ('', parts[0].lower())
    surname = parts[-1].lower().replace(' ', '_')
    given = '_'.join(p.lower() for p in parts[:-1])
    surname = re.sub(r'[^a-z0-9_]', '', surname)
    given = re.sub(r'[^a-z0-9_]', '', given)
    return (surname or 'unknown', given or 'unknown')


def stub_filename(name: str | None, pid: str) -> str:
    """Return the `{surname}__{given}_{P-id}.md` stub filename (SPEC §13).

    A blank or literal "unknown" name falls back to the surname-less
    `unknown__unknown_{P-id}` form rather than calling `stub_slug_name` on a
    name with nothing to slug - the double underscore is the same convention
    §13 uses for surname-less people (mononyms, enslaved ancestors named only
    by a given name), so an unresolved reference reads the same way on disk.
    """
    if name and name.lower() not in ('unknown', ''):
        surname, given = stub_slug_name(name)
    else:
        surname, given = 'unknown', 'unknown'
    return f'{surname}__{given}_{pid}.md'


def render_stub_content(
    pid: str,
    name: str | None,
    *,
    sex: str | None = None,
    gender: str | None = None,
    birth: str | None = None,
    death: str | None = None,
    birth_place: str | None = None,
    death_place: str | None = None,
) -> str:
    """Render a §9 person-stub record's frontmatter text (id/aliases/name/…/tier).

    Shared by `fha stubs` (unresolved-reference and `--from-names` minting) and
    `fha person new` (a human deliberately starting a stub with what they
    already know). The field order - id, aliases, name, [sex], [gender],
    living, birth/death, created, tier - is fixed so every stub reads the same
    way regardless of which tool wrote it; `tests/test_templates.py` checks it
    against `archive-template/people/stubs/_TEMPLATE.stub.md`.

    `aliases:` carries the P-id from birth - the line that makes a bare
    `[[P-…]]` cite click through in Obsidian. The display name registers as an
    alias automatically (the index reads it from `name:`), so a hand-typed
    `[[Name]]` resolves once the stub is promoted to a real name.

    `sex`/`gender` are omitted entirely (not written as blank/null) when not
    given - most stubs never carry either, and an absent key is friendlier to
    a hand reader than `sex: null`. `sex` is validated against
    `PERSON_SEX_VALUES` (SPEC §9); `gender` is free text, so it is trusted
    as-is.

    `birth`/`death` are PROVISIONAL, unsourced EDTF estimates (see
    `PROVISIONAL_VITAL_FIELDS`) - the honest "I know roughly when" a human or a
    tool may have before any source is filed. Given, they are written as real
    `birth: value` / `death: value` lines carrying the same reassuring inline
    comment a human reads before a source shows up; omitted, the field is
    offered instead as a commented-out hint (`# birth:   # …`) so it stays
    discoverable without faking an unsourced fact. Each of the two is decided
    independently - a stub can carry a real `birth:` and a still-commented
    `# death:`. Values are written verbatim: this function renders text, it
    does not validate EDTF shape (the caller - the CLI layer - normalises and
    validates the date before it ever reaches here, the same division of
    labor `process.py`'s scaffold renderer uses).
    """
    if sex is not None and sex not in PERSON_SEX_VALUES:
        raise ValueError(format_person_sex_error(sex))

    display_name = name if name and name.lower() != 'unknown' else 'unknown'
    lines = [
        '---',
        f'id: {pid}',
        f'aliases: [{pid}]',
        f'name: {yaml_inline(display_name)}',
    ]
    if sex is not None:
        lines.append(f'sex: {sex}')
    if gender is not None:
        lines.append(f'gender: {yaml_inline(gender)}')
    lines.append('living: unknown')
    if birth is not None:
        lines.append(f'birth: {birth}   # unsourced estimate - a tool will remind you to add a source')
    else:
        lines.append('# birth:   # an honest guess is fine - a tool will remind you to add a source later')
    # Optional place beside each provisional vital (plan-17 wireframe: the mint
    # and add-family forms ask "birth date + place, death date + place"). Same
    # provisional, unsourced standing as birth:/death: - purely frontmatter
    # family knowledge until a sourced claim supersedes the vital.
    if birth_place:
        lines.append(f'birth_place: {yaml_inline(str(birth_place).strip())}   # unsourced, goes with the birth estimate')
    if death is not None:
        lines.append(f'death: {death}   # unsourced estimate - a tool will remind you to add a source')
    else:
        lines.append('# death:   # same here; leave commented until you know')
    if death_place:
        lines.append(f'death_place: {yaml_inline(str(death_place).strip())}   # unsourced, goes with the death estimate')
    lines.append(f'created: {datetime.date.today().isoformat()}')
    lines.append('tier: stub')
    lines.append('---')
    return '\n'.join(lines) + '\n'


# Matches the `_{S-id}.md` suffix in a source record filename; used by
# find_source_record to locate a source by its ID without trusting the slug.
_SOURCE_RECORD_FILENAME_RE = re.compile(r'_(S-[0-9a-hjkmnp-tv-z]{10})\.md$', re.I)


def find_source_record(archive_root: str | Path, source_id: str) -> dict | None:
    """Return the parsed record dict for a source by its S-id, or None.

    Globs `sources/**/*.md` for a file whose `_{S-id}.md` suffix matches
    `source_id` (case-insensitive). The slug and subdirectory are mutable and
    are not matched - only the suffix carries identity. Used by `fha photoindex`
    to resolve `source-people` person references for photos that carry a matching
    `source_id` keyword: the source record's `people:` list is the human-maintained
    statement "this source shows these people," authoritative even when no bare
    P-id keyword has been written to the image file yet.

    Returns None when the record is absent or its frontmatter has parse errors;
    callers that need `people:` should treat None as "no people known from this source."
    """
    root = Path(archive_root)
    sources_dir = root / 'sources'
    if not sources_dir.is_dir():
        return None
    sid_norm = normalize_id(source_id)
    for p in sources_dir.rglob('*.md'):
        m = _SOURCE_RECORD_FILENAME_RE.search(p.name)
        if m and normalize_id(m.group(1)) == sid_norm:
            rec = read_record(p)
            if rec.get('parse_errors'):
                return None
            return rec
    return None


def configure_utf8_stdout() -> None:
    """Reconfigure stdout to UTF-8 so ✓/✗ render on Windows cp1252 terminals."""
    if hasattr(sys.stdout, 'reconfigure'):
        try:
            sys.stdout.reconfigure(encoding='utf-8')  # type: ignore[union-attr]
        except Exception:
            pass


# ── Output helpers ────────────────────────────────────────────────────────────

EXIT_CLEAN = 0
EXIT_WARNINGS = 1
EXIT_ERRORS = 2
EXIT_FAILURE = 3


class Finding:
    """A single lint finding (error or warning)."""

    __slots__ = ('severity', 'code', 'path', 'message')

    def __init__(self, severity: str, code: str, path: str | Path, message: str):
        self.severity = severity   # 'E' or 'W'
        self.code = code           # e.g. 'E001', 'W101'
        self.path = str(path)
        self.message = message

    def __str__(self) -> str:
        return f'{self.severity} {self.code} {self.path}: {self.message}'

    def as_dict(self) -> dict:
        return {
            'severity': self.severity,
            'code': self.code,
            'path': self.path,
            'message': self.message,
        }


def emit_findings(findings: list[Finding], use_json: bool = False) -> int:
    """
    Print findings to stdout and return the appropriate exit code.

    A convenience wrapper so tool CLIs don't need to know the EXIT_* →
    severity mapping.  Tools that want custom output formatting should
    loop over findings themselves and call EXIT_* constants directly.
    """
    import json

    if use_json:
        data = [f.as_dict() for f in findings]
        print(json.dumps(data, indent=2))
    else:
        for f in findings:
            print(str(f))

    has_errors = any(f.severity == 'E' for f in findings)
    has_warnings = any(f.severity == 'W' for f in findings)

    if has_errors:
        return EXIT_ERRORS
    if has_warnings:
        return EXIT_WARNINGS
    return EXIT_CLEAN


# ── The structured-result contract ────────────────────────────────────────────
#
# See the module docstring for the full rule.  In short: `run_*` returns a
# `Result`; `_cmd_*` renders it.  These two small dataclasses are the shared
# shape every tool conforms to, so a future consumer (a generator, a console, a
# UI) can read any tool's output as data instead of re-parsing each tool's text.

# Lint findings carry a one-letter severity ('E'/'W'); the Result contract uses a
# spelled-out level so a renderer never has to know lint's private alphabet.  The
# map is exact in both directions because lint only ever emits E or W.
_SEVERITY_TO_LEVEL: dict[str, str] = {'E': 'error', 'W': 'warning'}
LEVEL_TO_SEVERITY: dict[str, str] = {'error': 'E', 'warning': 'W'}


@dataclasses.dataclass
class Message:
    """One human-facing line a Result carries.

    `level` is the severity bucket - 'error', 'warning', or 'info' - so a renderer
    can count or color without parsing prose.  `text` is the plain-language body.
    `next_step` is the exact command or action that resolves it (AGENTS.md's
    "next-step rule"); it is None for purely informational lines, and for lint
    findings whose fix is already woven into `text`.

    `code` and `path` are optional structured locators.  They exist so a lint
    `Finding` (an E/W code against a specific file) folds losslessly into this
    one shape: code carries 'W101' etc., path carries the offending file.  Tools
    with no codes or no file context leave them None.
    """

    level: str
    text: str
    next_step: str | None = None
    code: str | None = None
    path: str | None = None

    def as_dict(self) -> dict:
        return {
            'level': self.level,
            'text': self.text,
            'next_step': self.next_step,
            'code': self.code,
            'path': self.path,
        }


@dataclasses.dataclass(eq=False)
class Result:
    """The structured return value of every tool's `run_*` function.

    One small, JSON-serializable record of what an operation computed and did.
    See the module docstring for the contract this participates in.  The defaults
    describe a clean, do-nothing success, so a caller can build one up
    incrementally: `Result().add('info', 'done')` or
    `Result(data={'rows': rows})`.

    Back-compat by design.  Before this contract, tools' run_* functions returned
    one of two shapes: a payload dict (`run_xref` → {'status', 'groups'}) or a
    bare exit-code int (`run_find` → EXIT_CLEAN).  A Result stands in for both so
    every caller keeps working while run_* uniformly returns a Result:
      - dict-style read access into `data`  → `result['groups']`, `result.get(k)`
      - equality with its exit code         → `result == EXIT_CLEAN`
    That is why `__eq__` is defined here (and the dataclass uses eq=False so this
    custom one is not overwritten); two Results compare by identity, which is all
    any caller needs.
    """

    ok: bool = True
    exit_code: int = EXIT_CLEAN
    data: dict = dataclasses.field(default_factory=dict)
    messages: list[Message] = dataclasses.field(default_factory=list)
    changed: list[str] = dataclasses.field(default_factory=list)

    def __eq__(self, other: object) -> bool:
        # `result == EXIT_CLEAN` lets callers/tests that previously received a
        # bare exit-code int keep comparing against the EXIT_* constants.
        if isinstance(other, Result):
            return self is other
        if isinstance(other, int):
            return self.exit_code == other
        return NotImplemented

    def add(
        self,
        level: str,
        text: str,
        *,
        next_step: str | None = None,
        code: str | None = None,
        path: str | Path | None = None,
    ) -> 'Result':
        """Append one human-facing message; returns self so calls can chain."""
        self.messages.append(
            Message(level, text, next_step, code,
                    str(path) if path is not None else None)
        )
        return self

    def note_changed(self, path: str | Path) -> 'Result':
        """Record a file this operation created/wrote/renamed; returns self."""
        self.changed.append(str(path))
        return self

    # Dict-style read access into `data`.  Several tools' run_* functions used to
    # return a plain payload dict (e.g. `run_report` → {'status', 'markdown', …});
    # exposing `result['markdown']` / `result.get('rows')` lets those callers (and
    # their tests) keep reading the payload by key while run_* now returns a
    # Result.  Read-only on purpose - building a Result is done through its fields.
    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.data

    def as_dict(self) -> dict:
        """Return a fully JSON-serializable view of this Result.

        `data` is coerced recursively: several wrappers stash non-JSON objects
        there (packet payloads keep `Path`s, places lint keeps `Finding`s), so a
        shallow copy would make `json.dumps(result.as_dict())` raise for exactly
        the headless consumers this contract is meant to serve.
        """
        return {
            'ok': self.ok,
            'exit_code': self.exit_code,
            'data': _jsonify(self.data),
            'messages': [m.as_dict() for m in self.messages],
            'changed': list(self.changed),
        }


def _jsonify(value: Any) -> Any:
    """Recursively coerce a value into a JSON-serializable form for `as_dict`.

    `Path`s become slash-normalized strings, objects exposing `as_dict()` (e.g.
    `Finding`) are expanded, and mappings/sequences are coerced element-wise.
    Anything else unrecognized falls back to `str()` so serialization never
    raises - a best-effort machine-readable view beats a `TypeError` for
    headless callers.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(k): _jsonify(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(v) for v in value]
    as_dict = getattr(value, 'as_dict', None)
    if callable(as_dict):
        return _jsonify(as_dict())
    return str(value)


def finding_to_message(finding: Finding) -> Message:
    """Fold a lint `Finding` into a Result `Message` (severity → level).

    The fix for a lint finding is already woven into its message text (e.g.
    "... run `fha views brackets --fix` to update"), so `next_step` stays None
    rather than duplicating it.
    """
    return Message(
        level=_SEVERITY_TO_LEVEL.get(finding.severity, 'info'),
        text=finding.message,
        next_step=None,
        code=finding.code,
        path=finding.path,
    )


def result_fail(
    result: Result,
    status: str,
    message: str,
    *,
    exit_code: int = EXIT_FAILURE,
    level: str = 'error',
    next_step: str | None = None,
) -> Result:
    """Mark `result` a non-success outcome and add its one human-facing line.

    The single refusal/not-found builder every write-back engine shares. Four
    tools had each grown a near-identical private copy - `fha confirm`'s
    `_fail`/`_notfound`, `fha claim`'s `_fail`/`_notfound`, `fha person`'s
    `_refuse_result`/`_not_found_result`, and `fha source`'s inline `_refuse`
    closure - so the shape (set `ok=False`, stamp `exit_code`, record
    `data['status']`, append one message) lives here once and they delegate.

    The default is the common case: a hard refusal (`EXIT_FAILURE`, an
    `error`-level line). A not-found result passes `exit_code=EXIT_WARNINGS`
    and `level='warning'` with `status='not-found'`; `next_step` carries the
    exact recovery command when there is one. The builder never changes the
    message text - each call site still owns its exact wording.
    """
    result.ok = False
    result.exit_code = exit_code
    result.data['status'] = status
    result.add(level, message, next_step=next_step)
    return result


def load_site_module():
    """Import tools/site.py under a private module name (shared by fha + serve).

    The tool's command is `fha site`, so its file must be `tools/site.py`
    (BUILD.md M8.1) - but the stem `site` collides with Python's stdlib `site`
    module, which is already in sys.modules from interpreter startup. A plain
    `import site` therefore returns the stdlib module, not ours. Loading the
    file by path under the alias `fha_site` sidesteps the collision without
    disturbing the cached stdlib module the way replacing sys.modules['site']
    would. `Path(__file__).parent` is `tools/` (this file's own directory), so
    the sibling `site.py` is found regardless of the caller's location.

    Both front doors (`fha` and `serve`) need this identical loader; it lives
    here so they cannot drift, even though a front door importing a tool engine
    is otherwise the exception, not the rule (tools never import tools).
    """
    import importlib.util

    mod = sys.modules.get('fha_site')
    if mod is not None:
        return mod
    path = Path(__file__).parent / 'site.py'
    spec = importlib.util.spec_from_file_location('fha_site', path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules['fha_site'] = mod
    spec.loader.exec_module(mod)
    return mod
