#!/usr/bin/env python3
"""
gedcom_import.py - fha gedcom import: the Ancestry on-ramp (TOOLING §13a2).

  fha gedcom import <file.ged> [--root PATH]              Dry-run: print the import plan
  fha gedcom import <file.ged> --apply [--root PATH]      Write the records
  fha gedcom import <file.ged> --plan-out FILE            Also write the FULL plan text

Someone leaves Ancestry (or any genealogy program) with a `.ged` file holding
years of work, and the archive's answer must not be "start over."  This tool
files a *foreign* GEDCOM the way this system files everything: the `.ged` file
itself becomes ONE source record, every individual becomes a person stub in
`people/stubs/`, and every assertion in the file becomes a `status: suggested`
claim citing that source with a line anchor back into the filed copy.  Nothing
imported is ever a fact - it is years of *leads*, reviewed gradually through
the normal claim-review gate (`fha claim`, driven by the review-claims skill).

This is the import side of the one-way-bridge contract (TOOLING §13a): the
archive's own GEDCOM *export* is never re-imported as truth (the self-import
guard below refuses a file stamped `HEAD SOUR fha`), but a foreign GEDCOM is a
legitimate on-ramp - it enters as evidence-to-review, never as facts.

Plan-then-apply, one-shot (the convert-mining pattern): the default run parses,
plans, and prints - it writes nothing.  `--apply` writes, registering an undo
before every write and unwinding everything in reverse on any failure, so a
failed apply leaves zero trace.  The audit CSV
(`.cache/gedcom_import/{sha12}.csv`) is the FINAL write: it is both the
xref-to-minted-id mapping and the re-run sentinel, so a rolled-back run leaves
no sentinel and a completed run can never be applied twice.

The one privacy-relevant default this tool writes is the `living:` flag - see
`living_flag_for_import` below.  Everything else enters at the stub/suggested
floor the spec already defines.

Tools never import tools (TOOLING §15): the parser, the mappers, and the
scaffolding here are self-contained re-uses of `_lib` primitives, not imports
of `fha process`/`fha stubs` (and a per-file inbox path would be wrong for a
2,000-person batch anyway).
"""

# ── CODE MAP ──────────────────────────────────────────────────────────────────
#
#  GEDCOM parsing
#    _decode_gedcom_bytes      - bytes → text; UTF-8 only, BOM/UTF-16/ANSEL guards
#    parse_gedcom              - line grammar → a tree of _Node records
#    _Node                     - one GEDCOM line + its children (+ source line no.)
#
#  Field derivation
#    gedcom_date_to_edtf       - GEDCOM date phrase → valid EDTF, or None
#    _parse_gedcom_name        - `Given /Surname/` → (display, given, surname)
#    living_flag_for_import    - THE living: heuristic (owner-flagged default)
#    _birth_year_upper         - latest plausible birth year from an EDTF
#
#  Mapping
#    build_plan                - parse + mint + map everything (no writes)
#    _map_individual           - one INDI → stub fields + claims
#    _map_family               - one FAM → marriage/divorce/relationship claims
#    _scan_existing_persons    - dedupe candidates from people/**/*.md
#
#  Rendering
#    _person_stub_text / _person_filename
#    _render_source_record / _render_claim / _render_audit_csv
#
#  Plan/apply + CLI
#    _plan_lines / print_plan / _preflight_apply / apply_plan
#    run_import / _cmd_import / _standalone_main
#
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import io
import re
import shutil
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    Result,
    configure_utf8_stdout,
    fmt_id_display,
    is_template_file,
    is_valid_edtf,
    load_fha_yaml,
    mint_ids,
    parse_filename,
    path_to_alias,
    read_record,
    resolve_path,
    resolve_root_arg,
)

import yaml

configure_utf8_stdout()

_GEDCOM_DOC_SUBDIR = 'gedcom'
_AUDIT_DIR = '.cache/gedcom_import'
_PLAN_DETAIL_CAP = 20      # stubs/duplicates shown inline before "... and N more"
_PROGRESS_EVERY = 100      # apply prints one progress line per this many stubs
_LIVING_CUTOFF_YEARS = 110

