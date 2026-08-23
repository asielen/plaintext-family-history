#!/usr/bin/env python3
"""
report.py - fha report: the session report (research feed).

  fha report [--full] [--section NAME]

The "login screen": refreshes the index/photo cache, runs lint, diffs the
result against the last session's snapshot, and assembles a markdown research
feed - discoveries first, chores second (TOOLING §15a).  Consumed almost
entirely through the `/today` skill, which narrates this output and offers to
start the top item.

ARCHITECTURE OVERVIEW
----------------------
`fha report` is the one tool in the suite explicitly designed to call other
tools' logic directly rather than treat them as black boxes (BUILD.md M5.1:
"call tool logic directly, not subprocess").  It imports `index`, `lint`,
`photoindex`, and `cooccur` as modules and calls their `run_*`/`build_index`/
`_run_lint_core` entry points in-process.  Every other tool in this suite
follows the "tools never import other tools" rule; report is the orchestrator
that sits above that rule, not an exception to be copied elsewhere.

Refresh sequence (TOOLING §15a step 1-3), run on every invocation regardless
of `--full`/`--section` (the report's own freshness, not its diffing baseline):
  1. `index.build_index(...)` - full index rebuild
  2. `photoindex.run_scan(..., full=False)` - incremental photo metadata
     refresh; runs *after* the index rebuild because it derives face-tag/
     name-variant matches from `.cache/index.sqlite` and should see this
     session's fresh data, not last session's. Wrapped in try/except so an
     exiftool failure degrades Section 6 only, not the whole report.
  3. `lint._run_lint_core(...)` - in-memory lint pass (gives both raw Finding
     objects and the Registry that produced them; `run_lint_silent` only
     returns counts, which the discoveries/vitals-gaps/contradictions
     sections need more than)

SNAPSHOT
--------
`.cache/last_report.json` is intentionally a superset of the minimal example
in BUILD.md/TOOLING §15a: alongside `source_ids`/`person_ids`/`claim_statuses`
it also stores per-claim status, claim_links, relationship edges, the W101
vitals-gap person set, and per-question status - the extra bookkeeping a
"what changed since last time" diff needs that aggregate counts alone cannot
answer (e.g. "did claim C-x move from needs-review to accepted" requires
knowing C-x's *prior* status, not just a prior total).  `--full` ignores this
snapshot (treats `prev` as empty) but still writes a fresh one afterward.

Writing `notes/discoveries.md` and confirming/dismissing `fha cooccur`
candidates both require human confirmation (TOOLING §15a) - that interactive
loop is owned by the today skill's reaction flow (mirrors `fha cooccur`'s
read-only tombstone discipline); this tool only ever proposes and prints.

CODE MAP
--------
  Constants
    SECTIONS                   - (key, number-label, title) in display order

  Snapshot
    _load_snapshot / _write_snapshot  - .cache/last_report.json read/write
    (parse_questions            - questions.md + research files -> {file :: heading: {...}} -
                                 lives in _lib, shared with site.py, issue #117)
    _vitals_gap_pids            - W101 findings -> sorted P-id list (via registry paths)
    _build_snapshot             - current-state snapshot dict from the just-refreshed index

  Section builders (one per TOOLING §15a section; each returns list[str] lines)
    _section_discoveries         - §0: claim status flips, new corroborations,
                                    newly-answered questions, vitals gaps closed,
                                    newly confirmed relationship edges
    _section_review_queue        - §1: W102 backlog, grouped by source
    _section_second_look         - §1b: parked needs-review + accepted-low-confidence
                                    claims, counts + oldest few
    _section_new_since_last      - §2: source/claim/person id set diff vs snapshot
    _section_vitals_gaps         - §3: W101 findings, formatted
    _section_contradictions      - §4: E009 findings, formatted
    _section_search_log          - §5: search_log lookups for current leads
    _section_answerable_questions - §5b: open questions with a closeable gap
    _live_alias, _is_missing_key - reconcile's 'MISSING:' catalog key, read/tested
    _photo_scan_notes            - §6: what this session's photo scan could NOT see
    _section_photo_triage        - §6: photoindex.run_triage embed
    _fetch_place_candidates       - the one places.run_candidates() call per
                                    report run, shared by both consumers below
                                    (Codex review, PR #142 finding 2 - a
                                    second independent fetch re-ran the whole
                                    GPS photo-cluster pass and doubled any
                                    stale-photo-index warning)
    _place_text_group_line       - one place-text cluster's report line;
                                    recommends `--into <L-id>` instead of
                                    minting via `--name` when the cluster's
                                    label already matches a registered place
                                    (Codex review, PR #142 finding 1); names
                                    a duplicate-name registry clash instead
                                    of recommending a mint through it when
                                    the match was ambiguous (Codex review,
                                    PR #142 follow-up finding 2 - real
                                    `ambiguous_ids` off the match, never
                                    guessed)
    _section_place_candidates    - §6b: renders `_fetch_place_candidates`'s
                                    result, each place-text cluster line
                                    carrying its own `fha confirm place`
                                    command (issue #79)
    _place_text_escalations      - oversized (20+ claim) place-text clusters,
                                    promoted above every section (issue #79
                                    point 2) - same clusters §6b lists, never
                                    re-derived; reads `_fetch_place_
                                    candidates`'s shared result, never its own
    _section_hypotheses          - §7: open hypotheses + draft-queue backlog
    _section_promotion_candidates - §7b: direct-line stubs + claim-heavy stubs
                                    (the fha person promote surface; stateless)
    _section_possible_connections - §8: cooccur.run_cooccur top candidates

  Rendering / orchestration
    _person_label                - 'Name [P-xxxx]' display helper
    _render_report                - assemble ordered markdown from section bodies
    run_report                    - top-level: refresh, diff, render, persist

  CLI
    register, _cmd_report, _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    configure_utf8_stdout,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    build_ahnentafel_map,
    extract_token_ids,
    FhaConfigError,
    Result,
    fmt_id_display,
    load_fha_yaml,
    match_place_text_to_registry,
    normalize_id,
    open_index_db,
    parse_questions,
    read_record,
    is_working_copy,
    resolve_root_arg,
    resolve_root_generation,
    root_generation_seed_position,
    shell_quote,
)

import cooccur
import index
import lint
import photoindex

configure_utf8_stdout()

# ── Section registry ─────────────────────────────────────────────────────────

SECTIONS: list[tuple[str, str, str]] = [
    ('discoveries', '0', 'Discoveries since last session'),
    ('review-queue', '1', 'Review queue'),
    ('second-look', '1b', 'Worth a second look'),
    ('new-since-last', '2', 'New since last session'),
    ('vitals-gaps', '3', 'Vitals gaps'),
    ('contradictions', '4', 'Contradictions'),
    ('search-log', '5', 'Search-log awareness'),
    ('answerable-questions', '5b', 'Answerable questions'),
    ('photo-triage', '6', 'Photo processing triage'),
    ('place-candidates', '6b', 'Place candidates'),
    ('hypotheses', '7', 'Hypotheses & draft queues'),
    ('promotion-candidates', '7b', 'Promotion candidates'),
    ('possible-connections', '8', 'Possible connections'),
]
_SECTION_KEYS = {key for key, _num, _title in SECTIONS}

_SEARCH_LOG_HORIZON_DAYS = 18 * 30   # TOOLING §15a §5 default re-run horizon
_CAPTURE_RECENCY_DAYS = 30   # how long an unreconciled `fha capture` stays called out here


# ── Snapshot ──────────────────────────────────────────────────────────────────

def _load_snapshot(archive_root: Path) -> dict:
    """Read .cache/last_report.json. Missing/corrupt file -> empty dict (no prior baseline)."""
    path = archive_root / '.cache' / 'last_report.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_snapshot(archive_root: Path, snapshot: dict) -> None:
    path = archive_root / '.cache' / 'last_report.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding='utf-8')


# The `## Q:` block grammar (heading/status/refs regexes) and the two parse
# functions that used to live here (`_parse_question_blocks`/`_parse_questions`)
# moved to `_lib.py` as `parse_question_blocks`/`parse_questions` (issue #117):
# `fha site` needed the same parse to surface a person's open questions on
# their page, and `site.py` cannot import `report.py` (tools never import
# tools - this module is the documented exception, not a precedent to copy).
# Imported above; every call site below kept its old behavior unchanged.


def _vitals_gap_pids(findings: list, registry) -> list[str]:
    """W101 findings -> sorted P-id list, via the registry's path->pid map."""
    path_to_pid: dict[str, str] = {}
    for pid, paths in registry.person_profile_paths.items():
        for p in paths:
            path_to_pid[str(p)] = pid
    pids = {
        path_to_pid[f.path] for f in findings
        if f.code == 'W101' and f.path in path_to_pid
    }
    return sorted(pids)


