#!/usr/bin/env python3
"""
lint.py - fha lint: verify the archive against the spec.

  fha lint [--root PATH]             Walk and check the archive; report-only
  fha lint --with-exif               Also verify embedded SOURCE: keywords (slow)
  fha lint --json                    Machine-readable JSON output
  fha lint --format-check            Check formatting without fixing
  fha lint --format-write            Apply conservative formatting fixes (frontmatter normalization deferred)
  fha lint --mint-stubs              Create missing person stubs (E005 set)
  fha lint --spawn-questions         Append questions for E009 contradictions
  fha lint --fix-ids                 Complete hand-authored id-less records AND
                                     id-less claims (mint, rename, alias, stamp);
                                     template placeholder ids (P-__________)
                                     count as missing and are replaced in place
  fha lint --fix-reciprocal          Add the missing mirror edge for each W116

Exit codes: 0 = clean, 1 = warnings only, 2 = errors, 3 = tool failure.
SPEC §16, TOOLING §3.

HOW IT WORKS - TWO PASSES, NO PRIOR INDEX
------------------------------------------
Lint is fully self-contained: it does NOT require `fha index` to have run.
It builds its own in-memory Registry on the first pass, then runs cross-file
checks on the second pass once the full picture is available.

Pass 1 - walk and collect  (_walk_archive):
  Read every person and source file; register IDs, claims, token references,
  and metadata.  File-level checks fire here - the ones that don't need to see
  the rest of the archive: bad IDs, missing required fields, malformed EDTF
  dates, duplicate claim IDs within a source.

Pass 2 - cross-file checks  (_cross_file_checks):
  With the complete Registry in hand, check things that require the whole
  picture: orphan token references, duplicate record IDs, summary-block drift
  against accepted claims, vitals gaps for curated persons, merged-person
  references, and reverse asset inventory.

WHY IN-MEMORY, NOT THE SQLITE INDEX
  The SQLite index may not exist, or may be stale.  Lint is the source of
  truth - the index must match what lint accepts, never the other way around.
  Building a fresh Registry per run ensures lint is always consistent with
  what's actually on disk.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    CLAIMS_RE,
    CLAIM_TYPES,
    COMPANION_KINDS,
    CROCKFORD_ALPHA,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FRONT_RE,
    ID_RE,
    LEVEL_TO_SEVERITY,
    PROVISIONAL_VITAL_FIELDS,
    SIGNIFICANCE,
    SOURCE_TYPES,
    mint_ids,
    TOKEN_RE,
    VITAL_TYPES,
    Finding,
    Result,
    alias_clashes,
    build_alias_map,
    claim_item_key_indent,
    claims_edit_problem,
    edtf_bounds,
    files_carry_searchable_text,
    finding_to_message,
    format_bracket_child,
    is_genetic_parent_subtype,
    nonbirth_bracket_label,
    parentage_parties,
    photos_ignore_matcher,
    photos_ignore_patterns,
    spouse_extended_base,
    spouse_parties,
    strip_generational_suffix,
    extract_token_ids,
    extract_wikilinks,
    link_field_refs,
    resolve_ref,
    strip_link_wrapper,
    carries_person_record_fields,
    fmt_id_display,
    format_edtf_error,
    format_exiftool_error,
    format_roots_orphan_warning,
    format_source_type_error,
    format_w120_message,
    id_type_of,
    is_fixture_path,
    is_person_file_kind,
    is_template_file,
    is_working_copy,
    is_valid_edtf,
    is_valid_id,
    FhaConfigError,
    load_fha_yaml,
    normalize_date,
    normalize_id,
    parse_filename,
    read_record,
    read_text_exact,
    reapply_newline,
    resolve_path,
    resolve_root_arg,
    roots_change_orphans,
    sex_slot_is_defaulted,
    unreadable_dir_recorder,
    walk_files,
    write_text_exact_atomic,
    yaml_inline,
)

import yaml

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Data model
#    Registry                    - in-memory snapshot of one lint pass
#
#  Constants / small helpers
#    _SOURCE_FILENAME_RE         - grammar check for source filenames (SPEC §13)
#    _PERSON_FILENAME_RE         - grammar check for person filenames (SPEC §13)
#    _is_placeholder_id          - template placeholder id (P-__________) = MISSING
#    REQUIRED_*_FIELDS           - required frontmatter keys per record type
#    _normalize_alias_path       - backslash→slash normalisation for path comparison
#    _mapped_root, _path_to_alias - resolve fha.yaml alias roots to absolute paths
#    _is_generated_file / _never_mintable - GENERATED views + READMEs are never records
#    _resolve_person_ref         - one persons:/roles: ref → P-id (alias map first)
#    _claim_person_ids           - resolved P-ids from a claim's persons: field
#    _id_near_miss               - a ref that LOOKS like a mistyped record code
#    _parse_summary_block        - parse **Born/Died/…:** lines from a profile body
#    _edtf_gloss                 - plain-language gloss for a canonical EDTF value
#    _check_date_value           - forgiving date check: valid/loose-W109/broken-E014
#    _collect_token_refs         - scan a text block for [ID] tokens → registry
#    _research_hypothesis_ids    - H-ids defined in a research file's ## Hypotheses
#    _question_blocks            - split a questions.md into per-heading blocks
#    _metadata_values            - normalise scalar/list exiftool field values
#    _w122_message               - W122: filename says generated page, content says person
#
#  Pass 1 - walk and collect
#    _walk_archive               - top-level coordinator; calls the _process_* functions
#    _process_person_file        - index one person file + file-level checks
#                                   (returns the record it read; content decides the kind)
#    _process_source_file        - index one source file + file-level checks + claims
#
#  Bracket / Ahnentafel checks (W103, W110, W119, W120)
#    _build_child_edges          - parent → {child → {nature,…}} from accepted claims
#    _build_children_of          - parent → {children}; genetic_only filters numbering
#    _check_bracket_lists        - W103: stale bracket lists + missing `+ spouse` half
#    _build_ahnentafel_lint      - BFS from root_person using in-memory registry
#    _check_ahnentafel_placement - W110: person file in wrong Ahnentafel folder;
#                                   also emits W120 (slot defaulted, sex: unrecorded)
#    _check_direct_line_stubs    - W119: direct-line ancestor still filed as a stub
#                                   (report-only lead; brackets --fix-promote applies)
#
#  Relationship reconciliation (W115, W116)
#    _check_relationships_reconciliation - sourced relationships: entry vs claim
#    _check_reciprocity          - W116: a sourced edge unmirrored on the other person
#    _claim_by_id / _role_pids / _claim_backs_edge - claim lookup + role matching
#
#  Pass 2 - cross-file checks
#    _cross_file_checks          - top-level coordinator for all cross-file rules
#    _check_summary_line         - E013/W104: one **Label:** segment vs accepted claims
#    _has_question_for           - E009: co-occurrence check across question blocks
#    _get_person_accepted_claims - build accepted-claim list for one person
#    _check_reverse_inventory    - E011: document files vs source inventory lists
#    _check_embedded_source_keywords - E012: exiftool SOURCE: keyword vs inventory
#    _read_source_keywords       - invoke exiftool; parse its JSON keyword output
#    _check_generated_headers    - W105: hand-edits below a GENERATED header
#    _check_readme_age           - W108: README.md older than SPEC.md
#    _check_agent_drift          - E018: deprecated commands in AGENTS.md
#    _check_untranscribed_evidence - W124: accepted claims on evidence the
#                                   archive holds no words for (no transcript)
#    _check_roots_change         - W121: a roots: change orphaned filed assets
#                                   (runs first; the E011 fallout follows it)
#    _check_unreadable_dirs      - W123: a record folder this lint could not open
#                                   (runs last; the caveat on everything above)
#
#  Format checks / fix modes
#    _check_format               - W109: final newline, CRLF line endings
#    _fix_format                 - apply conservative format fixes
#    _file_newline               - the file's own newline style, for inserted lines
#                                   (byte-preserving IO itself is _lib's
#                                    read_text_exact / write_text_exact_atomic)
#    _wrap_unfenced_claims / _fix_claims_fence - verified ```yaml wrap for W114
#    _merge_aliases_into_frontmatter - add slug/stem aliases to an existing block
#    _fix_mint_ids               - complete id-less records: mint + rename + alias
#    _claim_id_missing           - absent/blank/placeholder claim id = mintable
#    _fix_mint_claim_ids         - complete id-less claims: mint id, stamp reviewed
#    _claim_item_spans           - split the claims YAML into per-item spans
#    _fix_mint_stubs             - create stubs for the E005 set (--mint-stubs)
#    _fix_spawn_questions        - append question entries for E009 set (--spawn-questions)
#    _fix_reciprocal             - append missing mirror edges for the W116 set (--fix-reciprocal)
#    _append_relationship_entry  - additive frontmatter surgery for the mirror entry
#
#  Main entry / CLI
#    run_lint                    - orchestrates both passes; returns a Result
#    _cmd_lint                   - render a lint Result (human text or --json) → exit code
#    register                    - attach 'lint' to the main fha parser
#    _run_lint                   - argparse → run_lint → _cmd_lint bridge
#    _standalone_main            - for `python tools/lint.py` direct invocation
#
# ─────────────────────────────────────────────────────────────────────────────


# ── Registry built during a lint run ─────────────────────────────────────────

class Registry:
    """
    In-memory snapshot of everything found in one lint pass.

    Populated entirely by Pass 1 (_walk_archive).  Read by Pass 2
    (_cross_file_checks) once every file has been processed.

    Lint builds its own Registry rather than reading the SQLite index so it
    can run without `fha index` having been run, and so lint is always
    consistent with what's on disk rather than with a potentially stale cache.
    """

    def __init__(self, archive_root: Path, fha_config: dict):
        self.archive_root = archive_root
        self.fha_config = fha_config
        self.is_fixture = is_fixture_path(archive_root)
        self.is_working_copy = is_working_copy(archive_root)

        # id → list of paths where that id appears as THE record id (frontmatter)
        self.person_profile_paths: dict[str, list[Path]] = {}   # P-id → [profile paths]
        self.person_companion_paths: dict[str, list[Path]] = {}  # P-id → [companion paths]
        self.source_paths: dict[str, Path] = {}   # S-id → path
        self.claim_ids: dict[str, str] = {}        # C-id → source S-id
        self.place_ids: set[str] = set()           # L-ids
        self.hypothesis_ids: set[str] = set()      # H-ids

        # id → path (for any type, first seen)
        self.all_record_ids: dict[str, Path] = {}

        # Claims by source: {S-id: [claim_dict, ...]}
        self.source_claims: dict[str, list[dict]] = {}

        # Persons: {P-id: meta_dict}
        self.person_meta: dict[str, dict] = {}

        # Person profile bodies: {P-id: body_text} - read once during the walk so
        # the needs-sourcing backlog can scan for `(TODO: import source)` prose
        # without re-reading every file.
        self.person_bodies: dict[str, str] = {}

        # Source files whose `## Claims` content was read UNfenced (a human forgot
        # the ```yaml fence): {S-id: path}. `fha lint --fix-claims-fence` wraps them.
        self.unfenced_claim_sources: dict[str, Path] = {}

        # Hand-authored records with NO id: and a filename lacking the `_{ID}`
        # suffix - a valid pre-machine state (SPEC §4/§10). Auto-mintable, not an
        # error: [(path, 'P'|'S')]. `fha lint --fix-ids` mints + renames + aliases.
        self.idless_records: list[tuple[Path, str]] = []

        # Records whose id: is still a template placeholder (`P-__________`) -
        # a subset marker for idless_records so the auto-mintable listing and
        # `--fix-ids` can say "the placeholder will be replaced" instead of
        # "no ID yet", and so the fixer knows to rewrite the existing id: line
        # rather than insert a second one.
        self.placeholder_id_paths: set[Path] = set()

        # Source meta: {S-id: meta_dict}
        self.source_meta: dict[str, dict] = {}

        # Source file inventory: {S-id: {alias/path, ...}}
        self.source_inventory: dict[str, set[str]] = {}

        # All token IDs referenced in prose/frontmatter (value → list of (path, line))
        self.token_refs: dict[str, list[tuple[Path, int]]] = {}

        # Non-ID name/stem wikilinks in prose (lowercased target → [(path, line)]).
        # `[[Ken Smith]]` lands here, not in token_refs; used to tell a LATENT
        # name clash from an ACTIVE one (a link that actually uses the ambiguous
        # name and so must be pinned to an ID).
        self.name_link_refs: dict[str, list[tuple[Path, int]]] = {}

        # Source frontmatter cross-link fields ({S-id: {'people': [...], ...}}),
        # captured raw so Pass 2 can resolve them through the alias map.
        self.source_links: dict[str, dict[str, list[str]]] = {}

        # Questions file content (for E009)
        self.questions_content: str = ''

        # Research files content (for E009)
        self.research_content: dict[Path, str] = {}

        # Missing reciprocal relationship edges (W116), captured structurally so
        # `--fix-reciprocal` can append the mirror without re-parsing messages.
        # Each: {other_pid, owner_pid, mirror_role, subtype, claim_id}
        self.missing_mirrors: list[dict] = []

        # Record folders Pass 1 could not list (W123). A lint that reports
        # "0 errors" is a certificate, and it must not be issued over records
        # nobody could read: whatever is filed in these folders was never
        # checked against the spec at all.
        self.unreadable_dirs: list[Path] = []

        # The alias resolve map (alias_lower → canonical id), built once at the
        # start of Pass 2 from everything Pass 1 collected. Claim persons:/roles:
        # references resolve through it (TOOLING §3 E004: "resolved through the
        # alias map first"), so the fix modes and the backlog that run after
        # Pass 2 see the same resolution the checks did. Empty during Pass 1.
        self.alias_map: dict[str, str] = {}

    def all_known_ids(self) -> set[str]:
        """All IDs that have a defining record in the archive."""
        ids: set[str] = set()
        ids.update(self.person_profile_paths.keys())
        ids.update(self.person_companion_paths.keys())
        ids.update(self.source_paths.keys())
        ids.update(self.claim_ids.keys())
        ids.update(self.place_ids)
        ids.update(self.hypothesis_ids)
        return ids

    def has_person(self, pid: str) -> bool:
        """Return True if pid has at least a stub or profile record."""
        pid = normalize_id(pid)
        return pid in self.person_profile_paths or pid in self.person_companion_paths


# ── Filename grammar patterns (SPEC §13) ─────────────────────────────────────
_SOURCE_FILENAME_RE = re.compile(
    r'^[a-z0-9][a-z0-9\-]*_S-[0-9a-hjkmnp-tv-z]{10}$', re.I
)
_PERSON_FILENAME_RE = re.compile(
    # Optional MERGED-INTO-P-<survivor>__ tombstone prefix (SPEC §9): a merged
    # person's file persists forever under this rename, so the grammar must
    # accept it rather than flag the spec-mandated form as a bad filename.
    # The primary-sort-name slot before `__` is OPTIONAL (SPEC §13): a surname-less
    # person (a mononym, an enslaved ancestor by given name, a patronymic, a
    # foundling) leads with the double underscore, e.g. `__caesar_P-…`.
    # Both name slots admit interior hyphens (`smith-jones__anne`,
    # `hartley__mary-jane`) - SPEC §13 never forbids them, and hyphenated
    # surnames/given names are ordinary names, not grammar errors ("forgiving,
    # not fussy"). A hyphen cannot lead a slot (the first char stays a letter),
    # and the companion-kind suffix is untouched: kind classification lives in
    # _lib.parse_filename (an endswith test on `_research`/`_timeline`/
    # `_sources-index`/`_draft-queue`), which a hyphen inside a NAME slot can
    # never satisfy - the kind needs its own leading underscore.
    r'^(MERGED-INTO-P-[0-9a-hjkmnp-tv-z]{10}__)?'
    r'([a-z][a-z_-]*)?__[a-z][a-z_-]*(_[a-z][a-z0-9\-]*)?_P-[0-9a-hjkmnp-tv-z]{10}$', re.I
)

# A copy-paste template's placeholder id value, e.g. `P-__________` (the shipped
# archive-template forms are exactly TYPE dash + ten underscores; >=4 tolerates a
# hand-shortened run). Underscores are not in the Crockford Base32 alphabet, so
# this can never collide with a real id. A record carrying one is treated as
# having NO id (auto-mintable, `--fix-ids` replaces the placeholder) - the
# template's own comment promises "LINT WILL CREATE FOR YOU LATER IF MISSING",
# so a hard E002 here would break the template's contract with the human.
_PLACEHOLDER_ID_RE = re.compile(r'^[PSCLH]-_{4,}$', re.I)


def _is_placeholder_id(value: str) -> bool:
    """True when an id: value is a template placeholder (`P-__________`), which
    lint treats as MISSING rather than malformed - see _PLACEHOLDER_ID_RE."""
    return bool(_PLACEHOLDER_ID_RE.fullmatch(str(value).strip()))


# ── Required-field sets ───────────────────────────────────────────────────────

REQUIRED_PERSON_FIELDS = {'id', 'name', 'living'}
REQUIRED_SOURCE_FIELDS = {'id', 'title', 'source_type'}
REQUIRED_CLAIM_FIELDS  = {'id', 'type', 'persons', 'value', 'status', 'confidence'}

# Controlled vocabularies validated by E019 (SPEC §8.1 status lifecycle, §8.5
# confidence). Values outside these sets are typos that would silently corrupt
# accepted-claim rollups (e.g. `status: acccepted` is never counted as accepted).
VALID_CLAIM_STATUS = frozenset({
    'suggested', 'needs-review', 'accepted', 'disputed', 'rejected', 'superseded',
})
VALID_CONFIDENCE = frozenset({'high', 'medium', 'low'})

# ── Summary block parsing (E013) ──────────────────────────────────────────────

_SUMMARY_LABEL_RE = re.compile(
    r'\*\*(Born|Died|Married|Parents|Children):\*\*'
)
_SOURCE_KEYWORD_RE = re.compile(r'^SOURCE:\s*(S-[0-9a-hjkmnp-tv-z]{10})$', re.I)


def _normalize_alias_path(path_text: str) -> str:
    """Normalize stored archive paths to forward-slash alias form."""
    return path_text.replace('\\', '/').lstrip('./')


def _mapped_root(alias: str, registry: Registry) -> Path:
    """Return the absolute disk root for an asset alias such as documents or photos."""
    return resolve_path(alias, registry.fha_config, registry.archive_root)


def _path_to_alias(path: Path, alias: str, registry: Registry) -> str | None:
    """Convert a resolved asset path back to its stable alias/path spelling."""
    root = _mapped_root(alias, registry).resolve()
    try:
        rel = path.resolve().relative_to(root)
    except ValueError:
        return None
    return f'{alias}/{rel.as_posix()}'


# The generated-file ownership marker (TOOLING §1): a file is tool-owned when
# this is the prefix of its FIRST NON-BLANK line - never merely present in the
# body. Mirrors views.py's `first_nonblank_line(...).startswith(_GEN_MARKER)`
# ownership test (its marker adds "by fha views"; this one is tool-agnostic so
# any generator's output is recognized). Local copy on purpose - consolidating
# the check into _lib.py is noted follow-up work, not done here.
_GENERATED_MARKER = '<!-- GENERATED'


def _is_generated_file(path: Path) -> bool:
    """True when path's first non-blank line starts the GENERATED header.

    Generated views (couple-folder sources-index.md and friends) carry no `id:`
    BY DESIGN - they are rebuilt by `fha views`, never hand-completed. Without
    this check the id-less classifier proposed them as "hand-authored, no ID
    yet" and `--fix-ids` injected frontmatter ABOVE the header and renamed them
    into phantom person/source records with permanent garbage IDs."""
    try:
        text = path.read_text(encoding='utf-8', errors='ignore')
    except OSError:
        return False
    for line in text.splitlines():
        if line.strip():
            return line.startswith(_GENERATED_MARKER)
    return False


def _never_mintable(path: Path) -> bool:
    """True for files the id-less/auto-mintable classifier must never claim:
    generated views (tool-owned, id-less by design) and README.md files (the
    quickstart kit ships READMEs inside people/, which are documentation, not
    person records)."""
    return path.name.lower() == 'readme.md' or _is_generated_file(path)


def _resolve_person_ref(ref: str, alias_map: dict[str, str] | None) -> str | None:
    """One persons:/roles: reference → a normalized P-id, or None when inert.

    The same tolerance source frontmatter `people:` already gets (TOOLING §3
    E004: targets are "resolved through the alias map first"): an ID-shaped
    target is kept even when dangling (E005 owns integrity); a name resolves
    only when the alias map knows it unambiguously AND it names a person; an
    unknown or ambiguous name is "an inert note-link, not a finding" and
    contributes nothing. Mirrors _lib.resolve_typed_ref(want='P') - the shared
    helper index.py now consumes - so lint and the index agree on which persons
    a claim names (lint keeps this local copy until the K4 consolidation wave)."""
    if id_type_of(ref):
        return normalize_id(ref)
    resolved = resolve_ref(ref, alias_map) if alias_map else None
    if resolved and id_type_of(resolved) == 'P':
        return resolved
    return None


def _claim_person_ids(claim: dict, alias_map: dict[str, str] | None = None) -> list[str]:
    """Return normalized P-ids from a claim's persons: field.

    Entries pass through link_field_refs (bare IDs, quoted or unquoted
    wikilinks all reduce to their target) and then _resolve_person_ref, so
    `persons: ["[[Sam Rivera]]"]` - the form the quickstart teaches - joins to
    its person record instead of producing a literal `[[sam rivera]]` string.
    Callers without an alias map (there are none in the lint passes, but the
    default keeps the helper safe standalone) still get wrapped bare-ID
    tolerance."""
    if not isinstance(claim, dict):
        return []   # a malformed claims entry (a bare string) is lint fodder, not a crash
    out: list[str] = []
    for ref in link_field_refs(claim.get('persons')):
        pid = _resolve_person_ref(ref, alias_map)
        if pid:
            out.append(pid)
    return out


# Plain nouns for the record-type prefixes, used when a near-miss code is
# described to the human ("looks like a person code but ...").
_TYPE_WORD = {'P': 'person', 'S': 'source', 'C': 'claim', 'L': 'place',
              'H': 'hypothesis'}


def _id_near_miss(ref: str) -> tuple[str | None, str] | None:
    """(type letter or None, plain description) when `ref` looks like a
    MISTYPED record code; None when it reads as an ordinary name.

    A reference that almost parses as an ID must produce a finding, never
    silence: `P-de957bcda` (nine characters) or `P-de957bcdal` (an `l`, a
    letter Crockford Base32 leaves out) is a typo'd code, and treating it as
    an inert name-link silently detaches the claim from its person - the
    index drops the row and `fha stubs` skips it, so nothing anywhere would
    ever mention the typo. Two shapes qualify:

      - a type prefix (`P-`/`S-`/`C-`/`L-`/`H-`) whose body fails the ID
        grammar (wrong length, or letters outside 0-9 a-z minus i l o u) -
        the type letter is returned so the message can name the record kind;
      - a bare 8-12 character token that is mostly Crockford characters AND
        carries a digit (a code pasted without its prefix) - type None.

    The TOOLING contract that an unresolved NAME stays an inert note-link is
    preserved: names have no type prefix, and the bare shape demands a digit
    plus >=80% Crockford characters, which words never combine. Template
    placeholders (`C-__________`) are excluded - their story belongs to the
    E010/`--fix-ids` path, not the typo net. Callers check the alias map
    FIRST, so a string that genuinely resolves is never flagged.
    """
    s = ref.strip()
    if not s or is_valid_id(s) or _is_placeholder_id(s):
        return None
    pm = re.match(r'^([PSCLH])-(.*)$', s, re.I)
    if pm:
        letter, body = pm.group(1).upper(), pm.group(2)
        # Only a body that actually looks like a (mistyped) code is a near-miss:
        # code-length-ish, purely alphanumeric, and carrying a digit the way a
        # random Base32 id does. A plain note-link that merely starts with a
        # type letter and a hyphen (`L-something`, `C-note`, `C-Grandpa's`) is
        # left as the inert note-link the TOOLING contract promises, not a
        # blocking error - words don't combine near-code-length, all-alnum, AND
        # a digit.
        if re.fullmatch(r'[0-9A-Za-z]{8,12}', body) and any(ch.isdigit() for ch in body):
            if len(body) != 10:
                return letter, (
                    f'is {len(body)} character(s) after the {letter}- instead of 10 - '
                    f'codes are exactly 10 characters from the alphabet 0-9 a-z '
                    f'minus i l o u')
            bad = sorted({ch for ch in body if ch.lower() not in CROCKFORD_ALPHA})
            if bad:
                listed = ', '.join(repr(ch) for ch in bad)
                return letter, (
                    f'contains {listed} - the code alphabet is 0-9 a-z minus i l o u')
        return None
    # A bare code pasted without its prefix is EXACTLY 10 characters. Requiring
    # that (not the old 8-12 window) keeps a name+year token like `Anna1850` or
    # `John1042` - shorter, and a perfectly ordinary free-text name - out of the
    # blocking-error net (SPEC/TOOLING: unresolved names stay inert note-links).
    if (re.fullmatch(r'[0-9A-Za-z]{10}', s) and any(ch.isdigit() for ch in s)
            and not s.isdigit()):
        crockford = sum(1 for ch in s if ch.lower() in CROCKFORD_ALPHA)
        if crockford / len(s) >= 0.8:
            return None, (
                'reads like a bare record code missing its type prefix - codes '
                'are written like P-de957bcda1 (type letter, hyphen, then 10 '
                'characters)')
    return None


def _near_miss_text(ref: str, near: tuple[str | None, str]) -> str:
    """One phrase describing a near-miss ref, e.g.
    `'P-de957bcda' looks like a person code but is 9 character(s)...`."""
    letter, detail = near
    if letter:
        return f'{ref!r} looks like a {_TYPE_WORD[letter]} code but {detail}'
    return f'{ref!r} {detail}'


def _parse_summary_block(body: str) -> list[tuple[str, str, list[str], list[str]]]:
    """
    Parse the summary block from a curated profile body.
    Handles both multi-line and inline (single-line) summary blocks.
    Returns list of (label, segment_text, p_ids, s_ids) for each **Label:** occurrence.
    """
    # Collapse the body to one searchable string up to the first ## section
    # (summary block is before ## Biography etc.)
    section_break = re.search(r'^##\s+\w', body, re.M)
    summary_text = body[:section_break.start()] if section_break else body

    # Find all label positions
    matches = list(_SUMMARY_LABEL_RE.finditer(summary_text))
    if not matches:
        return []

    results = []
    for i, m in enumerate(matches):
        label = m.group(1)
        seg_start = m.end()
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(summary_text)
        segment = summary_text[seg_start:seg_end].strip()

        # The citation is the contract: extract the bare IDs from the segment's
        # tokens (new `[[P-…|display]]` or legacy `[P-…]`), comparing on the ID and
        # ignoring any display text. extract_token_ids handles both bracket forms.
        seg_ids = extract_token_ids(segment)
        p_ids = [i for i in seg_ids if id_type_of(i) == 'P']
        s_ids = [i for i in seg_ids if id_type_of(i) == 'S']
        results.append((label, segment, p_ids, s_ids))

    return results


# ── Forgiving date handling (PR 05) ──────────────────────────────────────────

def _edtf_gloss(edtf: str) -> str:
    """Plain-language gloss for a canonical EDTF value.

    Used when lint suggests a normalized date so the human sees the meaning, not
    just the code: '1870~' is shown as 'about 1870', '187X' as 'the 1870s'.
    """
    if '/' in edtf:
        a, b = edtf.split('/', 1)
        return f'between {a} and {b}'
    before_m = re.match(r'^\[\.{2}(.+)\]$', edtf)
    if before_m:
        return f'on or before {before_m.group(1)}'
    decade_m = re.match(r'^(\d{3})X$', edtf)
    if decade_m:
        return f'the {decade_m.group(1)}0s'
    if edtf.endswith('~'):
        return f'about {edtf[:-1]}'
    if edtf.endswith('?'):
        return f'{edtf[:-1]}, uncertain'
    return edtf


def _check_date_value(
    value: object,
    field: str,
    prefix: str,
    path: Path,
    findings: list[Finding],
) -> None:
    """Check one date field the forgiving way (PR 05 - "forgiving, not fussy").

    Three outcomes, in line with AGENTS.md → "Who you serve":
      - Already valid EDTF → nothing.
      - Loose but clear ("circa 1870", "1870s", "before 1920") → a gentle W109
        suggestion naming the canonical form and its meaning.  The human's intent
        is plain; only the spelling differs, so this is never a hard error.  The
        archive still reads the loose value correctly (edtf_bounds normalizes it),
        so nothing downstream breaks while the human gets a nudge toward the
        stored form.
      - Genuinely unreadable → E014 with copyable examples (format_edtf_error),
        one plain message rather than a wall of codes.

    `prefix` is an optional lead-in such as 'Claim C-… : ' so claim-level and
    source-level dates read naturally with the same helper.
    """
    val = str(value).strip()
    if not val or is_valid_edtf(val):
        return
    suggestion = normalize_date(val)
    if suggestion:
        findings.append(Finding('W', 'W109', path,
            f'{prefix}{field} {val!r} understood as {suggestion!r} '
            f'({_edtf_gloss(suggestion)}); store it that way to match the archive date form'))
    else:
        findings.append(Finding('E', 'E014', path,
            f'{prefix}{format_edtf_error(val, field=field)}'))


# ── Walk and collect ─────────────────────────────────────────────────────────

def _collect_token_refs(text: str, path: Path, registry: Registry) -> None:
    """Index citation tokens in text.

    ID tokens (`[[S-…]]`, legacy `[S-…]`) go to `token_refs` for the E004/E005
    resolution checks. Non-ID name/stem wikilinks (`[[Ken Smith]]`) go to
    `name_link_refs` - they are ordinary Obsidian links, never E004 candidates,
    but a name link is what makes a name clash ACTIVE (must be pinned to an ID)."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in TOKEN_RE.finditer(line):
            tid = normalize_id(m.group(1))
            registry.token_refs.setdefault(tid, []).append((path, lineno))
        for target, _disp, _frag, _span in extract_wikilinks(line):
            if id_type_of(target):
                continue   # ID wikilinks are already handled above
            registry.name_link_refs.setdefault(target.lower(), []).append((path, lineno))