_MONTHS = {'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4, 'MAY': 5, 'JUN': 6,
           'JUL': 7, 'AUG': 8, 'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12}

# INDI event tags this importer reads, mapped to (claim type, value verb).
# Anything not here that still carries a DATE becomes `event` + subtype (the
# §8.2 rule that nothing stalls for lack of a category); anything without a
# date is counted as unread - the filed copy preserves it, so nothing is lost.
_EVENT_MAP: dict[str, tuple[str, str]] = {
    'BIRT': ('birth', 'born'),
    'DEAT': ('death', 'died'),
    'CHR': ('baptism', 'christened'),
    'BAPM': ('baptism', 'baptized'),
    'BURI': ('burial', 'buried'),
    'OCCU': ('occupation', 'occupation'),
    'RESI': ('residence', 'residence'),
    'CENS': ('census', 'census'),
    'EDUC': ('education', 'education'),
    'IMMI': ('immigration', 'immigrated'),
    'EMIG': ('immigration', 'emigrated'),
    'NATU': ('immigration', 'naturalized'),
    '_MILT': ('military', 'military service'),
}

# FAMC PEDI value → relationship subtype (SPEC §8.2). `birth` (and absence)
# is the biological default and stays unwritten; anything unlisted passes
# through as free text - the subtype vocabulary is mostly closed, not sealed.
_PEDI_SUBTYPES = {'adopted': 'adoptive', 'foster': 'foster'}


def _today() -> str:
    return datetime.date.today().isoformat()


class GedcomImportError(Exception):
    """A user-facing refusal (bad file, guard tripped, collision) - exit 2.

    Every message names the cause AND the fix in plain words (AGENTS.md
    next-step rule); nothing has been written when this is raised."""


# ── Slug / YAML helpers (mirrors convert_mining.py; tools never import tools) ──

def _slugify(text: str) -> str:
    text = (text or '').strip().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return slug or 'item'


def _name_slot(text: str) -> str:
    """One person-filename slot: lowercase, underscores, [a-z0-9_] only.

    Mirrors stubs.py's sanitation (SPEC §13 person grammar uses underscores
    where source slugs use hyphens). Capped so a 2,000-stub import can't butt
    into Windows path-length limits on a deep archive root."""
    slot = re.sub(r'[^a-z0-9_]', '', (text or '').strip().lower().replace(' ', '_'))
    return (slot or 'unknown')[:60]


def _yaml_inline(value: str) -> str:
    """Single-line YAML scalar, quoted exactly when the parser needs it."""
    rendered = yaml.safe_dump(
        value, default_flow_style=True, allow_unicode=True, width=10 ** 9,
    ).strip()
    if rendered.endswith('...'):
        rendered = rendered[:-3].strip()
    return rendered


# ── GEDCOM parsing ─────────────────────────────────────────────────────────────

@dataclass
class _Node:
    """One GEDCOM line and its subordinate structure.

    `line` is the 1-based line number in the file - it becomes each claim's
    `anchor: "line N"`, and because the filed copy is byte-for-byte the
    original, the anchor stays true forever. CONC/CONT continuation lines fold
    into `value` and never get nodes of their own."""

    tag: str
    value: str
    xref: str          # '@I1@' when this line defines a record, else ''
    line: int
    children: list['_Node'] = field(default_factory=list)

    def child(self, tag: str) -> '_Node | None':
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def child_value(self, tag: str) -> str:
        c = self.child(tag)
        return c.value.strip() if c else ''

    def children_tagged(self, tag: str) -> list['_Node']:
        return [c for c in self.children if c.tag == tag]


# LEVEL [@XREF@] TAG [VALUE] - the whole 5.5/5.5.1 line grammar. Tags may be
# vendor extensions (leading underscore). VALUE keeps everything after the
# single separating space, including leading spaces beyond it (CONC folding
# depends on byte fidelity).
_GEDCOM_LINE_RE = re.compile(
    r'^(\d+)\s+(?:(@[^@\s]+@)\s+)?([A-Za-z0-9_]+)(?: (.*))?$'
)


def _decode_gedcom_bytes(raw: bytes, path: Path) -> str:
    """Decode a GEDCOM file as UTF-8, refusing anything else with a plain fix.

    v1 is UTF-8-only by design: Ancestry's downloads are already UTF-8, and an
    ANSEL translation table is future work, not a v1 requirement. The guards
    fire BEFORE planning so a wrong-encoding file can never produce a half-read
    plan: a UTF-16 BOM and an undecodable byte stream each get a message naming
    the re-export fix; the HEAD CHAR ANSEL declaration is checked after parsing
    (see build_plan) because ANSEL bytes often happen to decode."""
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        raise GedcomImportError(
            f'{path.name} is UTF-16 encoded, and this importer reads UTF-8 only. '
            'Open the file in your genealogy program and re-export/save it as '
            'UTF-8 - Ancestry\'s downloads are already UTF-8 - then re-run the import.'
        )
    if raw[:3] == b'\xef\xbb\xbf':
        raw = raw[3:]
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError as e:
        raise GedcomImportError(
            f'{path.name} is not UTF-8 text (byte {e.start} is not valid UTF-8 - '
            'older exports often use the ANSEL encoding). Open it in your '
            'genealogy program and re-export/save it as UTF-8 - Ancestry\'s '
            'downloads are already UTF-8 - then re-run the import.'
        )


def parse_gedcom(text: str) -> tuple[list[_Node], list[str]]:
    """Parse GEDCOM text into a list of level-0 record nodes.

    Returns (records, malformed) where `malformed` describes lines that fit no
    grammar (reported as warnings, never fatal - the filed copy keeps them).
    CONC appends to the last node's value with NO space (GEDCOM splits values
    mid-word); CONT appends a newline + text. Both attach to the most recently
    parsed node regardless of level bookkeeping, which is how every real-world
    producer emits them."""
    records: list[_Node] = []
    stack: list[_Node] = []       # stack[level] = the open node at that level
    last: _Node | None = None
    malformed: list[str] = []

    for lineno, rawline in enumerate(text.splitlines(), start=1):
        if not rawline.strip():
            continue
        m = _GEDCOM_LINE_RE.match(rawline.strip('﻿'))
        if not m:
            malformed.append(f'line {lineno}: {rawline.strip()[:60]!r}')
            continue
        level = int(m.group(1))
        xref = m.group(2) or ''
        tag = m.group(3).upper()
        value = m.group(4) or ''

        if tag == 'CONC':
            if last is not None:
                last.value += value
            continue
        if tag == 'CONT':
            if last is not None:
                last.value += '\n' + value
            continue

        node = _Node(tag=tag, value=value, xref=xref, line=lineno)
        if level == 0:
            records.append(node)
            stack = [node]
        elif level <= len(stack):
            parent = stack[level - 1]
            parent.children.append(node)
            del stack[level:]
            stack.append(node)
        else:
            # A level that skips ahead (e.g. 0 → 2) is structurally invalid;
            # report it rather than guessing a parent.
            malformed.append(f'line {lineno}: level jumps to {level}')
            continue
        last = node
    return records, malformed


# ── Date translation (inverting the exporter's grammar, gedcom.py) ────────────

def _one_gedcom_date_to_edtf(s: str, approx: bool = False) -> str | None:
    """One plain GEDCOM date token (`12 JAN 1850`, `JAN 1850`, `1850`) → EDTF.

    `approx` appends `~` at the token's own precision (`1850~`, `1850-01~`),
    which is how ABT/EST/CAL qualify a date they wrap. Returns None for
    anything that doesn't fit - the caller keeps the wording in the claim's
    value instead (SPEC §11: date as written in value, best EDTF in date)."""
    s = s.strip().upper()
    suffix = '~' if approx else ''
    m = re.fullmatch(r'(\d{1,2}) ([A-Z]{3}) (\d{3,4})', s)
    if m and m.group(2) in _MONTHS:
        day, month, year = int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3))
        return f'{year:04d}-{month:02d}-{day:02d}{suffix}'
    m = re.fullmatch(r'([A-Z]{3}) (\d{3,4})', s)
    if m and m.group(1) in _MONTHS:
        month, year = _MONTHS[m.group(1)], int(m.group(2))
        return f'{year:04d}-{month:02d}{suffix}'
    m = re.fullmatch(r'(\d{3,4})', s)
    if m:
        return f'{int(m.group(1)):04d}{suffix}'
    return None


def gedcom_date_to_edtf(raw: str) -> str | None:
    """GEDCOM date phrase → a valid EDTF string, or None to omit `date:`.

    The translation table (inverting the exporter's `_edtf_to_gedcom`):

      12 JAN 1850     → 1850-01-12          ABT/EST/CAL X → X~ at its precision
      JAN 1850        → 1850-01             BEF X         → [..X]
      1850            → 1850                AFT X         → [X..] IF the EDTF
      BET A AND B     → A/B                                 suite accepts the
      FROM A TO B     → A/B                                 after-form, else None

    The AFT arm is a runtime probe rather than a hardcoded No: today's
    `_lib._EDTF_PATTERNS` has no `[X..]` form, so AFT dates omit `date:` and
    keep "after 1850" in the claim value - but if the suite ever grows the
    after-form, this starts emitting it with no code change here.

    Every produced string is validated with `is_valid_edtf` before being
    returned; a failure downgrades to None (omit the date), never an invalid
    record - lint E014 must have nothing to find in an imported archive.
    """
    s = (raw or '').strip()
    if not s:
        return None
    up = s.upper()
    edtf: str | None = None

    m = re.fullmatch(r'(?:ABT|EST|CAL)\.? (.+)', up)
    if m:
        edtf = _one_gedcom_date_to_edtf(m.group(1), approx=True)
    elif up.startswith('BEF '):
        core = _one_gedcom_date_to_edtf(up[4:])
        if core:
            edtf = f'[..{core}]'
    elif up.startswith('AFT '):
        core = _one_gedcom_date_to_edtf(up[4:])
        if core:
            edtf = f'[{core}..]'   # the probe: only survives if the suite validates it
    else:
        m = re.fullmatch(r'BET (.+) AND (.+)', up)
        if not m:
            m = re.fullmatch(r'FROM (.+) TO (.+)', up)
        if m:
            a = _one_gedcom_date_to_edtf(m.group(1))
            b = _one_gedcom_date_to_edtf(m.group(2))
            edtf = f'{a}/{b}' if a and b else None
        else:
            edtf = _one_gedcom_date_to_edtf(up)

    return edtf if edtf and is_valid_edtf(edtf) else None


# ── Names ─────────────────────────────────────────────────────────────────────

def _parse_gedcom_name(raw: str) -> tuple[str, str, str]:
    """GEDCOM `Given /Surname/ suffix` → (display name, given slot, surname slot).

    Inverts the exporter's `_gedcom_name`. Display drops the slashes and joins
    the pieces in order; the slots feed the stub filename grammar. A name with
    no slash form is best-effort split on the last whitespace token (the same
    convention `fha stubs` uses)."""
    raw = ' '.join((raw or '').split())
    if not raw:
        return '', '', ''
    m = re.match(r'^(.*?)\s*/([^/]*)/\s*(.*)$', raw)
    if m:
        given, surname, suffix = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        display = ' '.join(p for p in (given, surname, suffix) if p)
        return display, given or surname, surname
    parts = raw.split()
    if len(parts) == 1:
        return raw, parts[0], ''
    return raw, ' '.join(parts[:-1]), parts[-1]


