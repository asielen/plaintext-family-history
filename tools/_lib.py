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
import fnmatch
import json
import os
import re
import secrets
import shutil
import sqlite3
import shlex
import sys
import tempfile
import time
import unicodedata
from collections import deque
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
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
#    TEXT_COMPANION_ROLES      - files: roles that hold a source's words as text
#    SEARCHABLE_TEXT_SUFFIXES  - file extensions the text search actually reads
#    COMPANION_KINDS           - generated file kinds that share a P-id with their profile
#
#  Which sources a text search can see inside (#46)
#    file_entry_carries_text   - one files: entry -> is its content searchable text?
#    files_carry_searchable_text - a source's files: block -> any text at all?
#
#  Who a marriage claim marries (SPEC §8.3, TOOLING §197)
#    spouse_parties            - a claim's (pid, role) list -> the people it says
#                                 married EACH OTHER; the one rule index, gedcom
#                                 and lint all read, so they cannot disagree
#    parentage_parties         - the same question for parentage: a claim's
#                                 (pid, role) list -> (children, parents), roles
#                                 only, never position; read by index and lint
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
#    read_cache_meta / write_cache_meta - one meta(key, value) row of a cache
#    photos_ignore_patterns    - fha.yaml photos_ignore: patterns, normalized
#    photos_ignore_matcher     - is_ignored(rel) closure shared by scan + freshness
#    photoindex_config_fingerprint - the fha.yaml settings a photo catalog is built from
#    photoindex_config_drift   - plain words for how those settings changed since
#    photoindex_status         - classify .cache/photos.sqlite freshness for find/doctor
#
#  Safety copies of originals (`originals_backup:`, TOOLING §13f)
#    BackupRefused             - "no safety copy, so do not write" (carries the sentence)
#    originals_backup_dir      - fha.yaml originals_backup: -> resolved folder, or None
#    _fold_path_text           - case/Unicode-flattened path spelling
#    _path_contains            - exact / name-fold / no containment, by parent climb
#    format_size               - bytes -> B/KB/MB/GB a human reads
#    OriginalBackup            - one run's policy: copy once per file, warn once,
#                                 fail closed; the ONE rule all four writers use
#
#  Record parsing
#    read_text_exact            - newline-exact record read (no CRLF/LF translation)
#    write_text_exact           - its non-atomic mirror; NOT for archive records
#    _refuse_unwritable_target  - the read-only-record refusal os.replace skips
#    _carry_ownership           - give the temp the record's owner/group before it lands
#    write_text_exact_atomic    - the record writer: temp + fsync + os.replace
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
#    parse_filename            - decompose filename into {id_str, kind, is_companion,
#                                 kind_ambiguous} (the §13 kind slot is shared with the
#                                 last given name, so the kind can only ever be a guess)
#    PERSON_RECORD_FIELDS      - the SPEC §9 fields that mark a file as a person record
#    carries_person_record_fields - does this frontmatter say "I am a person record"?
#    person_file_kind          - what a people/ file IS: content first, filename as hint
#    is_person_file_kind       - is this person file the `research`/`timeline`/… companion?
#                                 (the §13 kind SLOT, never a substring of the stem;
#                                  pass `meta` and the file's own content decides)
#    ParsedName, parse_media_filename - decompose an unprocessed photo/scan filename
#                                 into base_id + variant/part-kind/page/crop (TOOLING §6/§9)
#
#  EDTF handling
#    is_valid_edtf             - validate an EDTF string against this project's subset
#    normalize_date            - loose human date ("circa 1870", "1870s") → canonical EDTF
#    edtf_bounds               - compute (date_min, date_max) ISO strings
#    _pad_date, _last_day      - internal date-padding helpers
#    edtf_confidence           - sortable confidence score (components + marker rank)
#
#  Photo DATE: keyword resolution (SPEC §20) - shared by photoindex + process
#    PHOTO_DATE_PATTERN_RE     - the letter grammar a DATE: keyword may carry
#    PHOTO_EXIF_DATE_RE        - the leading YYYY:MM:DD of an EXIF DateTimeOriginal
#    photo_date_markers_to_edtf - INTERNAL: digits + confidence markers → EDTF
#    photo_date_pattern_to_edtf - letter grammar + DateTimeOriginal → EDTF
#    resolve_photo_edtf        - one photo's date: the letter form only, or nothing
#    is_nonspec_photo_date_keyword - is this DATE: keyword outside the §20 grammar?
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
#    strip_generational_suffix - pull a trailing Jr/Sr/II/III/IV/V off name tokens (issue #53)
#    stub_slug_name            - display name → (surname_slug, given_slug) for a filename
#    stub_filename             - {surname}__{given}_{P-id}.md, the stub naming grammar
#    render_stub_content       - the stub frontmatter text `fha stubs`/`fha person new` write
#
#  Ahnentafel derivation + stub promotion (the shared promote engine)
#    build_ahnentafel_map      - index BFS: {P-id → Ahnentafel position} (SPEC §12.2)
#    ahnentafel_generation     - position → generation depth (the --generations cap)
#    couple_folder_prefix      - position → the even couple-folder number
#    couple_folder_dirs        - digit-prefixed dirs under people/ (not stubs/connections)
#    couple_folder_for_prefix  - canonical on-disk couple folder for a number, or None
#    AmbiguousCoupleFolderError - two folders share one prefix; caller must refuse
#    research_template_text / render_research_content - the SPEC §16 research scaffold
#    research_companion_filename - {slug}_{P-id}.md → {slug}_research_{P-id}.md
#    PromotionError            - promotion refused/failed (rolled back), plain message
#    promote_person_record     - the ONE engine: tier flip + move + research scaffold,
#                                 transactional, shared by person promote and
#                                 views brackets --fix-promote
#    relocate_person_in_index  - rewrite every path-keyed index row after a record move
#    sync_generated_view_rows  - keep notes_fts/person_files in step with the
#                                 companion views `fha views` writes and deletes
#    extract_tokens            - (id, display, fragment, span) per citation token
#    extract_token_ids         - the IDs of all citation tokens in a text block
#    extract_bare_ids          - all bare IDs from a text block
#    normalize_place_text      - lowercase/collapse-whitespace key for comparing
#                                 free-text place names without a shared place_id
#
#  Alias resolution / publication guards
#    resolve_typed_ref         - structured-field ref → typed canonical ID (K4 shared home)
#    strip_unaccepted_drafts   - drop AI-DRAFT prose + AI markers pre-publication (fail-closed)
#    transcript_review_state   - has a human checked this transcript against the
#                                 picture? unreviewed | verified | unmarked | damaged
#    transcript_text_is_unchecked - that state collapsed to the one question a
#                                 consumer asks (damaged counts as unchecked)
#    GENERATED_PREFIX, is_generated_text, is_generated_file - GENERATED-header ownership test
#
#  Walking a tree that might not open
#    unreadable_dir_recorder   - os.walk onerror collector: the folders it failed on
#    walk_files                - rglob replacement WITH that error seam
#    unreadable_dir_hold_mtimes - times to hold a cache behind so it reads 'stale'
#
#  Archive freshness
#    _is_generated_companion   - a real `fha views` output under people/ (not a
#                                 human file that shares its name)
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


def spouse_parties(persons_with_roles: Iterable[tuple[str, Any]]) -> list[str]:
    """
    Who a marriage/divorce claim says were married TO EACH OTHER.

    `persons:` is the index of who a claim is ABOUT, not a list of couples
    (SPEC §8.3). A marriage or divorce record routinely names more than the two
    principals - a Vermont marriage certificate names the couple and both sets
    of parents, a divorce decree can name witnesses or a judge - and listing all
    of them is the correct way to write the claim. The semantics live in the
    optional `roles:` map, which is what this reads.

    Input is the claim's people paired with the role each plays, in the claim's
    own order: `[(pid, role), …]`, role None or '' where the claim gave none.

    People are counted ONCE each, first appearance keeping their place. A claim
    can name the same person twice - `persons: [P-a, "[[Alice Smith]]"]` is one
    man written two ways, and `claim_persons` stores a row per entry with no
    UNIQUE constraint to stop it. Counting entries instead of people read those
    two rows as a couple and married the man to himself: a spouse edge from a
    person to themselves, which `fha lint` cannot see (W125 needs two distinct
    people to speak) and every consumer reads back as fact. Every producer
    assigns a person's role by their id, so the duplicate entries carry the
    same role and first-one-wins loses nothing.

    Then three cases, in order:

      1. `roles: spouse:` naming TWO OR MORE people -> those people, whatever
         else is listed. Two is a couple; three or more is the serial case (one
         claim recording successive marriages), and every pairing is derived.
      2. Otherwise, exactly two people named AND no named person carrying an
         explicit role other than spouse -> those two. This is the ordinary
         hand-written claim and by far the commonest shape; it must keep working
         without ceremony. Note what falls in here besides "no roles: map at
         all": a map naming ONE resolvable spouse. A typo'd id, a spouse left
         out of `persons:`, an alias that stopped resolving - each leaves a
         single spouse and a partner with NO role, and one name is not an answer
         to "who married whom". Treating it as one would silently drop the edge
         from an ordinary two-person marriage.
         What the role test excludes is the claim that answered the question and
         said no: `roles: {spouse: [P-a], parent: [P-b]}` calls P-b a parent, so
         pairing the two contradicts the claim in its own words and marries a
         man to his father-in-law - the same corruption case 3 refuses, reached
         through the fallback instead. A silence is inferred only from a silence.
      3. Otherwise -> NOTHING. The tool cannot tell the couple from their
         parents, and it must not guess: an invented spouse edge is read back as
         fact by `fha relate`, the tree views, `fha report`'s confirmed-
         connections list and the GEDCOM export, while a missing one is merely
         missing. Silence is recoverable; a false marriage is not.

    Nothing here is silent in the archive: `fha lint` W125 warns whenever a
    couple claim resolves two or more distinct people and this returns nothing -
    cases 2-refused and 3 alike - so the silence is never the end of the story.

    Shared by `fha index` (spouse edges, and the `date_end` a divorce writes)
    and `fha gedcom` (which FAM a marriage event belongs to) so the archive and
    its export can never answer this question differently - a rule implemented
    twice drifts, and the two disagreeing is worse than either being wrong
    alone. `fha lint`'s W115 reads the same rule over raw claim YAML.
    """
    first_role: dict[str, str] = {}
    for pid, role in persons_with_roles:
        if pid not in first_role:
            first_role[pid] = str(role or '').strip().lower()
    pairs = list(first_role.items())

    spouses = [pid for pid, role in pairs if role == 'spouse']
    if len(spouses) >= 2:
        return spouses
    if len(pairs) == 2 and not any(role and role != 'spouse' for _pid, role in pairs):
        return [pid for pid, _role in pairs]
    return []


def parentage_parties(
    persons_with_roles: Iterable[tuple[str, Any]],
) -> tuple[list[str], list[str]]:
    """
    Who a claim says was born, and who it says they were born to.

    Returns `(children, parents)` - or `([], [])` when the claim has not
    answered both halves of the question, so one truth test (`if children and
    parents`) covers every caller.

    Input is the claim's people paired with the role each plays, in the claim's
    own order: `[(pid, role), …]`, role None or '' where the claim gave none.
    People are counted ONCE each, first appearance keeping their place, for the
    reason spelled out in `spouse_parties`: `persons: [P-a, "[[Sam Rivera]]"]`
    is one child written two ways, and `claim_persons` stores a row per entry.

    **Roles only. There is no positional fallback, at any person count.** This
    is the one place the rule is stricter than `spouse_parties`, and the
    difference is not fussiness:

      - Marriage is symmetric, so a claim naming exactly two people admits
        exactly one answer - A married B, and it does not matter which is
        which. Parentage is DIRECTED. `persons: [P-a, P-b]` on a birth record
        does not say which of them was born, and getting it backwards makes a
        newborn her own mother's parent.
      - `persons: [child, father, mother]` is a habit, not a contract. SPEC
        §8.3 says so outright: positional convention alone is too fragile for
        exporters and tree regeneration, which is why `roles:` exists.
      - The extra person on a birth record is not reliably a parent. Registers
        name informants, midwives, attending physicians, and the deponent whose
        statement fixed the date. A two-person fallback would file each of them
        as somebody's father.

    So a claim that has not said gets nothing derived from it. A false parent
    edge is read back as fact by `fha relate`, the tree views, `fha report` and
    the GEDCOM export, and it drags a whole Ahnentafel line with it; a missing
    one is merely missing. Silence is recoverable, a false parent is not.

    Nothing here is silent in the archive: `fha lint` W126 warns whenever an
    accepted birth claim names two or more people and this returns nothing, so
    the refusal is never the end of the story - the same bargain W125 strikes
    for couple claims.

    Shared by `fha index` (both the `birth` and the `relationship` branch of
    relationship derivation, so two ways of writing one fact reach the tree as
    the same edges) and by `fha lint` (W126). One rule, one home: a rule
    implemented twice drifts, and the two disagreeing is worse than either
    being wrong alone.
    """
    first_role: dict[str, str] = {}
    for pid, role in persons_with_roles:
        if pid not in first_role:
            first_role[pid] = str(role or '').strip().lower()

    children = [pid for pid, role in first_role.items() if role == 'child']
    parents = [pid for pid, role in first_role.items() if role == 'parent']
    if not children or not parents:
        return [], []
    return children, parents


def spouse_extended_base(
    base_name: str, partner_ids: list[str], names: dict[str, str],
) -> tuple[str, str | None]:
    """Extend a couple folder's base name with the missing second partner.

    SPEC §12.2's illustrated convention names both partners ('040 Thomas
    Hartley + Margaret Cole'), but a tool-created folder starts with only the
    promoted person's name (`{NNN} {name}`, the promote engine's grammar) and
    nothing ever added the spouse. This derives the `+ second spouse` half from
    the same folder-occupancy data that drives the W103 bracket refresh -
    shared by lint and views so both derive byte-identical names.

    Deliberately conservative - folder names are free-form human convenience
    (SPEC §12.2), so the rule is: only ADD, never rewrite, never guess. The
    extension fires only when ALL hold:
      - the folder's derived couple (`partner_ids`: occupants minus occupants
        who are children of occupants) has exactly two members;
      - the base name (numeric prefix + text, bracket list already stripped)
        carries no `+` yet - an existing spouse half, even a hand-written
        nickname, is never touched;
      - the text after the numeric prefix exactly matches ONE partner's
        recorded name (case/whitespace-insensitive) - a base hand-crafted
        enough not to match is left alone rather than guessed at.

    The placeholder guard runs on BOTH halves. A base that is itself a
    placeholder (`004 unknown` - the folder a promotion had to invent for a
    person with no recorded name) is left alone: appending a real partner would
    bake the placeholder in for good, since this rule only ever adds and never
    revisits a base that already carries a `+`.

    Returns (new_base, other_name): the possibly-extended base name, and the
    appended partner's name when it changed (None otherwise).
    """
    if len(partner_ids) != 2:
        return base_name, None

    m = re.match(r'^(\d+[a-z]?\s+)(.*)$', base_name)
    if not m or '+' in m.group(2):
        return base_name, None
    if is_placeholder_name(m.group(2)):
        return base_name, None

    def _norm(s: str) -> str:
        return ' '.join(s.split()).casefold()

    base_person = _norm(m.group(2))
    matched = [pid for pid in partner_ids
               if names.get(pid) and _norm(names[pid]) == base_person]
    if len(matched) != 1:
        return base_name, None
    other = next(pid for pid in partner_ids if pid != matched[0])
    other_name = names.get(other)
    # A partner with no recorded name resolves to their bare P-id, or to one
    # of the archive's placeholders - a folder name is for humans, so never
    # write an ID or a placeholder into it.
    if (not other_name or other_name == other or is_placeholder_name(other_name)
            or _norm(other_name) == base_person):
        return base_name, None
    return f'{base_name} + {other_name}', other_name


def is_placeholder_name(name: object) -> bool:
    """True for the strings that stand in for 'no name recorded'.

    `fha stubs` mints an unnamed reference as `name: unknown`, the index stores
    'unknown' for a record with no `name:` at all, and a bare `name:` key (YAML
    null) reaches the index as the string 'None'. None of these may be written
    into a couple-folder name or offered as a partner label; every naming
    surface (W103's `+ spouse` half, W119's invented folder, --realign) checks
    here so they can never disagree.
    """
    if name is None:
        return True
    text = ' '.join(str(name).split()).casefold()
    return text in ('', 'unknown', 'none', 'null', 'unnamed', '?')

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


def sex_slot_is_defaulted(sex: object) -> bool:
    """Whether a lone linked parent with this `sex:` value gets a DEFAULTED
    Ahnentafel slot worth a W120 note.

    The derivation puts a lone parent whose sex is not F in the father/even
    slot. That is a default, not a derivation, whenever the value is absent,
    blank, the legacy `U`, or something the vocabulary does not recognise
    (`f`, `male`) - the human can settle it by recording `sex: M`/`sex: F`.
    An explicitly recorded `intersex` or `unknown` (SPEC §9's vocabulary) is a
    fact the human already stated: the tie-break is the designed behaviour
    for it (TOOLING §7), there is nothing more to record, and a permanent
    warning against a correct record would only teach the human to overwrite
    it. Shared by lint and views so the twins fire on the same set.
    """
    text = ('' if sex is None else str(sex)).strip()
    return text not in PERSON_SEX_VALUES


def format_w120_message(name: str, pos: int, sex: object, cmd_hint: str) -> str:
    """The W120 finding text, one wording for lint and views."""
    text = ('' if sex is None else str(sex)).strip()
    if text and text != 'U':
        cause = (f'their record carries `sex: {text}`, which the tools do not '
                 'recognise (the vocabulary is M | F | intersex | unknown)')
    else:
        cause = 'their record has no sex: recorded'
    return (
        f'{name} took Ahnentafel position {pos} (the father/even slot) by '
        f'default: they are the only linked parent of that couple and {cause}, '
        'so their slot - and every ancestor number above them - is a guess, '
        'not a derivation. Record `sex: M` or `sex: F` on their record '
        f'(`fha person set-sex <P-id> M|F`) to confirm or correct the placement, '
        f'then run {cmd_hint}.'
    )


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
        f'`{pip_command("pyyaml")}`, then run `fha doctor` to check your archive.'
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

# The `files:` roles whose companion holds a source's words as text: the
# `transcript` role SPEC §12.1 lists among the filename suffixes, and the
# `extracted-text` role `fha source extract` stamps on a PDF text-layer dump.
# `transcription` is the older spelling - it is what the shipped example
# archive's records say, and hand-written records use it too - and it means the
# same thing, so it is accepted here rather than read as an unknown role. Being
# fussy about the spelling would make a fully transcribed source count as one
# nobody can read, which is the one mistake this vocabulary exists to prevent.
TEXT_COMPANION_ROLES: frozenset[str] = frozenset({
    'transcript', 'transcription', 'extracted-text',
})

# File extensions the archive's text search actually opens and reads. Anything
# else - a scan, a photograph, a PDF, a recording - is opaque to it: a PDF's own
# text layer is only searchable once `fha source extract` has dumped it into a
# companion, and an image or a recording only once somebody has written out what
# it says.
SEARCHABLE_TEXT_SUFFIXES: frozenset[str] = frozenset({'.md', '.txt'})

# Companion file kinds: generated view files that share a P-id with their profile
# and live in the same folder.  Enumerated here so that parse_filename (kind
# detection) and index.py (person_files.kind column) stay in sync when new view
# types are added - add the kind here, and both consumers pick it up automatically.
COMPANION_KINDS: frozenset[str] = frozenset({'research', 'timeline', 'sources-index', 'draft-queue'})
# The subset a tool writes FROM the index (`fha views`). Their content is
# derived, so writing one changes nothing the index needs to re-read; the
# `research` companion is human-written and stays a record for freshness.
GENERATED_COMPANION_KINDS: frozenset[str] = frozenset({'timeline', 'sources-index', 'draft-queue'})

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
INDEX_SCHEMA_VERSION = 7
PHOTOINDEX_SCHEMA_VERSION = 1
CACHE_SCHEMA_KEY = 'schema_version'