# Where hypothesis records LIVE (SPEC §16): the `## Hypotheses` section of a
# person research file, one `- id: H-… / hypothesis: … / …` entry per belief.
# These two patterns mirror index.py's discovery (_extract_section_body +
# _parse_md_list_blocks feeding _index_hypotheses_block) without importing it
# (tools never import tools): the section is the text between the heading and
# the next `##`, and an id field sits either on the entry's `- id:` line or on
# an indented continuation line. Only the H-id is needed here, so the entry
# parse reduces to the id-line shapes.
_HYPOTHESES_SECTION_RE = re.compile(
    r'^##\s*Hypotheses\s*$(.*?)(?=^##\s|\Z)', re.M | re.S,
)
_HYPOTHESIS_ID_LINE_RE = re.compile(
    r'^[ \t]*(?:-[ \t]+)?id:[ \t]*["\']?(H-[0-9a-hjkmnp-tv-z]{10})\b', re.M | re.I,
)


def _research_hypothesis_ids(body: str) -> set[str]:
    """H-ids DEFINED in a research file's `## Hypotheses` section.

    SPEC §16 homes hypothesis records there, and index.py already indexes them
    from there - so lint must count them as existing records too, or every
    `[[H-…]]` cite of a research-file hypothesis is a false E004 "create the
    missing record". Scope mirrors the index: only `id:` entry lines inside the
    Hypotheses section define an H-id; a mere `[[H-…]]` citation elsewhere in
    the file is a reference, never a definition, so a genuinely dangling H-id
    still fails E004."""
    ids: set[str] = set()
    for section in _HYPOTHESES_SECTION_RE.finditer(body):
        for m in _HYPOTHESIS_ID_LINE_RE.finditer(section.group(1)):
            ids.add(normalize_id(m.group(1)))
    return ids


def _w122_message(path: Path, parsed: dict, meta: dict) -> str:
    """W122: the file's name says generated page, the file itself says person.

    Written for a genealogist with a paper-filing mental model (AGENTS.md,
    "Who you serve"), so it says four things and no more: what the tools
    thought, that reading it as this person's own record is the right answer,
    the one rename that ends the confusion, and that keeping the name is a
    perfectly good choice. No jargon - the words "frontmatter", "companion" and
    "kind slot" are the machinery, and the machinery is ours to operate.

    The suggested name simply drops the last word of the name part. A person
    filename never has to carry every given name (SPEC §13's slug is a sort
    aid, not the name itself); the full name lives on the `name:` line and
    nothing else about the record changes. When dropping that word would leave
    no name at all (`hartley__timeline_P-…`, someone whose one given name IS
    the word), no filename is proposed - the message asks for one instead of
    offering `hartley___P-…`.
    """
    kind = parsed['kind']
    who = str(meta.get('name') or '').strip()
    # Trim by length, not by matching text: the filename's id may be written in
    # any case (`_P-…` / `_p-…`) while parse_filename lowercases it.
    id_display = fmt_id_display(parsed['id_str'])
    before_id = path.stem[:-(len(parsed['id_str']) + 1)]   # …__marie_timeline
    shortened = before_id[:-(len(kind) + 1)]               # …__marie
    if shortened and not shortened.endswith('_'):
        rename = (f'rename this file to {shortened}_{id_display}{path.suffix} - '
                  f'the file name does not have to carry every given name, and '
                  f'the full name stays on the name: line inside')
    else:
        rename = (f'give the file a name that does not end in "{kind}" just '
                  f'before the code - the file name does not have to carry '
                  f'every given name, and the full name stays on the name: '
                  f'line inside')
    subject = f'{who}\'s' if who else 'this person\'s'
    word = kind.replace('-', ' ')
    return (
        f'The name of {path.name} ends in "{kind}", which is how this archive '
        f'names the {word} page it builds for a person - but the file holds a '
        f'person\'s own details, so the tools read it as {subject} record. That '
        f'is the right reading and nothing is missing; the only oddity is that '
        f'a {word} page built for this person comes out named '
        f'{before_id}_{kind}_{id_display}{path.suffix}, with the word twice. '
        f'To clear it up, {rename}. If "{kind}" really is part of this '
        f'person\'s name, leave the file exactly as it is: the record is read '
        f'correctly either way, and this note will simply keep appearing.'
    )


def _walk_archive(archive_root: Path, registry: Registry, findings: list[Finding]) -> None:
    """
    Pass 1: walk the archive tree and populate the registry.

    File-level checks fire here - the ones that don't need to see the whole
    archive.  Anything that requires knowing whether another record exists
    (orphan references, vitals gaps, summary-block drift) is deferred to Pass 2.

    Walk order: places → people → sources → notes.  Places are indexed first
    so their L-ids are available when Pass 2 checks place references in claims.
    """

    # Places
    places_path = archive_root / 'places' / 'places.yaml'
    if places_path.exists():
        try:
            with open(places_path, encoding='utf-8') as f:
                places = yaml.safe_load(f) or []
            for place in (places if isinstance(places, list) else []):
                if isinstance(place, dict):
                    pid = normalize_id(str(place.get('id', '')))
                    if pid and pid.startswith('l-'):
                        registry.place_ids.add(pid)
                        registry.all_record_ids[pid] = places_path
        except Exception as e:
            findings.append(Finding('E', 'E010', places_path, f'places.yaml parse error: {e}'))

    # Notes: load questions + research for E009 check
    questions_path = archive_root / 'notes' / 'questions.md'
    if questions_path.exists():
        try:
            registry.questions_content = questions_path.read_text(encoding='utf-8')
        except OSError:
            pass

    # People
    #
    # `walk_files` with a recorder, not rglob: lint's whole product is the
    # sentence "your archive matches the spec", and rglob would hand it that
    # sentence over a subtree it never opened. Every record walk below shares
    # one recorder; W123 reports the folders at the end of Pass 2.
    on_error = unreadable_dir_recorder(registry.unreadable_dirs)
    people_root = archive_root / 'people'
    if people_root.exists():
        for path in sorted(walk_files(people_root, suffix='.md', on_error=on_error)):
            rec = _process_person_file(path, registry, findings)
            # Collect research file content for E009. The record it just read
            # comes back so the kind is decided by CONTENT here too: a file
            # named `…_research_P-….md` that carries a person record is that
            # person's profile (SPEC §13's kind slot is also a legal last given
            # name), and SPEC §16 homes neither ## Hypotheses nor ## Open
            # Questions in a profile. Reading it as research pulled her whole
            # file into the E009 scope.
            if rec is not None and is_person_file_kind(path, 'research', rec['meta']):
                try:
                    registry.research_content[path] = path.read_text(encoding='utf-8')
                except OSError:
                    pass

    # Sources
    sources_root = archive_root / 'sources'
    if sources_root.exists():
        for path in sorted(walk_files(sources_root, suffix='.md', on_error=on_error)):
            _process_source_file(path, registry, findings)

    # Notes FTS (for token refs)
    notes_root = archive_root / 'notes'
    if notes_root.exists():
        for path in sorted(walk_files(notes_root, suffix='.md', on_error=on_error)):
            try:
                text = path.read_text(encoding='utf-8')
                _collect_token_refs(text, path, registry)
                # Collect H-ids from notes
                for m in ID_RE.finditer(text):
                    if m.group(1).upper() == 'H':
                        registry.hypothesis_ids.add(normalize_id(m.group(0)))
            except OSError:
                pass


def _process_person_file(path: Path, registry: Registry,
                         findings: list[Finding]) -> dict | None:
    """Process one person file into the registry, with file-level checks.

    Returns the record it read (frontmatter + body), or None for a file that
    is not a record at all (a `_TEMPLATE.*` copy). The caller needs the same
    frontmatter to decide whether this is a research companion, and reading
    the file twice to answer the same question is how the two readings drifted
    apart in the first place.
    """
    if is_template_file(path):
        return None   # `_TEMPLATE.*` is a teaching template, not a record
    rec = read_record(path)
    meta = rec['meta']

    # Parse errors → E010
    for code, msg in rec['parse_errors']:
        findings.append(Finding('E', code, path, msg))

    pid_raw = str(meta.get('id', ''))
    pid = normalize_id(pid_raw)
    id_placeholder = _is_placeholder_id(pid_raw)

    # E002: ID format check. A template placeholder (`P-__________`) is not
    # malformed - it is the shipped "fill me in later" value, handled as a
    # MISSING id below so the record stays auto-mintable, never a hard error.
    if pid_raw and not id_placeholder and not is_valid_id(pid_raw):
        findings.append(Finding('E', 'E002', path, f'Malformed ID: {pid_raw!r}'))

    # What this file IS: its own content first, the filename as a hint.
    #
    # SPEC §13 puts the companion kind immediately before the P-id
    # (`hartley__thomas_timeline_P-…`), but underscores are legal inside given
    # names, so that slot is shared with the last given name and the grammar
    # cannot separate them. Reading the stem alone filed Marie Timeline
    # Hartley's record under the companion paths, where none of the §9 profile
    # checks run - and lint reported the archive clean while she had no index
    # row anywhere (`_lib.carries_person_record_fields`). Content can only
    # promote a file to a profile, never demote one, so a sparse stub named as
    # a profile stays a profile.
    stem = path.stem
    parsed = parse_filename(path)
    is_person_record = carries_person_record_fields(meta)
    is_companion = bool(
        parsed and parsed.get('is_companion', False) and not is_person_record)

    # W122: the filename and the content disagree, and the tools resolved it
    # in the content's favour. Reported rather than settled in silence - the
    # human is the only one who knows whether "Timeline" is this person's name.
    if parsed and parsed.get('kind_ambiguous') and is_person_record:
        findings.append(Finding('W', 'W122', path,
                                _w122_message(path, parsed, meta)))

    # H-ids defined in this file's ## Hypotheses section (SPEC §16 homes them in
    # `…_research_P-….md`). The kind comes from the shared filename grammar plus
    # this file's own frontmatter, not a substring search of the stem:
    # `research` anywhere but the slot before the P-id is part of the given
    # names, and a file in that slot that carries a person record is that
    # person's profile - reading either one as a research file turned one
    # person's working notes into archive-wide hypothesis records.
    # Applied before any id checks so a mid-graduation (id-less) research file's
    # hypotheses still count as existing records for E004.
    if is_person_file_kind(path, 'research', meta):
        registry.hypothesis_ids.update(_research_hypothesis_ids(rec['body']))

    if id_placeholder:
        registry.placeholder_id_paths.add(path)
        if parsed:
            # The filename already carries the real code; the frontmatter just
            # wasn't updated. That is the E003 filename-vs-record mismatch, with
            # the fix being a paste, not a mint.
            findings.append(Finding('E', 'E003', path,
                f'id: is still the template placeholder {pid_raw!r}, but the filename '
                f'already carries {fmt_id_display(parsed["id_str"])} - paste that code '
                f'into the id: line.'))
        # From here the record is treated as having no id at all: no E002, and
        # (when the filename has no id either) it lands on the auto-mintable
        # list, where --fix-ids replaces the placeholder with a fresh id.
        pid_raw, pid = '', ''

    # E002: Filename grammar check
    if pid and not is_companion:
        # Profile filename: {primary_sort_name}__{given}[_{kind}]_{P-id}.md
        # (the sort-name slot may be empty: a surname-less `__caesar_P-…`).
        if _PERSON_FILENAME_RE.fullmatch(stem):
            pass
        elif '__' not in stem:
            # No double-underscore sort separator. Don't reject a hand-named
            # one-word file (SPEC §13): guide toward the grammar. The surname-less
            # convention is to LEAD with `__` (`__caesar_P-…`); a name that should
            # sort under a surname wants `{surname}__{given}_P-…`.
            findings.append(Finding('W', 'W117', path,
                f'Person filename {path.name} has no "__" sort separator. The sort '
                f'name goes before "__" ({{surname}}__{{given}}_P-…); for someone with '
                f'no surname, lead with the double underscore (__{stem.split("_")[0]}_P-…). '
                f'Rename if it should sort under a surname; otherwise this is fine.'))
        else:
            findings.append(Finding('E', 'E002', path,
                f'Person filename fails SPEC §13 grammar: {path.name}'))
    elif pid and is_companion:
        if not _PERSON_FILENAME_RE.fullmatch(stem):
            findings.append(Finding('E', 'E002', path,
                f'Person companion filename fails SPEC §13 grammar: {path.name}'))
    elif not parsed and 'P-' in stem.upper():
        findings.append(Finding('E', 'E002', path,
            f'Person filename missing valid trailing P-id: {path.name}'))

    # E003: Filename ID vs record ID
    if parsed and pid:
        file_id = normalize_id(parsed['id_str'])
        if file_id != pid:
            findings.append(Finding('E', 'E003', path,
                f'Filename ID {file_id!r} ≠ record id {pid!r}'))

    if not pid:
        # Generated companion files (timeline, sources-index, draft-queue) carry
        # no frontmatter `id:`, but their filename still encodes the P-id; derive
        # it from there so W110 placement checks (which scan person_companion_paths)
        # still see these files instead of silently missing stray ones.
        if is_companion and parsed:
            pid = normalize_id(parsed['id_str'])
            registry.person_companion_paths.setdefault(pid, []).append(path)
        elif not pid_raw and parsed is None and not _never_mintable(path):
            # A hand-authored, id-less record (no `id:`, no `_{P-id}` in the
            # filename). Not an error - auto-mintable on the next `fha lint
            # --fix-ids`. Surfaced (not silently dropped, which was the data-loss
            # trap) so the human sees it. GENERATED views (a couple folder's
            # sources-index.md) and README.md files are id-less BY DESIGN, never
            # mintable - see _never_mintable.
            registry.idless_records.append((path, 'P'))
        # Can't do further cross-reference checks without an id, but the record
        # still goes back to the caller: its content is what decides whether
        # this is a research companion, id or no id.
        return rec

    # Register in registry
    if is_companion:
        registry.person_companion_paths.setdefault(pid, []).append(path)
        # Accumulate companion body text so _needs_sourcing_backlog can scan TODOs
        # across all files belonging to this person, not just the profile.
        registry.person_bodies[pid] = registry.person_bodies.get(pid, '') + '\n' + rec['body']
    else:
        registry.person_profile_paths.setdefault(pid, []).append(path)
        registry.person_meta[pid] = meta
        registry.person_bodies[pid] = rec['body']

    registry.all_record_ids[pid] = path

    # E010: Required fields (only on profile files, not companions)
    if not is_companion:
        for field in REQUIRED_PERSON_FIELDS:
            if field not in meta or meta[field] == '':
                findings.append(Finding('E', 'E010', path,
                    f'Person profile missing required field: {field!r}'))

    # Collect token refs from body
    _collect_token_refs(rec['body'], path, registry)

    # E016: merged_into field
    merged_into = normalize_id(str(meta.get('merged_into', '')))
    if merged_into:
        registry.all_record_ids.setdefault(merged_into, path)

    return rec