def _person_filename(given: str, surname: str, pid: str) -> str:
    """`{surname}__{given}_{P-id}.md` per the stub grammar (stubs.py, SPEC §13).

    A surname-less person leads with the double underscore (`__caesar_P-….md`);
    a person with no NAME at all files as `unknown__unknown_{P-id}.md`."""
    if not given and not surname:
        return f'unknown__unknown_{pid}.md'
    surname_slot = _name_slot(surname) if surname else ''
    given_slot = _name_slot(given) if given else 'unknown'
    return f'{surname_slot}__{given_slot}_{pid}.md'


# ── The living: heuristic ─────────────────────────────────────────────────────

def _birth_year_upper(edtf: str | None) -> int | None:
    """Latest plausible birth year encoded in an EDTF value, or None.

    The living heuristic needs "born more than 110 years ago" to be TRUE only
    when the latest possible reading of the date is that old, so the error
    direction stays safe: an interval uses its upper bound, a decade its final
    year, a before-form its cutoff. An after-form or phrase date yields None
    (unbounded - the person could be young), which keeps them `unknown`."""
    if not edtf:
        return None
    s = edtf.strip()
    if s.startswith('[..') and s.endswith(']'):
        s = s[3:-1]
    if '/' in s:
        s = s.split('/', 1)[1]
    s = s.replace('~', '').replace('?', '')
    m = re.match(r'^(\d{3})X', s)
    if m:
        return int(m.group(1)) * 10 + 9
    m = re.match(r'^(\d{4})', s)
    return int(m.group(1)) if m else None


def living_flag_for_import(has_death: bool, birth_edtf: str | None,
                           today_year: int | None = None) -> str:
    """THE `living:` heuristic - the one privacy-relevant default this import writes.

    Rule (the plan's RECOMMENDED option, pending owner sign-off at review):
    a person with a DEAT structure (even a dateless `1 DEAT Y`), or whose
    latest plausible birth year is more than 110 years ago, gets
    `living: false`; everyone else stays at the stub default `unknown`
    (= treated as living by every export, SPEC §19).

    Why not all-`unknown` (the alternative): importing 2,000 people at
    `unknown` redacts the entire tree from every export and site until 2,000
    flags are flipped by hand. This rule mirrors what every mainstream program
    (Ancestry included) computes; its failure mode requires the user's own
    tree to assert a death for a living person, and the residue's error
    direction is safe (dead-marked-unknown = over-redaction, never exposure).

    Deliberately a single isolated predicate so the owner's review can confirm
    or flip it in one place."""
    if has_death:
        return 'false'
    year = _birth_year_upper(birth_edtf)
    if year is not None:
        if today_year is None:
            today_year = datetime.date.today().year
        if today_year - year > _LIVING_CUTOFF_YEARS:
            return 'false'
    return 'unknown'


# ── Plan data model ───────────────────────────────────────────────────────────

@dataclass
class _PersonStub:
    xref: str
    pid: str
    name: str                 # display name ('' when the INDI had no NAME)
    given: str
    surname: str
    variants: list[str]
    sex: str                  # 'M' | 'F' | ''
    living: str               # 'false' | 'unknown'
    birth: str | None         # provisional EDTF
    death: str | None


@dataclass
class _Claim:
    cid: str
    claim_type: str
    value: str
    persons: list[str]
    date: str | None
    place_text: str | None
    confidence: str
    anchor: str
    subtype: str | None = None
    roles: dict | None = None      # {'spouse': [..]} / {'child': pid, 'parent': [..]}
    notes: str | None = None
    fam_xref: str = ''             # '' for INDI-derived claims


@dataclass
class ImportPlan:
    archive_root: Path
    ged_path: Path
    ged_hash: str                  # full sha256 hex
    line_count: int
    sid: str
    slug: str
    doc_dest_path: Path
    doc_dest_alias: str
    record_path: Path
    title: str
    repository: str
    source_date: str | None        # EDTF from HEAD DATE
    export_date_raw: str
    stubs: list[_PersonStub]
    claims: list[_Claim]
    family_count: int
    cited_sources: list[tuple[str, int]]   # (title, times cited)
    duplicates: list[str]                  # pre-rendered report lines
    warnings: list[str]
    unread_lines: int
    unread_top: list[tuple[str, int]]      # (tag, line count), most common first


# ── Mapping ───────────────────────────────────────────────────────────────────

def _event_value(display: str, verb: str, node: _Node, xref: str) -> str:
    """Human-readable claim value: assertion first, GEDCOM xref for traceability.

    The date goes in AS WRITTEN (SPEC §11: the value keeps the source's
    wording; `date:` carries the best EDTF), so an unparseable `AFT 1900` or
    interpreted date is preserved even when the date field is omitted."""
    head = f'{display}: {verb}' if display else verb
    if node.value.strip() and node.value.strip().upper() != 'Y':
        head += f' {node.value.strip()}'
    date_raw = node.child_value('DATE')
    if date_raw:
        head += f' {date_raw}'
    place = node.child_value('PLAC')
    bits = [head]
    if place:
        bits.append(place)
    return f'{", ".join(bits)} (GEDCOM {xref})'


def _source_citation_bits(node: _Node, sour_titles: dict[str, str],
                          sour_use: Counter) -> list[str]:
    """Resolve an event's SOUR children to citation titles (and count usage).

    A pointer (`2 SOUR @S1@`) resolves to that record's TITL (falling back to
    the xref); an inline text SOUR is used verbatim. These are the persona's
    years of citations - they ride each claim's notes as research leads, and
    their presence lifts confidence to medium."""
    bits: list[str] = []
    for s in node.children_tagged('SOUR'):
        ref = s.value.strip()
        if re.fullmatch(r'@[^@\s]+@', ref):
            title = sour_titles.get(ref, ref)
            sour_use[ref] += 1
        else:
            title = ref
            if title:
                sour_use[f'(inline) {title}'] += 1
        if title:
            bits.append(title)
    return bits


