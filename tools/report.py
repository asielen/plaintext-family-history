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
    _parse_questions            - notes/questions.md -> {heading: {status, refs, block}}
    _vitals_gap_pids            - W101 findings -> sorted P-id list (via registry paths)
    _build_snapshot             - current-state snapshot dict from the just-refreshed index

  Section builders (one per TOOLING §15a section; each returns list[str] lines)
    _section_discoveries         - §0: claim status flips, new corroborations,
                                    newly-answered questions, vitals gaps closed,
                                    newly confirmed relationship edges
    _section_review_queue        - §1: W102 backlog, grouped by source
    _section_new_since_last      - §2: source/claim/person id set diff vs snapshot
    _section_vitals_gaps         - §3: W101 findings, formatted
    _section_contradictions      - §4: E009 findings, formatted
    _section_search_log          - §5: search_log lookups for current leads
    _section_answerable_questions - §5b: open questions with a closeable gap
    _section_photo_triage        - §6: photoindex.run_triage embed
    _section_place_candidates    - §6b: places.run_candidates() embed
    _section_hypotheses          - §7: open hypotheses + draft-queue backlog
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
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    extract_token_ids,
    FhaConfigError,
    Result,
    fmt_id_display,
    load_fha_yaml,
    normalize_id,
    open_index_db,
    read_record,
    is_working_copy,
    resolve_root_arg,
)

import cooccur
import index
import lint
import photoindex

# ── Section registry ─────────────────────────────────────────────────────────

