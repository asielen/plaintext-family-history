#!/usr/bin/env python3
"""
wikitree.py - fha wikitree: render a curated profile to the WikiTree dialect.

  fha wikitree <P-id> [--out FILE] [--root PATH]

Renders one curated person's profile prose into the user's extended **WikiTree
dialect** (TOOLING §13) - the markup family-tree wikis expect, not vanilla
markdown. The profile `.md` body is the input; the structured index supplies
source citations, person links, and the place/date behind each cited claim.

WHAT IT EMITS
-------------
- **Named-ref reuse.** Every `[S-id]` token in the body becomes a self-closing
  `<ref name="S-id"/>` at the use site; the full `<ref name="S-id">{citation}</ref>`
  definitions are gathered once into the hidden
  `<div name="references" style="display: none">` block at the top (deduplicated,
  in first-use order). `== Sources ==` ends with `<references/>` so the wiki
  renders them.
- **Spacetime spans.** A factual sentence carrying exactly one `[S-id]` whose
  (subject, source) pair resolves to a single dated+placed claim is wrapped in
  `<span class="spacetime" data-loc="{place}" data-date="{ISO}">…</span>` - the
  dialect's machine-readable annotation, emitted from claims instead of by hand.
  Sentences with several citations (e.g. the summary block) are left unwrapped:
  one span can only carry one place/date, so a multi-fact sentence is not forced
  into a single misleading annotation.
- **Person links.** `[P-id]` → `[[{external_ids.wikitree}|{name}]]` when a
  WikiTree id is recorded for that person, else the plain name.
- **Ancestry images.** Ancestry image URLs in a source's `external_links`
  become `{{Ancestry Image|db|id}}`, appended to that source's reference.
- **Template hooks.** Optional claim-type → infobox mappings in
  `tools/wikitree_templates.yaml` (e.g. military service → `{{US Civil War}}`)
  render the configured templates near the top.

Privacy: a `living: true`/`living: unknown` subject is refused, and profile
prose that cites restricted or DNA sources is refused rather than partly
rewritten. WikiTree output is external-facing (AGENTS.md privacy rule). Output
goes to stdout, or `--out FILE`; it is never uploaded.

CODE MAP
--------
  Source data
    _load_templates                  - parse tools/wikitree_templates.yaml
    _ancestry_image_template         - Ancestry URL → {{Ancestry Image|db|id}}
    _source_reference                - one source's citation text + image templates
    _person_link                     - [P-id] → wiki link or plain name
    _spacetime_index                 - (subject, source) → single dated+placed claim

  Rendering
    _convert_heading                 - markdown ## / ### → == / ===
    _render_token, _transform_line, _split_sentences
    _render_templates                - infobox templates from the hooks file
    run_wikitree                     - assemble the full document

  CLI
    _cmd_wikitree, register, _standalone_main
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import yaml

from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    Result,
    configure_utf8_stdout,
    edtf_bounds,
    extract_token_ids,
    extract_tokens,
    fmt_id_display,
    id_type_of,
    is_valid_id,
    normalize_id,
    open_index_db,
    read_record,
    resolve_root_arg,
)

configure_utf8_stdout()

_REQUIRED_TABLES = (
    'persons', 'person_variants', 'person_external', 'sources', 'claims',
    'claim_persons', 'places',
)


# ── The `restricted` marker (SPEC §19, §21) ────────────────────────────────────
# WikiTree is a public-publication path, so it fails closed on anything
# `restricted` - a source, a claim, a person, or a name - with no opt-in. The
# index carries no person/name-level `restricted`, so those are read from the
# record file. Duplicated per export tool (tools never import tools, TOOLING §15).

def _is_restricted_value(value) -> bool:
    """True when a `restricted:` value withholds a record from public output.

    The marker is open (SPEC §19): the plain boolean `true` or any free-text
    type all mean restricted; only absent/false is not. (`read_record` coerces
    booleans to `'true'`/`'false'`.) A public path has no opt-in - even
    `restricted: by-request` is honored - so one truthiness test suffices."""
    return value not in (None, False, '', 'false')

_TEMPLATES_FILE = Path(__file__).parent / 'wikitree_templates.yaml'

_PLACEHOLDER_RE = re.compile(r'^\s*\*?\(none yet\)\*?\s*$', re.I)

_TODO_MARKER_RE = re.compile(r'\(TODO:', re.I)


# ── Template hooks ──────────────────────────────────────────────────────────────

def _load_templates(path: Path = _TEMPLATES_FILE) -> dict:
    """Load the optional claim-type → infobox mapping file. Missing/empty/invalid
    → {} (the feature is opt-in; a bad hooks file must not break the export)."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# ── Ancestry images ─────────────────────────────────────────────────────────────