def _build_snapshot(conn, archive_root: Path, findings: list, registry) -> dict:
    """Current-state snapshot dict, built right after the refresh sequence."""
    source_ids = sorted(r[0] for r in conn.execute('SELECT id FROM sources'))
    person_ids = sorted(r[0] for r in conn.execute('SELECT id FROM persons'))
    source_fingerprints = {
        r['id']: '|'.join(
            str(r[k] or '') for k in ('title', 'source_type', 'restricted', 'path')
        )
        for r in conn.execute('SELECT id, title, source_type, restricted, path FROM sources')
    }
    person_fingerprints = {
        r['id']: '|'.join(
            str(r[k] or '') for k in (
                'name', 'surname', 'sex', 'living', 'tier', 'status',
                'no_known_marriages', 'no_known_children',
            )
        )
        for r in conn.execute(
            '''
            SELECT id, name, surname, sex, living, tier, status,
                   no_known_marriages, no_known_children
            FROM persons
            '''
        )
    }
    claim_status_by_id = {r[0]: r[1] for r in conn.execute('SELECT id, status FROM claims')}
    # claim_persons participants (person + role) are part of a claim's identity
    # too -- reattaching a claim to a different person/role is a real change
    # even though every scalar claim field stays the same, so it must flow
    # into the fingerprint or section 2 ("changed since last session") misses it.
    claim_persons_by_claim: dict[str, list[str]] = {}
    for r in conn.execute(
        'SELECT claim_id, person_id, position, role FROM claim_persons ORDER BY claim_id, position'
    ):
        claim_persons_by_claim.setdefault(r['claim_id'], []).append(
            f"{r['person_id']}:{r['position']}:{r['role'] or ''}"
        )
    claim_fingerprints = {
        r['id']: '|'.join(
            str(r[k] or '')
            for k in (
                'source_id', 'type', 'subtype', 'date_edtf', 'place_id', 'place_text',
                'value', 'status', 'reviewed', 'confidence', 'information', 'evidence',
                'asset', 'anchor', 'hypothesis', 'negated', 'notes',
            )
        ) + '|persons=' + ','.join(claim_persons_by_claim.get(r['id'], []))
        for r in conn.execute(
            '''
            SELECT id, source_id, type, subtype, date_edtf, place_id, place_text,
                   value, status, reviewed, confidence, information, evidence,
                   asset, anchor, hypothesis, negated, notes
            FROM claims
            '''
        )
    }
    claim_statuses = {
        status: sum(1 for s in claim_status_by_id.values() if s == status)
        for status in ('accepted', 'needs-review', 'suggested')
    }
    claim_links = sorted(
        [r[0], r[1], r[2]] for r in conn.execute('SELECT claim_id, rel, target_id FROM claim_links')
    )
    relationships = sorted(
        {tuple(r) for r in conn.execute('SELECT person_id, rel, other_id FROM relationships')}
    )
    questions = parse_questions(archive_root)
    # E009 contradiction messages, so a resolution that adds an open question
    # (refs both claim-ids, no claim_links change) without changing claim
    # status still shows up as "resolved" in section 0 -- a pure claim_links
    # diff alone never catches that case, since claim_links never changed.
    e009_messages = sorted(f.message for f in findings if f.code == 'E009')

    return {
        'generated': datetime.date.today().isoformat(),
        'source_ids': source_ids,
        'person_ids': person_ids,
        'source_fingerprints': source_fingerprints,
        'person_fingerprints': person_fingerprints,
        'claim_ids': sorted(claim_status_by_id),
        'claim_statuses': claim_statuses,
        'claim_status_by_id': claim_status_by_id,
        'claim_fingerprints': claim_fingerprints,
        'claim_links': claim_links,
        'relationships': [list(t) for t in relationships],
        'vitals_gap_person_ids': _vitals_gap_pids(findings, registry),
        'question_status_by_heading': {h: info['status'] for h, info in questions.items()},
        'e009_messages': e009_messages,
    }


# ── Formatting helper ─────────────────────────────────────────────────────────

def _person_label(conn, pid: str) -> str:
    row = conn.execute('SELECT name FROM persons WHERE id=?', (pid,)).fetchone()
    name = row[0] if row else pid
    return f'{name} [{fmt_id_display(pid)}]'


# ── Section 0: Discoveries since last session ────────────────────────────────

def _section_discoveries(conn, prev: dict, current: dict) -> list[str]:
    lines: list[str] = []

    prev_claim_status = prev.get('claim_status_by_id', {})
    newly_accepted = sorted(
        cid for cid, status in current['claim_status_by_id'].items()
        if status == 'accepted' and prev_claim_status.get(cid) == 'needs-review'
    )
    if newly_accepted:
        lines.append('**Claims newly accepted (were needs-review):**')
        for cid in newly_accepted:
            row = conn.execute(
                'SELECT source_id, type, value FROM claims WHERE id=?', (cid,)
            ).fetchone()
            if row:
                lines.append(
                    f"- {fmt_id_display(cid)} ({row['type']}: {row['value']}) "
                    f"- [{fmt_id_display(row['source_id'])}]"
                )

    prev_links = {tuple(x) for x in prev.get('claim_links', [])}
    cur_links = {tuple(x) for x in current['claim_links']}
    new_corrob = sorted(t for t in (cur_links - prev_links) if t[1] == 'corroborates')
    if new_corrob:
        lines.append('**New corroboration links:**')
        for cid, _rel, target in new_corrob:
            lines.append(f'- {fmt_id_display(cid)} corroborates {fmt_id_display(target)}')

    prev_q = prev.get('question_status_by_heading', {})
    cur_q = current['question_status_by_heading']

    def _q_prev_status(key: str) -> str:
        # Keys are '{file} :: {heading}' (see parse_questions); a snapshot
        # written before the namespacing keyed by bare heading, so fall back
        # to it - otherwise every already-answered question would re-announce
        # as "newly answered" once after a tools update.
        return prev_q.get(key) or prev_q.get(key.split(' :: ', 1)[-1], '')

    newly_answered = sorted(
        h for h, status in cur_q.items()
        if status.startswith('answered') and not _q_prev_status(h).startswith('answered')
    )
    if newly_answered:
        # Display the plain heading; when the same heading text exists in more
        # than one file (the case the namespaced keys preserve), append the
        # file so the two lines stay tellable-apart.
        heading_counts: dict[str, int] = {}
        for k in cur_q:
            plain = k.split(' :: ', 1)[-1]
            heading_counts[plain] = heading_counts.get(plain, 0) + 1
        lines.append('**Questions newly answered:**')
        for h in newly_answered:
            plain = h.split(' :: ', 1)[-1]
            label = plain if heading_counts.get(plain, 1) == 1 else f'{plain} ({h.split(" :: ", 1)[0]})'
            lines.append(f'- {label} - {cur_q[h]}')

    prev_gaps = set(prev.get('vitals_gap_person_ids', []))
    cur_gaps = set(current['vitals_gap_person_ids'])
    newly_complete = sorted(prev_gaps - cur_gaps)
    if newly_complete:
        lines.append('**Profiles newly vital-complete:**')
        for pid in newly_complete:
            lines.append(f'- {_person_label(conn, pid)}')

    prev_rels = {tuple(x) for x in prev.get('relationships', [])}
    cur_rels = {tuple(x) for x in current['relationships']}
    seen_pairs: set[tuple[str, str, str]] = set()
    confirmed: list[tuple[str, str, str]] = []
    for a, rel, b in sorted(cur_rels - prev_rels):
        key = tuple(sorted((a, b))) + (rel,)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        confirmed.append((a, rel, b))
    if confirmed:
        lines.append('**Confirmed connections:**')
        for a, rel, b in confirmed:
            lines.append(f'- {_person_label(conn, a)} - {rel} - {_person_label(conn, b)}')

    # Contradictions (E009) that no longer fire this session.  A resolution
    # logged as a new open question referencing both claim-ids (rather than a
    # claim_links/status change) wouldn't otherwise surface anywhere above.
    prev_e009 = set(prev.get('e009_messages', []))
    cur_e009 = set(current.get('e009_messages', []))
    resolved_e009 = sorted(prev_e009 - cur_e009)
    if resolved_e009:
        lines.append('**Contradictions resolved (no longer flagged):**')
        for msg in resolved_e009:
            lines.append(f'- {msg}')

    return lines or ['No discoveries since last session.']


# ── Section 1: Review queue (W102) ────────────────────────────────────────────

def _section_review_queue(conn) -> list[str]:
    sources = conn.execute(
        '''
        SELECT s.id AS sid, s.title, MIN(c.date_min) AS oldest
        FROM claims c JOIN sources s ON s.id = c.source_id
        WHERE c.status = 'suggested'
        GROUP BY s.id
        ORDER BY oldest ASC, s.id ASC
        '''
    ).fetchall()
    if not sources:
        return ['No suggested claims awaiting review.']

    lines: list[str] = []
    for row in sources:
        claims = conn.execute(
            "SELECT id, type, value FROM claims WHERE source_id=? AND status='suggested' "
            'ORDER BY date_min',
            (row['sid'],),
        ).fetchall()
        lines.append(
            f"- {row['title']} [{fmt_id_display(row['sid'])}] - {len(claims)} suggested claim(s)"
        )
        for c in claims:
            lines.append(f"    - {fmt_id_display(c['id'])} {c['type']}: {c['value']}")
    return lines


# ── Section 1b: Worth a second look ──────────────────────────────────────────

_SECOND_LOOK_SHOWN = 3   # oldest few per bucket - a briefing, not a backlog dump


def _second_look_person(conn, cid: str) -> str:
    row = conn.execute(
        'SELECT person_id FROM claim_persons WHERE claim_id = ? ORDER BY position LIMIT 1',
        (cid,)).fetchone()
    return _person_label(conn, row['person_id']) if row else '(no person linked)'