def _map_individual(
    indi: _Node, pid: str, cid_pool: list[str],
    sour_titles: dict[str, str], sour_use: Counter,
    consumed: set[int], date_downgrades: list[str],
) -> tuple[_PersonStub, list[_Claim], dict[str, str]]:
    """One INDI record → its person stub, its claims, and its FAMC pedigree map.

    Returns (stub, claims, {fam_xref: PEDI value}) - the pedigree map lets the
    FAM pass stamp `subtype: adoptive`/`foster` on the right parent-child claim.
    Claims take ids from `cid_pool` in order (pre-minted in one batch)."""
    consumed.add(id(indi))
    names = indi.children_tagged('NAME')
    for n in names:
        consumed.add(id(n))
    display, given, surname = ('', '', '')
    variants: list[str] = []
    if names:
        display, given, surname = _parse_gedcom_name(names[0].value)
        for extra in names[1:]:
            v = _parse_gedcom_name(extra.value)[0]
            if v and v != display:
                variants.append(v)

    sex = indi.child_value('SEX').upper()
    sex_node = indi.child('SEX')
    if sex_node:
        consumed.add(id(sex_node))
    if sex not in ('M', 'F'):
        sex = ''

    claims: list[_Claim] = []
    birth_edtf: str | None = None
    death_edtf: str | None = None
    has_death = False
    pedi_map: dict[str, str] = {}

    def _consume_event_children(node: _Node) -> None:
        for tag in ('DATE', 'PLAC', 'TYPE'):
            c = node.child(tag)
            if c:
                consumed.add(id(c))
        for s in node.children_tagged('SOUR'):
            consumed.add(id(s))

    def _event_claim(node: _Node, claim_type: str, verb: str,
                     subtype: str | None = None) -> _Claim:
        consumed.add(id(node))
        _consume_event_children(node)
        date_raw = node.child_value('DATE')
        edtf = gedcom_date_to_edtf(date_raw)
        if date_raw and not edtf:
            date_downgrades.append(f'{indi.xref} {node.tag}: {date_raw!r}')
        cites = _source_citation_bits(node, sour_titles, sour_use)
        notes = f'GEDCOM cites: {"; ".join(cites)}' if cites else None
        return _Claim(
            cid=cid_pool.pop(0), claim_type=claim_type,
            value=_event_value(display, verb, node, indi.xref),
            persons=[pid], date=edtf,
            place_text=node.child_value('PLAC') or None,
            confidence='medium' if cites else 'low',
            anchor=f'line {node.line}', subtype=subtype, notes=notes,
        )

    for node in indi.children:
        tag = node.tag
        if tag in ('NAME', 'SEX'):
            continue
        if tag == 'FAMS':
            consumed.add(id(node))      # families come from the FAM records
            continue
        if tag == 'FAMC':
            consumed.add(id(node))
            pedi = node.child('PEDI')
            if pedi:
                consumed.add(id(pedi))
                pedi_map[node.value.strip()] = pedi.value.strip().lower()
            else:
                pedi_map.setdefault(node.value.strip(), '')
            continue
        if tag == 'NOTE':
            if re.fullmatch(r'@[^@\s]+@', node.value.strip()):
                continue                # pointer note: unread (counted), kept in the file
            consumed.add(id(node))
            text = node.value.strip()
            if not text:
                continue
            first, _, rest = text.partition('\n')
            claims.append(_Claim(
                cid=cid_pool.pop(0), claim_type='note',
                value=f'{display}: {first}' if display else first,
                persons=[pid], date=None, place_text=None,
                confidence='low', anchor=f'line {node.line}',
                notes=rest.strip().replace('\n', ' ') or None,
            ))
            continue
        if tag in _EVENT_MAP:
            claim_type, verb = _EVENT_MAP[tag]
            claim = _event_claim(node, claim_type, verb)
            if tag == 'BIRT':
                birth_edtf = claim.date
            elif tag == 'DEAT':
                has_death = True
                death_edtf = claim.date
            claims.append(claim)
            continue
        if tag == 'EVEN' or node.child('DATE') is not None:
            # §8.2: nothing stalls for lack of a category - a typed or dated
            # structure this importer doesn't know becomes `event` + subtype.
            subtype = node.child_value('TYPE') or tag.lower()
            claims.append(_event_claim(node, 'event', subtype, subtype=subtype))
            continue
        # anything else: unread; the filed copy preserves it.

    stub = _PersonStub(
        xref=indi.xref, pid=pid, name=display, given=given, surname=surname,
        variants=variants, sex=sex,
        living=living_flag_for_import(has_death, birth_edtf),
        birth=birth_edtf, death=death_edtf,
    )
    return stub, claims, pedi_map


def _map_family(
    fam: _Node, xref_pid: dict[str, str], names: dict[str, str],
    cid_pool: list[str], sour_titles: dict[str, str], sour_use: Counter,
    pedi: dict[tuple[str, str], str], consumed: set[int],
    date_downgrades: list[str], warnings: list[str],
) -> list[_Claim]:
    """One FAM record → marriage/divorce claims + one relationship claim per child.

    Roles follow the exporter's convention (`roles: {spouse: [...]}` on couple
    events; `{child: P-c, parent: [...]}` on parent-child edges - E015 requires
    roles on every relationship claim). A pointer to an INDI not in the file
    (a dangling HUSB/WIFE/CHIL) is warned about and skipped, never guessed."""
    consumed.add(id(fam))
    claims: list[_Claim] = []

    def _resolve(tag: str) -> list[str]:
        out = []
        for node in fam.children_tagged(tag):
            consumed.add(id(node))
            ref = node.value.strip()
            if ref in xref_pid:
                out.append(ref)
            else:
                warnings.append(
                    f'family {fam.xref} points at {ref} ({tag}) which is not in the '
                    'file; that link was skipped (the filed copy keeps the reference).')
        return out

    spouses = _resolve('HUSB') + _resolve('WIFE')
    children = _resolve('CHIL')
    spouse_pids = [xref_pid[x] for x in spouses]

    def _couple_event(node: _Node, claim_type: str, phrase: str) -> None:
        consumed.add(id(node))
        for tag in ('DATE', 'PLAC', 'TYPE'):
            c = node.child(tag)
            if c:
                consumed.add(id(c))
        for s in node.children_tagged('SOUR'):
            consumed.add(id(s))
        if len(spouses) < 2:
            warnings.append(
                f'family {fam.xref} has a {node.tag} event but fewer than two known '
                'spouses; the event was skipped (the filed copy keeps it).')
            return
        date_raw = node.child_value('DATE')
        edtf = gedcom_date_to_edtf(date_raw)
        if date_raw and not edtf:
            date_downgrades.append(f'{fam.xref} {node.tag}: {date_raw!r}')
        cites = _source_citation_bits(node, sour_titles, sour_use)
        a, b = (names.get(x) or xref_pid[x] for x in spouses[:2])
        head = f'{a} {phrase} {b}'
        if date_raw:
            head += f', {date_raw}'
        place = node.child_value('PLAC')
        if place:
            head += f', {place}'
        claims.append(_Claim(
            cid=cid_pool.pop(0), claim_type=claim_type,
            value=f'{head} (GEDCOM {fam.xref})',
            persons=list(spouse_pids[:2]), date=edtf,
            place_text=place or None,
            confidence='medium' if cites else 'low',
            anchor=f'line {node.line}',
            roles={'spouse': list(spouse_pids[:2])},
            notes=f'GEDCOM cites: {"; ".join(cites)}' if cites else None,
            fam_xref=fam.xref,
        ))

    marr = fam.child('MARR')
    if marr is not None:
        _couple_event(marr, 'marriage', 'married')
    div = fam.child('DIV')
    if div is not None:
        _couple_event(div, 'divorce', 'divorced (from)')

    for child_xref in children:
        child_pid = xref_pid[child_xref]
        subtype_raw = pedi.get((child_xref, fam.xref), '')
        subtype = _PEDI_SUBTYPES.get(subtype_raw, subtype_raw or None)
        if subtype_raw in ('birth', ''):
            subtype = None      # biological is the unwritten default (§8.2)
        parent_names = ' and '.join(names.get(x) or xref_pid[x] for x in spouses) or 'unknown parents'
        child_name = names.get(child_xref) or child_pid
        nature = f' ({subtype})' if subtype else ''
        claims.append(_Claim(
            cid=cid_pool.pop(0), claim_type='relationship',
            value=f'{child_name} is a child{nature} of {parent_names} (GEDCOM {fam.xref})',
            persons=[child_pid] + spouse_pids, date=None, place_text=None,
            confidence='low', anchor=f'line {fam.line}',
            subtype=subtype,
            roles={'child': child_pid, 'parent': list(spouse_pids)},
            fam_xref=fam.xref,
        ))
    return claims


# ── Dedupe (report, never merge) ──────────────────────────────────────────────

def _norm_name_key(name: str) -> tuple[str, ...]:
    """Casefolded, punctuation-stripped name tokens for dedupe comparison."""
    return tuple(re.sub(r'[^\w\s]', '', (name or '').casefold()).split())


def _year_of(value) -> int | None:
    m = re.search(r'\d{4}', str(value or ''))
    return int(m.group(0)) if m else None


