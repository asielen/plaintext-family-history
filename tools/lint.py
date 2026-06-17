#!/usr/bin/env python3
"""
lint.py — fha lint: verify the archive against the spec.

  fha lint [--root PATH]             Walk and check the archive; report-only
  fha lint --with-exif               Also verify embedded SOURCE: keywords (slow)
  fha lint --json                    Machine-readable JSON output
  fha lint --format-check            Check formatting without fixing
  fha lint --format-write            Planned formatter write mode (not yet implemented)
  fha lint --mint-stubs              Create missing person stubs (E005 set)
  fha lint --spawn-questions         Append questions for E009 contradictions
  fha lint --fix-inventory           Planned inventory fixer (not yet implemented)

Exit codes: 0 = clean, 1 = warnings only, 2 = errors, 3 = tool failure.
SPEC §16, TOOLING §3.

HOW IT WORKS — TWO PASSES, NO PRIOR INDEX
------------------------------------------
Lint is fully self-contained: it does NOT require `fha index` to have run.
It builds its own in-memory Registry on the first pass, then runs cross-file
checks on the second pass once the full picture is available.

Pass 1 — walk and collect  (_walk_archive):
  Read every person and source file; register IDs, claims, token references,
  and metadata.  File-level checks fire here — the ones that don't need to see
  the rest of the archive: bad IDs, missing required fields, malformed EDTF
  dates, duplicate claim IDs within a source.

Pass 2 — cross-file checks  (_cross_file_checks):
  With the complete Registry in hand, check things that require the whole
  picture: orphan token references, duplicate record IDs, summary-block drift
  against accepted claims, vitals gaps for curated persons, merged-person
  references, and reverse asset inventory.

WHY IN-MEMORY, NOT THE SQLITE INDEX
  The SQLite index may not exist, or may be stale.  Lint is the source of
  truth — the index must match what lint accepts, never the other way around.
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
    CLAIM_TYPES,
    COMPANION_KINDS,
    CROCKFORD_ALPHA,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FRONT_RE,
    ID_RE,
    SIGNIFICANCE,
    SOURCE_TYPES,
    TOKEN_RE,
    VITAL_TYPES,
    Finding,
    edtf_bounds,
    extract_token_ids,
    find_archive_root,
    is_fixture_path,
    is_valid_edtf,
    is_valid_id,
    load_fha_yaml,
    normalize_id,
    parse_filename,
    read_record,
    resolve_path,
)

import yaml

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Data model
#    Registry                    — in-memory snapshot of one lint pass
#
#  Constants / small helpers
#    _SOURCE_FILENAME_RE         — grammar check for source filenames (SPEC §13)
#    _PERSON_FILENAME_RE         — grammar check for person filenames (SPEC §13)
#    REQUIRED_*_FIELDS           — required frontmatter keys per record type
#    _normalize_alias_path       — backslash→slash normalisation for path comparison
#    _mapped_root, _path_to_alias — resolve fha.yaml alias roots to absolute paths
#    _claim_person_ids           — extract normalised P-ids from a claim's persons: field
#    _parse_summary_block        — parse **Born/Died/…:** lines from a profile body
#    _collect_token_refs         — scan a text block for [ID] tokens → registry
#    _question_blocks            — split a questions.md into per-heading blocks
#    _metadata_values            — normalise scalar/list exiftool field values
#
#  Pass 1 — walk and collect
#    _walk_archive               — top-level coordinator; calls the _process_* functions
#    _process_person_file        — index one person file + file-level checks
#    _process_source_file        — index one source file + file-level checks + claims
#
#  Bracket / Ahnentafel checks (W103, W110)
#    _build_children_of          — accepted child-of claims → parent→children map
#    _check_bracket_lists        — W103: stale couple-folder bracket lists
#    _build_ahnentafel_lint      — BFS from root_person using in-memory registry
#    _check_ahnentafel_placement — W110: person file in wrong Ahnentafel folder
#
#  Pass 2 — cross-file checks
#    _cross_file_checks          — top-level coordinator for all cross-file rules
#    _check_summary_line         — E013/W104: one **Label:** segment vs accepted claims
#    _has_question_for           — E009: co-occurrence check across question blocks
#    _get_person_accepted_claims — build accepted-claim list for one person
#    _check_reverse_inventory    — E011: document files vs source inventory lists
#    _check_embedded_source_keywords — E012: exiftool SOURCE: keyword vs inventory
#    _read_source_keywords       — invoke exiftool; parse its JSON keyword output
#    _check_generated_headers    — W105: hand-edits below a GENERATED header
#    _check_readme_age           — W108: README.md older than SPEC.md
#    _check_agent_drift          — E018: deprecated commands in AGENTS.md
#
#  Format checks / fix modes
#    _check_format               — W109: final newline, CRLF line endings
#    _fix_format                 — apply conservative format fixes
#    _fix_mint_stubs             — create stubs for the E005 set (--mint-stubs)
#    _fix_spawn_questions        — append question entries for E009 set (--spawn-questions)
#
#  Main entry / CLI
#    run_lint                    — orchestrates both passes and emits findings
#    register                    — attach 'lint' to the main fha parser
#    _run_lint                   — argparse → run_lint bridge
#    _standalone_main            — for `python tools/lint.py` direct invocation
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

        # Source meta: {S-id: meta_dict}
        self.source_meta: dict[str, dict] = {}

        # Source file inventory: {S-id: {alias/path, ...}}
        self.source_inventory: dict[str, set[str]] = {}

        # All token IDs referenced in prose/frontmatter (value → list of (path, line))
        self.token_refs: dict[str, list[tuple[Path, int]]] = {}

        # Questions file content (for E009)
        self.questions_content: str = ''

        # Research files content (for E009)
        self.research_content: dict[Path, str] = {}

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
    r'^[a-z][a-z_]*__[a-z][a-z_]*(_[a-z][a-z0-9\-]*)?_P-[0-9a-hjkmnp-tv-z]{10}$', re.I
)

# ── Required-field sets ───────────────────────────────────────────────────────

REQUIRED_PERSON_FIELDS = {'id', 'name', 'living'}
REQUIRED_SOURCE_FIELDS = {'id', 'title', 'source_type'}
REQUIRED_CLAIM_FIELDS  = {'id', 'type', 'persons', 'value', 'status'}

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


def _claim_person_ids(claim: dict) -> list[str]:
    """Return normalized P-ids from a claim's persons: field."""
    persons = claim.get('persons') or []
    if isinstance(persons, str):
        persons = [persons]
    return [normalize_id(str(p)) for p in persons if p]


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

        p_ids = [
            normalize_id(t.strip('[]'))
            for t in re.findall(r'\[P-[0-9a-hjkmnp-tv-z]{10}\]', segment, re.I)
        ]
        s_ids = [
            normalize_id(t.strip('[]'))
            for t in re.findall(r'\[S-[0-9a-hjkmnp-tv-z]{10}\]', segment, re.I)
        ]
        results.append((label, segment, p_ids, s_ids))

    return results