_ANCESTRY_DBID_RE = re.compile(r'[?&]dbid=(\d+)', re.I)
_ANCESTRY_H_RE = re.compile(r'[?&]h=(\d+)', re.I)
# Newer discovery URLs: /view/{imageId}:{dbId}
_ANCESTRY_VIEW_RE = re.compile(r'/view/(\d+):(\d+)', re.I)


def _ancestry_image_template(url: str) -> str | None:
    """Map a recognized Ancestry image URL to `{{Ancestry Image|db|id}}`, else None."""
    if 'ancestry.' not in url.lower():
        return None
    view = _ANCESTRY_VIEW_RE.search(url)
    if view:
        image_id, db = view.group(1), view.group(2)
        return f'{{{{Ancestry Image|{db}|{image_id}}}}}'
    dbid = _ANCESTRY_DBID_RE.search(url)
    h = _ANCESTRY_H_RE.search(url)
    if dbid and h:
        return f'{{{{Ancestry Image|{dbid.group(1)}|{h.group(1)}}}}}'
    return None


def _external_link_urls(meta: dict) -> list[str]:
    """Flatten a source's `external_links` (list or dict of str) into urls."""
    raw = meta.get('external_links') or []
    urls: list[str] = []
    if isinstance(raw, dict):
        raw = list(raw.values())
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                urls.append(item)
            elif isinstance(item, dict):
                for v in item.values():
                    if isinstance(v, str):
                        urls.append(v)
    elif isinstance(raw, str):
        urls.append(raw)
    return urls


def _source_reference(archive_root: Path, source_row: sqlite3.Row) -> str:
    """Citation text for a source's `<ref>` definition: the record's `citation`
    field (collapsed to one line), falling back to its title, plus any Ancestry
    image templates derived from its `external_links`."""
    citation = ''
    image_templates: list[str] = []
    try:
        rec = read_record(archive_root / source_row['path'])
        meta = rec['meta']
        citation = ' '.join(str(meta.get('citation', '') or '').split())
        for url in _external_link_urls(meta):
            tpl = _ancestry_image_template(url)
            if tpl and tpl not in image_templates:
                image_templates.append(tpl)
    except Exception:
        pass
    if not citation:
        citation = source_row['title'] or fmt_id_display(source_row['id'])
    if image_templates:
        citation = f'{citation} ' + ' '.join(image_templates)
    return citation


# ── Person links & spacetime ────────────────────────────────────────────────────

def _person_info(conn: sqlite3.Connection, pid: str, cache: dict) -> tuple[str | None, list[str], str | None, str | None]:
    """Return (name, name-forms, wikitree_id, living) for a P-id, memoized per run.

    name-forms are the strings that, if they already appear in the prose right
    before the cross-ref token, identify a "Name [P-id]" pattern: the full name,
    its first given word, and any recorded name variants - longest first so the
    fullest match wins.
    """
    if pid in cache:
        return cache[pid]
    row = conn.execute('SELECT name, living FROM persons WHERE id = ?', (pid,)).fetchone()
    name = row['name'] if row else None
    living = row['living'] if row else None
    wt = conn.execute(
        "SELECT ext_id FROM person_external WHERE person_id = ? AND system = 'wikitree'",
        (pid,),
    ).fetchone()
    wikitree_id = wt['ext_id'] if wt and wt['ext_id'] else None
    forms: list[str] = []
    if name:
        forms.append(name)
        parts = name.split()
        if parts:
            forms.append(parts[0])
        for v in conn.execute(
            'SELECT variant FROM person_variants WHERE person_id = ?', (pid,)
        ).fetchall():
            variant = v['variant']
            # Restricted name variants (deadnames, SPEC §18) are excluded from
            # person_variants at index time, so every entry here is public.
            if variant:
                forms.append(variant)
    # Deduplicate, longest first so 'Margaret A. Cole' beats 'Margaret'.
    forms = sorted({f for f in forms if f}, key=len, reverse=True)
    result = (name, forms, wikitree_id, living)
    cache[pid] = result
    return result