def _scan_existing_persons(archive_root: Path) -> list[dict]:
    """Existing persons' names + variants + provisional birth year, from the tree.

    Scans `people/**/*.md` directly rather than the index - the machine doing a
    first big import may never have built one, and the tree is truth. Templates
    and companion files (research/timeline/...) are skipped; a hand-authored
    record with no ID grammar yet still counts (its `name:` is what matters).
    The birth year read is the record's provisional `birth:` frontmatter - the
    surface a stub or curated record carries on itself. (A year asserted only
    in a source's claims is not seen here; the dedupe is a best-effort report,
    and merge-identities does the real evidence pass later.)"""
    people_root = archive_root / 'people'
    out: list[dict] = []
    if not people_root.is_dir():
        return out
    for path in sorted(people_root.rglob('*.md')):
        if is_template_file(path):
            continue
        parsed = parse_filename(path)
        if parsed is not None and (parsed.get('id_type') != 'P'
                                   or parsed.get('kind') != 'profile'):
            continue
        try:
            rec = read_record(path)
        except Exception:
            continue
        meta = rec.get('meta') or {}
        name = str(meta.get('name') or '').strip()
        if not name:
            continue
        variants = []
        for v in (meta.get('name_variants') or []):
            if isinstance(v, dict):
                v = v.get('value')
            if v:
                variants.append(str(v))
        pid = str(meta.get('id') or '').strip()
        out.append({
            'pid': pid, 'name': name, 'variants': variants,
            'birth_year': _year_of(meta.get('birth')),
            'birth_raw': str(meta.get('birth') or ''),
            'path': path,
        })
    return out


def _find_duplicates(archive_root: Path, stubs: list[_PersonStub]) -> list[str]:
    """Report lines for incoming people who look like someone already filed.

    Match rule (plan §dedupe): normalized name tokens equal AND birth years
    within ±2 - or either year absent and the names match exactly. Import
    proceeds regardless; merging identities is a human decision (the
    merge-identities skill, and plan 16's confirmed write-back)."""
    existing = _scan_existing_persons(archive_root)
    if not existing:
        return []
    by_key: dict[tuple[str, ...], list[dict]] = {}
    for person in existing:
        for n in [person['name']] + person['variants']:
            key = _norm_name_key(n)
            if key:
                by_key.setdefault(key, []).append(person)
    lines: list[str] = []
    for stub in stubs:
        for name in ([stub.name] if stub.name else []) + stub.variants:
            key = _norm_name_key(name)
            for person in by_key.get(key, []):
                in_year = _year_of(stub.birth)
                ex_year = person['birth_year']
                if in_year is not None and ex_year is not None and abs(in_year - ex_year) > 2:
                    continue
                in_b = f' b.{in_year}' if in_year else ''
                ex_b = f' b.{person["birth_raw"]}' if person['birth_raw'] else ''
                try:
                    where = person['path'].relative_to(archive_root)
                except ValueError:
                    where = person['path']
                lines.append(
                    f'{stub.xref} {name}{in_b}  ~  {fmt_id_display(person["pid"]) or "?"} '
                    f'{person["name"]}{ex_b} ({Path(where).as_posix()})')
                break       # one report line per incoming person is enough
            else:
                continue
            break
    return lines


# ── Building the plan ─────────────────────────────────────────────────────────

def _count_unread(records: list[_Node], consumed: set[int]) -> tuple[int, list[tuple[str, int]]]:
    """Tally lines whose tags this importer never read (honesty, not loss).

    A consumed node's unconsumed children count too (a CHAN under an INDI, an
    ADDR under a RESI); everything under an unread node is unread with it. The
    filed copy preserves 100% of the file, so this is a transparency line in
    the plan, never a warning."""
    tally: Counter = Counter()
    total = 0

    def walk(node: _Node, under_unread: str | None) -> None:
        nonlocal total
        if under_unread is not None:
            tally[under_unread] += 1
            total += 1
            for c in node.children:
                walk(c, under_unread)
            return
        if id(node) not in consumed:
            tally[node.tag] += 1
            total += 1
            for c in node.children:
                walk(c, node.tag)
        else:
            for c in node.children:
                walk(c, None)

    for rec in records:
        walk(rec, None)
    return total, tally.most_common(5)