SECTIONS: list[tuple[str, str, str]] = [
    ('discoveries', '0', 'Discoveries since last session'),
    ('review-queue', '1', 'Review queue'),
    ('new-since-last', '2', 'New since last session'),
    ('vitals-gaps', '3', 'Vitals gaps'),
    ('contradictions', '4', 'Contradictions'),
    ('search-log', '5', 'Search-log awareness'),
    ('answerable-questions', '5b', 'Answerable questions'),
    ('photo-triage', '6', 'Photo processing triage'),
    ('place-candidates', '6b', 'Place candidates'),
    ('hypotheses', '7', 'Hypotheses & draft queues'),
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


_QUESTION_HEADING_RE = re.compile(r'^## Q:\s*(.+)$', re.M)
_QUESTION_STATUS_RE = re.compile(r'^- status:\s*(.+)$', re.M)
_QUESTION_REFS_RE = re.compile(r'^- refs:\s*\[(.*?)\]', re.M)


def _parse_question_blocks(text: str) -> dict[str, dict]:
    """Parse one file's text into {heading: {'status', 'refs', 'block'}}."""
    out: dict[str, dict] = {}
    blocks = re.split(r'(?=^## Q:)', text, flags=re.M)
    for block in blocks:
        m = _QUESTION_HEADING_RE.match(block)
        if not m:
            continue
        heading = m.group(1).strip()
        status_m = _QUESTION_STATUS_RE.search(block)
        refs_m = _QUESTION_REFS_RE.search(block)
        refs = [
            normalize_id(r.strip()) for r in (refs_m.group(1).split(',') if refs_m else [])
            if r.strip()
        ]
        out[heading] = {
            'status': status_m.group(1).strip() if status_m else '',
            'refs': refs,
            'block': block,
        }
    return out


def _parse_questions(archive_root: Path) -> dict[str, dict]:
    """
    Parse notes/questions.md AND every person research file's
    `## Open Questions` block into {heading: {'status', 'refs', 'block'}}.

    Mirrors `fha lint`'s E009 question-scanning scope exactly: lint.py's
    `_has_question_for` checks both `registry.questions_content`
    (notes/questions.md) and `registry.research_content` (every person
    research file collected in `_walk_archive` via `'_research_' in
    path.stem or path.stem.endswith('_research')`, under `people/`). The
    report's answerable-questions/discoveries sections must see the same
    question set lint does, or a question logged only in a person's research
    file would never get a closure proposal here.

    Headings are kept as the dict key as before; in theory two files could
    use the same heading text and one would shadow the other, but in
    practice question headings are unique free text, so this is treated as
    a non-issue rather than namespaced by file.
    """
    out: dict[str, dict] = {}

    path = archive_root / 'notes' / 'questions.md'
    if path.exists():
        try:
            out.update(_parse_question_blocks(path.read_text(encoding='utf-8')))
        except OSError:
            pass

    people_root = archive_root / 'people'
    if people_root.exists():
        for rpath in sorted(people_root.rglob('*.md')):
            if '_research_' in rpath.stem or rpath.stem.endswith('_research'):
                try:
                    text = rpath.read_text(encoding='utf-8')
                except OSError:
                    continue
                out.update(_parse_question_blocks(text))

    return out


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
    questions = _parse_questions(archive_root)
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
    newly_answered = sorted(
        h for h, status in cur_q.items()
        if status.startswith('answered') and not prev_q.get(h, '').startswith('answered')
    )
    if newly_answered:
        lines.append('**Questions newly answered:**')
        for h in newly_answered:
            lines.append(f'- {h} - {cur_q[h]}')

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
    questions = _parse_questions(archive_root)
    open_qs = {h: info for h, info in questions.items() if info['status'] == 'open'}
    if not open_qs:
        return ['No open questions.']

    lines: list[str] = []
    for heading, info in sorted(open_qs.items()):
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

def _section_photo_triage(
    archive_root: Path, fha_config: dict, scan_error: str | None = None
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
    result = photoindex.run_triage(archive_root, fha_config, top=10)
    if result['status'] in ('absent', 'unreadable'):
        return [f'Photo index {result["status"]} - run `fha photoindex` to enable triage.']

    candidates = result['candidates']
    if not candidates:
        return ['No unprocessed photo groups found.']

    lines = []
    for c in candidates:
        signals = ', '.join(c['signals']) if c['signals'] else 'no signals'
        lines.append(
            f"- {c['path']}  score={c['score']:+d}  [{signals}] - suggested: fha process {c['path']}"
        )
    return lines


# ── Section 6b: Place candidates ──────────────────────────────────────────────

def _section_place_candidates(archive_root: Path, fha_config: dict) -> list[str]:
    """
    Calls `places.run_candidates()` (BUILD.md M6.2). The import/attribute
    guards stay in place as a defensive fallback rather than a hard
    dependency - every other optional embed in this file (photoindex,
    cooccur) degrades the same way instead of raising.
    """
    try:
        import places as _places_tool   # noqa: PLC0415 - optional embed, see docstring
    except ImportError:
        return ['`fha places candidates` is not yet built (BUILD.md M6.2) - section deferred.']

    try:
        result = _places_tool.run_candidates(archive_root, fha_config)
    except AttributeError:
        return ['`fha places candidates` is not yet built (BUILD.md M6.2) - section deferred.']

    groups = result.get('groups') or []
    if not groups:
        return ['No recurring unlinked place-text or GPS clusters found.']
    return [f"- {g}" for g in groups]


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


# ── Section 8: Possible connections (fha cooccur) ─────────────────────────────

def _section_possible_connections(archive_root: Path) -> list[str]:
    result = cooccur.run_cooccur(archive_root, threshold=2)
    if result['status'] != 'ok':
        return ['`fha cooccur` could not run - check .cache/index.sqlite.']

    lines: list[str] = []

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
) -> str:
    """Assemble the report markdown: title, archive notes (when any), sections.

    `archive_notes` are the refresh's own warnings (today: build_index's
    malformed-coords messages) - lines a hand-edit produced that the refresh
    skipped over. They render right under the title, before any section,
    because the report IS the session-start path: a warning that only exists
    on the discarded Result is invisible exactly where the human looks first
    (round-2 finding 16). They print on section-filtered runs too - narrowing
    the view should never hide that a line of the archive was skipped."""
    lines = [f'# fha report - {generated}', '']
    if archive_notes:
        lines.append('**Archive notes from this refresh:**')
        lines.extend(f'- {note}' for note in archive_notes)
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
    try:
        photoindex.run_scan(archive_root, fha_config, full=False)
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

        bodies = {
            'discoveries': _section_discoveries(conn, prev, current),
            'review-queue': _section_review_queue(conn),
            'new-since-last': _section_new_since_last(prev, current),
            'vitals-gaps': _section_vitals_gaps(findings, registry),
            'contradictions': _section_contradictions(findings),
            'search-log': _section_search_log(conn, current),
            'answerable-questions': _section_answerable_questions(conn, archive_root),
            'photo-triage': _section_photo_triage(archive_root, fha_config, photo_scan_error),
            'place-candidates': _section_place_candidates(archive_root, fha_config),
            'hypotheses': _section_hypotheses(conn, archive_root),
            'possible-connections': _section_possible_connections(archive_root),
        }

        generated = datetime.date.today().isoformat()
        full_md = _render_report(generated, bodies, section_filter=None,
                                 archive_notes=archive_notes)
        printed_md = full_md if not section else _render_report(
            generated, bodies, section_filter=section, archive_notes=archive_notes)

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
