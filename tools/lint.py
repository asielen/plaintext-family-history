#!/usr/bin/env python3
"""
lint.py - fha lint: verify the archive against the spec.

  fha lint [--root PATH]             Walk and check the archive; report-only
  fha lint --with-exif               Also verify embedded SOURCE: keywords (slow)
  fha lint --json                    Machine-readable JSON output
  fha lint --format-check            Check formatting without fixing
  fha lint --format-write            Planned formatter write mode (not yet implemented)
  fha lint --mint-stubs              Create missing person stubs (E005 set)
  fha lint --spawn-questions         Append questions for E009 contradictions
  fha lint --fix-ids                 Complete hand-authored id-less records AND
                                     id-less claims (mint, rename, alias, stamp);
                                     template placeholder ids (P-__________)
                                     count as missing and are replaced in place
  fha lint --fix-reciprocal          Add the missing mirror edge for each W116
  fha lint --fix-inventory           Planned inventory fixer (not yet implemented)

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
    edtf_bounds,
    finding_to_message,
    format_bracket_child,
    is_genetic_parent_subtype,
    nonbirth_bracket_label,
    extract_token_ids,
    extract_wikilinks,
    link_field_refs,
    resolve_ref,
    strip_link_wrapper,
    fmt_id_display,
    format_edtf_error,
    format_exiftool_error,
    format_source_type_error,
    id_type_of,
    is_fixture_path,
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
    resolve_path,
    resolve_root_arg,
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
#    _parse_summary_block        - parse **Born/Died/…:** lines from a profile body
#    _edtf_gloss                 - plain-language gloss for a canonical EDTF value
#    _check_date_value           - forgiving date check: valid/loose-W109/broken-E014
#    _collect_token_refs         - scan a text block for [ID] tokens → registry
#    _research_hypothesis_ids    - H-ids defined in a research file's ## Hypotheses
#    _question_blocks            - split a questions.md into per-heading blocks
#    _metadata_values            - normalise scalar/list exiftool field values
#
#  Pass 1 - walk and collect
#    _walk_archive               - top-level coordinator; calls the _process_* functions
#    _process_person_file        - index one person file + file-level checks
#    _process_source_file        - index one source file + file-level checks + claims
#
#  Bracket / Ahnentafel checks (W103, W110)
#    _build_child_edges          - parent → {child → {nature,…}} from accepted claims
#    _build_children_of          - parent → {children}; genetic_only filters numbering
#    _check_bracket_lists        - W103: stale couple-folder bracket lists
#    _build_ahnentafel_lint      - BFS from root_person using in-memory registry
#    _check_ahnentafel_placement - W110: person file in wrong Ahnentafel folder
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
#
#  Format checks / fix modes
#    _check_format               - W109: final newline, CRLF line endings
#    _fix_format                 - apply conservative format fixes
#    _fix_mint_ids               - complete id-less records: mint + rename + alias
#    _claim_id_missing           - absent/blank/placeholder claim id = mintable
#    _fix_mint_claim_ids         - complete id-less claims: mint id, stamp reviewed
#    _claims_text_region / _claim_item_spans - locate claims YAML for text surgery
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
    contributes nothing. Mirrors index.py's _resolve_claim_ref(want='P') so
    lint and the index agree on which persons a claim names."""
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
    people_root = archive_root / 'people'
    if people_root.exists():
        for path in sorted(people_root.rglob('*.md')):
            _process_person_file(path, registry, findings)
            # Collect research file content for E009
            if '_research_' in path.stem or path.stem.endswith('_research'):
                try:
                    registry.research_content[path] = path.read_text(encoding='utf-8')
                except OSError:
                    pass

    # Sources
    sources_root = archive_root / 'sources'
    if sources_root.exists():
        for path in sorted(sources_root.rglob('*.md')):
            _process_source_file(path, registry, findings)

    # Notes FTS (for token refs)
    notes_root = archive_root / 'notes'
    if notes_root.exists():
        for path in sorted(notes_root.rglob('*.md')):
            try:
                text = path.read_text(encoding='utf-8')
                _collect_token_refs(text, path, registry)
                # Collect H-ids from notes
                for m in ID_RE.finditer(text):
                    if m.group(1).upper() == 'H':
                        registry.hypothesis_ids.add(normalize_id(m.group(0)))
            except OSError:
                pass