def _read_audit_header(audit_path: Path) -> dict[str, str]:
    """Parse the `# key: value` header lines of an existing audit CSV."""
    out: dict[str, str] = {}
    try:
        for line in audit_path.read_text(encoding='utf-8').splitlines():
            if not line.startswith('#'):
                break
            key, _, value = line.lstrip('# ').partition(':')
            out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def build_plan(archive_root: Path, fha_config: dict, ged_path: Path) -> ImportPlan:
    """Parse the GEDCOM and plan every record to write. No filesystem writes.

    Guard order matters: file-exists → encoding → looks-like-GEDCOM →
    self-import → already-imported sentinel, all BEFORE any minting, so every
    refusal is cheap and stateless. Minting is exactly three `mint_ids` calls
    (S, P, C) - one tree scan each, never one per record - which is what keeps
    a 2,000-person import tractable."""
    if not ged_path.is_file():
        raise GedcomImportError(
            f'{ged_path} does not exist. Check the path (the file Ancestry gives '
            'you is usually called something like "family-tree.ged" in your Downloads).')

    raw = ged_path.read_bytes()
    ged_hash = hashlib.sha256(raw).hexdigest()
    text = _decode_gedcom_bytes(raw, ged_path)
    records, malformed = parse_gedcom(text)

    head = next((r for r in records if r.tag == 'HEAD'), None)
    indis = [r for r in records if r.tag == 'INDI']
    fams = [r for r in records if r.tag == 'FAM']
    sours = [r for r in records if r.tag == 'SOUR' and r.xref]

    if head is None and not indis:
        raise GedcomImportError(
            f'{ged_path.name} does not look like a GEDCOM file (no "0 HEAD" header '
            'and no individual records). A GEDCOM starts with lines like "0 HEAD" - '
            'if this came from a genealogy program, re-export it in GEDCOM format.')

    consumed: set[int] = set()
    repository = ''
    source_date: str | None = None
    export_date_raw = ''
    if head is not None:
        consumed.add(id(head))
        head_sour = head.child('SOUR')
        if head_sour is not None:
            consumed.add(id(head_sour))
            if head_sour.value.strip().lower() == 'fha':
                raise GedcomImportError(
                    'this file was exported from an fha archive - re-importing it '
                    'would duplicate every person as an unreviewed copy. The archive '
                    'is the source of record; the GEDCOM is a one-way bridge out. '
                    'If you meant to import someone ELSE\'s tree, use their export file.')
            name_node = head_sour.child('NAME')
            if name_node is not None:
                consumed.add(id(name_node))
            repository = (name_node.value.strip() if name_node else '') or head_sour.value.strip()
        char_node = head.child('CHAR')
        if char_node is not None:
            consumed.add(id(char_node))
            if char_node.value.strip().upper() == 'ANSEL':
                raise GedcomImportError(
                    f'{ged_path.name} says it is ANSEL-encoded (an older genealogy '
                    'encoding), and this importer reads UTF-8 only. Open it in your '
                    'genealogy program and re-export/save it as UTF-8 - Ancestry\'s '
                    'downloads are already UTF-8 - then re-run the import.')
        date_node = head.child('DATE')
        if date_node is not None:
            consumed.add(id(date_node))
            export_date_raw = date_node.value.strip()
            source_date = gedcom_date_to_edtf(export_date_raw)
        gedc = head.child('GEDC')
        if gedc is not None:
            consumed.add(id(gedc))
            for c in gedc.children:
                consumed.add(id(c))
        note = head.child('NOTE')
        if note is not None:
            consumed.add(id(note))
    for r in records:
        if r.tag == 'TRLR':
            consumed.add(id(r))

    if not indis:
        raise GedcomImportError(
            f'{ged_path.name} contains no individuals (no INDI records) - there is '
            'nothing to import. If your program offered export options, choose the '
            'one that includes people, then re-run.')

    audit_path = archive_root / _AUDIT_DIR / f'{ged_hash[:12]}.csv'
    if audit_path.exists():
        header = _read_audit_header(audit_path)
        raise GedcomImportError(
            f'this GEDCOM was already imported on {header.get("imported", "an earlier date")} '
            f'as {header.get("source_id", "a source record")} - importing it again would '
            'duplicate every person. If you have a NEWER export from Ancestry, import '
            'that file instead; it is a different file and will import cleanly.')

    # GEDCOM SOUR records: titles for claim notes + the Notes-section lead list.
    sour_titles: dict[str, str] = {}
    for s in sours:
        consumed.add(id(s))
        titl = s.child('TITL')
        if titl is not None:
            consumed.add(id(titl))
        sour_titles[s.xref] = (titl.value.strip().replace('\n', ' ') if titl else '') or s.xref

    warnings: list[str] = []
    if malformed:
        shown = '; '.join(malformed[:3])
        warnings.append(
            f'{len(malformed)} line(s) did not parse as GEDCOM and were skipped '
            f'({shown}{", ..." if len(malformed) > 3 else ""}). The filed copy keeps them.')

    # Pass 1 (count) → mint in exactly three batches → pass 2 (assign) would
    # mean mapping twice; instead map with a placeholder pool sized by an exact
    # pre-count. Counting claims without mapping is error-prone, so: mint P-ids
    # first (count is just len(indis)), map INDIs/FAMs against a *deferred* C-id
    # pool, then mint the C batch and fill the ids in one zip.
    sid = mint_ids('S', 1, archive_root)[0]
    pid_list = mint_ids('P', len(indis), archive_root)
    xref_pid = {indi.xref: pid for indi, pid in zip(indis, pid_list)}

    class _DeferredPool(list):
        """Hands out placeholders and counts them; real C-ids land afterward."""

        def __init__(self) -> None:
            super().__init__()
            self.count = 0

        def pop(self, index: int = 0) -> str:  # type: ignore[override]
            self.count += 1
            return f'__C{self.count}__'

    cid_pool = _DeferredPool()
    date_downgrades: list[str] = []
    sour_use: Counter = Counter()

    stubs: list[_PersonStub] = []
    claims: list[_Claim] = []
    pedi: dict[tuple[str, str], str] = {}
    for indi in indis:
        stub, indi_claims, pedi_map = _map_individual(
            indi, xref_pid[indi.xref], cid_pool, sour_titles, sour_use,
            consumed, date_downgrades)
        stubs.append(stub)
        claims.extend(indi_claims)
        for fam_xref, pedi_value in pedi_map.items():
            pedi[(indi.xref, fam_xref)] = pedi_value

    display_names = {s.xref: s.name for s in stubs if s.name}
    for fam in fams:
        claims.extend(_map_family(
            fam, xref_pid, display_names, cid_pool, sour_titles, sour_use,
            pedi, consumed, date_downgrades, warnings))

    if cid_pool.count:
        real_cids = mint_ids('C', cid_pool.count, archive_root)
        for claim, cid in zip(claims, real_cids):
            claim.cid = cid

    if date_downgrades:
        shown = '; '.join(date_downgrades[:3])
        warnings.append(
            f'{len(date_downgrades)} date(s) could not be translated to the '
            f'archive\'s date form and were left out of the date field '
            f'({shown}{", ..." if len(date_downgrades) > 3 else ""}). The original '
            'wording is kept in each claim\'s text, so nothing is lost.')

    cited_sources = sorted(
        ((sour_titles.get(ref, ref.removeprefix('(inline) ')), n)
         for ref, n in sour_use.items()),
        key=lambda kv: (-kv[1], kv[0]))

    slug = _slugify(ged_path.stem)
    documents_root = resolve_path('documents', fha_config, archive_root)
    doc_dest = documents_root / _GEDCOM_DOC_SUBDIR / f'{slug}_{sid}.ged'
    doc_alias = path_to_alias(doc_dest, 'documents', fha_config, archive_root)
    record_path = archive_root / 'sources' / 'other' / f'{slug}_{sid}.md'
    title = f'GEDCOM export: {ged_path.name} ({len(indis)} people'
    title += f', exported {export_date_raw})' if export_date_raw else ')'

    unread_lines, unread_top = _count_unread(records, consumed)

    return ImportPlan(
        archive_root=archive_root, ged_path=ged_path, ged_hash=ged_hash,
        line_count=len(text.splitlines()),
        sid=sid, slug=slug, doc_dest_path=doc_dest, doc_dest_alias=doc_alias,
        record_path=record_path, title=title,
        repository=repository or 'unknown genealogy program',
        source_date=source_date, export_date_raw=export_date_raw,
        stubs=stubs, claims=claims, family_count=len(fams),
        cited_sources=cited_sources,
        duplicates=_find_duplicates(archive_root, stubs),
        warnings=warnings, unread_lines=unread_lines, unread_top=unread_top,
    )


# ── Rendering ─────────────────────────────────────────────────────────────────

def _person_stub_text(stub: _PersonStub, today: str) -> str:
    """Render one person stub (the stubs.py grammar, extended per the plan).

    The provisional `birth:`/`death:` lines are what make an imported tree
    immediately legible - dates show on every stub before any claim is
    reviewed, and the linter's existing needs-a-source tracking is exactly the
    right nudge (SPEC §9, §8.6). No `relationships:` blocks are written: the
    suggested claims are the durable home and the review path (plan decision)."""
    lines = [
        '---',
        f'id: {stub.pid}',
        f'aliases: [{stub.pid}]',
        f'name: {_yaml_inline(stub.name or "unknown")}',
    ]
    if stub.variants:
        rendered = ', '.join(_yaml_inline(v) for v in stub.variants)
        lines.append(f'name_variants: [{rendered}]')
    if stub.sex:
        lines.append(f'sex: {stub.sex}')
    lines.append(f'living: {stub.living}')
    if stub.birth:
        lines.append(f'birth: {_yaml_inline(stub.birth)}')
    if stub.death:
        lines.append(f'death: {_yaml_inline(stub.death)}')
    lines += [f'created: {today}', 'tier: stub', '---', '']
    return '\n'.join(lines)


def _render_claim(claim: _Claim) -> list[str]:
    """Render one suggested claim as block-style YAML for the ## Claims fence."""
    lines = [
        f'- value: {_yaml_inline(claim.value)}',
        f'  id: {claim.cid}',
        f'  type: {claim.claim_type}',
    ]
    if claim.subtype:
        lines.append(f'  subtype: {_yaml_inline(claim.subtype)}')
    lines.append(f'  persons: [{", ".join(claim.persons)}]')
    if claim.roles:
        lines.append('  roles:')
        for role, who in claim.roles.items():
            if isinstance(who, list):
                lines.append(f'    {role}: [{", ".join(who)}]')
            else:
                lines.append(f'    {role}: {who}')
    if claim.date:
        lines.append(f'  date: {_yaml_inline(claim.date)}')
    if claim.place_text:
        lines.append(f'  place_text: {_yaml_inline(claim.place_text)}')
    lines.append('  status: suggested')
    lines.append(f'  confidence: {claim.confidence}')
    lines.append(f'  anchor: {_yaml_inline(claim.anchor)}')
    if claim.notes:
        lines.append(f'  notes: {_yaml_inline(claim.notes)}')
    return lines