def _section_second_look(conn) -> list[str]:
    """Claims worth revisiting someday, in two buckets (owner decision
    2026-07-22): parked needs-review claims (looked at, could not settle -
    the SPEC §8.1 parked verdict; the reviewed: date says when) and accepted
    claims with low confidence (built on, but ripe for superseding when a
    better record turns up - SPEC §8.5 keeps status and confidence
    orthogonal). Counts plus the oldest few only, honoring the report's
    no-flood rule (§15a) - ranking the full set into concrete leads is
    research-next's job, and none of this is a defect list (§14a
    alarm-blindness: these are leads, not errors).
    """
    lines: list[str] = []

    parked = conn.execute(
        "SELECT id, type, value, reviewed FROM claims WHERE status = 'needs-review' "
        "ORDER BY CASE WHEN reviewed IS NULL OR reviewed = '' THEN 1 ELSE 0 END, reviewed ASC, id ASC",
    ).fetchall()
    if parked:
        lines.append(
            f'**Parked ({len(parked)}):** looked at, not settled yet - '
            'these resurface when a new source corroborates or contradicts them.'
        )
        for c in parked[:_SECOND_LOOK_SHOWN]:
            when = f"parked {c['reviewed']}" if c['reviewed'] else 'no review date'
            lines.append(
                f"- {_second_look_person(conn, c['id'])}: {c['type']}: {c['value']} ({when})")
        if len(parked) > _SECOND_LOOK_SHOWN:
            lines.append(f'- … and {len(parked) - _SECOND_LOOK_SHOWN} more')

    thin = conn.execute(
        "SELECT id, type, value, reviewed FROM claims "
        "WHERE status = 'accepted' AND confidence = 'low' "
        "ORDER BY CASE WHEN reviewed IS NULL OR reviewed = '' THEN 1 ELSE 0 END, reviewed ASC, id ASC",
    ).fetchall()
    if thin:
        lines.append(
            f'**Accepted on thin evidence ({len(thin)}):** low-confidence facts you are '
            'building on - each is a lead for corroboration, and a better record would '
            'supersede it.'
        )
        for c in thin[:_SECOND_LOOK_SHOWN]:
            lines.append(
                f"- {_second_look_person(conn, c['id'])}: {c['type']}: {c['value']}")
        if len(thin) > _SECOND_LOOK_SHOWN:
            lines.append(f'- … and {len(thin) - _SECOND_LOOK_SHOWN} more')

    return lines or ['Nothing waiting on a second look.']


# ── Section 2: New since last session ────────────────────────────────────────

def _section_new_since_last(prev: dict, current: dict) -> list[str]:
    new_sources = sorted(set(current['source_ids']) - set(prev.get('source_ids', [])))
    new_persons = sorted(set(current['person_ids']) - set(prev.get('person_ids', [])))
    new_claims = sorted(set(current['claim_ids']) - set(prev.get('claim_ids', [])))
    prev_claim_fingerprints = prev.get('claim_fingerprints', {})
    changed_claims = sorted(
        cid for cid, fingerprint in current['claim_fingerprints'].items()
        if cid in prev_claim_fingerprints and prev_claim_fingerprints[cid] != fingerprint
    )
    prev_source_fingerprints = prev.get('source_fingerprints', {})
    changed_sources = sorted(
        sid for sid, fingerprint in current['source_fingerprints'].items()
        if sid in prev_source_fingerprints and prev_source_fingerprints[sid] != fingerprint
    )
    prev_person_fingerprints = prev.get('person_fingerprints', {})
    changed_persons = sorted(
        pid for pid, fingerprint in current['person_fingerprints'].items()
        if pid in prev_person_fingerprints and prev_person_fingerprints[pid] != fingerprint
    )
    if (
        not new_sources and not new_persons and not new_claims and not changed_claims
        and not changed_sources and not changed_persons
    ):
        return ['No new sources or persons since last session.']

    lines: list[str] = []
    if new_sources:
        lines.append(
            f'**New sources ({len(new_sources)}):** '
            + ', '.join(fmt_id_display(s) for s in new_sources)
        )
    if new_persons:
        lines.append(
            f'**New persons ({len(new_persons)}):** '
            + ', '.join(fmt_id_display(p) for p in new_persons)
        )
    if new_claims:
        lines.append(
            f'**New claims ({len(new_claims)}):** '
            + ', '.join(fmt_id_display(c) for c in new_claims)
        )
    if changed_claims:
        lines.append(
            f'**Changed claims ({len(changed_claims)}):** '
            + ', '.join(fmt_id_display(c) for c in changed_claims)
        )
    if changed_sources:
        lines.append(
            f'**Changed sources ({len(changed_sources)}):** '
            + ', '.join(fmt_id_display(s) for s in changed_sources)
        )
    if changed_persons:
        lines.append(
            f'**Changed persons ({len(changed_persons)}):** '
            + ', '.join(fmt_id_display(p) for p in changed_persons)
        )
    return lines


# ── Section 3 / 4: Vitals gaps (W101) / Contradictions (E009) ────────────────

_W101_PID_RE = re.compile(r'\b(P-[0-9a-hjkmnp-tv-z]{10})\b', re.I)


def _section_vitals_gaps(findings: list, registry) -> list[str]:
    """
    Format lint W101 in the report's promised order: curated profiles first,
    then any non-curated/touched records if a future lint pass emits them.
    """
    def sort_key(f) -> tuple[int, str]:
        pid_m = _W101_PID_RE.search(f.message)
        pid = normalize_id(pid_m.group(1)) if pid_m else ''
        tier = str(registry.person_meta.get(pid, {}).get('tier', '')).lower()
        return (0 if tier == 'curated' else 1, f.message)

    w101 = sorted((f for f in findings if f.code == 'W101'), key=sort_key)
    if not w101:
        return ['No vitals gaps for curated persons.']
    return [f'- {f.message}' for f in w101]


def _section_contradictions(findings: list) -> list[str]:
    e009 = sorted((f for f in findings if f.code == 'E009'), key=lambda f: f.message)
    if not e009:
        return ['No unresolved contradictions.']
    return [f'- {f.message}' for f in e009]


# ── Section 5: Search-log awareness ───────────────────────────────────────────

def _section_search_log(conn, current: dict) -> list[str]:
    """
    Annotate leads from the other sections with prior search_log activity,
    then call out recent `fha capture` pages that aren't tied to any lead.

    Leads = persons with a vitals gap, a suggested-claim backlog (review
    queue), or a contradiction - the same person sets the other sections
    already surfaced, gathered here rather than threading lead lists between
    section functions.

    Capture rows always carry `person_id IS NULL` (TOOLING §13b: a stub
    hasn't been reconciled to a person yet), so they can never match a lead
    above by construction. Without a separate call-out they'd be invisible
    here even though they're sitting durably in search_log - listing the
    recent ones (capped to a short window so this doesn't grow into a
    permanent unread backlog) at least keeps them in view until `fha
    process` resolves the stub into a real record.
    """
    lead_pids: set[str] = set(current['vitals_gap_person_ids'])
    lead_pids.update(
        row[0] for row in conn.execute(
            "SELECT DISTINCT cp.person_id FROM claim_persons cp "
            "JOIN claims c ON c.id = cp.claim_id WHERE c.status = 'suggested'"
        )
    )
    lead_pids.update(
        row[0] for row in conn.execute(
            "SELECT DISTINCT cp.person_id FROM claim_links cl "
            "JOIN claim_persons cp ON cp.claim_id = cl.claim_id WHERE cl.rel = 'contradicts'"
        )
    )

    lines: list[str] = []
    if lead_pids:
        horizon = datetime.date.today() - datetime.timedelta(days=_SEARCH_LOG_HORIZON_DAYS)
        for pid in sorted(lead_pids):
            rows = conn.execute(
                'SELECT date, collection, repository, result FROM search_log WHERE person_id=? ORDER BY date DESC',
                (pid,),
            ).fetchall()
            if not rows:
                continue
            label = _person_label(conn, pid)
            for row in rows:
                try:
                    stale = datetime.date.fromisoformat(row['date']) < horizon
                except (TypeError, ValueError):
                    stale = False
                result = str(row['result'] or '').strip().lower()
                nil_result = result in {'nil', 'none', 'no results', 'not found', 'negative'}
                note = (
                    'worth re-running (stale nil search)'
                    if stale and nil_result
                    else f"already searched {row['date']}"
                )
                collection = row['collection'] or row['repository'] or '(unspecified collection)'
                lines.append(f'- {label} - {collection}: {note}')

    recency = datetime.date.today() - datetime.timedelta(days=_CAPTURE_RECENCY_DAYS)
    # notes/research-log.md entries are also person_id IS NULL (general/locality
    # searches aren't person-scoped) but aren't inbox captures - `fha capture`
    # is the only writer that stamps result `staged {path}` (capture.py:768),
    # so that prefix is what actually distinguishes a capture row here.
    captured = conn.execute(
        "SELECT date, question, repository, collection FROM search_log "
        "WHERE person_id IS NULL AND result LIKE 'staged %' ORDER BY date DESC LIMIT 20"
    ).fetchall()
    capture_lines = []
    for row in captured:
        try:
            recent = datetime.date.fromisoformat(row['date']) >= recency
        except (TypeError, ValueError):
            recent = False
        if not recent:
            continue
        collection = row['collection'] or row['repository'] or '(unspecified collection)'
        capture_lines.append(f"- {row['date']} - {collection}: {row['question']}")
    if capture_lines:
        if lines:
            lines.append('')
        lines.append('Recently captured (not yet linked to a person):')
        lines.extend(capture_lines)

    return lines or ['No matching search-log entries for current leads.']


# ── Section 5b: Answerable questions ──────────────────────────────────────────

# Vitals-gap closure (the P-id branch below) only makes sense for a question
# that is actually *about* birth/marriage/death - a question referencing the
# same person but asking about something else entirely (immigration date,
# residence, parentage, an alias) must not be proposed-closed just because
# that person's vitals later filled in.  Keyed on the same vocabulary as the
# `needed` vitals set so a match always lines up with what was just verified.
_VITALS_QUESTION_KEYWORDS = {
    'birth': ('born', 'birth', 'baptism', 'baptized', 'christened'),
    'marriage': ('marry', 'marri', 'wed', 'spouse', 'husband', 'wife'),
    'death': ('died', 'death', 'buried', 'burial', 'death certificate'),
}
# Generic vitals-completeness phrasing ("fully documented", "vitals gap")
# doesn't name a specific vital but is still clearly about the same closure
# this section proposes, unlike a question about immigration, residence, or
# parentage that merely happens to reference the person.
_VITALS_GENERIC_KEYWORDS = ('fully documented', 'vitals', 'vital record', 'documented?')