# ── Walk and collect ─────────────────────────────────────────────────────────

def _collect_token_refs(text: str, path: Path, registry: Registry) -> None:
    """Index all [ID] citation/cross-link tokens in text into registry.token_refs."""
    for lineno, line in enumerate(text.splitlines(), start=1):
        for m in TOKEN_RE.finditer(line):
            tid = normalize_id(m.group(1))
            registry.token_refs.setdefault(tid, []).append((path, lineno))


def _walk_archive(archive_root: Path, registry: Registry, findings: list[Finding]) -> None:
    """
    Pass 1: walk the archive tree and populate the registry.

    File-level checks fire here — the ones that don't need to see the whole
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
    rec = read_record(path)
    meta = rec['meta']

    # Parse errors → E010
    for code, msg in rec['parse_errors']:
        findings.append(Finding('E', code, path, msg))

    pid_raw = str(meta.get('id', ''))
    pid = normalize_id(pid_raw)

    # E002: ID format check
    if pid_raw and not is_valid_id(pid_raw):
        findings.append(Finding('E', 'E002', path, f'Malformed ID: {pid_raw!r}'))

    # Determine kind from filename
    stem = path.stem
    parsed = parse_filename(path)
    is_companion = parsed and parsed.get('is_companion', False)
    kind = (parsed or {}).get('kind', 'profile')

    # E002: Filename grammar check
    if pid and not is_companion:
        # Profile filename: {surname}__{given}[_{kind}]_{P-id}.md
        if not _PERSON_FILENAME_RE.fullmatch(stem):
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
        return   # can't do cross-reference checks without an id

    # Register in registry
    if is_companion:
        registry.person_companion_paths.setdefault(pid, []).append(path)
    else:
        registry.person_profile_paths.setdefault(pid, []).append(path)
        registry.person_meta[pid] = meta

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
    rec = read_record(path)
    meta = rec['meta']

    for code, msg in rec['parse_errors']:
        findings.append(Finding('E', code, path, msg))

    sid_raw = str(meta.get('id', ''))
    sid = normalize_id(sid_raw)

    # E002: ID format
    if sid_raw and not is_valid_id(sid_raw):
        findings.append(Finding('E', 'E002', path, f'Malformed ID: {sid_raw!r}'))
        return

    # E002 / filename grammar: {slug}_{S-id}.md
    stem = path.stem
    parsed = parse_filename(path)
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

    # E005: source-level people list must resolve, because index.py consumes it.
    for p_raw in (meta.get('people') or []):
        ppid = normalize_id(str(p_raw))
        if ppid and not registry.has_person(ppid):
            findings.append(Finding('E', 'E005', path,
                f'Source people: references person {ppid} but no person record exists'))

    # E007 / E017 / source_type check
    source_type = str(meta.get('source_type', ''))
    if source_type and source_type not in SOURCE_TYPES:
        findings.append(Finding('W', 'W109', path,
            f'Unknown source_type: {source_type!r} (not in controlled vocabulary)'))

    # E017: DNA must be restricted
    if source_type == 'dna' and meta.get('restricted') not in (True, 'true'):
        findings.append(Finding('E', 'E017', path,
            'DNA source must have restricted: true'))

    # E014: source_date EDTF check
    source_date = str(meta.get('source_date', ''))
    if source_date and not is_valid_edtf(source_date):
        findings.append(Finding('E', 'E014', path,
            f'Non-EDTF source_date: {source_date!r}'))

    # Claims
    claims = rec['claims']
    registry.source_claims[sid] = claims

    for claim in claims:
        if not isinstance(claim, dict):
            continue

        cid_raw = str(claim.get('id', ''))
        cid = normalize_id(cid_raw)

        # E002: Claim ID format
        if cid_raw and not is_valid_id(cid_raw):
            findings.append(Finding('E', 'E002', path,
                f'Malformed claim ID: {cid_raw!r}'))
            continue

        if not cid:
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
                f'Claim {cid} type {claim_type!r} not in vocabulary'))

        # E006: accepted claim must have reviewed
        status = str(claim.get('status', ''))
        reviewed = str(claim.get('reviewed', ''))
        if status == 'accepted' and not reviewed:
            findings.append(Finding('E', 'E006', path,
                f'Accepted claim {cid} missing reviewed date'))

        # E014: Claim date EDTF check
        date_val = str(claim.get('date', ''))
        if date_val and not is_valid_edtf(date_val):
            findings.append(Finding('E', 'E014', path,
                f'Claim {cid} non-EDTF date: {date_val!r}'))

        # E008: Significance override without reason
        if claim.get('significance') and not claim.get('significance_reason'):
            findings.append(Finding('E', 'E008', path,
                f'Claim {cid} has significance override but no significance_reason'))

        # E015: relationship claim must have roles
        if claim_type == 'relationship' and not claim.get('roles'):
            findings.append(Finding('E', 'E015', path,
                f'Claim {cid} (type: relationship) missing roles:'))

        # W109: accepted claim missing notes when it's substantive OR a low-confidence vital
        sig = SIGNIFICANCE.get(claim_type, 'incidental')
        confidence = str(claim.get('confidence', ''))
        if status == 'accepted' and not claim.get('notes'):
            is_substantive = sig == 'substantive'
            is_low_confidence_vital = sig == 'vital' and confidence == 'low'
            if is_substantive or is_low_confidence_vital:
                findings.append(Finding('W', 'W109', path,
                    f'Claim {cid} ({claim_type}) missing notes context (W109)'))

    # E011: file inventory checks
    inventory_paths: set[str] = set()
    for f in (meta.get('files') or []):
        if not isinstance(f, dict):
            continue
        file_path_str = str(f.get('file', ''))
        file_status = str(f.get('status', ''))

        if not file_path_str:
            continue

        inventory_paths.add(_normalize_alias_path(file_path_str))
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

def _build_children_of(registry: Registry) -> dict[str, set[str]]:
    """Build parent_pid → {child_pids} from accepted child-of relationship claims.

    Iterates all accepted claims of type 'relationship' with subtype 'child-of'
    and extracts the roles.child / roles.parent values.  Both scalars and lists
    are accepted in either field, matching the SPEC §8.4 schema.
    """
    children_of: dict[str, set[str]] = {}
    for claims in registry.source_claims.values():
        for claim in claims:
            if (str(claim.get('status', '')) != 'accepted'
                    or claim.get('type') != 'relationship'
                    or claim.get('subtype') != 'child-of'):
                continue
            roles = claim.get('roles') or {}
            child_val = roles.get('child')
            parent_val = roles.get('parent')
            if not child_val or not parent_val:
                continue
            child_ids = [child_val] if isinstance(child_val, str) else list(child_val)
            parent_ids = [parent_val] if isinstance(parent_val, str) else list(parent_val)
            for cpid in child_ids:
                cpid = normalize_id(str(cpid))
                for ppid in parent_ids:
                    ppid = normalize_id(str(ppid))
                    children_of.setdefault(ppid, set()).add(cpid)
    return children_of


def _check_bracket_lists(registry: Registry, findings: list[Finding]) -> None:
    """W103: stale couple-folder bracket lists.

    For each digit-prefixed directory under people/ (excluding stubs/connections),
    derives the expected bracket list from accepted child-of relationship claims
    whose parent field names a person residing in that folder.  ALL children
    appear — direct-line children with their own folder included — mirroring the
    bracket convention documented in TOOLING §7.

    WHY ALL CHILDREN: see _check_w103_brackets in views.py.  Same invariant, same
    source data, different backend (in-memory registry instead of SQLite).
    """
    children_of = _build_children_of(registry)

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

        # Derive expected children names
        child_pids: set[str] = set()
        for ppid in folder_pids:
            child_pids.update(children_of.get(ppid, set()))

        derived_names = sorted(
            str(registry.person_meta.get(cp, {}).get('name', '')).split()[0]
            for cp in child_pids
            if registry.person_meta.get(cp, {}).get('name')
        )

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
            f'root_person {root_pid!r} has no person record — '
            'Ahnentafel placement checks (W110) skipped; '
            'fix root_person in fha.yaml or run fha stubs'))
        return
    children_of = _build_children_of(registry)
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
                continue
            actual_prefix = int(m.group(1))
            if actual_prefix != expected_prefix:
                name = str(registry.person_meta.get(pid, {}).get('name', pid))
                findings.append(Finding('W', 'W110', p,
                    f'{name} (Ahnentafel {pos}) is in folder prefix {actual_prefix}, '
                    f'expected prefix {expected_prefix}; '
                    f'run `fha views brackets --fix` to correct'))


# ── Cross-file checks ─────────────────────────────────────────────────────────

def _cross_file_checks(registry: Registry, findings: list[Finding], with_exif: bool = False) -> None:
    """
    Pass 2: checks that require the full registry.

    Called after _walk_archive has finished, so every ID, claim, and token
    reference is already registered.  Rules that check existence of other
    records (E004 orphan refs, E005 missing persons, E013 summary drift,
    W101 vitals gaps) all live here.
    """

    known_ids = registry.all_known_ids()

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
                    f'Orphan reference [{token_id}] (line {ref_line}) — no matching record'))

        if tid_type == 'P' and not registry.has_person(token_id):
            # E005: referenced person has no record at all
            for ref_path, ref_line in refs[:1]:
                findings.append(Finding('E', 'E005', ref_path,
                    f'P-id {token_id} referenced at line {ref_line} but no person record exists'))

    # E004: check persons referenced in claim `persons:` fields
    for sid, claims in registry.source_claims.items():
        src_path = registry.source_paths.get(sid, Path(sid))
        for claim in claims:
            for ppid in _claim_person_ids(claim):
                if not registry.has_person(ppid):
                    findings.append(Finding('E', 'E005', src_path,
                        f'Claim {claim.get("id","?")} references person {ppid} but no person record exists'))

            # E004: place reference
            place_ref = normalize_id(str(claim.get('place', '')))
            if place_ref and place_ref not in registry.place_ids:
                findings.append(Finding('E', 'E004', src_path,
                    f'Claim {claim.get("id","?")} references unknown place {place_ref}'))

            # E004: corroborates/contradicts targets
            for link_type in ('corroborates', 'contradicts'):
                targets = claim.get(link_type) or []
                if isinstance(targets, str):
                    targets = [targets]
                for t in targets:
                    tid = normalize_id(str(t))
                    if tid and tid not in registry.claim_ids and tid not in known_ids:
                        findings.append(Finding('E', 'E004', src_path,
                            f'Claim {claim.get("id","?")} {link_type}: {tid} not found'))

            # E009: contradicts without an open question referencing both claims
            if claim.get('contradicts'):
                cid = normalize_id(str(claim.get('id', '')))
                targets = claim.get('contradicts')
                if isinstance(targets, str):
                    targets = [targets]
                for t in targets:
                    tid = normalize_id(str(t))
                    # Check if an open question references both C-ids
                    if not _has_question_for(cid, tid, registry):
                        findings.append(Finding('E', 'E009', src_path,
                            f'Claim {cid} contradicts {tid} but no open question references both'))

    # E013: summary block drift for curated profiles
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
                                registry, profile_path, findings)

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
                    if pid in _claim_person_ids(claim):
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
            raise RuntimeError('--with-exif requires exiftool on PATH') from e
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
    own YAML dict — it lives on the source record that contains them.
    """
    result = []
    for sid, claims in registry.source_claims.items():
        for claim in claims:
            if str(claim.get('status', '')) != 'accepted':
                continue
            if pid in _claim_person_ids(claim):
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
) -> None:
    """
    Verify one summary-block label segment against accepted claims (E013 / W104).
    Each [S-id] citation must have a matching accepted claim of the right type for
    this person; each [P-id] cross-link must resolve to a known person record.
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
                'README.md older than SPEC.md — may need updating (the README rule)'))


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
    'id', 'name', 'name_variants', 'face_tags', 'sex', 'living',
    'no_known_marriages', 'no_known_children', 'external_ids', 'created', 'tier',
]
_FRONTMATTER_KEY_ORDER_SOURCES = [
    'id', 'title', 'source_type', 'source_date', 'source_class', 'repository',
    'citation', 'external_links', 'people', 'restricted', 'provenance', 'rights',
    'physical_location', 'files', 'created',
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


def _fix_format(path: Path, dry_run: bool = False) -> None:
    """Apply conservative formatting fixes: CRLF→LF and ensure trailing newline."""
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return
    fixed = text.replace('\r\n', '\n')
    if fixed and not fixed.endswith('\n'):
        fixed += '\n'
    if fixed != text:
        if dry_run:
            print(f'Would fix formatting: {path.name}')
        else:
            path.write_text(fixed, encoding='utf-8')
            print(f'Fixed formatting: {path.name}')


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
    use_json: bool = False,
    format_check: bool = False,
    format_write: bool = False,
    dry_run: bool = False,
    mint_stubs: bool = False,
    spawn_questions: bool = False,
    fix_inventory: bool = False,
    spec_root: Path | None = None,  # TODO: use for TOOLING §3 spec-drift checks (E018 expansion)
) -> int:
    """
    Run all lint checks against archive_root and return an exit code.
    Report-only by default; mutating fix modes require explicit flags and
    respect --dry-run. Never modifies original source files or photos.
    """
    # Check that archive root looks right
    if not (archive_root / 'fha.yaml').exists():
        msg = f'No fha.yaml found at {archive_root} — is this an archive root?'
        if use_json:
            print(json.dumps([{'severity': 'E', 'code': 'E010',
                               'path': str(archive_root), 'message': msg}]))
        else:
            print(f'E E010 {archive_root}: {msg}')
            print('Summary: 1 error(s)')
        return EXIT_ERRORS

    findings, registry = _run_lint_core(archive_root, fha_config, with_exif=with_exif)

    # Format checks / fixes
    if format_check or format_write:
        for path in archive_root.rglob('*.md'):
            if '.cache' not in path.parts:
                _check_format(path, findings)
                if format_write:
                    _fix_format(path, dry_run=dry_run)

    # Fix modes (each respects --dry-run via its own parameter)
    if mint_stubs:
        _fix_mint_stubs(registry, findings, archive_root, dry_run=dry_run)
    if spawn_questions:
        _fix_spawn_questions(registry, findings, archive_root, dry_run=dry_run)
    if fix_inventory:
        if dry_run:
            print('--fix-inventory dry-run: would scan documents root and update files: blocks for E011 set')
        else:
            print('WARNING: --fix-inventory is not yet implemented.')
            print('         Run `fha process` on each document to update its source record.')

    # Sort findings by severity then path
    findings.sort(key=lambda f: (f.code, f.path))

    # Report
    if use_json:
        print(json.dumps([f.as_dict() for f in findings], indent=2))
    else:
        for f in findings:
            # Make paths relative for readability
            try:
                rel = Path(f.path).relative_to(archive_root)
                line = f'{f.severity} {f.code} {rel}: {f.message}'
            except ValueError:
                line = str(f)
            print(line)

        n_errors = sum(1 for f in findings if f.severity == 'E')
        n_warnings = sum(1 for f in findings if f.severity == 'W')

        if not use_json:
            if not findings:
                print('✓ No issues found.')
            else:
                parts = []
                if n_errors:
                    parts.append(f'{n_errors} error(s)')
                if n_warnings:
                    parts.append(f'{n_warnings} warning(s)')
                print(f'Summary: {", ".join(parts)}')

    if any(f.severity == 'E' for f in findings):
        return EXIT_ERRORS
    if any(f.severity == 'W' for f in findings):
        return EXIT_WARNINGS
    return EXIT_CLEAN


def _fix_mint_stubs(
    registry: Registry, findings: list[Finding], archive_root: Path, dry_run: bool = False
) -> None:
    """Create missing person stubs (E005 set) in people/stubs/. Respects dry_run."""
    stubs_dir = archive_root / 'people' / 'stubs'

    # Collect pids that appear in claims but have no record
    missing: set[str] = set()
    for sid, claims in registry.source_claims.items():
        for claim in claims:
            for ppid in _claim_person_ids(claim):
                if not registry.has_person(ppid):
                    missing.add(ppid)

    for ppid in sorted(missing):
        stub_path = stubs_dir / f'unknown__unknown_{ppid}.md'
        if stub_path.exists():
            continue
        if dry_run:
            print(f'Would create stub: people/stubs/unknown__unknown_{ppid}.md')
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
            print(f'Created stub: {stub_path.relative_to(archive_root)}')


def _fix_spawn_questions(
    registry: Registry, findings: list[Finding], archive_root: Path, dry_run: bool = False
) -> None:
    """Append templated questions for E009 contradictions. Respects dry_run."""
    questions_path = archive_root / 'notes' / 'questions.md'
    to_spawn = [f for f in findings if f.code == 'E009']
    if not to_spawn:
        return
    if dry_run:
        print(f'Would append {len(to_spawn)} question(s) to notes/questions.md')
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
        print(f'Appended {len(appended)} question(s) to {questions_path.relative_to(archive_root)}')


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
    p.add_argument('--fix-inventory', action='store_true',
                   help='Regenerate files: from ID glob for E011 set')
    p.set_defaults(func=_run_lint)


def _run_lint(args: argparse.Namespace) -> int:
    root = getattr(args, 'root', None)
    if root:
        archive_root = Path(root).resolve()
    else:
        archive_root = find_archive_root()
        if archive_root is None:
            print('ERROR: cannot find archive root. Use --root.', file=sys.stderr)
            return EXIT_FAILURE

    fha_config = load_fha_yaml(archive_root)
    spec_root = getattr(args, 'spec_root', None)

    return run_lint(
        archive_root=archive_root,
        fha_config=fha_config,
        with_exif=getattr(args, 'with_exif', False),
        use_json=getattr(args, 'use_json', False),
        format_check=getattr(args, 'format_check', False),
        format_write=getattr(args, 'format_write', False),
        dry_run=getattr(args, 'dry_run', False),
        mint_stubs=getattr(args, 'mint_stubs', False),
        spawn_questions=getattr(args, 'spawn_questions', False),
        fix_inventory=getattr(args, 'fix_inventory', False),
        spec_root=Path(spec_root) if spec_root else None,
    )


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
    parser.add_argument('--fix-inventory', action='store_true')
    args = parser.parse_args(argv)
    return _run_lint(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