def _process_source_file(path: Path, registry: Registry, findings: list[Finding]) -> None:
    """Process one source file into the registry, with file-level checks."""
    if is_template_file(path):
        return   # `_TEMPLATE.*` is a teaching template, not a record
    rec = read_record(path)
    meta = rec['meta']

    for code, msg in rec['parse_errors']:
        findings.append(Finding('E', code, path, msg))

    sid_raw = str(meta.get('id', ''))
    sid = normalize_id(sid_raw)
    id_placeholder = _is_placeholder_id(sid_raw)

    # E002: ID format. A template placeholder (`S-__________`) is handled as a
    # MISSING id below (auto-mintable), never as malformed - same doctrine as
    # the person walk.
    if sid_raw and not id_placeholder and not is_valid_id(sid_raw):
        findings.append(Finding('E', 'E002', path, f'Malformed ID: {sid_raw!r}'))
        return

    # E002 / filename grammar: {slug}_{S-id}.md
    stem = path.stem
    parsed = parse_filename(path)

    if id_placeholder:
        registry.placeholder_id_paths.add(path)
        if parsed:
            findings.append(Finding('E', 'E003', path,
                f'id: is still the template placeholder {sid_raw!r}, but the filename '
                f'already carries {fmt_id_display(parsed["id_str"])} - paste that code '
                f'into the id: line.'))
        sid_raw, sid = '', ''

    # A hand-authored, id-less record (no `id:`, no `_{S-id}` in the filename) is
    # a valid pre-machine state - auto-mintable, not an E002 grammar error.
    # GENERATED views and README.md files are id-less by design and are neither
    # mintable nor grammar-checked (same guard as the person walk).
    if not sid_raw and parsed is None:
        if not _never_mintable(path):
            registry.idless_records.append((path, 'S'))
        return
    if not _SOURCE_FILENAME_RE.fullmatch(stem):
        findings.append(Finding('E', 'E002', path,
            f'Source filename fails SPEC §13 grammar: {path.name}'))
    if parsed:
        file_id = normalize_id(parsed['id_str'])
        if sid and file_id != sid:
            findings.append(Finding('E', 'E003', path,
                f'Filename ID {file_id!r} ≠ record id {sid!r}'))

    if not sid:
        return

    # E001: duplicate source IDs
    if sid in registry.source_paths:
        findings.append(Finding('E', 'E001', path,
            f'Duplicate source ID {sid} (also in {registry.source_paths[sid]})'))

    registry.source_paths[sid] = path
    registry.source_meta[sid] = meta
    registry.all_record_ids[sid] = path

    # E010: Required fields
    for field in REQUIRED_SOURCE_FIELDS:
        if field not in meta or meta[field] == '':
            findings.append(Finding('E', 'E010', path,
                f'Source record missing required field: {field!r}'))

    # E005: a source's people: list must resolve, because index.py consumes it.
    # The field is now name-first-capable (`people: ["[[Ken Smith]]"]`): a bare
    # P-id that names no record is still the integrity error it always was, but a
    # name link is resolved against the alias map in Pass 2 (where every person is
    # known), so it is captured here rather than judged. An unresolved *name* is
    # never a hard error - it is forgiving input, not a typo'd ID.
    people_refs = link_field_refs(meta.get('people'))
    for ref in people_refs:
        if id_type_of(ref) == 'P' and not registry.has_person(normalize_id(ref)):
            findings.append(Finding('E', 'E005', path,
                f'Source people: references person {fmt_id_display(normalize_id(ref))} but no '
                'person record exists - create a stub with `fha stubs`, or fix the P-id.'))
    registry.source_links[sid] = {
        'people': people_refs,
        'places': link_field_refs(meta.get('places')),
    }

    # E007 / E017 / source_type check
    source_type = str(meta.get('source_type', ''))
    if source_type and source_type not in SOURCE_TYPES:
        findings.append(Finding('W', 'W109', path,
            format_source_type_error(source_type)))

    # E017: DNA sources must be restricted AND keep their raw files under
    # documents/dna/ (SPEC §8.5.5). The `restricted` marker is open (SPEC §19,
    # TOOLING §3): any non-empty value satisfies the rule - the plain boolean,
    # `restricted: dna`, or another free-text type - so only an absent/false
    # flag fails E017.
    if source_type == 'dna':
        if not _is_restricted(meta.get('restricted')):
            findings.append(Finding('E', 'E017', path,
                'DNA source must have restricted: true'))
        for f in (meta.get('files') or []):
            if not isinstance(f, dict):
                continue
            fpath = str(f.get('file', '')).replace('\\', '/')
            parts = [seg for seg in fpath.split('/') if seg]
            if len(parts) < 2 or parts[0] != 'documents' or parts[1] != 'dna':
                findings.append(Finding('E', 'E017', path,
                    f'DNA source file must be under documents/dna/: {fpath!r}'))

    # E014: source_date EDTF check (forgiving: loose-but-clear → W109 suggestion)
    _check_date_value(meta.get('source_date', ''), 'source_date', '', path, findings)

    # Claims
    claims = rec['claims']
    registry.source_claims[sid] = claims

    # W114: claims typed under ## Claims without the ```yaml fence. read_record
    # already reads them (so no data is lost - they index fine), but the fence is
    # the canonical form; offer to wrap it rather than leave the record untidy.
    if rec.get('unfenced_claims'):
        registry.unfenced_claim_sources[sid] = path
        findings.append(Finding('W', 'W114', path,
            'Claims under "## Claims" are not in a ```yaml fence. They still read '
            'correctly, but run `fha lint --fix-claims-fence` to wrap them in the '
            'canonical fenced block.'))

    for claim in claims:
        if not isinstance(claim, dict):
            continue

        cid_raw = str(claim.get('id', ''))
        cid = normalize_id(cid_raw)
        cid_placeholder = _is_placeholder_id(cid_raw)
        if cid_placeholder:
            # A template placeholder (`C-__________`) counts as no id at all -
            # never E002 - and --fix-ids replaces it in place.
            cid_raw, cid = '', ''

        # E002: Claim ID format
        if cid_raw and not is_valid_id(cid_raw):
            findings.append(Finding('E', 'E002', path,
                f'Malformed claim ID: {cid_raw!r}'))
            continue

        if not cid:
            if cid_placeholder:
                findings.append(Finding('E', 'E010', path,
                    f'Claim id is still the template placeholder '
                    f'(value={claim.get("value", "?")!r}) - run `fha lint --fix-ids` '
                    f'to replace it with a real code, or fill one in by hand '
                    f'(`fha id mint C`).'))
            else:
                findings.append(Finding('E', 'E010', path,
                    f'Claim missing required field: id (value={claim.get("value", "?")!r})'))
            continue

        # E001: duplicate claim IDs
        if cid in registry.claim_ids:
            findings.append(Finding('E', 'E001', path,
                f'Duplicate claim ID {cid} (also in source {registry.claim_ids[cid]})'))
        registry.claim_ids[cid] = sid
        registry.all_record_ids[cid] = path

        # E010: Required claim fields
        for field in REQUIRED_CLAIM_FIELDS:
            if field not in claim or claim[field] in (None, '', []):
                findings.append(Finding('E', 'E010', path,
                    f'Claim {cid} missing required field: {field!r}'))

        # E007: Claim type vocabulary
        claim_type = str(claim.get('type', ''))
        if claim_type and claim_type not in CLAIM_TYPES:
            findings.append(Finding('E', 'E007', path,
                f'Claim {cid} type {claim_type!r} is not a known claim type. '
                f'Use one of: {", ".join(sorted(CLAIM_TYPES))} '
                '(for anything else, use type: event or note with a free-text subtype:).'))

        # E006: accepted claim must have reviewed
        status = str(claim.get('status', ''))
        reviewed = str(claim.get('reviewed', ''))
        if status == 'accepted' and not reviewed:
            findings.append(Finding('E', 'E006', path,
                f'Accepted claim {cid} missing reviewed date'))

        # E019: status / confidence must come from their controlled vocabularies
        # (SPEC §8.1, §8.5). A typo'd value lints clean today but silently drops
        # the claim from accepted-claim rollups, so catch it.
        if status and status not in VALID_CLAIM_STATUS:
            findings.append(Finding('E', 'E019', path,
                f'Claim {cid} status {status!r} is not a valid review status. '
                f'Use one of: {", ".join(sorted(VALID_CLAIM_STATUS))}.'))
        conf_value = str(claim.get('confidence', ''))
        if conf_value and conf_value not in VALID_CONFIDENCE:
            findings.append(Finding('E', 'E019', path,
                f'Claim {cid} confidence {conf_value!r} is not valid - use high, medium, or low.'))

        # E014: Claim date EDTF check (forgiving: loose-but-clear → W109 suggestion)
        _check_date_value(claim.get('date', ''), 'date', f'Claim {cid}: ', path, findings)

        # E008: Significance override without reason
        if claim.get('significance') and not claim.get('significance_reason'):
            findings.append(Finding('E', 'E008', path,
                f'Claim {cid} has significance override but no significance_reason'))

        # E015: relationship claim must have roles
        if claim_type == 'relationship' and not claim.get('roles'):
            findings.append(Finding('E', 'E015', path,
                f'Claim {cid} (type: relationship) is missing its roles: field - add roles: '
                'naming each person\'s part (e.g. roles: [parent, child] or [spouse, spouse]).'))

        # W109: accepted claim missing notes when it's substantive OR a low-confidence vital
        sig = SIGNIFICANCE.get(claim_type, 'incidental')
        confidence = str(claim.get('confidence', ''))
        if status == 'accepted' and not claim.get('notes'):
            is_substantive = sig == 'substantive'
            is_low_confidence_vital = sig == 'vital' and confidence == 'low'
            if is_substantive or is_low_confidence_vital:
                findings.append(Finding('W', 'W109', path,
                    f'Claim {cid} ({claim_type}) is accepted but has no notes: context - '
                    'add a short notes: line explaining the evidence behind it.'))

    # E011: file inventory checks
    # In working-copy mode absent assets are assumed-present-elsewhere; skip.
    inventory_paths: set[str] = set()
    for f in (meta.get('files') or []):
        if not isinstance(f, dict):
            continue
        file_path_str = str(f.get('file', ''))
        file_status = str(f.get('status', ''))

        if not file_path_str:
            continue

        inventory_paths.add(_normalize_alias_path(file_path_str))
        if registry.is_working_copy:
            continue  # assets assumed-present on main machine; never flag as missing

        resolved = resolve_path(file_path_str, registry.fha_config, registry.archive_root)

        if not resolved.exists():
            if file_status == 'missing-fixture':
                # Allowed in example-archive/ and tests/fixtures/ as a W-level finding
                if registry.is_fixture:
                    pass   # allowed in fixture contexts, no warning needed
                else:
                    findings.append(Finding('E', 'E011', path,
                        f'status: missing-fixture is only allowed in example-archive/ or tests/fixtures/; '
                        f'file {file_path_str!r} is missing'))
            else:
                findings.append(Finding('E', 'E011', path,
                    f'Inventory file not found on disk: {file_path_str!r} - if the '
                    'file was moved within its asset folder, `fha reconcile` '
                    're-ties it automatically (preview with --dry-run); if the '
                    'roots: mapping in fha.yaml changed instead, see the W121 '
                    'finding on fha.yaml - reconcile cannot help there, because '
                    'nothing moved'))
    registry.source_inventory[sid] = inventory_paths

    # W102: suggested-claim backlog
    suggested = [c for c in claims if str(c.get('status', '')) == 'suggested']
    if suggested:
        findings.append(Finding('W', 'W102', path,
            f'{len(suggested)} suggested claim(s) awaiting review'))

    # Collect token refs
    _collect_token_refs(rec['body'], path, registry)


# ── Bracket and Ahnentafel checks (W103, W110, W119) ─────────────────────────

def _build_child_edges(registry: Registry) -> dict[str, dict[str, set[str]]]:
    """parent_pid → {child_pid: {subtype, …}} from accepted parent/child claims.

    A parent/child edge is identified by its `roles:` map (it names both a `child`
    and a `parent`), NOT by `subtype:` - `subtype` names the *nature* of the bond
    (biological, adoptive, step, …; SPEC §8.2). One pair may carry several natures
    across sources (a biological AND an adoptive edge - the co-valid NPE/adoption
    case), so each child maps to the SET of its edge natures. Scalars and lists are
    both accepted in either role (SPEC §8.4); a legacy `subtype: child-of` claim
    lands here too, recorded as the nature string it carries.
    """
    edges: dict[str, dict[str, set[str]]] = {}
    for claims in registry.source_claims.values():
        for claim in claims:
            if (not isinstance(claim, dict)
                    or str(claim.get('status', '')) != 'accepted'
                    or claim.get('type') != 'relationship'):
                continue
            # Role values resolve like persons: entries (wrapped IDs and
            # unambiguous names both land on their P-id; registry.alias_map is
            # populated before any Pass 2 caller runs), so brackets/Ahnentafel
            # derive the same edges the index's relationships table does.
            child_ids = _role_pids(claim, 'child', registry.alias_map)
            parent_ids = _role_pids(claim, 'parent', registry.alias_map)
            if not child_ids or not parent_ids:
                continue
            subtype = str(claim.get('subtype', '')).strip().lower()
            for cpid in sorted(child_ids):
                for ppid in sorted(parent_ids):
                    edges.setdefault(ppid, {}).setdefault(cpid, set()).add(subtype)
    return edges


def _build_children_of(registry: Registry, genetic_only: bool = False) -> dict[str, set[str]]:
    """parent_pid → {child_pids} from accepted parent/child relationship claims.

    With `genetic_only`, an edge survives only if at least one of its natures is
    genetic (SPEC §12.2) - so the Ahnentafel NUMBERING walk skips adoptive, step,
    foster, guardian, and social parents, while the bracket and relationship views
    (genetic_only=False) still show every child. An unset, legacy (`child-of`), or
    unrecognised nature defaults to genetic, so a legacy archive numbers exactly as
    before. Bloodline filtering changes numbering only; every parent edge stays
    visible elsewhere.
    """
    children_of: dict[str, set[str]] = {}
    for ppid, kids in _build_child_edges(registry).items():
        for cpid, natures in kids.items():
            if genetic_only and not any(is_genetic_parent_subtype(s) for s in natures):
                continue
            children_of.setdefault(ppid, set()).add(cpid)
    return children_of


def _check_bracket_lists(registry: Registry, findings: list[Finding]) -> None:
    """W103: stale couple-folder bracket lists.

    For each digit-prefixed directory under people/ (excluding stubs/connections),
    derives the expected bracket list from accepted parent/child relationship
    claims (by their roles: map) whose parent names a person residing in that
    folder, marking a child who joined other than by birth (`Ruth (adopted)`).
    ALL children appear - direct-line children with their own folder included -
    mirroring the bracket convention documented in TOOLING §7.

    WHY ALL CHILDREN: see _check_w103_brackets in views.py.  Same invariant, same
    source data, different backend (in-memory registry instead of SQLite).
    """
    child_edges = _build_child_edges(registry)
    children_of = {ppid: set(kids) for ppid, kids in child_edges.items()}

    # Build pid → folder name for all persons with profile files in people/
    pid_to_folder: dict[str, str] = {}
    people_dir = registry.archive_root / 'people'
    excluded = {'stubs', 'connections'}
    for pid, paths in registry.person_profile_paths.items():
        for p in paths:
            if (p.parent.parent == people_dir
                    and p.parent.name.lower() not in excluded
                    and re.match(r'^\d', p.parent.name)):
                pid_to_folder[pid] = p.parent.name
                break

    # Invert: folder name → {person_ids in that folder}
    folder_to_pids: dict[str, set[str]] = {}
    for pid, fname in pid_to_folder.items():
        folder_to_pids.setdefault(fname, set()).add(pid)

    # Check each couple folder
    for folder_name, folder_pids in sorted(folder_to_pids.items()):
        # Current bracket names from the folder name
        m = re.search(r'\[([^\]]*)\]', folder_name)
        current_names = (
            [n.strip() for n in m.group(1).split('+') if n.strip()]
            if m else []
        )

        # Derive expected children names.  Mirror views.py _check_w103_brackets
        # exactly: drop stray occupants (a folder member who is a child of another
        # member) from the PARENT set, then take all children of the remaining
        # members.  Subtracting a stray's children instead would also drop a
        # grandchild that ALSO has a direct child-of edge to a folder parent -
        # views keeps that child, so lint must too.
        member_children = {
            cpid
            for ppid in folder_pids
            for cpid in children_of.get(ppid, set())
        }
        stray_pids = member_children & folder_pids
        parents = folder_pids - stray_pids

        # All children of the (non-stray) folder parents, each marked with its
        # nature relative to THIS couple: a child with at least one genetic edge to
        # a folder parent reads as a birth child (bare given name); one joined only
        # by a social/legal bond reads `Given (adopted)`. Mirrors
        # views._check_w103_brackets so both backends derive identical lists.
        child_natures: dict[str, set[str]] = {}
        for ppid in parents:
            for cpid, natures in child_edges.get(ppid, {}).items():
                child_natures.setdefault(cpid, set()).update(natures)

        derived_entries = []
        for cpid, natures in child_natures.items():
            name = str(registry.person_meta.get(cpid, {}).get('name', ''))
            if not name:
                continue
            label = None
            if not any(is_genetic_parent_subtype(s) for s in natures):
                for s in sorted(natures):
                    label = nonbirth_bracket_label(s)
                    if label:
                        break
            derived_entries.append(format_bracket_child(name.split()[0], label))
        derived_names = sorted(derived_entries)

        # The same pass also proposes the missing `+ second spouse` half of the
        # base name (add-only, never rewrites, never guesses) - the shared
        # `_lib.spouse_extended_base` rule, mirroring views so both backends
        # derive identical target names.
        base_name = re.sub(r'\s*\[[^\]]*\]', '', folder_name).rstrip()
        partner_names = {
            pid: str(registry.person_meta.get(pid, {}).get('name', '') or '')
            for pid in parents
        }
        new_base, other_name = spouse_extended_base(
            base_name, sorted(parents), partner_names)

        bracket_stale = sorted(current_names) != sorted(derived_names)
        if bracket_stale or new_base != base_name:
            parts = []
            if new_base != base_name:
                parts.append(
                    f'couple folder names only one partner - add {other_name}')
            if bracket_stale:
                parts.append(
                    f'stale bracket list [{" + ".join(sorted(current_names))}] '
                    f'-> [{" + ".join(derived_names)}]')
            findings.append(Finding('W', 'W103',
                people_dir / folder_name,
                '; '.join(parts) + '; run `fha views brackets --fix` to update'))


def _build_ahnentafel_lint(
    root_pid: str, children_of: dict[str, set[str]], registry: Registry,
    sex_gaps: list[dict] | None = None,
) -> dict[str, int]:
    """BFS from root_pid → {person_id: Ahnentafel position} using in-memory data.

    Same algorithm as _build_ahnentafel_map in views.py, but works from the
    in-memory registry rather than the SQLite relationships table.  Parents are
    determined by inverting children_of: a person P is a parent of Q if Q is
    in children_of[P].

    `sex_gaps`, when a list is passed, collects the W120 set exactly as
    `_lib.build_ahnentafel_map` does: single-resolved-parent placements where
    that parent's `sex:` is not a recorded M/F, appended as {'pid', 'pos'} -
    the slot was a default, not a derivation, and the resulting folder numbers
    look confident while being a guess W110 can never catch.

    Determinism on same-sex / unknown pairs: lex-first P-id takes the even slot.
    With three or more genetic contributors (assisted reproduction), the two
    Ahnentafel slots are filled by the documented TOOLING 7 rule, not by the two
    lowest-P-id parents - see the multi-parent branch below. This must stay in
    step with build_ahnentafel_map in _lib.py, which does the same selection from
    the SQLite relationships table.
    """
    # Build child_pid → {parent_pids} from children_of for quick upward lookup
    parents_of: dict[str, set[str]] = {}
    for ppid, cset in children_of.items():
        for cpid in cset:
            parents_of.setdefault(cpid, set()).add(ppid)

    pid_to_pos: dict[str, int] = {root_pid: 1}
    queue: list[tuple[str, int]] = [(root_pid, 1)]

    while queue:
        pid, n = queue.pop(0)
        parent_pids = sorted(parents_of.get(pid, set()))
        if not parent_pids:
            continue

        if len(parent_pids) == 1:
            pp = parent_pids[0]
            sex = str(registry.person_meta.get(pp, {}).get('sex', 'U') or 'U')
            pos = 2 * n if sex != 'F' else 2 * n + 1
            if pp not in pid_to_pos:
                pid_to_pos[pp] = pos
                queue.append((pp, pos))
                if sex_gaps is not None and sex_slot_is_defaulted(sex):
                    sex_gaps.append({'pid': pp, 'pos': pos, 'sex': sex})
        else:
            # Two or more genetic parent edges - assisted reproduction (e.g. a
            # donor-egg mother, a surrogate-genetic mother, and a donor-sperm
            # father). The two-slot Ahnentafel model numbers exactly one
            # contributor per slot (father 2n, mother 2n+1). Taking the two
            # lowest-P-id parents could seat two female contributors in both
            # slots and drop the sperm contributor, so apply the TOOLING 7 rule:
            # rank each contributor for each slot by (sex-fitness, P-id) - father
            # prefers M, then U, then F; mother prefers F, then U, then M; P-id
            # breaks ties. Extra contributors beyond the two slots are left
            # unnumbered (the bracket list shows them). This reproduces the old
            # two-parent behaviour for every sex combination while staying
            # deterministic for three or more, matching _lib.build_ahnentafel_map.
            def _sex_of(pp: str) -> str:
                return str(registry.person_meta.get(pp, {}).get('sex', 'U') or 'U')

            def _father_rank(pp: str) -> tuple[int, str]:
                return ({'M': 0, 'U': 1, 'F': 2}.get(_sex_of(pp), 1), pp)

            def _mother_rank(pp: str) -> tuple[int, str]:
                return ({'F': 0, 'U': 1, 'M': 2}.get(_sex_of(pp), 1), pp)

            father = min(parent_pids, key=_father_rank)
            mother = min((pp for pp in parent_pids if pp != father),
                         key=_mother_rank)
            for pp, pos in [(father, 2 * n), (mother, 2 * n + 1)]:
                if pp not in pid_to_pos:
                    pid_to_pos[pp] = pos
                    queue.append((pp, pos))

    return pid_to_pos


def _check_ahnentafel_placement(registry: Registry, findings: list[Finding]) -> dict[str, int]:
    """W110: direct-line person files in the wrong couple folder.

    Requires root_person in fha.yaml.  Builds the Ahnentafel map from the
    in-memory registry, then verifies every direct-line person's profile files
    live in the couple folder whose numeric prefix equals their expected position
    (or position−1 if they hold the odd/mother slot).

    Also emits W120 for every placement the map builder made by DEFAULT rather
    than derivation: a lone linked parent with no recorded `sex:` silently
    takes the father (even) slot, so the folder numbers above them look
    confirmed while being a guess (the views twin reports the same set).

    Skips persons in people/connections/ or people/stubs/.

    Returns the derived {P-id: position} map (empty when root_person is absent
    or unresolvable) so the W119 direct-line-stub check right after it reads
    the same derivation instead of running the BFS twice.
    """
    root_person_raw = registry.fha_config.get('root_person')
    if not root_person_raw:
        return {}

    root_pid = normalize_id(str(root_person_raw))
    if not registry.has_person(root_pid):
        findings.append(Finding('W', 'W110', registry.archive_root / 'fha.yaml',
            f'root_person {root_pid!r} has no person record - '
            'Ahnentafel placement checks (W110) skipped; '
            'fix root_person in fha.yaml or run fha stubs'))
        return {}
    # Ahnentafel numbering follows only the genetic pedigree (SPEC §12.2); social
    # and legal parent edges are shown in the bracket list but never numbered.
    children_of = _build_children_of(registry, genetic_only=True)
    sex_gaps: list[dict] = []
    pid_to_pos = _build_ahnentafel_lint(root_pid, children_of, registry, sex_gaps)

    # W120: a lone linked parent with no recorded sex took the father (even)
    # slot by DEFAULT - a normal early-research state (SPEC never requires
    # `sex:` up front), but the derived folder numbers above them look
    # confident while being a guess, and W110 can never catch it because the
    # folders match their own flawed derivation. Report-only: the fix is a
    # fact about a person, which only the human can record.
    for gap in sex_gaps:
        pid = gap['pid']
        name = str(registry.person_meta.get(pid, {}).get('name', pid))
        profile_paths = registry.person_profile_paths.get(pid, [])
        where = (profile_paths[0] if profile_paths
                 else registry.archive_root / 'fha.yaml')
        findings.append(Finding('W', 'W120', where, format_w120_message(
            name, gap['pos'], gap.get('sex'), '`fha views brackets`')))

    people_dir = registry.archive_root / 'people'
    excluded = {'stubs', 'connections'}

    for pid, pos in pid_to_pos.items():
        if pos < 2:
            continue
        expected_prefix = pos if pos % 2 == 0 else pos - 1

        all_paths = (
            registry.person_profile_paths.get(pid, [])
            + registry.person_companion_paths.get(pid, [])
        )
        for p in all_paths:
            folder_name = p.parent.name
            if folder_name.lower() in excluded:
                continue
            if p.parent.parent != people_dir:
                continue
            m = re.match(r'^(\d+)', folder_name)
            if not m:
                name = str(registry.person_meta.get(pid, {}).get('name', pid))
                findings.append(Finding('W', 'W110', p,
                    f'{name} (Ahnentafel {pos}) is in folder {folder_name!r} with no '
                    f'numeric prefix, expected prefix {expected_prefix}; '
                    f'run `fha views brackets --fix` to correct'))
                continue
            actual_prefix = int(m.group(1))
            # Canonical placement: digit prefix followed by a space.
            # Suffix folders like '040b …' share the numeric prefix but are never
            # the correct location for a direct-line person file.
            if re.match(r'^(\d+) ', folder_name) and actual_prefix == expected_prefix:
                continue
            name = str(registry.person_meta.get(pid, {}).get('name', pid))
            findings.append(Finding('W', 'W110', p,
                f'{name} (Ahnentafel {pos}) is in folder prefix {actual_prefix}, '
                f'expected prefix {expected_prefix}; '
                f'run `fha views brackets --fix` to correct'))

    return pid_to_pos


def _check_direct_line_stubs(
    registry: Registry, findings: list[Finding], pid_to_pos: dict[str, int]
) -> None:
    """W119: direct-line ancestors still filed as stubs - a lead, never a defect.

    The mirror of `fha views brackets` check 4 (the established
    lint-detects / brackets-fixes split W103 and W110 already follow): any
    person with a DERIVED Ahnentafel position >= 2 whose record is
    `tier: stub` or still lives under people/stubs/. These are exactly the
    people the W110 placement machinery deliberately skips, and on a live
    archive this fires for every not-yet-curated ancestor at once - which is
    the POINT (it is the research-lead surface `fha report` §7b narrates),
    so the severity is warning, permanently: a stub is a legitimate state
    (SPEC §4) and the graduation is always the human's explicit act. The fix
    named is the previewed batch (`fha views brackets --fix-promote`) or the
    single-person verb; lint itself never promotes anyone.

    `pid_to_pos` comes from `_check_ahnentafel_placement` (empty when
    root_person is absent/unresolvable - that condition already carries its
    own W110 note, so this check stays silent rather than doubling it).
    """
    for pid, pos in sorted(pid_to_pos.items(), key=lambda kv: kv[1]):
        if pos < 2:
            continue
        meta = registry.person_meta.get(pid)
        if meta is None:
            continue   # referenced but recordless - the E005 / fha stubs set
        if str(meta.get('status', '')) == 'merged':
            continue
        profile_paths = registry.person_profile_paths.get(pid, [])
        if not profile_paths:
            continue
        profile = profile_paths[0]
        in_stubs = profile.parent.name.lower() == 'stubs'
        is_stub_tier = str(meta.get('tier') or 'stub').strip().lower() != 'curated'
        if not (is_stub_tier or in_stubs):
            continue
        name = str(meta.get('name', pid))
        display_pid = pid[0].upper() + pid[1:]
        findings.append(Finding('W', 'W119', profile,
            f'{name} (Ahnentafel {pos}) is a direct-line ancestor still filed '
            'as a stub - a research lead, not a defect; when you are ready to '
            'curate them, run `fha views brackets --fix-promote` (previewed, '
            f'confirmed) or `fha person promote {display_pid}`'))


# ── Cross-file checks ─────────────────────────────────────────────────────────

def _is_restricted(value) -> bool:
    """True when a `restricted:` value marks the record as restricted.

    The marker is open (SPEC §19): the plain boolean `true`, or any free-text
    type (`dna`, `by-request`, `deadname`, …), all mean restricted. Only an
    absent or explicitly-false flag is unrestricted. `read_record` coerces
    booleans to the strings `'true'`/`'false'`, so both forms are handled."""
    if value in (None, False, '', 'false'):
        return False
    return True


def _variant_values(variants) -> list[str]:
    """Flatten a `name_variants:` list to its display strings.

    A variant is normally a bare string, but a private prior name (a deadname,
    SPEC §9/§18) is written as a `{value:, restricted: true}` mapping so it can
    be redacted on export. Either way the *value* is what resolves through the
    alias surface - so a `[[prior name]]` link still finds the person (no E004)
    and the clash check still sees the name. The `restricted` flag matters only
    to the exporters; here we want the plain string."""
    out: list[str] = []
    for v in variants or []:
        if isinstance(v, dict):
            value = v.get('value')
            if value:
                out.append(str(value))
        elif v:
            out.append(str(v))
    return out