def _question_vitals_subset(heading: str, block: str, needed: set[str]) -> set[str]:
    """
    Return the subset of `needed` that the question text specifically names.

    If the question uses generic vitals-completeness phrasing ("fully
    documented", "vitals gap") rather than naming a specific vital, the
    question is genuinely about the full `needed` set, so the full set is
    returned in that case. Otherwise only the specifically-named vital(s)
    come back - a question that only asks "When was X born?" must not wait
    on an unrelated marriage/death gap before a closure is proposed.
    """
    text = f'{heading}\n{block}'.lower()
    if any(kw in text for kw in _VITALS_GENERIC_KEYWORDS):
        return set(needed)
    return {
        vital for vital in needed
        if any(kw in text for kw in _VITALS_QUESTION_KEYWORDS.get(vital, ()))
    }


def _section_answerable_questions(conn, archive_root: Path) -> list[str]:
    """
    Open questions whose referenced gap now has an accepted claim, or whose
    referenced C-id changed status - proposed only, never executed (TOOLING
    §15a: closing requires human confirmation).
    """
    questions = parse_questions(archive_root)
    open_qs = {h: info for h, info in questions.items() if info['status'] == 'open'}
    if not open_qs:
        return ['No open questions.']

    # When the same heading text is open in more than one file, suffix the
    # file so the proposal lines stay tellable-apart (headings recur across
    # research sheets once questions number in the hundreds).
    heading_counts: dict[str, int] = {}
    for info in open_qs.values():
        heading_counts[info['heading']] = heading_counts.get(info['heading'], 0) + 1

    lines: list[str] = []
    for _key, info in sorted(open_qs.items()):
        heading = info['heading']
        if heading_counts[heading] > 1:
            heading = f"{heading} ({info['file']})"
        proposal = None
        for cid in (r for r in info['refs'] if r.startswith('c-')):
            row = conn.execute('SELECT status, source_id FROM claims WHERE id=?', (cid,)).fetchone()
            if row and row['status'] == 'accepted':
                proposal = (
                    f'propose: answered [{fmt_id_display(row["source_id"])}] '
                    f'(claim {fmt_id_display(cid)} now accepted)'
                )
                break
        if not proposal:
            for pid in (r for r in info['refs'] if r.startswith('p-')):
                accepted_claims = conn.execute(
                    "SELECT c.type, c.negated FROM claims c "
                    "JOIN claim_persons cp ON cp.claim_id = c.id "
                    "WHERE cp.person_id=? AND c.status='accepted'",
                    (pid,),
                ).fetchall()
                claim_types = {r['type'] for r in accepted_claims}
                negated_marriage = any(
                    r['type'] == 'marriage' and r['negated'] in (1, True, 'true')
                    for r in accepted_claims
                )
                person_row = conn.execute(
                    'SELECT living, no_known_marriages FROM persons WHERE id=?', (pid,)
                ).fetchone()

                # Mirror lint.py's W101 vitals-gap rule exactly (lint.py
                # "W101: vitals gaps for curated people") so this section never
                # proposes a closure lint itself wouldn't consider complete.
                needed = {'birth'}
                no_known_marriages = bool(person_row) and person_row['no_known_marriages'] in (1, True, 'true')
                if not no_known_marriages and not negated_marriage:
                    needed.add('marriage')
                living = str(person_row['living']) if person_row else 'unknown'
                if living not in ('true', 'unknown'):
                    needed.add('death')
                mentioned = _question_vitals_subset(heading, info['block'], needed)
                if mentioned and mentioned.issubset(claim_types):
                    proposal = (
                        f'propose: review - {_person_label(conn, pid)} now has accepted '
                        f'{", ".join(sorted(mentioned))} claim(s)'
                    )
                    break
        if proposal:
            lines.append(f'- {heading}: {proposal} (human confirmation required)')

    return lines or ['No open question currently has a closing proposal.']


# ── Section 6: Photo processing triage ────────────────────────────────────────

# `fha photoindex reconcile` keeps a vanished photo's catalog row under the
# synthetic key 'MISSING:' + its last known path, so the caption and dates it
# carried outlive the file. photoindex.py owns the rule; restated here (report
# only ever displays these paths, never opens them) so a suggestion the human
# is meant to type is never built from a path that does not exist.
_MISSING_PREFIX = 'MISSING:'


def _live_alias(path: str) -> str:
    """The path a cached photo key names, with any 'MISSING:' prefix off."""
    return path[len(_MISSING_PREFIX):] if path.startswith(_MISSING_PREFIX) else path


def _is_missing_key(path: str) -> bool:
    """True when a cached photo path is reconcile's synthetic missing-file key."""
    return path.startswith(_MISSING_PREFIX)


def _photo_scan_notes(data: dict) -> list[str]:
    """Turn one `photoindex.run_scan` payload into the §6 'what it could not see' lines.

    Section 6 is where the human reliably looks, so everything the scan
    reported as unseen has to arrive here - not only on the standalone
    command's stderr, which a session-start `fha report` never shows him. Each
    note names the thing, says what was NOT thrown away, and gives the one
    command to run afterwards.

    Three separate conditions, deliberately three separate notes: a file
    exiftool could not read, a photo folder that would not list, and a folder
    of source records that would not list are different faults with different
    fixes, and folding them into one line would send him to the wrong one.
    Every key is read defensively - an older `.cache` payload or a partially
    populated summary must degrade to fewer notes, never to a KeyError inside
    the session-start feed.
    """
    notes: list[str] = []

    # A folder the walk could not open. First, because it is the one whose
    # consequence (rows kept rather than swept) is the least obvious.
    dirs = data.get('unreadable_dirs') or []
    if dirs:
        shown = ', '.join(dirs[:5])
        if len(dirs) > 5:
            shown += f' and {len(dirs) - 5} more'
        held = int(data.get('held_unreadable') or 0)
        kept = (
            f'The {held} photo(s) already catalogued from there were kept, not '
            'treated as deleted'
            if held else
            'Nothing was removed from the catalog for them'
        )
        notes.append(
            f'{len(dirs)} photo folder(s) could not be opened, so this scan did '
            f'not see what is inside them: {shown}. {kept}, and the photo '
            'catalog stays marked out of date. This is usually a folder whose '
            'permissions changed, or a drive or network share that is not '
            'connected - reconnect it (or restore your access), then run '
            '`fha photoindex` again'
        )

    # A folder of source RECORDS the scan could not read: the photos are all
    # there, but the archive's own statement of who is in them was unreadable.
    record_dirs = data.get('unreadable_record_dirs') or []
    if record_dirs:
        shown = ', '.join(record_dirs[:5])
        if len(record_dirs) > 5:
            shown += f' and {len(record_dirs) - 5} more'
        notes.append(
            f'{len(record_dirs)} folder(s) of source records could not be opened, '
            f'so this scan could not check which people your sources say are in '
            f'each photo: {shown}. The people already recorded for those photos '
            'were left exactly as they were, and the photo catalog stays marked '
            'out of date. Restore your access to the folder (or reconnect the '
            'drive it is on), then run `fha photoindex` again'
        )

    # A file exiftool could not read (#34) - skipped, prior row kept.
    n_unreadable = int(data.get('unreadable') or 0)
    if n_unreadable:
        sample = ', '.join(data.get('unreadable_sample') or [])
        note = (
            f'{n_unreadable} photo file(s) could not be read by exiftool and were '
            f'skipped (any prior catalog entry kept): {sample}'
        )
        # Those files never reached the catalog, so run_scan deliberately
        # holds the catalog's date back until they can be read - which is
        # why `fha find` will keep calling the photo index out of date.
        # Unexplained, that reads as a second, separate fault the human
        # cannot clear, so the report says it here, where he is looking.
        n_unindexed = int(data.get('unreadable_unindexed') or 0)
        if n_unindexed:
            note += (
                f'. The photo catalog stays marked out of date until those '
                f'{n_unindexed} file(s) can be read - close anything holding them '
                'open (or restore them from backup if they are damaged), then run '
                '`fha photoindex` again'
            )
        notes.append(note)

    return notes


def _section_photo_triage(
    archive_root: Path, fha_config: dict, scan_error: str | None = None,
    scan_notes: list[str] | None = None,
) -> list[str]:
    if is_working_copy(archive_root):
        return [
            'Photo triage is paused in working-copy mode because the photo files '
            'are on the main machine. Run `fha photoindex` on the main archive, '
            'or copy an existing .cache/photos.sqlite here for read-only photo queries.'
        ]
    if scan_error:
        return [
            f'Photo scan failed this session ({scan_error}) - triage results below may be '
            'stale; run `fha photoindex` once the issue is fixed.'
        ]
    # The scan's notes are prepended before the index verdict, not after: when
    # this session's scan could not read some files, that is very often WHY
    # the catalog below is missing or out of date, and printing the verdict
    # alone would send the human to re-run the command that just told him.
    lines = [f'Note: {n}' for n in (scan_notes or [])]
    result = photoindex.run_triage(archive_root, fha_config, top=10)
    if result['status'] in ('absent', 'unreadable'):
        return lines + [
            f'Photo index {result["status"]} - run `fha photoindex` to enable triage.'
        ]

    candidates = result['candidates']
    if not candidates:
        return lines + ['No unprocessed photo groups found.']

    for c in candidates:
        signals = ', '.join(c['signals']) if c['signals'] else 'no signals'
        # A group is named by whichever file the last scan picked as its
        # primary, and `fha photoindex reconcile` re-keys a vanished file
        # 'MISSING:' + its old path without choosing a new primary. Sending
        # the human to `fha process MISSING:photos/…` would be a dead end
        # twice over: that is not a path, and the file it names is not there.
        if _is_missing_key(c['path']):
            lines.append(
                f"- {_live_alias(c['path'])}  score={c['score']:+d}  [{signals}] - "
                'this photo is in the catalog but not on disk. Put it back, then run '
                '`fha photoindex reconcile --with-exif` before processing it.'
            )
            continue
        # `c['path']` is a real on-disk filename, not a controlled value - it
        # can carry a space ("Family Reunion 1962.jpg") or worse, so it needs
        # the same shell-safe quoting as §6b's `fha confirm place` command
        # (see that section's docstring); unquoted, the shell would split it
        # into multiple arguments and `fha process` would see a path that
        # does not exist.
        lines.append(
            f"- {c['path']}  score={c['score']:+d}  [{signals}] - "
            f"suggested: fha process {shell_quote(c['path'])}"
        )
    return lines


