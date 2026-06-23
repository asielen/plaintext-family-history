#!/usr/bin/env python3
"""
convert_mining.py — fha convert-mining: one-time legacy interview migration.

  fha convert-mining [--root PATH]            Dry-run: print the conversion plan
  fha convert-mining [--root PATH] --apply    Write the conformant records

Migrates a legacy transcript-mining pipeline's output (the `mining/` folder of
`sources.txt`, `facts.txt`, `stories.txt`, `questions.txt`, `aliases.txt`, and
the raw `transcripts/`) into conformant archive records (SPEC §12.1/§14,
TOOLING §11). It is the migration analogue of `fha process` + the draft pass, and is
**dry-run by default** — nothing is written without `--apply`. It is a one-shot
migration (re-applying mints fresh IDs and would duplicate), so the dry-run plan
is the safety gate; review it, then `--apply` once.

What it produces (TOOLING §11):

  1. **Sources first.** Each legacy `S###` → its transcript copied into
     `documents/interviews/{slug}_{S-id}.txt` (renamed with the minted S-id,
     `original_filename` kept), a `sources/interview/{slug}_{S-id}.md` record
     scaffolded with `source_type: interview`, `people:` resolved via the alias
     map, the legacy extraction pass recorded in `## AI Passes`, and stories in
     `## Stories`.
  2. **Facts → suggested claims.** Each `facts.txt` table row → a `suggested`
     claim on its source record: the Claim text → `value`; Earliest/Latest →
     a single EDTF date or interval; Confidence H/M/L → `confidence`; the type
     inferred from the Claim text by keyword (defaulting to `event` with the
     legacy Section as `subtype`). `Update(T###):` lines merge into the
     preceding claim's `notes`.
  3. **Anchors (best-effort).** The 3 rarest content words of a claim's value
     are searched in the transcript; a uniquely-matching line becomes
     `anchor: line N`, otherwise the anchor is omitted.
  4. **Stories → `## Stories`** (on the source record, persons resolved to
     `[P-id]` tokens); **questions → `notes/questions.md`** (`origin: tool`,
     `status: open`, with the person/source refs mapped to their new IDs).
  5. **People.** Every person the alias map (or a fact/story/question) names is
     resolved to a P-id; one without an existing record is minted as a stub
     (§5), so every claim/story/question reference resolves and the result lints
     with no errors.
  6. **Audit trail.** `.cache/convert_mapping.csv` records every legacy_id → new
     id mapping with a note.

Tools never import tools (TOOLING §15): the scaffolding here is a self-contained
re-use of the same `_lib` primitives `fha process` uses, not an import of it.
"""

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  Legacy parsing
#    parse_sources / parse_aliases / parse_facts / parse_stories / parse_questions
#    _parse_table_block        — markdown fact table → row dicts (+ Update lines)
#
#  Field derivation
#    derive_claim_type         — Claim text + Section → (type, subtype)
#    legacy_to_edtf            — Earliest/Latest cells → a valid EDTF, or None
#    _confidence               — H/M/L → high/medium/low
#    find_anchor               — 3 rarest words → unique transcript line, or None
#
#  Resolution + minting
#    PersonResolver            — name → P-id (alias or fresh), tracks stubs to mint
#    _person_filename / _person_stub_text
#    _slugify / _yaml_inline
#
#  Building
#    build_plan                — parse everything into a ConversionPlan (no writes)
#    _render_source_record     — one source record's full text
#    _render_questions_block / _render_mapping_csv
#
#  Apply + CLI
#    apply_plan / print_plan
#    register / _run_convert / _standalone_main
#
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import csv
import datetime
import io
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    CLAIM_TYPES,
    FhaConfigError,
    configure_utf8_stdout,
    fmt_id_display,
    id_type_of,
    is_valid_edtf,
    load_fha_yaml,
    mint_ids,
    normalize_id,
    path_to_alias,
    resolve_path,
    resolve_root_arg,
    scan_person_record_ids,
)

import yaml

configure_utf8_stdout()

_INTERVIEW_SOURCE_TYPE = 'interview'
_INTERVIEW_DOC_SUBDIR = 'interviews'


def _today() -> str:
    return datetime.date.today().isoformat()


class ConvertError(Exception):
    """A user-facing conversion failure (missing input or malformed legacy data)."""


# ── Slug / YAML helpers (mirrors process.py; tools never import tools) ──────────

def _slugify(text: str) -> str:
    text = (text or '').strip().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return slug or 'item'


def _yaml_inline(value: str) -> str:
    """Single-line YAML scalar, quoted exactly when the parser needs it."""
    rendered = yaml.safe_dump(
        value, default_flow_style=True, allow_unicode=True, width=10 ** 9,
    ).strip()
    if rendered.endswith('...'):
        rendered = rendered[:-3].strip()
    return rendered