def _alias_records(registry: Registry) -> list[dict]:
    """Assemble the records `build_alias_map`/`alias_clashes` operate on, from
    everything Pass 1 collected: persons (id + name + variants + stems), sources
    (id + stems), and the bare IDs of places/hypotheses (so a stem colliding with
    one is caught). Place names are not available to lint's on-disk registry, so
    place-name clashes are out of scope here (the index carries those).

    `status` rides along so `_lib._record_alias_strings` can hold a merged
    tombstone to its bare P-id: the tombstone keeps its `name:` for human
    readability, but the merge folded that name into the survivor, and
    registering it here too would clash the folded name out of the resolve
    map (and mint a fresh W112 for every completed merge)."""
    records: list[dict] = []
    for pid, meta in registry.person_meta.items():
        records.append({
            'id': pid,
            'name': meta.get('name'),
            'name_variants': _variant_values(meta.get('name_variants')),
            'aliases': meta.get('aliases') or [],
            'status': meta.get('status'),
        })
    for sid, meta in registry.source_meta.items():
        records.append({'id': sid, 'aliases': meta.get('aliases') or []})
    for rid in (registry.place_ids | registry.hypothesis_ids):
        records.append({'id': rid})
    return records


def _self_alias_ok(meta: dict, cid: str) -> bool:
    """True if a record either declares no `aliases:` (hasn't opted into the
    layer - not nagged) or its `aliases:` already includes its own canonical ID.

    Scoped this way on purpose: pre-alias records simply have no `aliases:` field
    and are left alone (forgiving, AGENTS.md), while a record that DID add aliases
    must carry the self-ID - the one line that makes `[[S-…]]` click through."""
    aliases = meta.get('aliases')
    if not aliases:
        return True
    entries = aliases if isinstance(aliases, list) else [aliases]
    present = {strip_link_wrapper(str(a)).lower() for a in entries}
    return normalize_id(cid) in present


def _alias_checks(registry: Registry, findings: list[Finding]) -> None:
    """The alias-layer maintenance + integrity checks (Pass 2).

      - W111 self-alias: a record that uses `aliases:` but omits its own ID.
      - W112 latent clash: one string names ≥2 records, but nothing links by it
        yet - normal in genealogy (same-name people), just a heads-up.
      - W113 active clash: a real `[[John Smith]]` (prose) or `people: [[John
        Smith]]` (frontmatter) link uses an ambiguous name - must be pinned to an
        ID. The system never guesses which record; the human (or `fha
        normalize-links`) chooses.
    """
    # W111 - self-alias present where the record opted into the alias layer.
    for pid, meta in registry.person_meta.items():
        if not _self_alias_ok(meta, pid):
            path = registry.person_profile_paths.get(pid, [Path(pid)])[0]
            findings.append(Finding('W', 'W111', path,
                f"aliases: is missing this record's own ID {fmt_id_display(pid)} - add it so "
                f'[[{fmt_id_display(pid)}]] resolves in Obsidian (run `fha normalize-links`)'))
    for sid, meta in registry.source_meta.items():
        if not _self_alias_ok(meta, sid):
            path = registry.source_paths.get(sid, Path(sid))
            findings.append(Finding('W', 'W111', path,
                f"aliases: is missing this record's own ID {fmt_id_display(sid)} - add it so "
                f'[[{fmt_id_display(sid)}]] resolves in Obsidian (run `fha normalize-links`)'))

    # W112 / W113 - name/stem clashes.
    clashes = alias_clashes(_alias_records(registry))
    for name, ids in sorted(clashes.items()):
        # Active sites: a prose name-wikilink, or a frontmatter people:/places:
        # entry that uses the ambiguous string.
        active_sites: list[tuple[Path, int | None]] = list(registry.name_link_refs.get(name, []))
        for sid, links in registry.source_links.items():
            for field in ('people', 'places'):
                for ref in links.get(field, []):
                    if strip_link_wrapper(ref).lower() == name:
                        active_sites.append((registry.source_paths.get(sid, Path(sid)), None))
        id_list = ', '.join(fmt_id_display(i) for i in ids)
        if active_sites:
            site_path, _line = active_sites[0]
            findings.append(Finding('W', 'W113', site_path,
                f"'{name}' is ambiguous - it names {len(ids)} records ({id_list}); a link uses "
                f'it but the system never guesses which. Pin it to an ID (run `fha normalize-links`).'))
        else:
            anchor = registry.all_record_ids.get(ids[0], Path(ids[0]))
            findings.append(Finding('W', 'W112', anchor,
                f"'{name}' names {len(ids)} records ({id_list}); any link to it must be pinned "
                'to an ID (it cannot resolve by name alone).'))


# ── Relationship reconciliation (W115 / W116) ─────────────────────────────────
#
# The person-doc `relationships:` block (SPEC §9) is the human-writable surface
# where relationship claims are applied to the lives they concern. A SOURCED
# entry (it carries `claim:`/`source:`) must reconcile against an accepted
# `relationship` claim - same pair, same role, same nature (subtype). An entry
# that cites a missing claim, or whose nature disagrees with the claim, is W115.
# A sourced edge recorded on one person but not mirrored on the other is W116;
# `fha lint --fix-reciprocal` offers to append the missing mirror. UNSOURCED
# beliefs (no link, or `status: hypothesis`) are never findings - they land on
# the informational needs-sourcing backlog, exactly like a provisional birth.

# entry `type` (the OTHER person's role) → (owner_role, other_role) in the claim.
# A `type: parent` entry on P's record means "the other person is P's parent",
# so P is the child and the other is the parent in the backing claim.
_EDGE_ROLE_MAP = {
    'parent':   ('child', 'parent'),
    'child':    ('parent', 'child'),
    'spouse':   ('spouse', 'spouse'),
    'enslaver': ('enslaved', 'enslaver'),
    'enslaved': ('enslaver', 'enslaved'),
    'employer': ('employee', 'employer'),
    'employee': ('employer', 'employee'),
}
# entry `type` → the reciprocal `type` the mirror entry on the other person uses.
_RECIPROCAL_ROLE = {
    'parent': 'child', 'child': 'parent', 'spouse': 'spouse', 'sibling': 'sibling',
    'enslaver': 'enslaved', 'enslaved': 'enslaver',
    'employer': 'employee', 'employee': 'employer',
}


def _claim_by_id(registry: Registry, cid: str) -> dict | None:
    """Return the claim dict for a C-id, or None. Claims live under their source,
    so this resolves the C-id → S-id index then scans that source's claims."""
    sid = registry.claim_ids.get(cid)
    if not sid:
        return None
    for claim in registry.source_claims.get(sid, []):
        if isinstance(claim, dict) and normalize_id(str(claim.get('id', ''))) == cid:
            return claim
    return None


def _role_pids(claim: dict, role: str, alias_map: dict[str, str] | None = None) -> set[str]:
    """Normalised P-ids filling one `roles:` key (scalar or list both accepted).

    Values resolve like persons: entries (`roles: {child: "[[Sam Rivera]]"}` is
    the quickstart's form), so role matching agrees with _claim_person_ids.

    A hand-written `roles:` is not always the mapping the schema asks for -
    E015's own message suggests the shorthand `roles: [parent, child]`, and a
    list carries no person to resolve. Treat any non-mapping as "no roles
    given" rather than letting `.get` raise: a lint pass must never hand the
    human a traceback over a hand-edit it can simply read as empty."""
    roles = claim.get('roles')
    val = roles.get(role) if isinstance(roles, dict) else None
    out: set[str] = set()
    for ref in link_field_refs(val):
        pid = _resolve_person_ref(ref, alias_map)
        if pid:
            out.add(pid)
    return out


def _entry_subtype(entry: dict) -> str:
    """The nature an entry asserts. An unqualified parent/child edge is
    `biological` by default (SPEC §8.2); other types have no default."""
    st = str(entry.get('subtype', '')).strip().lower()
    if st:
        return st
    role = str(entry.get('type', '')).strip().lower()
    return 'biological' if role in ('parent', 'child') else ''


def _claim_subtype_norm(claim: dict) -> str:
    """The claim's nature, normalised for comparison. A parent/child claim with no
    subtype - or the legacy role-marker `child-of` - reads as `biological`."""
    st = str(claim.get('subtype', '')).strip().lower()
    if st and st not in ('child-of', 'spouse-of'):
        return st
    roles = claim.get('roles') or {}
    if roles.get('child') and roles.get('parent'):
        return 'biological'
    return ''


def _claim_roles_by_person(
    claim: dict, alias_map: dict[str, str] | None = None,
) -> dict[str, str]:
    """Each named person's role on this claim, read the way the index stores it.

    `fha index` fills `claim_persons.role` by walking the `roles:` map in the
    order it was written and taking the FIRST key whose people include this
    person (index.py, the claim_persons insert). Lint mirrors that exactly:
    the two must not disagree about what a claim says, or a warning here
    describes a tree the indexer never built.

    Roles the claim never mentions are simply absent - a person with no role is
    a person the claim said nothing about, which is a different thing from a
    person it placed somewhere other than the couple.
    """
    roles = claim.get('roles')
    if not isinstance(roles, dict):
        return {}   # `roles: [parent, child]` names nobody; not a crash, just empty
    out: dict[str, str] = {}
    for role_name in roles:
        norm = str(role_name).strip().lower()
        for pid in _role_pids(claim, role_name, alias_map):
            out.setdefault(pid, norm)
    return out


def _claim_spouse_pids(
    claim: dict, alias_map: dict[str, str] | None = None,
) -> set[str]:
    """Who a couple claim says married each other, read the way the index reads it.

    A couple claim is a `marriage`, a `divorce`, or the legacy `relationship` +
    `subtype: spouse-of` - the three shapes that put spouse edges in the tree.

    A marriage certificate names the couple AND both sets of parents, and
    listing all six in `persons:` is correct - `persons:` is who the claim is
    about (SPEC §8.3) - so only `roles: spouse:` says which two of them married.
    `_lib.spouse_parties` is that rule, shared with `fha index` and
    `fha gedcom`; this wrapper just hands it the claim's people paired with
    their roles, resolved through the alias map so a name-linked entry counts
    like a bare P-id.

    Lint must read it identically or it contradicts the tools: treating every
    person on the certificate as a spouse makes W115 demand `relationships:`
    spouse entries between the bride and her father-in-law - marriages the same
    claim excludes and the indexer correctly refuses to derive. A lint warning
    whose repair corrupts the tree is worse than no warning.

    Only people actually named in `persons:` can carry a role, matching how the
    index builds `claim_persons`: a `roles:` entry naming somebody left out of
    `persons:` is a broken map, not a secret extra spouse.

    Every role is passed through, not just `spouse`. The rule's two-person
    fallback turns on whether the OTHER person carries an explicit role, so a
    wrapper that flattened every non-spouse role to "no role" would have lint
    reporting a couple the index refuses to derive.
    """
    named = _claim_person_ids(claim, alias_map)
    by_person = _claim_roles_by_person(claim, alias_map)
    return set(spouse_parties((pid, by_person.get(pid)) for pid in named))


def _claim_parentage_pids(
    claim: dict, alias_map: dict[str, str] | None = None,
) -> tuple[set[str], set[str]]:
    """Who a claim says was born and to whom, read the way the index reads it.

    Returns `(children, parents)`, both empty unless the claim answered both
    halves - `_lib.parentage_parties` is the rule, shared with `fha index` so
    W126 can never report a silence the indexer does not actually keep, nor
    stay quiet about one it does. This wrapper only hands that rule the claim's
    people paired with their roles, resolved through the alias map so a
    name-linked entry counts like a bare P-id.

    Only people actually named in `persons:` can carry a role, matching how the
    index builds `claim_persons`: a `roles:` entry naming somebody left out of
    `persons:` is a broken map, not a secret extra parent.
    """
    named = _claim_person_ids(claim, alias_map)
    by_person = _claim_roles_by_person(claim, alias_map)
    children, parents = parentage_parties(
        (pid, by_person.get(pid)) for pid in named)
    return set(children), set(parents)


def _claim_backs_edge(
    claim: dict, owner_pid: str, other_pid: str | None, role: str,
    alias_map: dict[str, str] | None = None,
) -> bool:
    """True if `claim` is an accepted relationship/marriage/birth claim that
    records the edge a person-doc entry asserts. When `other_pid` is None (the
    `to:` name has no minted record yet) only the owner's side is checked, so a
    forgiving name never produces a false reconciliation failure. `alias_map`
    lets name-linked persons:/roles: entries back an edge the same as bare
    P-ids.

    Lint reads the same claim types the indexer derives edges from, or it
    contradicts the tools: a birth claim whose `roles:` map names a child and a
    parent now puts that bond in the tree (#71), so a person-doc entry citing
    it is reconciled, not drifting. Telling the human their correctly-written
    record disagrees with a claim the archive itself is reading would send them
    to repair something that is not broken."""
    if str(claim.get('status', '')) != 'accepted':
        return False
    ctype = str(claim.get('type', ''))
    if role == 'spouse' and ctype == 'marriage':
        # Not everyone on a certificate married everyone else on it: the
        # claim's roles: map scopes the couple (_claim_spouse_pids).
        persons = _claim_spouse_pids(claim, alias_map)
        return owner_pid in persons and (other_pid is None or other_pid in persons)
    if role in ('parent', 'child') and ctype == 'birth':
        # Scoped by the derivation rule, not by the presence of a roles: key -
        # a birth claim the indexer derives nothing from backs nothing here.
        children, parents = _claim_parentage_pids(claim, alias_map)
        if not (children and parents):
            return False
        # An entry of `type: parent` says the owner HAS a parent, so the owner
        # is the child on the claim - the same inversion _EDGE_ROLE_MAP encodes.
        owner_side, other_side = ((children, parents) if role == 'parent'
                                  else (parents, children))
        return (owner_pid in owner_side
                and (other_pid is None or other_pid in other_side))
    pair = _EDGE_ROLE_MAP.get(role)
    if ctype != 'relationship' or not pair:
        return False
    owner_role, other_role = pair
    if owner_pid not in _role_pids(claim, owner_role, alias_map):
        return False
    if other_pid is not None and other_pid not in _role_pids(claim, other_role, alias_map):
        return False
    return True


def _person_reconcilable_role_label(
    claim: dict, pid: str, alias_map: dict[str, str] | None = None,
) -> str | None:
    """For the reverse check: the entry `type` this person's block would use to
    apply `claim`, or None if the claim isn't a kin edge naming them. Limited to
    parent/child/spouse so social and power ties never over-flag."""
    ctype = str(claim.get('type', ''))
    if ctype == 'marriage':
        # Only the couple the claim marries owes a spouse entry - a parent
        # named on the certificate owes nothing (_claim_spouse_pids).
        return 'spouse' if pid in _claim_spouse_pids(claim, alias_map) else None
    if ctype == 'birth':
        # The mirror of the forward check: a birth claim that derives parentage
        # is a kin claim, so an opted-in block that omits it is incomplete for
        # the same reason it would be if the bond were written as a
        # relationship claim. A birth claim the indexer derives nothing from
        # owes nothing - only the child and the parents it actually named do.
        children, parents = _claim_parentage_pids(claim, alias_map)
        if not (children and parents):
            return None
        if pid in children:
            return 'parent'     # the person was born → their entry names a parent
        if pid in parents:
            return 'child'
        return None
    if ctype != 'relationship':
        return None
    if pid in _role_pids(claim, 'child', alias_map):
        return 'parent'     # the person is a child → their entry names a parent
    if pid in _role_pids(claim, 'parent', alias_map):
        return 'child'
    if pid in _role_pids(claim, 'spouse', alias_map):
        return 'spouse'
    return None


def _profile_path_for(registry: Registry, pid: str) -> Path:
    """Best on-disk path to attach a finding to for a person."""
    paths = registry.person_profile_paths.get(pid)
    if paths:
        return paths[0]
    return registry.all_record_ids.get(pid, Path(pid))


def _check_reciprocity(
    registry: Registry, findings: list[Finding],
    owner_pid: str, other_pid: str, role: str, claim: dict, alias_map: dict[str, str],
) -> None:
    """W116: a sourced edge on owner_pid should be mirrored on other_pid, pointing
    at the same claim. Offers `--fix-reciprocal` rather than demanding both ends."""
    mirror_role = _RECIPROCAL_ROLE.get(role)
    if not mirror_role:
        return     # a tie we can't mirror automatically (e.g. member-of an org)
    cid = normalize_id(str(claim.get('id', '')))
    other_meta = registry.person_meta.get(other_pid) or {}
    for e in (other_meta.get('relationships') or []):
        if not isinstance(e, dict):
            continue
        e_claim = normalize_id(strip_link_wrapper(str(e.get('claim', '')))) if e.get('claim') else ''
        if cid and e_claim == cid:
            return     # mirror present, same claim
        e_to = resolve_ref(str(e.get('to', '')), alias_map) if e.get('to') else None
        e_to = normalize_id(e_to) if e_to else None
        if e_to == owner_pid and str(e.get('type', '')).strip().lower() == mirror_role:
            return     # mirror present, matched by person + role
    owner_name = str(registry.person_meta.get(owner_pid, {}).get('name') or fmt_id_display(owner_pid))
    findings.append(Finding('W', 'W116', _profile_path_for(registry, other_pid),
        f"{fmt_id_display(other_pid)} is missing the reciprocal '{mirror_role}' edge to "
        f"{owner_name} - it is recorded on {fmt_id_display(owner_pid)}'s relationships: "
        f"(claim {fmt_id_display(cid)}). Run `fha lint --fix-reciprocal` to add the mirror "
        f"(preview with --dry-run)."))
    registry.missing_mirrors.append({
        'other_pid': other_pid,
        'owner_pid': owner_pid,
        'mirror_role': mirror_role,
        'subtype': _claim_subtype_norm(claim),
        'claim_id': cid,
    })


def _check_relationships_reconciliation(
    registry: Registry, findings: list[Finding], alias_map: dict[str, str],
) -> None:
    """W115/W116 over every person-doc `relationships:` block (SPEC §9).

    Only persons who opted into the block are checked - like W111's self-alias,
    a record with no `relationships:` is left alone. For each SOURCED entry: the
    backing claim must exist and record this edge (else W115), its nature must
    match (else W115), and the other person should mirror it (else W116). The
    reverse direction (an accepted kin claim naming this person but absent from
    their block) is also W115, so an opted-in block stays complete.

    A kin claim here is whatever the indexer derives a kin edge from: a
    `relationship` claim, a `marriage` claim, and - since #71 - a `birth` claim
    whose `roles:` map names a child and a parent. Both directions read the
    same list, so an entry citing a birth claim reconciles and a birth claim
    the block omits is reported, exactly as for the other two."""
    for pid in sorted(registry.person_meta):
        block = registry.person_meta[pid].get('relationships')
        if not isinstance(block, list) or not block:
            continue
        profile_path = _profile_path_for(registry, pid)
        referenced_cids: set[str] = set()

        for entry in block:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get('type', '')).strip().lower()
            other_pid = resolve_ref(str(entry.get('to', '')), alias_map) if entry.get('to') else None
            other_pid = normalize_id(other_pid) if other_pid else None
            status = str(entry.get('status', '')).strip().lower()
            is_sourced = bool(entry.get('claim') or entry.get('source')) and status != 'hypothesis'
            if not is_sourced:
                continue   # an unsourced belief → needs-sourcing backlog, not a finding

            matched: dict | None = None
            if entry.get('claim'):
                cid = normalize_id(strip_link_wrapper(str(entry.get('claim'))))
                referenced_cids.add(cid)
                claim = _claim_by_id(registry, cid)
                if claim is None:
                    findings.append(Finding('W', 'W115', profile_path,
                        f"relationships: {role or 'edge'} entry links claim {fmt_id_display(cid)}, "
                        f"but no such claim exists - fix the link, or add the claim to its source."))
                    continue
                if not _claim_backs_edge(claim, pid, other_pid, role, alias_map):
                    findings.append(Finding('W', 'W115', profile_path,
                        f"relationships: entry links claim {fmt_id_display(cid)}, but that claim does "
                        f"not record this {role or 'relationship'} edge - check its persons and roles."))
                    continue
                matched = claim
            else:
                sid = normalize_id(strip_link_wrapper(str(entry.get('source'))))
                cands = [c for c in registry.source_claims.get(sid, [])
                         if isinstance(c, dict) and _claim_backs_edge(c, pid, other_pid, role, alias_map)]
                if not cands:
                    findings.append(Finding('W', 'W115', profile_path,
                        f"relationships: {role or 'edge'} entry cites source {fmt_id_display(sid)}, but it "
                        f"carries no accepted relationship claim for this edge - accept one, or link the "
                        f"claim directly with claim: [[C-…]]."))
                    continue
                matched = cands[0]
                referenced_cids.add(normalize_id(str(matched.get('id', ''))))

            entry_subtype = _entry_subtype(entry)
            claim_subtype = _claim_subtype_norm(matched)
            if entry_subtype and claim_subtype and entry_subtype != claim_subtype:
                findings.append(Finding('W', 'W115', profile_path,
                    f"relationships: entry for claim {fmt_id_display(normalize_id(str(matched.get('id', ''))))} "
                    f"says subtype {entry_subtype!r} but the claim says {claim_subtype!r} - make the nature match."))

            if other_pid:
                _check_reciprocity(registry, findings, pid, other_pid, role, matched, alias_map)

        # Reverse: an opted-in block should apply every accepted kin claim that names this person.
        for claims in registry.source_claims.values():
            for claim in claims:
                if not isinstance(claim, dict) or str(claim.get('status', '')) != 'accepted':
                    continue
                if str(claim.get('type', '')) not in ('relationship', 'marriage', 'birth'):
                    continue
                cid = normalize_id(str(claim.get('id', '')))
                if not cid or cid in referenced_cids:
                    continue
                label = _person_reconcilable_role_label(claim, pid, alias_map)
                if label is None:
                    continue
                findings.append(Finding('W', 'W115', profile_path,
                    f"{fmt_id_display(pid)} has a relationships: block but accepted claim "
                    f"{fmt_id_display(cid)} (a {label} edge naming them) isn't applied in it - add the "
                    f"entry and link the claim, or remove the block if it's not meant to be complete."))


