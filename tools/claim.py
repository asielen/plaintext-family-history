#!/usr/bin/env python3
"""
claim.py - fha claim: human-directed claim review + minting (AGENTS.md, SPEC §8).

  fha claim <C-id> [<C-id> ...]
                   --status accepted|disputed|rejected|needs-review|superseded
                   [--reviewed DATE] [--value "…"] [--date EDTF]
                   [--type TYPE] [--place L-id] [--place-text TEXT]
                   [--persons P-id[,P-id...]] [--confidence high|medium|low]
                   [--information primary|secondary|undetermined]
                   [--evidence direct|indirect|negative] [--anchor TEXT]
                   [--notes TEXT] [--keep-living] [--root PATH] [--dry-run]

  fha claim new --source S-id --type TYPE --value TEXT [--date DATE]
                [--place L-id] [--place-text TEXT] [--persons P-id[,P-id...]]
                [--subtype WORD] [--status suggested|accepted] [--negated]
                [--confidence high|medium|low]
                [--information primary|secondary|undetermined]
                [--evidence direct|indirect|negative] [--anchor TEXT]
                [--notes TEXT] [--keep-living] [--dry-run] [--root PATH]

Two verbs share one file because both are surgical `## Claims` block writers on
an existing source record, and the second (`new`) was built by lifting the
first's machinery rather than inventing a second way to edit the block.

**`fha claim <C-id>`** is the deterministic write-back a human reaches for
during review: moving one claim's `status:` out of `suggested` into any of the
SPEC §8.1 review outcomes (`accepted` / `disputed` / `rejected` / `needs-review`
/ `superseded`), and now also correcting any of `value`/`date`/`type`/`place`/
`place_text`/`persons`/`information`/`evidence`/`anchor`/`notes` on a claim
that already exists. Today that move is done by hand-editing the `## Claims`
YAML or through the `review-claims` AI skill; this tool makes it a real,
contract-safe CLI action that any front door (chat now, a UI later) can drive.

**Mills fields and notes (2026-08, issue #50):** `--information`/`--evidence`
close the gap `--confidence` left open - SPEC §8.5's analytical vocabulary
(Mills, *Evidence Explained*) was only ever settable by hand-editing the
claim's YAML, even though AI-assisted drafting is directed to populate these
fields by default (SPEC §8.4) and the linter's W106 pings an accepted claim
that is still missing them. `--anchor` and `--notes` close the same gap for
the position-in-source pointer and the free-text context line (W109 pings an
accepted substantive/low-confidence-vital claim with no `notes:`). All four
follow the same REPLACE contract every other field flag on this verb already
has (`--value`, `--place-text`, …) - there is no append mode; passing the
flag sets the field to exactly what was given. `--evidence`/`--information`
are validated against SPEC §8.5's closed vocabularies (shared with
`fha claim new` via `_validate_field_args`, `_lib.EVIDENCE_VALUES`/
`INFORMATION_VALUES`); `--anchor`/`--notes` are free text, quoted the same
way `--value`/`--place-text` are (`yaml_inline`). `--notes` deliberately does
NOT share `--value`'s block-scalar refusal: a hand-written `notes: >` or
`notes: |` block is common (SPEC §8.4's own illustrative example uses one),
and refusing to correct it would defeat the flag's whole purpose. The
existing `set_scalar` machinery already replaces a key's FULL span - including
a multi-line block-scalar continuation - safely (`_value_span_end`, built for
`persons:`'s block-list form and exercised here against `notes:` by
`tests/test_claim.py`'s `NotesFieldEditTests`), so no new corruption-guard
logic is needed for this field specifically.

**Contract (AGENTS.md / SPEC §8.2):** only the *human* moves a claim to
`accepted`, and an accepted claim must carry a `reviewed:` date (lint E006). The
human satisfies that contract by *directing* this tool - the editing method does
not matter, only that the decision is theirs. So `--status accepted` always
writes a `reviewed:` stamp: the explicit `--reviewed DATE` when given, otherwise
today (forgiving, since a human is at the keyboard). The tool never accepts on
its own. `disputed` / `rejected` / `superseded` change status rather than
deleting, so the research trail is preserved - `disputed` marks a claim that is
actively contested (e.g. a `contradicts:` standoff) rather than settled either
way, distinct from a `rejected` claim the reviewer has ruled out.

**Compatibility change (2026-07, deliberate):** `--status` is now OPTIONAL on
the edit verb. Before this build every call had to move the status; now a
field-only correction (`fha claim <C-id> --place L-baba9801fa`) is legal on its
own, and at least one mutation flag (`--status`/`--value`/`--date`/`--type`/
`--place`/`--place-text`/`--persons`/`--confidence`/`--information`/
`--evidence`/`--anchor`/`--notes`) is required so a bare `fha claim <C-id>`
with nothing to do is refused rather than silently doing nothing. `reviewed:`
is stamped only when `--status` is given, exactly as before - a field-only edit
never touches status or reviewed.

**Echo on success (2026-08, issue #54).** A successful field write used to say
only "value updated" / "place_text updated" - identical whether the value
written was the one intended or a corrupted one (the concrete repro: on
Windows PowerShell 5.1 a single-quoted string loses embedded double quotes
before the tool ever sees the argument, so `Robert Justin "Bob" Knipscheer`
silently arrives as `Robert Justin Bob Knipscheer`; `--dry-run` showed this in
its diff, a live run did not show it at all). Every free-text/vocabulary field
this verb (and `fha claim new`) can write now echoes the value it actually
wrote as an extra `field: value` line on success - cheap insurance against any
wrapper (a different shell, a CI runner, an MCP bridge) that mangles an
argument before this tool sees it. `--date`/`--type`/`--place`/`--persons`/
`--confidence`/`--information`/`--evidence` already echoed (or now echo,
for the two new Mills fields) their new value inline in the summary line -
closed-vocabulary or already-short values, so the summary line is legible
either way; `--value`/`--place-text`/`--anchor`/`--notes` gained the
explicit echo line.

**Write-time place resolution (2026-08, issue #79 point 3).** Both verbs now
look `--place-text` up against `places/places.yaml` (`_lib.
match_place_text_to_registry`, reusing the exact sorted-token-set
normalization `fha places candidates` clusters unlinked place_text with -
`_lib.place_text_cluster_key`, moved out of places.py so this file can share
it without violating "tools never import tools") whenever `--place` is not
also given. An EXACT match (same normalized text as a registered place's
`name:`/`alt_names:`) auto-attaches that `place_id` - the claim is minted (or,
on the edit verb, only when it does not already carry a `place:`) with
`place:` already set, no separate `fha confirm place` step needed for a place
the registry already knows. A NEAR match (same token set, different word
order or an expanded abbreviation) is reported as a nudge only and never
auto-attached - a wrong `place_id` is worse than an unlinked `place_text`
that the existing candidate-clustering report will still catch. A miss
changes nothing: `place_text:` is written exactly as before. This is the
"real fix" half of #79 (points 1-2, the report-side call-to-action and
threshold escalation, shipped earlier in #100/#142); it is a pure code-level
backstop underneath the skill-level lookup `process-source`/`review-claims`
already perform by hand (issue #81) - a claim minted straight from
`fha claim new` (no skill in the loop) gets the same resolution.

The edit is **surgical**: only the one named claim's entry inside its source
`.md` `## Claims` block is touched - its sibling claims, the block's key order,
and any hand comments are preserved - mirroring `places geocode`'s surgical
`places.yaml` edit and `process --more`'s frontmatter edit. The claim is located
by scanning the `sources/` records directly (the `.md` files are the truth), so
the command works even when `.cache/index.sqlite` is stale or absent. After a
write, re-run `fha index` so the new status enters the query surface.

**Batch status moves (2026-07, TOOLING §3b amendment).** The edit verb takes
SEVERAL C-ids at once - `fha claim C-a C-b C-c --status accepted` - because the
review gesture "accept 1, 2 and 4" is one human decision per claim delivered in
one breath, and the tool should meet that gesture with one command, not four
retypings. The gate: a batch is legal ONLY for a status-only mutation (any of
the five review statuses - batch-reject and batch-needs-review are as
legitimate as batch-accept). Any field-edit flag with more than one id is
refused: a field correction states a new fact about one particular claim, so it
is inherently per-claim and keeps the surgical single-id form. Validation is
ALL-OR-NOTHING: every id is checked (shape and existence) before any write, so
a typo'd id refuses the whole batch rather than applying half a decision.
Duplicate ids are deduped preserving order (one decision, applied once, said so
in the output). The live run edits each claim through the SAME single-claim
surgical path (`run_claim`) - one edit path, never a second one - holding the
results in memory, then writes each record ONCE however many of its claims the
batch touched (#225 follow-up: rewriting one census record per claim, a
fraction of a second apart, collided with Windows antivirus still reading the
last write). One `--dry-run` previews every edit in the batch.

**Accepting a death marks the person as no longer living (2026-11, #246).**
A person with an accepted death certificate, grave or obituary used to stay
`living: unknown`, which counts as living, so every shared export and the site
left them out. Accepting a `death`/`burial` claim - one claim, a batch, or
`fha claim new` minting an accepted one - now runs `_mark_deceased_not_living`
after the write: the person the claim names `deceased:` (or its one person,
when it has no roles) goes from `unknown` to `false` through the shared
`_lib.set_person_living` writer, with one plain line saying so. Nobody else is
ever changed, and a `living: true` is never overwritten - those cases, and a
claim that does not say who died, get a warning naming the fix. Who died is
`_lib.claim_death_reading`, the rule `fha lint` W136 reads; a claim whose
`roles:` names someone missing from its `persons:`, or any name link, names
nobody. Only a claim the command moves INTO accepted marks anyone, so
re-accepting never undoes an earlier `--keep-living`; moving an accepted death
claim back out never reverts the flag, but warns with the `set-living ...
unknown` command (#250 review). `--keep-living` accepts without touching any
flag, and a stopped batch's recovery command carries it.

**`fha claim new`** mints a brand-new claim onto an EXISTING source record - the
CLI-driven counterpart to hand-typing a claim item under `## Claims`. It shares
`_lib.append_claim_to_source` with `fha confirm cooccur` (both mint a claim and
append it to a source's block) rather than a third copy of that machinery.

**Deliberate scope line: no `--type relationship` here, ever - neither to mint
one with `claim new` nor to change an existing claim TO relationship with
`--type` on the edit verb.** SPEC §8.3 requires a `roles:` map on every
relationship claim, and this file's writers only ever set flat scalar keys - no
writer here builds `roles:`. The two sanctioned paths for an actual
relationship tie are unaffected: a hypothesis tie between two people goes
through `fha person relate`; a sourced tie discovered via shared sources goes
through `fha confirm cooccur` (which DOES build the `roles:` map, in its own
file, for exactly this reason). Anything else - a relationship needing a role
vocabulary these two verbs don't cover - is a hand-edit of the claim block,
same as always.

**Also deliberately out of scope: amending `roles:` (issue #50).** The issue
that added `--information`/`--evidence`/`--anchor`/`--notes` also flagged that
`roles:` cannot be set or amended on an existing claim, only tentatively
sketching `--role parent=P-a,P-b --role child=P-c` as a possible shape. That
needs real design, not a flag: `roles:` values can be a single P-id OR a list
(SPEC §8.3's `child: P-de957bcda1` vs `parent: [P-aaaaaaaaaa]`), the role
vocabulary spans four families (child/parent, spouse, head/household_member,
enslaved/enslaver, employer/employee, member) each with different arity
expectations, and (unlike every flat scalar this file writes) an edit here
would interact with `--persons` in ways that are not obvious - does setting a
role auto-add the person to `persons:` if absent, and does removing the last
role for a person imply removing them from `persons:` too? None of that is
settled by SPEC or by the issue itself, so it stays a hand-edit of the claim
block for now, same as minting a `relationship` claim does. One direction of
that interaction IS settled, though (#126/#173 follow-up): `--persons`
REPLACES the whole list, and a replacement that drops someone the claim's
EXISTING `roles:` map still names would silently orphan that role target -
the same hand-edit mistake W133 (`fha lint`) catches, reached through this
tool instead of a typo. `run_claim` refuses that specific edit
(`_existing_role_target_pids`) rather than writing it and waiting for the
next `fha lint` to notice.

CODE MAP
--------
  Shared
    _today                    - review-stamp default (overridable in tests)
    _ClaimEditRefused         - alias of _lib.ClaimEditRefused (shared with fha confirm)
    _fail / _notfound         - thin delegators to the shared _lib.result_fail builder
    _claim_type_problem       - unknown-type / relationship-refusal gate (shared by both verbs)
    _invalid_person_ids       - which --persons entries are not P-id shaped
    _unresolvable_person_ids  - which (shape-valid) P-ids have no person record
    _place_known_in_index     - best-effort L-id existence check (warns, never refuses)
    _validate_field_args      - the place/place_text/confidence/date/persons/
                                information/evidence checks both verbs share
                                verbatim (identical wording)
    _echo_field_lines         - "field: value" success lines shared by run_claim
                                and run_claim_new (issue #54)

  Accepting a death (#246)
    _pronouns / _stays_out    - she/her, he/him, they/them from sex:, for plain lines
    _effective_claim          - the claim as accepted, with this edit's --type/--persons
    _death_reading_of         - _lib.claim_death_reading for a non-negated death/burial
    _mark_deceased_not_living - THE accept-side step every accept path calls: mark
                                who died as living: false (unknown only), warn otherwise
    _warn_still_marked        - an accepted death claim that stops naming someone
                                (un-accepted, or edited): say who is still marked as
                                no longer living (never reverted)

  fha claim <C-id> [<C-id> ...] (review + field edit; batch = status-only)
    _find_claim_record        - scan sources/ for the .md holding one C-id
    _existing_role_target_pids - P-ids an existing roles: map already names
                                (#126/#173: a --persons REPLACE must not orphan one)
    _own_key_indent / _own_id_key_line - which id: line is an item's OWN key
    _apply_claim_review       - surgical `## Claims` block edit (status/value/date/
                                type/place/persons/confidence/information/
                                evidence/anchor/notes)
    run_claim                 - validate, locate, edit ONE claim, return a Result
    _cannot_write             - the one "cannot write" wording, single claim or batch
    _changed_meanwhile        - why a batch left a record someone else wrote mid-batch
    run_claim_batch           - dedupe, all-or-nothing validate, edit each claim via
                                run_claim in memory, write each record once
    _cmd_claim

  fha claim new (mint onto an existing source)
    _render_new_claim_lines   - build one new claim item's YAML lines (SPEC §8.4 field order)
    run_claim_new             - validate, locate source, mint id, append, return a Result
    _cmd_claim_new

  CLI wiring
    _add_arguments / _add_new_arguments
    register / build_claim_new_parser / _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from _lib import (
    locate_claim_records,
    record_claims_by_id,
    CLAIM_TYPES,
    CONFIDENCE_VALUES,
    DEATH_CLAIM_TYPES,
    EVIDENCE_VALUES,
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    INFORMATION_VALUES,
    ClaimEditRefused,
    default_confidence,
    INDEX_SCHEMA_VERSION,
    Result,
    append_claim_to_source,
    claim_item_key_indent,
    claims_edit_problem,
    configure_utf8_stdout,
    DeceasedReading,
    claim_death_reading,
    find_person_record_path,
    find_source_record_path,
    fmt_id_display,
    format_edtf_error,
    id_type_of,
    is_merged_meta,
    is_valid_id,
    link_field_refs,
    match_place_text_to_registry,
    mint_ids,
    normalize_date,
    normalize_id,
    normalize_living,
    parse_filename,
    parse_frontmatter_strict,
    read_record,
    read_text_exact,
    reapply_newline,
    resolve_root_arg,
    result_fail,
    scan_ids_in_tree,
    scan_person_record_ids,
    set_person_living,
    sqlite_cache_schema_status,
    write_text_exact_atomic,
    yaml_edtf,
    yaml_inline,
    safe_load_yaml,
)

configure_utf8_stdout()

# The review statuses a human can move a claim into (SPEC §8.1 lifecycle:
# `suggested → needs-review → accepted | disputed | rejected | superseded`).
# `suggested` is deliberately absent: the AI draft pass produces `suggested`;
# review only ever moves *out* of it, never back to it through this tool.
REVIEW_STATUSES = ('accepted', 'disputed', 'rejected', 'needs-review', 'superseded')

# The statuses `fha claim new` can mint a claim at (SPEC §8.1: suggested is the
# AI-draft entry point, accepted is the human-directed one - review only moves
# a claim OUT of suggested through the other statuses, so minting straight to
# disputed/rejected/needs-review/superseded makes no sense and stays refused).
NEW_CLAIM_STATUSES = ('suggested', 'accepted')

# run_claim's two per-call trailer lines, named so `run_claim_batch` can
# recognise and drop the per-claim repeats (a batch of five accepts should
# nudge `fha index` once, not five times) without the texts ever drifting
# apart from what the single-claim path prints.
_INDEX_REMINDER = 'Reminder: run `fha index` so the new status enters the query surface.'
_DRY_RUN_TRAILER = '[dry-run] No file written. Re-run without --dry-run to apply.'

# The SHAPE of a claim's `id:` key line (optionally after the list dash).
# Shape alone is not ownership: a block scalar (`notes: |`) can quote an
# `id: C-...` line verbatim, so every consumer must also check the line sits at
# the item's own key column (`_own_id_key_line`). Mirrors confirm.py - KEEP IN SYNC.
_CLAIM_ID_KEY_RE = re.compile(
    r'^\s*(?:-\s+)?id:\s*(C-[0-9a-hjkmnp-tv-z]{10})\b', re.I
)


def _today() -> str:
    return datetime.date.today().isoformat()


def _fail(result: Result, status: str, message: str, *, next_step: str | None = None) -> Result:
    """A plain EXIT_FAILURE refusal - delegates to the shared `_lib.result_fail`.

    Both `run_claim` and `run_claim_new` refuse from many call sites; the one
    shared builder (also used by confirm/person/source) keeps the shape from
    drifting. `next_step` carries the exact recovery command when there is one."""
    return result_fail(result, status, message, next_step=next_step)


def _notfound(result: Result, message: str, *, next_step: str | None = None) -> Result:
    """A plain EXIT_WARNINGS not-found - delegates to the shared `_lib.result_fail`."""
    return result_fail(result, 'not-found', message,
                       exit_code=EXIT_WARNINGS, level='warning', next_step=next_step)


def _claim_type_problem(claim_type: str) -> str | None:
    """Validate a `--type` value; return a refusal message, or None if it's fine.

    Two distinct refusals share this one gate, used by both `run_claim_new`
    and `run_claim`'s `--type` field edit so both refuse identically: an
    unrecognised type (not in the SPEC §8.2 vocabulary) and the deliberately
    unsupported `relationship` type. `relationship` needs a `roles:` map
    (SPEC §8.3) that this file's flat-scalar writers do not build - see the
    module docstring's scope note for the two sanctioned paths instead.
    """
    if claim_type not in CLAIM_TYPES:
        return (f'{claim_type!r} is not a claim type. Use one of: '
                f'{", ".join(sorted(CLAIM_TYPES))}.')
    if claim_type == 'relationship':
        return (
            'fha claim does not create or change a claim to type relationship - it needs '
            'a roles: map (SPEC §8.3) this tool does not build. For a hypothesis tie between '
            'two people, use `fha person relate`; for a sourced tie discovered from shared '
            'sources, use `fha confirm cooccur`; otherwise open the source file and add the '
            'roles: map by hand under ## Claims.'
        )
    return None


def _invalid_person_ids(raw_ids: list[str]) -> list[str]:
    """Return the entries of `raw_ids` that are not shaped like a P-id."""
    return [p for p in raw_ids if not (is_valid_id(p) and id_type_of(p) == 'P')]


def _existing_role_target_pids(claim: dict) -> set[str]:
    """Every P-id-SHAPED value already named under this claim's `roles:` map.

    `--persons` REPLACES the whole `persons:` list (module docstring, issue
    #50), and this file never amends `roles:` - but a claim minted by hand
    can already carry one, and a REPLACE that drops someone `roles:` still
    names is exactly the #126/#173 hand-edit mistake W133 (`fha lint`) exists
    to catch, just reached through the sanctioned edit verb instead of a typo:
    `_lib.resolve_claim_persons_with_roles`/`index.py`'s `_index_source` only
    ever walk `persons:` entries asking each one whether some role names it,
    so a role target `--persons` just orphaned is never a candidate again -
    dropped before `vital_subjects` ever sees it, and if that leaves every
    OTHER named person unroled, its single-person legacy fallback reads
    whoever is left as the claim's own subject (a death, say, silently
    reassigned to the widow). `run_claim` uses this to refuse the edit before
    it can create that shape, rather than writing it and waiting for the next
    `fha lint` to notice.

    Deliberately ID-shaped only, not a full `resolve_claim_persons_with_roles`
    walk: this file edits one claim straight off `sources/`, never a built
    index/registry (`_find_claim_record`'s own contract), so there is no
    alias map here to resolve a name-link role value through - `roles: {deceased:
    ["[[Grandpa Smith]]"]}` is invisible to this check the same way an
    unresolvable NAME is invisible to W133 itself. `fha lint` remains the
    backstop for that narrower shape; every reported case of this bug so far
    (the Codex reproduction) used a bare P-id, which this catches.
    """
    roles = claim.get('roles')
    if not isinstance(roles, dict):
        return set()
    out: set[str] = set()
    for role_val in roles.values():
        for ref in link_field_refs(role_val):
            if id_type_of(ref) == 'P':
                out.add(normalize_id(ref))
    return out


def _unresolvable_person_ids(archive_root: Path, ids: list[str]) -> list[str]:
    """Return the (already shape-valid) ids in `ids` with no person record file.

    SPEC §9: every referenced P-id must resolve to at least a stub. `persons:`
    is the load-bearing cross-link on a claim, so writing one that names
    nobody is worse than writing none - it leaves an E005 broken reference for
    lint to find later instead of a plain refusal now. Returned display-cased
    (`fmt_id_display`) since the only use is a human-facing message.
    """
    known = {normalize_id(p) for p in scan_person_record_ids(archive_root)}
    return [fmt_id_display(normalize_id(p)) for p in ids if normalize_id(p) not in known]


def _place_known_in_index(archive_root: Path, place_id: str) -> bool:
    """Best-effort check: does `place_id` appear in `.cache/index.sqlite`'s places table?

    Existence is checked against the INDEX, not `places/places.yaml` directly,
    because this is a deliberately soft, forgiving check (AGENTS.md: "forgiving,
    not fussy") - a freshly-registered place may not be reindexed yet, so a miss
    here becomes a WARNING the caller adds, never a refusal. Returns False both
    when the index genuinely lacks the id and when the index can't be trusted at
    all (missing, corrupt, wrong schema) - the caller's warning wording already
    covers both readings ("it may be new, or the index may be stale"), so the
    distinction does not need to leak out through this return value.
    """
    db_path = archive_root / '.cache' / 'index.sqlite'
    status, _detail = sqlite_cache_schema_status(db_path, INDEX_SCHEMA_VERSION, ('places',))
    if status != 'fresh':
        return False
    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.DatabaseError:
        return False
    try:
        row = conn.execute('SELECT 1 FROM places WHERE id=?', (normalize_id(place_id),)).fetchone()
        return row is not None
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def _resolve_place_text_for_write(
    archive_root: Path, place: str | None, place_text: str | None,
) -> dict | None:
    """Issue #79 point 3 - "resolve at write time": when a claim is about to
    carry `place_text` but no `place`, look the text up against the
    registry (`_lib.match_place_text_to_registry`) before writing.

    Returns None when there is nothing to look up (a `place` was already
    given explicitly - never override an explicit choice - or there is no
    `place_text` to match). Otherwise returns the match dict
    (`{'tier', 'place_id', 'name', 'registry_error'}`); the caller decides
    what to DO with it - only `run_claim`/`run_claim_new` know whether this
    is a brand-new claim or a correction to one that may already carry its
    own `place:`. `registry_error` (Codex review, PR #150) is set only when
    `places/places.yaml` EXISTS but could not be parsed - the caller warns
    with `_place_registry_error_note` instead of treating it as a silent
    miss (`tier` is always `None` alongside a set `registry_error`, since a
    malformed registry yields no rows to match against).

    Deliberately conservative: this only ever *proposes* an id for the
    caller to adopt when the tier is `'exact'`. A `'near'` tier is never
    auto-attached - see the docstring on `match_place_text_to_registry` for
    why a wrong `place_id` is worse than an unlinked `place_text`.
    """
    if place is not None or not place_text or not place_text.strip():
        return None
    return match_place_text_to_registry(archive_root, place_text)


def _place_match_note(cid_display: str, match: dict, *, dry_run: bool = False) -> str | None:
    """The human-facing line for a `_resolve_place_text_for_write` result,
    or None when the tier is neither `'exact'` nor `'near'` (nothing to say
    - a plain miss is silent, exactly as it was before this feature: an
    unlinked `place_text` is the normal case, not a shortfall).

    `'exact'` reports the automatic `place:` write - phrased as something
    that ALREADY HAPPENED on a live run, or something that WOULD happen on
    a `--dry-run` preview (`dry_run=True`). Before this distinction (Codex
    review, PR #150), a `--dry-run` call said "attached place: automatically"
    in the very same breath the dry-run trailer said nothing was written -
    two directly contradictory mutation-status lines in one preview. `'near'`
    reports what did NOT happen and why, with the exact command that would
    link it on the human's own say-so - the same `fha confirm place ...
    --into` arm `process-source`/`place-research` already document for this
    merge case; that wording is accurate on both dry-run and live calls (a
    near match is never auto-attached either way), so it needs no
    dry_run-conditional form.
    """
    if match['tier'] == 'exact':
        verb = 'would attach' if dry_run else 'attached'
        return (
            f"place_text matched the registered place {match['name']!r} "
            f"({match['place_id']}) - {verb} place: automatically (issue #79 "
            f"point 3: write-time place resolution). Wrong match? Correct it with "
            f"`fha claim {cid_display} --place L-...`.")
    if match['tier'] == 'near':
        return (
            f"place_text is close to the registered place {match['name']!r} "
            f"({match['place_id']}) but did not match exactly after normalizing, "
            "so it was NOT linked automatically - a wrong place is worse than none. "
            f"If this is the same place, link it with `fha confirm place {cid_display} "
            f"--into {match['place_id']}`.")
    return None


def _place_registry_error_note(place_text: str, error: str, *, dry_run: bool = False,
                               already: bool = False) -> str:
    """The human-facing warning for a `_resolve_place_text_for_write` result
    whose `registry_error` is set (Codex review, PR #150) - `places.yaml`
    exists but could not be parsed, so the write-time lookup never ran at
    all. Deliberately a WARNING, not a refusal: the claim still mints/edits
    successfully with `place_text` left unlinked (same as a genuine miss),
    but the human deserves an honest reason instead of a silent no-op that
    looks identical to "nothing in the registry matched".

    `dry_run` (Codex review, PR #150 follow-up) exists for the same reason
    `_place_match_note` takes it: the caller only ever emits this note AFTER
    the outcome is known - inside the `--dry-run` preview branch (nothing
    written) or after a live write actually SUCCEEDED - never up front where
    a genuine write failure would surface the false-success wording in the
    same breath as the write error. `dry_run=True` phrases the trailing
    sentence as a preview ("would still be written"); the live wording
    ("was still written") is only ever reached once the write has happened.
    `already` is the third outcome: the claim already said this, so nothing
    was written at all (`run_claim`'s `already-done`).
    """
    if already:
        outcome = 'already said this'
    else:
        outcome = 'would still be written' if dry_run else 'was still written'
    return (
        f'place_text {place_text!r} could not be checked against the place registry - '
        f'places/places.yaml has a problem: {error}. Run `fha lint` to find and fix it; '
        f"until then, place_text won't auto-resolve to a registered place. The claim "
        f'{outcome}, with place_text left unlinked.')


def _validate_field_args(
    result: Result,
    archive_root: Path,
    *,
    place: str | None,
    place_text: str | None,
    persons: list[str] | None,
    confidence: str | None,
    date: str | None,
    information: str | None = None,
    evidence: str | None = None,
) -> tuple[Result | None, str | None, list[str] | None]:
    """Validate the claim field-arguments common to both verbs of `fha claim`.

    `run_claim` (edit) and `run_claim_new` (mint) accept the same
    `--place`/`--place-text`/`--persons`/`--confidence`/`--date`/
    `--information`/`--evidence` arguments and refuse each malformed value
    with byte-identical wording; this is the one copy of those checks so the
    strings cannot drift. The checks run in a
    fixed order - confidence vocabulary, information/evidence vocabulary, date
    shape, place L-id shape, persons shape + resolvability - matching the order
    `run_claim_new` already used; `run_claim` calls this after its own
    status/type checks, which nudges (only) where its confidence refusal fires
    relative to a status refusal when more than one argument is malformed at
    once, but changes no message text.

    `--anchor`/`--notes` are NOT validated here - SPEC §8.4 leaves both free
    text (a position pointer, a context note), so there is no vocabulary to
    check; each caller passes them straight through to the writer.

    Returns `(refusal, normalized_date, persons_norm)`:
      - `refusal` - a completed refusal `Result` the caller returns as-is, or
        None when every check passed.
      - `normalized_date` - `normalize_date(date)` when `date` was given (and
        readable), else None.
      - `persons_norm` - the normalized P-id list when `persons` was given
        (possibly empty), else None. `run_claim` keeps None as "leave the
        list alone"; `run_claim_new` treats None as the empty list.
    """
    # --place and --place-text may be given together: they are DIFFERENT
    # facts, not two spellings of one (SPEC §15 - `place:` is the normalized
    # registry link, `place_text:` the place as written in the source; the
    # elevation flow's whole point is a claim carrying both). Each flag only
    # ever touches its own key - see _apply_claim_review.
    if confidence is not None and confidence not in CONFIDENCE_VALUES:
        return _fail(result, 'failed',
                     f'{confidence!r} is not a confidence level. confidence records evidence '
                     'quality (separate from status, the review state) - use one of: '
                     f'{", ".join(CONFIDENCE_VALUES)} (SPEC §8.5).'), None, None

    if information is not None and information not in INFORMATION_VALUES:
        return _fail(result, 'failed',
                     f'{information!r} is not a Mills information value. information records '
                     'whether the informant behind this assertion was there at the time '
                     '(primary), reporting secondhand (secondary), or unknown - use one of: '
                     f'{", ".join(INFORMATION_VALUES)} (SPEC §8.5).'), None, None

    if evidence is not None and evidence not in EVIDENCE_VALUES:
        return _fail(result, 'failed',
                     f'{evidence!r} is not a Mills evidence value. evidence records whether '
                     'the source answers the question directly (direct), only through '
                     "correlation (indirect), or is evidence of an absence (negative) - use "
                     f'one of: {", ".join(EVIDENCE_VALUES)} (SPEC §8.5).'), None, None

    normalized_date = None
    if date is not None:
        normalized_date = normalize_date(date)
        if normalized_date is None:
            return _fail(result, 'failed', '--date ' + format_edtf_error(date)), None, None

    if place is not None and not (is_valid_id(place) and id_type_of(place) == 'L'):
        return _fail(result, 'failed',
                     f'--place {place!r} is not a valid place ID. L-ids look like '
                     'L-baba9801fa. For the place as the source wrote it, use '
                     '--place-text (the two may be given together).'), None, None

    persons_norm: list[str] | None = None
    if persons is not None:
        bad_shape = _invalid_person_ids(persons)
        if bad_shape:
            return _fail(result, 'failed',
                         'Not a valid person ID: ' + ', '.join(repr(p) for p in bad_shape)
                         + '. P-ids look like P-de957bcda1.'), None, None
        missing = _unresolvable_person_ids(archive_root, persons)
        if missing:
            return _notfound(
                result,
                'No person record for: ' + ', '.join(missing) + '. Mint a stub first with '
                '`fha person new "Name"`, or run `fha stubs --from-names` to create one for '
                'every unresolved name, then retry.',
                next_step='fha person new "Name"'), None, None
        # A person typed twice (`--persons P-a,P-a`) is kept once, in first-seen
        # order: no claim means to name one person twice, and writing the
        # repeat would only earn the claim a lint W135 tidy-up warning (#65).
        persons_norm = list(dict.fromkeys(normalize_id(p) for p in persons))

    return None, normalized_date, persons_norm


# Thin alias: the exception type itself lives in `_lib.ClaimEditRefused` -
# lifted there so `fha claim new`'s call into `_lib.append_claim_to_source`
# (which raises that shared type) can be caught with the same name this file's
# own `_apply_claim_review` refusals already use. Kept as a module-level name
# here so every existing `except _ClaimEditRefused` site, and the tests that
# assert on `claim._ClaimEditRefused`, are unchanged.
_ClaimEditRefused = ClaimEditRefused


# ── Locate the claim's source record ──────────────────────────────────────────

# The single-pass claim lookup is shared with `fha confirm` (#225), so it lives in
# `_lib`; these private names stay for the call sites and tests that use them.
_record_claims = record_claims_by_id
_locate_claims = locate_claim_records


def _find_claim_record(archive_root: Path, claim_id: str, *,
                       hint: Path | None = None) -> tuple[Path, dict] | None:
    """Scan `sources/` for the record whose `## Claims` block holds `claim_id`.

    Returns `(path, claim_dict)` for the first match, or None. The `.md` files
    are archive truth, so this never consults `.cache/index.sqlite` - the write
    must work even when the index is stale or has never been built.

    `hint` is a record a caller already located the claim in (a batch's
    existence gate, #225): that one file is re-read - so the claim dict is
    fresh even after an earlier write in the same batch - and the full scan
    runs only if the claim is no longer there.
    """
    target = normalize_id(claim_id)
    if hint is not None:
        claim = _record_claims(hint).get(target)
        if claim is not None:
            return hint, claim
    return _locate_claims(archive_root, [target]).get(target)


# ── Surgical edit of the one claim's YAML entry ───────────────────────────────

def _own_key_indent(item_lines: list[str], base_indent: str) -> str | None:
    """The exact column of one claim item's OWN mapping keys, or None.

    Sibling of `_lib.claim_item_key_indent`, with one deliberate difference:
    no conventional fallback. The write path wants a best-effort column to
    place a new key at (a wrong guess there is caught by the pre-write
    guard), but *ownership* testing wants certainty - a guessed column could
    bless a look-alike line inside quoted scalar content, which is exactly
    the wrong-claim edit this check exists to prevent. So: an inline first
    key on the dash line pins the column; else the first content line after
    the dash does; else the column is unknowable and the item owns nothing.
    Mirrors confirm.py - KEEP IN SYNC.
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
    return None


def _own_id_key_line(
    lines: list[str], start: int, end: int, base_indent: str,
) -> tuple[int, str] | None:
    """Find one item's OWN `id:` mapping key; return (line_index, C-id) or None.

    `_CLAIM_ID_KEY_RE` describes the shape of an id key line, but a block
    scalar (`notes: |`) may quote such a line verbatim, and quoted lines sit
    DEEPER than the item's real key column (YAML requires it). Only two
    placements are the item's own key: the inline first key on the dash line
    (`- id: C-...` at the item's dash) and a line at exactly the item's key
    column (`_own_key_indent`). Matching on shape alone edited the first item
    whose quoted evidence mentioned the target id - a wrong refusal here (the
    status guard caught it), a wrong WRITE in confirm.py's twin.
    Mirrors confirm.py - KEEP IN SYNC.
    """
    key_indent = _own_key_indent(lines[start:end], base_indent)
    dash_id_re = re.compile(r'^' + re.escape(base_indent) + r'-\s+id:', re.I)
    key_id_re = None
    if key_indent is not None:
        key_id_re = re.compile(r'^' + re.escape(key_indent) + r'id:', re.I)
    for j in range(start, end):
        m = _CLAIM_ID_KEY_RE.match(lines[j])
        if not m:
            continue
        if j == start:
            if dash_id_re.match(lines[j]):
                return j, m.group(1)
        elif key_id_re is not None and key_id_re.match(lines[j]):
            return j, m.group(1)
    return None


def _apply_claim_review(
    text: str,
    claim_id: str,
    *,
    status: str | None = None,
    reviewed: str | None = None,
    value: str | None = None,
    date: str | None = None,
    type_: str | None = None,
    place: str | None = None,
    place_text: str | None = None,
    persons: list[str] | None = None,
    confidence: str | None = None,
    information: str | None = None,
    evidence: str | None = None,
    anchor: str | None = None,
    notes: str | None = None,
) -> tuple[str, bool]:
    """Surgically set any of status/reviewed/value/date/type/place/persons/
    confidence/information/evidence/anchor/notes on one claim.

    Only the claim whose `id:` matches `claim_id` (case-insensitive) inside the
    `## Claims` fenced YAML block is touched; every other line - sibling claims,
    key order, comments - is preserved. Returns `(new_text, changed)`; `changed`
    is False when the block or the claim isn't found (the caller reports a clean
    not-found rather than guessing).

    `status` is optional (2026-07 compatibility change): the caller (`run_claim`)
    guarantees at least one field is being set, but that field need not be
    status any more - a plain field correction is legal on its own. When
    `status` is None the existing `status:` line is located read-only, purely
    to anchor where any NEW keys land (a claim always has one; 0, the item's
    own first line, is the safe fallback if it somehow does not). `reviewed`
    is only ever passed together with `status` - the caller enforces that, so
    this function does not need to.

    `place`/`place_text` are independent keys (SPEC §15: the normalized link
    and the source-as-written wording legally coexist - elevation backfills
    `place:` while never altering `place_text:`), so each argument sets only
    its own key and never removes the other. `persons` REPLACES the whole
    `persons:` list (not append) - the caller pre-validates every P-id
    resolves to a record.

    Edits land at the item's OWN key column, derived from its lines
    (`claim_item_key_indent`): a claim legally written `-   value: farmer` keeps
    its keys at column 4, and writing at the conventional column 2 there would
    break the whole block's YAML. The caller re-parses the result before any
    write (`claims_edit_problem`), so a rewrite this function gets wrong is
    refused rather than saved.

    The claim is located by its OWN `id:` key line (`_own_id_key_line`), never
    by an id merely mentioned in its text - a block scalar quoting an
    `id: C-...` line used to draw the edit onto the quoting claim instead.

    Raises `_ClaimEditRefused` when an edit can't be made without risking
    corruption: `--value` against a multi-line block scalar (`value: >` /
    `value: |`), which a human edits by hand, and the belt-and-braces case
    where the located entry does not read back as the target claim when the
    block is parsed. `--notes` does NOT share `--value`'s block-scalar
    refusal (SPEC §8.4's own illustrative claim uses `notes: >` and a human
    correcting notes through this flag is the issue #50 use case) - the
    generic `set_scalar`/`_value_span_end` machinery below already replaces a
    key's FULL span, block-scalar continuation included, so no separate
    corruption guard is needed for it.
    """
    target = normalize_id(claim_id)
    lines = text.splitlines()

    # 1. Locate the `## Claims` fenced YAML block.
    heading = None
    for i, ln in enumerate(lines):
        if re.match(r'^##\s+Claims\b', ln):
            heading = i
            break
    if heading is None:
        return text, False

    open_fence = None
    for i in range(heading + 1, len(lines)):
        if lines[i].strip() == '```yaml':
            open_fence = i
            break
        if lines[i].startswith('## '):  # next section before any fence
            return text, False
    if open_fence is None:
        return text, False

    close_fence = None
    for i in range(open_fence + 1, len(lines)):
        if lines[i].strip() == '```':
            close_fence = i
            break
    if close_fence is None:
        return text, False

    content_start, content_end = open_fence + 1, close_fence

    # 2. Split the block into top-level claim items (dashes at the base indent).
    dash_lines = [
        i for i in range(content_start, content_end)
        if re.match(r'^(\s*)-\s', lines[i])
    ]
    if not dash_lines:
        return text, False
    base_indent = re.match(r'^(\s*)-', lines[dash_lines[0]]).group(1)
    item_starts = [
        i for i in dash_lines
        if re.match(r'^' + re.escape(base_indent) + r'-\s', lines[i])
    ]
    bounds = item_starts + [content_end]

    target_span = None
    span_index = None
    for k, start in enumerate(item_starts):
        end = bounds[k + 1]
        own = _own_id_key_line(lines, start, end, base_indent)
        if own is not None and normalize_id(own[1]) == target:
            target_span = (start, end)
            span_index = k
            break
    if target_span is None:
        return text, False

    # Belt and braces: the k-th top-level dash is the k-th parsed list item,
    # so the parsed item's `id` must equal the target. A mismatch means the
    # line-level read and YAML disagree about which claim this is - the edit
    # has no safe landing place, so refuse rather than touch the wrong claim.
    try:
        parsed_items = safe_load_yaml('\n'.join(lines[content_start:content_end]))
    except yaml.YAMLError:
        parsed_items = None
    aligned = (
        isinstance(parsed_items, list) and len(parsed_items) == len(item_starts)
        and isinstance(parsed_items[span_index], dict)
        and normalize_id(str(parsed_items[span_index].get('id') or '')) == target
    )
    if not aligned:
        raise _ClaimEditRefused(
            f'the entry carrying the id line for {fmt_id_display(target)} does not '
            'read back as that claim when the block is parsed, so the edit has no '
            'safe landing place. Open the file, make the change by hand under '
            '## Claims, then run `fha lint` to check it.'
        )

    start, end = target_span
    item = lines[start:end]
    # The item's real key column comes from its own lines, never from a fixed
    # base_indent+2 assumption - see claim_item_key_indent for the why. The
    # dash prefix mirrors it so a first-key rewrite keeps the item's column.
    key_indent = claim_item_key_indent(item, base_indent)
    dash_prefix = base_indent + '-' + ' ' * max(1, len(key_indent) - len(base_indent) - 1)

    def find_key(key: str) -> tuple[int | None, str | None, str | None]:
        """Return (index, kind, raw-value) of a top-level item key, or (None, …)."""
        dash_re = re.compile(r'^' + re.escape(base_indent) + r'-\s+' + re.escape(key) + r':\s*(.*)$')
        key_re = re.compile(r'^' + re.escape(key_indent) + re.escape(key) + r':\s*(.*)$')
        for idx, ln in enumerate(item):
            m = dash_re.match(ln)
            if m:
                return idx, 'dash', m.group(1)
            m = key_re.match(ln)
            if m:
                return idx, 'key', m.group(1)
        return None, None, None

    def _value_span_end(idx: int) -> int:
        """Return the exclusive end of the block owned by the key at `item[idx]`:
        every following line up to (not including) the next sibling key written
        at this item's own `key_indent`, or the item's end.

        A flow-style `key: value` line has no continuation, so this only ever
        advances past a genuine block-style value a human wrote by hand - most
        commonly a `persons:` (or any other) key followed by an indented `- …`
        list. Without this, replacing such a key with a flow-style rewrite
        (`set_scalar`) or deleting it (`remove_key`) touched only the header
        line and left its orphaned continuation lines behind, corrupting the
        item's YAML - a block-style `persons:` list rewritten by `--persons`
        used to leave its old `- P-…` lines stranded under the new inline
        list, and the pre-write guard then refused the whole edit."""
        sibling_re = re.compile(r'^' + re.escape(key_indent) + r'\S')
        j = idx + 1
        while j < len(item) and not sibling_re.match(item[j]):
            j += 1
        return j

    def set_scalar(key: str, value_text: str, insert_after: int) -> int:
        idx, kind, _ = find_key(key)
        if idx is not None:
            end = _value_span_end(idx)
            if kind == 'dash':
                item[idx:end] = [f'{dash_prefix}{key}: {value_text}']
            else:
                item[idx:end] = [f'{key_indent}{key}: {value_text}']
            return idx
        pos = insert_after + 1
        item.insert(pos, f'{key_indent}{key}: {value_text}')
        return pos

    # `place:` and `place_text:` are DIFFERENT facts - the normalized
    # registry link and the place as the source wrote it - and a claim
    # legally carries both (SPEC §15: elevation backfills `place:` per
    # claim while "`place_text` itself is never altered"). Each flag below
    # therefore touches ONLY its own key. This replaces the earlier
    # switch-removes-the-other behavior, which made any `--place` edit
    # (including the workbench's untouched-fields claim edit, P2 codex
    # finding, round 4, PR #31) silently erase the source's own wording.
    # With no up-front deletion, no line index computed below can go stale
    # (the 2026-07 stale-anchor corruption came from deleting AFTER anchors
    # were fixed; there is no deletion at all now).

    # 3. status (required on every valid claim). status is now OPTIONAL here
    # (2026-07 compat change: a field-only edit is legal on its own) - when
    # given, it is replaced in place as before; when not, the EXISTING status
    # line is located read-only, purely as the insert point other new keys
    # insert after (0, the item's own first line, is the safe fallback).
    #
    # NAMING NOTE: this "where does the next new key land" tracker is called
    # `insert_pos`, deliberately NOT `anchor` - SPEC §8.4 also names a real
    # claim FIELD `anchor:` (the position-in-source pointer, issue #50's
    # --anchor), and reusing the word for both would shadow one with the
    # other the moment both are in scope.
    if status is not None:
        status_idx = set_scalar('status', status, insert_after=0)
    else:
        found_status_idx, _found_kind, _ = find_key('status')
        status_idx = found_status_idx if found_status_idx is not None else 0

    insert_pos = status_idx
    if reviewed is not None:
        insert_pos = set_scalar('reviewed', reviewed, insert_after=status_idx)
    if date is not None:
        date_idx, date_kind, _ = find_key('date')
        if date_idx is not None:
            set_scalar('date', yaml_edtf(date), insert_after=date_idx)
        else:
            insert_pos = set_scalar('date', yaml_edtf(date), insert_after=insert_pos)
    if value is not None:
        _, _, raw = find_key('value')
        if raw is not None and (raw == '' or raw[:1] in ('>', '|')):
            raise _ClaimEditRefused(
                f'{fmt_id_display(target)} has a multi-line block-scalar value; '
                'edit the value by hand to avoid corrupting it.'
            )
        set_scalar('value', _yaml_inline(value), insert_after=status_idx)
    if type_ is not None:
        type_idx, _type_kind, _ = find_key('type')
        if type_idx is not None:
            set_scalar('type', type_, insert_after=type_idx)
        else:
            insert_pos = set_scalar('type', type_, insert_after=insert_pos)
    if place is not None:
        # Any existing place_text: stays untouched (SPEC §15).
        place_idx, _place_kind, _ = find_key('place')
        if place_idx is not None:
            set_scalar('place', place, insert_after=place_idx)
        else:
            insert_pos = set_scalar('place', place, insert_after=insert_pos)
    if place_text is not None:
        # Any existing place: link stays untouched (SPEC §15).
        pt_idx, _pt_kind, _ = find_key('place_text')
        if pt_idx is not None:
            set_scalar('place_text', _yaml_inline(place_text), insert_after=pt_idx)
        else:
            insert_pos = set_scalar('place_text', _yaml_inline(place_text), insert_after=insert_pos)
    if persons is not None:
        rendered_persons = '[' + ', '.join(fmt_id_display(p) for p in persons) + ']'
        persons_idx, _persons_kind, _ = find_key('persons')
        if persons_idx is not None:
            before_len = len(item)
            set_scalar('persons', rendered_persons, insert_after=persons_idx)
            # A block-style persons: list collapses to one flow-style line here,
            # shrinking `item`. `insert_pos` may already point past this key (e.g.
            # a newly-inserted --date landed right after status, which can sit
            # below a block-style persons: in file order) - shift it by the same
            # delta or confidence's fallback insert below lands short.
            delta = len(item) - before_len
            if delta and insert_pos > persons_idx:
                insert_pos += delta
        else:
            insert_pos = set_scalar('persons', rendered_persons, insert_after=insert_pos)
    if confidence is not None:
        conf_idx, _conf_kind, _ = find_key('confidence')
        if conf_idx is not None:
            set_scalar('confidence', confidence, insert_after=conf_idx)
        else:
            insert_pos = set_scalar('confidence', confidence, insert_after=insert_pos)
    if information is not None:
        info_idx, _info_kind, _ = find_key('information')
        if info_idx is not None:
            set_scalar('information', information, insert_after=info_idx)
        else:
            insert_pos = set_scalar('information', information, insert_after=insert_pos)
    if evidence is not None:
        evid_idx, _evid_kind, _ = find_key('evidence')
        if evid_idx is not None:
            set_scalar('evidence', evidence, insert_after=evid_idx)
        else:
            insert_pos = set_scalar('evidence', evidence, insert_after=insert_pos)
    if anchor is not None:
        anc_idx, _anc_kind, _ = find_key('anchor')
        if anc_idx is not None:
            set_scalar('anchor', _yaml_inline(anchor), insert_after=anc_idx)
        else:
            insert_pos = set_scalar('anchor', _yaml_inline(anchor), insert_after=insert_pos)
    if notes is not None:
        notes_idx, _notes_kind, _ = find_key('notes')
        if notes_idx is not None:
            before_len = len(item)
            set_scalar('notes', _yaml_inline(notes), insert_after=notes_idx)
            # Same block-collapse accounting as persons: above - a multi-line
            # `notes: >`/`notes: |` block scalar collapses to one flow-quoted
            # line, so any insert_pos already pointing past it must shift.
            delta = len(item) - before_len
            if delta and insert_pos > notes_idx:
                insert_pos += delta
        else:
            insert_pos = set_scalar('notes', _yaml_inline(notes), insert_after=insert_pos)

    new_lines = lines[:start] + item + lines[end:]
    trailing_nl = '\n' if text.endswith('\n') else ''
    return '\n'.join(new_lines) + trailing_nl, True


# Thin alias: the quoting rule itself lives in `_lib.yaml_inline` (shared by
# every surgical claim/frontmatter writer - see its docstring for the why).
# Kept as a module-level name here so existing call sites in this file (and
# any test importing `claim._yaml_inline`) do not need to change.
_yaml_inline = yaml_inline


def _echo_field_lines(**fields: str | None) -> list[str]:
    """Issue #54: one 'field: value' line per non-None free-text/vocabulary
    field, for the live-write success message.

    `--date`/`--type`/`--place`/`--persons`/`--confidence`/`--information`/
    `--evidence` already echo their new value INLINE in the one-line summary
    (`date -> 1880`, `persons -> [P-a, P-b]`, `information -> primary`, …) -
    closed-vocabulary or already-short values, so the summary line is
    legible either way - and are not repeated here. This covers the fields
    that used to say only "value updated"/"place_text updated" - identical
    whether the write landed correctly or a wrapper (a shell that ate a
    quote, a CI runner, an MCP bridge) mangled the argument first - plus the
    two new free-text SPEC §8.4 fields from issue #50 (`anchor`/`notes`),
    which would otherwise ship with the exact same blind spot on day one.
    Order is the caller's (`**fields` preserves insertion order in Python
    3.7+), so both call sites control which field prints first."""
    return [f'  {name}: {value}' for name, value in fields.items() if value is not None]


# ── Accepting a death: the person is no longer living (#246) ──────────────────

def _pronouns(meta: dict) -> tuple[str, str]:
    """(she, her) / (he, him) / (they, them), from the record's `sex:` (SPEC §9),
    so one plain line reads "she stays out", never "her stays out"."""
    sex = str(meta.get('sex') or '').strip().upper()
    return {'F': ('she', 'her'), 'M': ('he', 'him')}.get(sex, ('they', 'them'))


def _stays_out(meta: dict) -> str:
    """"she stays out" / "they stay out" - of shared exports and the site."""
    subject, _obj = _pronouns(meta)
    return f'{subject} {"stay" if subject == "they" else "stays"} out'


def _effective_claim(claim: dict, *, claim_type: str | None = None,
                     persons: list[str] | None = None) -> dict:
    """The claim as it reads once this edit lands: the accept, plus any
    `--type`/`--persons` correction given in the same command, so
    `fha claim C-x --type death --status accepted` is judged as the death
    claim it has just become."""
    out = dict(claim)
    out['status'] = 'accepted'
    if claim_type is not None:
        out['type'] = claim_type
    if persons is not None:
        out['persons'] = list(persons)
    return out


def _claim_label(claim: dict) -> str:
    raw = str(claim.get('id') or '').strip()
    return fmt_id_display(normalize_id(raw)) if is_valid_id(raw) else 'this claim'


def _death_reading_of(claim: dict) -> tuple[str, DeceasedReading] | None:
    """(type, reading) for a non-negated death/burial claim, else None.

    Read with `_lib.claim_death_reading` and no alias map: this tool works
    without the index, so any name link in `persons:`/`roles:` makes the
    reading `unsure` rather than letting the person it names drop out."""
    ctype = str(claim.get('type') or '').strip().lower()
    if ctype not in DEATH_CLAIM_TYPES:
        return None
    if str(claim.get('negated')).strip().lower() == 'true':
        return None   # a confirmed absence of a death says nobody died
    return ctype, claim_death_reading(claim)


def _read_person(archive_root: Path, pid: str) -> tuple[Path | None, dict | None]:
    path = find_person_record_path(archive_root, pid)
    if path is None:
        return None, None
    try:
        return path, parse_frontmatter_strict(read_text_exact(path))
    except (OSError, UnicodeDecodeError):
        return path, None


def _mark_deceased_not_living(
    archive_root: Path, result: Result, claims: list[dict], *,
    dry_run: bool = False, keep_living: bool = False,
) -> None:
    """After the human accepts death/burial claims, mark who died as no longer living.

    WHY: a person whose death the archive has accepted (a death certificate,
    a grave, an obituary) but whose `living:` still reads `unknown` is
    treated as living (SPEC §19), so every shared export and the site leave
    them out, and nothing said so. One real archive had 27 such people
    (#246). Accepting the claim is the human's decision that the person
    died; recording that is part of the same decision.

    The ONE accept-side helper: `run_claim` (one claim), `run_claim_batch`
    (after its records are written, from the claims as written) and
    `run_claim_new` (minting an accepted claim) all call it, and `fha serve`
    reaches all three, so every way a human accepts a death behaves the same.
    Callers pass only claims that THIS command moved into `accepted`: a claim
    that was already accepted never changes anyone (#250 review - otherwise a
    rerun undid an earlier `--keep-living`); lint W136 still reports a
    leftover.

    Who counts is `_lib.claim_death_reading` - the rule lint W136 reads: only
    a person the claim names `deceased:`, or the one person on a claim whose
    raw `roles:` is empty. The decision is per PERSON across every claim given:
    one claim naming them is enough, whatever order the claims came in. Only a
    person whose flag reads `unknown` (or is missing) is changed:
      - `living: false` already: nothing to do, and nothing said;
      - `living: true`: a person someone deliberately recorded as alive. A
        conflict, never overwritten - the warning names both ways out;
      - several people and no roles, a death read only by elimination, or a
        claim naming someone it cannot place on its own `persons:` list (a
        `roles:` entry for a person missing from it, any name link): nobody
        is changed, and the warning says what to check. The last kind never
        names a person to mark - the person it would name may be the widow;
      - a negated claim (SPEC §8.6, "we looked and this death did not
        happen") changes nobody.
    A merged record (SPEC §9 tombstone) is skipped, not followed: the claim
    should name the surviving record (lint E016), and the warning gives the
    `set-living` command for the survivor rather than guessing.

    `keep_living` (`--keep-living`) accepts without touching any flag.
    Messages go into `result`; a changed person record lands in
    `result.changed`, and `data['marked_not_living']` lists who was (or,
    under `dry_run`, would be) marked, so `fha serve` knows to rebuild the
    person rows too. The claim's own accept has already succeeded: nothing
    here changes the exit code.
    """
    marked: list[str] = result.data.setdefault('marked_not_living', [])
    named_by: dict[str, tuple[str, str]] = {}    # pid -> (claim, type) naming them
    elim_by: dict[str, tuple[str, str]] = {}     # pid -> (claim, type), by elimination
    for claim in claims:
        if str(claim.get('status') or '').strip().lower() != 'accepted':
            continue
        read = _death_reading_of(claim)
        if read is None:
            continue
        ctype, reading = read
        cid = _claim_label(claim)
        if reading.unsure:
            result.add('warning',
                       f'{cid} names someone it cannot match to one of its own people '
                       f'({", ".join(reading.unsure)}) - a role for someone missing from '
                       'its persons: list, or a name rather than an ID - so it '
                       'cannot be told for sure who died, and nobody was marked as no '
                       'longer living. Run `fha lint` to see what to fix on the claim, '
                       'then mark the person who died with `fha person set-living '
                       '<P-id> false`.')
            continue
        if reading.unscoped:
            shown = ', '.join(fmt_id_display(p) for p in reading.unscoped)
            result.add('warning',
                       f'{cid} names {len(reading.unscoped)} people ({shown}) and does not '
                       'say which of them died, so nobody was marked as no longer living. '
                       'Mark each person who died with `fha person set-living <P-id> '
                       'false`, or add `roles: {deceased: [<P-id>]}` to the claim so the '
                       'archive knows (`fha lint` W132 lists these).')
            continue
        for pid in reading.named:
            named_by.setdefault(pid, (cid, ctype))
        for pid in reading.by_elimination:
            elim_by.setdefault(pid, (cid, ctype))

    for pid in list(named_by) + [p for p in elim_by if p not in named_by]:
        named = pid in named_by
        cid, ctype = named_by[pid] if named else elim_by[pid]
        shown_pid = fmt_id_display(pid)
        path, meta = _read_person(archive_root, pid)
        if path is None or meta is None:
            result.add('warning',
                       f'{cid} is a {ctype} record for {shown_pid}, but '
                       + ('no person record was found for that id'
                          if path is None else f'{path.name} could not be read')
                       + ', so nobody was marked as no longer living. Check the id '
                       f'with `fha find {shown_pid}`.')
            continue
        name = str(meta.get('name') or '').strip() or shown_pid
        _subject, them = _pronouns(meta)
        if is_merged_meta(meta):
            survivor = normalize_id(str(meta.get('merged_into') or ''))
            where = (f'`fha person set-living {fmt_id_display(survivor)} false`'
                     if survivor and is_valid_id(survivor)
                     else f'`fha person set-living <P-id> false` on the surviving '
                          f'record (find it with `fha find {shown_pid}`)')
            result.add('warning',
                       f'{name} ({shown_pid}) was merged into another record, so '
                       f'{cid} should name the surviving record instead (`fha lint` '
                       'lists it as E016). Nobody was marked as no longer living; '
                       f'if this is the same person, run {where}.')
            continue
        living = normalize_living(meta.get('living'))
        if living == 'false':
            continue   # already recorded; nothing to say
        if living == 'true':
            result.add('warning',
                       f'{name} ({shown_pid}) is marked as living, but you accepted '
                       f'{cid}, a {ctype} record for {them}. {name} was left marked '
                       f'as living, so {_stays_out(meta)} of shared exports and the '
                       f'site. If {name} has died, run `fha person set-living '
                       f'{shown_pid} false`; if not, this record may be about someone '
                       f'else - look at {cid} again.')
            continue
        if living not in (None, 'unknown'):
            result.add('warning',
                       f'{name} ({shown_pid}) has living: {meta.get("living")!r}, which '
                       'is not one of true, false or unknown, so it was left alone. If '
                       f'{name} has died, run `fha person set-living {shown_pid} false`.')
            continue
        if not named:
            result.add('warning',
                       f'{cid} does not name anyone as `deceased:`; the archive reads '
                       f'{name} ({shown_pid}) as the one who died, because everyone '
                       f'else on it has another part. {name} was left as not known to '
                       f'have died, so {_stays_out(meta)} of shared exports and the '
                       f'site. If {name} did die, run `fha person set-living '
                       f'{shown_pid} false` (or add `roles: {{deceased: [{shown_pid}]}}` '
                       'to the claim); if not, give them their part on the claim.')
            continue
        if keep_living:
            result.add('info',
                       f'{name} ({shown_pid}) was left as living: unknown '
                       f'(--keep-living), so {_stays_out(meta)} of shared exports and '
                       'the site.')
            continue
        sub = set_person_living(archive_root, pid, 'false', dry_run=dry_run)
        status = sub.data.get('status')
        if status == 'dry-run':
            marked.append(shown_pid)
            result.add('info',
                       f'[dry-run] Would mark {name} as no longer living ({ctype} '
                       f'claim {cid}); shared exports and the site could then include '
                       f'{them}.')
        elif status == 'ok':
            marked.append(shown_pid)
            for changed in sub.changed:
                result.note_changed(changed)
            result.add('info',
                       f'{name} is now marked as no longer living ({ctype} claim '
                       f'{cid}); shared exports and the site may now include {them}.',
                       path=path)
        elif status != 'already':
            why = ' '.join(m.text for m in sub.messages if m.level in ('error', 'warning'))
            result.add('warning',
                       f'{cid} was accepted, but {name} could not be marked as no '
                       f'longer living: {why}')


def _warn_still_marked(
    archive_root: Path, result: Result,
    changes: list[tuple[dict, dict | None]], new_status: str | None, *,
    dry_run: bool = False,
) -> None:
    """An accepted death/burial claim that stops naming someone: say if they
    are still marked as no longer living.

    Accepting the claim may have marked the person as no longer living. Two
    later moves stop the claim saying they died, and neither undoes the flag
    (owner decision, #250 review): the flag may have been set by hand, or rest
    on another record, and quietly hiding someone again is its own surprise.
    So the change is said plainly instead - for each person the claim named
    as the deceased who now reads `living: false`, one warning naming the
    command that puts them back, `fha person set-living <P-id> unknown`:

      - the claim leaves `accepted` (rejected/disputed/needs-review/
        superseded): `changes` holds `(claim as it was, None)`;
      - the claim stays accepted but an edit stops naming them - `--persons`
        drops them, `--type` moves away from death/burial, or the claim now
        reads as negated: `changes` holds `(claim as it was, claim as edited)`.
        Somebody the edit newly names (Ada -> Mary) is NOT marked: only a
        move into accepted marks anyone, and lint W136 reports Mary.
    """
    seen: set[str] = set()
    for before, after in changes:
        read = _death_reading_of(before)
        if read is None:
            continue
        ctype, reading = read
        still: set[str] = set()
        if after is not None:
            now = _death_reading_of(after)
            still = set(now[1].named) if now is not None else set()
        cid = _claim_label(before)
        for pid in reading.named:
            if pid in seen or pid in still:
                continue
            seen.add(pid)
            _path, meta = _read_person(archive_root, pid)
            if meta is None or is_merged_meta(meta):
                continue
            if normalize_living(meta.get('living')) != 'false':
                continue
            name = str(meta.get('name') or '').strip() or fmt_id_display(pid)
            shown = fmt_id_display(pid)
            if after is None:
                moved = 'would no longer be' if dry_run else 'is no longer'
                lead = f'{cid} {moved} accepted (now {new_status}), but {name} ({shown})'
            else:
                moved = 'would no longer name' if dry_run else 'no longer names'
                lead = (f'{cid} {moved} {name} ({shown}) as the one who died, but '
                        f'{_pronouns(meta)[0]}')
            result.add('warning',
                       f'{lead} is still marked as no longer living, so shared exports '
                       f'and the site still include {_pronouns(meta)[1]}. If this '
                       f'{ctype} record was the only reason, run '
                       f'`fha person set-living {shown} unknown`.')


# ── Top-level operation ───────────────────────────────────────────────────────

def _cannot_write(record_path: Path, error: OSError) -> str:
    """The one wording for a record write that failed, single claim or batch."""
    return f'cannot write {record_path}: {error}'


_NOT_UTF8 = ('is not saved as UTF-8 text - re-save it as UTF-8 (`fha lint` names '
             'such files as W128)')


def _changed_meanwhile(record_path: Path, now: bytes) -> str:
    """Why a batch left a record alone: someone else wrote it while it ran.

    `now` is the record's bytes as just re-read. When the other editor also
    saved it in another encoding (a Windows editor's cp1252, most often), the
    human is told that too, in the words the archive's other tools use for
    it, because the rerun would otherwise refuse the file for a reason he
    has not been shown.
    """
    message = (f'{record_path} was changed by something else (another program, or a '
               'second fha session) while this batch ran, so the batch did not write '
               'it - nothing in it was changed by this batch, and the other edit is kept.')
    try:
        now.decode('utf-8')
    except UnicodeDecodeError:
        message += f' It also {_NOT_UTF8} before you rerun.'
    return message


def run_claim(
    archive_root: Path,
    *,
    claim_id: str,
    status: str | None = None,
    reviewed: str | None = None,
    value: str | None = None,
    date: str | None = None,
    claim_type: str | None = None,
    place: str | None = None,
    place_text: str | None = None,
    persons: list[str] | None = None,
    confidence: str | None = None,
    information: str | None = None,
    evidence: str | None = None,
    anchor: str | None = None,
    notes: str | None = None,
    dry_run: bool = False,
    keep_living: bool = False,
    _record_hint: Path | None = None,
    _pending: dict[Path, tuple[str, str]] | None = None,
    _in_batch: bool = False,
    _claims_read: dict[str, dict] | None = None,
) -> Result:
    """Edit one claim (status move and/or field corrections); return a Result.

    `data` is {'status': 'ok'|'already-done'|'invalid-id'|'invalid-status'|
    'invalid-type'|'no-op'|'not-found'|'refused'|'failed', 'claim_id', 'before_status',
    'after_status', 'reviewed', 'source', 'source_id'}. `source` is the record
    path (kept for backward compat); `source_id` is the canonical `S-…` id of
    the holding source, the field a downstream reindexer (`fha serve`) reads to
    know which source to refresh. On a real write the source `.md` is
    listed in `changed`; `--dry-run` previews the YAML change (a unified diff
    in the messages) and writes nothing. Before any write (or preview) the
    rewritten block is re-parsed (`claims_edit_problem`); a rewrite that would
    corrupt the block is a `refused` failure with nothing written, never a
    saved corruption - and when the problem predates the edit (the claim id
    already appears twice in the file, lint E001) the refusal names that
    repair instead of blaming the edit.

    `status` is OPTIONAL (2026-07 compatibility change - see the module
    docstring): at least one of status/value/date/claim_type/place/
    place_text/persons/confidence/information/evidence/anchor/notes must be
    given, or the call is a plain `no-op` refusal.
    `already-done` is the other "nothing happens" outcome, and the opposite
    kind: the edit asked for is already what the record says (the claim is
    already `accepted` with that `reviewed:` date - a rerun of a batch that
    stopped part way), so nothing is written and the call SUCCEEDS (exit 0,
    nothing in `changed`). `no-op` is a refusal of a call that asked for
    nothing; `already-done` is a request the archive had already met. Before
    #225's follow-up such a rerun rewrote the file with identical bytes, and
    on Windows every needless rewrite is another chance to collide with a
    scanner still reading the last one.
    `reviewed:` is stamped only together with a `status` move, exactly as
    before - passing `reviewed` without `status` is refused rather than
    silently ignored, so a human is never left wondering why a date they typed
    did not land. `claim_type == 'relationship'` is refused (see
    `_claim_type_problem`); `place`/`place_text` are independent keys (SPEC
    §15) - each sets only its own, never removing the other, and both may be
    given together; `persons` REPLACES the whole
    list and every P-id must already resolve to a record (SPEC §9) or the call
    refuses naming the missing id and the fix; a replacement that would drop
    someone the claim's existing `roles:` map still names is refused too
    (#126/#173 follow-up: `_existing_role_target_pids` - a bare P-id in
    `roles:` only, no name-link resolution, see its own docstring), naming
    who would be orphaned and the fix. `information`/`evidence` are
    validated against SPEC §8.5's closed vocabularies; `anchor`/`notes` are
    free text (issue #50) - all four REPLACE their field, same as `value`/
    `place_text`. The success message names the re-index next step
    (`fha index`) and echoes each free-text/vocabulary field's new value on
    its own line (issue #54 - a wrapper that mangled the argument before this
    tool saw it is now visible at the point the write happened).

    `place_text` given with no `place` and the claim not already carrying
    one is looked up against the registry (issue #79 point 3) - an exact
    match auto-attaches `place:`, reported as ALREADY attached on a live
    write, or as something that WOULD be attached under `--dry-run` (Codex
    review, PR #150 - the two must never say contradictory things in one
    preview); a near match is only noted, never attached; a miss changes
    nothing. If `places/places.yaml` exists but could not be parsed, the
    lookup cannot run at all - the edit still applies with `place_text`
    unlinked, but a warning names the parse problem and points at `fha
    lint`, rather than looking like an ordinary miss.

    `_pending` is `run_claim_batch`'s in-memory buffer (#225 follow-up):
    path -> (the record's text as first read, its text-to-be). The first half
    lets the batch see, just before it writes, whether something else changed
    the file meanwhile. When given, the record is read from
    the buffer if an earlier claim in the batch already edited it, and the
    finished text is put back there INSTEAD of written - every check above
    still runs per claim, and the batch writes each record once at the end.
    Nothing lands in `changed` in that mode, because nothing has been written
    yet; the batch adds the path when its write succeeds.

    Accepting a `death`/`burial` claim also marks who died as no longer
    living (`_mark_deceased_not_living`, #246; `keep_living` skips that).
    Only a claim this call moves INTO accepted counts: re-accepting an
    accepted claim changes nobody (#250 review). An accepted death/burial
    claim this call moves OUT of accepted, or edits so it no longer names
    someone as the deceased, warns when that person still reads
    `living: false` (`_warn_still_marked`) - never reverted.
    `_in_batch` (set by `run_claim_batch`, live or dry run) leaves both steps
    to the batch, which runs them once for the claims it actually applied;
    `_claims_read` hands it each claim exactly as this call read it.
    """
    result = Result(data={
        'status': None, 'claim_id': None, 'before_status': None,
        'after_status': status, 'reviewed': None, 'source': None,
        'source_id': None,
    })

    # Validate the C-id shape before touching the archive - a malformed id is a
    # plain refusal, never a traceback.
    if not (is_valid_id(claim_id) and id_type_of(claim_id) == 'C'):
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'invalid-id'
        result.add('error',
                   f'{claim_id!r} is not a valid claim ID. C-ids look like C-fd0000001a - '
                   'a C followed by a dash and 10 characters from the archive alphabet.')
        return result

    cid = normalize_id(claim_id)
    result.data['claim_id'] = fmt_id_display(cid)

    if (status is None and value is None and date is None and claim_type is None
            and place is None and place_text is None and persons is None
            and confidence is None and information is None and evidence is None
            and anchor is None and notes is None):
        return _fail(result, 'no-op',
                     'Nothing to change - pass at least one of --status, --value, --date, '
                     '--type, --place, --place-text, --persons, --confidence, '
                     '--information, --evidence, --anchor, or --notes.')

    if status is not None and status not in REVIEW_STATUSES:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'invalid-status'
        result.add('error',
                   f'{status!r} is not a review status. Use one of: {", ".join(REVIEW_STATUSES)}.')
        return result

    if reviewed is not None and status is None:
        return _fail(result, 'failed',
                     '--reviewed only takes effect together with --status (it stamps the '
                     'review date on the status you are setting). Add --status, or drop '
                     '--reviewed if you are only editing another field.')

    if claim_type is not None:
        problem = _claim_type_problem(claim_type)
        if problem is not None:
            return _fail(result, 'refused' if claim_type == 'relationship' else 'invalid-type',
                         problem)

    # The place/place_text/confidence/date/persons/information/evidence checks
    # are shared verbatim with `run_claim_new` - one helper, identical wording
    # (SPEC §8.4/§8.5).
    refusal, normalized_date, persons_norm = _validate_field_args(
        result, archive_root, place=place, place_text=place_text,
        persons=persons, confidence=confidence, date=date,
        information=information, evidence=evidence)
    if refusal is not None:
        return refusal

    # `reviewed:` is the human-review stamp, stamped only together with a
    # --status move (the combination without --status was already refused
    # above). The contract *requires* it for an accepted claim (lint E006);
    # for every review move we record when it happened, defaulting to today
    # when the human didn't pass one.
    if status is not None:
        if reviewed is None:
            reviewed = _today()
        else:
            try:
                datetime.date.fromisoformat(reviewed)
            except ValueError:
                return _fail(result, 'failed',
                             f'--reviewed {reviewed!r} is not a calendar date. Use YYYY-MM-DD, '
                             f'e.g. {_today()}.')
    result.data['reviewed'] = reviewed

    # `_record_hint`: where run_claim_batch's gate already found it (#225).
    found = _find_claim_record(archive_root, cid, hint=_record_hint)
    if found is None:
        result.ok = False
        result.exit_code = EXIT_WARNINGS
        result.data['status'] = 'not-found'
        result.add('warning',
                   f'No claim {fmt_id_display(cid)} found in any source record under '
                   f'{archive_root / "sources"}.',
                   next_step='fha find ' + fmt_id_display(cid))
        return result

    record_path, claim = found
    before_status = str(claim.get('status') or '')
    if _claims_read is not None:
        _claims_read[cid] = claim
    result.data['before_status'] = before_status
    result.data['source'] = str(record_path)

    # `--persons` REPLACES the whole persons: list, and this file never
    # amends roles: (module docstring, issue #50) - so a claim that already
    # carries a roles: map naming someone the new list drops would silently
    # orphan that role target, the #126/#173 hand-edit mistake W133 (`fha
    # lint`) exists to catch, reached here through the sanctioned edit verb
    # instead of a typo. Refuse before writing rather than create the shape
    # and wait for the next `fha lint` to notice it.
    if persons_norm is not None:
        orphaned = _existing_role_target_pids(claim) - set(persons_norm)
        if orphaned:
            named = ', '.join(fmt_id_display(p) for p in sorted(orphaned))
            return _fail(result, 'refused',
                         f'--persons would drop {named}, whom this claim\'s roles: map '
                         f'already names - removing them from persons: without also '
                         f'fixing the roles: entry can leave whoever is left misread as '
                         f'this record\'s own subject (fha lint W133; #126/#173). Include '
                         f'{named} in --persons too, or fix the roles: map by hand first '
                         '(and rerun `fha lint` to confirm), then retry.')

    # Publish the canonical `S-…` id of the holding source alongside its path.
    # A downstream reindexer (`fha serve`) reads `data['source_id']` to know
    # which source to refresh. Derived from the record's `_{S-id}.md` filename
    # suffix (`parse_filename`) rather than a second `read_record` parse of the
    # frontmatter: the suffix is the same identity convention
    # `find_source_record_path` locates the file by, so it is both cheaper (pure
    # string work, no re-parse) and reliable for any file that lives under
    # `sources/` at all. A record with no S-id suffix leaves the field None.
    parsed_name = parse_filename(record_path)
    if parsed_name and parsed_name.get('id_type') == 'S':
        result.data['source_id'] = fmt_id_display(parsed_name['id_str'])

    try:
        # Exact read/write so a one-line status edit doesn't churn every line
        # ending of a CRLF-authored record on Linux (or an LF one on Windows) -
        # the claims-surgery byte-faithful contract (read_text_exact docstring).
        if _pending is not None and record_path in _pending:
            text = _pending[record_path][1]   # an earlier claim in this batch edited it
        else:
            text = read_text_exact(record_path)
    except OSError as e:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'failed'
        result.add('error', f'cannot read {record_path}: {e}')
        return result
    except UnicodeDecodeError:
        # A ValueError, not an OSError: without this it escaped as a
        # traceback (a record re-saved by another editor since it was found).
        return _fail(result, 'failed',
                     f'Nothing was changed: {record_path} {_NOT_UTF8}, then retry.')

    # Issue #79 point 3: only when this claim does not ALREADY carry a
    # place: - never let a place_text correction silently re-point an
    # existing, presumably human-verified link.
    place_match = None
    if not claim.get('place'):
        place_match = _resolve_place_text_for_write(archive_root, place, place_text)
        if place_match is not None and place_match['tier'] == 'exact':
            place = place_match['place_id']

    place_display = fmt_id_display(normalize_id(place)) if place is not None else None

    try:
        new_text, changed = _apply_claim_review(
            text, cid, status=status, reviewed=reviewed,
            value=value, date=normalized_date, type_=claim_type,
            place=place_display, place_text=place_text, persons=persons_norm,
            confidence=confidence, information=information, evidence=evidence,
            anchor=anchor, notes=notes,
        )
    except _ClaimEditRefused as e:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'refused'
        result.add('error', str(e))
        return result

    if not changed:
        # The record parsed with the claim but its raw `## Claims` text didn't -
        # an unusual shape lint should surface; refuse rather than guess.
        result.ok = False
        result.exit_code = EXIT_WARNINGS
        result.data['status'] = 'not-found'
        result.add('warning',
                   f'Found {fmt_id_display(cid)} in {record_path} but could not locate its '
                   'entry in the ## Claims block to edit. Check the block by hand.')
        return result

    # Pre-write guard: re-parse the rewritten block and refuse rather than save
    # text that would hide every claim in this source from lint/index/report.
    # Runs before the dry-run preview too, so preview and live run agree.
    # `expect_status=status` is fine when status is None - claims_edit_problem
    # skips that particular check whenever no status change was requested.
    problem = claims_edit_problem(new_text, cid, expect_status=status)
    if problem is not None:
        result.ok = False
        result.exit_code = EXIT_FAILURE
        result.data['status'] = 'refused'
        if claims_edit_problem(text, cid) is not None:
            # The same check already fails on the UNEDITED text, so this edit
            # did not cause the problem. The only pre-existing state that can
            # reach this point is a duplicate of this claim id (finding the
            # claim required the block to parse and the id to be present), so
            # the honest advice is the duplicate-id repair, not a warning that
            # this edit would hide claims.
            result.add('error',
                       f'Refusing to change {fmt_id_display(cid)}: this claim id appears '
                       f'more than once in {record_path} - a duplicate-id problem (lint '
                       'E001) that predates this edit. Fix the duplicate first: open the '
                       'file, give one of those claims a fresh id (mint one with '
                       '`fha id mint C`), then retry. Nothing was written.')
        else:
            result.add('error',
                       f'Refusing to change {fmt_id_display(cid)}: {problem}, so saving would '
                       f'hide every claim in {record_path} from the tools. Nothing was written. '
                       'Open that file, edit the claim under ## Claims by hand, then run '
                       '`fha lint` to check it.')
        return result

    changed_bits = []
    if status is not None:
        changed_bits.append(f'status {before_status or "(none)"} -> {status} (reviewed {reviewed})')
    if value is not None:
        changed_bits.append('value updated')
    if normalized_date is not None:
        changed_bits.append(f'date -> {normalized_date}')
    if claim_type is not None:
        changed_bits.append(f'type -> {claim_type}')
    if place is not None:
        changed_bits.append(f'place -> {place_display}')
    if place_text is not None:
        changed_bits.append('place_text updated')
    if persons_norm is not None:
        changed_bits.append('persons -> [' + ', '.join(fmt_id_display(p) for p in persons_norm) + ']')
    if confidence is not None:
        changed_bits.append(f'confidence -> {confidence}')
    if information is not None:
        changed_bits.append(f'information -> {information}')
    if evidence is not None:
        changed_bits.append(f'evidence -> {evidence}')
    if anchor is not None:
        changed_bits.append('anchor updated')
    if notes is not None:
        changed_bits.append('notes updated')
    summary = f'{fmt_id_display(cid)}: ' + '; '.join(changed_bits)
    # Issue #54: every free-text field that could be silently mangled between
    # the human/agent typing it and this tool receiving it (value/place_text/
    # anchor/notes) gets its own echoed line on a live write - see
    # _echo_field_lines. information/evidence are closed-vocabulary, already
    # visible in the summary line above, so they are not repeated here.
    echo_lines = _echo_field_lines(value=value, place_text=place_text, anchor=anchor, notes=notes)

    # The registry-error warning names the write's OUTCOME ("was"/"would
    # still be written"), so its result.add() is deferred to the branch
    # that knows the outcome (Codex review, PR #150 follow-up) - built here,
    # emitted below, never up front where a live write that goes on to FAIL
    # would show the false-success wording in the same breath as the error.
    registry_error_note = None
    if place_match is not None:
        if place_match.get('registry_error'):
            registry_error_note = place_match['registry_error']
        note = _place_match_note(fmt_id_display(cid), place_match, dry_run=dry_run)
        if note is not None:
            result.add('info', note)

    # Nothing to change: the claim already says exactly this (a rerun of a
    # batch that stopped part way, or the same decision given twice). Writing
    # the same bytes back is not harmless - on Windows each rewrite can collide
    # with a scanner still reading the last one (#225 follow-up) - so this
    # writes nothing and says the claim was already done. It is a success, not
    # a refusal: the archive already holds what the human asked for.
    # The death-claim steps (#246). Only a move INTO accepted marks anyone:
    # a claim that was already accepted changes nobody, so a rerun cannot
    # undo an earlier --keep-living (#250 review). An accepted claim that
    # stops naming someone as the deceased - moved out of accepted, or
    # edited (--persons/--type) while it stays accepted - only warns
    # (`_warn_still_marked`). A batch (`_in_batch`) runs these itself.
    was_accepted = before_status.strip().lower() == 'accepted'
    accepted_now = None
    still_marked: tuple[dict, dict | None] | None = None
    if not _in_batch:
        edited = _effective_claim(claim, claim_type=claim_type, persons=persons_norm)
        if status == 'accepted' and not was_accepted:
            accepted_now = edited
        elif was_accepted and status is not None and status != 'accepted':
            still_marked = (claim, None)
        elif was_accepted:
            still_marked = (claim, edited)

    out_text = reapply_newline(new_text, text)
    if out_text == text:
        result.data['status'] = 'already-done'
        if status is not None and len(changed_bits) == 1:
            said = f'already {status} (reviewed {reviewed})'
        else:
            said = 'already reads that way'
        if dry_run:
            result.add('info', f'[dry-run] {fmt_id_display(cid)}: {said} - nothing would '
                               'change, so the file would not be rewritten.')
            result.add('info', _DRY_RUN_TRAILER)
        else:
            result.add('info', f'{fmt_id_display(cid)}: {said} - nothing to change, so '
                               'the file was left as it was.', path=record_path)
        if registry_error_note is not None:
            result.add('warning', _place_registry_error_note(
                place_text, registry_error_note, already=True))
        # Already accepted: nobody is marked (#250 review). A person left
        # unmarked is still reported by `fha lint` W136.
        return result

    if dry_run:
        result.data['status'] = 'ok'
        diff = difflib.unified_diff(
            text.splitlines(), new_text.splitlines(),
            fromfile=f'{record_path} (before)', tofile=f'{record_path} (after)',
            lineterm='',
        )
        result.add('info', f'[dry-run] Would set {summary}')
        for dline in diff:
            result.add('info', dline)
        if registry_error_note is not None:
            result.add('warning', _place_registry_error_note(place_text, registry_error_note, dry_run=True))
        if accepted_now is not None:
            _mark_deceased_not_living(archive_root, result, [accepted_now],
                                      dry_run=True, keep_living=keep_living)
        if still_marked is not None:
            _warn_still_marked(archive_root, result, [still_marked], status, dry_run=True)
        result.add('info', _DRY_RUN_TRAILER)
        return result

    if _pending is not None:
        # A batch writes each record once, after every claim in it is edited.
        first_read = _pending[record_path][0] if record_path in _pending else text
        _pending[record_path] = (first_read, out_text)
    else:
        try:
            # Atomic (temp + os.replace): a failed write must leave the record
            # exactly as it was. run_claim_batch tells the human, on a failed
            # write, "the rest were left as they were" and hands back a retry
            # command - a promise a truncating `write_text_exact` could not
            # keep, since a mid-write failure (disk full) would leave the
            # source torn and the advised retry would then read an unparseable
            # file.
            write_text_exact_atomic(record_path, out_text)
        except OSError as e:
            result.ok = False
            result.exit_code = EXIT_FAILURE
            result.data['status'] = 'failed'
            result.add('error', _cannot_write(record_path, e))
            return result
        result.note_changed(record_path)

    result.data['status'] = 'ok'
    result.add('info', f'Set {summary}', path=record_path)
    if registry_error_note is not None:
        result.add('warning', _place_registry_error_note(place_text, registry_error_note, dry_run=False))
    for line in echo_lines:
        result.add('info', line)
    if accepted_now is not None:
        _mark_deceased_not_living(archive_root, result, [accepted_now],
                                  keep_living=keep_living)
    if still_marked is not None:
        _warn_still_marked(archive_root, result, [still_marked], status)
    result.add('info', _INDEX_REMINDER, next_step='fha index')
    return result


def run_claim_batch(
    archive_root: Path,
    *,
    claim_ids: list[str],
    status: str | None = None,
    reviewed: str | None = None,
    value: str | None = None,
    date: str | None = None,
    claim_type: str | None = None,
    place: str | None = None,
    place_text: str | None = None,
    persons: list[str] | None = None,
    confidence: str | None = None,
    information: str | None = None,
    evidence: str | None = None,
    anchor: str | None = None,
    notes: str | None = None,
    dry_run: bool = False,
    keep_living: bool = False,
) -> Result:
    """The edit verb's front door: one C-id or several, one contract (TOOLING §3b).

    WHY this exists: the review gesture "accept 1, 2 and 4" is one human
    decision per claim delivered in one breath - during a review-claims
    session the human states a grouped verdict, and making him retype the
    command per claim adds friction to exactly the moment the whole pipeline
    is built around. So the batch form turns that one breath into one command,
    for ANY of the five review statuses (batch-reject and batch-needs-review
    are as legitimate as batch-accept).

    The status-only gate: with more than one distinct C-id, any field-edit
    argument (value/date/claim_type/place/place_text/persons/confidence/
    information/evidence/anchor/notes) is a plain refusal - a field correction
    states a new fact about one particular claim, so it is inherently
    per-claim and keeps the surgical single-id form. With exactly one distinct
    id this function delegates straight to
    `run_claim` with every argument, so the single-id behavior (field edits
    included) is bit-for-bit the old contract - callers like `_cmd_claim` can
    route everything through here.

    ALL-OR-NOTHING validation: every id is checked up front - shape first,
    then existence (each looked up with the same `sources/` scan the single
    write uses; a handful of ids on a personal archive keeps that cheap) - and
    any bad id refuses the WHOLE batch before any write, naming the id and the
    fix. Half-applied grouped decisions are worse than refused ones: the human
    said one thing about the group, so the archive should record all of it or
    none of it. Duplicate ids are deduped preserving order (one decision,
    applied once) and the dedupe is said in the output.

    The live run edits each claim through `run_claim` - the ONE surgical
    edit path, with its roles guard, edit refusals and re-parse check per
    claim - but in memory (`_pending`), then writes each record ONCE, in the
    order the batch first touched it (#225 follow-up). One census record
    holding forty of the batch's claims used to be rewritten forty times a
    fraction of a second apart, and on Windows a rewrite can collide with
    antivirus still reading the last one. One write per record removes that,
    and makes the batch faster besides. The result is byte for byte what
    applying the claims one by one gives, because each claim is edited
    against the text the earlier ones left. Each claim's own messages are
    kept, with the per-claim `fha index` / dry-run trailers folded into a
    single closing line, and a claim's "Set ..." line appears only once its
    record is actually written.

    Two things stop a batch, and both name what was applied, what was not,
    and the exact command that finishes the rest. A claim `run_claim`
    refuses (possible even after validation: a duplicate C-id inside one
    file) stops the editing; the claims edited before it are still written,
    as they were when each was written as it went. A record that fails to
    write stops the writing: its claims are not applied, the records already
    written stay written, and nothing after it is written (no write after a
    known failure). So does a record that something else changed while the
    batch held its edits: it is left alone rather than overwritten, so the
    other edit survives. Under `--dry-run` nothing is held or written: each claim
    previews against the file as it stands, the same preview as ever.

    A claim that already has the requested status and review date
    (`run_claim`'s `already-done`) counts as applied: nothing is written for
    it, and the closing lines say how many were already done. That is what
    makes rerunning a stopped batch with the same ids safe and quiet - only
    the claims that still need the change touch their files.

    `data` for a real batch is {'status': 'ok'|'refused'|'no-op'|
    'invalid-id'|'invalid-status'|'not-found'|'failed', 'claim_ids' (the
    deduped display list), 'applied' (ids written or already done, or
    previewed under dry-run), 'reviewed', 'results' (each `run_claim` data
    dict, in order; a claim whose edit was ready but whose record failed to
    write says 'failed', and one in a record the batch stopped before writing
    says 'not-written')};
    the single-id delegation returns `run_claim`'s data shape unchanged.
    """
    result = Result(data={
        'status': None, 'claim_ids': [], 'applied': [],
        'reviewed': None, 'results': [],
    })

    # Dedupe preserving order. A repeated id inside one grouped verdict is
    # still one decision about one claim - apply it once and say so, rather
    # than refusing a harmless slip of the tongue.
    seen: set[str] = set()
    deduped: list[str] = []
    duplicates: list[str] = []
    for raw in claim_ids:
        key = raw.strip().lower()
        if key in seen:
            if raw not in duplicates:
                duplicates.append(raw)
            continue
        seen.add(key)
        deduped.append(raw)

    if not deduped:
        return _fail(result, 'failed',
                     'No claim id given - pass at least one C-id (e.g. C-fd0000001a).')

    if len(deduped) == 1:
        # One distinct claim: the exact single-id contract, field edits and
        # all - run_claim owns every check, and its data/messages shape is
        # preserved so existing callers see no difference.
        single = run_claim(
            archive_root, claim_id=deduped[0], status=status, reviewed=reviewed,
            value=value, date=date, claim_type=claim_type, place=place,
            place_text=place_text, persons=persons, confidence=confidence,
            information=information, evidence=evidence, anchor=anchor, notes=notes,
            dry_run=dry_run, keep_living=keep_living)
        if duplicates:
            # Surface the dedupe first, so the human sees why one id "went
            # missing" before reading the edit itself (add-then-move keeps
            # Message construction inside Result.add).
            shown = (fmt_id_display(normalize_id(deduped[0]))
                     if is_valid_id(deduped[0]) and id_type_of(deduped[0]) == 'C'
                     else deduped[0])
            single.add('info', f'{shown} was given more than once - applying it once.')
            single.messages.insert(0, single.messages.pop())
        return single

    field_args = (('--value', value), ('--date', date), ('--type', claim_type),
                  ('--place', place), ('--place-text', place_text),
                  ('--persons', persons), ('--confidence', confidence),
                  ('--information', information), ('--evidence', evidence),
                  ('--anchor', anchor), ('--notes', notes))
    given = [name for name, val in field_args if val is not None]
    if given:
        return _fail(result, 'refused',
                     f'{"/".join(given)} cannot be combined with more than one claim id - '
                     'a field correction is inherently per-claim (each claim states its own '
                     'fact), so it takes exactly one C-id. Run '
                     f'`fha claim <C-id> {given[0]} ...` once per claim that needs the '
                     'correction, or drop the field flags to change just the status on all '
                     f'{len(deduped)} claims. Nothing was changed.')

    if status is None:
        return _fail(result, 'no-op',
                     f'{len(deduped)} claim ids but no --status. Moving several claims at '
                     'once is a status move (e.g. `fha claim C-a C-b --status accepted`) - '
                     'add --status, or edit fields one claim at a time with '
                     '`fha claim <C-id> ...`.')

    if status not in REVIEW_STATUSES:
        return _fail(result, 'invalid-status',
                     f'{status!r} is not a review status. Use one of: '
                     f'{", ".join(REVIEW_STATUSES)}. Nothing was changed.')

    if reviewed is not None:
        try:
            datetime.date.fromisoformat(reviewed)
        except ValueError:
            return _fail(result, 'failed',
                         f'--reviewed {reviewed!r} is not a calendar date. Use YYYY-MM-DD, '
                         f'e.g. {_today()}.')

    # All-or-nothing gate, part 1: every id must LOOK like a claim id before
    # anything is touched, so a typo refuses the batch, not half of it.
    for raw in deduped:
        if not (is_valid_id(raw) and id_type_of(raw) == 'C'):
            return _fail(result, 'invalid-id',
                         f'{raw!r} is not a valid claim ID. C-ids look like C-fd0000001a - '
                         'a C followed by a dash and 10 characters from the archive '
                         'alphabet. The whole batch was left unwritten - fix that id and '
                         're-run.')

    ids = [normalize_id(raw) for raw in deduped]
    result.data['claim_ids'] = [fmt_id_display(cid) for cid in ids]

    # All-or-nothing gate, part 2: every id must EXIST. One scan of sources/
    # finds them all (#225: one scan per id, and again per write, made a
    # 119-claim batch take ~36 minutes); each write is then handed the file its
    # claim was found in, which it re-reads for a fresh copy.
    located = _locate_claims(archive_root, ids)
    missing = [cid for cid in ids if cid not in located]
    if missing:
        shown = ', '.join(fmt_id_display(cid) for cid in missing)
        return _notfound(
            result,
            f'No claim found for: {shown} (searched every source record under '
            f'{archive_root / "sources"}). The whole batch was left unwritten - check '
            'the id, then re-run.',
            next_step='fha find ' + fmt_id_display(missing[0]))

    # One note per distinct claim, however many times (and in whatever case
    # mix) it was repeated on the command line.
    noted: set[str] = set()
    for raw in duplicates:
        dup_id = normalize_id(raw)
        if dup_id in noted:
            continue
        noted.add(dup_id)
        result.add('info',
                   f'{fmt_id_display(dup_id)} was given more than once - '
                   'applying it once.')

    # Edit every claim in memory first, then write each record ONCE (#225
    # follow-up). A batch accepting several claims in one census record used
    # to rewrite that file once per claim, a fraction of a second apart, and
    # on Windows a rewrite can find antivirus still reading the last one. Each
    # claim still goes through `run_claim` - the one surgical edit path, with
    # every per-claim check and message - which hands its finished text to
    # `pending` instead of writing it. A dry run never fills `pending`: each
    # claim previews against the file as it stands, exactly as before.
    pending: dict[Path, tuple[str, str]] | None = None if dry_run else {}
    planned: list[tuple[str, Result]] = []
    stopped: Result | None = None
    claims_read: dict[str, dict] = {}
    for cid in ids:
        sub = run_claim(archive_root, claim_id=cid, status=status,
                        reviewed=reviewed, dry_run=dry_run,
                        _record_hint=located[cid][0], _pending=pending,
                        _in_batch=True, _claims_read=claims_read)
        result.data['results'].append(sub.data)
        if sub.exit_code != EXIT_CLEAN:
            # Stop at the first refusal - never edit past a known problem.
            stopped = sub
            break
        planned.append((cid, sub))

    # Write each edited record once, in the order the batch first touched it.
    # After a refusal above this still writes the claims edited BEFORE it, the
    # same outcome as when each claim was written as it went. A failed write
    # stops here: that record's claims are not applied, records already
    # written stay written, and nothing after it is written.
    #
    # Holding the edits in memory opens a window (seconds, on a big batch)
    # between reading a record and writing it. If the human saves a note in
    # that file meanwhile, or a second session's `fha claim` writes it,
    # writing our copy would silently throw their edit away. So each record
    # is read again just before its write and must still match what the batch
    # first read; if it does not, it is left alone and the batch stops there,
    # exactly as for a failed write, so a rerun picks up the claims left.
    #
    # The check compares BYTES (the first read was strict UTF-8 with no
    # newline translation, so encoding it gives back the file's exact bytes)
    # and never decodes: another editor may have saved the file in cp1252,
    # and a decode error there is a ValueError that would escape this loop
    # and lose the report of the records already written. For the same
    # reason the write catches ValueError (an encode error) beside OSError -
    # every way out of this loop is the plain stop-and-report.
    written: set[Path] = set()
    write_failed: tuple[Path, str] | None = None
    for path, (first_read, out_text) in (pending or {}).items():
        try:
            now = Path(path).read_bytes()
        except OSError as e:
            write_failed = (path, _cannot_write(path, e))
            break
        if now != first_read.encode('utf-8'):
            write_failed = (path, _changed_meanwhile(path, now))
            break
        try:
            write_text_exact_atomic(path, out_text)
        except (OSError, ValueError) as e:
            write_failed = (path, _cannot_write(path, e))
            break
        written.add(path)
        result.note_changed(path)

    applied: list[str] = []
    accepted_claims: list[dict] = []
    unaccepted_claims: list[dict] = []
    as_written: dict[Path, dict[str, dict]] = {}
    for cid, sub in planned:
        if (dry_run or sub.data.get('status') == 'already-done'
                or Path(sub.data['source']) in written):
            applied.append(fmt_id_display(cid))
            # The death-claim steps (#246) read each claim as it now stands
            # on disk - re-read after the write - never the `located`
            # snapshot from before the batch, which a correction saved
            # meanwhile (Ada -> Mary) would make stale (#250 review). A dry
            # run reads the claim as `run_claim` just read it. Only a claim
            # this batch moved into (or out of) accepted counts.
            was_accepted = str(sub.data.get('before_status') or '').strip().lower() \
                == 'accepted'
            moved = sub.data.get('status') != 'already-done' and (
                (status == 'accepted') != was_accepted)
            if moved:
                if dry_run:
                    now = claims_read.get(cid)
                else:
                    path = Path(sub.data['source'])
                    if path not in as_written:
                        as_written[path] = _record_claims(path)
                    now = as_written[path].get(cid)
                if now is not None:
                    (accepted_claims if status == 'accepted' else unaccepted_claims).append(
                        _effective_claim(now) if status == 'accepted' else now)
            for msg in sub.messages:
                # The per-claim trailers collapse into one closing line below.
                if msg.text not in (_INDEX_REMINDER, _DRY_RUN_TRAILER):
                    result.messages.append(msg)
        else:
            # Its edit was ready, but its record was never written.
            failed_here = write_failed is not None and Path(sub.data['source']) == write_failed[0]
            sub.data['status'] = 'failed' if failed_here else 'not-written'

    # Every death the batch accepted (and actually wrote) marks who died as no
    # longer living - the same step a single accept takes (#246), run once
    # here rather than per claim so one person on two records is said once.
    if accepted_claims:
        _mark_deceased_not_living(archive_root, result, accepted_claims,
                                  dry_run=dry_run, keep_living=keep_living)
    if unaccepted_claims:
        _warn_still_marked(archive_root, result, [(c, None) for c in unaccepted_claims],
                           status, dry_run=dry_run)

    if stopped is not None or write_failed is not None:
        # Name what was applied, what was not, and the exact command that
        # finishes the rest. Under dry-run nothing was WRITTEN, only previewed,
        # so the message says "previewed" and the re-run command carries EVERY
        # id (the live-run wording would instruct a decision-dropping recovery:
        # the previewed-but-unwritten claims would silently never apply). An
        # explicit --reviewed rides along either way, so a finished batch
        # never ends up split across two review dates.
        if write_failed is not None:
            result.add('error', write_failed[1])
            exit_code, status_word = EXIT_FAILURE, 'failed'
        else:
            exit_code = stopped.exit_code
            status_word = stopped.data.get('status') or 'failed'
        if stopped is not None:
            for msg in stopped.messages:
                if msg.text not in (_INDEX_REMINDER, _DRY_RUN_TRAILER):
                    result.messages.append(msg)
        applied_set = set(applied)
        not_done = [fmt_id_display(c) for c in ids if fmt_id_display(c) not in applied_set]
        reviewed_part = f' --reviewed {reviewed}' if reviewed else ''
        # --keep-living rides along too: a recovery command that dropped it
        # would mark the people the human chose to keep private (#250 review).
        if keep_living:
            reviewed_part += ' --keep-living'
        level = 'warning' if exit_code == EXIT_WARNINGS else 'error'
        result.ok = False
        result.exit_code = exit_code
        result.data['status'] = status_word
        result.data['applied'] = applied
        if dry_run:
            all_ids = ' '.join(fmt_id_display(c) for c in ids)
            result.add(level,
                       f'Stopped at {not_done[0]}: {len(applied)} of '
                       f'{len(ids)} claims previewed - nothing was written '
                       '(dry-run). Fix the problem above, then re-run '
                       f'`fha claim {all_ids} --status {status}{reviewed_part} '
                       '--dry-run` to re-preview the whole batch.')
        else:
            done_text = ', '.join(applied) if applied else 'none'
            result.add(level,
                       f'Stopped at {not_done[0]}: {len(applied)} of {len(ids)} '
                       f'claims applied ({done_text}), the rest left as they were. Fix the '
                       'problem above, then re-run '
                       f'`fha claim {" ".join(not_done)} --status {status}{reviewed_part}` '
                       'for the rest.')
        return result

    result.data['status'] = 'ok'
    result.data['applied'] = applied
    if result.data['results']:
        result.data['reviewed'] = result.data['results'][0].get('reviewed')
    already = sum(1 for d in result.data['results'] if d.get('status') == 'already-done')
    if already:
        # Counted as applied (the archive already says what was asked), and
        # said plainly, so a rerun after a stopped batch reads as finished
        # rather than as a second round of edits.
        one = already == 1
        if already == len(ids):
            lead = f'All {already} claims'
        else:
            lead = f'{already} of the {len(ids)} claims'
        if dry_run:
            verb, outcome = ('is' if one else 'are'), 'would be'
        else:
            verb, outcome = ('was' if one else 'were'), 'was'
        result.add('info',
                   f'{lead} {verb} already {status} with that review date - '
                   f'nothing {outcome} rewritten for {"it" if one else "them"}.')
    if dry_run:
        result.add('info',
                   f'[dry-run] {len(ids)} claims previewed. No file written. '
                   'Re-run without --dry-run to apply.')
    elif result.changed:
        result.add('info',
                   'Reminder: run `fha index` so the new statuses enter the query surface.',
                   next_step='fha index')
    return result


# ── Top-level operation: fha claim new ──────────────────────────────────────────

def _render_new_claim_lines(
    cid: str, claim_type: str, value: str, *,
    persons: list[str], date: str | None, place: str | None, place_text: str | None,
    subtype: str | None, status: str, reviewed: str | None, confidence: str,
    negated: bool = False, information: str | None = None, evidence: str | None = None,
    anchor: str | None = None, notes: str | None = None,
) -> list[str]:
    """Build the YAML lines for one brand-new claim item (SPEC §8.4 field order).

    Field order mirrors SPEC §8.4's illustrative block (value, id, type,
    persons, date, place, status, confidence, reviewed, notes), with `subtype`
    moved into its natural slot right after `type` - SPEC lists subtype among
    the "other optional fields" at the end of the block, but it qualifies
    type, so a human skimming the item reads it more naturally there.
    `negated: true` sits there too, for the same reason: it inverts what the
    type asserts (a negated marriage claim means "confirmed never married"),
    so it must be visible before the reader reaches the value's details.
    `information` (issue #50) lands where SPEC §8.4's own illustrative block
    lists it, among the "other optional fields" right after `place_text`.
    `evidence` keeps its PRE-EXISTING slot right after `confidence` (where
    the negated-absence case already wrote `evidence: negative` before this
    issue) rather than moving to its SPEC-illustrated spot - the two cases
    (an explicit `--evidence`, an implicit one from `--negated`) render
    through the same line, so there is only one slot to place. `anchor`
    lands right after `evidence`, and `notes` lands in its SPEC-illustrated
    slot, right after `reviewed`.

    `confidence` is always written: SPEC §8.5 marks it required on every claim
    (lint E010, the same required set as `persons`), and the same section
    directs tooling to DEFAULT it from source_type rather than leave it
    missing - `run_claim_new` resolves the default (`_lib.default_confidence`)
    or takes the human's --confidence override, so by the time this renderer
    runs the value is settled. `information`/`notes` are SPEC-legal fields
    this verb used to always leave unset (lint only pings them
    informationally) and now write when the caller supplies them (issue #50).
    `evidence` is set whenever the caller supplies one, OR whenever `negated`
    is true - SPEC §8.6 pairs a confirmed absence with `evidence: negative`,
    and writing one without the other would be a half-recorded conclusion;
    `run_claim_new` refuses an `--evidence` that contradicts `--negated`
    before this renderer ever runs, so the two never disagree here.
    """
    lines = [
        f'- value: {_yaml_inline(value)}',
        f'  id: {fmt_id_display(cid)}',
        f'  type: {claim_type}',
    ]
    if subtype:
        lines.append(f'  subtype: {_yaml_inline(subtype)}')
    if negated:
        lines.append('  negated: true')
    if persons:
        lines.append('  persons: [' + ', '.join(fmt_id_display(p) for p in persons) + ']')
    if date:
        lines.append(f'  date: {yaml_edtf(date)}')
    if place:
        lines.append(f'  place: {fmt_id_display(normalize_id(place))}')
    if place_text:
        # Not an elif: the registry link and the source-as-written wording
        # are different facts and legally coexist on one claim (SPEC §15).
        lines.append(f'  place_text: {_yaml_inline(place_text)}')
    if information:
        lines.append(f'  information: {information}')
    lines.append(f'  status: {status}')
    lines.append(f'  confidence: {confidence}')
    effective_evidence = evidence if evidence is not None else ('negative' if negated else None)
    if effective_evidence:
        lines.append(f'  evidence: {effective_evidence}')
    if anchor:
        lines.append(f'  anchor: {_yaml_inline(anchor)}')
    if status == 'accepted' and reviewed:
        lines.append(f'  reviewed: {reviewed}')
    if notes:
        lines.append(f'  notes: {_yaml_inline(notes)}')
    return lines


def run_claim_new(
    archive_root: Path,
    *,
    source_id: str,
    claim_type: str,
    value: str,
    date: str | None = None,
    place: str | None = None,
    place_text: str | None = None,
    persons: list[str] | None = None,
    subtype: str | None = None,
    status: str = 'accepted',
    confidence: str | None = None,
    negated: bool = False,
    information: str | None = None,
    evidence: str | None = None,
    anchor: str | None = None,
    notes: str | None = None,
    dry_run: bool = False,
    claim_id: str | None = None,
    keep_living: bool = False,
) -> Result:
    """Mint a brand-new claim onto an existing source's `## Claims` block.

    `negated=True` mints a SPEC §8.6 confirmed absence - "we researched and it
    did not happen" - writing `negated: true` plus its §8.6 pairing
    `evidence: negative` (e.g. `--type marriage --negated --value "no marriage
    found"` = confirmed never married). This is the authoring path the BACKLOG
    long noted was missing: the negative fact is a normal claim of its type,
    so everything downstream already understands it (lint counts a negated
    marriage toward vitals completeness, xref treats opposite polarity as a
    contradiction candidate, cooccur/find exclude negated claims from
    positive-evidence surfaces). `--evidence` that contradicts `--negated`
    (anything but `negative`) is refused before any write - SPEC §8.6's
    pairing is mandatory, not a default that a conflicting override silently
    wins over.

    `information`/`evidence`/`anchor`/`notes` (issue #50) are the Mills
    analysis fields and free-text context SPEC §8.4 already allows on every
    claim; before this build `fha claim new` never wrote them, so every claim
    minted this way landed missing them and - for `notes` on a substantive or
    low-confidence-vital claim - immediately tripped the linter's own W109.
    `information`/`evidence` are validated against SPEC §8.5's closed
    vocabularies (shared with the edit verb via `_validate_field_args`);
    `anchor`/`notes` are free text, quoted the same way `place_text` is.

    `claim_id` is NOT a CLI flag - `fha claim new` always mints its own id,
    same as before. It exists for a caller (`fha serve`'s claim.new verb)
    that already ran this SAME function once as a dry run and is now re-
    running it live: reusing that earlier call's minted id here means the
    apply commits exactly the claim the human previewed, instead of
    `mint_ids` drawing a fresh random id on the second call (P2 codex
    finding, round 5, PR #30 - the workbench's two-step preview/apply flow
    otherwise shows one id and creates another). Still collision-checked
    against the whole tree, same as a freshly-minted id would be - a stale
    preview (something else changed the archive in between) is refused, not
    silently reused.

    `data` is {'status': 'ok'|'invalid-id'|'invalid-type'|'invalid-status'|
    'refused'|'failed'|'not-found', 'claim_id', 'source_id'}. On a real write
    the source `.md` is listed in `changed`; `--dry-run` previews the exact
    block that would be appended and writes nothing.

    Default `status='accepted'` is deliberate, and is NOT the same kind of
    optional as `run_claim`'s status (that one is optional because a
    field-only edit needs no status move at all): here a human typing this
    command IS the review gate AGENTS.md requires, so directing this tool at
    the default needs no extra ceremony - `reviewed:` is stamped to today
    automatically. An AI caller is bound by the same AGENTS.md contract to
    pass `status='suggested'` explicitly; this function cannot tell who is
    calling it, so the default assumes the human case and lets an automated
    caller opt out.

    `--type relationship` is refused (`_claim_type_problem`) - see the module
    docstring's scope note for the two sanctioned paths instead. `--persons`
    is OPTIONAL: a claim may legitimately be minted before its persons are
    linked (a source often names one, then more get attached during review),
    but the result carries a warning naming the follow-up command so the gap
    does not go unnoticed. `confidence` (required on every claim, SPEC §8.5)
    is defaulted from the source's `source_type` when not given - the
    spec-directed behavior ("Tooling defaults confidence from source_type") -
    with a message saying what was chosen and how to override.

    Every write funnels through `_lib.append_claim_to_source`, which raises
    `ClaimEditRefused` (caught here as `_ClaimEditRefused`) rather than ever
    saving a rewrite that would corrupt the block - the same insurance
    `run_confirm_cooccur` relies on for the identical append step.

    `place_text` given with no `place` is looked up against the registry
    before the claim is rendered (issue #79 point 3) - an exact match mints
    the claim with `place:` already attached, reported as ALREADY attached
    on a live write, or as something that WOULD be attached under
    `--dry-run` (Codex review, PR #150 - the two must never say
    contradictory things in one preview); a near match is only noted, never
    attached; a miss mints exactly as before, `place_text:` alone. If
    `places/places.yaml` exists but could not be parsed, the lookup cannot
    run at all - the claim still mints with `place_text` unlinked, but a
    warning names the parse problem and points at `fha lint`, rather than
    looking like an ordinary miss.
    """
    result = Result(data={'status': None, 'claim_id': None, 'source_id': None})

    if not (is_valid_id(source_id) and id_type_of(source_id) == 'S'):
        return _fail(result, 'invalid-id',
                     f'{source_id!r} is not a valid source ID. S-ids look like S-fa1234567b.')

    sid = normalize_id(source_id)
    result.data['source_id'] = fmt_id_display(sid)

    if not value or not value.strip():
        return _fail(result, 'failed',
                     '--value cannot be empty - a claim needs its human-readable summary.')

    problem = _claim_type_problem(claim_type)
    if problem is not None:
        return _fail(result, 'refused' if claim_type == 'relationship' else 'invalid-type',
                     problem)

    if status not in NEW_CLAIM_STATUSES:
        return _fail(result, 'invalid-status',
                     f'{status!r} is not a status `fha claim new` can mint at. Use one of: '
                     f'{", ".join(NEW_CLAIM_STATUSES)}. (Review moves a claim on to disputed/'
                     'rejected/needs-review/superseded later, with `fha claim <C-id> --status …`.)')

    if negated and evidence is not None and evidence != 'negative':
        return _fail(result, 'refused',
                     f'--negated records a confirmed absence, which SPEC §8.6 pairs with '
                     f'evidence: negative - --evidence {evidence!r} contradicts that. Drop '
                     '--evidence to let --negated set it automatically, or drop --negated '
                     'if this is not a confirmed-absence claim.')

    # The place/place_text/confidence/date/persons/information/evidence checks
    # are shared verbatim with `run_claim`'s edit verb - one helper, identical
    # wording (SPEC §8.4/§8.5).
    refusal, normalized_date, persons_norm = _validate_field_args(
        result, archive_root, place=place, place_text=place_text,
        persons=persons, confidence=confidence, date=date,
        information=information, evidence=evidence)
    if refusal is not None:
        return refusal
    # This verb builds a fresh claim, so an unset persons list is the empty
    # list (a claim minted before its people are linked), not "leave it alone".
    if persons_norm is None:
        persons_norm = []

    # Issue #79 point 3 - "resolve at write time": a brand-new claim has no
    # place: yet by definition, so this is the primary case the feature
    # targets. Only an exact registry match auto-attaches; see
    # _resolve_place_text_for_write / match_place_text_to_registry.
    place_match = _resolve_place_text_for_write(archive_root, place, place_text)
    if place_match is not None and place_match['tier'] == 'exact':
        place = place_match['place_id']

    source_path = find_source_record_path(archive_root, sid)
    if source_path is None:
        return _notfound(
            result,
            f'No source record {fmt_id_display(sid)} found under {archive_root / "sources"}.',
            next_step='fha find ' + fmt_id_display(sid))

    if claim_id:
        if not (is_valid_id(claim_id) and id_type_of(claim_id) == 'C'):
            return _fail(result, 'invalid-id', f'{claim_id!r} is not a valid C-id.')
        cid = normalize_id(claim_id)
        if cid in scan_ids_in_tree(archive_root):
            return _fail(
                result, 'refused',
                f'{fmt_id_display(cid)} already exists in the archive - the earlier '
                'preview is stale (something else changed since). Preview again, '
                'then apply.')
    else:
        cid = mint_ids('C', 1, archive_root)[0]
    result.data['claim_id'] = fmt_id_display(cid)

    # Confidence is required on every claim (SPEC §8.5) and the same section
    # directs tooling to default it from the source's type rather than leave
    # the field missing. Read the source's own record for its source_type; a
    # record that will not parse still gets the conservative 'medium'.
    if confidence is None:
        try:
            source_meta = read_record(source_path).get('meta') or {}
        except Exception:
            source_meta = {}
        source_type = source_meta.get('source_type')
        confidence = default_confidence(source_type)
        described = f'from source type {source_type!r}' if source_type else \
            'no source_type on the record, so the conservative middle'
        result.add('info',
                   f'confidence defaulted to {confidence} ({described} - SPEC §8.5 rubric). '
                   'Pass --confidence high|medium|low to override.')

    reviewed = _today() if status == 'accepted' else None
    if negated:
        result.add('info',
                   f'Minting a confirmed ABSENCE (SPEC §8.6): this records that the {claim_type} '
                   'did NOT happen, after research - written as `negated: true` with '
                   '`evidence: negative`. Put the searches that justify it in the source record '
                   '(a proof-argument source is the usual home).')
    item_lines = _render_new_claim_lines(
        cid, claim_type, value, persons=persons_norm, date=normalized_date,
        place=place, place_text=place_text, subtype=subtype, status=status, reviewed=reviewed,
        confidence=confidence, negated=negated, information=information, evidence=evidence,
        anchor=anchor, notes=notes,
    )

    try:
        before = read_text_exact(source_path)
    except OSError as e:
        return _fail(result, 'failed', f'cannot read {source_path}: {e}',
                     next_step='Check the file is not open in another program and try again.')

    try:
        after, changed = append_claim_to_source(before, item_lines)
    except ClaimEditRefused as e:
        return _fail(result, 'refused',
                     f'{fmt_id_display(cid)} in {source_path}: {e} Nothing was written.')

    if not changed:
        return _notfound(
            result,
            f'{source_path} has no `## Claims` block to append to. Add the block by hand '
            '(see SPEC.md §14 for the shape), then retry.')

    if not persons_norm:
        result.add('warning',
                   f'{fmt_id_display(cid)} has no persons: yet - `fha lint` will flag it until '
                   f'you link one: `fha claim {fmt_id_display(cid)} --persons P-id[,P-id...]`.')
    # The registry-error warning names the write's OUTCOME ("was"/"would
    # still be written"), so its result.add() is deferred to the branch
    # that knows the outcome (Codex review, PR #150 follow-up) - built here,
    # emitted below, never up front where a live write that goes on to FAIL
    # would show the false-success wording in the same breath as the error.
    registry_error_note = None
    if place_match is not None:
        if place_match.get('registry_error'):
            registry_error_note = place_match['registry_error']
        note = _place_match_note(fmt_id_display(cid), place_match, dry_run=dry_run)
        if note is not None:
            result.add('info', note)
    if place is not None and not _place_known_in_index(archive_root, place):
        result.add('warning',
                   f'--place {fmt_id_display(normalize_id(place))} was not found in the place '
                   'index (it may be new, or `.cache/index.sqlite` may be stale) - the claim '
                   'was still created. Run `fha index` to refresh, or check the id with '
                   f'`fha find {fmt_id_display(normalize_id(place))}`.')

    kind = f'negated {claim_type} (confirmed absence)' if negated else claim_type
    summary = f'{fmt_id_display(cid)}: {kind} ({status}) on {fmt_id_display(sid)}'
    # information/evidence are closed-vocabulary (like run_claim's own
    # changed_bits), so they are named inline here too rather than through
    # _echo_field_lines - without this, a caller passing --information/
    # --evidence to `claim new` never saw what was actually minted, the
    # exact issue #54 blind spot this verb otherwise closes.
    if information is not None:
        summary += f'; information -> {information}'
    if evidence is not None:
        summary += f'; evidence -> {evidence}'

    # Minting an accepted death is accepting it: the same step as `fha claim
    # C-x --status accepted` (#246). A new claim carries no roles:.
    accepted_now = ({'id': cid, 'type': claim_type, 'persons': list(persons_norm or []),
                     'status': 'accepted', 'negated': bool(negated)}
                    if status == 'accepted' else None)

    if dry_run:
        result.data['status'] = 'ok'
        result.add('info', f'[dry-run] Would mint {summary}')
        for dline in difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=f'{source_path} (before)', tofile=f'{source_path} (after)', lineterm='',
        ):
            result.add('info', dline)
        if registry_error_note is not None:
            result.add('warning', _place_registry_error_note(place_text, registry_error_note, dry_run=True))
        if accepted_now is not None:
            _mark_deceased_not_living(archive_root, result, [accepted_now],
                                      dry_run=True, keep_living=keep_living)
        result.add('info', '[dry-run] No file written. Re-run without --dry-run to apply.')
        return result

    try:
        # Atomic, same as run_claim: a source record is often the sole copy of
        # its evidence, so a failed mint must leave it untouched, not truncated.
        write_text_exact_atomic(source_path, reapply_newline(after, before))
    except OSError as e:
        return _fail(result, 'failed', f'cannot write {source_path}: {e}',
                     next_step='Check the file is not open elsewhere and the folder is writable, then retry.')

    if registry_error_note is not None:
        result.add('warning', _place_registry_error_note(place_text, registry_error_note, dry_run=False))
    result.data['status'] = 'ok'
    result.note_changed(source_path)
    result.add('info', f'Minted {summary}', path=source_path)
    # Issue #54, extended to the mint path: a live write's summary line never
    # showed the free-text values it just wrote, so the same silent-mangling
    # blind spot existed here too (a mint is a write like any other). value
    # is always given (required); place_text/anchor/notes are optional.
    for line in _echo_field_lines(value=value, place_text=place_text, anchor=anchor, notes=notes):
        result.add('info', line)
    if accepted_now is not None:
        _mark_deceased_not_living(archive_root, result, [accepted_now],
                                  keep_living=keep_living)
    if status == 'suggested':
        result.add('info',
                   'Minted as `suggested` - review it with '
                   f'`fha claim {fmt_id_display(cid)} --status accepted` before treating it as fact.',
                   next_step=f'fha claim {fmt_id_display(cid)} --status accepted')
    result.add('info', 'Reminder: run `fha index` so the new claim enters the query surface.',
               next_step='fha index')
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cmd_claim(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    persons = None
    if getattr(args, 'persons', None):
        persons = [p.strip() for p in args.persons.split(',') if p.strip()]

    # One front door for one id or several: run_claim_batch delegates a single
    # distinct id straight to run_claim, so the old single-claim behavior
    # (field edits included) is unchanged, and a batch gets the status-only
    # gate plus all-or-nothing validation.
    result = run_claim_batch(
        archive_root,
        claim_ids=list(args.claim_ids),
        status=getattr(args, 'status', None),
        reviewed=getattr(args, 'reviewed', None),
        value=getattr(args, 'value', None),
        date=getattr(args, 'date', None),
        claim_type=getattr(args, 'claim_type', None),
        place=getattr(args, 'place', None),
        place_text=getattr(args, 'place_text', None),
        persons=persons,
        confidence=getattr(args, 'confidence', None),
        information=getattr(args, 'information', None),
        evidence=getattr(args, 'evidence', None),
        anchor=getattr(args, 'anchor', None),
        notes=getattr(args, 'notes', None),
        dry_run=bool(getattr(args, 'dry_run', False)),
        keep_living=bool(getattr(args, 'keep_living', False)),
    )
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def _cmd_claim_new(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    persons = None
    if getattr(args, 'persons', None):
        persons = [p.strip() for p in args.persons.split(',') if p.strip()]

    result = run_claim_new(
        archive_root,
        source_id=args.source_id,
        claim_type=args.claim_type,
        value=args.value,
        date=getattr(args, 'date', None),
        place=getattr(args, 'place', None),
        place_text=getattr(args, 'place_text', None),
        persons=persons,
        subtype=getattr(args, 'subtype', None),
        status=args.status,
        confidence=getattr(args, 'confidence', None),
        negated=bool(getattr(args, 'negated', False)),
        information=getattr(args, 'information', None),
        evidence=getattr(args, 'evidence', None),
        anchor=getattr(args, 'anchor', None),
        notes=getattr(args, 'notes', None),
        dry_run=bool(getattr(args, 'dry_run', False)),
        keep_living=bool(getattr(args, 'keep_living', False)),
    )
    for msg in result.messages:
        stream = sys.stderr if msg.level == 'error' else sys.stdout
        prefix = 'ERROR: ' if msg.level == 'error' else ''
        print(f'{prefix}{msg.text}', file=stream)
    return result.exit_code


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('claim_ids', metavar='C-id', nargs='+',
                   help='The claim(s) to review (e.g. C-fd0000001a). Several C-ids apply '
                        'one status move to each - batches are status-only, and any bad id '
                        'stops the whole batch before anything is written.')
    p.add_argument('--status', choices=REVIEW_STATUSES,
                   help='The review status to set. At least one of --status/--value/--date/'
                        '--type/--place/--place-text/--persons/--confidence/--information/'
                        '--evidence/--anchor/--notes is required.')
    p.add_argument('--reviewed', metavar='DATE',
                   help='Review date (YYYY-MM-DD); defaults to today. Only takes effect '
                        'together with --status.')
    p.add_argument('--value', metavar='TEXT',
                   help='Optionally correct the claim value (single-line scalar only).')
    p.add_argument('--date', metavar='EDTF',
                   help="Optionally correct the claim's date (EDTF, e.g. 1880 or 1880-06-15).")
    p.add_argument('--type', dest='claim_type', metavar='TYPE', choices=sorted(CLAIM_TYPES),
                   help='Optionally correct the claim type (SPEC §8.2). Changing TO '
                        'relationship is refused - see `fha claim --help`.')
    p.add_argument('--place', metavar='L-id',
                   help='Optionally set the normalized place link. An existing '
                        'place_text: (the place as the source wrote it) is left '
                        'untouched - the two coexist (SPEC §15).')
    p.add_argument('--place-text', metavar='TEXT', dest='place_text',
                   help='Optionally set the place as written in the source. An '
                        'existing place: link is left untouched.')
    p.add_argument('--persons', metavar='P-id[,P-id...]',
                   help='Optionally REPLACE the whole persons: list, comma-separated P-ids '
                        '(every id must already have a person record).')
    p.add_argument('--confidence', choices=CONFIDENCE_VALUES,
                   help='Optionally set the evidence-quality level (SPEC §8.5; separate '
                        'from --status, the review state).')
    p.add_argument('--information', choices=INFORMATION_VALUES,
                   help='Optionally set the Mills information value (SPEC §8.5): was the '
                        'informant behind this assertion there at the time (primary), '
                        'reporting secondhand (secondary), or unknown.')
    p.add_argument('--evidence', choices=EVIDENCE_VALUES,
                   help='Optionally set the Mills evidence value (SPEC §8.5): does the '
                        'source answer the question directly (direct), only through '
                        'correlation (indirect), or is it evidence of an absence (negative).')
    p.add_argument('--anchor', metavar='TEXT',
                   help='Optionally set the position inside the source this claim rests on '
                        '(a timestamp, page, or line - e.g. "00:14:32" or "p. 12").')
    p.add_argument('--notes', metavar='TEXT',
                   help='Optionally REPLACE the claim\'s notes: - the context/detail behind '
                        'it. Safe even when the existing notes: is a multi-line block '
                        '(notes: > or notes: |); the whole field is replaced, never appended.')
    p.add_argument('--keep-living', action='store_true', dest='keep_living',
                   help='Accepting a death or burial claim normally marks the person who '
                        'died as no longer living (living: false), so shared exports and '
                        'the site can include them. Pass this to accept the claim and leave '
                        'their living flag as it is.')
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview the YAML change without writing.')


def _add_new_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('--source', required=True, metavar='S-id', dest='source_id',
                   help='The source this claim is cited to (e.g. S-fa1234567b).')
    p.add_argument('--type', required=True, dest='claim_type', metavar='TYPE',
                   choices=sorted(CLAIM_TYPES),
                   help='The claim type (SPEC §8.2). relationship is refused - see below.')
    p.add_argument('--value', required=True, metavar='TEXT',
                   help='The human-readable summary of the assertion.')
    p.add_argument('--date', metavar='DATE',
                   help='When it happened - EDTF (1880-06-15) or plain words like "about 1880".')
    p.add_argument('--place', metavar='L-id',
                   help='The place, by its registry id (e.g. L-baba9801fa).')
    p.add_argument('--place-text', metavar='TEXT', dest='place_text',
                   help='The place as written in the source - alone when it has no '
                        'L-id yet, or beside --place to keep the wording too.')
    p.add_argument('--persons', metavar='P-id[,P-id...]',
                   help='Who the claim is about, comma-separated P-ids. Optional, but lint '
                        'will flag the claim until at least one is linked.')
    p.add_argument('--negated', action='store_true',
                   help='Mint a confirmed ABSENCE (SPEC §8.6): "we researched and it did not '
                        'happen" - e.g. --type marriage --negated --value "no marriage found" '
                        'records confirmed-never-married. Writes negated: true and its pairing '
                        'evidence: negative on the claim.')
    p.add_argument('--subtype', metavar='WORD',
                   help='Free-text refinement of the type (e.g. deacon for occupation).')
    p.add_argument('--status', choices=NEW_CLAIM_STATUSES, default='accepted',
                   help='Review state to mint at (default: accepted - a human directing this '
                        'IS the review; AI callers must pass --status suggested).')
    p.add_argument('--confidence', choices=CONFIDENCE_VALUES,
                   help='Evidence-quality level (SPEC §8.5). Defaults from the source\'s '
                        'source_type (vital-record: high, interview: low, else medium).')
    p.add_argument('--information', choices=INFORMATION_VALUES,
                   help='Mills information value (SPEC §8.5): was the informant behind '
                        'this assertion there at the time (primary), reporting secondhand '
                        '(secondary), or unknown. Optional - omit if undetermined.')
    p.add_argument('--evidence', choices=EVIDENCE_VALUES,
                   help='Mills evidence value (SPEC §8.5): does the source answer the '
                        'question directly (direct), only through correlation (indirect), '
                        'or is it evidence of an absence (negative). --negated sets this to '
                        'negative automatically - --evidence must agree if both are given.')
    p.add_argument('--anchor', metavar='TEXT',
                   help='Position inside the source this claim rests on (a timestamp, '
                        'page, or line - e.g. "00:14:32" or "p. 12").')
    p.add_argument('--notes', metavar='TEXT',
                   help='The context/detail behind the claim - EXPECTED (not required) on '
                        'a substantive or low-confidence-vital claim (lint W109 pings an '
                        'accepted one with none).')
    p.add_argument('--keep-living', action='store_true', dest='keep_living',
                   help='Accepting a death or burial claim normally marks the person who '
                        'died as no longer living (living: false), so shared exports and '
                        'the site can include them. Pass this to accept the claim and leave '
                        'their living flag as it is.')
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.add_argument('--dry-run', action='store_true', dest='dry_run',
                   help='Preview the claim block that would be appended, without writing.')


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Record your verdict on a suggested fact - the human decision point.

  fha claim <C-id> --status accepted   Confirm a fact (stamps today's date)
  fha claim <C-id> --status disputed|rejected|needs-review|superseded
  fha claim C-a C-b C-c --status accepted   One verdict for several claims at once
  fha claim <C-id> --place L-baba9801fa   Correct a field without touching status

Only you move a claim to accepted. Nothing becomes a fact until you decide here.
Accepting a death or burial claim also marks the person who died as no longer
living, so shared exports and the site can include them (--keep-living to
leave the flag alone); someone marked as living is never changed.
At least one of --status/--value/--date/--type/--place/--place-text/--persons/
--confidence/--information/--evidence/--anchor/--notes is required. Several
C-ids at once move status only - every id is checked first, and one bad id
stops the whole batch before anything is written; field corrections take
exactly one claim. Preview any change first with --dry-run.

To mint a brand-new claim onto a source, use `fha claim new` (`fha claim new --help`)."""

_NEW_CLI_DESCRIPTION = """\
Mint a brand-new claim onto an existing source record.

  fha claim new --source S-… --type occupation --value "Bookkeeper, 1874 directory" \\
      --persons P-… --date 1874 --status accepted

Defaults to --status accepted (typing this command IS the review). Pass
--status suggested for an AI-drafted claim awaiting human review.
--type relationship is refused - it needs a roles: map this command does not
build; use `fha person relate` (hypothesis) or `fha confirm cooccur` (sourced)
instead. --information/--evidence/--anchor/--notes (SPEC §8.4/§8.5) mint the
Mills analysis fields and context note in the same step, instead of needing a
hand-edit right after minting. Preview any change first with --dry-run."""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'claim' onto the main fha parser.

    Only the flat review verb (`fha claim <C-id> …`) is registered here.
    `fha claim new …` is NOT reachable through this subparser tree - its
    positional-C-id shape would misparse `new` as a claim id - it is routed
    instead by `fha.py`'s `_intercept_claim_new`, the same early-interception
    pattern `fha gedcom import` uses (TOOLING §13a2)."""
    p = subs.add_parser(
        'claim',
        help='Review claims: move status (one C-id or several) and/or correct fields '
             '(human-directed write-back)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_cmd_claim)
    return p


def build_claim_new_parser() -> argparse.ArgumentParser:
    """Build the standalone parser for `fha claim new` / `python tools/claim.py new`.

    A separate parser (not a subparser of `register()`'s flat `claim` parser)
    because `fha.py`'s `_intercept_claim_new` builds and runs this BEFORE the
    main dispatcher's flat parser ever sees the argv - the same reason
    `gedcom_import.py` keeps its own standalone parser rather than nesting
    under `gedcom`'s positional-P-id parser.
    """
    parser = argparse.ArgumentParser(
        prog='fha claim new',
        description=_NEW_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_new_arguments(parser)
    parser.set_defaults(func=_cmd_claim_new)
    return parser


def _standalone_main(argv: list[str] | None = None) -> int:
    """Entry point for `python tools/claim.py …`.

    Mirrors `fha.py`'s `_intercept_claim_new`: when the first token is `new`,
    route to `build_claim_new_parser()` instead of the flat review parser, so
    `python tools/claim.py new --source … …` works the same as `fha claim new`.
    """
    argv_list = list(argv) if argv is not None else sys.argv[1:]
    if argv_list and argv_list[0] == 'new':
        args = build_claim_new_parser().parse_args(argv_list[1:])
        return args.func(args)

    parser = argparse.ArgumentParser(
        prog='fha claim',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    args = parser.parse_args(argv_list)
    return _cmd_claim(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