# ── Legacy parsing ────────────────────────────────────────────────────────────

def parse_sources(text: str) -> dict[str, dict]:
    """Parse `sources.txt` into {S###: {transcript, title, interviewee, date, model}}.

    Blocks are separated by blank lines; the first non-blank line of a block is
    the legacy id (`S001`), followed by `key: value` lines.
    """
    sources: dict[str, dict] = {}
    for block in re.split(r'\n\s*\n', text):
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        legacy_id = lines[0].strip()
        if not re.match(r'^S\d+$', legacy_id):
            continue
        fields: dict = {}
        for ln in lines[1:]:
            if ':' in ln:
                key, _, val = ln.partition(':')
                fields[key.strip().lower()] = val.strip()
        sources[legacy_id] = fields
    return sources


def parse_aliases(text: str) -> dict[str, str]:
    """Parse `aliases.txt` lines `Name = P-id` into {name: P-id} (case-folded key)."""
    aliases: dict[str, str] = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith('#') or '=' not in ln:
            continue
        name, _, pid = ln.partition('=')
        name, pid = name.strip(), pid.strip()
        if not name or not pid:
            continue
        if id_type_of(pid) != 'P':
            raise ConvertError(f'alias {name!r} maps to an invalid P-id {pid!r}.')
        aliases[name.lower()] = fmt_id_display(normalize_id(pid))
    return aliases


def _parse_table_block(block_lines: list[str]) -> list[dict]:
    """Parse one markdown fact table (with Update lines) into row dicts.

    Returns one dict per data row with lowercased column keys plus an `updates`
    list (the `Update(T###): …` continuation lines that follow a row attach to
    the most recent row).
    """
    rows: list[dict] = []
    header: list[str] | None = None
    for raw in block_lines:
        ln = raw.strip()
        if not ln:
            continue
        upd = re.match(r'^Update\(([^)]*)\):\s*(.*)$', ln, re.I)
        if upd:
            if rows:
                rows[-1]['updates'].append((upd.group(1).strip(), upd.group(2).strip()))
            continue
        if not ln.startswith('|'):
            continue
        cells = [c.strip() for c in ln.strip('|').split('|')]
        if all(set(c) <= set('-: ') for c in cells):
            continue  # separator row
        if header is None:
            header = [c.lower() for c in cells]
            continue
        row = {header[i]: (cells[i] if i < len(cells) else '') for i in range(len(header))}
        row['updates'] = []
        rows.append(row)
    return rows


def parse_facts(text: str) -> dict[str, list[dict]]:
    """Parse `facts.txt` (tables grouped under `## S###`) into {S###: [row, …]}."""
    facts: dict[str, list[dict]] = {}
    current: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current is not None:
            facts.setdefault(current, []).extend(_parse_table_block(buffer))

    for ln in text.splitlines():
        m = re.match(r'^##\s+(S\d+)\s*$', ln.strip())
        if m:
            flush()
            current = m.group(1)
            buffer = []
        else:
            buffer.append(ln)
    flush()
    return facts


def parse_stories(text: str) -> list[dict]:
    """Parse `stories.txt` `## Person (S###)` blocks into [{person, source, body}]."""
    stories: list[dict] = []
    current: dict | None = None
    body: list[str] = []

    def flush() -> None:
        if current is not None:
            current['body'] = '\n'.join(body).strip()
            stories.append(current)

    for ln in text.splitlines():
        m = re.match(r'^##\s+(.*?)\s*(?:\((S\d+)\))?\s*$', ln) if ln.startswith('##') else None
        if m:
            flush()
            current = {'person': m.group(1).strip(), 'source': m.group(2), 'body': ''}
            body = []
        elif current is not None:
            body.append(ln)
    flush()
    return [s for s in stories if s['body']]


def parse_questions(text: str) -> list[dict]:
    """Parse `questions.txt` `## Q: …` blocks into [{question, person, source}]."""
    questions: list[dict] = []
    current: dict | None = None

    def flush() -> None:
        if current is not None:
            questions.append(current)

    for ln in text.splitlines():
        q = re.match(r'^##\s+Q:\s*(.*)$', ln.strip())
        if q:
            flush()
            current = {'question': q.group(1).strip(), 'person': None, 'source': None}
        elif current is not None and ':' in ln:
            key, _, val = ln.partition(':')
            key = key.strip().lstrip('-').strip().lower()
            if key in ('person', 'source'):
                current[key] = val.strip() or None
    flush()
    return questions


# ── Field derivation ──────────────────────────────────────────────────────────