def _process_person_file(path: Path, registry: Registry, findings: list[Finding]) -> None:
    """Process one person file into the registry, with file-level checks."""
    if is_template_file(path):
        return   # `_TEMPLATE.*` is a teaching template, not a record
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

    # Determine kind from filename
    stem = path.stem
    parsed = parse_filename(path)
    is_companion = parsed and parsed.get('is_companion', False)
    kind = (parsed or {}).get('kind', 'profile')

    # H-ids defined in this file's ## Hypotheses section (SPEC §16 homes them in
    # `…_research_P-….md`). Same stem test index.py uses to pick research files,
    # applied before any id checks so a mid-graduation (id-less) research file's
    # hypotheses still count as existing records for E004.
    if '_research_' in stem or stem.endswith('_research'):
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
        return   # can't do further cross-reference checks without record metadata

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
                    f'Inventory file not found on disk: {file_path_str!r}'))
    registry.source_inventory[sid] = inventory_paths

    # W102: suggested-claim backlog
    suggested = [c for c in claims if str(c.get('status', '')) == 'suggested']
    if suggested:
        findings.append(Finding('W', 'W102', path,
            f'{len(suggested)} suggested claim(s) awaiting review'))

    # Collect token refs
    _collect_token_refs(rec['body'], path, registry)


# ── Bracket and Ahnentafel checks (W103, W110) ───────────────────────────────

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

        if sorted(current_names) != sorted(derived_names):
            findings.append(Finding('W', 'W103',
                people_dir / folder_name,
                f'stale bracket list [{" + ".join(sorted(current_names))}] '
                f'-> [{" + ".join(derived_names)}]; '
                f'run `fha views brackets --fix` to update'))