def _person_token_form(pid: str, name: str | None, wikitree_id: str | None) -> str:
    """Standalone [P-id] rendering (no preceding name to fold into a link)."""
    if wikitree_id:
        return f'[[{wikitree_id}|{name or fmt_id_display(pid)}]]'
    return name or fmt_id_display(pid)


def _fold_preceding_name(rendered: str, forms: list[str], wikitree_id: str | None) -> str | None:
    """If `rendered` ends with one of the person's name forms (on a word
    boundary), fold the cross-ref into it: linkify the name when a WikiTree id
    exists, else leave the name untouched (the token is redundant display-wise).
    Returns the rewritten `rendered`, or None if no preceding name was found."""
    tail = rendered.rstrip()
    trailing_ws = rendered[len(tail):]
    low = tail.lower()
    for form in forms:
        fl = form.lower()
        if low.endswith(fl):
            start = len(tail) - len(form)
            # Word boundary before the matched name (start-of-text or non-word char).
            if start > 0 and (tail[start - 1].isalnum() or tail[start - 1] == '_'):
                continue
            if wikitree_id:
                head = tail[:start]
                return f'{head}[[{wikitree_id}|{tail[start:]}]]{trailing_ws}'
            return rendered  # name already present; drop the token only
    return None


def _redact_living_name(rendered: str, forms: list[str]) -> str | None:
    """If `rendered` ends with one of the person's name forms, strip it for
    privacy redaction. Returns the stripped text, or None if no match."""
    tail = rendered.rstrip()
    trailing_ws = rendered[len(tail):]
    low = tail.lower()
    for form in forms:
        fl = form.lower()
        if low.endswith(fl):
            start = len(tail) - len(form)
            if start > 0 and (tail[start - 1].isalnum() or tail[start - 1] == '_'):
                continue
            return tail[:start].rstrip() + trailing_ws
    return None


def _spacetime_index(conn: sqlite3.Connection, pid: str) -> dict[str, tuple[str, str]]:
    """Map source_id → (place, ISO date) for sources whose claims about `pid`
    pin down exactly one dated+placed fact.

    Only sources with a *single* such claim qualify: an unambiguous spacetime
    annotation needs one place and one date, and a source contributing several
    placed/dated claims (a census naming residence, occupation, …) can't be
    reduced to one without guessing.
    """
    rows = conn.execute(
        """
        SELECT c.source_id, c.date_edtf, c.date_min, c.place_id, c.place_text
        FROM claim_persons cp
        JOIN claims c ON cp.claim_id = c.id
        WHERE cp.person_id = ?
          AND c.status IN ('accepted', 'needs-review')
          AND c.date_edtf IS NOT NULL AND c.date_edtf != ''
          AND ((c.place_text IS NOT NULL AND c.place_text != '')
               OR (c.place_id IS NOT NULL AND c.place_id != ''))
        """,
        (pid,),
    ).fetchall()

    by_source: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_source.setdefault(r['source_id'], []).append(r)

    place_cache: dict[str, str] = {}
    out: dict[str, tuple[str, str]] = {}
    for sid, claims in by_source.items():
        if len(claims) != 1:
            continue
        c = claims[0]
        place = c['place_text']
        if not place and c['place_id']:
            if c['place_id'] not in place_cache:
                prow = conn.execute(
                    'SELECT name, hierarchy FROM places WHERE id = ?', (c['place_id'],)
                ).fetchone()
                place_cache[c['place_id']] = (prow['name'] or prow['hierarchy']) if prow else ''
            place = place_cache[c['place_id']]
        iso = c['date_min'] or edtf_bounds(c['date_edtf'])[0]
        if place and iso:
            out[sid] = (' '.join(str(place).split()), iso)
    return out


_WIKILINK_TARGET_RE = re.compile(r'\[\[([^\]|]+)(?:\|[^\]]*)?\]\]')