# Keyword → claim type, in priority order. `relationship`/`name` are deliberately
# absent: relationship claims require `roles` (lint E015), and these heuristics
# can't responsibly infer either — they stay `event` for human review instead.
_TYPE_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (('born', 'birth'), 'birth'),
    (('married', 'marriage', 'wedding', 'wed '), 'marriage'),
    (('divorced', 'divorce'), 'divorce'),
    (('died', 'death', 'passed away'), 'death'),
    (('baptized', 'baptised', 'christened', 'baptism'), 'baptism'),
    (('buried', 'burial', 'interred'), 'burial'),
    (('served', 'infantry', 'regiment', 'enlisted', 'military', 'army', 'navy'), 'military'),
    (('immigrated', 'emigrated', 'immigration', 'naturalized'), 'immigration'),
    (('census', 'enumerated'), 'census'),
    (('worked', 'occupation', 'employed', 'bookkeeper', 'clerk', 'farmer'), 'occupation'),
    (('school', 'educated', 'graduated', 'college', 'university', 'education'), 'education'),
    (('lived', 'resided', 'residence', 'settled', 'moved to'), 'residence'),
]


def derive_claim_type(claim_text: str, section: str) -> tuple[str, str | None]:
    """Infer a claim `type` (and `subtype`) from the Claim text and Section.

    Keyword match wins; an unmatched fact becomes `event` with the legacy
    Section as `subtype` (per TOOLING §11), so the row still imports as a valid
    claim for the reviewer to retype.
    """
    t = (claim_text or '').lower()
    for keywords, claim_type in _TYPE_KEYWORDS:
        if any(k in t for k in keywords):
            return claim_type, None
    subtype = _slugify(section) if section else None
    return 'event', (subtype or None)


def _year_marker(cell: str) -> tuple[str | None, bool]:
    """Return (4-digit year, approximate?) parsed from a legacy date cell.

    Approximate when the cell carries the legacy `~` or `?` uncertainty markers.
    """
    cell = (cell or '').strip()
    if not cell:
        return None, False
    approx = '~' in cell or '?' in cell
    m = re.search(r'\b(1[5-9]\d{2}|20\d{2})\b', cell)
    return (m.group(1) if m else None), approx


def _decade_marker(cell: str) -> str | None:
    """Return the EDTF decade form (`189X`) for a legacy unknown-digit cell.

    TOOLING §11: the legacy `??` (and bare `?` on a 3-digit-plus-marker cell)
    unknown-final-digit marker maps to EDTF `X`, e.g. `189?`/`189??` → `189X`.
    """
    cell = (cell or '').strip()
    m = re.match(r'^(1[5-9]\d|20\d)\?{1,2}$', cell)
    return f'{m.group(1)}X' if m else None


def legacy_to_edtf(earliest: str, latest: str) -> str | None:
    """Map legacy Earliest/Latest cells to a valid EDTF date, or None.

    Equal (or single) → one value (`1890`, `1890~` when uncertain, `189X` for
    an unknown-final-digit decade per TOOLING §11); two different values on
    either side → the `min/max` interval — decade/decade (`189X/190X`),
    decade/year (`189X/1900`), or year/year alike. Anything that won't
    validate as EDTF is dropped rather than written (lint E014 would reject
    a malformed date).
    """
    e_decade = _decade_marker(earliest)
    l_decade = _decade_marker(latest)
    e_year, e_approx = _year_marker(earliest)
    l_year, l_approx = _year_marker(latest)

    def one(year: str, approx: bool) -> str:
        return f'{year}~' if approx else year

    e_bare = e_decade or e_year
    l_bare = l_decade or l_year
    e_token = e_decade or (one(e_year, e_approx) if e_year else None)
    l_token = l_decade or (one(l_year, l_approx) if l_year else None)

    if e_bare and l_bare and e_bare == l_bare:
        # Same underlying value on both sides (e.g. `1890~`/`1890`) — one
        # value, with the uncertainty marker if *either* side carried it.
        edtf = e_decade or one(e_bare, e_approx or l_approx)
    elif e_token and l_token:
        edtf = f'{e_token}/{l_token}'
    elif e_token:
        edtf = e_token
    elif l_token:
        edtf = l_token
    else:
        return None
    return edtf if is_valid_edtf(edtf) else None


def _confidence(cell: str) -> str | None:
    """Map a legacy Confidence cell (High/Medium/Low, or H/M/L) to the vocabulary."""
    c = (cell or '').strip().lower()
    if c.startswith('h'):
        return 'high'
    if c.startswith('m'):
        return 'medium'
    if c.startswith('l'):
        return 'low'
    return None


_STOPWORDS = frozenset({
    'the', 'and', 'for', 'was', 'with', 'his', 'her', 'she', 'they', 'were',
    'that', 'this', 'from', 'had', 'has', 'who', 'into', 'about', 'their',
    'after', 'before', 'while', 'when', 'where', 'which', 'born', 'died',
})