def _build_ahnentafel_lint(
    root_pid: str, children_of: dict[str, set[str]], registry: Registry
) -> dict[str, int]:
    """BFS from root_pid → {person_id: Ahnentafel position} using in-memory data.

    Same algorithm as _build_ahnentafel_map in views.py, but works from the
    in-memory registry rather than the SQLite relationships table.  Parents are
    determined by inverting children_of: a person P is a parent of Q if Q is
    in children_of[P].

    Determinism on same-sex / unknown pairs: lex-first P-id takes the even slot.
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
        else:
            # Take at most 2 parents; ignore additional (data quality issue)
            p1, p2 = parent_pids[0], parent_pids[1]
            s1 = str(registry.person_meta.get(p1, {}).get('sex', 'U') or 'U')
            s2 = str(registry.person_meta.get(p2, {}).get('sex', 'U') or 'U')
            if s1 == 'M' and s2 != 'M':
                father, mother = p1, p2
            elif s2 == 'M' and s1 != 'M':
                father, mother = p2, p1
            elif s1 == 'F' and s2 != 'F':
                mother, father = p1, p2
            elif s2 == 'F' and s1 != 'F':
                mother, father = p2, p1
            else:
                sorted_pair = sorted([p1, p2])
                father, mother = sorted_pair[0], sorted_pair[1]
            for pp, pos in [(father, 2 * n), (mother, 2 * n + 1)]:
                if pp not in pid_to_pos:
                    pid_to_pos[pp] = pos
                    queue.append((pp, pos))

    return pid_to_pos


def _check_ahnentafel_placement(registry: Registry, findings: list[Finding]) -> None:
    """W110: direct-line person files in the wrong couple folder.

    Requires root_person in fha.yaml.  Builds the Ahnentafel map from the
    in-memory registry, then verifies every direct-line person's profile files
    live in the couple folder whose numeric prefix equals their expected position
    (or position−1 if they hold the odd/mother slot).

    Skips persons in people/connections/ or people/stubs/.
    """
    root_person_raw = registry.fha_config.get('root_person')
    if not root_person_raw:
        return

    root_pid = normalize_id(str(root_person_raw))
    if not registry.has_person(root_pid):
        findings.append(Finding('W', 'W110', registry.archive_root / 'fha.yaml',
            f'root_person {root_pid!r} has no person record - '
            'Ahnentafel placement checks (W110) skipped; '
            'fix root_person in fha.yaml or run fha stubs'))
        return
    # Ahnentafel numbering follows only the genetic pedigree (SPEC §12.2); social
    # and legal parent edges are shown in the bracket list but never numbered.
    children_of = _build_children_of(registry, genetic_only=True)
    pid_to_pos = _build_ahnentafel_lint(root_pid, children_of, registry)

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
    place-name clashes are out of scope here (the index carries those)."""
    records: list[dict] = []
    for pid, meta in registry.person_meta.items():
        records.append({
            'id': pid,
            'name': meta.get('name'),
            'name_variants': _variant_values(meta.get('name_variants')),
            'aliases': meta.get('aliases') or [],
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
    the quickstart's form), so role matching agrees with _claim_person_ids."""
    val = (claim.get('roles') or {}).get(role)
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


def _claim_backs_edge(
    claim: dict, owner_pid: str, other_pid: str | None, role: str,
    alias_map: dict[str, str] | None = None,
) -> bool:
    """True if `claim` is an accepted relationship/marriage claim that records the
    edge a person-doc entry asserts. When `other_pid` is None (the `to:` name has
    no minted record yet) only the owner's side is checked, so a forgiving name
    never produces a false reconciliation failure. `alias_map` lets name-linked
    persons:/roles: entries back an edge the same as bare P-ids."""
    if str(claim.get('status', '')) != 'accepted':
        return False
    ctype = str(claim.get('type', ''))
    if role == 'spouse' and ctype == 'marriage':
        persons = set(_claim_person_ids(claim, alias_map))
        return owner_pid in persons and (other_pid is None or other_pid in persons)
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
        return 'spouse' if pid in _claim_person_ids(claim, alias_map) else None
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
    their block) is also W115, so an opted-in block stays complete."""
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
                if str(claim.get('type', '')) not in ('relationship', 'marriage'):
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
            # is fine; an unresolved name is an inert note-link, not a finding.
            for link_type in ('corroborates', 'contradicts'):
                for t in link_field_refs(claim.get(link_type)):
                    if id_type_of(t):
                        tid = normalize_id(t)
                        if tid not in registry.claim_ids and tid not in known_ids:
                            findings.append(Finding('E', 'E004', src_path,
                                f'Claim {claim.get("id","?")} {link_type}: {tid} not found - '
                                f'fix the ID, or point it at an existing claim.'))

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
        claimed_types = {str(c.get('type', '')) for c in person_claims}

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
    _check_ahnentafel_placement(registry, findings)

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


def _check_embedded_source_keywords(registry: Registry, findings: list[Finding]) -> None:
    """E012 and photo-side E011 checks using exiftool keyword reads."""
    scan_paths: set[Path] = set()
    path_aliases: dict[Path, str] = {}

    for alias in ('documents', 'photos'):
        root = _mapped_root(alias, registry)
        if root.exists():
            for file_path in (p for p in root.rglob('*') if p.is_file()):
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
        text = path.read_text(encoding='utf-8')
    except OSError:
        return
    fixed = text.replace('\r\n', '\n')
    if fixed and not fixed.endswith('\n'):
        fixed += '\n'
    if fixed != text:
        if dry_run:
            progress.append(f'Would fix formatting: {path.name}')
        else:
            path.write_text(fixed, encoding='utf-8')
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
    fix_inventory: bool = False,
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
    if fix_inventory:
        if dry_run:
            progress.append('--fix-inventory dry-run: would scan documents root and update files: blocks for E011 set')
        else:
            progress.append('WARNING: --fix-inventory is not yet implemented.')
            progress.append('         Run `fha process` on each document to update its source record.')
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
    field, so the needs-sourcing backlog stops listing that field once one exists."""
    out: set[tuple[str, str]] = set()
    for claims in registry.source_claims.values():
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            ctype = str(claim.get('type', ''))
            if ctype in PROVISIONAL_VITAL_FIELDS and str(claim.get('status', '')) == 'accepted':
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


def _wrap_unfenced_claims(path: Path) -> str | None:
    """Return `path`'s text with the unfenced `## Claims` content wrapped in a
    ```yaml fence, or None if there is nothing to wrap. Text surgery only - the
    YAML the human typed is preserved verbatim, just fenced."""
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return None
    m = re.search(r'(^##\s+Claims\s*\r?\n)(.*?)(?=^##\s|\Z)', text, re.S | re.M)
    if not m:
        return None
    content = m.group(2)
    fence_line = re.compile(r'^\s*```[a-zA-Z]*\s*$')
    yaml_text = '\n'.join(
        ln for ln in content.splitlines() if not fence_line.match(ln)
    ).strip('\n')
    if not yaml_text.strip():
        return None
    tail = text[m.end():]
    sep = '\n' if tail.startswith('##') else ''
    new_section = m.group(1) + f'```yaml\n{yaml_text}\n```\n' + sep
    return text[:m.start()] + new_section + tail


def _fix_claims_fence(
    registry: Registry,
    archive_root: Path,
    progress: list[str],
    changed: list[str],
    dry_run: bool = False,
) -> None:
    """Wrap every source whose `## Claims` content was read unfenced in a proper
    ```yaml fence. Previewed under --dry-run; never silently rewrites."""
    for sid, path in sorted(registry.unfenced_claim_sources.items()):
        wrapped = _wrap_unfenced_claims(path)
        if wrapped is None:
            continue
        rel = path.relative_to(archive_root)
        if dry_run:
            progress.append(f'--fix-claims-fence dry-run: would wrap the claims in {rel} in a ```yaml fence')
        else:
            path.write_text(wrapped, encoding='utf-8')
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
    immediately re-flag it."""
    def letters(word: str) -> str:
        return re.sub(r'[^a-z]+', '', word.lower())
    parts = [p for p in str(name).split() if letters(p)]
    if len(parts) >= 2:
        return (letters(parts[-1]) or 'unknown',
                '_'.join(letters(p) for p in parts[:-1]) or 'unknown')
    if parts:
        return letters(parts[0]) or 'unknown', 'unknown'
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


def _insert_id_and_aliases(text: str, new_id: str, aliases: list[str]) -> str:
    """Add `id:` and `aliases:` at the top of a record's frontmatter (creating the
    frontmatter if the hand-author wrote none). `aliases` carries every string the
    file used to be known by - the slugified stem and, when different, the stem as
    written - so an existing `[[Sam Rivera]]` link keeps resolving after the §13
    rename; the new ID self-aliases.

    A record copied from a shipped template arrives with the id already PRESENT
    as a placeholder (`id: P-__________`, plus the same token in `aliases:` -
    "paste the same code here too"). For those, the surgery is a same-type token
    rewrite across the frontmatter: the id: value and the placeholder alias both
    become the minted id, everything else on their lines (spacing, the teaching
    comments) survives byte-for-byte. Same-type only, so the person template's
    commented `[[S-…]]` examples are left for their own record's mint; and since
    underscores are not Crockford Base32 characters, no real id can ever match
    the placeholder pattern."""
    entries = ', '.join([new_id] + [_yaml_alias_entry(a) for a in aliases])
    alias_line = f'aliases: [{entries}]'
    fm = FRONT_RE.match(text)
    has_aliases = bool(fm) and re.search(r'^aliases:', fm.group(1), re.M)
    has_id = bool(fm) and re.search(r'^id:', fm.group(1), re.M)
    if fm and has_id:
        # Replace the existing blank `id:` line in-place rather than prepending a
        # duplicate key (last-key-wins in YAML would silently discard the new value).
        # FRONT_RE group(1) excludes the final \n before ---, so reassemble fully.
        fm_replaced = re.sub(r'^id:[ \t]*$', f'id: {new_id}', fm.group(1), flags=re.M)
        placeholder_re = re.compile(
            rf'(?<![A-Za-z0-9_]){re.escape(new_id[0])}-_{{4,}}(?![A-Za-z0-9_])', re.I)
        fm_replaced = placeholder_re.sub(new_id, fm_replaced)
        if not has_aliases:
            fm_replaced = f'{alias_line}\n' + fm_replaced
        return f'---\n{fm_replaced}\n---\n' + text[fm.end():]
    lines = [f'id: {new_id}']
    if not has_aliases:
        lines.append(alias_line)
    insert = '\n'.join(lines) + '\n'
    if fm:
        open_end = text.index('\n', text.index('---')) + 1
        return text[:open_end] + insert + text[open_end:]
    return f'---\n{insert}---\n\n{text}'


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
            text = path.read_text(encoding='utf-8')
        except OSError:
            remaining.append((path, kind))
            continue
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
        path.write_text(_insert_id_and_aliases(text, new_id, aliases), encoding='utf-8')
        if new_path != path and not new_path.exists():
            path.rename(new_path)
            changed.append(str(new_path))
            final_path = new_path
        else:
            new_rel = rel
            changed.append(str(path))
            final_path = path
        progress.append(
            f'Minted {new_id} for {new_rel}{ph_note} (old name kept as an alias)')
        if kind == 'S':
            minted_sources.append(final_path)
    registry.idless_records = remaining
    return minted_sources


def _claims_text_region(text: str) -> tuple[int, int] | None:
    """(start, end) character offsets of the claims YAML inside a source file.

    Fenced form first (the CLAIMS_RE contract read_record uses - the ```yaml
    interior), falling back to the whole `## Claims` section for the unfenced
    W114 form. None when there is no claims block to edit."""
    m = CLAIMS_RE.search(text)
    if m:
        return m.span(1)
    m = re.search(r'^##\s+Claims[^\n]*\r?\n(.*?)(?=^##\s|\Z)', text, re.S | re.M)
    if m:
        return m.span(1)
    return None


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


_STATUS_ACCEPTED_LINE_RE = re.compile(
    r'^([ \t]+)status:[ \t]*([\'"]?)accepted\2[ \t]*(#.*)?\r?$', re.M,
)

# A claim's `id:` line still carrying the template placeholder (`id: C-__________`),
# as the shipped source template teaches. Group 1 is exactly the token to rewrite,
# so the line's spacing and trailing teaching comment survive the mint untouched.
_CLAIM_PLACEHOLDER_ID_LINE_RE = re.compile(
    r'^[ \t]*(?:-[ \t]+)?id:[ \t]*(C-_{4,})(?![A-Za-z0-9_])', re.I | re.M,
)


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
    count mismatch, a one-line `- {...}` flow claim, a bare `-` item) is
    refused with a message naming the by-hand fix, never guessed at.
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
    contract; this function is the per-file surgery."""
    try:
        rel = path.relative_to(archive_root)
    except ValueError:
        rel = path
    rec = read_record(path)
    claims = rec['claims']
    if not any(isinstance(c, dict) and _claim_id_missing(c) for c in claims):
        return
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        progress.append(f'--fix-ids: could not read {rel}; its claims were left alone.')
        return

    region = _claims_text_region(text)
    spans = _claim_item_spans(text, *region) if region else None
    if not spans or len(spans) != len(claims):
        # The text scan and the parser disagree about the claim entries (an
        # entry that parses to nothing, prose bullets in the section, ...).
        # Refuse the whole file rather than risk inserting into the wrong claim.
        progress.append(
            f"--fix-ids: the claims block in {rel} doesn't line up with what the "
            f'parser reads, so nothing was changed there - add the missing id: '
            f'lines by hand (mint values with `fha id mint C`).')
        return

    # Plan first, mint second: refusals must not consume minted ids.
    # Each plan is either an INSERT (`id:` line added after the `- ` marker) or a
    # REPLACE (a template placeholder `id: C-__________` value rewritten on its
    # existing line - the template-copy case, where the id line is already there).
    plans: list[dict] = []   # {insert_at, continuation, replace_span, stamp_at, stamp_text}
    today = _today()
    for (i, (span_start, span_end)), claim in zip(enumerate(spans), claims):
        if not isinstance(claim, dict) or not _claim_id_missing(claim):
            continue
        plan = {
            'insert_at': None,
            'continuation': '',
            'replace_span': None,
            'stamp_at': None,
            'stamp_text': '',
        }
        if str(claim.get('id') or '').strip():
            # Placeholder id: locate its own `id:` line inside this claim's span
            # and rewrite just the token (spacing and teaching comment survive).
            pm = _CLAIM_PLACEHOLDER_ID_LINE_RE.search(text, span_start, span_end)
            if not pm:
                label = str(claim.get('value', ''))[:40] or f'entry {i + 1}'
                progress.append(
                    f'--fix-ids: could not find the placeholder id: line for claim '
                    f'"{label}" in {rel} - replace it by hand (`fha id mint C`).')
                continue
            plan['replace_span'] = pm.span(1)
        else:
            line_end = text.find('\n', span_start, span_end)
            first_line = text[span_start:line_end if line_end != -1 else span_end]
            m = re.match(r'^([ \t]*)-([ \t]+)', first_line)
            rest = first_line[m.end():] if m else ''
            if not m or not rest.strip() or rest.lstrip().startswith('{'):
                # A bare `-` item or a one-line `- {...}` flow claim: inserting a
                # block field would corrupt it. Name the claim and the by-hand fix.
                label = str(claim.get('value', ''))[:40] or f'entry {i + 1}'
                progress.append(
                    f'--fix-ids: claim "{label}" in {rel} is written in a one-line form '
                    f'this fix cannot edit safely - add its id: by hand (`fha id mint C`).')
                continue
            plan['insert_at'] = span_start + m.end()
            plan['continuation'] = m.group(1) + ' ' * (m.end() - len(m.group(1)))
        if str(claim.get('status', '')) == 'accepted' and not str(claim.get('reviewed') or '').strip():
            sm = _STATUS_ACCEPTED_LINE_RE.search(text, span_start, span_end)
            if sm:
                stamp_line_end = text.find('\n', sm.end(), span_end)
                if stamp_line_end != -1:
                    plan['stamp_at'] = stamp_line_end + 1
                    plan['stamp_text'] = f'{sm.group(1)}reviewed: {today}\n'
                else:
                    # status: is the last line of the region with no newline of
                    # its own - open a fresh line before stamping.
                    plan['stamp_at'] = span_end
                    plan['stamp_text'] = f'\n{sm.group(1)}reviewed: {today}\n'
            else:
                label = str(claim.get('value', ''))[:40] or f'entry {i + 1}'
                progress.append(
                    f'--fix-ids: could not find the status: accepted line for claim '
                    f'"{label}" in {rel} - its id was minted, but add reviewed: {today} '
                    f'by hand (or run `fha claim <C-id> --status accepted`).')
        plans.append(plan)

    if not plans:
        return
    n_stamped = sum(1 for p in plans if p['stamp_at'] is not None)
    n_placeholder = sum(1 for p in plans if p['replace_span'] is not None)
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
    # placeholder rewrite replaces exactly the token's span. Applied bottom-up so
    # earlier offsets stay valid; spans never overlap (each lives in its own
    # claim item, and a stamp inserts at a line boundary the id edit never spans).
    edits: list[tuple[int, int, str]] = []
    for plan, cid in zip(plans, new_ids):
        if plan['replace_span'] is not None:
            start, end = plan['replace_span']
            edits.append((start, end, cid))
        else:
            edits.append((plan['insert_at'], plan['insert_at'],
                          f"id: {cid}\n{plan['continuation']}"))
        if plan['stamp_at'] is not None:
            edits.append((plan['stamp_at'], plan['stamp_at'], plan['stamp_text']))
    for start, end, snippet in sorted(edits, key=lambda t: t[0], reverse=True):
        text = text[:start] + snippet + text[end:]
    path.write_text(text, encoding='utf-8')
    changed.append(str(path))
    progress.append(f'Minted {len(plans)} claim id(s) in {rel}{ph_note}')
    if n_stamped:
        progress.append(
            f'Stamped reviewed: {today} on {n_stamped} hand-accepted claim(s) in {rel} '
            f"(a hand-written 'accepted' is your decision; the stamp records when the "
            f'tools met it)')


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
            stub_path.write_text(stub_content, encoding='utf-8')
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
    existing = questions_path.read_text(encoding='utf-8') if questions_path.exists() else ''
    appended = []
    for f in to_spawn:
        appended.append(
            f'\n## Q: Contradiction: {f.message}\n'
            f'- origin: tool\n- status: open\n- refs: []\n'
            f'- context:\n  - (tool, {_today()}) Auto-spawned by fha lint E009.\n'
        )
    if appended:
        questions_path.write_text(existing + '\n'.join(appended), encoding='utf-8')
        progress.append(f'Appended {len(appended)} question(s) to {questions_path.relative_to(archive_root)}')
        changed.append(str(questions_path))


def _format_mirror_entry(
    owner_pid: str, owner_name: str, role: str, subtype: str, claim_id: str,
) -> list[str]:
    """The YAML list-item lines for a mirror relationship entry pointing back at
    the person who already records the edge. Pinned `[[P-id|Name]]` so it reads
    and resolves; subtype omitted when there is nothing to say."""
    lines = [
        f'  - to: "[[{fmt_id_display(owner_pid)}|{owner_name}]]"',
        f'    type: {role}',
    ]
    if subtype:
        lines.append(f'    subtype: {subtype}')
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
            text = path.read_text(encoding='utf-8')
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
        path.write_text(new_text, encoding='utf-8')
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

def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'lint',
        help='Verify the archive against the spec',
        description=__doc__,
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
    p.add_argument('--fix-inventory', action='store_true',
                   help='Regenerate files: from ID glob for E011 set')
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
        fix_inventory=getattr(args, 'fix_inventory', False),
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
        description=__doc__,
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
    parser.add_argument('--fix-inventory', action='store_true')
    args = parser.parse_args(argv)
    return _run_lint(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