def _render_source_record(plan: ImportPlan, today: str) -> str:
    """The ONE source record every imported claim cites (SPEC §14).

    Deliberate shapes, all from the plan doc: `source_type: other` +
    `subtype: gedcom` (adding a `gedcom` source type to §14's vocabulary is
    offered as a logged decision, not assumed); `source_class: derivative` (a
    compiled tree, not a record); NO `people:` frontmatter (2,000 link entries
    is an unusable cross-link surface, and the claims carry every person
    reference); one big `## Claims` block (SPEC §14: claims are queried through
    the index and reviewed in filtered passes, never read linearly)."""
    lines = [
        '---',
        f'id: {plan.sid}',
        f'title: {_yaml_inline(plan.title)}',
        'source_type: other',
        'subtype: gedcom',
        'source_class: derivative',
        f'repository: {_yaml_inline(plan.repository)}',
    ]
    if plan.source_date:
        lines.append(f'source_date: {_yaml_inline(plan.source_date)}')
    lines += [
        'citation: >',
        f'  {plan.ged_path.name}, a GEDCOM family-tree export from {plan.repository}'
        f'{", exported " + plan.export_date_raw if plan.export_date_raw else ""};'
        f' imported by fha gedcom import on {today}.',
        'files:',
        f'  - file: {_yaml_inline(plan.doc_dest_alias)}',
        '    role: original',
        f'    original_filename: {_yaml_inline(plan.ged_path.name)}',
        f'created: {today}',
        '---',
        '',
        '## Claims',
        '```yaml',
    ]
    for i, claim in enumerate(plan.claims):
        if i:
            lines.append('')
        lines += _render_claim(claim)
    lines.append('```')
    lines += [
        '',
        '## AI Passes',
        '```yaml',
        f'- date: {today}',
        '  model: none (deterministic tool)',
        '  harness: fha gedcom import',
        f'  task: {_yaml_inline(f"Import GEDCOM export {plan.ged_path.name} ({len(plan.stubs)} persons, {plan.family_count} families, {len(plan.claims)} suggested claims)")}',
        '  outputs: [source-record, person-stubs, suggested-claims]',
        '  human_reviewed: false',
        '```',
        '',
        '## Notes',
        f'Imported from {plan.ged_path.name} with `fha gedcom import` on {today}: '
        f'{len(plan.stubs)} individuals, {plan.family_count} families, '
        f'{len(plan.claims)} assertions - every one filed as a suggested claim '
        'above, citing this record with a line anchor into the filed copy. '
        'Nothing here is a reviewed fact yet.',
    ]
    if plan.cited_sources:
        lines += [
            '',
            f'This export cites {len(plan.cited_sources)} databases/collections. '
            'They are research leads - find the original records to source these '
            'claims properly.',
        ]
        for title, count in plan.cited_sources:
            plural = 's' if count != 1 else ''
            lines.append(f'- {title} (cited by {count} event{plural})')
    lines.append('')
    return '\n'.join(lines)


def _render_audit_csv(plan: ImportPlan, today: str) -> str:
    """The audit CSV: sentinel + xref→minted-id mapping (final write on apply).

    `#`-prefixed header lines carry the file identity (name, full hash, date,
    S-id) the re-run guard reads; body rows map every GEDCOM handle to what it
    minted. Xrefs are file-local handles, not durable identities - this CSV is
    their only home (they are deliberately NOT written into `external_ids:`)."""
    buf = io.StringIO()
    buf.write(f'# file: {plan.ged_path.name}\n')
    buf.write(f'# sha256: {plan.ged_hash}\n')
    buf.write(f'# imported: {today}\n')
    buf.write(f'# source_id: {plan.sid}\n')
    writer = csv.writer(buf)
    writer.writerow(['gedcom_xref', 'minted_id', 'kind', 'note'])
    writer.writerow(['(file)', plan.sid, 'source', plan.title])
    for stub in plan.stubs:
        writer.writerow([stub.xref, stub.pid, 'person', stub.name or 'unknown'])
    fam_claims: dict[str, list[str]] = {}
    for claim in plan.claims:
        if claim.fam_xref:
            fam_claims.setdefault(claim.fam_xref, []).append(claim.cid)
    for fam_xref, cids in fam_claims.items():
        writer.writerow([fam_xref, ' '.join(cids), 'family',
                         f'{len(cids)} claim(s) from this family record'])
    return buf.getvalue()


# ── Plan output ───────────────────────────────────────────────────────────────

def _stub_plan_line(stub: _PersonStub) -> str:
    bits = []
    if stub.birth:
        bits.append(f'b.{stub.birth}')
    bits.append(f'living: {stub.living}')
    return f'{stub.xref} {stub.name or "(no name)"} ({", ".join(bits)}) -> {stub.pid}'


def _plan_lines(plan: ImportPlan, *, applied: bool, capped: bool = True) -> list[str]:
    """The plan text, shared by stdout (capped) and --plan-out (full).

    Headline counts first, then capped detail - a 2,000-person plan must be
    skimmable in a terminal. The living-heuristic counts are printed because
    that default is the one privacy-relevant thing this import decides."""
    cap = _PLAN_DETAIL_CAP if capped else len(plan.stubs)
    head = ('Applied GEDCOM import' if applied
            else 'GEDCOM import plan (dry-run - use --apply to write)')
    living_false = sum(1 for s in plan.stubs if s.living == 'false')
    living_unknown = len(plan.stubs) - living_false
    lines = [
        head,
        f'  File: {plan.ged_path.name} ({plan.line_count:,} lines, '
        f'sha256 {plan.ged_hash[:12]})',
        f'  Plan: {len(plan.stubs):,} persons, {plan.family_count:,} families, '
        f'{len(plan.claims):,} suggested claims, {len(plan.cited_sources):,} cited '
        f'databases, {len(plan.duplicates):,} possible duplicates',
        f'  Living flags: {living_false:,} marked living: false (a death record, or '
        f'born more than {_LIVING_CUTOFF_YEARS} years ago); {living_unknown:,} left '
        'living: unknown (treated as living by every export)',
        f'  Source record: {_rel_display(plan.record_path, plan.archive_root)}',
        f'  Filed copy:    {plan.doc_dest_alias} (the original file is not touched)',
    ]
    lines.append(f'  Person stubs (people/stubs/): {len(plan.stubs):,}')
    for stub in plan.stubs[:cap]:
        lines.append(f'    {_stub_plan_line(stub)}')
    if len(plan.stubs) > cap:
        lines.append(f'    ... and {len(plan.stubs) - cap:,} more '
                     '(use --plan-out FILE to write the full plan)')
    if plan.duplicates:
        lines.append('')
        lines.append(f'  Possible matches with people already in your archive '
                     f'({len(plan.duplicates)}):')
        for dup in plan.duplicates[:cap]:
            lines.append(f'    {dup}')
        if len(plan.duplicates) > cap:
            lines.append(f'    ... and {len(plan.duplicates) - cap:,} more')
        lines.append('  These will still be imported as NEW people - merging '
                     'identities is a human decision.')
        lines.append('  After import, ask "are these the same person?" '
                     '(the merge-identities skill).')
    if plan.unread_lines:
        top = ', '.join(f'{tag} x{n}' for tag, n in plan.unread_top)
        lines.append('')
        lines.append(f'  {plan.unread_lines:,} line(s) carried tags this importer does '
                     f'not read ({top}) - the original file is kept, nothing is lost.')
    return lines