def find_anchor(value: str, transcript_lines: list[str]) -> str | None:
    """Best-effort `anchor: line N` from the 3 rarest content words of `value`.

    Content words present in the transcript are ranked rarest-first; the tool
    looks for a line containing all 3 (then 2, then the single rarest). The
    first uniqueness — exactly one matching line — yields that 1-based line
    number; if no subset is unique, the anchor is omitted (TOOLING §11).
    """
    lower_lines = [ln.lower() for ln in transcript_lines]
    freq: Counter = Counter()
    for ln in lower_lines:
        for w in re.findall(r'[a-z]{4,}', ln):
            freq[w] += 1
    words = [w for w in re.findall(r'[a-z]{4,}', value.lower())
             if w not in _STOPWORDS and freq.get(w, 0) > 0]
    # De-dup preserving order, then rank by transcript rarity (ascending).
    seen: list[str] = []
    for w in words:
        if w not in seen:
            seen.append(w)
    rarest = sorted(seen, key=lambda w: freq[w])[:3]
    if not rarest:
        return None
    for k in range(len(rarest), 0, -1):
        subset = rarest[:k]
        matches = [i for i, ln in enumerate(lower_lines, 1)
                   if all(w in ln for w in subset)]
        if len(matches) == 1:
            return f'line {matches[0]}'
    return None


# ── Person resolution + minting ────────────────────────────────────────────────

@dataclass
class _Person:
    name: str
    pid: str
    from_alias: bool
    needs_stub: bool


class PersonResolver:
    """Resolve a legacy person name to a P-id, minting stubs as needed (§5).

    A name in the alias map takes that P-id; an unaliased name gets a freshly
    minted one. Either way, a P-id with no existing person record under
    `people/` is flagged `needs_stub` so the conversion writes a stub for it —
    that is what keeps every claim/story/question reference lint-clean (E005).
    """

    def __init__(self, aliases: dict[str, str], existing_pids: set[str], to_mint: list[str]):
        self._aliases = aliases
        self._existing = existing_pids       # lowercased P-ids already on disk
        self._mint_pool = list(to_mint)
        self._by_name: dict[str, _Person] = {}

    def resolve(self, name: str) -> _Person:
        key = (name or '').strip().lower()
        if key in self._by_name:
            return self._by_name[key]
        alias_pid = self._aliases.get(key)
        if alias_pid:
            pid = alias_pid
            from_alias = True
        else:
            if not self._mint_pool:
                raise ConvertError('ran out of pre-minted P-ids (internal counting error).')
            pid = self._mint_pool.pop(0)
            from_alias = False
        needs_stub = normalize_id(pid) not in self._existing
        person = _Person(name=name.strip(), pid=pid, from_alias=from_alias, needs_stub=needs_stub)
        self._by_name[key] = person
        return person

    def all_people(self) -> list[_Person]:
        return list(self._by_name.values())


def _person_filename(name: str, pid: str) -> str:
    tokens = [t for t in re.split(r'\s+', name.strip()) if t]
    if len(tokens) >= 2:
        surname = _slugify(tokens[-1])
        given = _slugify(' '.join(tokens[:-1]))
    else:
        surname = _slugify(tokens[0]) if tokens else 'unknown'
        given = surname
    return f'{surname}__{given}_{pid}.md'


def _person_stub_text(name: str, pid: str) -> str:
    return (
        '---\n'
        f'id: {pid}\n'
        f'name: {_yaml_inline(name)}\n'
        'tier: stub\n'
        'living: unknown\n'
        '---\n'
    )


# ── Build ──────────────────────────────────────────────────────────────────────

@dataclass
class _Claim:
    cid: str
    legacy_source: str
    value: str
    claim_type: str
    subtype: str | None
    persons: list[str]
    date: str | None
    place_text: str | None
    confidence: str | None
    anchor: str | None
    notes: str | None


@dataclass
class _Source:
    legacy_id: str
    sid: str
    title: str
    slug: str
    transcript_src: Path
    doc_dest_alias: str          # documents/interviews/{slug}_{S-id}.txt
    doc_dest_path: Path
    people: list[str]
    notes: str
    ai_pass_date: str
    ai_pass_model: str
    stories: list[str]
    claims: list[_Claim]


@dataclass
class ConversionPlan:
    archive_root: Path
    sources: list[_Source]
    stub_people: list[_Person]        # persons needing a stub record
    questions: list[dict]             # rendered question dicts
    mapping_rows: list[tuple[str, str, str]]
    warnings: list[str]


def _read_text(path: Path) -> str:
    return path.read_text(encoding='utf-8') if path.is_file() else ''