# ── Section 6b: Place candidates ──────────────────────────────────────────────

def _place_text_group_line(archive_root: Path, g: dict, match: dict | None = None) -> str:
    """
    One place-text cluster's report line: label, claim count, date spread,
    and the exact `fha confirm place` command that clears it (issue #79
    point 1) - every claim id in `g['claim_ids']`, not a sample, so running
    the line once clears the whole cluster. Extracted so `_section_place_
    candidates` (§6b's own listing) and `_place_text_escalations` (the
    oversight-threshold notice, issue #79 point 2) render the identical line
    for the identical cluster - the two can never disagree about what a
    cluster is called or what command resolves it.

    Before recommending a mint, checks the cluster's label against the
    ALREADY-registered places (`_lib.match_place_text_to_registry`, the same
    exact/near normalization - `place_text_cluster_key`/`normalize_place_text`
    - the claim-write-time resolver from issue #79 point 3 uses). A cluster
    can legitimately still be unlinked even though its place IS registered:
    point 3's auto-attach only runs at claim-write time and only on an
    'exact' match, so (a) any claim drafted before that landed, and (b) any
    'near' match (word order/abbreviation - point 3 deliberately never
    auto-attaches those), both stay sitting on bare `place_text` forever. A
    banner or listing that always suggests `--name` in that state walks the
    human straight into minting a duplicate `L-id` for a place that already
    has one (Codex review, PR #142 finding 1) - so a match here recommends
    `fha confirm place ... --into <existing L-id>` instead, and `--name` is
    offered only once no registered place matches at all.

    A `tier: None` result also covers a SECOND, distinct situation that
    `match_place_text_to_registry` deliberately collapses to the same tier:
    the cluster's label ties between two or more already-registered
    place_ids (a PL002 duplicate-name registry problem in its own right).
    Falling through to the `--name` mint recommendation there would invite
    the human to create a THIRD, duplicate place_id instead of resolving the
    clash (Codex review, PR #142 finding 2 follow-up) - so this checks
    `match['ambiguous_ids']` (the real tied ids the match reported, never
    guessed at here) and names the clash plus `fha places lint` as the way
    to see it, instead. `--name` is now offered only once BOTH `tier` is
    `None` AND `ambiguous_ids` is empty - i.e. truly no registered place
    matches at all, ambiguous or otherwise.

    `match` lets a caller that already looked the cluster's label up pass
    that result in, instead of this function repeating the same registry
    read a second time - `run_report`'s escalation-banner renderer does
    exactly that (see `_place_text_escalations`'s docstring and the finding-1
    fix in `_render_report`), since it also needs to know whether any
    escalated cluster already has a registry match to word its own banner
    accurately. Omitted (the §6b listing's own call), it is looked up here
    exactly as before.
    """
    spread = f"{g['date_min']}/{g['date_max']}" if g['date_min'] or g['date_max'] else 'no dates'
    name = g['label']
    ids = ' '.join(fmt_id_display(cid) for cid in g['claim_ids'])
    if match is None:
        match = match_place_text_to_registry(archive_root, name)
    if match['tier'] and match['place_id']:
        tier_word = 'matches' if match['tier'] == 'exact' else 'near-matches'
        return (
            f"{name} - {g['claim_count']} claim(s), {spread} - "
            f"{tier_word} the already-registered {match['name']!r} "
            f"({match['place_id']}) - link with "
            f"`fha confirm place {ids} --into={match['place_id']}`"
        )
    ambiguous_ids = match.get('ambiguous_ids')
    if ambiguous_ids:
        return (
            f"{name} - {g['claim_count']} claim(s), {spread} - "
            f"matches MULTIPLE registered places ({', '.join(ambiguous_ids)}) - "
            f"pick one with `fha confirm place {ids} --into=<one of the above>`, "
            'or run `fha places lint` to see the clash'
        )
    return (
        f"{name} - {g['claim_count']} claim(s), {spread} - "
        f'register with `fha confirm place {ids} --name={shell_quote(name)}`'
    )


def _fetch_place_candidates(archive_root: Path, fha_config: dict) -> tuple[dict | None, str | None]:
    """
    The one `places.run_candidates()` call a report run makes (BUILD.md
    M6.2), shared by both `_section_place_candidates` (§6b's own listing)
    and `_place_text_escalations` (the oversight-threshold banner, issue #79
    point 2).

    Before this existed, the two called `run_candidates()` independently -
    on every report run with escalations enabled, that meant `_gps_clusters`'
    full photo-index read and greedy-clustering pass ran TWICE for no reason
    (the escalation feature only ever needs the place-text half), and any
    stale-photo-index warning printed twice in the same report (Codex
    review, PR #142 finding 2). One fetch, passed to both, fixes both.

    Returns `(candidates_data, error_line)`: on success, `candidates_data`
    is `run_candidates()`'s `.data` dict (`place_text_groups`/`gps_clusters`/
    the legacy flat `groups`) and `error_line` is `None`; on a degraded
    tools install, `candidates_data` is `None` and `error_line` is the exact
    message §6b prints for that failure. The import/attribute guards stay in
    place as a defensive fallback rather than a hard dependency - every
    other optional embed in this file (photoindex, cooccur) degrades the
    same way instead of raising.
    """
    try:
        import places as _places_tool   # noqa: PLC0415 - optional embed, see docstring
    except ImportError:
        return None, ('`fha places` could not be loaded (tools/places.py missing or damaged) - '
                       'section skipped. Run `fha update-tools` to restore it.')
    try:
        result = _places_tool.run_candidates(archive_root, fha_config)
    except AttributeError:
        return None, ('`fha places` is out of date (no candidates engine) - section skipped. '
                       'Run `fha update-tools` to refresh the tools.')
    return result.data, None


def _section_place_candidates(
    archive_root: Path, candidates: dict | None, error: str | None,
) -> list[str]:
    """
    Renders `_fetch_place_candidates`'s result.

    Issue #79 point 1: every place-text cluster line now grows a ready-to-run
    `fha confirm place` command instead of just describing the cluster - this
    section was the one place in the report that named a problem and stopped,
    unlike its siblings (§7b's `fha person promote`, §8's confirm/dismiss
    affordances). The claim ids and the proposed name come straight off
    `place_text_groups` - the same majority-vote `label` and `claim_ids` that
    `fha places candidates` itself prints - reused rather than re-derived, so
    this line can never disagree with what running that command by hand would
    show. Every claim id in the cluster goes into the command (not a sample),
    so copying the line clears the whole cluster in one run instead of
    leaving a shrunken candidate to surface again next session.

    GPS clusters keep their plain descriptive line: they are photo groups,
    not claims, so there is nothing for `fha confirm place` to relink - a
    fabricated verb for them would be the claim-write-time-resolution/
    first-run-flow work issue #79 explicitly defers (point 4), not this
    one. (Point 2, escalation on threshold, is `_place_text_escalations`
    below - no longer deferred.)

    `label` is a claim's free-text `place_text`, not a controlled value like
    §7b's person id or a claim/place id elsewhere in this file, so it can
    legitimately contain a `"`, a leading `-`, or a literal space. Splicing
    it into `--name "{label}"` breaks the printed command's quoting (or
    worse, is unsafe to paste into a shell) exactly when the record text is
    quoting something itself, e.g. a place named off a deed as `The "Old
    Manse"`. `--name=` plus `_lib.shell_quote` keeps
    the value a single argv token no matter its contents - including a
    leading `-` that would otherwise make argparse mistake the value for
    another flag - on POSIX and on Windows alike (see `shell_quote`'s own
    docstring: a plain double-quote wrap is not enough there, per the
    `claim.py`/issue #54 precedent of this exact bug shape).
    """
    if error:
        return [error]

    place_text_groups = candidates.get('place_text_groups')
    gps_clusters = candidates.get('gps_clusters')
    if place_text_groups is None and gps_clusters is None:
        # A places.py old enough to predate the structured keys (only the
        # flat pre-formatted `groups` list existed before) - same
        # degrade-not-crash posture as the error guard above: an out-of-date
        # tool loses the call-to-action line instead of crashing the report.
        groups = candidates.get('groups') or []
        if not groups:
            return ['No recurring unlinked place-text or GPS clusters found.']
        return [f"- {g}" for g in groups]

    place_text_groups = place_text_groups or []
    gps_clusters = gps_clusters or []
    if not place_text_groups and not gps_clusters:
        return ['No recurring unlinked place-text or GPS clusters found.']

    lines: list[str] = []
    for g in place_text_groups:
        lines.append(f'- {_place_text_group_line(archive_root, g)}')
    for c in gps_clusters:
        lines.append(
            f"- GPS cluster near {c['lat']:.4f},{c['lon']:.4f} - "
            f"{c['photo_count']} photo(s), no known place nearby"
        )
    return lines