def _cross_file_checks(registry: Registry, findings: list[Finding], with_exif: bool = False) -> None:
    """
    Pass 2: checks that require the full registry.

    Called after _walk_archive has finished, so every ID, claim, and token
    reference is already registered.  Rules that check existence of other
    records (E004 orphan refs, E005 missing persons, E013 summary drift,
    W101 vitals gaps) all live here.
    """

    known_ids = registry.all_known_ids()

    # The resolve map, built once and stashed on the registry: every claim
    # persons:/roles: reference below resolves through it, and the fix modes
    # + needs-sourcing backlog (which run after this pass) reuse it so their
    # view of "which persons does this claim name" matches the checks'.
    alias_map = build_alias_map(_alias_records(registry))
    registry.alias_map = alias_map

    # Alias-layer maintenance + integrity (self-alias, name/stem clashes).
    _alias_checks(registry, findings)

    # W115/W116: reconcile each person-doc relationships: block against claims,
    # and check reciprocity. The alias map resolves a forgiving `to:` to a P-id.
    _check_relationships_reconciliation(registry, findings, alias_map)

    # E001: duplicate person profiles
    for pid, paths in registry.person_profile_paths.items():
        if len(paths) > 1:
            findings.append(Finding('E', 'E001', paths[0],
                f'Duplicate person profile ID {pid}: {[str(p) for p in paths]}'))

    # E004 / E005: check all token references resolve
    for token_id, refs in registry.token_refs.items():
        tid_type = token_id[0].upper() if token_id else ''

        if token_id not in known_ids:
            # E004: orphan reference
            for ref_path, ref_line in refs[:3]:   # report first 3 sites
                findings.append(Finding('E', 'E004', ref_path,
                    f'Orphan reference [{token_id}] (line {ref_line}) - no matching record. '
                    'Create the missing record (for a person, run `fha stubs`) or fix the ID.'))

        if tid_type == 'P' and not registry.has_person(token_id):
            # E005: referenced person has no record at all
            for ref_path, ref_line in refs[:1]:
                findings.append(Finding('E', 'E005', ref_path,
                    f'P-id {token_id} referenced at line {ref_line} but no person record exists - '
                    'create a stub with `fha stubs`, or fix the ID.'))

    # E005: persons referenced in claim `persons:` fields must have a record.
    # References resolve through the alias map first (TOOLING §3 E004): a
    # wrapped or bare P-id that names no record is the integrity error; a name
    # that resolves is fine; an unresolved/ambiguous NAME is an inert
    # note-link, never an E005 dead end (_claim_person_ids drops it).
    for sid, claims in registry.source_claims.items():
        src_path = registry.source_paths.get(sid, Path(sid))
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            for ppid in _claim_person_ids(claim, alias_map):
                if not registry.has_person(ppid):
                    findings.append(Finding('E', 'E005', src_path,
                        f'Claim {claim.get("id","?")} references person {fmt_id_display(ppid)} but no '
                        'person record exists - create a stub with `fha stubs`, or fix the P-id.'))

            # E005, near-miss net: a persons: entry that LOOKS like a mistyped
            # code (P-de957bcda, nine characters) must be said out loud - left
            # inert it silently detaches the claim from its person everywhere
            # (index drops the row, stubs skips it). A name that resolves is
            # fine; an unresolvable plain NAME stays the inert note-link the
            # TOOLING contract promises.
            for raw_ref in link_field_refs(claim.get('persons')):
                if id_type_of(raw_ref) or (alias_map and resolve_ref(raw_ref, alias_map)):
                    continue
                near = _id_near_miss(raw_ref)
                if near:
                    findings.append(Finding('E', 'E005', src_path,
                        f'Claim {claim.get("id","?")} persons: {_near_miss_text(raw_ref, near)}; '
                        f"fix the typo or use the person's name as written."))

            # W118: a claim whose persons: names people but resolves to NONE of
            # them detaches silently - it joins no one's timeline, vitals, or
            # merge checks, and (unlike a dead P-id or a near-miss code, which
            # are E005) leaves no other signal. This is the exact gap the
            # forgiving name-link contract opens: an unresolved plain NAME stays
            # an inert note-link (never an error), but a claim that names ONLY
            # unresolved people is very likely a typo/rename, so warn - never
            # block. Suppressed when a ref is already a near-miss E005 above, to
            # avoid double-reporting the same broken reference.
            person_refs = link_field_refs(claim.get('persons'))
            if person_refs and not _claim_person_ids(claim, alias_map):
                already_flagged = any(
                    _id_near_miss(r) for r in person_refs
                    if not id_type_of(r) and not (alias_map and resolve_ref(r, alias_map)))
                if not already_flagged:
                    listed = ', '.join(repr(str(r)) for r in person_refs)
                    findings.append(Finding('W', 'W118', src_path,
                        f'Claim {claim.get("id","?")} persons: {listed} resolves to no '
                        'person record, so the claim attaches to no one (it will be '
                        "missing from every timeline, vitals tally, and merge check). "
                        'Check the spelling, add the name as an alias on the right '
                        'person, or create the person - or leave it if it is only a note.'))

            # W125: a marriage/divorce naming more than two people without
            # saying which two were the couple. Certificates routinely name the
            # couple AND both sets of parents, and listing all six in persons:
            # is correct (SPEC §8.3) - but then only the roles: map says who
            # married whom. Without a usable one the index cannot tell the
            # couple from their parents and deliberately records NO spouse link
            # rather than guessing one (_lib.spouse_parties). That silence is
            # safe but invisible: the couple simply never appears in the tree.
            # This warning is what makes it visible, and it is the whole reason
            # the indexer is allowed to stay quiet.
            # The condition IS the derivation rule, not a count of people: a
            # couple claim that resolves two or more distinct persons and
            # yields no couple. That single test covers every shape the
            # silence takes - more than two people with no roles: map, more
            # than two with a map resolving fewer than two spouses (one typo'd
            # id, one spouse left out of persons:), and exactly two where the
            # map calls one of them something other than a spouse. A
            # "more than two people" heuristic could see only the first two,
            # and the third would be exactly the silence this exists to
            # prevent, going unreported.
            # Distinct PERSONS, not persons: entries: a bare P-id and a
            # name-link for one man are two entries and one person, and there
            # is no couple to ask about (spouse_parties folds them together for
            # the same reason).
            # The legacy `relationship` + `subtype: spouse-of` shape derives
            # spouse edges through the same rule, so it earns the same warning.
            # Scoped to spouse-of alone: an ordinary parent/child relationship
            # claim names three people (a child and two parents) and has no
            # business being asked for a spouse role.
            # Only claims that actually derive edges can lose one. Derivation
            # reads `accepted`, non-negated claims (index.py
            # _derive_relationships), so a `suggested` claim's missing roles:
            # map has cost nothing yet - the repair there is review, which W102
            # already tracks, and this warning starts the day it is accepted.
            # A NEGATED marriage is a researched absence, "we looked and they
            # did not marry" (SPEC §8.6); telling the human a marriage is
            # missing from the tree because of it would be backwards.
            claim_type = str(claim.get('type', '')).strip().lower()
            derives_spouses = claim_type in ('marriage', 'divorce') or (
                claim_type == 'relationship'
                and str(claim.get('subtype', '')).strip().lower() == 'spouse-of')
            derives_edges = (
                str(claim.get('status', '')).strip() == 'accepted'
                and claim.get('negated') not in (True, 'true'))
            if derives_spouses and derives_edges:
                named = _claim_person_ids(claim, alias_map)
                distinct = {pid: None for pid in named}   # ordered, deduplicated
                if len(distinct) >= 2 and not _claim_spouse_pids(claim, alias_map):
                    where_they_go = (
                        ' - they will be missing from the family tree, from '
                        '`fha relate`, and from the charts on their pages. ')
                    if len(distinct) > 2:
                        # The certificate shape: too many people, nothing saying
                        # which two of them are the couple.
                        cost = ('no marriage is recorded as ending'
                                if claim_type == 'divorce' else
                                'no marriage is recorded between any of them')
                        findings.append(Finding('W', 'W125', src_path,
                            f'Claim {claim.get("id","?")} (type: {claim_type}) names '
                            f'{len(distinct)} people but does not say which two of them '
                            f'were the couple, so {cost}{where_they_go}'
                            'Leave everyone in persons: (a certificate names the parents '
                            'too, and that is right) and add a roles: map naming the '
                            'pair - `roles:` then an indented `spouse: [P-…, P-…]` line.'))
                    else:
                        # Exactly two people, and the claim placed one of them
                        # somewhere other than the couple. It HAS said which two
                        # of them were the couple - it said these two were not -
                        # so the certificate wording would read as nonsense here.
                        by_person = _claim_roles_by_person(claim, alias_map)
                        elsewhere = ', '.join(
                            f'{fmt_id_display(pid)} a {by_person[pid]}'
                            for pid in distinct
                            if by_person.get(pid) and by_person[pid] != 'spouse')
                        cost = ('no marriage is recorded as ending'
                                if claim_type == 'divorce' else
                                'no marriage is recorded between them')
                        findings.append(Finding('W', 'W125', src_path,
                            f'Claim {claim.get("id","?")} (type: {claim_type}) names '
                            f'2 people, but its roles: map calls {elsewhere} rather than '
                            f'a spouse, so {cost}{where_they_go}'
                            'If these two were the couple, name them both - `roles:` then '
                            'an indented `spouse: [P-…, P-…]` line. If they were not, the '
                            'couple is missing from persons: - add them there.'))

            # W126: an accepted birth claim that names other people and gets no
            # parentage into the tree (issue #71). A birth record is where an
            # archive states parentage most plainly - "born to X and Y" - and
            # `persons: [child, father, mother]` is the habit everyone writes.
            # But that order is a habit, not a contract (SPEC §8.3: positional
            # convention alone is too fragile), and the extra person on a birth
            # register is as often an informant or the attending physician as a
            # parent. So the indexer derives a parent edge from the roles: map
            # or from nothing at all (_lib.parentage_parties) - and without
            # this warning, primary parentage evidence would sit accepted in
            # the archive contributing exactly nothing, which is the state the
            # issue was filed about wearing a different hat.
            # The condition IS the derivation rule, as in W125: two or more
            # distinct persons named, and no parent edge derived. That covers
            # every shape the silence takes - no roles: map, a map naming a
            # child and no parent, a map naming parents and nobody born, and a
            # map whose role words are outside the SPEC vocabulary (`mother:`,
            # `father:`), which is the shape a person-count test would miss and
            # the one most likely to look correct to whoever wrote it.
            # Distinct PERSONS, not persons: entries: a bare P-id and a
            # name-link for one baby are two entries and one person, and one
            # person is not a parentage to ask about.
            # Only accepted, non-negated claims derive edges, so only those can
            # lose one: a suggested claim's repair is review (W102 tracks that
            # backlog) and a negated birth claim (SPEC §8.6) exists to deny the
            # very bond this would ask it to record.
            # Scoped to `birth` alone. A `relationship` claim missing its map
            # is E015's business - `roles:` is REQUIRED there - and couple
            # claims are W125's, so no claim collects two warnings for one map.
            if claim_type == 'birth' and derives_edges:
                named = _claim_person_ids(claim, alias_map)
                distinct = {pid: None for pid in named}   # ordered, deduplicated
                children, parents = _claim_parentage_pids(claim, alias_map)
                if len(distinct) >= 2 and not (children and parents):
                    where_it_goes = (
                        ' - the parents will be missing from the family tree, '
                        'from `fha relate`, and from the charts on their pages. ')
                    head = (f'Claim {claim.get("id","?")} (type: birth) names '
                            f'{len(distinct)} people')
                    by_person = _claim_roles_by_person(claim, alias_map)
                    role_children = [pid for pid in distinct
                                     if by_person.get(pid) == 'child']
                    role_parents = [pid for pid in distinct
                                    if by_person.get(pid) == 'parent']
                    if role_children:
                        # Half a map: it says who was born and leaves everyone
                        # else unplaced. Reading "everyone else" as the parents
                        # is precisely the guess the indexer refuses.
                        findings.append(Finding('W', 'W126', src_path,
                            f'{head} and says who was born, but marks none of the '
                            f'others as a parent, so no parent link is recorded'
                            f'{where_it_goes}'
                            'If the others are the parents, add them to the roles: '
                            'map - an indented `parent: [P-…, P-…]` line beside the '
                            '`child:` one. If they are not (an informant, a doctor, '
                            'a witness), nothing is missing and this note is safe '
                            'to leave.'))
                    elif role_parents:
                        # The mirror: the parents are marked and nobody is
                        # marked as born. The subject is not positional either.
                        marked = (f'marks one of them as a parent'
                                  if len(role_parents) == 1 else
                                  f'marks {len(role_parents)} of them as parents')
                        findings.append(Finding('W', 'W126', src_path,
                            f'{head} and {marked}, but does not say who was born, '
                            f'so no parent link is recorded{where_it_goes}'
                            'Add an indented `child: [P-…]` line to the roles: map '
                            'naming the person this record is the birth of.'))
                    else:
                        # The reporter's shape: a certificate of live birth,
                        # accepted, naming the child and both parents, and
                        # contributing nothing to the pedigree.
                        findings.append(Finding('W', 'W126', src_path,
                            f'{head} but does not say which of them was born and '
                            f'which are the parents, so no parent link is recorded'
                            f'{where_it_goes}'
                            'Leave everyone in persons: (a birth record names the '
                            'parents too, and that is right) and add a roles: map - '
                            '`roles:` then indented `child: [P-…]` and '
                            '`parent: [P-…, P-…]` lines.'))

            # place reference - forgiving (PR 05): never reject a place the human
            # typed.  A well-formed L-id (bare or [[wrapped]]) that doesn't
            # resolve is a broken link (E004, an integrity problem).  A NAME that
            # resolves via the alias map to a registered place is fine.  Anything
            # else is just the place as-written in the wrong field - point to
            # place_text:, don't error.
            place_raw = strip_link_wrapper(str(claim.get('place', ''))).strip()
            if place_raw:
                if id_type_of(place_raw) == 'L':
                    place_ref = normalize_id(place_raw)
                    if place_ref not in registry.place_ids:
                        findings.append(Finding('E', 'E004', src_path,
                            f'Claim {claim.get("id","?")} place {fmt_id_display(place_ref)} '
                            f'is not a registered place - register it with `fha places` '
                            f'or fix the L-id'))
                elif id_type_of(resolve_ref(place_raw, alias_map) or '') == 'L':
                    pass   # a place name that resolves unambiguously - nothing to say
                else:
                    findings.append(Finding('W', 'W109', src_path,
                        f'Claim {claim.get("id","?")} place: {place_raw!r} is not a place '
                        f'L-id - put the place as written in place_text: instead, or run '
                        f'`fha places` to register it and get an L-id'))

            # E004: corroborates/contradicts targets, resolved through the alias
            # map first. An ID-shaped target (bare or [[wrapped]]) that names no
            # record stays the error it always was; a name target that resolves
            # is fine; an unresolved name is an inert note-link, not a finding -
            # unless it is a NEAR-MISS code (C-de957bcda, nine characters), which
            # is a typo to fix, not a note-link to ignore.
            for link_type in ('corroborates', 'contradicts'):
                for t in link_field_refs(claim.get(link_type)):
                    if id_type_of(t):
                        tid = normalize_id(t)
                        if tid not in registry.claim_ids and tid not in known_ids:
                            findings.append(Finding('E', 'E004', src_path,
                                f'Claim {claim.get("id","?")} {link_type}: {tid} not found - '
                                f'fix the ID, or point it at an existing claim.'))
                    elif not (alias_map and resolve_ref(t, alias_map)):
                        near = _id_near_miss(t)
                        if near:
                            findings.append(Finding('E', 'E004', src_path,
                                f'Claim {claim.get("id","?")} {link_type}: {_near_miss_text(t, near)}; '
                                f'fix the typo, or point it at an existing claim by its full C-id.'))

            # E009: contradicts without an open question referencing both claims.
            # Targets go through link_field_refs so a wrapped `[[C-…]]` is
            # checked as its bare C-id (the form questions cite).
            if claim.get('contradicts'):
                cid = normalize_id(str(claim.get('id', '')))
                for t in link_field_refs(claim.get('contradicts')):
                    if not id_type_of(t):
                        continue   # a name target has no C-id for a question to cite
                    tid = normalize_id(t)
                    # Check if an open question references both C-ids
                    if not _has_question_for(cid, tid, registry):
                        findings.append(Finding('E', 'E009', src_path,
                            f'Claim {cid} contradicts {tid} but no open question records the conflict - '
                            'run `fha lint --spawn-questions` to open one, or add a `## Q:` block to notes/questions.md.'))

    # E013: summary block drift for curated profiles
    children_of = _build_children_of(registry)   # parent_pid → {child_pids}
    for pid, paths in registry.person_profile_paths.items():
        profile_path = paths[0]
        meta = registry.person_meta.get(pid, {})
        if str(meta.get('tier', '')) != 'curated':
            continue

        rec = read_record(profile_path)
        summary = _parse_summary_block(rec['body'])
        if not summary:
            continue

        # Gather accepted claims for this person
        person_claims = _get_person_accepted_claims(pid, registry)

        for label, text, p_ids, s_ids in summary:
            _check_summary_line(label, text, p_ids, s_ids, person_claims,
                                registry, profile_path, findings,
                                profile_pid=pid, children_of=children_of)

    # W101: vitals gaps for curated people
    for pid in registry.person_profile_paths:
        meta = registry.person_meta.get(pid, {})
        if str(meta.get('tier', '')) != 'curated':
            continue
        living = str(meta.get('living', 'unknown'))

        person_claims = _get_person_accepted_claims(pid, registry)
        # Positive-fact set only: a negated claim ("not born in 1900") is a
        # confirmed absence, not a settled vital, so it must not satisfy the
        # birth/death completeness check below. The marriage branch keeps its
        # own explicit negated-marriage rule (a negated marriage IS a
        # completeness signal - "never married").
        claimed_types = {str(c.get('type', '')) for c in person_claims
                         if c.get('negated') not in (True, 'true')}

        missing_vitals = []
        for vital in ('birth', 'marriage', 'death'):
            if vital == 'death' and living in ('true', 'unknown'):
                continue   # death not applicable while living
            if vital == 'marriage':
                if meta.get('no_known_marriages') in (True, 'true'):
                    continue   # confirmed no marriages
                negated_marriage = any(
                    str(c.get('type', '')) == 'marriage' and c.get('negated') in (True, 'true')
                    for c in person_claims
                )
                if negated_marriage:
                    continue
            if vital not in claimed_types:
                missing_vitals.append(vital)

        if missing_vitals:
            profile_path = registry.person_profile_paths[pid][0]
            findings.append(Finding('W', 'W101', profile_path,
                f'Curated person {pid} missing vital(s): {", ".join(missing_vitals)}'))

    # W106: accepted claims missing Mills analysis fields
    for sid, claims in registry.source_claims.items():
        src_path = registry.source_paths.get(sid, Path(sid))
        for claim in claims:
            if str(claim.get('status', '')) == 'accepted':
                missing_mills = []
                if not claim.get('information'):
                    missing_mills.append('information')
                if not claim.get('evidence'):
                    missing_mills.append('evidence')
                if missing_mills:
                    cid = claim.get('id', '?')
                    findings.append(Finding('W', 'W106', src_path,
                        f'Accepted claim {cid} missing Mills field(s): {", ".join(missing_mills)}'))

    # W124: accepted claims resting on evidence nobody has written out
    _check_untranscribed_evidence(registry, findings)

    # E016: new claims referencing a merged person
    for pid, meta in registry.person_meta.items():
        if str(meta.get('status', '')) == 'merged':
            for sid, claims in registry.source_claims.items():
                src_path = registry.source_paths.get(sid, Path(sid))
                for claim in claims:
                    if pid in _claim_person_ids(claim, alias_map):
                        findings.append(Finding('E', 'E016', src_path,
                            f'Claim {claim.get("id","?")} references merged person {pid} '
                            f'(merged into {meta.get("merged_into","?")})'))

    # W107: direct [token] references to merged persons (gradual cleanup)
    for pid, meta in registry.person_meta.items():
        if str(meta.get('status', '')) == 'merged':
            if pid in registry.token_refs:
                target = meta.get('merged_into', '?')
                display_pid = pid[0].upper() + pid[1:]  # P-xxxx: uppercase type prefix only
                for ref_path, ref_line in registry.token_refs[pid][:5]:
                    findings.append(Finding('W', 'W107', ref_path,
                        f'[{display_pid}] at line {ref_line} references merged person '
                        f'(merged into {target}); update to the survivor P-id'))

    # W103: stale folder bracket lists
    _check_bracket_lists(registry, findings)

    # W110: direct-line person in wrong Ahnentafel couple folder
    # W119: direct-line ancestor still filed as a stub (reads W110's derived map)
    pid_to_pos = _check_ahnentafel_placement(registry, findings)
    _check_direct_line_stubs(registry, findings, pid_to_pos)

    # W104: summary line without supporting accepted claim (handled in E013 pass)
    # W105: hand-edits under GENERATED header
    _check_generated_headers(registry.archive_root, findings)

    # W108: README.md older than SPEC.md
    _check_readme_age(registry.archive_root, findings)

    # E011/E012: reverse asset inventory and optional embedded metadata checks
    # In working-copy mode the asset files live on the main machine; skip.
    # The run_lint caller emits a single informational note in data['wc_note'].
    if not registry.is_working_copy:
        _check_reverse_inventory(registry, findings, with_exif)

    # W123: a record folder Pass 1 could not open. Last, so it reads as the
    # caveat on everything above it rather than as one more finding among many.
    _check_unreadable_dirs(registry, findings)


def _check_untranscribed_evidence(
    registry: Registry, findings: list[Finding],
) -> None:
    """W124: accepted claims resting on evidence nobody has written out (#46).

    A source can be processed, have claims drafted from its pictures, reviewed
    and accepted - and the archive still holds no text of what the document
    says. The pictures are the only copy. Every later reader then reads the
    claim values instead, inheriting whatever the first pass misread, and a text
    search over the archive answers for what some earlier pass chose to write
    down while looking exactly like a search of the evidence.

    That second effect is why this is a lint rule and not just a nicety. A null
    text search on such an archive is a statement about coverage, not about the
    family, and it has already been read the other way: a surname was searched
    for, found only in one claim's value, judged invented, and struck - while it
    sat in plain handwriting on a chart in a 22-page image-only scan. The
    archive where that happened held 43 such sources carrying 135 accepted
    claims and had no way to say so.

    Read entirely from the record's own `files:` roles, never from the files
    themselves: lint does not open a PDF to ask whether it has a text layer (it
    would need an optional dependency and every source's worth of reading to
    answer a warning). A source with no files listed is not flagged - there is
    nothing to transcribe. Warning, not error: an untranscribed source is a
    normal state of research, and the fix is work, not a correction.
    """
    for sid, meta in sorted(registry.source_meta.items()):
        raw_files = meta.get('files') or []
        entries = [
            (str(f.get('role', '')), str(f.get('file', '')))
            for f in raw_files if isinstance(f, dict)
        ]
        if not entries or files_carry_searchable_text(entries):
            continue

        accepted = [
            c for c in registry.source_claims.get(sid, [])
            if isinstance(c, dict) and str(c.get('status', '')) == 'accepted'
        ]
        if not accepted:
            continue

        path = registry.source_paths.get(sid, registry.archive_root / str(sid))
        display = fmt_id_display(sid)
        findings.append(Finding('W', 'W124', path,
            f'{len(accepted)} accepted claim(s) rest on evidence this archive '
            f'holds no words for: every file of {display} is a scan, '
            'photograph, PDF or recording with no transcript beside it. '
            'A search of your archive cannot look inside them, so '
            'anything written in this document reads as though it were not '
            'there. If it is a PDF that carries its own text layer, run '
            f'`fha source extract {display}`; otherwise read the file and type '
            'out what it says, then attach it with `fha process <one of its '
            'files> --more <your-transcript.md> transcript`. Either way, run '
            '`fha index` afterwards so the words become searchable.'))


def _check_unreadable_dirs(registry: Registry, findings: list[Finding]) -> None:
    """W123: name every record folder this lint could not open.

    Without it, `fha lint` answers "0 errors" for an archive whose `people/`
    or `sources/` subtree it never listed - the most confident possible way to
    say nothing at all. The finding is a warning rather than an error because
    the archive itself is very probably fine: what failed is this machine's
    access to it, and calling a permissions change a spec violation would be
    both wrong and unfixable by editing a record. But it does move lint off
    exit 0, which is the whole point - a clean bill of health must not be
    issued over records nobody read.

    Worded for someone who has never seen a permission bit: what was skipped,
    what it means for the answer above, the two ordinary causes, and the one
    command to run afterwards.
    """
    for path in registry.unreadable_dirs:
        try:
            shown = path.relative_to(registry.archive_root).as_posix()
        except ValueError:
            shown = str(path).replace('\\', '/')
        findings.append(Finding(
            'W', 'W123', path,
            f'This folder could not be opened, so nothing filed in {shown} was '
            'checked - the rest of this report says nothing about it either way. '
            'Usually a folder whose permissions changed, or a drive or network '
            'share that is not connected. Reconnect it (or restore your access '
            'to the folder), then run `fha lint` again.'))


def _check_reverse_inventory(
    registry: Registry,
    findings: list[Finding],
    with_exif: bool,
) -> None:
    """Detect files carrying known S-ids that are absent from source inventories."""
    documents_root = _mapped_root('documents', registry)
    if documents_root.exists():
        for file_path in sorted(p for p in documents_root.rglob('*') if p.is_file()):
            parsed = parse_filename(file_path)
            if not parsed or parsed.get('id_type') != 'S':
                continue
            sid = normalize_id(parsed['id_str'])
            source_path = registry.source_paths.get(sid)
            if not source_path:
                continue
            alias_path = _path_to_alias(file_path, 'documents', registry)
            if alias_path and alias_path not in registry.source_inventory.get(sid, set()):
                findings.append(Finding('E', 'E011', source_path,
                    f'On-disk document carries {sid} but is absent from files: {alias_path!r}'))

    if with_exif:
        _check_embedded_source_keywords(registry, findings)


def _files_to_keyword_scan(alias: str, root: Path, registry: Registry):
    """Files under one asset root that E012's exiftool pass should read.

    The photos root honours `photos_ignore:` (#35) exactly as the catalog scan
    does. Without it this check reads every file in the bulk photo-service
    export the setting exists to exclude - on the motivating archive that is
    63,156 files handed to exiftool for keywords nobody filed. An ignored
    subtree is pruned unwalked rather than filtered afterwards, so the cost
    goes away rather than moving; `documents` has no such setting and walks
    whole. A malformed `photos_ignore:` prunes nothing here rather than
    guessing, and `fha photoindex` is where the human sees the parse error.
    """
    if alias != 'photos':
        yield from (p for p in root.rglob('*') if p.is_file())
        return
    try:
        patterns = photos_ignore_patterns(registry.fha_config)
    except RuntimeError:
        patterns = []
    if not patterns:
        yield from (p for p in root.rglob('*') if p.is_file())
        return
    is_ignored = photos_ignore_matcher(patterns)
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if not is_ignored((here / d).relative_to(root).as_posix())
        ]
        for name in filenames:
            p = here / name
            if not is_ignored(p.relative_to(root).as_posix()):
                yield p