def build_plan(archive_root: Path, fha_config: dict, mining_dir: Path) -> ConversionPlan:
    """Parse the legacy export and plan every record to write (no filesystem writes)."""
    if not mining_dir.is_dir():
        raise ConvertError(f'no mining/ folder found at {mining_dir}.')

    sources_raw = parse_sources(_read_text(mining_dir / 'sources.txt'))
    if not sources_raw:
        raise ConvertError('mining/sources.txt is missing or defines no sources.')
    aliases = parse_aliases(_read_text(mining_dir / 'aliases.txt'))
    facts = parse_facts(_read_text(mining_dir / 'facts.txt'))
    stories = parse_stories(_read_text(mining_dir / 'stories.txt'))
    questions = parse_questions(_read_text(mining_dir / 'questions.txt'))

    warnings: list[str] = []

    # Names that will need a P-id: every interviewee, every fact Person, every
    # story/question person. Count the unaliased ones to size the mint pool.
    names: list[str] = []

    def note_name(n: str | None) -> None:
        if n and n.strip() and n.strip() not in names:
            names.append(n.strip())

    for meta in sources_raw.values():
        note_name(meta.get('interviewee'))
    for rows in facts.values():
        for row in rows:
            note_name(row.get('person'))
    for s in stories:
        note_name(s.get('person'))
    for q in questions:
        note_name(q.get('person'))

    unaliased = [n for n in names if n.lower() not in aliases]
    existing_pids = scan_person_record_pids(archive_root)
    minted_pids = mint_ids('P', len(unaliased), archive_root) if unaliased else []
    resolver = PersonResolver(aliases, existing_pids, minted_pids)

    # Mint S-ids (one per source) and C-ids (one per fact row) in single batches.
    legacy_source_ids = list(sources_raw.keys())
    total_facts = sum(len(facts.get(sid, [])) for sid in legacy_source_ids)
    sid_pool = mint_ids('S', len(legacy_source_ids), archive_root)
    cid_pool = mint_ids('C', total_facts, archive_root) if total_facts else []

    legacy_to_sid: dict[str, str] = {}
    built_sources: list[_Source] = []
    mapping_rows: list[tuple[str, str, str]] = []
    attached_story_indices: set[int] = set()

    documents_root = resolve_path('documents', fha_config, archive_root)

    for legacy_id, sid in zip(legacy_source_ids, sid_pool):
        meta = sources_raw[legacy_id]
        legacy_to_sid[legacy_id] = sid
        transcript_name = meta.get('transcript') or f'{legacy_id}.txt'
        transcript_src = mining_dir / 'transcripts' / transcript_name
        if not transcript_src.is_file():
            warnings.append(f'{legacy_id}: transcript {transcript_name!r} not found; '
                            'source will have no asset file.')
        title = meta.get('title') or f'Interview {legacy_id}'
        slug = _slugify(title)
        doc_dest = documents_root / _INTERVIEW_DOC_SUBDIR / f'{slug}_{sid}.txt'
        doc_alias = path_to_alias(doc_dest, 'documents', fha_config, archive_root)

        transcript_lines = (
            transcript_src.read_text(encoding='utf-8').splitlines()
            if transcript_src.is_file() else []
        )

        # People named on this source: the interviewee plus every fact's Person.
        people_pids: list[str] = []
        interviewee = meta.get('interviewee')
        if interviewee:
            people_pids.append(resolver.resolve(interviewee).pid)

        source_claims: list[_Claim] = []
        rows = facts.get(legacy_id, [])
        for row in rows:
            cid = cid_pool.pop(0)
            person_name = row.get('person') or interviewee or ''
            if not person_name:
                warnings.append(f'{legacy_id}: a fact row has no Person; skipped.')
                continue
            value = (row.get('claim') or '').strip()
            if not value:
                warnings.append(f'{legacy_id}: a fact row for {person_name!r} has a blank '
                                 'Claim cell; skipped (would otherwise lint E010).')
                continue
            person = resolver.resolve(person_name)
            if person.pid not in people_pids:
                people_pids.append(person.pid)
            claim_type, subtype = derive_claim_type(value, row.get('section', ''))
            if claim_type not in CLAIM_TYPES:
                claim_type, subtype = 'event', subtype
            date = legacy_to_edtf(row.get('earliest', ''), row.get('latest', ''))
            place_text = (row.get('place') or '').strip() or None
            notes_bits: list[str] = []
            for tref, utext in row.get('updates', []):
                notes_bits.append(f'Update ({tref}): {utext}' if tref else f'Update: {utext}')
            notes = ' '.join(notes_bits) or None
            anchor = find_anchor(value, transcript_lines) if value else None
            source_claims.append(_Claim(
                cid=cid, legacy_source=legacy_id, value=value, claim_type=claim_type,
                subtype=subtype, persons=[person.pid], date=date, place_text=place_text,
                confidence=_confidence(row.get('confidence', '')), anchor=anchor, notes=notes,
            ))
            mapping_rows.append((f'{legacy_id}:{row.get("claim", "")[:30]}', cid, f'claim ({claim_type})'))

        # Stories for this source, persons resolved to [P-id] tokens.
        source_stories: list[str] = []
        for idx, s in enumerate(stories):
            if s.get('source') == legacy_id:
                attached_story_indices.add(idx)
                token = ''
                if s.get('person'):
                    story_person = resolver.resolve(s['person'])
                    token = f'[{story_person.pid}] '
                    if story_person.pid not in people_pids:
                        people_pids.append(story_person.pid)
                source_stories.append(f'{token}{s["body"]}'.strip())

        extraction_date = meta.get('date') or ''
        model = meta.get('model') or 'unknown model'
        notes_body = (
            f'Imported from legacy mining pass ({legacy_id}). '
            f'Extraction model: {model}'
            + (f'; run date {extraction_date}.' if extraction_date else '.')
        )

        built_sources.append(_Source(
            legacy_id=legacy_id, sid=sid, title=title, slug=slug,
            transcript_src=transcript_src, doc_dest_alias=doc_alias, doc_dest_path=doc_dest,
            people=people_pids, notes=notes_body, ai_pass_date=extraction_date or _today(),
            ai_pass_model=model, stories=source_stories, claims=source_claims,
        ))
        mapping_rows.append((legacy_id, sid, f'source: {title}'))

    # Facts referencing an unknown source are reported, not silently dropped.
    for legacy_id in facts:
        if legacy_id not in sources_raw:
            warnings.append(f'facts.txt references unknown source {legacy_id}; its rows were skipped.')

    # Stories that never matched a source in the loop above (missing or
    # unknown source ref) are reported too, not silently dropped.
    for idx, s in enumerate(stories):
        if idx not in attached_story_indices:
            ref = s.get('source') or '(none)'
            warnings.append(f'stories.txt has a story referencing source {ref!r} '
                             'that was not converted; its narrative text was skipped.')

    # Rendered questions (refs mapped to new ids).
    rendered_questions: list[dict] = []
    for q in questions:
        refs: list[str] = []
        if q.get('person'):
            refs.append(resolver.resolve(q['person']).pid)
        if q.get('source'):
            if q['source'] in legacy_to_sid:
                refs.append(legacy_to_sid[q['source']])
            else:
                warnings.append(f'questions.txt references unknown source {q["source"]} '
                                 f'on question {q["question"]!r}; the source ref was dropped.')
        rendered_questions.append({'question': q['question'], 'refs': refs})

    # People mapping + which need stubs (de-duplicated by P-id: multiple alias
    # names can point at the same unminted P-id, and that P-id gets one stub).
    stub_people: list[_Person] = []
    stubbed_pids: set[str] = set()
    for person in resolver.all_people():
        origin = 'alias' if person.from_alias else 'minted'
        mapping_rows.append((person.name, person.pid, f'person ({origin})'))
        if person.needs_stub and normalize_id(person.pid) not in stubbed_pids:
            stub_people.append(person)
            stubbed_pids.add(normalize_id(person.pid))

    return ConversionPlan(
        archive_root=archive_root, sources=built_sources, stub_people=stub_people,
        questions=rendered_questions, mapping_rows=mapping_rows, warnings=warnings,
    )