# The `meta` row `fha photoindex` stamps with the fha.yaml settings the catalog
# was built from. Schema version says "this cache has the right shape"; this
# says "this cache holds the right files". They are separate questions: a
# `photos_ignore:` edit changes neither the shape nor any photo's mtime, so
# without a stored copy of the settings, nothing in the freshness check can
# tell that the catalog no longer matches the config (see
# photoindex_config_drift). Stored under the same `meta(key, value)` table the
# schema version already uses - one place for "what is this cache", and the
# photoindex DDL's INSERT touches only the schema_version row, so this survives
# every reopen until the next scan restamps it.
PHOTOINDEX_CONFIG_KEY = 'build_config'

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


# The last `roots:` mapping the tools ran against, remembered so a change can
# be judged BEFORE it does damage. Disposable cache, like everything under
# .cache/: absent means "nothing to compare", never an error.
ROOTS_STAMP_NAME = 'roots.json'


def _read_roots_stamp(archive_root: Path) -> dict[str, str] | None:
    path = archive_root / '.cache' / ROOTS_STAMP_NAME
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): str(v) for k, v in data.items()}


def _write_roots_stamp(archive_root: Path, roots: dict[str, str]) -> None:
    cache = archive_root / '.cache'
    try:
        cache.mkdir(parents=True, exist_ok=True)
        (cache / ROOTS_STAMP_NAME).write_text(
            json.dumps(roots, indent=2, sort_keys=True), encoding='utf-8')
    except OSError:
        pass  # a stamp that cannot be written just means no warning next time


def _iter_filed_asset_paths(archive_root: Path):
    """Yield every alias-form `files:` entry across the source records."""
    sources_dir = archive_root / 'sources'
    if not sources_dir.is_dir():
        return
    for path in sorted(sources_dir.rglob('*.md')):
        try:
            rec = read_record(path)
        except Exception:
            continue
        for f in (rec.get('meta') or {}).get('files') or []:
            if isinstance(f, dict) and f.get('file'):
                yield path, str(f['file']).replace('\\', '/').lstrip('./')


def roots_change_orphans(
    archive_root: str | Path, fha_config: dict, *, record: bool = True,
) -> list[dict]:
    """
    Detect a `roots:` change that has orphaned already-filed assets (#36).

    Compares the current `roots:` mapping with the one remembered in
    `.cache/roots.json`. For every alias whose value changed, every source
    record's `files:` entry under that alias is resolved twice: an entry that
    resolved under the OLD root and does not under the NEW one is an orphan -
    the record still names it, the file still exists, and only the mapping
    moved out from under it. This is the check `fha lint` E011 already makes,
    run at the moment it can still be undone with a one-line revert instead
    of after a wall of errors whose suggested remedy (`fha reconcile`) cannot
    apply, because nothing moved.

    Returns one dict per orphaning alias: {alias, old, new, orphaned,
    sample}. Side effects on the stamp: no stamp yet -> seeded with the
    current mapping, nothing reported (there is nothing to compare); a change
    that orphans nothing -> accepted, stamp updated; a change that orphans ->
    stamp left alone, so the warning stays until the human reverts the value
    or re-points the records. `roots:` shapes that are not a mapping are
    doctor's business (it already reports them) and are ignored here.

    `record=False` makes the call read-only: it still compares and reports,
    but never seeds or advances the stamp. `fha lint` uses that - a linter
    must not create files under an archive it was pointed at (a fixture, a
    read-only checkout); `fha index` and `fha doctor`, which already own
    `.cache/`, do the recording.
    """
    archive_root = Path(archive_root)
    roots_now = get_roots(fha_config)
    if not isinstance(roots_now, dict):
        return []
    roots_now = {str(k): str(v) for k, v in roots_now.items() if v is not None}
    remembered = _read_roots_stamp(archive_root)
    if remembered is None:
        if record:
            _write_roots_stamp(archive_root, roots_now)
        return []
    changed = {
        alias for alias in set(remembered) | set(roots_now)
        if remembered.get(alias) != roots_now.get(alias)
    }
    if not changed:
        return []

    old_config = {'roots': remembered}
    per_alias: dict[str, dict] = {}
    for _record, entry in _iter_filed_asset_paths(archive_root):
        alias = entry.split('/', 1)[0]
        if alias not in changed:
            continue
        if resolve_path(entry, fha_config, archive_root).exists():
            continue
        if not resolve_path(entry, old_config, archive_root).exists():
            continue  # was already broken before the change - not this change's doing
        info = per_alias.setdefault(alias, {
            'alias': alias,
            'old': remembered.get(alias),
            'new': roots_now.get(alias),
            'orphaned': 0,
            'sample': [],
        })
        info['orphaned'] += 1
        if len(info['sample']) < 3:
            info['sample'].append(entry)

    if not per_alias:
        if record:
            _write_roots_stamp(archive_root, roots_now)
        return []
    return [per_alias[a] for a in sorted(per_alias)]


def format_roots_orphan_warning(item: dict, archive_root: str | Path) -> str:
    """One plain-language warning line for a `roots_change_orphans` item."""
    fha_yaml = Path(archive_root) / 'fha.yaml'
    old = item['old'] if item['old'] is not None else '(unset)'
    new = item['new'] if item['new'] is not None else '(unset)'
    sample = ', '.join(item['sample'])
    more = item['orphaned'] - len(item['sample'])
    more_txt = f' and {more} more' if more > 0 else ''
    return (
        f"roots: {item['alias']} changed from {old!r} to {new!r} in {fha_yaml}, and "
        f"{item['orphaned']} filed file(s) that resolved under the old value no longer "
        f'do ({sample}{more_txt}). Nothing on disk moved, so `fha reconcile` cannot '
        're-tie them. Revert the value, or re-point those records - and if the aim '
        'was to keep part of the library out of the photo catalog, use '
        '`photos_ignore:` in fha.yaml instead of narrowing the root.'
    )


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


def read_cache_meta(db_path: str | Path, key: str) -> str | None:
    """Read one `meta(key, value)` row out of a disposable cache, or None.

    Its own connection, because the freshness callers ask this BEFORE they are
    willing to open the cache for queries at all - the whole point is to decide
    whether the rows can be trusted. Every failure reads as None, meaning "this
    cache does not say": no file, no `meta` table, an unreadable database, or a
    key an older build never wrote. Callers must treat None as unknown rather
    than as a mismatch, so that a cache built before a key existed is not
    reported broken.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        # sqlite3.connect() would CREATE an empty database here, leaving a
        # bogus cache file behind for a question that was only ever a read.
        return None
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        if conn is not None:
            conn.close()
    return None if row is None else str(row[0])


def write_cache_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Stamp one `meta(key, value)` row on an already-open cache connection.

    Takes the connection rather than a path so the stamp lands inside the
    writer's own transaction: a build configuration recorded before the rows
    it describes are committed would claim a catalog that does not exist yet
    if the run then failed. The caller commits.
    """
    conn.execute('INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)', (key, value))


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


def photos_ignore_patterns(fha_config: dict) -> list[str]:
    """
    The `photos_ignore:` patterns from fha.yaml, normalized to posix form.

    A photos root often holds material that is not the archive's subject - the
    motivating case (#35) was a root of 88,131 files, 63,156 of them one bulk
    photo-service export, drowning the few dozen scanned ancestor photos in
    every triage ranking. Narrowing `roots: photos` instead is NOT safe (it
    orphans already-filed `files:` entries, #36), so exclusion has to live at
    scan level. Accepts a single string or a list; anything else is a clean
    RuntimeError (the scan callers' existing error path).

    Lives here rather than in photoindex.py because the scan is not the only
    reader: `photoindex_status` has to prune the same subtrees when it decides
    whether the catalog is current, or an ignored file would mark an
    up-to-date catalog stale.
    """
    raw = fha_config.get('photos_ignore')
    if raw is None:
        return []
    if isinstance(raw, (str, int, float)):
        raw = [raw]
    # Scalars are coerced: YAML reads an unquoted year folder (`- 2019`) as
    # an int, and the intent is unambiguous. Anything structured is refused.
    if not isinstance(raw, list) or not all(
        isinstance(p, (str, int, float)) and not isinstance(p, bool) for p in raw
    ):
        raise RuntimeError(
            'photos_ignore in fha.yaml must be a list of path patterns '
            "(e.g.\n  photos_ignore:\n    - 'Flickr Export'\n    - '*.tif')"
        )
    out = []
    for p in raw:
        text = str(p).replace('\\', '/').strip().strip('/')
        if text:
            out.append(text)
    return out


def photos_ignore_matcher(patterns: list[str]):
    """Build `is_ignored(rel_posix)` for a set of `photos_ignore:` patterns.

    Matching is fnmatch-style against the posix path relative to the photos
    root, case-insensitively - the photo library lives on a case-insensitive
    filesystem on both Windows and macOS, and 'flickr export' silently failing
    to prune 'Flickr Export' would read as the feature not working. The
    patterns are folded once here rather than on every candidate: a walk that
    tests 88,000 names should fold two patterns, not 176,000 strings.

    Returned as a closure so the walkers (the scan's `_iter_photo_files` and
    the freshness watermark below) apply one identical rule; a photo the scan
    skips must not be a photo the freshness check trips over.
    """
    folded = [pat.casefold() for pat in patterns]

    def is_ignored(rel: str) -> bool:
        rel_cf = rel.casefold()
        return any(fnmatch.fnmatchcase(rel_cf, pat) for pat in folded)

    return is_ignored


def photoindex_config_fingerprint(fha_config: dict) -> str:
    """The fha.yaml settings that decide WHICH photos the catalog holds, as one
    canonical JSON string to store beside the catalog and compare against later.

    Only settings that change the *membership* of the catalog belong here:

      photos_ignore  - the patterns the scan prunes by. Adding one leaves rows
                       for newly excluded files in the catalog; removing one
                       leaves files uncatalogued.
      photos root    - the folder the scan walks. Repoint it and every row
                       describes the old folder, under path aliases that look
                       exactly the same.

    Neither of those touches a photo's mtime, and the freshness watermark is
    made of mtimes, so a change to either is invisible to it in one direction
    and worse than invisible in the other: an old file whose mtime predates
    photos.sqlite can never raise the watermark, so un-ignoring its folder
    would leave it out of the catalog forever while the catalog read 'fresh'.
    Storing the settings is what makes the change itself a freshness
    dependency.

    The root is fingerprinted as the value written in fha.yaml, not as the
    resolved absolute path, so that moving the whole archive to another folder
    (or another machine) does not read as a configuration change and force a
    needless rescan. Patterns are sorted and de-duplicated because reordering
    the list changes nothing about which files match.

    Nothing else from fha.yaml belongs here: the biography voice, the site
    title, the promotion threshold and `root_person` change what tools SAY, not
    what the catalog CONTAINS, and folding them in would nag for a rescan that
    has nothing to do.
    """
    try:
        patterns = photos_ignore_patterns(fha_config)
    except RuntimeError:
        # A malformed photos_ignore: prunes nothing (photoindex_status's own
        # reading of it) and the scan refuses with the real explanation. Two
        # readers, one interpretation.
        patterns = []
    roots = fha_config.get('roots')
    raw_root = roots.get('photos') if isinstance(roots, dict) else None
    # An absent `photos` alias resolves to <archive>/photos (resolve_path), so
    # it has to fingerprint the same as an explicit `photos: photos` or an
    # archive that has never touched the key would read as drifted on the
    # first upgrade. './photos' and 'photos/' are the same folder too, and
    # re-typing one as the other is not a configuration change.
    photos_root = str(raw_root or 'photos').replace('\\', '/').rstrip('/')
    while photos_root.startswith('./'):
        photos_root = photos_root[2:]
    return json.dumps(
        {'photos_ignore': sorted(set(patterns)), 'photos_root': photos_root},
        sort_keys=True,
    )