def _rel_display(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def print_plan(plan: ImportPlan, *, applied: bool) -> None:
    for line in _plan_lines(plan, applied=applied):
        print(line)
    for w in plan.warnings:
        print(f'  WARNING: {w}', file=sys.stderr)


_CLOSING_POSTURE = """\
Imported - not yet verified. {claims:,} statements from your tree are filed as
'suggested' claims citing this GEDCOM. You do not need to review them all,
ever: they surface gradually in `fha report` and the daily briefing, and
facts join timelines, exports, and the family tree as you accept them.
Start close to home: "review the claims about {first_person}".
Next: fha index, then fha lint."""


# ── Apply ─────────────────────────────────────────────────────────────────────

def _planned_stub_path(plan: ImportPlan, stub: _PersonStub) -> Path:
    return (plan.archive_root / 'people' / 'stubs'
            / _person_filename(stub.given, stub.surname, stub.pid))


def _preflight_apply(plan: ImportPlan) -> None:
    """Refuse the apply before ANY write when it could collide or repeat.

    The same-hash audit sentinel was already checked at plan time; it is
    re-checked here (cheap) so a plan object can never be applied twice in one
    process either. Destination collisions list the first five - a collision
    usually means a resumed partial run or a hand-placed file, both of which
    deserve eyes, not an overwrite."""
    audit_path = plan.archive_root / _AUDIT_DIR / f'{plan.ged_hash[:12]}.csv'
    if audit_path.exists():
        header = _read_audit_header(audit_path)
        raise GedcomImportError(
            f'this GEDCOM was already imported on {header.get("imported", "an earlier date")} '
            f'as {header.get("source_id", "a source record")} - importing it again would '
            'duplicate every person. If you have a NEWER export from Ancestry, import '
            'that file instead; it is a different file and will import cleanly.')

    destinations = [_planned_stub_path(plan, s) for s in plan.stubs]
    destinations += [plan.doc_dest_path, plan.record_path]
    conflicts = [p for p in destinations if p.exists()]
    if conflicts:
        shown = ', '.join(_rel_display(p, plan.archive_root) for p in conflicts[:5])
        if len(conflicts) > 5:
            shown += f', ... ({len(conflicts)} total)'
        raise GedcomImportError(
            f'a planned destination already exists: {shown}. Nothing was written. '
            'Move or rename the existing file(s), or re-run the dry-run plan to '
            'see everything this import would create.')


def _write_text(path: Path, text: str) -> None:
    """The one text-write seam (tests inject failure here to prove rollback)."""
    path.write_text(text, encoding='utf-8')


def apply_plan(plan: ImportPlan, progress=print) -> list[str]:
    """Write everything, or nothing: every write registers its undo first.

    Write order: filed .ged copy → person stubs (one progress line per
    {_PROGRESS_EVERY}) → the source record → the audit CSV LAST (so a
    rolled-back run leaves no re-run sentinel). Any exception unwinds every
    registered undo in reverse; the caller translates the failure. Returns the
    list of written paths for Result.changed."""
    root = plan.archive_root
    _preflight_apply(plan)
    today = _today()
    undo: list = []
    written: list[str] = []

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
        # Undo registered BEFORE the write: a write that fails partway (disk
        # full) can still leave a partial file behind.
        undo.append(lambda p=path: p.unlink(missing_ok=True))
        _write_text(path, text)
        written.append(str(path))

    try:
        ensure_parent(plan.doc_dest_path)
        undo.append(lambda p=plan.doc_dest_path: p.unlink(missing_ok=True))
        shutil.copy2(plan.ged_path, plan.doc_dest_path)
        written.append(str(plan.doc_dest_path))

        total = len(plan.stubs)
        for i, stub in enumerate(plan.stubs, start=1):
            write_new(_planned_stub_path(plan, stub), _person_stub_text(stub, today))
            if i % _PROGRESS_EVERY == 0 and i < total:
                progress(f'  wrote {i:,}/{total:,} person stubs...')

        write_new(plan.record_path, _render_source_record(plan, today))
        write_new(root / _AUDIT_DIR / f'{plan.ged_hash[:12]}.csv',
                  _render_audit_csv(plan, today))
    except Exception:
        for fn in reversed(undo):
            try:
                fn()
            except Exception:
                pass
        raise
    return written


# ── Engine ────────────────────────────────────────────────────────────────────

def _check_plan_out(plan_out: str, archive_root: Path) -> Path:
    """Validate the --plan-out destination before planning.

    Refused inside the archive root except the top-level `out/` directory
    (packet's guard): a stray plan .txt inside `sources/` or `notes/` would be
    picked up by search/index passes as if it were archive material."""
    out_path = Path(plan_out)
    if not out_path.is_absolute():
        out_path = Path.cwd() / out_path
    try:
        rel = out_path.resolve().relative_to(archive_root.resolve())
    except ValueError:
        rel = None
    if rel is not None and rel.parts and rel.parts[0] != 'out':
        raise GedcomImportError(
            f'--plan-out {plan_out} is inside your archive\'s {rel.parts[0]}/ folder. '
            'Write it outside the archive, or into the archive\'s out/ folder, so '
            'the plan text is never mistaken for an archive record.')
    return out_path


def run_import(archive_root: Path, fha_config: dict, ged_path: Path, *,
               apply: bool = False, plan_out: str | None = None) -> Result:
    """Plan (and optionally apply) a GEDCOM import; return a Result.

    Like convert-mining, the plan is printed inline - the human preview IS this
    intake tool's surface - and the Result carries the structured outcome:
    `data` = {'applied', 'persons', 'families', 'claims', 'cited_sources',
    'duplicates', 'warnings', 'source_id', 'audit_csv'}; `changed` lists every
    written file on apply (stub paths, source record, filed .ged copy, audit
    CSV) and is empty on dry-run.

    Exit arms (the CLI returns Result.exit_code unchanged):
      0 clean plan/apply · 1 completed with warnings · 2 refusal before/without
      writes (GedcomImportError) · 3 write failure during apply - everything
      rolled back, and the message says so (the catch-all arm: the one thing
      the user must know is that nothing needs cleanup)."""
    try:
        out_path = _check_plan_out(plan_out, archive_root) if plan_out else None
        plan = build_plan(archive_root, fha_config, ged_path)
    except GedcomImportError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return Result(ok=False, exit_code=EXIT_ERRORS)

    changed: list[str] = []
    applied = False
    if apply:
        try:
            changed = apply_plan(plan)
            applied = True
        except GedcomImportError as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return Result(ok=False, exit_code=EXIT_ERRORS)
        except Exception as e:  # noqa: BLE001 - the rolled-back catch-all arm
            print(
                f'ERROR: import failed and every write was rolled back: {e}. '
                'Nothing needs cleanup - fix the cause and re-run (the re-run '
                'guard will not trip; the audit file is written last).',
                file=sys.stderr)
            return Result(ok=False, exit_code=EXIT_FAILURE)

    print_plan(plan, applied=applied)

    if out_path is not None:
        full_text = '\n'.join(_plan_lines(plan, applied=applied, capped=False)
                              + [f'WARNING: {w}' for w in plan.warnings]) + '\n'
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(full_text, encoding='utf-8')
            print(f'Full plan written: {out_path}')
        except OSError as e:
            print(f'ERROR: could not write --plan-out {out_path}: {e}', file=sys.stderr)
            return Result(ok=False, exit_code=EXIT_ERRORS)

    if applied:
        first = next((s.name for s in plan.stubs if s.name), 'your closest ancestor')
        print()
        print(_CLOSING_POSTURE.format(claims=len(plan.claims), first_person=first))

    exit_code = EXIT_WARNINGS if plan.warnings else EXIT_CLEAN
    return Result(
        exit_code=exit_code, changed=changed,
        data={
            'applied': applied,
            'persons': len(plan.stubs),
            'families': plan.family_count,
            'claims': len(plan.claims),
            'cited_sources': len(plan.cited_sources),
            'duplicates': list(plan.duplicates),
            'warnings': list(plan.warnings),
            'source_id': plan.sid,
            'audit_csv': str(plan.archive_root / _AUDIT_DIR / f'{plan.ged_hash[:12]}.csv'),
        })


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cmd_import(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha gedcom import')
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    ged_path = Path(args.ged_file)
    if not ged_path.is_absolute():
        ged_path = Path.cwd() / ged_path
    return run_import(
        archive_root, fha_config, ged_path,
        apply=bool(args.apply), plan_out=getattr(args, 'plan_out', None),
    ).exit_code


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('ged_file', metavar='FILE.ged',
                   help='The GEDCOM file to import (e.g. an Ancestry tree download).')
    p.add_argument('--apply', action='store_true',
                   help='Write the records (default: print the dry-run plan only).')
    p.add_argument('--plan-out', metavar='FILE', dest='plan_out',
                   help='Also write the FULL (uncapped) plan text to FILE - refused '
                        'inside the archive except its out/ folder.')
    p.add_argument('--root', metavar='PATH',
                   help='Archive root (auto-detected if omitted).')
    p.add_argument('--spec-root', metavar='PATH',
                   help='Spec docs root (accepted for CLI consistency).')


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha gedcom import',
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    parser.set_defaults(func=_cmd_import)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