def _resolve_wikilink_ids(conn: sqlite3.Connection, text: str) -> tuple[set[str], set[str]]:
    """Return (source_ids, person_ids) resolved from bare name-style wikilinks.

    extract_token_ids() handles [[S-id]] / [[P-id]] tokens; this catches the
    alias form [[Source Title]] or [[Person Name]] where the target is a title
    or name alias rather than a bare ID.  Each target is looked up in the
    aliases table and split by kind (S → source, P → person)."""
    source_ids: set[str] = set()
    person_ids: set[str] = set()
    for m in _WIKILINK_TARGET_RE.finditer(text):
        target = m.group(1).strip()
        if is_valid_id(target):
            continue  # already handled by extract_token_ids
        row = conn.execute(
            'SELECT canonical_id FROM aliases WHERE alias = ? COLLATE NOCASE LIMIT 1',
            (target,),
        ).fetchone()
        if row is None:
            continue
        cid = row['canonical_id']
        if not cid:
            continue
        kind = id_type_of(cid)
        if kind == 'S':
            source_ids.add(cid)
        elif kind == 'P':
            person_ids.add(cid)
    return source_ids, person_ids


def _restricted_claim_ids(conn: sqlite3.Connection, archive_root: Path) -> set[str]:
    """Claim IDs carrying a per-claim `restricted:` marker in their source record.

    The claims table has no claim-level `restricted` column — the flag lives in
    the source record file. Reads every public (non-index-restricted) source file
    and collects claim IDs where the claim dict's `restricted:` is truthy."""
    out: set[str] = set()
    for row in conn.execute('SELECT id, path, restricted FROM sources').fetchall():
        if row['restricted'] or not row['path']:
            continue
        try:
            rec = read_record(archive_root / row['path'])
        except Exception:
            continue
        for claim in rec.get('claims') or []:
            if not isinstance(claim, dict):
                continue
            cid = normalize_id(str(claim.get('id', '')).strip())
            if cid and _is_restricted_value(claim.get('restricted')):
                out.add(cid)
    return out


def _restricted_source_refs(conn: sqlite3.Connection, archive_root: Path, text: str) -> list[sqlite3.Row]:
    """Non-publishable source tokens cited in the profile body.

    SPEC §21 bars restricted, DNA, and publication_ok=false sources from
    public output. The WikiTree exporter works from curated prose, so
    deleting just the `<ref>` would leave an unsupported public fact behind.
    Failing closed gives the human a precise cleanup list and avoids silent
    leakage. A free-text restricted type (`restricted: by-request`) stores
    `restricted=0` in the index, so cited sources are also read from their
    record files to catch it.
    """
    extra_sids, _ = _resolve_wikilink_ids(conn, text)
    source_ids = sorted({
        t for t in extract_token_ids(text) if id_type_of(t) == 'S'
    } | extra_sids)
    if not source_ids:
        return []
    placeholders = ','.join('?' * len(source_ids))
    rows = conn.execute(
        f"""
        SELECT id, title, source_type, restricted, publication_ok, path
        FROM sources
        WHERE id IN ({placeholders})
        ORDER BY id
        """,
        source_ids,
    ).fetchall()
    flagged: list[sqlite3.Row] = []
    for row in rows:
        bad = (
            (row['restricted'] or 0) != 0
            or (row['source_type'] or '') == 'dna'
            or (row['publication_ok'] is not None and int(row['publication_ok']) == 0)
        )
        if not bad and row['path']:
            try:
                bad = _is_restricted_value(read_record(archive_root / row['path'])['meta'].get('restricted'))
            except Exception:
                bad = False
        if bad:
            flagged.append(row)
    return flagged


def _restricted_person_refs(conn: sqlite3.Connection, archive_root: Path, text: str) -> list[sqlite3.Row]:
    """Restricted person tokens linked in the profile body.

    A `[[P-id]]` link to a person whose record carries a `restricted` marker
    (any value) can't appear in public WikiTree output. The index has no
    person-level `restricted` column, so each linked person's record file is
    read. Returns the offending persons (id + name) for the cleanup message."""
    _, extra_pids = _resolve_wikilink_ids(conn, text)
    person_ids = sorted({
        t for t in extract_token_ids(text) if id_type_of(t) == 'P'
    } | extra_pids)
    if not person_ids:
        return []
    placeholders = ','.join('?' * len(person_ids))
    rows = conn.execute(
        f'SELECT id, name, path FROM persons WHERE id IN ({placeholders}) ORDER BY id',
        person_ids,
    ).fetchall()
    flagged: list[sqlite3.Row] = []
    for row in rows:
        if not row['path']:
            continue
        try:
            value = read_record(archive_root / row['path'])['meta'].get('restricted')
        except Exception:
            continue
        if _is_restricted_value(value):
            flagged.append(row)
    return flagged