def scan_person_record_pids(archive_root: Path) -> set[str]:
    """Lowercased P-ids of existing person records under people/ (avoid re-minting stubs)."""
    return scan_person_record_ids(archive_root)


# ── Rendering ──────────────────────────────────────────────────────────────────

def _render_claim(claim: _Claim) -> list[str]:
    """Render one suggested claim as block-style YAML lines for the ## Claims fence."""
    lines = [
        f'- value: {_yaml_inline(claim.value)}',
        f'  id: {claim.cid}',
        f'  type: {claim.claim_type}',
    ]
    if claim.subtype:
        lines.append(f'  subtype: {_yaml_inline(claim.subtype)}')
    lines.append(f'  persons: [{", ".join(claim.persons)}]')
    if claim.date:
        # Quote through the YAML emitter so a bare year ('1890') stays a string
        # rather than parsing back as an int (and a '~'/'/' EDTF stays literal).
        lines.append(f'  date: {_yaml_inline(claim.date)}')
    if claim.place_text:
        lines.append(f'  place_text: {_yaml_inline(claim.place_text)}')
    lines.append('  status: suggested')
    if claim.confidence:
        lines.append(f'  confidence: {claim.confidence}')
    if claim.anchor:
        lines.append(f'  anchor: {_yaml_inline(claim.anchor)}')
    if claim.notes:
        lines.append(f'  notes: {_yaml_inline(claim.notes)}')
    return lines