# ── Escalation: oversized place-text clusters (issue #79 point 2) ────────────

_PLACE_ESCALATION_THRESHOLD = 20
# The issue's own suggested cutoff (#79: "a cluster with 20+ claims is not a
# candidate, it is an oversight"). No other claims-per-cluster threshold
# exists in this codebase to weigh it against - `_PROMOTION_DEFAULT_THRESHOLD`
# (5) is the nearest analog but counts accepted claims piling up on ONE stub
# PERSON, a different scale of signal than claims sharing one unlinked place
# text across possibly many people - so the issue's own number stands; a
# human reviewer can always tune it in review.


def _place_text_escalations(candidates: dict | None) -> list[dict]:
    """
    §6b place-text clusters (`_fetch_place_candidates`'s `place_text_groups`)
    that have reached oversight scale.

    Issue #79's motivating case was a 13-month-old archive with 359 of 762
    claims (47%) carrying `place_text` and ZERO registered places - #6b lists
    every recurring cluster as a "maybe register this" candidate, but a
    cluster this large was never a judgment call competing for attention
    with the rest of that list; it is a standing gap someone already meant
    to close. This reads the exact same `place_text_groups` §6b lists from
    (never re-derives cluster membership - same discipline as the point-1
    fix, #79/#100) and narrows to the ones at or past
    `_PLACE_ESCALATION_THRESHOLD`, so an escalation can never name a cluster
    §6b itself would not also list.

    Takes `_fetch_place_candidates`'s already-computed result rather than
    calling `places.run_candidates()` itself (that used to be a second,
    independent fetch - Codex review, PR #142 finding 2) - degrades to `[]`
    when `candidates` is `None` (the same tools-degraded states §6b already
    explains via its own `error` line, so this bonus call-out does not
    repeat that explanation a second time in different words at the top of
    the report).
    """
    if not candidates:
        return []
    groups = candidates.get('place_text_groups') or []
    return [g for g in groups if g['claim_count'] >= _PLACE_ESCALATION_THRESHOLD]


# ── Section 7: Hypotheses & draft queues ──────────────────────────────────────

def _person_has_draft_queue_backlog(conn, archive_root: Path, person_id: str) -> bool:
    """
    True if `person_id` has ≥1 accepted-claim source not cited in their
    profile body - computed live from the index, mirroring exactly what
    `views.py`'s `_generate_draft_queue` does (accepted_sids - cited_sids),
    rather than reading the generated draft-queue file. The generated file
    can lag behind the index (claim just accepted, `fha views draft-queue`
    not yet re-run), which would make this section silently stale.
    """
    row = conn.execute(
        "SELECT path FROM person_files WHERE person_id=? AND kind='profile'",
        (person_id,),
    ).fetchone()
    if not row:
        return False
    try:
        rec = read_record(archive_root / row['path'])
    except OSError:
        return False
    body = rec['body']
    cited_sids = {
        tid for tid in extract_token_ids(body) if tid.startswith('s-')
    }
    accepted_sids = {
        normalize_id(r[0]) for r in conn.execute(
            "SELECT DISTINCT c.source_id FROM claim_persons cp "
            "JOIN claims c ON c.id = cp.claim_id "
            "WHERE cp.person_id=? AND c.status='accepted'",
            (person_id,),
        )
    }
    return bool(accepted_sids - cited_sids)


def _section_hypotheses(conn, archive_root: Path) -> list[str]:
    lines: list[str] = []

    open_hyps = conn.execute(
        "SELECT person_id, COUNT(*) AS n FROM hypotheses WHERE status='open' GROUP BY person_id"
    ).fetchall()
    if open_hyps:
        lines.append('**Open hypotheses:**')
        for row in open_hyps:
            lines.append(f"- {_person_label(conn, row['person_id'])} - {row['n']} open hypothesis/es")
    else:
        lines.append('No open hypotheses.')

    curated_pids = [
        r[0] for r in conn.execute("SELECT id FROM persons WHERE tier='curated'")
    ]
    backlog_pids = {
        pid for pid in curated_pids
        if _person_has_draft_queue_backlog(conn, archive_root, pid)
    }

    if backlog_pids:
        lines.append('**Draft-queue backlog:**')
        for pid in sorted(backlog_pids):
            lines.append(f'- {_person_label(conn, pid)} has uncited accepted claims pending')
    else:
        lines.append('No draft-queue backlog.')

    return lines


# ── Section 7b: Promotion candidates ──────────────────────────────────────────

_PROMOTION_SHOWN = 5   # top few per bucket - a briefing, not a backlog dump
_PROMOTION_DEFAULT_THRESHOLD = 5   # fha.yaml promotion.claims_threshold default


def _stub_person_rows(conn) -> dict[str, dict]:
    """Every not-yet-curated person in the index: {P-id: {'name','pos'?}}.

    "Stub" here means what the promote machinery means: `tier` is not curated,
    OR the record still lives under people/stubs/ (a half promotion - tier
    flipped by hand but never filed). Merged tombstones are excluded: they
    resolve through merged_into and are never promoted.
    """
    out: dict[str, dict] = {}
    for r in conn.execute('SELECT id, name, tier, status, path FROM persons'):
        if str(r['status'] or '').lower() == 'merged':
            continue
        parts = (r['path'] or '').replace('\\', '/').split('/')
        in_stubs = len(parts) >= 3 and parts[0] == 'people' and parts[1].lower() == 'stubs'
        if str(r['tier'] or 'stub').lower() != 'curated' or in_stubs:
            out[r['id']] = {'name': r['name'] or fmt_id_display(r['id'])}
    return out


def _section_promotion_candidates(conn, fha_config: dict) -> list[str]:
    """Stubs that have earned a real page - the report side of the promotion
    surface (`fha person promote` / `fha views brackets --fix-promote`).

    Two buckets, both stateless (computed live from the index, no snapshot):
      (a) the W119 set - direct-line ancestors (derived Ahnentafel position
          >= 2, via the shared `_lib.build_ahnentafel_map` over accepted
          relationship claims from `root_person`, exactly how brackets
          computes it - seeded at `root_person`'s fha.yaml `root_generation`
          position, #72) whose record is still a stub; closest generations
          first, each offering the promote verb;
      (b) any stub whose accepted-claim count has reached the threshold -
          the "Frank keeps turning up" signal that a person has earned
          curation regardless of line. The threshold reads from fha.yaml's
          `promotion:` block, key `claims_threshold`, DEFAULT 5:

              promotion:
                claims_threshold: 5

          A non-numeric value falls back to the default with a plain note.
          A non-direct claim-heavy stub is now offered the verb too (#80):
          `fha person promote <P-id> --into connections/` (SPEC §12.3), files
          them flat with no numbering - rather than the old dead-end note.

    Leads, never defects (§14a alarm-blindness): counts plus the top few,
    no flood, and nothing here writes or proposes an automatic write -
    promotion is always the human's explicit act.
    """
    # Shape-aware read: `promotion:` may legitimately be absent, a mapping
    # (the documented form), or - in a loosely hand-edited fha.yaml - a bare
    # scalar. A non-mapping must never crash the report; a numeric scalar is
    # leniently read as the threshold itself, anything else falls back with
    # the plain note below.
    promo = fha_config.get('promotion')
    if isinstance(promo, dict):
        raw_threshold = promo.get('claims_threshold', _PROMOTION_DEFAULT_THRESHOLD)
    elif promo is None:
        raw_threshold = _PROMOTION_DEFAULT_THRESHOLD
    else:
        raw_threshold = promo
    lines: list[str] = []
    try:
        threshold = int(raw_threshold)
    except (TypeError, ValueError):
        threshold = _PROMOTION_DEFAULT_THRESHOLD
        lines.append(
            f'(promotion.claims_threshold in fha.yaml is {raw_threshold!r}, not a '
            f'number - using the default {_PROMOTION_DEFAULT_THRESHOLD}.)')

    stubs = _stub_person_rows(conn)
    accepted_counts = {
        r['person_id']: r['n'] for r in conn.execute(
            "SELECT cp.person_id AS person_id, COUNT(DISTINCT c.id) AS n "
            "FROM claim_persons cp JOIN claims c ON c.id = cp.claim_id "
            "WHERE c.status = 'accepted' GROUP BY cp.person_id"
        )
    }

    # Bucket (a): direct-line stubs, closest generations first.
    pid_to_pos: dict[str, int] = {}
    root_person_raw = fha_config.get('root_person')
    if root_person_raw:
        root_pid = normalize_id(str(root_person_raw))
        if conn.execute('SELECT id FROM persons WHERE id=?', (root_pid,)).fetchone():
            # root_generation (#72): which Ahnentafel slot root_person occupies
            # - #1 by default ('self'), #2 under 'children'. Degrades the same
            # way the claims_threshold fallback above does: a bad fha.yaml
            # value never crashes a report, but it is never silent either.
            try:
                root_generation = resolve_root_generation(fha_config)
            except FhaConfigError as e:
                lines.append(
                    f'({e} Direct-line stub leads are skipped until it is fixed.)')
            else:
                root_position = root_generation_seed_position(root_generation)
                pid_to_pos = build_ahnentafel_map(
                    conn, root_pid, root_position=root_position)
    direct = sorted(
        ((pid, pos) for pid, pos in pid_to_pos.items()
         if pos >= 2 and pid in stubs),
        key=lambda kv: kv[1])
    if direct:
        lines.append(
            f'**Direct-line ancestors still filed as stubs ({len(direct)}):** '
            'each has a place in the numbered folders waiting for them.')
        for pid, pos in direct[:_PROMOTION_SHOWN]:
            n = accepted_counts.get(pid, 0)
            claims_note = f', {n} accepted claim(s)' if n else ''
            lines.append(
                f'- {_person_label(conn, pid)} - Ahnentafel {pos}{claims_note} - '
                f'promote with `fha person promote {fmt_id_display(pid)}`')
        if len(direct) > _PROMOTION_SHOWN:
            lines.append(f'- … and {len(direct) - _PROMOTION_SHOWN} more '
                         '(`fha views brackets --fix-promote` previews the whole batch)')

    # Bucket (b): claim-heavy stubs beyond bucket (a).
    direct_pids = {pid for pid, _pos in direct}
    heavy = sorted(
        ((pid, n) for pid, n in accepted_counts.items()
         if n >= threshold and pid in stubs and pid not in direct_pids),
        key=lambda kv: (-kv[1], kv[0]))
    if heavy:
        lines.append(
            f'**Stubs that keep turning up ({len(heavy)} with {threshold}+ '
            'accepted claims):** no curated page yet, but the evidence is piling up.')
        for pid, n in heavy[:_PROMOTION_SHOWN]:
            # Direct-line stubs were routed to bucket (a) above, so everyone
            # here is off the line (or the line is underivable - no
            # root_person, in which case the command below still names the
            # right verb; `fha person promote` gives its own plain refusal
            # if root_person needs fixing first). #80: offer the verb.
            lines.append(f'- {_person_label(conn, pid)} has {n} accepted claims '
                         'and no curated profile - promote with '
                         f'`fha person promote {fmt_id_display(pid)} --into '
                         'connections/` (SPEC §12.3)')
        if len(heavy) > _PROMOTION_SHOWN:
            lines.append(f'- … and {len(heavy) - _PROMOTION_SHOWN} more')

    return lines or [
        'No promotion candidates - no direct-line ancestor is still a stub, '
        f'and no stub has {threshold} or more accepted claims.']