# ── Rendering ────────────────────────────────────────────────────────────────────

def _convert_heading(line: str) -> str | None:
    """Markdown heading → WikiTree heading. The H1 (page title) is dropped
    (the wiki page title already carries the name). Returns None to drop the line."""
    m = re.match(r'^(#{1,6})\s+(.*)$', line)
    if not m:
        return line
    level = len(m.group(1))
    text = m.group(2).strip()
    if level == 1:
        return None
    eq = '=' * level
    return f'{eq} {text} {eq}'


def _render_tokens(
    conn: sqlite3.Connection, text: str, used_sources: list[str], person_cache: dict,
) -> str:
    """Render all citation tokens in `text` left to right.

    `[[S-id]]` → self-closing ref (and registered in `used_sources`); `[[P-id]]` →
    a WikiTree link, folded onto a preceding "Name " when the prose already
    names the person (so "Margaret A. Cole [[P-id]]" doesn't render the name
    twice); `[[L-id]]` → place name; anything else → the display id.

    When the human wrote the display *inside* the token (`[[P-id|Margaret Cole]]`),
    that display is explicit - use it as the link text and suppress the
    preceding-name folding (the fold exists only to avoid printing an
    already-present name twice, which doesn't apply when the name is in-token).
    Both bracket forms and the legacy single-bracket `[P-id]` flow through here."""
    out: list[str] = []
    pos = 0
    for pid_raw, display, _fragment, (start, end) in extract_tokens(text):
        out.append(text[pos:start])
        pos = end
        pid = normalize_id(pid_raw)
        kind = id_type_of(pid)
        if kind == 'S':
            if pid not in used_sources:
                used_sources.append(pid)
            out.append(f'<ref name="{fmt_id_display(pid)}"/>')
        elif kind == 'P':
            name, forms, wikitree_id, living = _person_info(conn, pid, person_cache)
            if living in ('true', 'unknown'):
                # Privacy: strip a preceding prose name; never emit the in-token
                # display name either (it is simply not appended).
                redacted = _redact_living_name(''.join(out), forms) if forms else None
                if redacted is not None:
                    out = [redacted]
                out.append('[living person]')
            elif display:
                # Explicit in-token display: use it as the link text, no folding.
                out.append(_person_token_form(pid, display, wikitree_id))
            else:
                folded = _fold_preceding_name(''.join(out), forms, wikitree_id) if forms else None
                if folded is not None:
                    out = [folded]
                else:
                    out.append(_person_token_form(pid, name, wikitree_id))
        elif kind == 'L':
            if display:
                out.append(display)
            else:
                row = conn.execute('SELECT name FROM places WHERE id = ?', (pid,)).fetchone()
                out.append(row['name'] if row and row['name'] else fmt_id_display(pid))
        else:
            out.append(display or fmt_id_display(pid))
    out.append(text[pos:])
    # Collapse runs of spaces left where a redundant cross-ref token was dropped.
    return re.sub(r' {2,}', ' ', ''.join(out))


_SENTENCE_END_RE = re.compile(r'[.!?]\s+')
_ABBREVIATIONS = {
    'mr', 'mrs', 'ms', 'dr', 'jr', 'sr', 'st', 'co', 'vs', 'etc', 'no',
    'rev', 'hon', 'gen', 'col', 'capt', 'sgt', 'lt', 'inc', 'ca', 'fl',
}