def _check_embedded_source_keywords(registry: Registry, findings: list[Finding]) -> None:
    """E012 and photo-side E011 checks using exiftool keyword reads."""
    scan_paths: set[Path] = set()
    path_aliases: dict[Path, str] = {}

    for alias in ('documents', 'photos'):
        root = _mapped_root(alias, registry)
        if root.exists():
            for file_path in _files_to_keyword_scan(alias, root, registry):
                resolved = file_path.resolve()
                scan_paths.add(resolved)
                alias_path = _path_to_alias(file_path, alias, registry)
                if alias_path:
                    path_aliases[resolved] = alias_path

    if not scan_paths:
        return

    try:
        keyword_map = _read_source_keywords(sorted(scan_paths))
    except RuntimeError as e:
        findings.append(Finding('E', 'E012', registry.archive_root, str(e)))
        return

    inventory_by_alias: dict[str, str] = {}
    for sid, paths in registry.source_inventory.items():
        for alias_path in paths:
            inventory_by_alias[alias_path] = sid

    for disk_path, keyword_sids in keyword_map.items():
        if not keyword_sids:
            continue
        alias_path = path_aliases.get(disk_path)
        inventory_sid = inventory_by_alias.get(alias_path or '')
        for keyword_sid in keyword_sids:
            source_path = registry.source_paths.get(keyword_sid, registry.archive_root)
            if inventory_sid and inventory_sid != keyword_sid:
                findings.append(Finding('E', 'E012', source_path,
                    f'Embedded SOURCE {keyword_sid} disagrees with inventory source '
                    f'{inventory_sid} for {alias_path or disk_path}'))
            elif not inventory_sid and keyword_sid in registry.source_paths:
                findings.append(Finding('E', 'E011', registry.source_paths[keyword_sid],
                    f'File carries embedded SOURCE {keyword_sid} but is absent from files: '
                    f'{alias_path or disk_path}'))


def _read_source_keywords(paths: list[Path]) -> dict[Path, set[str]]:
    """Read SOURCE: S-id keywords from files using exiftool JSON output."""
    result: dict[Path, set[str]] = {}
    batch_size = 50
    for start in range(0, len(paths), batch_size):
        batch = paths[start:start + batch_size]
        cmd = ['exiftool', '-j', '-Keywords', '-Subject'] + [str(p) for p in batch]
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                encoding='utf-8',
            )
        except FileNotFoundError as e:
            raise RuntimeError(format_exiftool_error('fha lint --with-exif')) from e
        if proc.returncode not in (0, 1):
            raise RuntimeError(f'exiftool failed while reading embedded metadata: {proc.stderr.strip()}')
        try:
            rows = json.loads(proc.stdout or '[]')
        except json.JSONDecodeError as e:
            raise RuntimeError(f'exiftool returned invalid JSON: {e}') from e
        for row in rows:
            source_file = row.get('SourceFile')
            if not source_file:
                continue
            keywords = _metadata_values(row.get('Keywords')) + _metadata_values(row.get('Subject'))
            source_ids = {
                normalize_id(m.group(1))
                for value in keywords
                for m in [_SOURCE_KEYWORD_RE.match(value.strip())]
                if m
            }
            result[Path(source_file).resolve()] = source_ids
    return result


def _metadata_values(value: object) -> list[str]:
    """Return metadata scalar/list values as strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def _question_blocks(text: str) -> list[str]:
    """Split markdown into per-heading blocks, keeping each heading with its content."""
    return re.split(r'(?=^##\s)', text, flags=re.M) if text else []


def _has_question_for(cid1: str, cid2: str, registry: Registry) -> bool:
    """
    Return True if a question block exists that references both cid1 and cid2
    within the same block. Checks questions.md and person research files.
    Requiring co-occurrence within one block avoids false passes where the two
    IDs happen to appear in separate, unrelated questions.
    """
    all_blocks: list[str] = _question_blocks(registry.questions_content)
    for content in registry.research_content.values():
        all_blocks.extend(_question_blocks(content))

    return any(cid1 in block.lower() and cid2 in block.lower() for block in all_blocks)


def _get_person_accepted_claims(pid: str, registry: Registry) -> list[dict]:
    """
    Return all accepted claims that name pid in their persons: list.

    Injects a synthetic '_source_id' key into each claim dict so that callers
    (E013 summary checks, W101 vitals checks) can identify which source a claim
    came from without a second lookup.  Claims don't carry source_id in their
    own YAML dict - it lives on the source record that contains them.
    Persons resolve via registry.alias_map, so a claim naming this person by
    `[[Name]]` counts toward their vitals/summary exactly like a bare P-id.
    """
    result = []
    for sid, claims in registry.source_claims.items():
        for claim in claims:
            if not isinstance(claim, dict) or str(claim.get('status', '')) != 'accepted':
                continue
            if pid in _claim_person_ids(claim, registry.alias_map):
                result.append({**claim, '_source_id': sid})
    return result


def _check_summary_line(
    label: str,
    text: str,
    p_ids: list[str],
    s_ids: list[str],
    person_claims: list[dict],
    registry: Registry,
    profile_path: Path,
    findings: list[Finding],
    profile_pid: str = '',
    children_of: dict[str, set[str]] | None = None,
) -> None:
    """
    Verify one summary-block label segment against accepted claims (E013 / W104).
    Each [S-id] citation must have a matching accepted claim of the right type for
    this person; each [P-id] cross-link must resolve to a known person record.
    For Parents/Children, each [P-id] must also be supported by an accepted
    child-of relationship claim (E013), not merely exist as a record.
    """
    label_to_types = {
        'Born': ['birth', 'baptism'],
        'Died': ['death', 'burial'],
        'Married': ['marriage'],
        'Parents': ['relationship'],
        'Children': ['relationship'],
    }
    expected_types = label_to_types.get(label, [])

    for sid in s_ids:
        # Check that this source has an accepted claim of the right type for this person
        matching = [
            c for c in person_claims
            if normalize_id(str(c.get('_source_id', ''))) == normalize_id(sid)
            and str(c.get('type', '')) in expected_types
        ]
        if not matching and expected_types:
            findings.append(Finding('W', 'W104', profile_path,
                f'Summary **{label}:** cites [S-{sid.split("-", 1)[-1]}] but no accepted '
                f'{"|".join(expected_types)} claim found for that source and person'))

    # Check P-id cross-links resolve (p_ids are already normalized by _parse_summary_block)
    for ref_pid in p_ids:
        if not registry.has_person(ref_pid):
            findings.append(Finding('E', 'E004', profile_path,
                f'Summary block {label} references unknown person {ref_pid}'))

    # E013: Parents/Children cross-links must match an accepted child-of relationship
    # claim (TOOLING §E013), not merely resolve to a person record.
    if label in ('Parents', 'Children') and children_of is not None and profile_pid:
        for ref_pid in p_ids:
            if not registry.has_person(ref_pid):
                continue   # already reported as E004 above
            if label == 'Parents':
                supported = profile_pid in children_of.get(ref_pid, set())
            else:  # Children
                supported = ref_pid in children_of.get(profile_pid, set())
            if not supported:
                findings.append(Finding('E', 'E013', profile_path,
                    f'Summary **{label}:** lists {ref_pid} but no accepted child-of '
                    f'relationship claim links them to {profile_pid}'))


def _check_generated_headers(archive_root: Path, findings: list[Finding]) -> None:
    """W105: detect hand-edits below a GENERATED header."""
    gen_header = re.compile(r'^<!-- GENERATED', re.M)
    for path in archive_root.rglob('*.md'):
        if '.cache' in path.parts:
            continue
        try:
            text = path.read_text(encoding='utf-8')
        except OSError:
            continue
        if gen_header.search(text):
            # Existence of the header is noted; we'd need mtime vs generation
            # to detect actual hand-edits. Flag the presence for now only
            # if content after header appears to have been manually changed.
            # (Full detection requires comparing against a known-good generated state.)
            pass   # deferred: W105 requires mtime tracking


def _check_readme_age(archive_root: Path, findings: list[Finding]) -> None:
    """W108: README.md older than SPEC.md."""
    readme = archive_root / 'README.md'
    spec = archive_root / 'SPEC.md'
    if readme.exists() and spec.exists():
        if readme.stat().st_mtime < spec.stat().st_mtime:
            findings.append(Finding('W', 'W108', readme,
                'README.md older than SPEC.md - may need updating (the README rule)'))


# ── E018: agent-instruction drift ────────────────────────────────────────────

_DEPRECATED_COMMANDS = ['fha promote']

def _check_agent_drift(archive_root: Path, findings: list[Finding]) -> None:
    """E018: check AGENTS.md and skills for deprecated commands."""
    agents_path = archive_root / 'AGENTS.md'
    if not agents_path.exists():
        return
    try:
        text = agents_path.read_text(encoding='utf-8')
    except OSError:
        return

    for cmd in _DEPRECATED_COMMANDS:
        if cmd in text:
            findings.append(Finding('E', 'E018', agents_path,
                f'AGENTS.md references deprecated command: {cmd!r}'))

    # Check for photo-rename instructions (locked rule)
    if re.search(r'rename.*photo|photo.*rename', text, re.I):
        # Only flag if it says to rename (not the prohibition)
        pass   # too ambiguous to check textually


def _check_roots_change(archive_root: Path, fha_config: dict, findings: list[Finding]) -> None:
    """W121: a `roots:` value changed and orphaned already-filed assets (#36).

    The E011s that follow such a change name each orphan individually and
    suggest `fha reconcile`, which cannot help - nothing moved. This one
    finding names the cause (the changed value, and what it was), sits on
    fha.yaml where the fix lives, and appears at the top of the report ahead
    of the per-record fallout. Sticky until reverted or re-pointed (see
    `_lib.roots_change_orphans` for the stamp semantics).
    """
    # record=False: lint reads and reports, it does not seed the stamp - a
    # linter pointed at a fixture or a read-only checkout must not create
    # files there. `fha index` / `fha doctor` own the recording.
    for item in roots_change_orphans(archive_root, fha_config, record=False):
        findings.append(Finding('W', 'W121', archive_root / 'fha.yaml',
                                format_roots_orphan_warning(item, archive_root)))


# ── Format check ─────────────────────────────────────────────────────────────

_FRONTMATTER_KEY_ORDER_PERSONS = [
    'id', 'aliases', 'name', 'name_variants', 'face_tags', 'sex', 'living',
    'no_known_marriages', 'no_known_children', 'external_ids', 'created', 'tier',
]
_FRONTMATTER_KEY_ORDER_SOURCES = [
    'id', 'aliases', 'title', 'source_type', 'source_date', 'source_class',
    'repository', 'citation', 'external_links', 'people', 'places', 'restricted',
    'provenance', 'rights', 'physical_location', 'files', 'created',
]


def _check_format(path: Path, findings: list[Finding]) -> None:
    """Conservative format checks."""
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return

    # Check final newline
    if text and not text.endswith('\n'):
        findings.append(Finding('W', 'W109', path, 'File missing final newline'))

    # Check for Windows line endings (CRLF)
    if '\r\n' in text:
        findings.append(Finding('W', 'W109', path, 'File uses CRLF line endings'))


def _fix_format(
    path: Path,
    progress: list[str],
    changed: list[str],
    dry_run: bool = False,
) -> None:
    """Apply conservative formatting fixes: CRLF→LF and ensure trailing newline.

    Per the structured-result contract (run_* does not print), the per-file
    progress line goes into `progress` for `_cmd_lint` to render, and a real
    write is recorded in `changed`.  The file write itself is a side effect that
    stays here in the compute layer.
    """
    try:
        # Exact IO, not `Path.read_text`. The default read translates CRLF to LF
        # before this function ever sees it, so `.replace('\r\n', '\n')` below
        # found nothing and the CRLF half of the fix only worked by accident -
        # via the default WRITE translating back to os.linesep, which happens to
        # be LF on Linux and CRLF on Windows. That means the fix for W109 "File
        # uses CRLF line endings" did nothing on a CRLF file that already ended
        # in a newline, and on Windows converted a clean LF archive TO CRLF.
        # Reading and writing exactly makes the code do what its docstring says
        # on every platform.
        text = read_text_exact(path)
    except OSError:
        return
    fixed = text.replace('\r\n', '\n')
    if fixed and not fixed.endswith('\n'):
        fixed += '\n'
    if fixed != text:
        if dry_run:
            progress.append(f'Would fix formatting: {path.name}')
        else:
            write_text_exact_atomic(path, fixed)
            progress.append(f'Fixed formatting: {path.name}')
            changed.append(str(path))


# ── Main lint entry point ─────────────────────────────────────────────────────

def _run_lint_core(
    archive_root: Path,
    fha_config: dict,
    with_exif: bool = False,
) -> tuple[list[Finding], 'Registry']:
    """Run the three core lint passes and return (findings, registry).

    Shared by run_lint (which then adds format/fix passes and prints output)
    and run_lint_silent (which just counts findings for fha doctor).  Keeping
    both entry points in sync automatically: any new core pass added here is
    reflected in both.
    """
    findings: list[Finding] = []
    registry = Registry(archive_root, fha_config)
    _check_roots_change(archive_root, fha_config, findings)
    _walk_archive(archive_root, registry, findings)
    _cross_file_checks(registry, findings, with_exif=with_exif)
    _check_agent_drift(archive_root, findings)
    return findings, registry


def run_lint(
    archive_root: Path,
    fha_config: dict,
    with_exif: bool = False,
    format_check: bool = False,
    format_write: bool = False,
    dry_run: bool = False,
    mint_stubs: bool = False,
    spawn_questions: bool = False,
    fix_claims_fence: bool = False,
    fix_ids: bool = False,
    fix_reciprocal: bool = False,
    spec_root: Path | None = None,  # TODO: use for TOOLING §3 spec-drift checks (E018 expansion)
) -> Result:
    """
    Run all lint checks against archive_root and return a structured `Result`.

    The reference implementation of the structured-result contract (_lib.py): this
    function computes findings and performs the mutating fix modes (their file
    writes are side effects that belong in the compute layer), but it does NOT
    print the human report - `_cmd_lint` renders that from the returned Result.
    Report-only by default; mutating fix modes require explicit flags and respect
    --dry-run. Never modifies original source files or photos.

    The Result carries:
      - messages: every finding, folded into Message form (severity → level).
      - data.n_errors / data.n_warnings: the counts the summary line needs.
      - data.progress: the per-operation lines fix modes emit, in order, for
        `_cmd_lint` to print ahead of the findings report.
      - data.config_missing: set when there is no fha.yaml (a special early case
        whose output `_cmd_lint` renders differently - compact JSON, absolute path).
      - changed: files actually created/written by the fix modes (empty on dry-run).
    """
    # Check that archive root looks right
    if not (archive_root / 'fha.yaml').exists():
        msg = f'No fha.yaml found at {archive_root} - is this an archive root?'
        result = Result(
            ok=False,
            exit_code=EXIT_ERRORS,
            data={'config_missing': True, 'message': msg,
                  'n_errors': 1, 'n_warnings': 0, 'progress': []},
        )
        result.add('error', msg, code='E010', path=archive_root)
        return result

    findings, registry = _run_lint_core(archive_root, fha_config, with_exif=with_exif)

    progress: list[str] = []
    changed: list[str] = []
    wc_note: str | None = None
    if registry.is_working_copy:
        n_inventoried = sum(len(v) for v in registry.source_inventory.values())
        wc_note = (
            f'[working copy] {n_inventoried} asset file(s) assumed present on the main'
            ' machine - E011/E012 asset-on-disk checks skipped'
        )

    # Format checks / fixes
    if format_check or format_write:
        for path in archive_root.rglob('*.md'):
            if '.cache' not in path.parts and not is_template_file(path):
                _check_format(path, findings)
                if format_write:
                    _fix_format(path, progress, changed, dry_run=dry_run)

    # Fix modes (each respects --dry-run via its own parameter)
    if mint_stubs:
        _fix_mint_stubs(registry, archive_root, progress, changed, dry_run=dry_run)
    if spawn_questions:
        _fix_spawn_questions(registry, findings, archive_root, progress, changed, dry_run=dry_run)
    if fix_claims_fence:
        _fix_claims_fence(registry, archive_root, progress, changed, dry_run=dry_run)
    if fix_ids:
        # Records first, then their claims: an id-less source's claims never
        # reached the registry (Pass 1 stops before parsing them without an
        # S-id), so the claim half re-reads the just-completed files.
        minted_sources = _fix_mint_ids(registry, archive_root, progress, changed, dry_run=dry_run)
        _fix_mint_claim_ids(registry, archive_root, progress, changed, dry_run=dry_run,
                            extra_source_paths=minted_sources)
    if fix_reciprocal:
        _fix_reciprocal(registry, archive_root, progress, changed, dry_run=dry_run)

    # Sort findings by severity then path
    findings.sort(key=lambda f: (f.code, f.path))

    n_errors = sum(1 for f in findings if f.severity == 'E')
    n_warnings = sum(1 for f in findings if f.severity == 'W')
    if n_errors:
        exit_code = EXIT_ERRORS
    elif n_warnings:
        exit_code = EXIT_WARNINGS
    else:
        exit_code = EXIT_CLEAN

    # Informational needs-sourcing worklist - deliberately NOT a finding, so it
    # never moves the exit code off its documented level (it is a worklist, like
    # the suggested-claim backlog, not a gate).
    backlog = _needs_sourcing_backlog(registry)

    # Hand-authored id-less records: reported as auto-mintable (not E002/E010), so
    # a human's pre-machine record is surfaced and completable, never silently lost.
    # A record still carrying the template's placeholder id gets its own wording -
    # "no ID yet" would read as a lie next to a visible `id: P-__________` line.
    mintable = []
    for path, _kind in registry.idless_records:
        rel = path.relative_to(archive_root)
        if path in registry.placeholder_id_paths:
            mintable.append(
                f'{rel}: id is still the template placeholder - run '
                '`fha lint --fix-ids` to replace it with a real code (the old '
                'filename is kept as an alias, so existing [[links]] keep working).')
        else:
            mintable.append(
                f'{rel}: no ID yet (hand-authored) - run '
                '`fha lint --fix-ids` to add one (the old filename is kept as an alias, '
                'so existing [[links]] keep working).')

    return Result(
        ok=(n_errors == 0),
        exit_code=exit_code,
        data={'n_errors': n_errors, 'n_warnings': n_warnings, 'progress': progress,
              'wc_note': wc_note, 'backlog': backlog, 'mintable': mintable},
        messages=[finding_to_message(f) for f in findings],
        changed=changed,
    )


def _cmd_lint(result: Result, archive_root: Path, use_json: bool = False) -> int:
    """Render a lint Result to stdout and return the process exit code.

    The only layer that prints lint's report.  Reproduces the historical output
    byte-for-byte: progress lines first (fix-mode operations, both modes), then
    either the indented `--json` payload or the relative-path findings list plus
    the summary line.  The no-fha.yaml case keeps its distinct format (compact
    JSON, absolute path, "Summary: 1 error(s)").
    """
    data = result.data

    if data.get('config_missing'):
        msg = data['message']
        if use_json:
            print(json.dumps([{'severity': 'E', 'code': 'E010',
                               'path': str(archive_root), 'message': msg}]))
        else:
            print(f'E E010 {archive_root}: {msg}')
            print('Summary: 1 error(s)')
        return result.exit_code

    # Fix-mode progress prints ahead of the report, regardless of --json.
    for line in data.get('progress', []):
        print(line)

    # Working-copy mode note prints before findings (not a finding itself).
    # Suppressed under --json so stdout stays a valid JSON document.
    if data.get('wc_note') and not use_json:
        print(data['wc_note'])

    messages = result.messages

    if use_json:
        payload = [
            {
                'severity': LEVEL_TO_SEVERITY.get(m.level, m.level),
                'code': m.code,
                'path': m.path,
                'message': m.text,
            }
            for m in messages
        ]
        print(json.dumps(payload, indent=2))
    else:
        for m in messages:
            severity = LEVEL_TO_SEVERITY.get(m.level, m.level)
            # Make paths relative for readability
            try:
                rel = Path(m.path).relative_to(archive_root)
                line = f'{severity} {m.code} {rel}: {m.text}'
            except ValueError:
                line = f'{severity} {m.code} {m.path}: {m.text}'
            print(line)

        if not messages:
            if not data.get('wc_note'):
                print('✓ No issues found.')
        else:
            parts = []
            if data.get('n_errors'):
                parts.append(f'{data["n_errors"]} error(s)')
            if data.get('n_warnings'):
                parts.append(f'{data["n_warnings"]} warning(s)')
            print(f'Summary: {", ".join(parts)}')

        # Informational worklists, printed after the findings/summary so they
        # never read as part of the pass/fail report (no effect on exit code).
        mintable = data.get('mintable') or []
        if mintable:
            print('\nAuto-mintable records (no ID yet - not errors):')
            for line in mintable:
                print(f'  - {line}')
        backlog = data.get('backlog') or []
        if backlog:
            print('\nNeeds sourcing (worklist - informational, not errors):')
            for line in backlog:
                print(f'  - {line}')

    return result.exit_code


_TODO_SOURCE_RE = re.compile(r'\(TODO:\s*import source\)', re.I)


def _friendly_to(to_raw: object) -> str:
    """A readable label for a relationships entry's `to:` target - the display
    name after a `|` when present, else the bare stripped target."""
    s = str(to_raw or '').strip()
    m = re.search(r'\|([^\]]+)', s)
    if m:
        return m.group(1).strip()
    return strip_link_wrapper(s)


def _accepted_vital_pids(registry: Registry) -> set[tuple[str, str]]:
    """{(P-id, 'birth'|'death')} for every accepted vital claim naming a person.

    A sourced, accepted vital claim SUPERSEDES the provisional `birth:`/`death:`
    field, so the needs-sourcing backlog stops listing that field once one exists.

    Negated claims do NOT supersede: a `--negated` birth ("not born in 1900")
    is a confirmed absence, not the settled date the provisional field records,
    so it must not silence the needs-sourcing reminder for a real provisional
    date. Same polarity rule as the W101 vitals-gap check above."""
    out: set[tuple[str, str]] = set()
    for claims in registry.source_claims.values():
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            ctype = str(claim.get('type', ''))
            if (ctype in PROVISIONAL_VITAL_FIELDS
                    and str(claim.get('status', '')) == 'accepted'
                    and claim.get('negated') not in (True, 'true')):
                for ppid in _claim_person_ids(claim, registry.alias_map):
                    out.add((ppid, ctype))
    return out


def _needs_sourcing_backlog(registry: Registry) -> list[str]:
    """An INFORMATIONAL worklist (never an error or warning): per person, a
    provisional `birth:`/`death:` not yet backed by an accepted claim, and prose
    marked `(TODO: import source)`. The inverse of the W101 vitals-gap check - it
    flags a present-but-unsourced vital, not a missing one. A provisional date is
    a legitimate starting state, so this nudges toward a source, never blocks.

    Two things are deliberately NOT listed (TOOLING §3: the backlog is for
    RECORDED provisional dates): a present-but-empty key (`death:` alone, the
    shipped template shape) records nothing, and a death entry for a person
    whose `living:` is true or unknown - death is inapplicable while living
    (SPEC §8.2), so nudging for a death source there would be noise."""
    accepted = _accepted_vital_pids(registry)
    lines: list[str] = []
    for pid in sorted(registry.person_meta):
        meta = registry.person_meta[pid]
        name = str(meta.get('name') or fmt_id_display(pid))
        living = str(meta.get('living', '')).strip().lower()
        for field in sorted(PROVISIONAL_VITAL_FIELDS):
            raw = meta.get(field)
            # A present-but-empty key (`death:` with nothing after it, as the
            # quickstart people ship) parses to None - nothing is RECORDED, so
            # there is nothing to source. Only a real value belongs here.
            value = '' if raw is None else str(raw).strip()
            if not value or (pid, field) in accepted:
                continue   # absent/empty, or already superseded by an accepted claim
            if field == 'death' and living in ('true', 'unknown'):
                continue   # death is inapplicable while living (SPEC §8.2;
                           # unknown counts as living, same as the privacy rules)
            lines.append(
                f'{name} ({fmt_id_display(pid)}): provisional {field}: {value!r} - '
                f'recorded but not yet backed by a source. Add one when you can '
                f'(e.g. `fha process` the record, then accept a {field} claim).'
            )
        n_todo = len(_TODO_SOURCE_RE.findall(registry.person_bodies.get(pid, '')))
        if n_todo:
            lines.append(
                f'{name} ({fmt_id_display(pid)}): {n_todo} prose passage(s) marked '
                f'"(TODO: import source)" - still to be sourced.'
            )
        # A relationships: entry with no claim:/source: link, or one carrying
        # status: hypothesis, is a known relationship not yet sourced - listed the
        # same way as a provisional date, never a gate. A sourced claim supersedes it.
        for entry in (meta.get('relationships') or []):
            if not isinstance(entry, dict):
                continue
            status = str(entry.get('status', '')).strip().lower()
            sourced = bool(entry.get('claim') or entry.get('source'))
            if sourced and status != 'hypothesis':
                continue
            role = str(entry.get('type', '')).strip() or 'relationship'
            target = _friendly_to(entry.get('to'))
            tail = ' (hypothesis)' if status == 'hypothesis' else ''
            lines.append(
                f'{name} ({fmt_id_display(pid)}): {role} relationship to '
                f'{target or "(unnamed)"}{tail} - recorded but not yet sourced. '
                f'Link its claim:/source: when you find the evidence.'
            )
    return lines


# Byte-preserving IO for the fix modes comes from `_lib`
# (`read_text_exact` / `write_text_exact_atomic`). lint used to keep private
# copies of that pair here while the shared ones were still being added to
# `_lib.py`; they landed, lint was never switched over, and so lint alone
# missed the later upgrade from the truncating writer to the atomic one. The
# lesson is worth the comment: a private copy of shared code does not get the
# shared code's fixes. The fix modes are the wrong place to learn that - they
# rewrite person and source records in bulk and unattended, so nobody is
# watching when one goes wrong.


def _file_newline(text: str) -> str:
    """The newline convention of an exactly-read text: CRLF when any CRLF
    appears, else LF. Inserted lines copy the file's own style so surgery
    never leaves a file with mixed endings."""
    return '\r\n' if '\r\n' in text else '\n'


# A line that would read as a Markdown code fence (``` at any indent). Inside
# an unfenced claims section such a line either is a half-typed fence or is
# quoted evidence inside a claim value - both make the auto-wrap unsafe.
_FENCE_LOOKALIKE_RE = re.compile(r'^\s*```')


def _wrap_unfenced_claims(path: Path) -> tuple[str | None, str | None]:
    """Compute the ```yaml wrap for `path`'s unfenced `## Claims` content.

    Returns (new_text, None) when the wrap is verified sound, (None, reason)
    for a plain-language refusal, and (None, None) when there is nothing to
    wrap. Two guarantees the first version broke:

      - The fenced block must RE-READ to exactly the claims the unfenced
        reader parsed. That reader joins the section's lines and .strip()s
        the result - dedenting the FIRST line - before parsing, so the fence
        must carry that same dedented text: a tab-indented first item fenced
        verbatim was invalid YAML, and the W114 message had told the human
        to run exactly this fix. The wrap is also re-parsed end to end and
        refused on any mismatch, so a bad wrap can never reach disk.
      - No content line is ever deleted. A ``` line anywhere in the section
        (a claim value quoting a code block, or a half-typed fence) would
        terminate the new fence early, so those files are refused with the
        line number instead of the old behavior of silently dropping the
        lines from the human's evidence.
    """
    try:
        text = read_text_exact(path)
    except OSError:
        return None, None
    nl = _file_newline(text)
    m = re.search(r'(^##\s+Claims\s*\r?\n)(.*?)(?=^##\s|\Z)', text, re.S | re.M)
    if not m:
        return None, None
    content_lines = m.group(2).splitlines()
    for offset, ln in enumerate(content_lines):
        if _FENCE_LOOKALIKE_RE.match(ln):
            line_no = text[:m.start(2)].count('\n') + offset + 1
            return None, (
                f'line {line_no} of {path.name} has a ``` line inside the claims '
                f'section (a half-typed fence, or ``` quoted inside a claim '
                f'value). Wrapping automatically would cut the block short there, '
                f'so nothing was changed - add the ```yaml fence by hand: put '
                f'```yaml on the line above the first claim and ``` on the line '
                f'after the last one.')
    # Dedent exactly the way the unfenced reader does (join + strip), so the
    # fenced interior is the very text whose parse produced the W114 claims.
    yaml_text = '\n'.join(content_lines).strip()
    if not yaml_text:
        return None, None
    try:
        expected = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        expected = None
    tail = text[m.end():]
    sep = nl if tail.startswith('##') else ''
    fenced_body = yaml_text.replace('\n', nl)
    new_text = (text[:m.start()] + m.group(1)
                + f'```yaml{nl}{fenced_body}{nl}```{nl}' + sep + tail)
    # Verify before anyone writes: parse the new text the way read_record
    # will, and demand the identical claim list back.
    fm = FRONT_RE.match(new_text)
    cm = CLAIMS_RE.search(new_text[fm.end():] if fm else new_text)
    reread = None
    if cm is not None:
        try:
            reread = yaml.safe_load(cm.group(1))
        except yaml.YAMLError:
            reread = None
    if not isinstance(reread, list) or not isinstance(expected, list) \
            or reread != expected:
        return None, (
            f'wrapping the claims in {path.name} in a ```yaml fence did not '
            f'read back to the same claims, so nothing was changed - add the '
            f'fence by hand: put ```yaml on the line above the first claim '
            f'and ``` on the line after the last one.')
    return new_text, None