def photoindex_config_drift(archive_root: str | Path, fha_config: dict) -> str | None:
    """Plain words for how fha.yaml has changed since the photo catalog was
    built, or None when it has not (or the catalog predates the stamp).

    Returned as a sentence fragment a CLI can drop into its own message,
    because the human's next step is the same either way (rescan) but the
    reason is not, and "run fha photoindex" with no reason reads as the tool
    nagging. Callers that only need the yes/no answer test for None.

    A catalog with no stored configuration - written by a build before this
    existed - returns None rather than a mismatch. It would be true but
    useless to report drift we cannot actually detect, and the very next scan
    stamps it, so the check arms itself.
    """
    db_path = Path(archive_root) / '.cache' / 'photos.sqlite'
    stored = read_cache_meta(db_path, PHOTOINDEX_CONFIG_KEY)
    if stored is None:
        return None
    try:
        was = json.loads(stored)
    except (ValueError, TypeError):
        return None
    if not isinstance(was, dict):
        return None
    now = json.loads(photoindex_config_fingerprint(fha_config))

    reasons = []
    if was.get('photos_ignore') != now['photos_ignore']:
        old_count = len(was.get('photos_ignore') or [])
        new_count = len(now['photos_ignore'])
        if new_count > old_count:
            detail = 'photos you were keeping are now excluded'
        elif new_count < old_count:
            detail = 'photos you were excluding are not in the catalog yet'
        else:
            detail = 'the catalog was built from the old list'
        reasons.append(
            f'the photos_ignore list in fha.yaml changed since the last scan - {detail}')
    if was.get('photos_root') != now['photos_root']:
        reasons.append(
            'the photos folder in fha.yaml changed since the last scan - the '
            'catalog still describes the old one')
    if not reasons:
        return None
    return '; '.join(reasons)


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

    `photos_ignore:` prunes this walk exactly as it prunes the scan (#35).
    Both halves of that matter: an ignored file is not in the catalog and can
    never make it out of date, so letting one drive the watermark would mark a
    current catalog stale - and `fha find --text` skips cataloged photo
    captions whenever the catalog is stale, so a single touched file in a bulk
    export would silently switch off caption search until a rescan that has
    nothing to do. The pruning also keeps this check from walking the 60,000
    files the setting exists to avoid walking.

    Editing that setting is itself a freshness dependency, checked before the
    walk (`photoindex_config_drift`): the patterns decide what the catalog
    holds, but changing them moves no file's mtime, so the watermark cannot
    see it. Both directions are real and neither self-heals. Add a pattern and
    the rows for the newly excluded files stay searchable. Remove one and the
    files it was hiding are older than photos.sqlite, so they can never raise
    the watermark - they would stay out of the catalog forever with the status
    reading 'fresh'. The same is true of repointing `roots: photos`. The
    catalog is reported 'stale' (the one status every caller already handles
    correctly) and the reason is available in plain words from
    `photoindex_config_drift` for the CLI to print alongside it.
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

    if photoindex_config_drift(archive_root, fha_config) is not None:
        # A configuration change has no file mtime of its own to be behind, so
        # the lag reported is the catalog's own age - how long ago it was built
        # from settings that no longer apply. Returned before the walk because
        # the walk is the expensive part and its answer cannot change this one.
        return ('stale', max(0.0, time.time() - mtime))

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
        try:
            patterns = photos_ignore_patterns(fha_config)
        except RuntimeError:
            # A malformed photos_ignore: is the scan's error to report, in its
            # own plain words. Here it means "prune nothing": the watermark
            # covers the whole root, the catalog reads stale, and the human is
            # sent to `fha photoindex` - which is where the real explanation
            # of the broken setting is waiting.
            patterns = []
        is_ignored = photos_ignore_matcher(patterns)
        # Directory mtimes are included (not just file mtimes) so that a deletion
        # or rename - which bumps the parent directory's mtime but touches no
        # remaining file - still makes the index look stale instead of silently
        # staying 'fresh' with photo_fts rows pointing at files that no longer exist.
        # os.walk (not rglob) because an ignored folder must be pruned rather
        # than walked and discarded - pruning is the whole point on a root
        # holding a 60,000-file export. It also yields the root itself as the
        # first dirpath, so the root's own mtime needs no separate stat.
        #
        # The `onerror` recorder is the freshness half of the rule in "Walking
        # a tree that might not open": without it, a folder this walk cannot
        # list contributes NO mtime - neither its files' nor its own - and the
        # watermark comes back lower than the truth, so a catalog missing
        # everything inside that folder reports itself `fresh`. Worse, if the
        # photos ROOT is what will not list, the walk yields nothing at all
        # and the answer falls back to the index/record mtimes alone.
        unreadable_dirs: list[Path] = []
        on_error = unreadable_dir_recorder(unreadable_dirs)
        for dirpath, dirnames, filenames in os.walk(photos_root, onerror=on_error):
            rel_dir = Path(dirpath).relative_to(photos_root).as_posix()
            prefix = '' if rel_dir == '.' else f'{rel_dir}/'
            if patterns:
                dirnames[:] = [d for d in dirnames if not is_ignored(f'{prefix}{d}')]
            candidates = [Path(dirpath)]
            candidates += [
                Path(dirpath) / name for name in filenames
                if not (patterns and is_ignored(f'{prefix}{name}'))
            ]
            for p in candidates:
                try:
                    m = p.stat().st_mtime
                    if m > max_mtime:
                        max_mtime = m
                except OSError:
                    # A dangling symlink or a file that vanished mid-walk is
                    # not a freshness signal we can read; skip it.
                    pass

        if unreadable_dirs:
            # Fail closed. There is no honest watermark for a folder nobody
            # could open: any of its photos may have changed a second ago, and
            # the folder's own mtime says nothing about the files inside it.
            # Reporting 'now' means the catalog keeps reading `stale` for as
            # long as the folder stays shut, which is exactly the state it is
            # in - `fha photoindex` is the command that says which folder and
            # why, so the human is never left with an unexplained staleness.
            max_mtime = max(max_mtime, time.time())

    if max_mtime == 0.0 or mtime >= max_mtime:
        return ('fresh', 0.0)          # empty root, or db newer than newest photo/index
    return ('stale', max_mtime - mtime)


# ── Safety copies of originals (`originals_backup:`, TOOLING §13f) ────────────
#
# Four places in the tools write embedded metadata into an original photo:
# `fha process` (the SOURCE: keyword and its rollback) and `fha photoindex`
# (tag-person's P-id keyword, set-summary's UserComment).  All four use
# exiftool's `-overwrite_original_in_place`, which is deliberate - it edits the
# file rather than replacing it, so an external photo library (Lightroom) does
# not lose track of it - but it also means the only copy of a family photograph
# is being rewritten with no copy anywhere.
#
# `originals_backup:` in fha.yaml names a folder outside the archive where ONE
# pristine copy of each such file is kept before it is first written to.  The
# rule lives here, not in the two tools, because a data-safety guard duplicated
# four ways is a guard that drifts.

class BackupRefused(Exception):
    """The safety copy could not be made, so the write must not go ahead.

    Carries the finished, human-facing sentence (file, cause, next step) that
    the caller puts straight into its existing per-file failure channel - the
    writers already report one error string per path, and a refused backup is
    just another reason a particular file was not written.
    """


def originals_backup_dir(fha_config: dict, archive_root: str | Path) -> Path | None:
    """The resolved `originals_backup:` folder from fha.yaml, or None if unset.

    Shape and tolerance follow the settings either side of it: one plain path
    like `roots:`/`backup: path:` (absolute used as-is, relative joined to the
    archive root - SPEC §12.4), read the way `photos_ignore_patterns` reads its
    own key, and a value whose shape is not understood raises a plain
    RuntimeError carrying a copy-pasteable example rather than guessing.

    Degrading differs from `photos_ignore:` in one direction on purpose.  A
    malformed ignore list, read as "ignore nothing", catalogs too much; a
    malformed backup setting read as "no backup configured" would silently
    drop protection the human asked for, at the one moment it matters.  So an
    unreadable value - including an empty string, which is a half-finished
    edit, not an "off" switch - is an error the caller must surface, never an
    absent setting.
    """
    raw = fha_config.get('originals_backup')
    if raw is None:
        return None
    text = str(raw).strip() if isinstance(raw, (str, os.PathLike)) else ''
    if not text:
        raise RuntimeError(
            'originals_backup in fha.yaml must be one folder path, outside your '
            'archive, where a safety copy of each photo is kept before fha '
            'writes into it (e.g.\n'
            '  originals_backup: D:/PhotoOriginals)\n'
            'Remove the line to turn safety copies off.'
        )
    p = Path(text)
    return (p if p.is_absolute() else Path(archive_root) / p).resolve()


def _fold_path_text(name: str) -> str:
    """One path spelling with case and Unicode composition flattened away.

    macOS and Windows both hand back a name whose capitals - and, on HFS+,
    whose accent composition - differ from the one that was asked for, so two
    strings can name one folder.  Used only by the containment guard below,
    where matching too much costs the human one sentence and matching too
    little costs him a photograph.  (`fha backup`'s destination guard reaches
    the same conclusion for its zips; the rule is stated twice because tools
    never import tools and backup.py owns its own copy.)
    """
    return unicodedata.normalize('NFC', name).casefold()


def _path_contains(parent: str | Path, child: str | Path) -> str | None:
    """How `parent` contains `child`: 'exact', 'name-fold', or None.

    Answered by climbing `child`'s resolved parents rather than by a string
    prefix, so a destination that does not exist yet still gets a true answer
    (the climb reaches the folder that will hold it), and so two spellings of
    one folder are one folder.  A match found only after folding is reported
    separately: on a case-sensitive disk those really are two folders, and a
    refusal that insisted otherwise would be a dead end.
    """
    target = Path(child).resolve()
    base = Path(parent).resolve()
    cur = target
    while True:
        if cur == base:
            return 'exact'
        if cur.parent == cur:
            break
        cur = cur.parent
    folded = _fold_path_text(str(base))
    cur = target
    while True:
        if _fold_path_text(str(cur)) == folded:
            return 'name-fold'
        if cur.parent == cur:
            return None
        cur = cur.parent


def format_size(n: int) -> str:
    """Bytes as a size a human reads (B/KB/MB/GB/TB, one decimal place)."""
    size = float(n)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f'{int(size)} {unit}' if unit == 'B' else f'{size:.1f} {unit}'
        size /= 1024
    return f'{int(n)} B'


class OriginalBackup:
    """One run's safety-copy policy for originals fha writes metadata into.

    Built once per command and handed to every writer in that run, which is
    what makes "warn once, not once per photo" true on an 88,000-file library
    and what lets one line report the run's whole disk cost.

    Three behaviours, decided by `originals_backup:`:

      * configured    - `ensure()` copies the file to a path-mirrored place
                        under the destination before the first write to it,
                        and raises `BackupRefused` if that copy fails.  A file
                        that already has a copy there is left alone: the
                        valuable artifact is the photo as it was before fha
                        ever touched it, so the SECOND keyword write must not
                        overwrite it with an already-modified version.  That
                        also bounds the cost at one copy of each photo written.
      * not configured - `ensure()` warns once for the whole run, naming the
                        setting and what it protects, and proceeds.  Safety
                        copies are opt-in; refusing here would break every
                        existing archive on upgrade.
      * misconfigured  - a value that cannot be read, or a destination inside
                        the archive or an asset root, refuses every write with
                        the cause and the fix.  Fail closed: a backup that
                        silently did not happen is worse than none, because it
                        is relied on.

    **Identity.** The copy is filed under the file's alias path
    (`photos/1912/margaret.jpg` -> `<dest>/photos/1912/margaret.jpg`) - the same
    identity the source record's `files:` entry carries, and the one a human can
    read.  SPEC §12.1 is right that a photo's filename is not its identity (the
    embedded `SOURCE:` keyword is, with the path as a hint), and the honest
    consequence is stated here rather than papered over: if another system later
    renames or moves the photo, this copy stays where it was made, under the old
    path.  Nothing is lost - the pristine copy is still on disk with its
    contents intact - but the next write to the photo at its new path takes a
    fresh copy there, and that one is of a file fha has already written to.  The
    durable identity cannot fill the gap: the first write of all is `fha
    process` MINTING the `SOURCE:` keyword, so at the moment the pristine copy
    matters most there is no keyword to key it by.
    """

    def __init__(self, archive_root: str | Path, fha_config: dict) -> None:
        self.archive_root = Path(archive_root)
        self.fha_config = fha_config or {}
        self.copied = 0          # pristine copies made this run
        self.already = 0         # files that already had one
        self.bytes = 0           # bytes written this run
        self.dest: Path | None = None
        self.refusal: str | None = None   # why no write may proceed, if so
        self._pending: list[tuple[str, str]] = []
        self._announced = False
        self._reported = (0, 0, 0)
        try:
            self.dest = originals_backup_dir(self.fha_config, self.archive_root)
        except RuntimeError as e:
            self.refusal = str(e)
            return
        if self.dest is not None:
            self.refusal = self._destination_conflict(self.dest)

    # -- configuration ----------------------------------------------------

    def _protected_folders(self) -> list[tuple[str, Path]]:
        """The folders a safety copy must never land inside, labelled.

        Every mapped asset root (plus the spec defaults, which exist whether or
        not `roots:` names them) and the archive root itself.  A copy inside an
        asset root would be scanned, keyworded and counted as a second photo of
        the same picture - it would defeat the thing it is for.  A copy
        elsewhere inside the archive is refused for the reason `fha backup`
        refuses it for zips (§13e): a copy that lives inside the thing it
        protects shares its disk, its sync folder and its accidents.
        """
        out: list[tuple[str, Path]] = []
        for alias in sorted(set(get_roots(self.fha_config)) | {'photos', 'documents'}):
            out.append((f'your {alias} root',
                        resolve_path(alias, self.fha_config, self.archive_root)))
        out.append(('your archive', self.archive_root))
        return out

    def _destination_conflict(self, dest: Path) -> str | None:
        """Plain refusal text if `dest` is not a safe place to keep copies.

        Checked in both directions.  A destination inside a protected folder is
        the obvious mistake; a destination that CONTAINS one (`originals_backup:
        D:/Family` with `roots: photos: D:/Family/Photos`) puts the live library
        inside the safety copies and ends in the same place.
        """
        for label, folder in self._protected_folders():
            inward = _path_contains(folder, dest)
            outward = _path_contains(dest, folder)
            if inward is None and outward is None:
                continue
            # The archive root is checked last, so an asset root inside the
            # archive is reported as the asset root - the more specific and
            # more alarming of the two reasons.
            if label == 'your archive':
                harm = ('Copies kept there share the archive\'s disk, its sync '
                        'folder and its accidents, which is most of what they '
                        'are meant to survive.')
            else:
                harm = ('Copies kept there would be scanned, keyworded and '
                        'counted as extra photos of the same picture, and a '
                        'lost disk would take the copies with the originals.')
            if inward == 'name-fold' or outward == 'name-fold':
                return (
                    f'the safety-copy folder {dest} is spelled the same as '
                    f'{label} ({folder}) apart from capital letters or accents. '
                    f'On most Macs and on every Windows PC those are ONE folder, '
                    f'so the copies would land inside the very thing they are '
                    f'protecting. {harm} Point `originals_backup:` in fha.yaml at a '
                    f'clearly different folder outside your archive '
                    f'(e.g. originals_backup: D:/PhotoOriginals), then re-run.'
                )
            where = 'inside' if inward else 'the folder holding'
            return (
                f'the safety-copy folder {dest} is {where} {label} ({folder}). '
                f'{harm} Point `originals_backup:` in fha.yaml at a folder '
                f'outside your archive (e.g. originals_backup: D:/PhotoOriginals), '
                f'then re-run.'
            )
        return None

    # -- the guard --------------------------------------------------------

    def _alias_path(self, path: Path) -> str | None:
        """`path` as the archive files it (`photos/1912/x.jpg`), or None.

        The most specific root wins, so a documents root nested inside a photos
        root does not answer for its own files.  One function for both readers -
        the copy's filename and the refusal's wording - so the file a message
        names is always the file the copy was filed under.
        """
        resolved = Path(path).resolve()
        best: tuple[int, str] | None = None
        for alias in sorted(set(get_roots(self.fha_config)) | {'photos', 'documents'}):
            root = resolve_path(alias, self.fha_config, self.archive_root).resolve()
            try:
                rel = resolved.relative_to(root)
            except ValueError:
                continue
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, f'{alias}/{rel.as_posix()}')
        return best[1] if best else None

    def _copy_target(self, path: Path) -> Path:
        """Where `path`'s pristine copy is filed under the destination.

        The alias path when the file is under a mapped root, the
        archive-relative path when it is inside the archive but under no root,
        and `_elsewhere/…` for a file outside both - a case `fha process` can
        still be pointed at.
        """
        alias_path = self._alias_path(path)
        if alias_path is not None:
            return self.dest / alias_path
        resolved = Path(path).resolve()
        try:
            return self.dest / resolved.relative_to(self.archive_root.resolve())
        except ValueError:
            pass
        stripped = str(resolved)[len(resolved.anchor):].replace('\\', '/').strip('/')
        return self.dest / '_elsewhere' / stripped

    def ensure(self, path: str | Path) -> None:
        """Make sure a pristine copy of `path` exists before it is written to.

        Returns quietly when the copy is in place (or was already), warns once
        per run when the setting is absent, and raises `BackupRefused` when the
        setting is on and the copy did not happen - the caller must then not
        write.  The copy lands via a `.part` temporary and `os.replace`, so an
        interrupted run leaves either a complete copy or none; a half-written
        one would be indistinguishable from a pristine copy on the next run,
        which is the one wrong answer this whole feature exists to prevent.
        """
        src = Path(path)
        if self.refusal is not None:
            raise BackupRefused(
                f'refused to write to {self._label(src)}: {self.refusal} '
                f'Nothing was written to the file.'
            )
        if self.dest is None:
            self._warn_unconfigured()
            return
        target = self._copy_target(src)
        if target.exists():
            self.already += 1
            return
        tmp = target.with_name(target.name + '.part')
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, tmp)
            os.replace(tmp, target)
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise BackupRefused(
                f'refused to write to {self._label(src)}: its safety copy could '
                f'not be made at {target} ({e}). Nothing was written to the file. '
                f'Check that the folder in `originals_backup:` exists and has room, '
                f'then re-run - or remove `originals_backup:` from fha.yaml to write '
                f'without safety copies.'
            ) from e
        self.copied += 1
        try:
            self.bytes += target.stat().st_size
        except OSError:
            pass

    def _label(self, path: Path) -> str:
        """The file named the way the human filed it (alias form when possible)."""
        return self._alias_path(path) or Path(path).name

    # -- what the human is told -------------------------------------------

    def _warn_unconfigured(self) -> None:
        if self._announced:
            return
        self._announced = True
        self._pending.append((
            'warning',
            'No safety copies are being kept. fha is about to write keywords or '
            'captions into your original photo files, and there is no copy to '
            'fall back on if a write is interrupted. Add one line to fha.yaml to '
            'keep one pristine copy of each photo before it is first written to:\n'
            '  originals_backup: D:/PhotoOriginals\n'
            '(any folder outside your archive; copies are made once per photo, '
            'so the space it needs is one copy of the photos you work on).',
        ))

    def announce(self) -> None:
        """Say up front what will happen, before the human confirms a write.

        Called by the command layer ahead of its yes/no prompt so the
        no-safety-copies warning arrives while it can still be acted on -
        after 500 photos are written it is only news.
        """
        if self.refusal is not None:
            if not self._announced:
                self._announced = True
                self._pending.append((
                    'warning',
                    f'Safety copies are configured but unusable: {self.refusal} '
                    f'Until that is fixed, these writes will be refused.',
                ))
            return
        if self.dest is None:
            self._warn_unconfigured()
            return
        if not self._announced:
            self._announced = True
            self._pending.append((
                'info',
                f'Safety copies: a pristine copy of each photo is kept in '
                f'{self.dest} before it is written to.',
            ))

    def drain_messages(self) -> list[tuple[str, str]]:
        """Take the messages not yet reported: (level, text) pairs.

        Draining rather than reading keeps "warn once per run" true no matter
        how many times a caller asks, and lets a batch command (`fha process`
        over a folder) report each group's copies as they happen without
        restating the ones it already reported.
        """
        out = list(self._pending)
        self._pending.clear()
        counts = (self.copied, self.already, self.bytes)
        if counts != self._reported:
            copied = self.copied - self._reported[0]
            already = self.already - self._reported[1]
            written = self.bytes - self._reported[2]
            self._reported = counts
            parts = []
            if copied:
                # The size is the point of this line: it is what a human weighs
                # when deciding whether to keep the setting on.
                parts.append(f'{copied} original(s) copied to {self.dest} '
                             f'({format_size(written)})')
            if already:
                parts.append(f'{already} already had a copy from an earlier run')
            if parts:
                out.append(('info', 'Safety copies: ' + '; '.join(parts) + '.'))
        return out


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
    write half of a round-trip even when the read half preserved it.

    DO NOT USE THIS ON AN ARCHIVE RECORD - use `write_text_exact_atomic` below.
    Opening in `'w'` mode truncates the target before the first byte is
    written, so a write that dies partway (disk full, the process killed)
    leaves the record holding its first few bytes and nothing else, while the
    caller's `except OSError` reports a clean refusal. The archive's truth is
    destroyed by a command that says nothing happened. That defect reached ten
    call sites in `person.py` and eight more across `places`, `confirm`,
    `lint`, `normalize_links`, `serve`, `stubs` and `packet` before it was
    found, purely because this function and the atomic one sit next to each
    other with near-identical names and nothing here said which to reach for.

    The only cases where this writer is defensible are ones where the target
    holds nothing worth keeping - a file being created for the first time on a
    path a preflight has proven empty (`convert_mining.apply_plan`'s
    `write_new`, `gedcom_import`'s), or disposable output under `.cache/` or
    `generated/`. Even there it is merely sufficient, never better: the atomic
    writer costs one rename and is correct everywhere. If you are adding a new
    call site, the answer is almost certainly `write_text_exact_atomic`."""
    with Path(path).open('w', encoding='utf-8', newline='') as f:
        f.write(text)


def _refuse_unwritable_target(path: Path) -> None:
    """Raise the `PermissionError` an in-place write would have raised.

    `os.replace` swaps a DIRECTORY ENTRY, so the kernel checks write+execute on
    the parent FOLDER and never asks whether the caller may write the file being
    replaced. `open(path, 'w')` asks exactly that. Without this probe, every verb
    converted to the atomic writer quietly gained the power to overwrite a record
    the plain writer refused - and a record made read-only is how a careful
    archivist pins a file he does not want touched. Honour it.

    The probe is a real `os.open(..., O_WRONLY)` rather than `os.access` because
    only the kernel answering the actual open matches the writer being restored.
    `os.access` asks with the REAL uid/gid instead of the effective one, and its
    answer is documented as unreliable under POSIX ACLs and on network
    filesystems - so it can say "writable" exactly where the old writer refused,
    and refuse where it wrote. Asking the true question costs one file descriptor
    and changes nothing: no `O_CREAT` and no `O_TRUNC`, so the record is neither
    created nor emptied and its timestamps are untouched. `O_NONBLOCK` matters
    only for the exotic case of a named pipe sitting at a record's path, where it
    stops the probe waiting forever for a reader.

    Only a `PermissionError` becomes a refusal. Any other error means the
    question was something else - a directory at the path, a file that vanished
    between the calls - and is left to the write itself to report in its own
    words rather than guessed at here.

    Root bypasses file permission bits entirely, so this grants what a plain
    `open(path, 'w')` also granted: root has always been able to overwrite a 0444
    record, and this is not the layer that changes that.
    """
    try:
        fd = os.open(str(path), os.O_WRONLY | getattr(os, 'O_NONBLOCK', 0))
    except FileNotFoundError:
        return                    # a record that does not exist has no mode to honour
    except PermissionError:
        raise                     # byte for byte what open(path, 'w') raised
    except OSError:
        return
    os.close(fd)


def _carry_ownership(tmp_name: str, target: os.stat_result) -> None:
    """Give the temp file the record's owner and group before it lands.

    The mode alone is not the whole permission: the temp file belongs to whoever
    ran the command, so on a shared archive the replace would hand the record to
    a new owner and group. A 0640 record whose group flips from `family` to the
    caller's own is the same lost read access the mode copy above exists to
    prevent - just spelled with a different field.

    Best effort by necessity, and that is not a weakness: only root may hand a
    file to another user and only a member may hand one to another group, so
    every failure here is a change the caller could not have made anyway. The
    group-only retry is the case that actually fires - two people in one `family`
    group, neither of them root. Called BEFORE the `chmod` because `chown` clears
    the setgid bit that the mode copy is about to restore.
    """
    if not hasattr(os, 'chown'):            # Windows has no POSIX ownership
        return
    for uid, gid in ((target.st_uid, target.st_gid), (-1, target.st_gid)):
        try:
            os.chown(tmp_name, uid, gid)
            return
        except OSError:
            continue


def write_text_exact_atomic(path: str | Path, text: str) -> None:
    """Crash-safe `write_text_exact`: the target is only ever the old bytes or
    the new bytes, never a torn half.

    `write_text_exact` opens the target in `'w'` mode, which truncates it before
    the first byte is written - so a write that dies partway (disk full, the
    process killed, an interrupted promotion) leaves the record truncated on
    disk. For a person record that is often the SOLE copy of that ancestor, a
    half-written file is the archive's worst outcome, and the promotion writer
    below reports 'nothing was left half-promoted' - a promise a truncating
    write cannot keep. This writes the full text to a sibling temp file, flushes
    it to the platter (`fsync`), then `os.replace`s it over the target in one
    atomic step. On any failure the original is left exactly as it was and the
    temp file is removed, so callers can set their 'wrote it' flag AFTER this
    returns and trust that a raise means the target was never touched. Newline
    handling mirrors `write_text_exact` (`newline=''` - no CRLF translation).

    PERMISSIONS: `os.replace` swaps a directory entry, which the kernel judges
    by the parent FOLDER - it never looks at the record being replaced. Two
    things follow, both handled here. The record would end up wearing the temp
    file's identity, so mode and ownership are fixed up before the replace: an
    existing target keeps its own permission bits, group and owner, a new record
    gets the plain-open umask default. Without that a promotion would quietly
    demote a group-readable record to owner-only. And a record the caller may NOT
    write would be replaced anyway, where `write_text_exact` raised
    `PermissionError` on the open and the command refused - so
    `_refuse_unwritable_target` puts that question back before anything is
    written, and the same `PermissionError` (errno EACCES, the record's own path)
    reaches the caller's `except OSError`.

    NOT preserved, by nature of an atomic replace: a target that is a symlink or
    one name of a hard-linked pair is REPLACED by the new regular file rather
    than written through, so the link breaks and the other name keeps the old
    bytes. The archive has no symlinks by rule (AGENTS.md Don'ts) and no tool
    makes a hard link, so this is a hand-built structure the writer cannot honour
    - not a case it silently gets wrong. Extended attributes and POSIX ACLs do
    not survive the swap either; mode, group and owner are what the archive's own
    permission model is written in. And a read-only PARENT folder refuses here
    where a plain write would have succeeded, because the temp file has to be a
    sibling for the rename to be atomic: an ordinary OSError refusal that costs
    the human nothing but a message."""
    path = Path(path)
    # Refuse a pinned record BEFORE creating anything: the refusal then costs no
    # I/O and leaves no temp file to clean up.
    _refuse_unwritable_target(path)
    # Temp file must share the target's directory so os.replace is a same-
    # filesystem rename (atomic); a cross-device temp would fall back to a
    # non-atomic copy. The leading dot keeps the stray temp hidden and out of
    # record globs if the process is killed between fsync and replace.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # os.replace installs THIS temp inode as the record, and mkstemp made it
        # 0600 (owner-only). Left alone that silently strips a normal 0644 /
        # group-readable record to owner-only during the tier flip, and other
        # family members or a backup job in a shared archive lose read access.
        # Match what a plain write_text_exact (open(..., 'w')) would leave: an
        # EXISTING target keeps its own mode; a NEW record gets the umask default
        # (0o666 & ~umask), never the 0600 mkstemp handed us.
        try:
            target = os.stat(str(path))
        except FileNotFoundError:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp_name, 0o666 & ~umask)
        else:
            # Owner and group first (chown clears setgid), then the permission
            # bits (incl. setgid/sticky, so a group-shared archive folder's
            # inheritance survives) onto the temp before it lands.
            _carry_ownership(tmp_name, target)
            os.chmod(tmp_name, target.st_mode & 0o7777)
        os.replace(tmp_name, str(path))
    except OSError:
        # The replace never happened, so the original (if any) still stands;
        # drop the partial temp so no untracked file is left behind.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


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


def read_record(path: str | Path, on_decode_error=None) -> dict:
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
            'undecodable': bool,    the file's BYTES are not UTF-8, so nothing
                                    below was read from it - distinct from a
                                    record that decoded and then would not
                                    parse (that is `parse_errors`), and from a
                                    file that could not be opened at all (also
                                    `parse_errors`, as it always was). Only
                                    ever True when `on_decode_error` was
                                    supplied; see below.
            'parse_errors': list,   [(code, message), ...]
        }

    Every record read in the suite funnels through here, so this is the site
    #68's crash is actually reached from: a person or source file saved in
    another codepage (cp1252, a Windows editor's default) takes down whatever
    command is running, because `UnicodeDecodeError` is a ValueError and the
    guard below catches `OSError`.

    `on_decode_error` is how a caller asks for that to be REPORTED instead.
    Supply it and a bad decode calls it with the `Path` (the callback
    contract `read_text_or_report` shares) and returns the empty record with
    `undecodable: True`, for the caller to skip. **Omit it and the read still
    raises**, exactly as it always has - deliberately, and this is the part
    worth reading before changing it:

      an undecodable record answered as an empty one is INVISIBLE to the two
      guards this codebase actually uses. A caller that tests
      `rec['parse_errors']` sees none (the record is not malformed - nothing
      was read), and a caller that wraps the read in `except Exception` never
      fires. Both then proceed on empty content. Empty content is not neutral
      here: `restricted:` reads as absent, so `fha site` and `fha wikitree`
      publish a person who asked to be left out; `fha process`'s sidecar
      refusal is skipped and the stub is deleted after scaffolding a record
      from nothing; `fha stubs` mints a second record for a P-id that already
      has one. A traceback is a bad answer, but it is a LOUD one, and every
      one of those is worse.

    So the report is opt-in, per caller, and a caller opts in by having
    somewhere to put the report: `fha lint` (one W128 naming the file) and
    `fha index` (the build's undecodable-files warning). The rest of #68's
    sites are fixed by giving them a channel, one at a time - not by making
    this function quietly hand every existing guard an empty record.
    """
    path = Path(path)
    errors: list[tuple[str, str]] = []

    try:
        text = path.read_text(encoding='utf-8')
    except OSError as e:
        return {
            'meta': {}, 'claims': [], 'stories': None, 'body': '',
            'unfenced_claims': False, 'undecodable': False,
            'parse_errors': [('E010', f'Cannot read file: {e}')],
        }
    except UnicodeDecodeError:
        if on_decode_error is None:
            raise   # see the docstring: silence here defeats the callers' guards
        on_decode_error(path)
        # No E010: the record is not malformed and the human has nothing to
        # correct inside it - only its encoding is wrong, which is a warning
        # (`fha lint`'s W128 / `fha index`'s own note), not a spec violation.
        # The caller asked for this shape and reads `undecodable` to skip it.
        return {
            'meta': {}, 'claims': [], 'stories': None, 'body': '',
            'unfenced_claims': False, 'undecodable': True,
            'parse_errors': [],
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
        'undecodable': False,
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

    Returns dict with keys: id_str, id_type, kind (for persons), is_companion,
    kind_ambiguous.  Returns None if filename doesn't match any expected pattern.

    What this parser CANNOT do, and why `kind_ambiguous` exists: SPEC §13 allows
    underscores inside given names, so the optional companion-kind slot and the
    last given-name segment are the SAME slot.  `hartley__marie_timeline_P-…` is
    either a generated timeline or the profile of Marie Timeline Hartley, and no
    reading of the name will ever separate them.  Whenever the kind was matched
    by suffix - and so could equally be part of the name - `kind_ambiguous` is
    True and `kind`/`is_companion` are a GUESS.

    A caller that can see the file's frontmatter must let the content decide
    (`person_file_kind`): a file that says it is a person record outranks a file
    that is merely named like a companion.  A caller that cannot see the
    frontmatter - a rename planner, a path-shaped lookup - has to treat
    `is_companion` as the hint it is.
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
        'kind_ambiguous': False,
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
                # Matched by suffix, so it may equally be the last given name
                # (Marie Timeline Hartley).  Say so rather than let the guess
                # travel as a fact.
                result['kind_ambiguous'] = True
                break
        if result['kind'] is None:
            result['kind'] = 'profile'
        # Verify double-underscore surname separator
        if '__' not in before_id.split('_research')[0].split('_timeline')[0].split('_sources-index')[0]:
            # May be a source file accidentally named with P-id - not valid person filename
            pass

    return result


# SPEC §9's required person-record fields are `id`, `name` and `living`.  Only
# the last two are usable as a "this file is a person record" signal: the
# research companion (SPEC §16, `RESEARCH_TEMPLATE_FALLBACK`) carries an `id:`
# of its own, so testing `id` would promote every research file in every
# archive to a profile.  `name` and `living` appear on person records and on
# nothing else the person walker meets.
PERSON_RECORD_FIELDS = ('name', 'living')


def carries_person_record_fields(meta: dict) -> bool:
    """True when a file's own frontmatter asserts it IS a person record (SPEC §9).

    Why content and not the filename: SPEC §13's person grammar is
    `{primary_sort_name}__{given_names}[_{kind}]_{P-id}.md` and underscores
    inside given names are legal, so the optional kind slot and the last
    given-name segment are the SAME slot.  The grammar is ambiguous by
    construction - `hartley__marie_timeline_P-…` is either a generated timeline
    or the profile of Marie Timeline Hartley, and no reading of the name will
    ever tell them apart.  Reading it the wrong way is the expensive direction:
    `index._index_person` writes the `persons` row only for a profile, so a
    misclassified profile gets no row at all and the person vanishes from
    `fha find`, every view, every count, the tree, the site, GEDCOM, WikiTree
    and every packet - while the file sits there untouched, so nothing looks
    broken.  A file that says what it is outranks a file that is merely named
    something; the filename stays a hint, content overrides it.  The same test
    catches the inverse error, a generated companion someone hand-edited into a
    real record.

    Key presence, not truthiness: `living: false` is the commonest value the
    field takes and is falsy in Python, so a plain `meta.get('living')` would
    read a long-dead ancestor's record as carrying nothing.

    This lives in `_lib` and nowhere else because index and lint drifting on
    exactly this question is what lost the person in the first place: the
    indexer read her file one way and the linter the other, so the archive had
    no row for her and `fha lint` reported it clean.
    """
    for field in PERSON_RECORD_FIELDS:
        value = meta.get(field)
        if field in meta and value is not None and str(value).strip():
            return True
    return False


def person_file_kind(path: str | Path, meta: dict) -> str:
    """What a file under people/ IS: 'profile' or a SPEC §13 companion kind.

    Content decides, the filename hints.  A file whose frontmatter carries the
    SPEC §9 person-record fields is a profile whatever its stem says; a
    kind-suffixed stem with no such frontmatter is the generated companion it
    looks like (see `carries_person_record_fields` for why the stem alone
    cannot answer).

    Note the asymmetry: content can only promote a file TO a profile, never
    demote one.  A profile-named file with sparse frontmatter (a stub carrying
    just `id:`) stays a profile, which is what it is.

    Callers guard their own non-record files first: the kind slot is read for
    `.md` only, so a stray `.txt` under people/ answers 'profile' here.
    """
    if carries_person_record_fields(meta):
        return 'profile'
    parsed = parse_filename(path)
    return (parsed or {}).get('kind') or 'profile'


def is_person_file_kind(path: str | Path, kind: str, meta: dict | None = None) -> bool:
    """True when a person file is of `kind` - SPEC §13's slot, content first.

    The kind slot is one place - immediately before the P-id
    (`hartley__thomas_research_P-…`) - so the same word anywhere else in the
    name is part of the given names. The substring test this replaces
    (`'_research_' in stem`) read `smith__research_anne_P-…`, the profile of a
    woman whose given names are Research Anne, as a research companion: her
    `## Hypotheses` entries became archive-wide hypothesis records and her
    `## Open Questions` block joined the question scope, neither of which SPEC
    §16 homes in a profile.

    `meta` closes the other half of the same hole. The slot before the P-id is
    ALSO a legal last given name, so `smith__anne_research_P-…` may be Anne
    Research Smith's own record - and read as a research file, her whole file
    went into lint's E009 research scope and the report's question scope just
    the same. A caller holding the record passes its frontmatter and gets the
    content-first answer (`person_file_kind`); one that only has a path keeps
    the filename-only reading, which is a guess (`parse_filename`'s
    `kind_ambiguous`) and is documented as one.

    A file with no id at all is the one case the grammar cannot parse. That is
    a real state, not an error - a mid-graduation companion is named before the
    id is minted (`smith__anne_research.md`) - and with the id absent the kind
    slot sits at the end of the stem, so the suffix test is exact there rather
    than a guess. `smith__research_anne.md` still reads as a profile.
    """
    if meta is not None and carries_person_record_fields(meta):
        return kind == 'profile'
    parsed = parse_filename(path)
    if parsed is not None:
        return parsed.get('id_type') == 'P' and parsed.get('kind') == kind
    return Path(path).stem.endswith(f'_{kind}')


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


def edtf_confidence(edtf: str | None) -> tuple[int, int]:
    """
    Sortable confidence score for an EDTF string: more present components
    (day > month > year) beats fewer, and an unmarked (fully confident)
    component beats '~' beats '?' (SPEC §20).

    Two readings of one score. `fha photoindex` sorts by the whole tuple to
    pick a variation group's best date among its variants; `fha photoindex
    triage` and `fha process` folder triage read only the second element,
    where 0 means "no approximation marker anywhere" - the confident-date
    evidence signal (TOOLING §15b).

    Lives here rather than in photoindex.py because those two triage rankings
    are documented as ordering the same folder the same way, and a scoring
    rule copied into the second tool is a rule that drifts (it did: the
    process-side copy tested a raw keyword body and could never fire).
    """
    if not edtf:
        return (-1, 0)
    n_components = edtf.count('-') + 1
    if '?' in edtf:
        marker_rank = 2
    elif '~' in edtf:
        marker_rank = 1
    else:
        marker_rank = 0
    return (n_components, -marker_rank)


# ── Photo DATE: keyword resolution (SPEC §20) ─────────────────────────────────
#
# Every tool that reads a photo's date reads it here. `fha photoindex` resolves
# it once per scan into `photos.edtf`; `fha process` folder triage resolves it
# live off the file it is about to rank. The two are documented as ordering the
# same folder the same way, so the grammar, the EXIF pairing and the
# what-counts-as-a-date rule are one implementation, not two.

# The DATE: keyword states the PRECISION of a photo's date and nothing else
# (SPEC §20 rule 1: 'Y!M!D!', 'Y!M~', 'Y~', 'Y!M?D?' - '!' confident, '~'
# best guess, '?'/omitted unknown, per component). The date's VALUE lives in
# EXIF DateTimeOriginal - photo metadata cannot hold a partial date, so the
# pipeline writes a forced full YYYY-MM-DD there (rule 2: a technical
# workaround, never truth on its own) and the keyword says which components
# of it are to be believed. This letter grammar is the whole of what a photo's
# DATE: keyword may say, and matching it here is what decides whether a photo
# gets a date at all: the code once also read digits straight off a keyword
# body ('1942!-11!-25!'), which matched nothing on a real library and left
# `edtf` NULL on every row with every date feature silently dead (#40), and
# which the archive owner then ruled out of the spec outright (2026-08-16 -
# see `resolve_photo_edtf`). Trailing time components (H hour, M minute, S
# second) are matched and discarded - the archive's EDTF is date-only; note
# the second 'M' means minutes, so a real pattern can read 'Y!M!D?H!M!'.
PHOTO_DATE_PATTERN_RE = re.compile(
    r'^Y([!~?])?(?:(M)([!~?])?(?:(D)([!~?])?((?:[HMS][!~?]?)*))?)?$', re.I
)
PHOTO_EXIF_DATE_RE = re.compile(r'^\s*(\d{4})[:\-](\d{2})[:\-](\d{2})(?!\d)')


def photo_date_markers_to_edtf(pattern: str) -> str | None:
    """
    Assemble an EDTF string from digits carrying per-component confidence
    markers ('1942!-11!-25!', '1960~') - SPEC §20's table, one step down.

    This is an INTERNAL assembler, not a reader of archive keywords.
    `photo_date_pattern_to_edtf` builds this digit-plus-marker form by pairing
    the spec's letter grammar with the EXIF value, then calls this to apply the
    §20 rules in one place. Nothing reads a digit-bearing string straight off
    a photo: that form is outside the spec (see `resolve_photo_edtf`).

    Building stops at the first component that is missing or marked '?' -
    §20 states 'Y!' is deliberately equivalent to 'Y!M?D?', i.e. an
    unconfirmed component is the same as an absent one, not a reason to guess.
    """
    m = re.match(r'^(\d{4})([!~?])?(?:-(\d{2})([!~?])?(?:-(\d{2})([!~?])?)?)?$', pattern.strip())
    if not m:
        # The one caller assembles this shape itself, so this cannot fire
        # today; it stays so the function is total - a future caller gets
        # "no date" rather than an AttributeError on a None match.
        return None

    year, year_c, month, month_c, day, day_c = m.groups()
    if year_c == '?':
        return None

    # Collect the present, non-'?' components with their per-component markers.
    comps: list[tuple[str, str | None]] = [(year, year_c)]
    if month and month_c != '?':
        comps.append((month, month_c))
        if day and day_c != '?':
            comps.append((day, day_c))

    if len(comps) == 1:
        # Year only: an approximate year trails its qualifier (EDTF `1960~`).
        edtf = year + ('~' if year_c == '~' else '')
    else:
        # Multi-component: EDTF qualifies a component with a '~' written
        # immediately *before* it (SPEC §20: `Y!M~` -> `1960-~05`), so a
        # per-component best-guess marker is preserved on the right component
        # instead of being collapsed into one trailing '~' (or dropped when a
        # confident component follows the approximate one).
        edtf = '-'.join(('~' + comp if mark == '~' else comp) for comp, mark in comps)

    return edtf if is_valid_edtf(edtf) else None


def photo_date_pattern_to_edtf(pattern: str, exif_date: object) -> str | None:
    """Resolve a shape-only DATE: keyword against EXIF DateTimeOriginal.

    'Y!M!D?' + '1916:06:10 10:53:21' -> '1916!-06!-10?' -> '1916-06'
    'Y!M~D?' + '1942:03:15 00:00:00' -> '1942!-03~-15?' -> '1942-~03'
    'Y~'     + '1960:00:00 00:00:00' -> '1960~'         -> '1960~'
    'Y!M'    + '1916:06:10 10:53:21' -> '1916!-06?'     -> '1916'
    'YMD'    + '1942:11:25 10:00:00' -> '1942?-11?-25?' -> None

    Builds the digit-plus-marker form `photo_date_markers_to_edtf` already
    understands and delegates to it, so SPEC §20's rules (stop at the first
    unconfirmed component, '~' placement, validation) stay in one place.

    An omitted marker is UNKNOWN, exactly like '?' - SPEC §20 rule 1 spells
    out the grammar as "'!' confident, '~' best guess, '?'/omitted unknown",
    and rule 2 says the forced full YYYY-MM-DD in EXIF is a compatibility
    workaround that never becomes truth. Defaulting a bare component to
    confident would do precisely that: 'DATE: YMD' would turn a scanner's
    own clock into an exact archive date. So a component the pipeline did
    not affirm is dropped, and a pattern with no affirmed component at all
    ('Y', 'YMD') resolves to nothing - the photo stays undated until someone
    marks a component confident.

    A keyword body that is not this letter grammar returns None here, and that
    is the whole answer for the photo: `resolve_photo_edtf` has no second
    reader behind this one.
    """
    m = PHOTO_DATE_PATTERN_RE.match(pattern.strip())
    if not m or exif_date is None:
        return None
    d = PHOTO_EXIF_DATE_RE.match(str(exif_date))
    if not d:
        return None
    year, month, day = d.groups()
    year_c, has_month, month_c, has_day, day_c, _time_parts = m.groups()
    parts = year + (year_c or '?')
    if has_month:
        parts += '-' + month + (month_c or '?')
        if has_day:
            parts += '-' + day + (day_c or '?')
    return photo_date_markers_to_edtf(parts)


def resolve_photo_edtf(date_pattern: str | None, exif_date: object) -> str | None:
    """
    One photo's resolved EDTF date, or None when the photo carries no DATE:
    keyword.

    Only a keyworded date resolves. The keyword's presence is what marks a
    date as REVIEWED (archive-owner decision, 2026-08-15): a photo without one
    has not been looked at yet, whatever its EXIF says, and resolving EXIF
    alone would promote unreviewed machine metadata into the same field as
    human-confirmed fact - and misdate scans, where DateTimeOriginal is often
    the scan's own date (a 1925 print dated 2021 is worse than undated). So an
    un-keyworded photo stays NULL here by design, not as a gap.

    The letter form is the ONLY form this resolves (archive-owner decision,
    2026-08-16). SPEC §20 rule 1 defines the keyword grammar as per-component
    precision letters - 'Y!M!D!', 'Y!M~', 'Y~' - and nothing else; the
    parenthesised '(1942-11-25)' in that table glosses the resulting date, it
    is not a second syntax. A digit-bearing keyword ('DATE: 1880', or the
    marker form '1942!-11!-25!') is outside the spec, so the archive does not
    read a date out of it. An owner is free to type whatever he likes into his
    own keywords; the system simply does not treat a non-spec form as evidence.
    Such a photo stays undated - visibly, via the scan's
    `nonspec_date_keywords` count, rather than silently.
    """
    if not date_pattern:
        return None
    return photo_date_pattern_to_edtf(date_pattern, exif_date)


def is_nonspec_photo_date_keyword(date_pattern: str | None) -> bool:
    """True for a DATE: keyword that carries something other than the SPEC §20
    letter grammar - the forms the archive deliberately does not read.

    Counted so a library keyworded in a non-spec form (hand-typed years, the
    AI pipeline's digit-plus-marker form) reports as undated WITH a reason,
    instead of the human wondering why photos he can see a date on never
    acquired one.
    """
    if not date_pattern:
        return False
    return PHOTO_DATE_PATTERN_RE.match(date_pattern.strip()) is None


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


# ── Which sources a text search can see inside (#46) ─────────────────────────

def file_entry_carries_text(role: str, path: str) -> bool:
    """Does one `files:` entry put the source's words into the archive as text?

    Two ways it can, and they are the only two: the entry is tagged with a role
    that means "this file is what the evidence says" (`transcript`,
    `transcription`, `extracted-text`), or the file is itself a plain-text file
    the search reads anyway (a `.md` or `.txt` attached with no role at all).

    Everything else - a scan, a photograph, a PDF, a recording - holds its words
    in a form no text search can read. That is true of a PDF with a perfectly
    good text layer, too, until `fha source extract` dumps it into a companion:
    the search opens `.md` and `.txt` files and nothing else.

    The role is what decides, not the extension, because a role-tagged companion
    is a promise about the file's content that outranks any guess from its name.
    """
    if str(role or '').strip().lower() in TEXT_COMPANION_ROLES:
        return True
    suffix = PurePosixPath(str(path or '').replace('\\', '/')).suffix.lower()
    return suffix in SEARCHABLE_TEXT_SUFFIXES


def files_carry_searchable_text(file_entries: Iterable[tuple[str, str]]) -> bool:
    """True when at least one of a source's files holds text a search can read.

    `file_entries` is an iterable of `(role, path)` pairs - the shape both
    callers already have: `fha lint` reads them straight off a record's `files:`
    frontmatter, `fha find` reads them out of the index's `source_files` table.
    Sharing the predicate is what keeps lint's warning and find's coverage note
    counting the same sources; two hand-written copies of this rule would drift
    on the first new role.

    A source with no files at all is not "unreadable" - there is nothing to
    read - so callers test emptiness separately rather than folding it in here.
    """
    return any(file_entry_carries_text(role, path) for role, path in file_entries)


# ── Filename grammar helpers ──────────────────────────────────────────────────

def render_file_entry(item: dict) -> list[str]:
    """Render a parsed files: list item dict as block-style frontmatter lines.

    Shared by `fha process` (bundle/--more/inline-list normalization) and
    `fha source extract` (appending a derived-artifact entry) - one renderer,
    so every tool that writes a files: item emits the identical two-space
    block style and quoting.
    """
    keys = list(item.keys())
    first, *rest = keys
    lines = [f'  - {first}: {yaml_inline(item[first])}']
    lines += [f'    {k}: {yaml_inline(item[k])}' for k in rest]
    return lines


def append_file_entry_to_record(record_text: str, entry_lines: list[str]) -> str:
    """Insert a files: list item into a record's frontmatter (text surgery).

    The text is edited rather than YAML round-tripped so human comments and
    field order survive untouched (the same discipline as `fha places
    geocode`'s surgical edits). The new item is appended after the last line
    of the existing `files:` block; a record with no `files:` block gets one
    created just before the closing frontmatter `---`; an inline-list value
    (`files: [...]`) is normalized to block form with its existing items
    preserved. `entry_lines` are already indented (two spaces for the
    `- file:` line). Moved here from process.py so `fha source extract` can
    append its derived-artifact entry through the same single pathway.
    """
    lines = record_text.split('\n')

    # CRLF-faithful both ways: a byte-faithful read (read_text_exact) leaves
    # '\r' on every line of a CRLF-authored record, so the fence scan strips
    # it for comparison AND every line this helper INTRODUCES carries the
    # record's own ending - otherwise a CRLF record would come out with a
    # bare-LF island exactly where the edit landed.
    cr = '\r' if '\r\n' in record_text else ''
    entry_lines = [ln.rstrip('\r') + cr for ln in entry_lines]
    fence_idx = [i for i, ln in enumerate(lines) if ln.rstrip('\r') == '---']
    if len(fence_idx) < 2:
        raise ValueError('record has no parseable frontmatter')
    start, end = fence_idx[0], fence_idx[1]

    files_idx = None
    for i in range(start + 1, end):
        if lines[i].rstrip() == 'files:' or lines[i].rstrip().startswith('files:'):
            files_idx = i
            break

    if files_idx is None:
        insert_at = end
        block = ['files:' + cr] + entry_lines
        return '\n'.join(lines[:insert_at] + block + lines[insert_at:])

    if lines[files_idx].rstrip() != 'files:':
        inline_value = lines[files_idx].split(':', 1)[1].strip()
        existing_items = yaml.safe_load(inline_value) if inline_value not in ('', '~', 'null') else None
        lines[files_idx] = 'files:' + cr
        if existing_items:
            preserved_lines = []
            for item in existing_items:
                preserved_lines.extend(ln + cr for ln in render_file_entry(item))
            lines[files_idx + 1:files_idx + 1] = preserved_lines
            end += len(preserved_lines)

    block_end = end
    for i in range(files_idx + 1, end):
        stripped = lines[i]
        if stripped and not stripped[0].isspace():
            block_end = i
            break
    return '\n'.join(lines[:block_end] + entry_lines + lines[block_end:])


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


# The four states a transcript companion's text can be in, named once so every
# consumer says the same word. Only two of them matter to a caller deciding
# whether to trust the words: see transcript_text_is_unchecked below.
TRANSCRIPT_UNREVIEWED = 'unreviewed'
TRANSCRIPT_VERIFIED = 'verified'
TRANSCRIPT_UNMARKED = 'unmarked'
TRANSCRIPT_DAMAGED = 'damaged'


def transcript_review_state(text: str) -> str:
    """Has a human checked this transcript against the picture it was read from?

    A model that reads a scan and types out what it says produces text that is
    searchable and, from the outside, indistinguishable from evidence. A misread
    word does not fail loudly the way a missing transcript does - it returns
    confident hits nobody re-examines. So a transcript states its own status in
    its text, and this function reads it.

    The rule is the transcribe-source skill's contract ("The marker - how a
    consumer tells an unreviewed transcript from a checked one"), and it reuses
    the archive's existing marker pair rather than inventing a third convention:
    the same `<!-- AI-DRAFT ... -->` / `<!-- AI-ACCEPTED ... -->` comments
    `write-biography` writes and `fha confirm draft` flips, read with the same
    regexes strip_unaccepted_drafts uses above.

    Returns one of four states, decided on the companion's FULL text:

      unreviewed  a complete AI-DRAFT marker is present. A machine read the
                  images; no human has checked the text against them.
      verified    a complete AI-ACCEPTED marker and no AI-DRAFT marker. A human
                  compared it to the image.
      unmarked    neither marker word appears. A human typed it, or `fha source
                  extract` dumped it mechanically out of a PDF's own text layer.
                  The archive makes no AI claim about it either way - and this
                  is the common case, which is why it must never be reported as
                  unreviewed: flag every transcript and the flag stops meaning
                  anything.
      damaged     the literal word appears outside any complete marker (an
                  unterminated `<!--`, a stray prose mention). Draft can no
                  longer be told from checked.

    unreviewed outranks verified: one AI-DRAFT marker anywhere makes the whole
    file unreviewed, because the marker sits at the END of the span it covers
    and a file carrying both has an unchecked span in it somewhere.
    """
    body = text or ''
    has_draft_word = 'AI-DRAFT' in body
    has_accepted_word = 'AI-ACCEPTED' in body
    if not has_draft_word and not has_accepted_word:
        return TRANSCRIPT_UNMARKED

    # The same accounting strip_unaccepted_drafts closes with: remove every
    # complete marker, and any marker word still standing was never inside one.
    residue = _AI_ACCEPTED_MARK_RE.sub('', _AI_DRAFT_MARK_RE.sub('', body))
    if 'AI-DRAFT' in residue or 'AI-ACCEPTED' in residue:
        return TRANSCRIPT_DAMAGED
    if _AI_DRAFT_MARK_RE.search(body):
        return TRANSCRIPT_UNREVIEWED
    return TRANSCRIPT_VERIFIED


def transcript_text_is_unchecked(text: str) -> bool:
    """Should a consumer warn that nobody has checked these words yet?

    True for `unreviewed` and for `damaged`. Damaged fails CLOSED - a file whose
    markers cannot be read is treated as unchecked, never as checked - matching
    strip_unaccepted_drafts, which withholds everything rather than guess which
    prose was accepted. Over-warning costs a reader one glance at the original;
    under-warning lets a machine's reading of a picture pass for the picture.

    The fail-closed collapse lives here rather than at each call site so that
    every consumer inherits it by using the function instead of remembering the
    rule.
    """
    return transcript_review_state(text) in (
        TRANSCRIPT_UNREVIEWED, TRANSCRIPT_DAMAGED)


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

    The write is atomic even though generated output is regenerable, because of
    how the ownership guard above fails otherwise. A truncating write that dies
    partway leaves a file whose first line is a fragment of the GENERATED
    marker - so on the NEXT run the guard no longer recognises it, and the
    human is told the tool refuses to overwrite a file it does not own, about a
    file the tool wrote itself. Regenerating is supposed to be the cure for a
    damaged generated file; that failure mode makes it the one thing that
    cannot fix it, and leaves a non-technical reader with no next step.
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
    write_text_exact_atomic(out_path, content)
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
        # Name the REAL path and a command that exists. The templates ship
        # inside whichever tools directory is running - `tools/templates/` in a
        # workshop clone, `.fha/tools/templates/` in an installed archive - so a
        # hardcoded `tools/templates/` sends an archive owner to a path that is
        # not there. And the old advice could not work either: a stamped archive
        # REFUSES `fha install`, and a zip user has no git to restore from.
        # `update-tools` is the command that actually replaces a damaged tool
        # file, so name that.
        here = Path(__file__).parent / 'templates' / template_name
        raise RuntimeError(
            f'the {template_name} template is missing or broken (expected at '
            f'{here}) - {e}. Restore it with `fha update-tools --repo '
            f'PATH-TO-WORKSHOP`, which reinstalls tool files that have gone '
            f'missing, then re-run.'
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


# ── Walking a tree that might not open ────────────────────────────────────────
#
# THE RULE (learned the expensive way, photoindex 2026-08): an enumeration that
# cannot see everything must not let its caller act as though it saw
# everything.
#
# `os.walk` swallows the OSError from a directory it cannot list and simply
# moves on, so a folder whose permissions changed - or an external drive that
# unmounted mid-walk - looks exactly like an empty folder. `Path.rglob` and
# `Path.glob` do the same thing and give you no hook at all to notice. A caller
# that then DELETES the rows for everything it did not see (a cache sweep, a
# drop-and-rebuild index) erases real content while reporting a clean run, and
# a caller that watermarks freshness against what it saw certifies a cache as
# current when half its inputs were never read.
#
# So: walk with `walk_files` and hand it a recorder. A missing row is
# recoverable; a deleted row and a false 'fresh' are not.

def unreadable_dir_recorder(into: list):
    """Build an `os.walk` `onerror` callback that records the folders it failed on.

    Appends the offending directory as a `Path` (de-duplicated, first-seen
    order) to `into`. The caller reads a non-empty list as "this walk is
    incomplete by at least that much" and fails closed.

    The error carries the path in `err.filename`; an OSError raised with no
    filename (nothing in the stdlib does this today, but a patched `listdir`
    in a test might) is ignored rather than recorded as `None`, because a
    recorder that can put junk in the list is a recorder nobody trusts.
    """
    def note(err: OSError) -> None:
        name = getattr(err, 'filename', None)
        if not name:
            return
        path = Path(name)
        if path not in into:
            into.append(path)
    return note


def walk_files(root: Path, suffix: str | None = None, on_error=None):
    """Yield the files under `root`, with the error seam `rglob` does not have.

    The drop-in replacement for `root.rglob('*')` / `root.rglob('*.md')` in
    any code that deletes, sweeps, or certifies freshness. `rglob` has no
    `onerror` equivalent, so the only way to learn that a subdirectory would
    not open is to walk with `os.walk` and pass one - which is what this does.

    `suffix` filters by extension, case-insensitively ('.md'), because that is
    what every record walk in this codebase wants and doing it here keeps the
    call sites short. Pass `on_error` (see `unreadable_dir_recorder`) whenever
    under-seeing would be worse than slow.

    Order is `os.walk`'s, not sorted - callers that need a stable order sort
    the result, exactly as they had to with `rglob`.
    """
    if not root.is_dir():
        return
    want = suffix.lower() if suffix else None
    for dirpath, _dirnames, filenames in os.walk(root, onerror=on_error):
        here = Path(dirpath)
        for name in filenames:
            if want is not None and not name.lower().endswith(want):
                continue
            yield here / name


# `walk_files`/`unreadable_dir_recorder` close the "a folder that would not
# LIST" gap (#36-class). This pair closes the sibling gap one level down: a
# folder that lists fine but holds a file that will not DECODE (#68/#62's
# class, applied to the file the codebase already knew how to catch once and
# forgot to catch thirty more times).
#
# `Path.read_text(encoding='utf-8')` raises `UnicodeDecodeError` on a file
# saved in another codepage - cp1252 is what a Windows editor writes by
# default, and the accented names a genealogy archive is full of (Krakow,
# Muller, nee) are exactly the bytes that differ from UTF-8. `UnicodeDecodeError`
# is a **ValueError**, not an `OSError`, so the near-universal `except OSError`
# guard in this codebase does not catch it, and the read raises straight out
# of whatever command is running - a plain-text file taking down an entire
# `fha index` build with a traceback for an answer, the crash that made this a
# filed issue rather than a hypothetical one. `index.py`'s `dump_text` read
# already caught it by name at one site; this is that same catch, shared, so
# the other sites stop reinventing it one at a time.
#
# This helper serves a caller that wants the file's TEXT. The record readers
# have their own door onto the same contract - `read_record` takes the same
# `on_decode_error` callback and answers `undecodable: True` - because a
# record that would not decode has to be told apart from one that is empty,
# and a bare `None` return cannot say which happened.

def read_text_or_report(path: str | Path, on_decode_error=None) -> str | None:
    """Read a record as UTF-8 text, turning a bad decode into a reportable gap
    instead of an unhandled crash.

    Returns the file's text, or `None` on EITHER failure a caller already had
    to plan for: the file cannot be opened at all (`OSError` - missing,
    permissions, a race with a delete; every existing call site already
    treated this as a silent skip, and that behaviour is unchanged here), or
    the file opens but its bytes are not valid UTF-8 (`UnicodeDecodeError`,
    see the note above this function).

    `on_decode_error`, when given, is called with the offending `path` - and
    ONLY on the decode failure, never on a plain missing/unreadable file,
    which every caller already treats as ordinary. Pass
    `undecodable_file_recorder`'s callback to collect paths for one aggregated
    report instead of a silent skip (the shape `fha lint`'s W128 uses); pass a
    plain `list.append` for a caller that aggregates its own way (the shape
    `index.py`'s note/research-log/capture-log reads use, which catch
    `UnicodeDecodeError` by name from #66 and are not migrated onto this
    function); pass nothing at all for a caller that only needs the text and
    already treats an absent file as ordinary.

    Never re-encodes, rewrites, or otherwise touches the file - it is the
    human's, and it is not damaged, only saved in a different encoding.
    """
    try:
        return Path(path).read_text(encoding='utf-8')
    except OSError:
        return None
    except UnicodeDecodeError:
        if on_decode_error is not None:
            on_decode_error(Path(path))
        return None


def answer_undecodable(path: Path) -> None:
    """The `on_decode_error` for a caller that reports the file ITSELF.

    Passing this to `read_record` opts that one read into the answering
    contract - `undecodable: True` instead of a raised `UnicodeDecodeError` -
    for a caller who then says his own piece about the file he was handed:
    `fha process`'s sidecar and refile refusals name the one file the human
    typed, `fha views draft-queue` names the one profile it skipped. There is
    no list to collect and nothing to de-duplicate, which is the whole
    difference from `undecodable_file_recorder` (a whole-archive walk, one
    aggregated report, many files).

    It exists because the bare `lambda p: None` these sites used to pass reads
    like "ignore the decode error", which is the OPPOSITE of what supplying it
    does: omit the callback and the read raises; supply one and it answers. A
    name is the only place that distinction can live at the call site.
    """
    return None


def archive_relative(path: str | Path, archive_root: str | Path) -> str:
    """A path as the human filed it - `people/003 Hartley/ann_P-….md`, never
    `/Users/…` or `C:\\Users\\…`.

    The one authority for naming a file in a report. Every warning that names a
    file the tools could not read (`fha index`'s undecodable-files note, `fha
    lint`'s W128, `fha stubs`' and `fha normalize-links`' skip reports, `fha
    views`' per-person skips) has to spell that file the same way, and a report
    can end up committed to the archive, mailed to a cousin, or pasted into an
    issue - so it never carries a local absolute path.

    A path somehow OUTSIDE the archive keeps its own spelling, forward-slashed:
    naming it wrongly is worse than naming it long. Purely textual - the
    filesystem is never consulted, so this answers for a path that no longer
    exists just as well as for one that does.
    """
    try:
        return Path(path).relative_to(archive_root).as_posix()
    except ValueError:
        return str(path).replace('\\', '/')


def undecodable_file_recorder(into: list):
    """Build a `read_text_or_report` `on_decode_error` callback that records
    the files it failed to decode as UTF-8.

    Appends the offending path (de-duplicated, first-seen order) to `into` -
    the same contract as `unreadable_dir_recorder`, one level down (a file
    instead of a folder). De-duplication matters here specifically: a single
    lint run reads some files through more than one pass (a notes file through
    the token-ref walk AND the GENERATED-header sweep; any file through
    `--format-check`), and without it the same undecodable file would earn a
    separate finding from each pass that touched it, or need coordination that
    caring about which pass ran first should never require.
    """
    def note(path: Path) -> None:
        if path not in into:
            into.append(path)
    return note


def unreadable_dir_hold_mtimes(dirs) -> list[float]:
    """File times a cache must sit behind so an incomplete walk keeps reading 'stale'.

    A directory nothing could list has no readable file inside it to watermark
    against, so the times taken are the directory's own and its parent's. The
    parent matters: a freshness walk never yields an unreadable directory from
    its own listing either, so the directory's own mtime may be absent from
    the watermark entirely - but the parent's is there, because reaching the
    child means the parent listed fine.

    Accepts an iterable of `Path`s. `photoindex` keeps `(alias, path)` pairs
    for its own reporting and unwraps them before calling here.
    """
    out: list[float] = []
    for path in dirs:
        for candidate in (Path(path), Path(path).parent):
            try:
                out.append(candidate.stat().st_mtime)
            except OSError:
                continue
    return out


# ── Archive freshness ─────────────────────────────────────────────────────────

def _is_generated_companion(path: Path, archive_root: Path) -> bool:
    """A `fha views` output under people/, excluded from the freshness
    watermark: a per-person companion (timeline / sources-index / draft-queue,
    P-id in the name) or the couple-folder `sources-index.md` (`fha views
    sources-index --couple-folders`, also written by `fha views refresh`),
    which carries no P-id at all.

    Three tests, all required, because being wrong here means a human's own
    file stops counting as a record: text search would keep serving its old
    `notes_fts` row for as long as he leaves the rest of the archive alone,
    and nothing would ever tell him to reindex.

      1. It is under people/. The generators write nowhere else in the record
         tree, while `notes/` is a place a human writes freely - and a note he
         happens to name `notes/sources-index.md` IS indexed (`_index_notes`
         reads every .md under notes/), so it has to keep its vote.
      2. Its name is one the generators use, in the place they use it: the
         P-id form anywhere under people/, the bare `sources-index.md` only at
         the root of a couple folder (people/<folder>/, which is any folder
         directly under people/ that is not stubs/ or connections/ - the same
         definition views uses when it writes them).
      3. It actually carries the GENERATED header. The name is a convention;
         the header is the ownership contract, and it is the only test that
         separates a file `fha views` wrote from one a human wrote in the same
         folder with the same name (which `write_generated_file` would then
         refuse to overwrite, so it can sit there indefinitely). Only files
         that already passed the two cheap tests are read.
    """
    try:
        rel_parts = path.relative_to(archive_root).parts
    except ValueError:
        return False
    if not rel_parts or rel_parts[0] != 'people':
        return False

    if path.name == 'sources-index.md':
        if len(rel_parts) != 3 or rel_parts[1].lower() in ('stubs', 'connections'):
            return False
    else:
        parsed = parse_filename(path)
        if not parsed or parsed.get('kind') not in GENERATED_COMPANION_KINDS:
            return False

    return is_generated_file(path)


def _newest_md_mtime(dirs, keep=None) -> float:
    """Max mtime across the `.md` files under `dirs` - or 'now' if any folder shut.

    The one walk behind all three record watermarks below, so that a fix to
    the fail-closed rule lands in every one of them at once (before this, each
    had its own `rglob` and the rule had three places to be forgotten).

    `keep(path)` filters which files vote; a file whose mtime cannot be read
    (a dangling symlink, a file deleted mid-walk) simply does not vote.

    When a subdirectory would not list, the answer is `time.time()` rather
    than the max of what was visible. A watermark is a promise that nothing
    newer exists, and a walk that skipped a subtree cannot make it: the caller
    would stamp its cache 'fresh' over records it never read. 'Now' is the
    honest answer - every cache reads stale until the folder opens again - and
    it is self-clearing, needing no state anywhere.
    """
    unreadable: list[Path] = []
    on_error = unreadable_dir_recorder(unreadable)
    max_mtime = 0.0
    for d in dirs:
        for p in walk_files(Path(d), suffix='.md', on_error=on_error):
            if keep is not None and not keep(p):
                continue
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            if mtime > max_mtime:
                max_mtime = mtime
    if unreadable:
        return max(max_mtime, time.time())
    return max_mtime


def newest_record_mtime(archive_root: Path) -> float:
    """Max mtime (epoch seconds) across sources/people/notes .md files and places/places.yaml.

    Used as the freshness baseline for index.sqlite and photos.sqlite: if the
    cache is older than this, it is stale.  Returns 0.0 on a brand-new archive
    that has no record files yet (trivially up-to-date).

    Generated companion views (timeline, sources-index, draft-queue) are
    excluded (#37): `fha views` writes them FROM the index, so counting them
    made every view write stale the index it had just read - the documented
    per-person close-out (`views timeline`, `views sources-index`, `views
    draft-queue`) failed on its second call and needed a full rebuild between
    every write. Their content is derived; the index only records that they
    exist (person_files), which the next ordinary rebuild picks up. The
    exclusion is deliberately narrow - see `_is_generated_companion`: only a
    real generated file, in the place the generators write it, loses its vote.

    fha.yaml is part of the watermark because it decides how records are read
    (the roots a source's `files:` entries resolve through, and every other
    indexed setting); an edit there stales the index even though no record
    file changed.

    A folder under sources/, people/ or notes/ that will not list makes this
    return 'now' rather than a watermark it cannot stand behind - see
    `_newest_md_mtime`. Every reader (`fha find`, `fha doctor`, `fha views`)
    then treats the index as out of date, which is the truth: records it never
    read may have changed.
    """
    dirs = [archive_root / d for d in ('sources', 'people', 'notes')]
    max_mtime = _newest_md_mtime(
        dirs, keep=lambda p: not _is_generated_companion(p, archive_root))
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

    Fails closed on a folder that will not list (`_newest_md_mtime`): the
    photo catalog reads stale rather than certifying itself over source
    records it never saw.
    """
    sources_dir = archive_root / 'sources'
    if subdir:
        sources_dir = sources_dir / subdir
    return _newest_md_mtime([sources_dir])


def newest_person_record_mtime(archive_root: Path) -> float:
    """Max mtime (epoch seconds) across person *profile* records only.

    Narrower than `newest_record_mtime`: face-tag/name matching only reads
    `face_tags`/`name_variants` from profile records, so generated companion
    files (research/timeline/sources-index/draft-queue) and folder-level
    `sources-index.md` files under people/ must not bust this freshness
    check just because `fha views refresh` touched them.
    Returns 0.0 on a brand-new archive that has no person records yet.

    Fails closed on a folder that will not list (`_newest_md_mtime`): an
    unreadable `people/` subtree used to lower this watermark silently, so the
    photo catalog read `fresh` while face-tag and name-variant edits it never
    saw sat in the records - and `photo_people` kept serving the old matches.
    """
    def _is_profile(p: Path) -> bool:
        parsed = parse_filename(p)
        return (parsed is not None
                and parsed['id_type'] == 'P' and parsed['kind'] == 'profile')

    return _newest_md_mtime([archive_root / 'people'], keep=_is_profile)


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


def find_person_record_path(
    archive_root: str | Path, person_id: str, unreadable: list | None = None,
) -> Path | None:
    """Scan `people/` for one P-id's primary person record (not a companion view).

    The `.md` files are archive truth, so this never consults
    `.cache/index.sqlite` - a stale or absent index must never block or mislead
    a write aimed at a person record (the `fha claim` locate-by-scanning rule).
    Matches stubs, curated profiles, and merged tombstones alike: identity is
    the `_{P-id}.md` filename suffix (`parse_filename`), so the folder, slug,
    and any `MERGED-INTO-…` prefix are irrelevant. Companion files (research/
    timeline/sources-index/draft-queue) share the P-id but are generated views,
    never the record itself, so they are excluded.

    "Companion" is read the way the rest of the tools read it: SPEC §13's kind
    slot is shared with the last given name, so a name ending in `_timeline` is
    only a hint (`parse_filename`'s `kind_ambiguous`). When the id matches and
    the name says companion, the file's own frontmatter is consulted - a file
    carrying the SPEC §9 person fields IS the record (`person_file_kind`).
    Without that, Marie Timeline Hartley's record answered "no record found" to
    every verb that locates by scanning, while her file sat in plain sight. A
    plainly-named profile always wins; the content fallback only fills a lookup
    that would otherwise come back empty, and never redirects one that already
    worked.

    Shared here because several tools need the same lookup (`fha confirm draft`,
    `fha person set-living`, `fha confirm merge`) - the same
    shared-infrastructure rationale as `mint_ids`.

    Pass a list as `unreadable` to learn whether the scan saw all of `people/`
    (the mirror of `find_source_record_path`'s parameter). A None answer from
    an incomplete scan means "not found here, and I could not look everywhere";
    today's callers all refuse on None, which is already fail-closed, so the
    parameter exists for any future caller that would DELETE on it.
    """
    target = normalize_id(person_id)
    people_dir = Path(archive_root) / 'people'
    if not people_dir.is_dir():
        return None
    named_like_a_companion: Path | None = None
    on_error = unreadable_dir_recorder(unreadable) if unreadable is not None else None
    for path in sorted(walk_files(people_dir, suffix='.md', on_error=on_error)):
        parsed = parse_filename(path)
        if not parsed or parsed.get('id_str') != target:
            continue
        if parsed.get('id_type') != 'P':
            continue
        if not parsed.get('is_companion'):
            return path
        if named_like_a_companion is None and parsed.get('kind_ambiguous'):
            try:
                meta = read_record(path)['meta']
            except Exception:
                continue
            if carries_person_record_fields(meta):
                named_like_a_companion = path
    return named_like_a_companion


def find_source_record_path(
    archive_root: str | Path, source_id: str, unreadable: list | None = None,
) -> Path | None:
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

    Pass a list as `unreadable` to learn whether the scan could see all of
    `sources/`. `rglob` swallowed an unlistable subdirectory silently, so a
    record sitting behind a folder whose permissions changed came back as
    None - indistinguishable from "there is no such source". Callers that
    merely refuse on None are already failing closed and can ignore the
    parameter; a caller that DELETES on None (photoindex's `source-people`
    tier, whose rows `_rebuild_photo_people` rewrites) must pass it and hold
    its rows instead.
    """
    target = normalize_id(source_id)
    sources_dir = Path(archive_root) / 'sources'
    if not sources_dir.is_dir():
        return None
    on_error = unreadable_dir_recorder(unreadable) if unreadable is not None else None
    for path in sorted(walk_files(sources_dir, suffix='.md', on_error=on_error)):
        parsed = parse_filename(path)
        if not parsed or parsed.get('id_str') != target:
            continue
        if parsed.get('id_type') == 'S':
            return path
    return None


# Generational suffixes SPEC §13's filename split must never mistake for a
# surname (issue #53: "Roy Eugene Dodson Jr" filed as `jr__roy_eugene_dodson`,
# sorting the son under a different letter than his father). Matched
# case-insensitively with a trailing period tolerated ("Jr." and "Jr" are the
# same token) - see `strip_generational_suffix`.
PERSON_NAME_SUFFIXES: frozenset[str] = frozenset({'jr', 'sr', 'ii', 'iii', 'iv', 'v'})


def strip_generational_suffix(parts: list[str]) -> tuple[list[str], str | None]:
    """Pull a trailing generational suffix off whitespace-split name tokens.

    Shared by every site that derives a person's SPEC §13 filename surname
    from a plain name string (`_lib.stub_slug_name` behind both `fha person
    new` and `fha stubs --from-names`, `lint._person_filename_parts` behind
    `--fix-ids`' rename, and `index.py`'s name-based surname fallback) so the
    suffix list and its match rule live in exactly one place. Before this fix
    each site re-implemented "the last word is the surname" independently and
    only some of them would have grown suffix awareness at the same time -
    the same drift the marriage-edge work (#58/PR #61) had to undo for
    `spouse_parties`.

    Returns `(core_parts, suffix)`: `core_parts` is `parts` with the trailing
    suffix token removed and `suffix` is its lowercased, period-stripped form
    ('jr', 'sr', 'ii', 'iii', 'iv', 'v') - or `(parts, None)` unchanged when
    the last token is not a recognised suffix.

    Deliberately does nothing when `parts` has fewer than two tokens: a bare
    "Jr" or "IV" has no token left to carry the name once the suffix is
    pulled off, so it is left alone and reads as an ordinary (if odd) given
    name/mononym rather than being reduced to nothing. The same rule makes
    "IV" used as an actual given name indistinguishable from the
    generational suffix once a second token precedes it ("John IV" reads as
    given "John" + suffix "IV") - a deliberate, documented tradeoff; the
    `--surname` override on `fha person new` (`stub_slug_name`'s `surname`
    argument) is the escape hatch for any name this heuristic gets wrong.
    """
    if len(parts) < 2:
        return parts, None
    candidate = parts[-1].rstrip('.').lower()
    if candidate in PERSON_NAME_SUFFIXES:
        return parts[:-1], candidate
    return parts, None


def stub_slug_name(name: str, surname: str | None = None) -> tuple[str, str]:
    """Parse a display name into (surname_slug, given_slug) for a stub filename.

    Best effort, not a real name-parsing engine: the last word is taken as the
    surname and everything before it as given names, because that is right
    often enough for the filename to be recognisable, and a stub filename is
    provisional anyway (renamed by hand once a human files the person
    properly). Sanitised to `[a-z0-9_]` so the slug is always a safe filename
    component regardless of what punctuation the display name carries.

    A trailing generational suffix (Jr, Sr, II, III, IV, V - see
    `strip_generational_suffix`) is never taken as the surname: it is pulled
    off first, and rides at the END of the given-name slug instead, so
    `dodson__roy_eugene_jr_P-….md` sorts directly under
    `dodson__roy_eugene_P-….md` (issue #53). Splitting then continues on
    whatever tokens remain: two or more -> the last is the surname as usual;
    exactly one -> the SAME surname-less treatment the true mononym case
    below gets ("Roy Jr" has no more of a real surname than "Roy" alone
    does - the suffix does not promote "Roy" into one).

    A SINGLE-token name (with no suffix to strip) is a surname-less person -
    a mononym (`Cher`), an enslaved ancestor recorded only by a given name, a
    patronymic. SPEC §13 files those with the sort-name slot EMPTY, so the
    filename leads with the double underscore (`__cher_P-….md`), a distinct
    no-surname sort group - hence the empty surname slug here, not the
    literal 'unknown'. 'unknown' stays reserved for the genuinely nameless
    fallback below (a blank or whitespace-only display name), which is a
    missing name, not a mononym. The suffix fix above leaves that
    surname-less contract exactly as it was.

    That path used to return the token unslugged, which is the one place
    the `[a-z0-9_]` promise at the top of this docstring was not kept: a
    mononym carrying punctuation came back verbatim, so `Bob/Rob` filed as
    `__bob/rob_P-….md` - a path separator inside a filename, pointing the
    write at a folder that is not there - and `?` or a colon produced a
    name Windows refuses outright. Every other return here sanitises; this
    one now does too, and a token with nothing left after sanitising falls
    back to 'unknown' in the given slot (`__unknown_P-….md`) while staying
    surname-less, because a mononym nobody can spell is still a mononym.

    `surname`, when given, overrides the automatic split outright - the
    escape hatch for names no heuristic should be expected to get right:
    Spanish double surnames ("García López"), particles ("van der Berg"),
    surname-first conventions. It becomes the surname slug as typed; the
    given-name slug is `name` with that surname text removed from wherever
    it sits - the end (the common case) or the start (a surname-first name) -
    matched whole-word and case-insensitively AFTER a trailing generational
    suffix is set aside (so `--surname Dodson` on "Roy Eugene Dodson Jr"
    still finds "Dodson" at the end of what is left and the suffix still
    rides in the given slug, exactly as the automatic path would place it).
    When the surname text is neither a prefix nor a suffix of `name` (an
    unrelated override), the full name is kept as the given slug rather than
    silently dropping part of it - a redundant sort handle is honest; a
    guessed-wrong deletion is not.
    """
    parts = name.strip().split()
    if not parts:
        return ('unknown', 'unknown')
    if surname is not None and surname.strip():
        surname_tokens = surname.strip().split()
        n = len(surname_tokens)
        core, suffix = strip_generational_suffix(parts)
        lowered_core = [p.lower() for p in core]
        surname_lowered = [t.lower() for t in surname_tokens]
        if lowered_core[-n:] == surname_lowered:
            given_tokens = core[:-n] if n else core
        elif lowered_core[:n] == surname_lowered:
            given_tokens = core[n:]
        else:
            # Matches neither end of the suffix-stripped core - the override
            # is unrelated to `name`, or the caller's suffix and surname text
            # overlap in a way this heuristic cannot untangle. Keep the FULL
            # original name rather than guess: dropping the wrong words would
            # be worse than a redundant given slug.
            given_tokens = parts
            suffix = None
        if suffix:
            given_tokens = given_tokens + [suffix]
        surname_slug = re.sub(r'[^a-z0-9_]', '', '_'.join(t.lower() for t in surname_tokens))
        given_slug = re.sub(r'[^a-z0-9_]', '', '_'.join(p.lower() for p in given_tokens))
        return (surname_slug or 'unknown', given_slug or 'unknown')
    if len(parts) == 1:
        given = re.sub(r'[^a-z0-9_]', '', parts[0].lower())
        return ('', given or 'unknown')
    core, suffix = strip_generational_suffix(parts)
    if len(core) == 1:
        given = core[0] if not suffix else f'{core[0]}_{suffix}'
        given = re.sub(r'[^a-z0-9_]', '', given.lower())
        return ('', given or 'unknown')
    surname_tok = core[-1].lower().replace(' ', '_')
    given_parts = core[:-1] + ([suffix] if suffix else [])
    given = '_'.join(p.lower() for p in given_parts)
    surname_tok = re.sub(r'[^a-z0-9_]', '', surname_tok)
    given = re.sub(r'[^a-z0-9_]', '', given)
    return (surname_tok or 'unknown', given or 'unknown')


def name_match_key(text: str) -> str:
    """A name reduced to the form a SPEC §13 filename slug would have reduced it to.

    Two strings get the same key exactly when the filename grammar could not
    have told them apart: `"Anne Müller"`, `"anne muller"` and the slug
    `muller__anne` all key as `"anne mller"`. Word order does not matter (the
    tokens are sorted), because `{surname}__{given}` reverses the spoken
    order and both spellings name the same person.

    The reduction is `stub_slug_name`'s, applied token by token: split on
    whitespace and underscores (the slug's own word separator), then drop
    everything outside `[a-z0-9]` from each token, exactly as that function's
    final `re.sub` does. Hyphens are dropped rather than split on, so
    `"Mary-Jane Hartley"` keys the same as its `hartley__maryjane` slug.
    An empty key ('' - no letters or digits anywhere) matches nothing on
    purpose: it says the name is unusable, which is not the same as saying
    two unusable names are the same name.

    This exists for one question, and it is a question about a file NOBODY
    COULD READ (#68): "could this alias be the name of that record?" A record
    whose bytes will not decode still has a filename, and SPEC §13 derives
    that filename from the name - so the key is the most the archive can
    honestly say about who is in there. It is used only to WITHHOLD a
    resolution, never to make one: the reduction is lossy (Müller and Muller
    key alike, and so would two genuinely different names that slug the same),
    and a lossy key may say "I cannot be sure these are different people" but
    must never be allowed to say "these are the same person".
    """
    tokens = [re.sub(r'[^a-z0-9]', '', part.lower())
              for part in re.split(r'[\s_]+', str(text or ''))]
    return ' '.join(sorted(tok for tok in tokens if tok))


def record_filename_name_key(path: str | Path) -> str:
    """`name_match_key` of what a record's FILENAME says its name is.

    The trailing `_{ID}` is dropped first (it is identity, not name), leaving
    the `{surname}__{given}` of a person record or the `{slug}` of a source
    record - both of which are name-derived by SPEC §13. A filename with no
    trailing ID keys on the whole stem, which is the best available reading of
    a hand-authored file that never got one.

    A companion (`…_research_P-…`) keys on its kind word too, so it matches no
    person's name - correct, and harmless: this key only ever withholds, and a
    key that matches nothing withholds nothing.
    """
    stem = Path(path).stem
    stem = re.sub(r'_[PSCLH]-[0-9a-hjkmnp-tv-z]{10}$', '', stem, flags=re.I)
    return name_match_key(stem)


def stub_filename(name: str | None, pid: str, surname: str | None = None) -> str:
    """Return the `{surname}__{given}_{P-id}.md` stub filename (SPEC §13).

    A blank or literal "unknown" name falls back to the surname-less
    `unknown__unknown_{P-id}` form rather than calling `stub_slug_name` on a
    name with nothing to slug - the double underscore is the same convention
    §13 uses for surname-less people (mononyms, enslaved ancestors named only
    by a given name), so an unresolved reference reads the same way on disk.

    `surname` is passed straight through to `stub_slug_name` (see its
    docstring) - the explicit override for names the automatic split
    cannot be expected to get right.
    """
    if name and name.lower() not in ('unknown', ''):
        surname_slug, given = stub_slug_name(name, surname=surname)
    else:
        surname_slug, given = 'unknown', 'unknown'
    return f'{surname_slug}__{given}_{pid}.md'


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


# ── Ahnentafel derivation + stub promotion (the shared promote engine) ────────
#
# The Ahnentafel walk (SPEC §12.2) and the "graduate a stub to curated"
# operation are each needed by more than one tool: `fha views brackets` derives
# positions for W110/W119 and applies `--fix-promote`; `fha person promote` is
# the single-person verb; `fha report` lists promotion candidates (§7b).
# Tools never import tools, so the one shared derivation and the one shared
# mutation engine live here (the `mint_ids`/`find_person_record_path`
# rationale). Promotion is ALWAYS an explicit human act - a verb the human
# runs, or a previewed fix he confirms - never a side effect of accepting a
# claim (SPEC §4: hand-labor scales with curiosity; TOOLING §5's
# placement-is-a-human-act rule carves out exactly this engine).

def build_ahnentafel_map(
    conn: sqlite3.Connection, root_pid: str,
    sex_gaps: list[dict] | None = None,
) -> dict[str, int]:
    """BFS from root_pid to build {person_id -> Ahnentafel position} from the index.

    Seed: root_pid -> 1.  Parents of person at position N:
      sex='M' -> 2N (father's slot), sex='F' -> 2N+1 (mother's slot).
      Same-sex or sex='U' pairs: lexicographically-first P-id -> 2N (deterministic).
    Terminates when no accepted parent edges remain (the relationships table is
    derived from accepted claims only - see index.py).

    `sex_gaps`, when a list is passed, collects the W120 set: every placement
    made by the single-resolved-parent branch where that parent's `sex:` is not
    a recorded M/F, appended as {'pid', 'pos'}. Such a person took the father
    (even) slot by DEFAULT, not by derivation - a completely normal early-
    research state (SPEC never requires `sex:` up front), but the resulting
    folder number looks confident while actually being a guess, and W110 can
    never catch it because the folders match their own flawed derivation. Two
    RESOLVED parents with unset or matching sex are deliberately NOT collected:
    that is the genuine same-sex/unknown-pair case the deterministic tie-break
    below exists for (TOOLING §7).

    WHY BFS: Ahnentafel is a breadth-first numbering by definition.  Depth-first
    would produce the same positions but BFS is the natural traversal shape.
    Moved here verbatim from views.py so person.py and report.py can derive the
    same positions without importing views (lint keeps its own registry-backed
    twin, `_build_ahnentafel_lint`, because lint never reads the index).
    """
    # Numbering follows only the GENETIC pedigree (SPEC §12.2): a parent edge is
    # skipped when its claim's nature is an explicit social/legal kind. The nature
    # lives on the backing claim, so we join relationships → claims by claim_id;
    # an unset/unknown/legacy nature defaults to genetic (NOT IN the social set),
    # so a legacy archive numbers exactly as before. DISTINCT collapses the
    # co-valid case (a biological AND an adoptive edge to the same parent) to the
    # one surviving genetic edge.
    social = sorted(SOCIAL_PARENT_SUBTYPES)
    social_ph = ','.join('?' * len(social))
    pid_to_pos: dict[str, int] = {root_pid: 1}
    queue: deque[tuple[str, int]] = deque([(root_pid, 1)])

    while queue:
        pid, n = queue.popleft()
        parent_rows = conn.execute(
            f"""
            SELECT DISTINCT r.other_id AS pid, p.sex
            FROM relationships r
            JOIN persons p ON r.other_id = p.id
            LEFT JOIN claims c ON r.claim_id = c.id
            WHERE r.person_id = ? AND r.rel = 'parent'
              AND COALESCE(LOWER(c.subtype), '') NOT IN ({social_ph})
            ORDER BY r.other_id
            """,
            (pid, *social),
        ).fetchall()

        parents = [(r['pid'], r['sex'] or 'U') for r in parent_rows]
        if not parents:
            continue

        if len(parents) == 1:
            p_pid, p_sex = parents[0]
            pos = 2 * n if p_sex != 'F' else 2 * n + 1
            if p_pid not in pid_to_pos:
                pid_to_pos[p_pid] = pos
                queue.append((p_pid, pos))
                if sex_gaps is not None and sex_slot_is_defaulted(p_sex):
                    sex_gaps.append({'pid': p_pid, 'pos': pos, 'sex': p_sex})
        else:
            # Two or more genetic parent edges - assisted reproduction (a
            # donor-egg mother plus a surrogate-genetic mother plus a
            # donor-sperm father), or a co-valid biological/surrogate-genetic
            # pair. The two-slot Ahnentafel model (SPEC 12.2, TOOLING 7) numbers
            # exactly one contributor per slot: the father slot is 2n, the
            # mother slot 2n+1. Taking the first two SQL rows would let two
            # female contributors land in both slots and drop the sperm
            # contributor entirely, and the choice would swing with row order.
            #
            # TOOLING 7 fixes the rule: "The parent with sex: M takes position
            # 2n, sex: F takes 2n+1; for same-sex or sex: U pairs the
            # first-encountered parent (by P-id, deterministic) takes the lower
            # slot. Where two genetic contributors share a role ... the genetic
            # contributor anchors the number and the other is shown beside it."
            # So we rank every contributor for each slot by (sex-fitness, P-id):
            # the father slot prefers M, then U, then F; the mother slot prefers
            # F, then U, then M; P-id breaks every tie so runs always agree.
            # The father-slot winner is chosen first and removed, then the
            # mother-slot winner from the rest. Extra contributors beyond the two
            # slots are intentionally left unnumbered here - `fha views brackets`
            # lists them beside the couple. This reproduces the old two-parent
            # behaviour exactly for every sex combination while staying
            # deterministic for three or more contributors.
            def _father_rank(edge: tuple[str, str]) -> tuple[int, str]:
                pid_, sex_ = edge
                return ({'M': 0, 'U': 1, 'F': 2}.get(sex_, 1), pid_)

            def _mother_rank(edge: tuple[str, str]) -> tuple[int, str]:
                pid_, sex_ = edge
                return ({'F': 0, 'U': 1, 'M': 2}.get(sex_, 1), pid_)

            father = min(parents, key=_father_rank)[0]
            remaining = [e for e in parents if e[0] != father]
            mother = min(remaining, key=_mother_rank)[0]
            for pp, pos in [(father, 2 * n), (mother, 2 * n + 1)]:
                if pp not in pid_to_pos:
                    pid_to_pos[pp] = pos
                    queue.append((pp, pos))

    return pid_to_pos


def ahnentafel_generation(pos: int) -> int:
    """Generation depth of an Ahnentafel position: root (1) -> 0, parents
    (2-3) -> 1, grandparents (4-7) -> 2, and so on.  Positions in generation g
    span 2**g .. 2**(g+1)-1, so the depth is the bit length minus one - the
    arithmetic behind every `--generations N` cap."""
    return max(int(pos).bit_length() - 1, 0)


def couple_folder_prefix(pos: int) -> int:
    """The couple-folder number for a direct-line position: the even (male-slot)
    Ahnentafel number - a person at odd position N files in folder N-1
    (SPEC §12.2: the wife is implicitly 2n+1)."""
    return pos if pos % 2 == 0 else pos - 1


def couple_folder_dirs(archive_root: str | Path) -> list[Path]:
    """Digit-prefixed directories directly under people/, excluding stubs/
    and connections/ - the couple-folder candidates every Ahnentafel check
    walks. Shared by views (W103/W110/W119) and the promote engine so the two
    can never disagree about what counts as a couple folder."""
    people = Path(archive_root) / 'people'
    if not people.exists():
        return []
    excluded = {'stubs', 'connections'}
    return sorted(
        e for e in people.iterdir()
        if e.is_dir()
        and e.name.lower() not in excluded
        and re.match(r'^\d', e.name)
    )


class AmbiguousCoupleFolderError(Exception):
    """Two or more canonical couple folders share one numeric prefix.

    A hand-organization mistake - e.g. both '002 Father' and '002 Mother'
    carry prefix 2 when SPEC §12.2 wants ONE folder per ancestral couple. With
    the prefix pointing at two folders, filing a direct-line person's record
    would have to guess which one, and a wrong guess splits or mixes the
    couple. The engine cannot pick safely, so it raises this instead of
    silently taking the lexicographically first folder; callers name the
    conflicting folders and tell the human to rename one so prefixes are
    unique. Carries `prefix` (the int) and `folders` (the on-disk names)."""

    def __init__(self, prefix: int, folders: list[str]):
        self.prefix = int(prefix)
        self.folders = list(folders)
        super().__init__(
            f'couple-folder prefix {self.prefix} matches more than one folder: '
            + ', '.join(self.folders))


def couple_folder_for_prefix(archive_root: str | Path, prefix: int) -> Path | None:
    """The canonical on-disk couple folder for a numeric prefix, or None.

    Canonical means digits then a literal space ('040 Thomas …'); suffix
    folders ('040b Thomas …') share the number but are a direct ancestor's
    NON-ancestral marriages (SPEC §12.2), never the destination for a
    direct-line person's files. The digit string is compared as an int so
    zero-padding differences ('40 ' vs '040 ') cannot split a couple.

    Returns the one match, or None when none exist. When TWO OR MORE canonical
    folders share the prefix (a hand-organization mistake like '002 Father'
    beside '002 Mother'), returning either one would silently file a record
    into an arbitrary half of the couple, so this raises
    `AmbiguousCoupleFolderError` naming the conflict and lets the caller refuse
    plainly instead of guessing."""
    matches = []
    for folder in couple_folder_dirs(archive_root):
        m = re.match(r'^(\d+) ', folder.name)
        if m and int(m.group(1)) == int(prefix):
            matches.append(folder)
    if not matches:
        return None
    if len(matches) > 1:
        raise AmbiguousCoupleFolderError(prefix, [f.name for f in matches])
    return matches[0]


# The built-in research scaffold, used when no _TEMPLATE.research.md is found
# (an archive installed before the template shipped). Kept in step with
# archive-template/people/_TEMPLATE.research.md by tests/test_person.py; the
# section set is SPEC §16's research-file body verbatim.
RESEARCH_TEMPLATE_FALLBACK = '''---
id: P-__________
created: 2026-01-01
---

## Research Notes

*(working notes - what you are chasing and why)*

## Open Questions

*(what you do not know yet)*

## Hypotheses

*(testable beliefs, not yet facts - a guess is never a claim)*

## Research Log

*(searches you have run, including empty ones, so nothing is fruitlessly re-searched)*
'''


def research_template_text(archive_root: str | Path) -> str:
    """The research-companion template text (SPEC §16), from the nearest source.

    Looks for `people/_TEMPLATE.research.md` inside the archive first (where
    `fha install`/`fha update-tools` place it, and where a human may have
    customized it), then the public repo's `archive-template/people/` (the
    development / fixtures-in-repo case, resolved relative to this file), and
    finally falls back to the built-in scaffold so promotion never fails just
    because an older install predates the template."""
    candidates = [
        Path(archive_root) / 'people' / '_TEMPLATE.research.md',
        Path(__file__).resolve().parent.parent / 'archive-template' / 'people' / '_TEMPLATE.research.md',
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.read_text(encoding='utf-8')
        except OSError:
            continue
    return RESEARCH_TEMPLATE_FALLBACK


def render_research_content(pid: str, archive_root: str | Path) -> str:
    """Fill the research template for one person: real P-id, today's date,
    hand-instruction comments stripped.

    The template doubles as a hand-copy seed (its `#` comment lines teach a
    human what the file is for) and the machine scaffold `fha person promote`
    writes; the machine copy substitutes the `P-__________` placeholder and
    the `created:` date and drops the frontmatter's full-line comments, so a
    scaffolded file starts clean while the template stays instructive.
    Comment-stripping is bounded to the frontmatter on purpose: in the body a
    leading `#` is a markdown heading, not a comment."""
    text = research_template_text(archive_root)
    text = re.sub(r'^id:.*$', f'id: {fmt_id_display(pid)}', text, count=1, flags=re.M)
    text = re.sub(r'^created:.*$', f'created: {datetime.date.today().isoformat()}',
                  text, count=1, flags=re.M)
    lines = text.split('\n')
    span = frontmatter_fence_span(lines)
    if span is not None:
        start, end = span
        kept = (lines[:start + 1]
                + [ln for ln in lines[start + 1:end] if not ln.lstrip().startswith('#')]
                + lines[end:])
        lines = kept
    return '\n'.join(lines)


# Person record filename: `{slug}_{P-id}.md` (SPEC §13). Captures the slug and
# the id AS WRITTEN so a companion filename can be derived case-faithfully.
_PERSON_RECORD_FILENAME_RE = re.compile(
    r'^(?P<slug>.+)_(?P<pid>P-[0-9a-hjkmnp-tv-z]{10})\.md$', re.I)


def research_companion_filename(record_filename: str) -> str | None:
    """`{slug}_{P-id}.md` -> `{slug}_research_{P-id}.md` (SPEC §13's `kind`
    slot), or None when the record filename does not carry the id suffix (a
    pre-machine hand-named record - the caller refuses plainly)."""
    m = _PERSON_RECORD_FILENAME_RE.match(record_filename)
    if not m:
        return None
    return f"{m.group('slug')}_research_{m.group('pid')}.md"


class PromotionError(Exception):
    """A stub promotion could not be (or was not) applied.

    Carries a plain-language message naming the cause and the fix. When the
    engine raises this after starting to write, every completed step has
    already been rolled back - the record is byte-for-byte where it started.
    """


def promote_person_record(
    archive_root: Path,
    pid: str,
    record_path: Path,
    dest_folder: Path,
    *,
    dry_run: bool = False,
) -> dict:
    """Promote one stub person record to curated - the ONE mutation engine
    behind `fha person promote` and `fha views brackets --fix-promote`.

    Three writes, applied together or not at all:
      1. flip the record's `tier:` to curated (surgical single-line edit,
         vetted by `frontmatter_edit_problem` before anything is written);
      2. move the record file into `dest_folder` when it currently sits in a
         reserved folder (people/stubs/ or people/connections/) or loose under
         people/ - the one sanctioned tool move out of stubs/ (TOOLING §5's
         carve-out); a record already inside `dest_folder` is left in place;
      3. settle the `_research` companion (SPEC §16): if a hand-written one
         already sits beside the SOURCE record and the record is moving, MOVE
         it to the destination so its notes travel with the record; if one
         already sits at the destination, leave it; otherwise scaffold a fresh
         blank one from the `_TEMPLATE.research.md` grammar.

    Steps already satisfied are skipped, so the engine is idempotent and also
    FINISHES a half-promotion (a record hand-flipped to curated but still
    parked in stubs/, the dead end the views stub guard refuses).

    WHY ROLLBACK-BY-HAND rather than a temp-dir dance: the writes touch a
    handful of paths, all inside people/, and each has an exact inverse
    (rewrite old bytes / move the record back / move the companion back or
    delete the fresh scaffold / remove the folder this run created). On any
    OSError the inverses run in reverse order and PromotionError is raised -
    the archive is never left mid-move.

    The caller owns the DECISION layer: deriving/validating `dest_folder`
    (Ahnentafel, --into), the merged-tombstone and already-curated refusals,
    and every human-facing Result/preview. This function owns the WRITE.
    Returns {'status', 'tier_flip', 'move', 'old_path', 'new_path',
    'research_path', 'research_source_path', 'research_create', 'research_move',
    'folder_create', 'steps'} - `steps` is the plain-words list a preview
    prints verbatim. `dry_run` computes the identical plan and writes nothing.
    """
    pid = normalize_id(pid)

    try:
        text = read_text_exact(record_path)
    except OSError as e:
        raise PromotionError(
            f'cannot read {record_path}: {e}. Check the file is not open in '
            'another program and try again.') from e

    fm = FRONT_RE.match(text)
    meta = None
    if fm is not None:
        try:
            meta = yaml.safe_load(fm.group(1))
        except yaml.YAMLError:
            meta = None
    if not isinstance(meta, dict):
        raise PromotionError(
            f'the header of {record_path.name} does not read as YAML, so it '
            f'cannot be promoted safely. Open {record_path}, fix the header by '
            'hand (run `fha lint` to see the problem line), then retry. '
            'Nothing was written.')
    if is_merged_meta(meta):
        raise PromotionError(
            f'{record_path.name} is a merged tombstone - readers resolve '
            'through its merged_into: pointer, so it is never promoted. '
            'Nothing was written.')

    name = str(meta.get('name') or '').strip() or fmt_id_display(pid)
    tier = str(meta.get('tier') or 'stub').strip().lower()
    needs_flip = tier != 'curated'

    # ── Plan step 1: the tier flip (build + vet the rewrite, write nothing) ──
    new_text = text
    if needs_flip:
        lines = text.split('\n')
        bounds = frontmatter_fence_span(lines)
        if bounds is None:
            raise PromotionError(
                f'could not locate the frontmatter fences in {record_path.name} '
                f'to edit safely. Open {record_path} and set tier: curated by '
                'hand, then run `fha lint`. Nothing was written.')
        start, end = bounds
        pattern = re.compile(r'tier:(?=\s|$)')
        tier_lines = [i for i in range(start + 1, end) if pattern.match(lines[i])]
        new_lines = list(lines)
        if len(tier_lines) > 1:
            raise PromotionError(
                f'{record_path.name} has more than one top-level tier: line in '
                'its header, so the right one to edit cannot be chosen safely. '
                f'Fix the duplicate by hand, then retry. Nothing was written.')
        if tier_lines:
            # Swap only the value; keep any trailing `# comment` and a CRLF.
            m = re.match(r'^(tier:)([ \t]*)([^#]*?)([ \t]*)(#.*?)?(\r?)$',
                         lines[tier_lines[0]])
            comment, cr = (m.group(5), m.group(6)) if m else (None, '')
            if comment:
                sep = m.group(4) or '  '
                new_lines[tier_lines[0]] = f'tier: curated{sep}{comment}{cr}'
            else:
                new_lines[tier_lines[0]] = f'tier: curated{cr}'
        else:
            # Key absent (legal for a hand-made record): append in the stub
            # scaffold's field order - tier is the last field before the fence.
            cr = '\r' if lines[start].endswith('\r') else ''
            new_lines.insert(end, f'tier: curated{cr}')
        new_text = '\n'.join(new_lines)
        problem = frontmatter_edit_problem(
            new_text, before_meta=meta, changed_keys={'tier'})
        if problem is None:
            after_meta = yaml.safe_load(FRONT_RE.match(new_text).group(1))
            if str(after_meta.get('tier') or '').strip().lower() != 'curated':
                problem = (f'the tier would read {after_meta.get("tier")!r} '
                           'instead of curated')
        if problem is not None:
            raise PromotionError(
                f'refusing to promote {name}: {problem}, so saving could '
                f'corrupt the record. Open {record_path} and set tier: curated '
                'by hand, then run `fha lint`. Nothing was written.')

    # ── Plan step 2: the move ────────────────────────────────────────────────
    needs_move = record_path.parent.resolve() != dest_folder.resolve()
    new_record_path = (dest_folder / record_path.name) if needs_move else record_path
    if needs_move and new_record_path.exists():
        raise PromotionError(
            f'a file named {record_path.name} already exists in '
            f'{dest_folder.name}/ - refusing to overwrite it. Compare the two '
            f'files (`fha find {fmt_id_display(pid)}`), resolve the duplicate, '
            'then retry. Nothing was written.')
    folder_create = needs_move and not dest_folder.exists()

    # ── Plan step 3: the research companion ──────────────────────────────────
    research_name = research_companion_filename(record_path.name)
    if research_name is None:
        raise PromotionError(
            f'{record_path.name} does not carry its P-id in the filename, so '
            'the research companion cannot be named. Run `fha lint --fix-ids` '
            'to formalize the filename first. Nothing was written.')
    source_research_path = record_path.parent / research_name
    research_path = (dest_folder / research_name) if needs_move else source_research_path
    # REFUSE the two-file split: a POPULATED companion sits beside the stub AND
    # another companion already sits at the destination (two DIFFERENT files -
    # e.g. after a hand-repaired partial promotion). Neither MOVE nor CREATE
    # below fires in that case, so promotion would keep the destination file and
    # silently STRAND the source companion (with its notes) under people/stubs/ -
    # a split, silent orphaning of the human's notes. There is no safe automatic
    # merge (only the human knows which notes are canonical), so we stop in the
    # PLAN phase - before any write - and hand the reconcile back to the human.
    # This is caught here rather than as the ordinary destination-only SKIP,
    # which is safe precisely because no source companion is being left behind.
    if (needs_move and source_research_path.exists() and research_path.exists()
            and source_research_path.resolve() != research_path.resolve()):
        raise PromotionError(
            f'{name} has TWO research companions and promotion cannot choose '
            f'between them: one beside the stub at {source_research_path} and '
            f'one already at the destination {research_path}. Promoting would '
            'keep the destination file and strand the stub one - with its '
            'notes - under people/stubs/. Merge the notes into one of these two '
            'files, delete the other, then retry. Nothing was written.')
    # Three mutually exclusive fates for the companion:
    #   MOVE   - a hand-written companion already sits beside the SOURCE record
    #            (people/stubs/) and the record is moving. It must travel WITH
    #            the record; otherwise promotion scaffolds a blank one at the
    #            destination and strands the populated notes in stubs/, which
    #            reads as "the notes were lost" and splits the person's files.
    #   SKIP   - a companion already sits at the DESTINATION (an idempotent
    #            re-run, or a promote-in-place): leave it exactly as it is.
    #   CREATE - no companion anywhere: scaffold a fresh blank one.
    research_move = (needs_move and source_research_path.exists()
                     and not research_path.exists())
    research_create = not research_path.exists() and not research_move

    # ── The plain-words plan (previews print these verbatim) ─────────────────
    def _rel(p: Path) -> str:
        return archive_relative(p, archive_root)

    steps: list[str] = []
    if needs_flip:
        steps.append(f'set tier: stub -> curated in {record_path.name}')
    if needs_move:
        suffix = ' (creating the folder)' if folder_create else ''
        steps.append(f'move {_rel(record_path)} -> {_rel(new_record_path)}{suffix}')
    if research_move:
        steps.append(
            f'move the research companion {_rel(source_research_path)} -> '
            f'{_rel(research_path)} (your notes travel with the record)')
    elif research_create:
        steps.append(f'create the research companion {_rel(research_path)}')
    else:
        steps.append(f'research companion already exists ({_rel(research_path)}) - left as is')

    plan = {
        'status': 'dry-run' if dry_run else 'ok',
        'tier_flip': needs_flip,
        'move': needs_move,
        'old_path': record_path,
        'new_path': new_record_path,
        'research_path': research_path,
        'research_source_path': source_research_path,
        'research_create': research_create,
        'research_move': research_move,
        'folder_create': folder_create,
        'steps': steps,
    }
    if dry_run:
        return plan

    # ── Apply, with rollback on any failure ──────────────────────────────────
    # Every write below is atomic (write_text_exact_atomic: temp file + fsync +
    # os.replace), so a write that dies partway leaves the target untouched and
    # nothing partial on disk. That is what lets each 'wrote it' flag be set
    # AFTER the call returns: a raise means the step did not happen, so the
    # rollback correctly skips undoing it - no truncated sole record, no
    # untracked half-written companion file.
    wrote_flip = moved = wrote_research = made_folder = moved_research = False
    try:
        if folder_create:
            dest_folder.mkdir(parents=True)
            made_folder = True
        if needs_flip:
            write_text_exact_atomic(record_path, reapply_newline(new_text, text))
            wrote_flip = True
        if needs_move:
            shutil.move(str(record_path), str(new_record_path))
            moved = True
        if research_move:
            # An existing hand-written companion travels with the record instead
            # of being re-scaffolded (its inverse restores it to the source dir).
            shutil.move(str(source_research_path), str(research_path))
            moved_research = True
        elif research_create:
            write_text_exact_atomic(
                research_path, render_research_content(pid, archive_root))
            wrote_research = True
    except OSError as e:
        # Undo in reverse order; best-effort (a rollback failure is reported
        # inside the raised message rather than swallowed). The restore write is
        # itself atomic so an interrupted rollback cannot truncate the record it
        # is trying to save.
        rollback_notes: list[str] = []
        # Track where the profile physically IS as rollback proceeds. If the flip
        # ran, the profile was moved to new_record_path (when needs_move) - and the
        # move-inverse below only returns it to record_path if it actually succeeds.
        # The flip-undo must target the profile's real location, never blindly
        # record_path: after a FAILED move-back the old path is absent, and the
        # atomic writer creates a missing target, so writing there would leave the
        # curated profile at new_record_path AND a second stub with the same P-id
        # at the old path.
        profile_path = new_record_path if moved else record_path
        for action in ('research', 'research_move', 'move', 'flip', 'folder'):
            try:
                if action == 'research' and wrote_research:
                    research_path.unlink()
                elif action == 'research_move' and moved_research:
                    shutil.move(str(research_path), str(source_research_path))
                elif action == 'move' and moved:
                    shutil.move(str(new_record_path), str(record_path))
                    profile_path = record_path      # move-back succeeded
                elif action == 'flip' and wrote_flip:
                    # Undo the tier flip at wherever the profile actually is. If the
                    # move-back above failed, profile_path is still new_record_path
                    # (holding the flipped text) - rewrite the old text THERE, so
                    # rollback never conjures a duplicate record at the absent old
                    # path. A profile that is nowhere expected is named, not created.
                    if profile_path.exists():
                        write_text_exact_atomic(profile_path, text)
                    else:
                        rollback_notes.append(
                            'could not undo the flip step (the profile is not at '
                            f'{_rel(profile_path)})')
                elif action == 'folder' and made_folder:
                    dest_folder.rmdir()
            except OSError as undo_err:
                rollback_notes.append(f'could not undo the {action} step ({undo_err})')
        detail = ('; '.join(rollback_notes) + ' - run `fha lint` to check the record'
                  ) if rollback_notes else 'every completed step was undone'
        raise PromotionError(
            f'promoting {name} failed partway ({e}); {detail}. '
            'Nothing is left half-promoted unless noted above.') from e

    return plan


# Matches the `_{S-id}.md` suffix in a source record filename; used by
# find_source_record to locate a source by its ID without trusting the slug.
_SOURCE_RECORD_FILENAME_RE = re.compile(r'_(S-[0-9a-hjkmnp-tv-z]{10})\.md$', re.I)


def find_source_record(
    archive_root: str | Path, source_id: str, unreadable: list | None = None,
) -> dict | None:
    """Return the parsed record dict for a source by its S-id, or None.

    Walks `sources/**/*.md` for a file whose `_{S-id}.md` suffix matches
    `source_id` (case-insensitive). The slug and subdirectory are mutable and
    are not matched - only the suffix carries identity. Used by `fha photoindex`
    to resolve `source-people` person references for photos that carry a matching
    `source_id` keyword: the source record's `people:` list is the human-maintained
    statement "this source shows these people," authoritative even when no bare
    P-id keyword has been written to the image file yet.

    Returns None when the record is absent or its frontmatter has parse errors;
    callers that need `people:` should treat None as "no people known from this source."

    That None is exactly why `unreadable` exists. `rglob` cannot report a
    subdirectory it failed to list, so a source behind an unreadable folder
    answered "no people known from this source" - and photoindex, which
    rebuilds `photo_people` from scratch on every scan, deleted that photo's
    `source-people` rows on the strength of it. Pass a list here and treat a
    non-empty one as "unverified", never as "none".
    """
    root = Path(archive_root)
    sources_dir = root / 'sources'
    if not sources_dir.is_dir():
        return None
    sid_norm = normalize_id(source_id)
    on_error = unreadable_dir_recorder(unreadable) if unreadable is not None else None
    for p in walk_files(sources_dir, suffix='.md', on_error=on_error):
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



def _index_tables_with_path_column(conn: sqlite3.Connection) -> list[str]:
    """Every index table (FTS included) carrying a `path` column - the
    relocation set. FTS5 shadow tables are skipped by name."""
    names = [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
        if not re.search(r'_(config|data|idx|docsize|content)$', row[0])
    ]
    out = []
    for name in names:
        cols = {row[1] for row in conn.execute(f'PRAGMA table_info("{name}")')}
        if 'path' in cols:
            out.append(name)
    return out


def relocate_person_in_index(
    archive_root: Path,
    pid: str,
    moves: list[tuple[Path, Path]],
    *,
    tier: str | None = None,
    new_research: Path | None = None,
) -> str:
    """
    Keep .cache/index.sqlite correct across a person-record move without a
    rebuild (#37) - the index-side twin of `promote_person_record`, here in
    `_lib` because tools never import tools (TOOLING §1).

    `fha person promote` moves a record out of people/stubs/ - a path change
    the mtime watermark cannot see (a move keeps the file's time), which is
    why promote used to DELETE the cache to force a rebuild. That made the
    second promote in a batch fail with 'index.sqlite is unreadable or has an
    incompatible schema... (schema version is missing)', which reads like
    corruption, or with a root_person error that sent the human to fix
    fha.yaml and run `fha stubs` when only the cache was absent. Rewriting the
    rows in place is exact and cheap: every table keyed by `path` (persons,
    person_files, notes_fts, citations, hypotheses, research_log, ...) has its
    old relative path swapped for the new one; the tier flip is applied to
    the persons row; a freshly scaffolded research companion gets its
    person_files row and its body in notes_fts (a scaffold carries no
    hypotheses or log entries yet - the next full build indexes those). The
    sqlite write also bumps index.sqlite's own mtime past the record's, so
    the freshness check reads 'fresh' - correctly, because it is.

    Returns 'indexed', or 'index_absent' when there is no usable full index
    to update (the caller then just says 'run fha index', as before).
    """
    db_path = Path(archive_root) / '.cache' / 'index.sqlite'
    status, _detail = sqlite_cache_schema_status(
        db_path, INDEX_SCHEMA_VERSION, ('persons', 'sources', 'claims'))
    if status != 'fresh' and status != 'stale':
        return 'index_absent'
    pid = normalize_id(pid)
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            tables = _index_tables_with_path_column(conn)
            for old, new in moves:
                old_rel = str(Path(old).relative_to(archive_root))
                new_rel = str(Path(new).relative_to(archive_root))
                if old_rel == new_rel:
                    continue
                for table in tables:
                    conn.execute(
                        f'UPDATE "{table}" SET path=? WHERE path=?', (new_rel, old_rel))
            if tier is not None:
                conn.execute('UPDATE persons SET tier=? WHERE id=?', (tier, pid))
            if new_research is not None:
                rel = str(Path(new_research).relative_to(archive_root))
                conn.execute(
                    'INSERT OR REPLACE INTO person_files(person_id, kind, path, generated) '
                    'VALUES (?,?,?,0)', (pid, 'research', rel))
                try:
                    body = read_record(new_research).get('body') or ''
                except Exception:
                    body = ''
                conn.execute('DELETE FROM notes_fts WHERE path=?', (rel,))
                if body.strip():
                    conn.execute(
                        'INSERT INTO notes_fts(path, content) VALUES (?,?)', (rel, body))
    finally:
        conn.close()
    return 'indexed'


def sync_generated_view_rows(
    archive_root: Path,
    written: list[Path] | tuple[Path, ...] = (),
    removed: list[Path] | tuple[Path, ...] = (),
) -> str:
    """
    Keep the index's rows for generated companion views in step with the files
    `fha views` just wrote or deleted - the row-side twin of the #37 watermark
    exclusion, here in `_lib` because tools never import tools (TOOLING §1).

    Generated companions (timeline, sources-index, draft-queue) are deliberately
    left OUT of `newest_record_mtime` (#37): they are written FROM the index, so
    counting them made every view write stale the index it had just read. But
    `index._index_person` still puts each companion's body into `notes_fts` and
    a row in `person_files`, so without this the two halves disagree: after a
    `fha views refresh`, `fha find --text` would keep returning the PREVIOUS
    timeline's text (or miss a newly generated one) for as long as the index
    stayed "fresh", and `fha views clean` would leave rows for files that no
    longer exist. Rewriting the handful of affected rows is exact and cheap -
    the same trade `relocate_person_in_index` makes for a promote - and it keeps
    a batch of view writes from forcing a full rebuild.

    Only per-person `.md` companions under people/ carry rows: the couple-folder
    `sources-index.md` has no P-id, so `_index_person` skips it, and the
    standalone `--format html` twins live under generated/ which the indexer
    never scans. Both are ignored here for the same reason.

    Returns 'indexed' (rows updated), 'index_absent' (no usable index to
    update - the caller just advises `fha index`), or 'index_error' (the index
    is there but the write failed; the caller must say so, because the rows are
    now the stale ones).
    """
    db_path = Path(archive_root) / '.cache' / 'index.sqlite'
    status, _detail = sqlite_cache_schema_status(
        db_path, INDEX_SCHEMA_VERSION, ('persons', 'sources', 'claims'))
    if status != 'fresh':
        return 'index_absent'

    def _companion_row(path: Path) -> tuple[str, str, str] | None:
        """(relative path, person_id, kind) for a companion-NAMED file, else None.

        Filename-only: enough for a removed file, whose content is already
        gone, and the first half of the test for a written one.
        """
        path = Path(path)
        if path.suffix.lower() != '.md':
            return None
        try:
            rel = str(path.relative_to(archive_root))
        except ValueError:
            return None
        if rel.replace('\\', '/').split('/')[0] != 'people':
            return None
        parsed = parse_filename(path)
        if not parsed or parsed['id_type'] != 'P':
            return None
        if parsed.get('kind') not in GENERATED_COMPANION_KINDS:
            return None
        return rel, parsed['id_str'], parsed['kind']

    def _written_row(path: Path) -> dict | None:
        """What to write for one just-generated file, or None if it carries no rows.

        The record is READ BEFORE the file is classified, because the filename
        cannot settle what the file is: SPEC §13's kind slot is shared with the
        last given name, so `hartley__marie_timeline_P-…` may be Marie Timeline
        Hartley's own record. Classifying by name alone made this the one path
        that could put a companion row back over a person record and re-lose
        her until the next full rebuild - the exact failure the content-first
        rule exists to prevent (`carries_person_record_fields`).

        So a file that carries person-record fields keeps whatever
        `person_files` row it has and gets no companion row written over it;
        its search text is refreshed either way, which is the whole reason this
        sync runs (#37 keeps these paths out of the freshness watermark, so
        nothing downstream would notice the file changed).

        `person_id` and `generated` are derived exactly as
        `index._index_person` derives them - a frontmatter id wins over the
        filename's, and a file carrying one, or carrying a person record, is
        never machine output. Deriving them differently would make these
        incremental rows disagree with a rebuild.
        """
        row = _companion_row(path)
        if row is None:
            return None
        rel, filename_pid, kind = row
        try:
            rec = read_record(archive_root / rel)
        except Exception:
            # Unreadable or unparseable (it was written moments ago, so this is
            # a filesystem oddity, not a normal state): index it as empty
            # rather than let a read error surface as a traceback over a view
            # write that already succeeded. Same posture as
            # relocate_person_in_index's research-companion read.
            rec = {'meta': {}, 'body': ''}
        meta = rec.get('meta') or {}
        is_person_record = carries_person_record_fields(meta)
        meta_pid = normalize_id(str(meta.get('id') or ''))
        return {
            'rel': rel,
            'person_id': meta_pid or filename_pid,
            'kind': kind,
            'body': rec.get('body') or '',
            'is_person_record': is_person_record,
            'generated': 0 if (meta.get('id') or is_person_record) else 1,
        }

    targets_written = [r for r in (_written_row(p) for p in written) if r]
    targets_removed = [r for r in (_companion_row(p) for p in removed) if r]
    if not targets_written and not targets_removed:
        return 'indexed'

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            with conn:
                for rel, _pid, _kind in targets_removed:
                    conn.execute('DELETE FROM notes_fts WHERE path=?', (rel,))
                    conn.execute('DELETE FROM person_files WHERE path=?', (rel,))
                for row in targets_written:
                    rel = row['rel']
                    # Delete before rewrite: notes_fts is an FTS5 table with no
                    # unique key, so a plain insert would stack a second body
                    # row for the same path on every regeneration.
                    conn.execute('DELETE FROM notes_fts WHERE path=?', (rel,))
                    if row['body'].strip():
                        conn.execute(
                            'INSERT INTO notes_fts(path, content) VALUES (?,?)',
                            (rel, row['body']))
                    if row['is_person_record']:
                        # Her `person_files` row says 'profile' and must keep
                        # saying it: person_files is keyed by (person_id, kind,
                        # path), so a companion row here would not replace the
                        # profile row but sit BESIDE it, and the incremental
                        # index would stop matching a rebuild.
                        continue
                    conn.execute(
                        'INSERT OR REPLACE INTO person_files'
                        '(person_id, kind, path, generated) VALUES (?,?,?,?)',
                        (row['person_id'], row['kind'], rel, row['generated']))
        finally:
            conn.close()
    except sqlite3.Error:
        return 'index_error'
    return 'indexed'


# ── Output helpers ────────────────────────────────────────────────────────────

EXIT_CLEAN = 0
EXIT_WARNINGS = 1
EXIT_ERRORS = 2
EXIT_FAILURE = 3

# The archive subfolder that holds the vendored machinery (tools/, docs/,
# design/) so a real archive's root reads as the genealogy, not the tooling.
# `fha install` remaps those subtrees under here; an
# older flat archive into it. The workshop repo itself stays flat. Shared here
# because scaffold (writes it), serve (watches design under it), and doctor
# (locates docs under it) all need the same name.
VENDOR_DIR = '.fha'


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


VENDOR_DIR_NAME = '.fha'


def pip_command(target: str) -> str:
    """Return a `pip install` command that targets the RUNNING interpreter.

    A bare `pip install x` (or even `python -m pip install x`) can install into a
    different interpreter than the one that just failed to import: the launcher
    picks the first of `python3`/`python` (or `py -3`/`python` on Windows) that
    is new enough, which need not be the one whose site-packages the human has
    been installing into. Following that advice then changes nothing, and the
    command keeps failing with the same message - the worst kind of instruction,
    one that looks followed.

    `sys.executable` is by definition the interpreter running this code, which is
    the one missing the import, so `-m pip` against it always lands where it
    needs to.

    Quoted, because that path is not guaranteed to be shell-safe: a virtualenv
    under `~/Family Tools/` or a Windows profile with a space in the name yields
    a command the shell splits at the space, so the fix we advertise fails before
    Python even starts - and it fails in a way that reads as "these instructions
    are wrong" rather than "add quotes". POSIX shells take shlex quoting; cmd.exe
    and PowerShell do not understand single quotes, so Windows gets double ones.
    """
    def _q(word: str) -> str:
        if os.name == 'nt':
            return f'"{word}"' if ' ' in word else word
        return shlex.quote(word)

    # Both halves need it, not just the executable. `-r <path>` carries an
    # archive path, and "Family Archive" is an ordinary folder name - quoting the
    # interpreter while leaving the requirements file bare just moves the split
    # one argument to the right.
    exe = _q(sys.executable)
    if target.startswith('-r '):
        arg = f'-r {_q(target[3:])}'
    else:
        arg = _q(target)
    return f'{exe} -m pip install {arg}'


def requirements_hint() -> str:
    """Return the `pip install -r ...` path that is correct for THIS layout.

    The tool suite lives at `tools/` in a workshop clone but at `.fha/tools/` in
    an installed archive, so a hard-coded `tools/requirements.txt` in a runtime
    error message sends installed-archive owners to a path that does not exist -
    precisely when a dependency is already missing and they need the command to
    work. `requirements.txt` is always this module's own sibling, so anchoring on
    `__file__` is right in both layouts.

    Returned ABSOLUTE. The command is printed for the human to paste, and they
    can be standing anywhere the launcher supports - `fha views --format html`
    run from `people/` is ordinary usage. A path relative to the archive root
    then resolves beneath the current subdirectory instead and fails with "file
    not found", which is a worse outcome than the missing dependency it was
    meant to fix. An absolute path is correct from every directory.
    """
    return str((Path(__file__).parent / 'requirements.txt').resolve())


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