# ── Section 8: Possible connections (fha cooccur) ─────────────────────────────

def _section_possible_connections(archive_root: Path) -> list[str]:
    result = cooccur.run_cooccur(archive_root, threshold=2)
    if result['status'] != 'ok':
        return ['`fha cooccur` could not run - check .cache/index.sqlite.']

    lines: list[str] = []

    if result['migrated_legacy_dismissed']:
        # The one write `fha cooccur` (and so this report) can make on its
        # own (#48) - a housekeeping carry-forward of a human's earlier
        # decision, not a new one. Named here so a `today`-skill run never
        # narrates it as silent: the report promises no write without the
        # human seeing it, and this is the one exception that needs no
        # confirmation but still needs to be seen.
        lines.append(
            "_Moved this archive's earlier dismissed-pairs file from "
            '`.cache/cooccur_dismissed.json` to its durable home '
            '(`notes/cooccur_dismissed.json`) - a one-time housekeeping '
            'move, nothing to do on your end._'
        )

    pairs = result['person_pairs'][:10]
    if pairs:
        lines.append('**Person co-occurrence:**')
        for c in pairs:
            lines.append(
                f"- {c['name_a']} [{fmt_id_display(c['person_a'])}] <-> "
                f"{c['name_b']} [{fmt_id_display(c['person_b'])}] "
                f"- {c['source_count']} source(s)  [confirm] [dismiss]"
            )

    place_pairs = result['place_pairs'][:10]
    if place_pairs:
        lines.append('**Shared-place co-occurrence:**')
        for c in place_pairs:
            lines.append(
                f"- {c['name_a']} [{fmt_id_display(c['person_a'])}] <-> "
                f"{c['name_b']} [{fmt_id_display(c['person_b'])}] "
                f"@ {c['place_label']}  [confirm] [dismiss]"
            )

    org_groups = result['org_groups'][:10]
    if org_groups:
        lines.append('**Org/entity recurrence:**')
        for g in org_groups:
            lines.append(
                f"- {g['label']} [{g['category']}] - "
                f"{g['person_count']} people, {g['source_count']} sources"
            )

    return lines or ['No candidate connections found.']


# ── Rendering / orchestration ──────────────────────────────────────────────────