def _fix_claims_fence(
    registry: Registry,
    archive_root: Path,
    progress: list[str],
    changed: list[str],
    dry_run: bool = False,
) -> None:
    """Wrap every source whose `## Claims` content was read unfenced in a proper
    ```yaml fence. Previewed under --dry-run; never silently rewrites, and a
    file the wrap cannot make round-trip-safe is refused with the by-hand fix
    (in preview and live mode alike - a dry run must predict the refusal too)."""
    for sid, path in sorted(registry.unfenced_claim_sources.items()):
        wrapped, refusal = _wrap_unfenced_claims(path)
        if refusal:
            progress.append(f'--fix-claims-fence: {refusal}')
            continue
        if wrapped is None:
            continue
        rel = path.relative_to(archive_root)
        if dry_run:
            progress.append(f'--fix-claims-fence dry-run: would wrap the claims in {rel} in a ```yaml fence')
        else:
            write_text_exact_atomic(path, wrapped)
            progress.append(f'Wrapped claims fence: {rel}')
            changed.append(str(path))


def _slugify_segment(text: str) -> str:
    """Lowercase hyphen-slug for a source filename / alias (SPEC §13 slug grammar)."""
    s = re.sub(r'[^a-z0-9]+', '-', str(text).lower()).strip('-')
    return s or 'source'


def _person_filename_parts(name: str, fallback_slug: str) -> tuple[str, str]:
    """(surname, given) for the §13 person filename `{surname}__{given}_{P-id}`.

    Derived from the `name:` field - surname is the last word, given the rest -
    falling back to the hand-filename when there is no usable name. Letters only,
    so the generated filename matches the strict person grammar and lint won't
    immediately re-flag it.

    A trailing generational suffix (Jr, Sr, II, III, IV, V) is pulled off
    first via the shared `_lib.strip_generational_suffix` (issue #53) so
    `--fix-ids` never mints a hand-authored "Roy Eugene Dodson Jr" as
    `jr__roy_eugene_dodson` - the suffix rides at the end of the given slug
    instead, the same rule `_lib.stub_slug_name` applies for `fha person new`
    and `fha stubs --from-names`, so all three sites file a suffixed name the
    same way. A leftover single core token after the suffix is stripped ("Roy
    Jr") gets the same surname-less treatment `stub_slug_name` gives it - the
    suffix does not promote the one remaining word into a surname. The
    ORIGINAL single-token-with-no-suffix behaviour just below (surname =
    that word, given = 'unknown') is untouched - it predates this fix and is
    not part of it; SPEC §13's actual mononym convention (empty surname) is
    `stub_slug_name`'s job, not this function's."""
    def letters(word: str) -> str:
        return re.sub(r'[^a-z]+', '', word.lower())
    parts = [p for p in str(name).split() if letters(p)]
    core, suffix = strip_generational_suffix(parts)
    if len(core) >= 2:
        given_parts = core[:-1] + ([suffix] if suffix else [])
        return (letters(core[-1]) or 'unknown',
                '_'.join(letters(p) for p in given_parts) or 'unknown')
    if core:
        if suffix:
            given = f'{core[0]} {suffix}'
            return '', '_'.join(letters(p) for p in given.split()) or 'unknown'
        return letters(core[0]) or 'unknown', 'unknown'
    seg = re.sub(r'[^a-z]+', '', fallback_slug.lower())
    return (seg or 'unknown'), 'unknown'


def _yaml_alias_entry(value: str) -> str:
    """One alias, quoted for a YAML flow list when it isn't a plain-safe token.

    A verbatim filename stem ("Sam Rivera", "1950 census - Brooks household",
    or one containing a comma) would split or misparse unquoted inside
    `aliases: [...]`; JSON string quoting is valid YAML and escapes everything."""
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_\-]*', value):
        return value
    return json.dumps(value)


def _merge_aliases_into_frontmatter(
    fm_text: str, new_id: str, aliases: list[str], nl: str,
) -> tuple[str, int]:
    """Append missing alias entries to an EXISTING `aliases:` block, in place.

    Shipped templates SHIP an `aliases:` block (the placeholder-id teaching
    form), and hand authors write their own - so "a block already exists" must
    mean MERGE, never skip: skipping loses the old-filename aliases and every
    `[[old name]]` link dies on the §13 rename. Handles the two shapes a hand
    file uses: a block list (`- entry` item lines; new entries are appended
    after the last item at the same dash indent) and a flow list
    (`aliases: [a, b]`; new entries are spliced in before the `]`). Entries
    already present are not duplicated (compared lowercased, the way the
    resolve map compares). Any shape it does not recognize is left untouched.

    Returns (new_fm_text, n_entries_added) - the caller's "(old name kept as
    an alias)" note prints only when n > 0, so the message can never lie.
    """
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return fm_text, 0
    if not isinstance(parsed, dict):
        return fm_text, 0
    raw = parsed.get('aliases')
    if isinstance(raw, list):
        existing = [str(a) for a in raw if a is not None]
    elif raw is None:
        existing = []
    else:
        return fm_text, 0   # a scalar aliases: value - a hand form; leave it alone
    have = {a.strip().lower() for a in existing}
    have.add(normalize_id(new_id))
    to_add = [a for a in aliases if a.strip().lower() not in have]
    if not to_add:
        return fm_text, 0
    entries = [_yaml_alias_entry(a) for a in to_add]

    m = re.search(r'^aliases:[^\n]*$', fm_text, re.M)
    if not m:
        return fm_text, 0
    head_line = m.group(0)
    flow_open = re.match(r'^aliases:[ \t]*\[', head_line)
    if flow_open:
        # Splice before the list's CLOSING bracket, located as the LAST `]` on
        # the line - not the first. A first-`]` split (an earlier `[^\]]*` group)
        # stopped inside an embedded `[[wikilink]]` alias, mangling both the old
        # entry and the new one. rfind finds the real close even past `]]`.
        close = head_line.rfind(']')
        if close >= flow_open.end():
            open_part = head_line[:flow_open.end()]     # 'aliases: ['
            body = head_line[flow_open.end():close]      # existing entries verbatim
            suffix = head_line[close:]                   # '] ...trailing'
            joined = ', '.join(entries)
            new_body = f'{body}, {joined}' if body.strip() else joined
            new_line = open_part + new_body + suffix
            return fm_text[:m.start()] + new_line + fm_text[m.end():], len(to_add)
    if '[' in head_line:
        # A flow list whose `]` sits on a later line - a shape this surgery
        # does not edit. Leave the hand form alone rather than risk splicing
        # a block item into the middle of a flow list.
        return fm_text, 0

    # Block-list form: consume the contiguous `- item` lines after the head
    # and insert the new entries after the last one, copying its dash indent.
    # (match(pos) anchors at pos by itself - a leading ^ would only match at
    # position 0 without re.M and stop the walk after the first item.)
    pos = m.end()
    if pos < len(fm_text) and fm_text[pos] == '\n':
        pos += 1
    body = fm_text[pos:]
    # `[ \t]*` (not `[ \t]+`): a valid block list may sit at zero indent under
    # the key (`aliases:\n- old`). Requiring leading whitespace missed those,
    # then defaulted item_prefix to two spaces and inserted an indent-2 item
    # ahead of the indent-0 ones - mixed-indent frontmatter YAML rejects. Now we
    # copy whatever dash indent the existing items use (including none).
    item_re = re.compile(r'[ \t]*-[ \t]+[^\r\n]*\r?\n?')
    consumed = 0
    item_prefix = None
    while True:
        im = item_re.match(body, consumed)
        if not im:
            break
        if item_prefix is None:
            item_prefix = re.match(r'[ \t]*-[ \t]+', im.group(0)).group(0)
        consumed = im.end()
    insert_at = pos + consumed
    if item_prefix is None:
        item_prefix = '  - '
    if insert_at == len(fm_text) and not fm_text.endswith('\n'):
        # The block is the last thing in the frontmatter and its final line has
        # no newline (FRONT_RE's group excludes the one before ---): open one.
        return fm_text + ''.join(f'{nl}{item_prefix}{e}' for e in entries), len(to_add)
    new_lines = ''.join(f'{item_prefix}{e}{nl}' for e in entries)
    return fm_text[:insert_at] + new_lines + fm_text[insert_at:], len(to_add)


def _insert_id_and_aliases(
    text: str, new_id: str, aliases: list[str], nl: str = '\n',
) -> tuple[str, bool]:
    """Add `id:` and alias entries to a record's frontmatter (creating the
    frontmatter if the hand-author wrote none). Returns (new_text, kept_alias):
    kept_alias is True only when alias entries were actually written, so the
    caller's "(old name kept as an alias)" note can never lie.

    `aliases` carries every string the file used to be known by - the slugified
    stem and, when different, the stem as written - so an existing
    `[[Sam Rivera]]` link keeps resolving after the §13 rename; the new ID
    self-aliases (through the alias line here, or through the record's own
    `id:` field when a block already exists).

    A record copied from a shipped template arrives with the id already PRESENT
    as a placeholder (`id: P-__________`, plus the same token in `aliases:` -
    "paste the same code here too"). For those, the surgery is a same-type token
    rewrite across the frontmatter: the id: value and the placeholder alias both
    become the minted id, everything else on their lines (spacing, the teaching
    comments) survives byte-for-byte. Same-type only, so the person template's
    commented `[[S-…]]` examples are left for their own record's mint; and since
    underscores are not Crockford Base32 characters, no real id can ever match
    the placeholder pattern. Because templates also ship the `aliases:` block,
    an existing block is MERGED into (slug + verbatim stem appended, deduped),
    never skipped - the old skip silently dropped every old-name alias.

    `nl` is the file's own newline convention (from _file_newline): every line
    this surgery writes copies it, so an LF archive edited on Windows stays LF.
    """
    entries = ', '.join([new_id] + [_yaml_alias_entry(a) for a in aliases])
    alias_line = f'aliases: [{entries}]'
    fm = FRONT_RE.match(text)
    has_aliases = bool(fm) and re.search(r'^aliases:', fm.group(1), re.M)
    has_id = bool(fm) and re.search(r'^id:', fm.group(1), re.M)
    if fm and has_id:
        # Replace the existing blank `id:` line in-place rather than prepending
        # a duplicate key (last-key-wins in YAML would silently discard the new
        # value). A trailing comment on the blank line survives; so does a CR.
        # FRONT_RE group(1) excludes the final newline before ---, so reassemble.
        inner = re.sub(
            r'^id:[ \t]*((?:#[^\r\n]*)?\r?)$',
            lambda mm: f'id: {new_id}' + ('   ' if mm.group(1).startswith('#') else '') + mm.group(1),
            fm.group(1), flags=re.M)
        placeholder_re = re.compile(
            rf'(?<![A-Za-z0-9_]){re.escape(new_id[0])}-_{{4,}}(?![A-Za-z0-9_])', re.I)
        inner = placeholder_re.sub(new_id, inner)
        if has_aliases:
            inner, n_added = _merge_aliases_into_frontmatter(inner, new_id, aliases, nl)
            kept = n_added > 0
        else:
            inner = f'{alias_line}{nl}' + inner
            kept = True
        return f'---{nl}{inner}{nl}---{nl}' + text[fm.end():], kept
    if fm:
        # Frontmatter with no id: key. Insert the id first; aliases merge into
        # an existing block (a hand-author may keep one without any id) or a
        # fresh alias line is added right under the id.
        inner = fm.group(1)
        if has_aliases:
            inner, n_added = _merge_aliases_into_frontmatter(inner, new_id, aliases, nl)
            kept = n_added > 0
            inner = f'id: {new_id}{nl}' + inner
        else:
            inner = f'id: {new_id}{nl}{alias_line}{nl}' + inner
            kept = True
        return f'---{nl}{inner}{nl}---{nl}' + text[fm.end():], kept
    return f'---{nl}id: {new_id}{nl}{alias_line}{nl}---{nl}{nl}{text}', True


def _mint_write_problem(new_text: str, new_id: str) -> str | None:
    """Re-parse the frontmatter this surgery just built and confirm it is still
    a sound record before `--fix-ids` writes it. Mirrors the claims-edit re-parse
    guard: a corrupting rewrite must be a refusal that names the file, never a
    silent success. Returns a one-line problem description, or None when the
    result is safe to write. Checks that the frontmatter (a) parses as a YAML
    mapping, (b) carries the minted `id:`, and (c) if `aliases:` is present, that
    it parses as a plain list (an indent slip or a bracket splice inside an
    embedded `[[wikilink]]` turns the block into a scalar/None or raises)."""
    fm = FRONT_RE.match(new_text)
    if not fm:
        return 'the record lost its frontmatter block'
    try:
        parsed = yaml.safe_load(fm.group(1))
    except yaml.YAMLError as exc:
        return f'the rewritten frontmatter no longer parses ({exc.__class__.__name__})'
    if not isinstance(parsed, dict):
        return 'the rewritten frontmatter no longer reads as a mapping'
    if normalize_id(str(parsed.get('id') or '')) != normalize_id(new_id):
        return f'the minted id {new_id} did not land in the frontmatter'
    if 'aliases' in parsed and not isinstance(parsed.get('aliases'), (list, type(None))):
        return 'the aliases block was corrupted into a non-list value'
    return None


def _fix_mint_ids(
    registry: Registry,
    archive_root: Path,
    progress: list[str],
    changed: list[str],
    dry_run: bool = False,
) -> list[Path]:
    """Mint an ID for each hand-authored, id-less record, write it into the
    frontmatter, rename the file to the §13 grammar, and KEEP the old filename as
    an alias - both the slugified form and, when different, the stem exactly as
    written, so a human's existing `[[Sam Rivera]]` links keep resolving after the
    rename. Previewed under --dry-run; never an error, always an explicit, opt-in
    completion.

    Returns the source-record paths it minted (post-rename; the original path
    under --dry-run) so the claim-id half of --fix-ids can revisit them: an
    id-less source's claims never reached the registry (Pass 1 stops before
    claims parsing when there is no S-id), so the claim pass must re-read the
    completed files."""
    remaining: list[tuple[Path, str]] = []
    minted_sources: list[Path] = []
    for path, kind in registry.idless_records:
        # _never_mintable is re-checked here as defense in depth: a GENERATED
        # view or README must never gain frontmatter/a rename even if a stale
        # or hand-built registry entry claims otherwise.
        if not path.exists() or _never_mintable(path):
            continue
        new_id = mint_ids(kind, 1, archive_root)[0]
        slug = _slugify_segment(path.stem)
        aliases = [slug]
        if path.stem.lower() != slug:
            aliases.append(path.stem)
        try:
            text = read_text_exact(path)
        except OSError:
            remaining.append((path, kind))
            continue
        nl = _file_newline(text)
        if kind == 'P':
            name = str(read_record(path)['meta'].get('name', ''))
            surname, given = _person_filename_parts(name, path.stem)
            new_name = f'{surname}__{given}_{new_id}.md'
        else:
            new_name = f'{slug}_{new_id}.md'
        new_path = path.with_name(new_name)
        rel = path.relative_to(archive_root)
        new_rel = new_path.relative_to(archive_root)
        # A template copy still carries `id: {TYPE}-__________`; say so - "mint"
        # alone would not explain that the visible placeholder line gets rewritten.
        ph_note = (' (replacing the template placeholder id)'
                   if path in registry.placeholder_id_paths else '')
        if dry_run:
            progress.append(
                f'--fix-ids dry-run: would mint {new_id} for {rel}{ph_note}, '
                f'rename → {new_rel}, and keep the old name as an alias')
            remaining.append((path, kind))
            if kind == 'S':
                minted_sources.append(path)
            continue
        new_text, kept_alias = _insert_id_and_aliases(text, new_id, aliases, nl)
        problem = _mint_write_problem(new_text, new_id)
        if problem is not None:
            # Refuse rather than write a corrupt record. Name the file and the
            # reason; the human can add the id by hand. The unusable id we minted
            # is simply not used (mint_ids only advances a counter file).
            progress.append(
                f'--fix-ids: refused to mint {new_id} for '
                f'{path.relative_to(archive_root)} - {problem}; '
                'add the id by hand')
            remaining.append((path, kind))
            continue
        write_text_exact_atomic(path, new_text)
        if new_path != path and not new_path.exists():
            path.rename(new_path)
            changed.append(str(new_path))
            final_path = new_path
        else:
            new_rel = rel
            changed.append(str(path))
            final_path = path
        # "(old name kept as an alias)" prints only when alias entries were
        # actually written - an existing block that already carried them (or a
        # merge that had nothing to add) must not be reported as new work.
        alias_note = ' (old name kept as an alias)' if kept_alias else ''
        progress.append(f'Minted {new_id} for {new_rel}{ph_note}{alias_note}')
        if kind == 'S':
            minted_sources.append(final_path)
    registry.idless_records = remaining
    return minted_sources


def _claim_item_spans(text: str, start: int, end: int) -> list[tuple[int, int]] | None:
    """Split the claims region into one (start, end) span per top-level `- ` item.

    Item lines are recognized by the exact indent of the FIRST `- ` line, which
    cannot collide with anything nested: YAML puts a nested sequence at or below
    its key's indent, and keys sit deeper than the item dash, so every deeper
    `- ` (a roles list, a block-scalar line) is skipped. Returns None when the
    region has no items."""
    seg = text[start:end]
    indent: str | None = None
    starts: list[int] = []
    offset = 0
    for line in seg.splitlines(keepends=True):
        m = re.match(r'^([ \t]*)-([ \t]|\r?\n|$)', line)
        if m:
            if indent is None:
                indent = m.group(1)
            if m.group(1) == indent:
                starts.append(start + offset)
        offset += len(line)
    if not starts:
        return None
    spans = []
    for i, s in enumerate(starts):
        spans.append((s, starts[i + 1] if i + 1 < len(starts) else end))
    return spans


def _claim_id_missing(claim: dict) -> bool:
    """True when a claim has no usable id - absent, blank, or still the template
    placeholder (`C-__________`). The one definition of "mintable claim" shared by
    the E010 path implicitly (via _is_placeholder_id) and both --fix-ids halves,
    so the checks and the fixer can never disagree about which claims need ids."""
    raw = str(claim.get('id') or '').strip()
    return not raw or _is_placeholder_id(raw)


def _fix_mint_claim_ids(
    registry: Registry,
    archive_root: Path,
    progress: list[str],
    changed: list[str],
    dry_run: bool = False,
    extra_source_paths: list[Path] | None = None,
) -> None:
    """The claim half of --fix-ids: mint `id:` into claims that have none, and
    stamp `reviewed:` on the hand-accepted ones among them.

    WHY IT EXISTS: the quickstart kit teaches id-less claims - a legitimate
    by-hand starting state - but each was E010 with nothing minting it, so the
    by-hand → tools graduation dead-ended. This applies the AGENTS.md "linter
    mints on contact" doctrine (a record a human created with no ID yet is
    valid; the linter completes it) to claims. A claim still carrying the
    template's placeholder id (`C-__________`) counts as id-less too - the
    archive-template teaches that exact shape with a "a tool can fill it"
    comment - and its placeholder token is rewritten in place rather than a
    second id: line being inserted.

    WHY THE reviewed: STAMP: an accepted claim must carry a reviewed: date
    (E006), and a hand-written `status: accepted` is a decision the human has
    already made - TOOLING §3b: "the editing method does not matter, only that
    the decision is theirs", and directing the tool "is the human's accept",
    stamped today exactly as `fha claim --status accepted` stamps it. Scoped
    narrowly on purpose: only claims THIS run mints an id into; an accepted
    claim that already has an id keeps its E006 and the `fha claim` workflow.

    SURGERY, NEVER REGENERATION: edits are pure text insertions - `id:` right
    after the item's `- ` marker (the first field moves down one line, its
    bytes untouched), `reviewed:` right after the `status: accepted` line -
    so sibling claims, key order, quoting, and hand comments all survive.
    Anything the text scan cannot line up with the parsed claims (an item
    count mismatch, a one-line `- {...}` flow claim, a bare `-` item, an
    anchor-led `- &c1` item) is refused with a message naming the by-hand
    fix, never guessed at - and the whole rewritten file is re-parsed before
    the write (claims_edit_problem + a minted-ids-landed count), so a bad
    rewrite becomes a refusal instead of a corrupted source.
    """
    candidates: dict[Path, bool] = {}
    for sid in sorted(registry.source_claims):
        claims = registry.source_claims[sid]
        if any(isinstance(c, dict) and _claim_id_missing(c) for c in claims):
            p = registry.source_paths.get(sid)
            if p:
                candidates[p] = True
    for p in (extra_source_paths or []):
        candidates.setdefault(p, True)

    for path in candidates:
        if not path.exists() or _never_mintable(path):
            continue
        _mint_claim_ids_in_file(path, archive_root, progress, changed, dry_run)