def _split_sentences(text: str) -> list[str]:
    """Split prose into sentences, without breaking on initials ("Margaret A.
    Cole") or common abbreviations ("Mrs. Smith"). A naive split-on-period would
    fragment a name mid-token and defeat the "Name [P-id]" cross-ref folding."""
    sentences: list[str] = []
    start = 0
    for m in _SENTENCE_END_RE.finditer(text):
        wm = re.search(r'(\S+)$', text[: m.start()])
        word = wm.group(1) if wm else ''
        if len(word) == 1 and word.isalpha():        # single-letter initial
            continue
        if word.lower().rstrip('.') in _ABBREVIATIONS:
            continue
        sentences.append(text[start: m.end()].strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences or ['']


def _transform_line(
    conn: sqlite3.Connection, line: str, spacetime: dict[str, tuple[str, str]],
    used_sources: list[str], person_cache: dict,
) -> str:
    """Transform one prose line: spacetime-wrap qualifying sentences, then render tokens."""
    out_sentences: list[str] = []
    for sentence in _split_sentences(line):
        sids = [
            t for t in extract_token_ids(sentence) if id_type_of(t) == 'S'
        ]
        rendered = _render_tokens(conn, sentence, used_sources, person_cache)
        if len(sids) == 1 and sids[0] in spacetime:
            loc, date = spacetime[sids[0]]
            # Only annotate when the claim's year actually appears in the sentence.
            # A source can be cited in several sentences while contributing one
            # dated+placed claim; without this guard the (e.g. marriage) date
            # would be stamped onto an unrelated (e.g. birth) sentence that
            # merely shares the citation.
            year = date[:4]
            if year and year in sentence:
                loc_attr = loc.replace('"', '&quot;')
                rendered = (
                    f'<span class="spacetime" data-loc="{loc_attr}" data-date="{date}">'
                    f'{rendered}</span>'
                )
        out_sentences.append(rendered)
    return ' '.join(out_sentences)


def _claim_attr(conn: sqlite3.Connection, claim: sqlite3.Row, attr: str) -> str:
    if attr == 'date':
        return claim['date_edtf'] or ''
    if attr == 'value':
        return claim['value'] or ''
    if attr == 'place':
        if claim['place_text']:
            return claim['place_text']
        if claim['place_id']:
            row = conn.execute('SELECT name FROM places WHERE id = ?', (claim['place_id'],)).fetchone()
            return (row['name'] or '') if row else ''
    return ''


def _render_templates(
    conn: sqlite3.Connection, pid: str, templates: dict,
    restricted_claims: set[str] | None = None,
) -> list[str]:
    """Render infobox templates for the subject from the claim-type hooks file."""
    if not templates:
        return []
    rows = conn.execute(
        """
        SELECT c.id, c.type, c.subtype, c.date_edtf, c.place_id, c.place_text, c.value
        FROM claim_persons cp
        JOIN claims c ON cp.claim_id = c.id
        JOIN sources s ON s.id = c.source_id
        WHERE cp.person_id = ? AND c.status = 'accepted'
          AND COALESCE(s.restricted, 0) = 0
          AND COALESCE(s.source_type, '') != 'dna'
          AND COALESCE(s.publication_ok, 1) != 0
        """,
        (pid,),
    ).fetchall()
    if restricted_claims:
        rows = [r for r in rows if normalize_id(str(r['id'])) not in restricted_claims]
    out: list[str] = []
    for c in rows:
        spec = templates.get(c['type'])
        if not isinstance(spec, dict):
            continue
        name = spec.get('template')
        if not name:
            continue
        fields = spec.get('fields') or {}
        parts = [str(name)]
        if isinstance(fields, dict):
            for wt_field, claim_attr in fields.items():
                val = _claim_attr(conn, c, str(claim_attr))
                if val:
                    parts.append(f'{wt_field}={val}')
        rendered = '{{' + '|'.join(parts) + '}}'
        if rendered not in out:
            out.append(rendered)
    return out


def _wikitree_payload(archive_root: Path, pid: str) -> dict:
    """
    Render the WikiTree-dialect markup for one curated person. Returns:
      {'status': 'ok'|'not-found'|'not-curated'|'living-subject'|'restricted-subject'|
       'restricted-sources'|'restricted-people'|'no-index'|'bad-args',
       'text': str|None, 'messages': [str, ...]}
    """
    if not is_valid_id(pid) or id_type_of(pid) != 'P':
        return {'status': 'bad-args', 'text': None,
                'messages': [f'ERROR: {pid!r} is not a valid P-id.']}

    conn = open_index_db(archive_root, _REQUIRED_TABLES, strict=True)
    if conn is None:
        return {'status': 'no-index', 'text': None, 'messages': []}

    try:
        person = conn.execute(
            'SELECT id, name, tier, living, path, status FROM persons WHERE id = ?', (pid,)
        ).fetchone()
        if person is None:
            return {'status': 'not-found', 'text': None, 'messages': []}
        if person['status'] == 'merged':
            return {'status': 'merged', 'text': None,
                    'messages': [f'{fmt_id_display(pid)} is a merged record - export the active identity instead.']}
        if person['tier'] != 'curated':
            return {'status': 'not-curated', 'text': None,
                    'messages': [f'{fmt_id_display(pid)} is not curated - wikitree exports curated profiles only.']}
        if person['living'] in ('true', 'unknown'):
            return {'status': 'living-subject', 'text': None,
                    'messages': [
                        f'{fmt_id_display(pid)} has living={person["living"]}; '
                        'wikitree refuses living/unknown subjects (external-facing output).'
                    ]}

        rec = read_record(archive_root / person['path'])
        body = rec['body']

        # A restricted subject (any value, including by-request) is refused
        # outright - WikiTree is public-facing, no opt-in (SPEC §21).
        if _is_restricted_value(rec['meta'].get('restricted')):
            return {'status': 'restricted-subject', 'text': None,
                    'messages': [
                        f'{fmt_id_display(pid)} is restricted; WikiTree is public-facing and '
                        'has no opt-in. Remove the restriction or export a different person.'
                    ]}

        restricted_refs = _restricted_source_refs(conn, archive_root, body)
        if restricted_refs:
            items = ', '.join(
                f'{fmt_id_display(r["id"])} ({r["title"] or "untitled"})'
                for r in restricted_refs
            )
            return {'status': 'restricted-sources', 'text': None,
                    'messages': [
                        'WikiTree output is public-facing; remove or rewrite citations '
                        f'to restricted/DNA sources before export: {items}.'
                    ]}

        # A restricted PERSON linked in the prose can't be published either;
        # fail closed with the offending P-ids named for cleanup (same posture
        # as restricted sources), rather than silently dropping the link.
        restricted_people = _restricted_person_refs(conn, archive_root, body)
        if restricted_people:
            items = ', '.join(
                f'{fmt_id_display(r["id"])} ({r["name"] or "unnamed"})'
                for r in restricted_people
            )
            return {'status': 'restricted-people', 'text': None,
                    'messages': [
                        'WikiTree output is public-facing; remove or rewrite links '
                        f'to restricted people before export: {items}.'
                    ]}
        spacetime = _spacetime_index(conn, pid)
        templates = _load_templates()
        claim_restricted = _restricted_claim_ids(conn, archive_root)

        used_sources: list[str] = []
        person_cache: dict = {}
        body_lines: list[str] = []
        for raw in body.splitlines():
            if _PLACEHOLDER_RE.match(raw):
                continue
            if _TODO_MARKER_RE.search(raw):
                continue
            if raw.lstrip().startswith('#'):
                converted = _convert_heading(raw)
                if converted is None:
                    continue
                body_lines.append(converted)
                continue
            if not raw.strip():
                body_lines.append('')
                continue
            # rstrip: a dropped trailing cross-ref token ("… · Calvin [P-id]")
            # can leave a stray space at end of line.
            body_lines.append(_transform_line(conn, raw, spacetime, used_sources, person_cache).rstrip())

        # Reference definitions, in first-use order, for the sources actually cited.
        ref_defs: list[str] = []
        if used_sources:
            placeholders = ','.join('?' * len(used_sources))
            src_rows = {
                r['id']: r for r in conn.execute(
                    f'SELECT id, title, path FROM sources WHERE id IN ({placeholders})',
                    used_sources,
                ).fetchall()
            }
            for sid in used_sources:
                row = src_rows.get(sid)
                fid = fmt_id_display(sid)
                if row is None:
                    ref_defs.append(f'<ref name="{fid}">{fid} (source record not found)</ref>')
                    continue
                citation = _source_reference(archive_root, row)
                ref_defs.append(f'<ref name="{fid}">{citation}</ref>')

        template_lines = _render_templates(conn, pid, templates, claim_restricted)

        out: list[str] = []
        out.append('<div name="references" style="display: none">')
        out.extend(ref_defs)
        out.append('</div>')
        out.append('')
        if template_lines:
            out.extend(template_lines)
            out.append('')

        # Trim leading/trailing blank lines from the transformed body.
        while body_lines and not body_lines[0].strip():
            body_lines.pop(0)
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        out.extend(body_lines)

        # Sources section ends with <references/> (the dialect's render anchor).
        if not any(re.match(r'^==\s*Sources\s*==', ln) for ln in body_lines):
            out.append('')
            out.append('== Sources ==')
        out.append('<references/>')

        text = '\n'.join(out) + '\n'
        messages = []
        if not used_sources:
            messages.append('Note: the profile body cites no sources - references block is empty.')
        return {'status': 'ok', 'text': text, 'messages': messages}
    finally:
        conn.close()


def run_wikitree(archive_root: Path, pid: str) -> Result:
    """Render the WikiTree markup for one curated person; return a Result.

    `data` is the {'status', 'text', 'messages'} payload `_wikitree_payload`
    computes; Result exposes dict-style access (_lib.py), so callers keep reading
    `result['text']` / `result['status']` unchanged.  Producing the markup is
    pure (the `_cmd` layer prints/writes it), so `changed` stays empty.
    """
    payload = _wikitree_payload(archive_root, pid)
    status = payload['status']
    # Mirror _cmd_wikitree's per-status exit codes so headless callers returning
    # Result.exit_code see a refused export as a failure, not a clean 0.
    if status == 'ok':
        exit_code = EXIT_WARNINGS if payload.get('messages') else EXIT_CLEAN
    elif status in ('not-found', 'not-curated'):
        exit_code = EXIT_WARNINGS
    else:  # bad-args, no-index, merged, living-subject, restricted-subject,
           # restricted-sources, restricted-people
        exit_code = EXIT_FAILURE
    return Result(ok=(status == 'ok'), exit_code=exit_code, data=payload)


# ── CLI ──────────────────────────────────────────────────────────────────────────

def _cmd_wikitree(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    pid = normalize_id(getattr(args, 'person_id', '') or '')
    if not pid:
        print('ERROR: a P-id argument is required.', file=sys.stderr)
        return EXIT_FAILURE

    result = run_wikitree(archive_root, pid)
    for m in result['messages']:
        print(m, file=sys.stderr)

    status = result['status']
    if status == 'bad-args':
        return EXIT_FAILURE
    if status == 'no-index':
        return EXIT_FAILURE
    if status == 'merged':
        return EXIT_FAILURE
    if status == 'living-subject':
        return EXIT_FAILURE
    if status == 'restricted-subject':
        return EXIT_FAILURE
    if status == 'restricted-sources':
        return EXIT_FAILURE
    if status == 'restricted-people':
        return EXIT_FAILURE
    if status == 'not-found':
        print(f'{fmt_id_display(pid)}: not found in index.', file=sys.stderr)
        return EXIT_WARNINGS
    if status == 'not-curated':
        return EXIT_WARNINGS

    out = getattr(args, 'out', None)
    if out:
        out_path = Path(out)
        if not out_path.is_absolute():
            out_path = Path.cwd() / out_path
        try:
            out_path.write_text(result['text'], encoding='utf-8')
        except OSError as e:
            print(f'ERROR: could not write {out_path}: {e}', file=sys.stderr)
            return EXIT_FAILURE
        print(f'WikiTree markup written: {out_path}')
    else:
        sys.stdout.write(result['text'])

    return EXIT_WARNINGS if result['messages'] else EXIT_CLEAN


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    p = subs.add_parser(
        'wikitree',
        help='Render a curated profile to the WikiTree dialect (refs, spacetime spans, links).',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('person_id', metavar='P-id', help='Curated person to export.')
    p.add_argument('--out', metavar='FILE', help='Write to FILE (default: stdout).')
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.add_argument('--spec-root', metavar='PATH', help='Spec docs root (accepted for CLI consistency).')
    p.set_defaults(func=_cmd_wikitree)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha wikitree', description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('person_id', metavar='P-id', help='Curated person to export.')
    parser.add_argument('--out', metavar='FILE', help='Write to FILE (default: stdout).')
    parser.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    parser.add_argument('--spec-root', metavar='PATH', help='Spec docs root (accepted for CLI consistency).')
    parser.set_defaults(func=_cmd_wikitree)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