def _render_source_record(source: _Source) -> str:
    """Render a source record's full text (frontmatter + claims + notes + stories)."""
    people_inline = f'[{", ".join(source.people)}]' if source.people else '[]'
    outputs = ['source-record', 'suggested-claims']
    if source.stories:
        outputs.append('stories')
    lines = [
        '---',
        f'id: {source.sid}',
        f'title: {_yaml_inline(source.title)}',
        f'source_type: {_INTERVIEW_SOURCE_TYPE}',
        'source_class: original',
        'repository: family interview',
        'citation: >',
        f'  {source.title} (oral-history interview; imported from legacy mining {source.legacy_id}).',
        f'people: {people_inline}',
    ]
    if source.transcript_src.is_file():
        lines += [
            'files:',
            f'  - file: {_yaml_inline(source.doc_dest_alias)}',
            '    role: primary',
            f'    original_filename: {_yaml_inline(source.transcript_src.name)}',
        ]
    lines += [
        f'created: {_today()}',
        '---',
        '',
        '## Claims',
        '```yaml',
    ]
    for i, claim in enumerate(source.claims):
        if i:
            lines.append('')
        lines += _render_claim(claim)
    lines.append('```')
    lines += [
        '',
        '## AI Passes',
        '```yaml',
        f'- date: {_yaml_inline(source.ai_pass_date)}',
        f'  model: {_yaml_inline(source.ai_pass_model)}',
        '  harness: legacy-mining-import',
        f'  task: {_yaml_inline(f"Import legacy mining pass {source.legacy_id}")}',
        f'  outputs: [{", ".join(outputs)}]',
        '  human_reviewed: false',
        '```',
    ]
    lines += ['', '## Notes', source.notes]
    if source.stories:
        lines += ['', '## Stories']
        for story in source.stories:
            lines += [story, '']
    if not source.stories:
        lines.append('')
    return '\n'.join(lines)


def _render_questions_block(questions: list[dict]) -> str:
    """Render imported questions as `notes/questions.md` blocks (origin: tool)."""
    out: list[str] = []
    today = _today()
    for q in questions:
        out.append(f'## Q: {q["question"]}')
        out.append('- origin: tool')
        out.append('- status: open')
        if q['refs']:
            out.append(f'- refs: [{", ".join(q["refs"])}]')
        out.append('- context:')
        out.append(f'  - (tool, {today}) Imported from legacy mining questions.txt.')
        out.append('')
    return '\n'.join(out)