def _mint_claim_ids_in_file(
    path: Path,
    archive_root: Path,
    progress: list[str],
    changed: list[str],
    dry_run: bool,
) -> None:
    """Mint ids (and reviewed: stamps) into one source file's id-less claims.

    Re-reads the file fresh rather than trusting the registry: --fix-ids may
    have just completed and renamed this very file (an id-less source's claims
    never reached the registry at all). See _fix_mint_claim_ids for the
    contract; this function is the per-file surgery.

    GUARDED SURGERY - the rules that keep a text edit off the human's evidence:
      - every scan is anchored to the item's OWN key lines (the mapping column
        claim_item_key_indent derives, or the dash line itself), so a
        `status: accepted` or `id:` LOOKALIKE inside a `value: |` scalar can
        never be stamped or rewritten;
      - a blank `id:` line (the key present, the value empty) is completed IN
        PLACE - inserting a second id: key would make YAML keep the last,
        blank, one: the mint would be silently void, E002 would persist, and
        every rerun would burn another id into the same claim;
      - an anchor-led item (`- &c1`) is refused: a field inserted above the
        anchor detaches it and the whole block stops parsing;
      - reads and writes are exact-newline, so an LF archive edited on Windows
        stays LF outside the edited spans, and inserted lines copy the file's
        own newline style;
      - before anything is written, the FULL rewritten text goes back through
        claims_edit_problem plus a parse-back check that every minted id
        actually landed on a claim. Any doubt is a per-file refusal that names
        the by-hand fix - so the success message can never lie.
    """
    try:
        rel = path.relative_to(archive_root)
    except ValueError:
        rel = path
    rec = read_record(path)
    claims = rec['claims']
    if not any(isinstance(c, dict) and _claim_id_missing(c) for c in claims):
        return
    try:
        text = read_text_exact(path)
    except OSError:
        progress.append(f'--fix-ids: could not read {rel}; its claims were left alone.')
        return
    nl = _file_newline(text)

    # Fenced blocks only: the write guard (claims_edit_problem) vets the
    # ```yaml form, and the unfenced W114 state has its own dedicated fixer.
    # Run in the same command, the fence fix lands first (run_lint order), so
    # a combined `--fix-claims-fence --fix-ids` still completes in one pass.
    fm = FRONT_RE.match(text)
    body_start = fm.end() if fm else 0
    cm = CLAIMS_RE.search(text[body_start:])
    if not cm:
        progress.append(
            f'--fix-ids: the claims in {rel} are not inside a ```yaml fence, so '
            f'no ids were minted there - run `fha lint --fix-claims-fence` '
            f'first, then run `fha lint --fix-ids` again.')
        return
    region = (body_start + cm.start(1), body_start + cm.end(1))
    spans = _claim_item_spans(text, *region)
    if not spans or len(spans) != len(claims):
        # The text scan and the parser disagree about the claim entries (an
        # entry that parses to nothing, prose bullets in the section, ...).
        # Refuse the whole file rather than risk inserting into the wrong claim.
        progress.append(
            f"--fix-ids: the claims block in {rel} doesn't line up with what the "
            f'parser reads, so nothing was changed there - add the missing id: '
            f'lines by hand (mint values with `fha id mint C`).')
        return

    # Plan first, mint second: refusals must not consume minted ids. Each plan
    # is an INSERT (`id:` added after the `- ` marker), a PLACEHOLDER rewrite
    # (`id: C-__________` token replaced on its line), or a BLANK completion
    # (`id:` with no value, completed on its existing line).
    base_indent = re.match(r'^[ \t]*', text[spans[0][0]:spans[0][1]]).group(0)
    plans: list[dict] = []
    deferred_notes: list[str] = []   # emitted only after a real, successful write
    today = _today()
    for i, ((span_start, span_end), claim) in enumerate(zip(spans, claims)):
        if not isinstance(claim, dict) or not _claim_id_missing(claim):
            continue
        span_text = text[span_start:span_end]
        key_indent = claim_item_key_indent(span_text.splitlines(), base_indent)
        # The item's OWN key lines: its dash line, or a line at exactly the
        # mapping's key column. Anything deeper is scalar content.
        dash_prefix = rf'{re.escape(base_indent)}-[ \t]+'
        key_prefix = rf'(?:{dash_prefix}|{re.escape(key_indent)})'
        label = str(claim.get('value', ''))[:40] or f'entry {i + 1}'
        plan = {'kind': None, 'insert_at': None, 'continuation': '',
                'replace_span': None, 'snippet_suffix': '',
                'stamp_at': None, 'stamp_text': ''}

        if str(claim.get('id') or '').strip():
            # A template placeholder id (the only non-blank mintable form):
            # rewrite just the token so spacing and teaching comment survive.
            pm = re.compile(
                rf'^{key_prefix}id:[ \t]*(C-_{{4,}})(?![A-Za-z0-9_])',
                re.I | re.M).search(span_text)
            if not pm:
                progress.append(
                    f'--fix-ids: could not find the placeholder id: line for claim '
                    f'"{label}" in {rel} - replace it by hand (`fha id mint C`).')
                continue
            plan['kind'] = 'placeholder'
            plan['replace_span'] = (span_start + pm.start(1), span_start + pm.end(1))
        elif 'id' in claim:
            # The id: key EXISTS but parses blank (`id:` alone, '', ~, null) -
            # the same blank _claim_id_missing sees. Complete that line in place.
            bm = re.compile(
                rf"^{key_prefix}id:(?P<val>[ \t]*(?:''|\"\"|~|[Nn]ull|NULL)?[ \t]*)"
                rf'(?P<tail>(?:#[^\r\n]*)?\r?)$',
                re.M).search(span_text)
            if not bm:
                progress.append(
                    f'--fix-ids: claim "{label}" in {rel} has an id: line written in '
                    f'a form this fix cannot complete safely - paste a code onto it '
                    f'by hand (mint one with `fha id mint C`).')
                continue
            plan['kind'] = 'blank'
            plan['replace_span'] = (span_start + bm.start('val'), span_start + bm.end('val'))
            plan['snippet_suffix'] = ' ' if bm.group('tail').startswith('#') else ''
        else:
            nl_at = span_text.find('\n')
            first_line = span_text[:nl_at if nl_at != -1 else len(span_text)].rstrip('\r')
            dm = re.match(rf'^{re.escape(base_indent)}-[ \t]+', first_line)
            rest = first_line[dm.end():] if dm else ''
            lead = rest.lstrip()[:1]
            if lead in ('&', '*', '!'):
                # A YAML anchor (&c1), alias (*c1) or tag on the dash line: an
                # id: inserted above it detaches the marker from its node and
                # the WHOLE block stops parsing - every claim in the source
                # would vanish. Refuse the item and name the by-hand fix.
                progress.append(
                    f'--fix-ids: claim "{label}" in {rel} starts with a YAML '
                    f'anchor/alias marker ({rest.strip().split()[0]}), which this '
                    f'fix cannot edit safely - mint a code with `fha id mint C` and '
                    f'paste it onto an `id:` line inside that claim by hand.')
                continue
            if not dm or not rest.strip() or rest.lstrip().startswith('{'):
                # A bare `-` item or a one-line `- {...}` flow claim: inserting a
                # block field would corrupt it. Name the claim and the by-hand fix.
                progress.append(
                    f'--fix-ids: claim "{label}" in {rel} is written in a one-line form '
                    f'this fix cannot edit safely - add its id: by hand (`fha id mint C`).')
                continue
            plan['kind'] = 'insert'
            plan['insert_at'] = span_start + dm.end()
            plan['continuation'] = base_indent + ' ' * (dm.end() - len(base_indent))

        if str(claim.get('status', '')) == 'accepted' and not str(claim.get('reviewed') or '').strip():
            sm = re.compile(
                rf'^{key_prefix}status:[ \t]*([\'"]?)accepted\1[ \t]*(#.*)?\r?$',
                re.M).search(span_text)
            if sm:
                stamp_nl = span_text.find('\n', sm.end())
                if stamp_nl != -1:
                    plan['stamp_at'] = span_start + stamp_nl + 1
                    plan['stamp_text'] = f'{key_indent}reviewed: {today}{nl}'
                else:
                    # status: is the last line of the region with no newline of
                    # its own - open a fresh line before stamping.
                    plan['stamp_at'] = span_end
                    plan['stamp_text'] = f'{nl}{key_indent}reviewed: {today}{nl}'
            else:
                deferred_notes.append(
                    f'--fix-ids: could not find the status: accepted line for claim '
                    f'"{label}" in {rel} - its id was minted, but add reviewed: {today} '
                    f'by hand (or run `fha claim <C-id> --status accepted`).')
        plans.append(plan)

    if not plans:
        return
    n_stamped = sum(1 for p in plans if p['stamp_at'] is not None)
    n_placeholder = sum(1 for p in plans if p['kind'] == 'placeholder')
    ph_note = (f' ({n_placeholder} of them replacing template placeholder ids)'
               if n_placeholder else '')
    if dry_run:
        line = f'--fix-ids dry-run: would mint {len(plans)} claim id(s) in {rel}{ph_note}'
        if n_stamped:
            line += (f' and stamp reviewed: {today} on {n_stamped} hand-accepted '
                     f'claim(s) (the accepted status is already your decision on record)')
        progress.append(line)
        return

    new_ids = mint_ids('C', len(plans), archive_root)
    # Edits are (start, end, snippet): an insertion is a zero-width span, a
    # placeholder/blank rewrite replaces exactly its value span. Applied
    # bottom-up so earlier offsets stay valid; spans never overlap (each lives
    # in its own claim item, and a stamp inserts at a line boundary).
    edits: list[tuple[int, int, str]] = []
    for plan, cid in zip(plans, new_ids):
        if plan['kind'] == 'placeholder':
            start, end = plan['replace_span']
            edits.append((start, end, cid))
        elif plan['kind'] == 'blank':
            start, end = plan['replace_span']
            edits.append((start, end, f' {cid}' + plan['snippet_suffix']))
        else:
            edits.append((plan['insert_at'], plan['insert_at'],
                          f'id: {cid}{nl}{plan["continuation"]}'))
        if plan['stamp_at'] is not None:
            edits.append((plan['stamp_at'], plan['stamp_at'], plan['stamp_text']))
    new_text = text
    for start, end, snippet in sorted(edits, key=lambda t: t[0], reverse=True):
        new_text = new_text[:start] + snippet + new_text[end:]

    # The write guard: any doubt about the rewrite = a refusal, never a write.
    # claims_edit_problem re-parses the block the way read_record will; the
    # parse-back count proves each minted id actually sits on a claim, so the
    # "Minted N claim id(s)" line below can never overstate what happened.
    problem = claims_edit_problem(new_text)
    if problem is None:
        fm2 = FRONT_RE.match(new_text)
        cm2 = CLAIMS_RE.search(new_text[fm2.end():] if fm2 else new_text)
        parsed = None
        if cm2 is not None:
            try:
                parsed = yaml.safe_load(cm2.group(1))
            except yaml.YAMLError:
                parsed = None
        landed = {normalize_id(str(c.get('id') or ''))
                  for c in (parsed if isinstance(parsed, list) else [])
                  if isinstance(c, dict)}
        n_landed = sum(1 for cid in new_ids if normalize_id(cid) in landed)
        if n_landed != len(new_ids):
            problem = (f'only {n_landed} of the {len(new_ids)} minted id(s) '
                       f'read back on a claim afterwards')
    if problem is not None:
        progress.append(
            f'--fix-ids: minting claim ids into {rel} was stopped before writing '
            f'anything - {problem}. The file is unchanged; add the id: lines by '
            f'hand instead (mint codes with `fha id mint C`).')
        return

    write_text_exact_atomic(path, new_text)
    changed.append(str(path))
    progress.append(f'Minted {len(plans)} claim id(s) in {rel}{ph_note}')
    if n_stamped:
        progress.append(
            f'Stamped reviewed: {today} on {n_stamped} hand-accepted claim(s) in {rel} '
            f"(a hand-written 'accepted' is your decision; the stamp records when the "
            f'tools met it)')
    progress.extend(deferred_notes)


def _fix_mint_stubs(
    registry: Registry,
    archive_root: Path,
    progress: list[str],
    changed: list[str],
    dry_run: bool = False,
) -> None:
    """Create missing person stubs (E005 set) in people/stubs/. Respects dry_run.

    Per the structured-result contract, the "Created stub:" / "Would create stub:"
    lines accumulate in `progress` (rendered later by `_cmd_lint`) rather than
    printing here, and each real write is recorded in `changed`.
    """
    stubs_dir = archive_root / 'people' / 'stubs'

    # Collect pids that appear in claims but have no record. Resolution via
    # registry.alias_map keeps this the exact E005 set: wrapped bare IDs
    # unwrap, and an unresolvable NAME is inert (a stub can't be minted for a
    # name here - that is `fha stubs --from-names`, a deliberate human step).
    missing: set[str] = set()
    for sid, claims in registry.source_claims.items():
        for claim in claims:
            for ppid in _claim_person_ids(claim, registry.alias_map):
                if not registry.has_person(ppid):
                    missing.add(ppid)

    for ppid in sorted(missing):
        stub_path = stubs_dir / f'unknown__unknown_{ppid}.md'
        if stub_path.exists():
            continue
        if dry_run:
            progress.append(f'Would create stub: people/stubs/unknown__unknown_{ppid}.md')
        else:
            stubs_dir.mkdir(parents=True, exist_ok=True)
            stub_content = (
                f'---\nid: {ppid}\n'
                f'name: unknown\n'
                f'living: unknown\n'
                f'created: {_today()}\n'
                f'tier: stub\n---\n'
            )
            # Exact + atomic like every other record write: a stub is a real
            # person record from the moment it lands, and this loop creates them
            # in bulk with nobody watching.
            write_text_exact_atomic(stub_path, stub_content)
            progress.append(f'Created stub: {stub_path.relative_to(archive_root)}')
            changed.append(str(stub_path))


def _fix_spawn_questions(
    registry: Registry,
    findings: list[Finding],
    archive_root: Path,
    progress: list[str],
    changed: list[str],
    dry_run: bool = False,
) -> None:
    """Append templated questions for E009 contradictions. Respects dry_run.

    Like the other fix modes, progress text accumulates in `progress` and the
    written questions.md is recorded in `changed`, leaving `_cmd_lint` the only
    layer that prints.
    """
    questions_path = archive_root / 'notes' / 'questions.md'
    to_spawn = [f for f in findings if f.code == 'E009']
    if not to_spawn:
        return
    if dry_run:
        progress.append(f'Would append {len(to_spawn)} question(s) to notes/questions.md')
        return
    (archive_root / 'notes').mkdir(parents=True, exist_ok=True)
    existing = read_text_exact(questions_path) if questions_path.exists() else ''
    appended = []
    for f in to_spawn:
        appended.append(
            f'\n## Q: Contradiction: {f.message}\n'
            f'- origin: tool\n- status: open\n- refs: []\n'
            f'- context:\n  - (tool, {_today()}) Auto-spawned by fha lint E009.\n'
        )
    if appended:
        # The question log is the human's research trail. Appending rewrites it
        # whole, so a torn write would trade every logged question for the new
        # ones - and this runs unattended under --fix.
        write_text_exact_atomic(
            questions_path,
            reapply_newline(existing + '\n'.join(appended), existing))
        progress.append(f'Appended {len(appended)} question(s) to {questions_path.relative_to(archive_root)}')
        changed.append(str(questions_path))


def _format_mirror_entry(
    owner_pid: str, owner_name: str, role: str, subtype: str, claim_id: str,
) -> list[str]:
    """The YAML list-item lines for a mirror relationship entry pointing back at
    the person who already records the edge. Pinned `[[P-id|Name]]` so it reads
    and resolves; subtype omitted when there is nothing to say.

    `owner_name` is an EXISTING person's `name:` field, not a value this
    fixer validated - a human may have typed a quote into it long before
    `--fix-reciprocal` ran. Escaped for the double-quoted YAML scalar it
    lands in (same fix/reasoning as `person._relationship_item_lines`, PR
    #30 review sweep) rather than left to splice in raw.
    """
    escaped_name = owner_name.replace('\\', '\\\\').replace('"', '\\"')
    lines = [
        f'  - to: "[[{fmt_id_display(owner_pid)}|{escaped_name}]]"',
        f'    type: {role}',
    ]
    if subtype:
        # Free text (same field person._relationship_item_lines quotes): a
        # ': ' or ' #' typed into a claim's subtype long ago must not corrupt
        # the mirrored record's frontmatter here either.
        lines.append(f'    subtype: {yaml_inline(subtype)}')
    if claim_id:
        lines.append(f'    claim: "[[{fmt_id_display(claim_id)}]]"')
    return lines


def _append_relationship_entry(text: str, item_lines: list[str]) -> str | None:
    """Insert a relationships list-item into a person record's frontmatter.

    Additive text surgery: appends to an existing block (a `relationships:` key
    followed by indented items) or creates one just before the closing `---`.
    Returns None when the frontmatter is missing or `relationships:` is written
    in a form we won't safely edit (e.g. inline `relationships: [...]`), so the
    caller can report it rather than corrupt the file."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != '---':
        return None
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if close is None:
        return None

    rel_idx = None
    for i in range(1, close):
        if re.match(r'^relationships:\s*(#.*)?$', lines[i]):
            rel_idx = i
            break
        if re.match(r'^relationships:\s*\S', lines[i]):
            return None     # inline form - refuse rather than create a duplicate key

    if rel_idx is None:
        lines[close:close] = ['relationships:'] + list(item_lines)
    else:
        end = rel_idx + 1
        while end < close and (lines[end].startswith(' ') or lines[end].startswith('\t')):
            end += 1
        lines[end:end] = list(item_lines)

    result = '\n'.join(lines)
    if text.endswith('\n'):
        result += '\n'
    return result


def _fix_reciprocal(
    registry: Registry,
    archive_root: Path,
    progress: list[str],
    changed: list[str],
    dry_run: bool = False,
) -> None:
    """W116 fix: append each missing mirror entry to the other person's
    relationships: block. Additive only (never overwrites human text), previewed
    under --dry-run, and conflict-safe - the W116 pass already confirmed the
    mirror is absent, and a person with no record is reported, not invented."""
    seen: set[tuple[str, str, str]] = set()
    for m in registry.missing_mirrors:
        key = (m['other_pid'], m['claim_id'], m['mirror_role'])
        if key in seen:
            continue
        seen.add(key)
        other_pid = m['other_pid']
        owner_name = str(registry.person_meta.get(m['owner_pid'], {}).get('name')
                         or fmt_id_display(m['owner_pid']))
        paths = registry.person_profile_paths.get(other_pid)
        if not paths:
            progress.append(
                f"--fix-reciprocal: {fmt_id_display(other_pid)} has no person record to hold the "
                f"mirror - run `fha stubs` first; skipped.")
            continue
        path = paths[0]
        rel = path.relative_to(archive_root)
        if dry_run:
            progress.append(
                f"--fix-reciprocal dry-run: would add a '{m['mirror_role']}' edge to "
                f"{owner_name} (claim {fmt_id_display(m['claim_id'])}) in {rel}")
            continue
        try:
            text = read_text_exact(path)
        except OSError:
            progress.append(f"--fix-reciprocal: could not read {rel}; skipped.")
            continue
        item = _format_mirror_entry(m['owner_pid'], owner_name, m['mirror_role'],
                                    m['subtype'], m['claim_id'])
        new_text = _append_relationship_entry(text, item)
        if not new_text or new_text == text:
            progress.append(
                f"--fix-reciprocal: couldn't safely place the mirror in {rel} "
                f"(its relationships: block isn't a simple list) - add it by hand.")
            continue
        # A person record, edited in a loop over every missing mirror edge:
        # atomic so one failure costs one skipped edge, not one lost ancestor.
        write_text_exact_atomic(path, reapply_newline(new_text, text))
        changed.append(str(path))
        progress.append(
            f"Added reciprocal '{m['mirror_role']}' edge to {owner_name} "
            f"(claim {fmt_id_display(m['claim_id'])}) in {rel}")


def _today() -> str:
    return datetime.date.today().isoformat()


def run_lint_silent(
    archive_root: Path,
    fha_config: dict,
) -> tuple[int, int, list[Finding]]:
    """Run lint core passes without output. Returns (n_errors, n_warnings, e018_findings).

    Used by fha doctor to embed a lint summary in the health report.
    Delegates to _run_lint_core so any new core pass is automatically reflected here.
    """
    if not (archive_root / 'fha.yaml').exists():
        return (1, 0, [])
    findings, _ = _run_lint_core(archive_root, fha_config)
    n_errors = sum(1 for f in findings if f.severity == 'E')
    n_warnings = sum(1 for f in findings if f.severity == 'W')
    e018 = [f for f in findings if f.code == 'E018']
    return n_errors, n_warnings, e018


# ── CLI ───────────────────────────────────────────────────────────────────────

# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Check the archive for problems and report them (a spell-check for your data).

  fha lint                Walk and check the archive; report only
  fha check               The same command, plainer name
  fha lint --fix-ids      Give IDs to records you named in plain English
  fha lint --mint-stubs   Create placeholder records for people named but not filed

Catches broken links, missing dates, contradictions, and people referenced but
never filed. Run it any time something feels off. Exit: 0 clean, 1 warnings,
2 errors, 3 tool failure."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'lint',
        aliases=['check'],
        help='Check the archive for problems and report them (alias: `fha check`)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH',
                   help='Archive root (overrides auto-detection)')
    p.add_argument('--spec-root', metavar='PATH',
                   help='Spec docs root (when separate from archive root)')
    p.add_argument('--with-exif', action='store_true',
                   help='Also verify embedded SOURCE: keywords via exiftool (slow)')
    p.add_argument('--json', action='store_true', dest='use_json',
                   help='Machine-readable JSON output')
    p.add_argument('--format-check', action='store_true',
                   help='Check formatting only (no fixes)')
    p.add_argument('--format-write', action='store_true',
                   help='Apply conservative formatting fixes')
    p.add_argument('--dry-run', action='store_true',
                   help='Preview mutating operations without writing')
    p.add_argument('--mint-stubs', action='store_true',
                   help='Create person stubs for E005 set')
    p.add_argument('--spawn-questions', action='store_true',
                   help='Append questions to notes/questions.md for E009 contradictions')
    p.add_argument('--fix-claims-fence', action='store_true',
                   help='Wrap hand-written claims that forgot the ```yaml fence (with --dry-run to preview)')
    p.add_argument('--fix-ids', action='store_true',
                   help='Mint IDs for hand-authored id-less records (rename, keep the old name '
                        'as an alias) and for id-less claims inside sources (with --dry-run to preview)')
    p.add_argument('--fix-reciprocal', action='store_true',
                   help='Add the missing mirror edge for each W116 (with --dry-run to preview)')
    # NOTE: the real E011 fixer shipped as the standalone `fha reconcile`
    # (reconcile.py, TOOLING §9) - deliberately NOT re-added here as a fix
    # flag: one owner per mutation, and E011's message names the tool.
    # Original note kept below for the history of the removal:
    # --fix-inventory (an E011 documents-inventory fixer) was removed while
    # unimplemented - a flag that only printed a warning taught users flags might
    # be decorative. Re-add it here with the real fixer when it is built.
    p.set_defaults(func=_run_lint)


def _run_lint(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    spec_root = getattr(args, 'spec_root', None)

    result = run_lint(
        archive_root=archive_root,
        fha_config=fha_config,
        with_exif=getattr(args, 'with_exif', False),
        format_check=getattr(args, 'format_check', False),
        format_write=getattr(args, 'format_write', False),
        dry_run=getattr(args, 'dry_run', False),
        mint_stubs=getattr(args, 'mint_stubs', False),
        spawn_questions=getattr(args, 'spawn_questions', False),
        fix_claims_fence=getattr(args, 'fix_claims_fence', False),
        fix_ids=getattr(args, 'fix_ids', False),
        fix_reciprocal=getattr(args, 'fix_reciprocal', False),
        spec_root=Path(spec_root) if spec_root else None,
    )
    return _cmd_lint(result, archive_root, use_json=getattr(args, 'use_json', False))


# ── Standalone ────────────────────────────────────────────────────────────────

def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha lint',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH')
    parser.add_argument('--spec-root', metavar='PATH')
    parser.add_argument('--with-exif', action='store_true')
    parser.add_argument('--json', action='store_true', dest='use_json')
    parser.add_argument('--format-check', action='store_true')
    parser.add_argument('--format-write', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--mint-stubs', action='store_true')
    parser.add_argument('--spawn-questions', action='store_true')
    parser.add_argument('--fix-claims-fence', action='store_true')
    parser.add_argument('--fix-ids', action='store_true')
    parser.add_argument('--fix-reciprocal', action='store_true')
    args = parser.parse_args(argv)
    return _run_lint(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