def _render_report(
    generated: str,
    bodies: dict[str, list[str]],
    section_filter: str | None,
    archive_notes: list[str] | None = None,
    place_escalation_lines: list[str] | None = None,
    place_escalation_matches: list[dict] | None = None,
) -> str:
    """Assemble the report markdown: title, archive notes (when any), sections.

    `archive_notes` are the refresh's own warnings (build_index's
    malformed-coords messages, an orphaning `roots:` change, and a record
    folder the rebuild could not open) - things the refresh skipped over. The
    last of those is why they matter most: while a folder stays shut the index
    reads stale forever, and without the reason printed here the human would
    be sent round the `fha index` loop with nothing to fix. They render right
    under the title, before any section,
    because the report IS the session-start path: a warning that only exists
    on the discarded Result is invisible exactly where the human looks first
    (round-2 finding 16). They print on section-filtered runs too - narrowing
    the view should never hide that a line of the archive was skipped.

    `place_escalation_lines` (issue #79 point 2, `_place_text_escalations`)
    are §6b place-text clusters that crossed the oversight-scale threshold -
    already rendered by `_place_text_group_line` (the caller does this, not
    here, because that render needs `archive_root` for the registry-match
    check finding 1 added, and this function stays archive-agnostic) - shown
    at the same above-every-section position as `archive_notes` and for the
    same reason: a cluster this large is not one more candidate that can
    afford to lose the "what should I do this session" contest by sitting
    quietly at position 10 of 13. Also printed on section-filtered runs,
    matching `archive_notes`' own rule - and the full cluster is still
    listed at its normal spot in §6b below, so nothing here shrinks that
    section's own listing.

    `place_escalation_matches` is `run_report`'s parallel list of each of
    those same clusters' `_lib.match_place_text_to_registry` result (the
    exact dicts `_place_text_group_line` used to decide `--into` vs
    `--name` for its own bullet) - here purely to word the banner's HEADING
    accurately. Before this existed the heading unconditionally said "no
    place registered", even for a cluster whose bullet, right below it,
    was busy recommending `--into <existing L-id>` because the label DOES
    match something already registered - self-contradictory, and a human
    skimming only the bold heading would still walk away minting a
    duplicate (Codex review, PR #142 finding 1). Bucketed by tier: any
    cluster with a real `tier` already has a registered place waiting to be
    linked; a `tier: None` cluster with `ambiguous_ids` matches more than
    one registered place and needs a human pick, not a mint; only a
    `tier: None` cluster with no `ambiguous_ids` either is a genuine miss.
    All-miss keeps the original "no place registered" wording (still
    accurate there); all-registered says so instead; anything mixed - or
    `None` (an older/direct caller that has not looked matches up) - falls
    back to a phrasing that is true of every cluster in the list either way,
    "not yet linked to a place", rather than asserting a state some of the
    clusters do not share.

    When `section_filter` narrows the view to something other than
    `place-candidates`, §6b itself is omitted from what actually prints, so
    pointing the banner at "the Place candidates section below" would name
    content the human cannot see in this run (Codex review, PR #142
    finding 3) - the banner instead names the exact follow-up command,
    `fha report --section place-candidates`."""
    lines = [f'# fha report - {generated}', '']
    if archive_notes:
        lines.append('**Archive notes from this refresh:**')
        lines.extend(f'- {note}' for note in archive_notes)
        lines.append('')
    if place_escalation_lines:
        if section_filter and section_filter != 'place-candidates':
            pointer = 'run `fha report --section place-candidates` to see every cluster'
        else:
            pointer = 'see the Place candidates section below for every cluster'
        matches = place_escalation_matches or []
        if matches and len(matches) == len(place_escalation_lines):
            registered = sum(1 for m in matches if m.get('tier'))
            ambiguous = sum(
                1 for m in matches if not m.get('tier') and m.get('ambiguous_ids'))
            genuine_miss = len(matches) - registered - ambiguous
            if registered == len(matches):
                state_phrase = 'already registered but not yet linked'
            elif ambiguous == len(matches):
                state_phrase = 'ambiguous - each matches multiple registered places'
            elif genuine_miss == len(matches):
                state_phrase = 'no place registered'
            else:
                state_phrase = 'not yet linked to a place'
        else:
            state_phrase = 'no place registered'
        lines.append(
            f'**{len(place_escalation_lines)} place-text cluster(s) past the '
            f'{_PLACE_ESCALATION_THRESHOLD}-claim oversight threshold, {state_phrase} '
            '- this is not a candidate to weigh, it is an oversight '
            f'to close ({pointer}):**'
        )
        lines.extend(f'- {line}' for line in place_escalation_lines)
        lines.append('')
    for key, number, title in SECTIONS:
        if section_filter and key != section_filter:
            continue
        lines.append(f'## {number}. {title}')
        lines.append('')
        lines.extend(bodies.get(key) or ['(no data)'])
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def run_report(
    archive_root: Path,
    fha_config: dict,
    full: bool = False,
    section: str | None = None,
) -> Result:
    """
    Run the full refresh -> diff -> render -> persist pipeline.

    Returns a `Result` whose `data` carries the report as data and as text:
      - data['status']:   'ok' (kept for back-compat; subscriptable via Result).
      - data['markdown']: the text to print this run - only the requested
        section when `section` is given.
      - data['full_markdown']: the complete report (what the snapshot/cache hold).
      - data['sections']: the per-section structured bodies (key -> list[str]),
        so a consumer can read each section as data, not just parsed text.
        Also carries a `'place-escalations'` entry (same list[str] shape,
        the escalated clusters' own rendered lines) alongside the numbered
        sections - it is not one of `SECTIONS`/`_SECTION_KEYS`, so it is
        never itself a `--section` filter target or its own `## N.` heading;
        it exists here purely so a consumer reading `data['sections']` sees
        the same escalation the markdown banner shows.
      - data['place_escalations']: the escalated clusters as raw structured
        dicts (`label`, `claim_count`, `claim_ids`, `date_min`, `date_max` -
        `_place_text_escalations`' own return shape, never re-derived), so a
        workbench or other headless consumer can tell a 20-claim oversight
        escalation apart from an ordinary §6b candidate without reparsing
        Markdown (Codex review, PR #142 finding 4 - previously this state
        only ever reached `_render_report`'s Markdown output).
    The persisted snapshot and `.cache/report_{date}.md` always hold the complete
    report - `--section` narrows what's printed this run, not what's recorded -
    and both written files are listed in `result.changed`.  `result.exit_code`
    follows the refresh lint pass (0/1/2); the index rebuild's own warnings
    surface as the markdown's archive-notes block and as `result.messages`,
    never in the exit code.  `result['markdown']` etc. work because
    Result exposes dict-style read access into `data` (_lib.py).

    Raises ValueError for an unknown `section` name.
    """
    if section is not None and section not in _SECTION_KEYS:
        raise ValueError(
            f'unknown --section {section!r}; choose one of: ' + ', '.join(sorted(_SECTION_KEYS))
        )

    # Refresh sequence (TOOLING §15a steps 1-3) - always incremental for
    # photos/index regardless of report's own --full (which only controls
    # whether the snapshot diff baseline is used, not how fresh the caches are).
    #
    # index.build_index runs *before* photoindex.run_scan: run_scan derives
    # its face-tag/name-variant photo-person matches from the current
    # .cache/index.sqlite (via _load_face_tag_index), so scanning against the
    # not-yet-rebuilt index would use a stale person/face-tag snapshot for
    # this cycle. Rebuilding first means run_scan always sees this session's
    # fresh data. photoindex.py's own staleness handling (_index_is_fresh)
    # already tolerates an index that lags behind - it just preserves
    # existing weak matches rather than failing - so this ordering is safe
    # either way; it's strictly an improvement.
    #
    # The build's Result is kept, not discarded: its messages (malformed
    # place coords a hand-edit produced) would otherwise be invisible on
    # this session-start path - `fha report` is the one place the human
    # reliably looks (round-2 finding 16). Each message text already names
    # the file, the bad line, and the fix.
    index_result = index.build_index(archive_root, fha_config)
    archive_notes = [m.text for m in index_result.messages]
    photo_scan_error: str | None = None
    photo_scan_notes: list[str] = []
    try:
        scan = photoindex.run_scan(archive_root, fha_config, full=False)
        photo_scan_notes = _photo_scan_notes(scan.data or {})
    except (RuntimeError, OSError) as e:
        # `fha report` is the session-start feed across many sections
        # (0-5b/7/8); a photo-scan failure (e.g. exiftool missing or
        # erroring) must not take the whole report down. Section 6 reports
        # the failure instead of silently looking clean.
        photo_scan_error = str(e)
    findings, registry = lint._run_lint_core(archive_root, fha_config)

    conn = open_index_db(
        archive_root,
        (
            'persons', 'sources', 'claims', 'claim_persons', 'claim_links',
            'relationships', 'hypotheses', 'person_files', 'search_log',
        ),
    )
    if conn is None:
        raise RuntimeError('index could not be opened after refresh')

    try:
        prev = {} if full else _load_snapshot(archive_root)
        current = _build_snapshot(conn, archive_root, findings, registry)

        # One `places.run_candidates()` fetch, shared by §6b's own listing
        # and the oversight-threshold escalation below it never re-derives
        # (Codex review, PR #142 finding 2 - two independent fetches used to
        # run the whole GPS photo-cluster pass, and any stale-photo-index
        # warning, twice per report).
        place_candidates, place_candidates_error = _fetch_place_candidates(archive_root, fha_config)

        bodies = {
            'discoveries': _section_discoveries(conn, prev, current),
            'review-queue': _section_review_queue(conn),
            'second-look': _section_second_look(conn),
            'new-since-last': _section_new_since_last(prev, current),
            'vitals-gaps': _section_vitals_gaps(findings, registry),
            'contradictions': _section_contradictions(findings),
            'search-log': _section_search_log(conn, current),
            'answerable-questions': _section_answerable_questions(conn, archive_root),
            'photo-triage': _section_photo_triage(
                archive_root, fha_config, photo_scan_error, photo_scan_notes),
            'place-candidates': _section_place_candidates(
                archive_root, place_candidates, place_candidates_error),
            'hypotheses': _section_hypotheses(conn, archive_root),
            'promotion-candidates': _section_promotion_candidates(conn, fha_config),
            'possible-connections': _section_possible_connections(archive_root),
        }

        place_escalations = _place_text_escalations(place_candidates)
        # Looked up once per escalated cluster and reused for both the
        # rendered bullet (`_place_text_group_line`, passed in below instead
        # of letting it repeat this same registry read) and the banner's own
        # wording just below - the banner needs to know whether ANY
        # escalated cluster already matches a registered place so it never
        # again claims "no place registered" for one that does (Codex
        # review, PR #142 finding 1).
        place_escalation_matches = [
            match_place_text_to_registry(archive_root, g['label']) for g in place_escalations
        ]
        place_escalation_lines = [
            _place_text_group_line(archive_root, g, match=m)
            for g, m in zip(place_escalations, place_escalation_matches)
        ]
        # Not one of SECTIONS/_SECTION_KEYS (never a `--section` filter target
        # or its own `## N.` heading) - present purely so `data['sections']`
        # exposes the same escalation a headless consumer can already read as
        # raw dicts off `data['place_escalations']` below (Codex review,
        # PR #142 finding 4).
        bodies['place-escalations'] = place_escalation_lines or [
            'No place-text clusters past the oversight threshold.']

        generated = datetime.date.today().isoformat()
        full_md = _render_report(generated, bodies, section_filter=None,
                                 archive_notes=archive_notes,
                                 place_escalation_lines=place_escalation_lines,
                                 place_escalation_matches=place_escalation_matches)
        printed_md = full_md if not section else _render_report(
            generated, bodies, section_filter=section, archive_notes=archive_notes,
            place_escalation_lines=place_escalation_lines,
            place_escalation_matches=place_escalation_matches)

        cache_dir = archive_root / '.cache'
        cache_dir.mkdir(parents=True, exist_ok=True)
        report_path = cache_dir / f'report_{generated}.md'
        report_path.write_text(full_md, encoding='utf-8')
        _write_snapshot(archive_root, current)
        snapshot_path = cache_dir / 'last_report.json'
    finally:
        conn.close()

    # Map the refresh's lint pass onto the tool suite's shared 0/1/2 exit-code
    # contract (TOOLING §1) instead of always reporting clean - an E-level
    # finding (duplicate IDs, malformed records, etc.) must surface as exit 2,
    # a W-level-only run as exit 1, same as `fha lint` itself would report.
    # The index rebuild's warnings (the archive-notes block) deliberately do
    # NOT move this code: report's documented exit contract is the lint
    # verdict, and its consumers (the `today` skill) read it as such - the
    # notes are printed, not exit-changing. Running `fha index` directly
    # still exits 1 on them, per §1's warnings contract.
    if any(f.severity == 'E' for f in findings):
        exit_code = EXIT_ERRORS
    elif any(f.severity == 'W' for f in findings):
        exit_code = EXIT_WARNINGS
    else:
        exit_code = EXIT_CLEAN

    return Result(
        ok=(exit_code != EXIT_ERRORS),
        exit_code=exit_code,
        data={
            'status': 'ok',
            'markdown': printed_md,
            'full_markdown': full_md,
            'sections': bodies,
            'place_escalations': place_escalations,
        },
        # The index warnings also ride as structured messages for headless
        # consumers; the markdown embeds the same texts as the archive-notes
        # block, so a front door should render one surface or the other.
        messages=list(index_result.messages),
        changed=[str(report_path), str(snapshot_path)],
    )


# ── CLI ───────────────────────────────────────────────────────────────────────

# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Your session briefing: what's new, what's worth a look, what to work on next.

  fha report                    The research feed (discoveries first, chores next)
  fha report --full             Everything, not just the highlights
  fha report --section NAME     Just one section

Refreshes the index, runs the checks, and compares against last session. Usually
you'll hear this narrated by asking "what should I work on?"."""


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'report',
        help='Generate the session research report (refresh, diff, render)',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (overrides auto-detection)')
    p.add_argument('--full', action='store_true', help='Ignore the snapshot baseline (everything looks new)')
    p.add_argument(
        '--section', metavar='NAME', choices=sorted(_SECTION_KEYS),
        help='Print only this section (still refreshes and records the full snapshot)',
    )
    p.set_defaults(func=_cmd_report)


def _cmd_report(args: argparse.Namespace) -> int:
    # resolve_root_arg carries the archive guard: a typo'd --root used to
    # mint a .cache and print a healthy-empty report with exit 0 (round-2
    # finding 10). Refusal fires before the refresh writes anything.
    archive_root = resolve_root_arg(args, command='fha report')
    if archive_root is None:
        return EXIT_FAILURE

    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    try:
        result = run_report(
            archive_root, fha_config,
            full=getattr(args, 'full', False),
            section=getattr(args, 'section', None),
        )
    except (ValueError, RuntimeError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE

    print(result['markdown'])
    return result.exit_code


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha report',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH')
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--section', metavar='NAME', choices=sorted(_SECTION_KEYS))
    args = parser.parse_args(argv)
    return _cmd_report(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