def _render_mapping_csv(rows: list[tuple[str, str, str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['legacy_id', 'new_id', 'notes'])
    for r in rows:
        writer.writerow(r)
    return buf.getvalue()


# ── Apply + print ───────────────────────────────────────────────────────────────

def print_plan(plan: ConversionPlan, *, applied: bool) -> None:
    verb = 'Wrote' if applied else 'Would write'
    head = 'Applied conversion' if applied else 'Conversion plan (dry-run — use --apply to write)'
    print(head)
    print(f'  Sources:       {len(plan.sources)}')
    print(f'  Claims:        {sum(len(s.claims) for s in plan.sources)}')
    print(f'  Person stubs:  {len(plan.stub_people)}')
    print(f'  Questions:     {len(plan.questions)}')
    for s in plan.sources:
        print(f'  {verb} source {s.legacy_id} -> {s.sid} '
              f'({len(s.claims)} claim(s)) [{s.slug}]')
    for p in plan.stub_people:
        print(f'  {verb} person stub {p.name} -> {p.pid}')
    for w in plan.warnings:
        print(f'  WARNING: {w}', file=sys.stderr)


def _record_path(root: Path, source: _Source) -> Path:
    """Return the source-record destination for a planned converted source."""
    return root / 'sources' / _INTERVIEW_SOURCE_TYPE / f'{source.slug}_{source.sid}.md'


def _preflight_apply(plan: ConversionPlan) -> None:
    """Refuse apply when the planned migration would clobber existing files.

    `convert-mining` is intentionally one-shot: re-running it mints new IDs, so
    the strongest repeat-run sentinel is the mapping file, not the fresh IDs.
    Destination checks still catch a manual collision or a resumed partial run
    before any write occurs.
    """
    root = plan.archive_root
    mapping_path = root / '.cache' / 'convert_mapping.csv'
    if mapping_path.exists():
        raise ConvertError(
            '.cache/convert_mapping.csv already exists; refusing to apply this '
            'one-time migration again.'
        )

    destinations: list[Path] = []
    destinations += [
        root / 'people' / 'stubs' / _person_filename(p.name, p.pid)
        for p in plan.stub_people
    ]
    destinations += [s.doc_dest_path for s in plan.sources if s.transcript_src.is_file()]
    destinations += [_record_path(root, s) for s in plan.sources]
    conflicts = [p for p in destinations if p.exists()]
    if conflicts:
        def display(p: Path) -> str:
            try:
                return str(p.relative_to(root))
            except ValueError:
                return str(p)
        shown = ', '.join(display(p) for p in conflicts[:5])
        if len(conflicts) > 5:
            shown += f', ... ({len(conflicts)} total)'
        raise ConvertError(f'planned destination already exists: {shown}')


def apply_plan(plan: ConversionPlan, fha_config: dict) -> None:
    """Write every planned record transactionally enough to avoid partial state.

    Files are preflighted before the first write, then each write registers an
    undo action. If a later write fails, new files are removed and an existing
    `notes/questions.md` is restored to its previous text.
    """
    root = plan.archive_root
    _preflight_apply(plan)
    undo: list = []

    def ensure_parent(path: Path) -> None:
        missing: list[Path] = []
        parent = path.parent
        while parent != root and not parent.exists():
            missing.append(parent)
            parent = parent.parent
        path.parent.mkdir(parents=True, exist_ok=True)
        for created in reversed(missing):
            undo.append(lambda p=created: p.rmdir())

    def write_new(path: Path, text: str) -> None:
        ensure_parent(path)
        # Register the cleanup before writing, same as copy_new — a
        # write_text() that fails partway (e.g. disk full) can still leave a
        # partially-written file behind.
        undo.append(lambda p=path: p.unlink(missing_ok=True))
        path.write_text(text, encoding='utf-8')

    def copy_new(src: Path, dest: Path) -> None:
        ensure_parent(dest)
        # Register the cleanup before copying: a copy2() that fails partway
        # (e.g. disk full) can still leave a partially-written dest behind,
        # and missing_ok=True makes the unlink harmless if it never got that far.
        undo.append(lambda p=dest: p.unlink(missing_ok=True))
        shutil.copy2(src, dest)

    try:
        # 1) Person stubs.
        for person in plan.stub_people:
            write_new(
                root / 'people' / 'stubs' / _person_filename(person.name, person.pid),
                _person_stub_text(person.name, person.pid),
            )

        # 2) Transcripts → documents/interviews/, and 3) source records.
        for s in plan.sources:
            if s.transcript_src.is_file():
                copy_new(s.transcript_src, s.doc_dest_path)
            write_new(_record_path(root, s), _render_source_record(s))

        # 4) Questions appended to notes/questions.md.
        if plan.questions:
            qpath = root / 'notes' / 'questions.md'
            block = _render_questions_block(plan.questions)
            if qpath.exists():
                existing = qpath.read_text(encoding='utf-8')
                undo.append(lambda p=qpath, text=existing: p.write_text(text, encoding='utf-8'))
                qpath.write_text(existing.rstrip('\n') + '\n\n' + block, encoding='utf-8')
            else:
                write_new(qpath, '# Open Questions (general)\n\n' + block)

        # 6) Audit trail.
        write_new(root / '.cache' / 'convert_mapping.csv',
                  _render_mapping_csv(plan.mapping_rows))
    except Exception:
        for fn in reversed(undo):
            try:
                fn()
            except Exception:
                pass
        raise


# ── CLI ───────────────────────────────────────────────────────────────────────

def run_convert(archive_root: Path, fha_config: dict, *, apply: bool) -> int:
    mining_dir = archive_root / 'mining'
    try:
        plan = build_plan(archive_root, fha_config, mining_dir)
    except ConvertError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_ERRORS

    if apply:
        try:
            apply_plan(plan, fha_config)
        except ConvertError as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return EXIT_ERRORS
        except OSError as e:
            print(f'ERROR: conversion write failed and was rolled back: {e}', file=sys.stderr)
            return EXIT_FAILURE
        print_plan(plan, applied=True)
        print('Wrote .cache/convert_mapping.csv')
        print('Run `fha index` then `fha lint` to review the imported records.')
    else:
        print_plan(plan, applied=False)
    return EXIT_CLEAN


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'convert-mining',
        help='Migrate a legacy transcript-mining export into conformant records',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_run_convert)


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('--root', metavar='PATH', help='Archive root (contains mining/)')
    p.add_argument('--apply', action='store_true',
                   help='Write the records (default: dry-run plan only)')


def _run_convert(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    return run_convert(archive_root, fha_config, apply=bool(args.apply))


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha convert-mining',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    args = parser.parse_args(argv)
    return _run_convert(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
