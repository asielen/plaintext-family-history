#!/usr/bin/env python3
"""
gedcom_import.py - fha gedcom import: import a family tree file such as an Ancestry export (TOOLING §13a2).

  fha gedcom import <file.ged> [--root PATH]              Dry-run: print the import plan
  fha gedcom import <file.ged> --apply [--root PATH]      Write the records
  fha gedcom import <file.ged> --plan-out FILE            Also write the FULL plan text
  fha gedcom import <file.ged> --as-new-tree              A newer download as a separate tree

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

A newer download of a tree already imported is not imported again from
scratch, which would make every person a second time. It is planned as an
update: the people are matched to the records the earlier import made, and
only new people and statements the tree changed are listed. A person who
cannot be matched for certain is listed with the reason and nothing is planned
for them. The earlier import is found from its source record and its filed
copy, never from `.cache` alone. This plan can be read but not applied yet
(`--apply` refuses it); see the "A newer download" section below.

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
#    parse_gedcom              - line grammar → a tree of _Node records (+ joined level-less lines)
#    _clean_vendor_markup      - MyHeritage/double-encoded HTML entities and <br> → plain text
#    _Node                     - one GEDCOM line + its children (+ source line no.)
#
#  Photo links (listed, never written)
#    _collect_photo_links      - OBJE/FILE under each person → _PhotoLink (web links by host only)
#    _match_photo_links        - read-only match against .cache/photos.sqlite (path, then file name)
#    _photo_plan_lines / _photo_notes_lines / _photo_handoff_text - plan, source Notes, after-apply hint
#
#  Field derivation
#    gedcom_date_to_edtf       - GEDCOM date phrase → valid EDTF, or None
#    _translate_gedcom_date    - strict grammar first, then the free-form fallback
#    _loose_date_to_edtf       - typed date (#293) via normalize_date, YYYY-NN + D/M guards
#    _strict_gedcom_date_to_edtf - the GEDCOM 5.5.1 grammar (ABT/BEF/AFT/BET/FROM)
#    _edtf_ymd                 - numeric ordering key for range sanity checks
#    _parse_gedcom_name        - `Given /Surname/ Suffix` (+ `2 NSFX`) → (display, given, surname)
#    _with_nsfx                - fold a `2 NSFX` value into a parsed name, once
#    _oneline                  - CONT-folded HEAD value → one line (YAML-safe)
#    living_flag_for_import    - THE living: heuristic (owner-flagged default)
#    _birth_year_upper         - latest plausible birth year from an EDTF
#
#  Mapping
#    build_plan                - parse + mint + map everything (no writes)
#    _map_file: every INDI then every FAM of one file (no ids minted)
#    _map_individual           - one INDI → stub fields + claims
#    _map_family               - one FAM → marriage/divorce/relationship claims
#    _scan_existing_persons    - dedupe candidates from people/**/*.md
#    _birth_window             - (earliest, latest) birth year for the dedupe overlap test
#
#  A newer download of a tree already imported (plan only)
#    _gedcom_source_paths: the earlier imports' source records, found by what they say
#    _read_prior_imports: each earlier import, with its people and its statements
#    _read_filed_copy: the earlier download itself, re-read from documents/
#    _audit_people: the cache's record-number list (fills gaps only)
#    _same_tree: is this file a newer download of an earlier import?
#    _match_people: who is who (certain, or held with the reason)
#    _number_doubt: when the same record number is not the same person
#    _new_fact / _old_fact: one statement in its comparison shape
#    _judge: same / decided / new / more detail / changed
#    _date_relation / _place_relation: the two comparisons "changed" turns on
#    _plan_update: turn a mapped plan into "only what changed"
#    _reimport_question_block: the questions.md entry a changed statement gets
#
#  Rendering
#    _person_stub_text / _person_filename
#    _render_source_record / _render_claim / _render_audit_csv
#
#  Plan/apply + CLI
#    _plan_lines / print_plan
#    _update_plan_lines: the plan text for a newer download
#    _duplicate_lines / _file_note_lines: plan parts both kinds of plan share
#    _missing_ancestors / _unreachable_anchor - dead-drive-safe parent walks
#    _preflight_apply          - sentinel/collision/reachability refusals, pre-write
#    _require_valid_frontmatter - render guard: never write a corrupt record
#    apply_plan / _write_plan_out
#    run_import / _cmd_import / _standalone_main
#
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import html
import html.entities
import io
import re
import shutil
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    PHOTO_EXTENSIONS,
    render_person_body_scaffold,  # the full §16 profile body every new person record gets
    link_list_lines,
    normalize_id,
    stub_slug_name,
    surname_frontmatter_line,
    person_alias_mirror,
    yaml_alias_entry,
    person_name_slot,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    Result,
    claim_person_pids,
    claim_role_pids,
    claim_spouse_pids,
    configure_utf8_stdout,
    edtf_bounds,
    fmt_id_display,
    humanize_edtf,
    is_template_file,
    is_valid_edtf,
    is_working_copy,
    LinkWriter,
    link_entry,
    link_writer,
    load_fha_yaml,
    mint_ids,
    normalize_date,
    normalize_place_text,
    parse_filename,
    path_to_alias,
    photoindex_status,
    person_alias_map,
    read_record,
    resolve_path,
    resolve_root_arg,
    strip_generational_suffix,
    safe_load_yaml,
    shell_quote,
    fha_launcher,
    sqlite_readonly_uri,
    through_survivors,
    yaml_inline,
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

# Administrative audit stamps most programs put on every record (CHAN = last
# changed, CREA = created). They carry a DATE but are metadata about the file,
# never events in a person's life - they stay UNREAD (counted honestly in the
# plan's unread tally, preserved in the filed copy), and must never fall into
# the dated-structure `event` fallback below.
_ADMIN_TAGS = {'CHAN', 'CREA'}


def _today() -> str:
    return datetime.date.today().isoformat()


class GedcomImportError(Exception):
    """A user-facing refusal (bad file, guard tripped, collision) - exit 2.

    Every message names the cause AND the fix in plain words (AGENTS.md
    next-step rule); nothing has been written when this is raised."""


class GedcomEnvironmentError(Exception):
    """This machine cannot complete the apply right now - exit 3.

    Distinct from GedcomImportError (a problem with the file or the request,
    exit 2): the file and the plan are fine, but the environment is missing a
    piece - typically the documents root lives on an external drive that is
    not plugged in. Nothing has been written when this is raised, and the
    message names the unreachable root and the fix."""


# ── Slug / YAML helpers (mirrors convert_mining.py; tools never import tools) ──

def _slugify(text: str) -> str:
    text = (text or '').strip().lower()
    slug = re.sub(r'[^a-z0-9]+', '-', text).strip('-')
    return slug or 'item'


def _name_slot(text: str) -> str:
    """One person-filename slot: lowercase, underscores, [a-z0-9_] only.

    Mirrors stubs.py's sanitation (SPEC §13 person grammar uses underscores
    where source slugs use hyphens). Capped so a 2,000-stub import can't butt
    into Windows path-length limits on a deep archive root.

    Accented letters fold to their base letters first (`_lib.fold_accents`,
    issue #279), the same fold `stub_slug_name` applies, so an imported
    "Wiśniewska" files as `wisniewska` rather than `winiewska`. Built by
    `_lib.person_name_slot`, so a word that is only punctuation never
    leaves a stray underscore that lint's filename check would refuse."""
    return (person_name_slot(text) or 'unknown')[:60].rstrip('_')


# The one quoting rule every writer shares (`_lib.yaml_inline`): a title or
# name holding ` #` comes out quoted, so YAML never reads its tail as a comment.
_yaml_inline = yaml_inline


def _oneline(text: str) -> str:
    """Collapse a CONT/CONC-folded value to one whitespace-normalized line.

    A CONT under a HEAD-level value (`1 SOUR` / `2 NAME` / `1 DATE`) is legal
    GEDCOM but purely cosmetic - and those values are interpolated into the
    rendered source record's `citation: >` folded scalar, where a raw newline
    would emit a column-0 continuation line and corrupt the whole frontmatter.
    Flattening at read time keeps every downstream render single-line-safe."""
    return ' '.join((text or '').split())


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


# The only tags GEDCOM (5.5.1 and 7.0) allows under a NOTE or TEXT. A line one
# level below a NOTE with any other "tag" is a broken note line (see parse_gedcom).
_NOTE_CHILD_TAGS = frozenset({'CONC', 'CONT', 'SOUR', 'REFN', 'RIN', 'CHAN',
                              'LANG', 'MIME', 'TRAN'})

# Signs a broken level number can carry ("-1 NAME", "+1 NAME", a typographic
# minus or dash pasted from a word processor).
_LEVEL_SIGNS = '-+−‐‑‒–—﹣＋－'


# After a sign or an odd space: digits, then a GEDCOM tag ("NAME", "@I1@ INDI").
# The tag must be a real GEDCOM 5.5.1/7.0 tag or start with "_", so note prose
# such as "-1920 I started school" or "-1942 WWII took both brothers" is prose,
# not a broken level.
_BROKEN_LEVEL_RE = re.compile(
    r'\d+\s+(?:@[^@\s]+@\s+)?(_[A-Z0-9_]+|[A-Z][A-Z0-9]{1,5})(?:\s|$)')
_GEDCOM_TAGS = frozenset('''
    ABBR ADDR ADOP ADR1 ADR2 ADR3 AFN AGE AGNC ALIA ANCE ANCI ANUL ASSO AUTH
    BAPL BAPM BARM BASM BIRT BLES BURI CALN CAST CAUS CENS CHAN CHAR CHIL CHR
    CHRA CITY CONC CONF CONL CONT COPR CORP CREM CROP CTRY DATA DATE DEAT DESC
    DESI DEST DIV DIVF DSCR EDUC EMAIL EMIG ENDL ENGA EVEN EXID FACT FAM FAMC
    FAMS FAX FCOM FILE FONE FORM GEDC GIVN GRAD HEAD HEIGHT HUSB IDNO IMMI INDI
    LANG LATI LEFT LONG MAP MARB MARC MARL MARR MARS MEDI MIME NAME NATI NATU
    NCHI NICK NMR NO NOTE NPFX NSFX OBJE OCCU ORDI ORDN PAGE PEDI PHON PHRASE
    PLAC POST PROB PROP PUBL QUAY REFN RELA RELI REPO RESI RESN RETI RFN RIN
    ROLE ROMN SCHMA SDATE SEX SLGC SLGS SNOTE SOUR SPFX SSN STAE STAT SUBM SUBN
    SURN TEMP TEXT TIME TITL TRAN TRLR TYPE UID VERS WIDTH WIFE WILL WWW
'''.split())


def _looks_like_level(line: str) -> bool:
    """True when a line that failed the grammar still looks like an attempt at
    a GEDCOM line, so it is reported as unreadable instead of being joined into
    the value above it, where a "-1 NAME x" line would quietly become part of
    his name.

    A line starting with a digit (after spaces, tabs or a BOM) always counts.
    One that starts with a sign or dash, or with another kind of space (a
    no-break space), counts only when a capitalised tag follows the number:
    "-1 NAME x" is reported, while note prose such as "-5 below zero that
    January" or "+2 more cousins" still joins the note, words kept."""
    s = line.lstrip(' \t﻿')
    if s[:1].isdigit():
        return True
    rest = s.lstrip()
    if rest[:1] and rest[0] in _LEVEL_SIGNS:
        rest = rest[1:]
    elif rest == s:
        return False
    m = _BROKEN_LEVEL_RE.match(rest)
    return bool(m) and (m.group(1).startswith('_') or m.group(1) in _GEDCOM_TAGS)


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


def parse_gedcom(text: str) -> tuple[list[_Node], list[str], list[int]]:
    """Parse GEDCOM text into a list of level-0 record nodes.

    A note from MyHeritage arrives whole: a line with no level number that
    does not start with a digit joins the node above it with one space (the
    words are kept; node line numbers stay true because no node is made).
    Returns (records, malformed, joined). `malformed` describes lines that fit
    no grammar (reported as warnings, never fatal - the filed copy keeps them);
    a line that starts with a digit (or a sign and a number before a tag, so
    "-1 NAME x" counts; see _looks_like_level), or text before any node, still lands there
    because it looks like an attempt at a GEDCOM line (except, in a file that
    has level-less lines, a line nested under a NOTE/TEXT with a tag a note
    cannot have, such as "2 brothers nearby"). `joined` holds the line
    number of every level-less line that was folded in.
    CONC appends to the last node's value with NO space (GEDCOM splits values
    mid-word); CONT appends a newline + text. Both attach to the most recently
    parsed node regardless of level bookkeeping, which is how every real-world
    producer emits them."""
    records: list[_Node] = []
    stack: list[_Node] = []       # stack[level] = the open node at that level
    last: _Node | None = None
    last_level = 0
    malformed: list[str] = []
    joined: list[int] = []
    lines = text.splitlines()
    # Whether the file has level-less lines at all. This keeps the plain
    # "starts with a digit" test on purpose: a line reported by the stricter
    # _looks_like_level must never switch the prose rule off for the file.
    broken = any(ln.strip() and not _GEDCOM_LINE_RE.match(ln.strip('﻿'))
                 and not ln.lstrip(' 	﻿')[:1].isdigit() for ln in lines)

    for lineno, rawline in enumerate(lines, start=1):
        if not rawline.strip():
            continue
        m = _GEDCOM_LINE_RE.match(rawline.strip('﻿'))
        # A broken note line that happens to start with a number ("2 brothers
        # nearby", "1923 was a hard year", "2 I remember the farm") is prose,
        # not a tag. Only in a file that already has level-less lines (so a
        # tidy file parses exactly as before), and only for a line nested
        # under the NOTE with a tag a note cannot have. A line at the note's
        # own level or above ("1 WILL", "1 Birt") is a real record line and a
        # hand-typed "2 cont" is still a CONT.
        prose = (broken and m is not None and last is not None
                 and last.tag in ('NOTE', 'TEXT') and int(m.group(1)) > last_level
                 and not m.group(3).startswith('_')
                 and m.group(3).upper() not in _NOTE_CHILD_TAGS)
        if not m or prose:
            if last is not None and (prose or not _looks_like_level(rawline)):
                last.value += ' ' + rawline.strip()
                joined.append(lineno)
            else:
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
        last_level = level
    return records, malformed, joined


_DOUBLE_ENTITY_RE = re.compile(r'&amp;(?:lt|gt|amp|quot|apos|nbsp|#\d+|#x[0-9a-f]+);', re.I)
_BR_RE = re.compile(r'<br\s*/?>', re.I)
# Only a complete entity (ending in ";") is decoded. html.unescape alone would
# also turn "&region=" in a pasted web link into "®ion=", breaking the link.
_ENTITY_RE = re.compile(r'&(?:#(\d+)|#[xX]([0-9a-fA-F]+)|([A-Za-z][A-Za-z0-9]{1,31}));')


def _decode_entity(m: re.Match) -> str:
    if m.group(3):
        return html.entities.html5.get(m.group(3) + ';', m.group(0))
    return html.unescape(m.group(0))


def _clean_vendor_markup(text: str, records: list[_Node]) -> int:
    """Notes from MyHeritage read as plain words, not as web-page codes.

    MyHeritage double-encodes its notes (`&amp;lt;br&amp;gt;` for a line break),
    which would otherwise land in his archive as junk. Only a file that has
    double-encoded entities or says `1 SOUR MYHERITAGE` is touched; a file that
    legitimately says "Smith &amp; Sons" stays as typed. Changes node values in
    place (before any mapping) and returns how many nodes changed. A `<br>` in
    a NOTE or TEXT becomes a newline, so the existing first-line/rest split
    keeps the text before the break as the claim and the rest as its notes."""
    if not (_DOUBLE_ENTITY_RE.search(text)
            or re.search(r'^1 SOUR MYHERITAGE\b', text[:4000], re.M | re.I)):
        return 0
    changed = 0

    def walk(node: _Node) -> None:
        nonlocal changed
        old = node.value
        if old and ('&' in old or '<' in old):
            v = re.sub(r'&amp;(?=(?:lt|gt|amp|quot|apos|nbsp|#\d+|#x[0-9a-f]+);)', '&',
                       old, flags=re.I)
            v = _ENTITY_RE.sub(_decode_entity, v)
            v = _BR_RE.sub('\n' if node.tag in ('NOTE', 'TEXT') else ' ', v)
            v = re.sub(r'[ \t ]+', ' ', v).strip()
            if v != old:
                node.value = v
                changed += 1
        for child in node.children:
            walk(child)

    for rec in records:
        walk(rec)
    return changed


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


def _edtf_ymd(edtf: str) -> tuple[int, ...]:
    """Numeric (year[, month[, day]]) key of a plain EDTF token, for ordering.

    Only ever fed the output of `_one_gedcom_date_to_edtf` (zero-padded
    `YYYY[-MM[-DD]]`, optionally `~`-suffixed), so int-splitting on `-` is
    exact. Tuples compare element-wise and a shorter tuple sorts before its
    own extension, which is the right call for mixed-precision ranges: a
    range whose left bound is strictly later than its right is malformed."""
    return tuple(int(part) for part in edtf.rstrip('~').split('-'))


def gedcom_date_to_edtf(raw: str) -> str | None:
    """GEDCOM date phrase (or a free-form typed date) → valid EDTF, or None.

    The public answer of `_translate_gedcom_date`, without the flag saying
    which reader produced it: the strict GEDCOM grammar first, then the
    archive's loose-date reader (#293). See those two for the rules."""
    return _translate_gedcom_date(raw)[0]


def _translate_gedcom_date(raw: str) -> tuple[str | None, bool]:
    """(EDTF or None, read_loosely) for one GEDCOM DATE value.

    The strict GEDCOM grammar (`_strict_gedcom_date_to_edtf`) is tried first,
    so every date a genealogy program wrote reads exactly as it always has.
    Only when it returns None is the value treated as free text a person
    typed and handed to `_loose_date_to_edtf` (#293). `read_loosely` is True
    when that fallback produced the date, so the plan can count those apart
    from the dates no reader understood."""
    s = (raw or '').strip()
    if not s:
        return None, False
    strict = _strict_gedcom_date_to_edtf(s)
    if strict:
        return strict, False
    loose = _loose_date_to_edtf(s)
    return loose, loose is not None


# `YYYY-NN` (hyphen or en dash), the short way to write a year range
# ("1914-18"), alone or inside a hedged value ("about 1908-10") - but not the
# start of a full ISO day ("1908-10-12") or of a four-digit range ("1998-2002").
_SHORT_YEAR_RANGE_RE = re.compile(r'(?<!\d)(\d{4})\s*[-\u2013]\s*(\d{2})(?![\d\-\u2013])')
# `D/M/YYYY` or `M/D/YYYY` - read only when the order is not in doubt.
_SLASH_DATE_RE = re.compile(r'(\d{1,2})/(\d{1,2})/(\d{4})')


def _loose_date_to_edtf(s: str) -> str | None:
    """A date a person typed into their tree program → valid EDTF, or None.

    Ancestry and others let people type dates freely and export them exactly
    as typed ("3 December 1810", "after 1888", "Jul 7 1879", "1998-2002",
    "Bef. 1900"). The archive already has one reader for typed dates,
    `_lib.normalize_date` (behind `fha claim --date` and `fha person new
    --birth`), so the importer uses it rather than growing a second grammar
    that could drift from it. Two cases are settled here first:

    * `YYYY-NN`. `normalize_date` returns any value that is already valid
      EDTF unchanged, so `1908-10` would come back as October 1908. GEDCOM
      5.5.1 has no numeric date form at all: a program holding "October
      1908" writes `OCT 1908`. A `YYYY-NN` in a DATE line was therefore typed
      by a person, and written that way it is the common short form of a year
      range ("1914-18"). It is read as a range only when NN is a later year
      in the same century as YYYY (`1908-10` → `1908/1910`), the one reading
      that is a real span; `1908-05`, `1908-08` and `1998-02` (which would
      cross a century, and reads as well as February 1998) are left out. The
      rule is deliberate for values like `1905-11`, which could also be read
      as November 1905: a typed `YYYY-NN` with NN later than YY is a range
      (owner decision, #293). The short form is rewritten to the four-digit
      range and handed to `normalize_date`, so a hedge is kept on both
      endpoints ("about 1908-10" → `1908~/1910~`) and "before 1908-10",
      which has no stored form, is left out - the same rules as a typed
      "about 1908-1910". A full ISO day (`1908-10-12`) is not ambiguous and
      passes.
    * `D/M/YYYY` vs `M/D/YYYY`. Read only when exactly one of the first two
      parts is over 12, so it can only be the day (`19/05/1987`,
      `5/19/1987` → `1987-05-19`). `05/06/1950` could be either and is left
      out: a wrong month silently misplaces an event, while a missing one
      costs nothing - the wording stays in the claim value.

    The result is validated with `is_valid_edtf`, so an impossible day
    (`31/02/1950`) is left out rather than written.
    """
    text = s.strip()
    shorts = list(_SHORT_YEAR_RANGE_RE.finditer(text))
    if len(shorts) > 1:
        return None
    if shorts:
        m = shorts[0]
        year, suffix = m.group(1), int(m.group(2))
        if suffix <= int(year[2:]):
            return None
        text = f'{text[:m.start()]}{year}-{year[:2]}{suffix:02d}{text[m.end():]}'
    m = _SLASH_DATE_RE.fullmatch(text)
    if m:
        first, second, year = int(m.group(1)), int(m.group(2)), m.group(3)
        if first > 12 and 1 <= second <= 12:
            day, month = first, second
        elif second > 12 and 1 <= first <= 12:
            day, month = second, first
        else:
            return None
        edtf = f'{year}-{month:02d}-{day:02d}'
        return edtf if is_valid_edtf(edtf) else None
    edtf = normalize_date(text)
    return edtf if edtf and is_valid_edtf(edtf) else None


def _strict_gedcom_date_to_edtf(raw: str) -> str | None:
    """GEDCOM date phrase in the standard's own grammar → valid EDTF, or None.

    The translation table (inverting the exporter's `_edtf_to_gedcom`):

      12 JAN 1850     → 1850-01-12          ABT/EST/CAL X → X~ at its precision
      JAN 1850        → 1850-01             BEF X         → [..X]
      1850            → 1850                AFT X         → [X..]
      BET A AND B     → A/B
      FROM A TO B     → A/B

    A range whose bounds are reversed (`BET 1900 AND 1850`) is treated as
    malformed and omits `date:` - the wording stays in the claim value.

    The AFT arm is a runtime probe: it emits `[X..]` only because the suite
    validates it. SPEC §11 gained that after-form (#206); before that, AFT
    dates omitted `date:` and kept "after 1850" in the claim value only.

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
            if a and b and _edtf_ymd(a) > _edtf_ymd(b):
                # A reversed range ('BET 1900 AND 1850') is malformed, not a
                # guessable date: `1900/1850` would query as an empty window
                # and feed the living heuristic the wrong bound (is_valid_edtf
                # now refuses it too; this stricter tuple test also catches
                # '1900-05' after '1900', which it lets through). Same posture
                # as the AFT fallback: no date, wording kept in the value,
                # counted in the could-not-translate report.
                edtf = None
            else:
                edtf = f'{a}/{b}' if a and b else None
        else:
            edtf = _one_gedcom_date_to_edtf(up)

    return edtf if edtf and is_valid_edtf(edtf) else None


# ── Names ─────────────────────────────────────────────────────────────────────

def _parse_gedcom_name(raw: str, nsfx: str = '') -> tuple[str, str, str]:
    """GEDCOM `Given /Surname/ suffix` → (display name, given slot, surname slot).

    Inverts the exporter's `_gedcom_name`. Display drops the slashes and joins
    the pieces in order; the slots feed the stub filename grammar.

    A name with NO slash form - common in files written by hand or by an app
    that never fills the surname markup - is split on the last whitespace
    token, but only after a trailing generational suffix is set aside with
    the shared `_lib.strip_generational_suffix`. Without that, an incoming
    `1 NAME Roy Eugene Dodson Jr` filed as `jr__roy_eugene_dodson_P-….md`
    and indexed with the surname "Jr" - issue #53/#78's defect, arriving
    through the import on-ramp, and it poisons the export too (the next
    `fha gedcom` reads "Jr" back out of the index as his surname). "Roy Jr"
    keeps the same surname-less reading `stub_slug_name` gives it: the
    suffix does not promote the one remaining token into a surname.

    The suffix then rides at the END of the given slot, in both forms, so
    the filename this feeds sorts directly under the un-suffixed one
    (`dodson__roy_eugene_jr_P-….md` beside `dodson__roy_eugene_P-….md`) -
    the same placement `stub_slug_name` gives it, which is what makes a
    father and son legible side by side in a file browser.

    `nsfx` is the `2 NAME`-subordinate `NSFX` value when the file carries
    one. 5.5.1 makes NSFX a proper sub-tag of the name structure, and a
    great many apps put the suffix THERE and nowhere else - `1 NAME Roy
    Eugene /Dodson/` with `2 NSFX Jr` under it. Reading only the NAME line
    dropped that "Jr" out of the archive entirely: the same generational
    suffix this function exists to place correctly, lost rather than
    misfiled. It is appended to the display name and to the given slot -
    the placement the rest of this function uses - and only when the NAME
    text does not already end with it, so the form THIS repo exports
    (suffix on the NAME line AND an `NSFX` sub-tag naming it again) is not
    doubled into "Roy Eugene Dodson Jr Jr". Appending happens after the
    surname split, so a non-generational NSFX ("Esq", "PhD") can never
    land in the surname slot either."""
    raw = ' '.join((raw or '').split())
    nsfx = ' '.join((nsfx or '').split())
    if not raw:
        return '', '', ''
    m = re.match(r'^(.*?)\s*/([^/]*)/\s*(.*)$', raw)
    if m:
        given, surname, suffix = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
        display = ' '.join(p for p in (given, surname, suffix) if p)
        # No `or surname` here: a NAME line that carries only a surname
        # (`1 NAME /Dodson/`) has no given name, and repeating the surname
        # in the given slot would invent one - `dodson__dodson_P-….md`,
        # the `cher__cher` defect this same change fixed in
        # `convert_mining`. `_person_filename` already answers an empty
        # given slot with 'unknown', which is what `_lib.stub_filename`
        # answers for the same name.
        given_slot = ' '.join(p for p in (given, suffix) if p)
        return _with_nsfx(display, given_slot, surname, nsfx)
    parts = raw.split()
    if len(parts) == 1:
        return _with_nsfx(raw, parts[0], '', nsfx)
    core, suffix = strip_generational_suffix(parts)
    if len(core) < 2:
        return _with_nsfx(raw, ' '.join(parts), '', nsfx)
    given_slot = ' '.join(core[:-1] + ([parts[-1]] if suffix else []))
    return _with_nsfx(raw, given_slot, core[-1], nsfx)


def _with_nsfx(display: str, given_slot: str, surname: str,
               nsfx: str) -> tuple[str, str, str]:
    """Fold a `2 NSFX` value into a parsed name, unless it is already there.

    Split out so every return path of `_parse_gedcom_name` folds it the same
    way (see that function for why the sub-tag is read at all). "Already
    there" is a trailing-token match on the display name, period-insensitive
    so `2 NSFX Jr` under `1 NAME Roy /Dodson/ Jr.` is recognised as the same
    suffix rather than appended a second time."""
    if not nsfx:
        return display, given_slot, surname
    tail = display.split()[-1:]
    if tail and tail[0].rstrip('.').lower() == nsfx.rstrip('.').lower():
        return display, given_slot, surname
    return (f'{display} {nsfx}'.strip(),
            f'{given_slot} {nsfx}'.strip(),
            surname)


def _person_filename(given: str, surname: str, pid: str, *,
                     no_surname: bool = False) -> str:
    """`{surname}__{given}_{P-id}.md` per the stub grammar (stubs.py, SPEC §13).

    A NAME with no surname in it (`1 NAME Caesar`) says nothing about one,
    so the slot reads `unknown` (`unknown__caesar_P-….md`), as `fha person
    new` files the same name. Only a NAME that marks the surname empty
    (`1 NAME Caesar //`, `no_surname`) leads with the bare double underscore
    (`__caesar_P-….md`), SPEC §13's no-surname form. A person with no NAME at
    all files as `unknown__unknown_{P-id}.md`; and a person with a surname
    but no given name (`1 NAME /Dodson/`) takes 'unknown' in the given slot
    rather than a second copy of the surname, the same answer
    `_lib.stub_filename` gives for that name."""
    if not given and not surname:
        return f'unknown__unknown_{pid}.md'
    surname_slot = _name_slot(surname) if surname else ('' if no_surname else 'unknown')
    given_slot = _name_slot(given) if given else 'unknown'
    return f'{surname_slot}__{given_slot}_{pid}.md'


# ── The living: heuristic ─────────────────────────────────────────────────────

def _birth_year_upper(edtf: str | None) -> int | None:
    """Latest plausible birth year encoded in an EDTF value, or None.

    The living heuristic needs "born more than 110 years ago" to be TRUE only
    when the latest possible reading of the date is that old, so the error
    direction stays safe: an interval uses its upper bound, a decade its final
    year, a before-form its cutoff, and an approximate or uncertain year its
    widened end (`1915~` can be 1916). That is `edtf_bounds`' upper bound,
    the one reading every other tool uses. An after-form (open ceiling) or a
    phrase date yields None (unbounded - the person could be young), which
    keeps them `unknown`."""
    if not edtf or not is_valid_edtf(edtf.strip()):
        return None
    upper = edtf_bounds(edtf.strip())[1]
    if upper == '9999-12-31':      # the open ceiling of an after-form
        return None
    return int(upper[:4])


def living_flag_for_import(has_death: bool, birth_edtf: str | None,
                           today_year: int | None = None) -> str:
    """THE `living:` heuristic - the one privacy-relevant default this import writes.

    Rule (the plan's recommended option, owner-approved 2026-07-10):
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

    Deliberately a single isolated predicate: the owner confirmed this rule at
    review (over the all-`unknown` alternative), and it stays here so a future
    change lives in one place."""
    if has_death:
        return 'false'
    year = _birth_year_upper(birth_edtf)
    if year is not None:
        if today_year is None:
            today_year = datetime.date.today().year
        if today_year - year > _LIVING_CUTOFF_YEARS:
            return 'false'
    return 'unknown'


# ── Several birth or death facts for one person (#449) ────────────────────────

def _date_span_days(edtf: str) -> int:
    """How many days an EDTF value could mean: 0 for an exact day, about 365
    for a bare year, more for `about`, ranges and open forms. The smaller the
    span, the more exact the date. An unreadable value counts as vaguest."""
    if not is_valid_edtf(edtf):
        return 10 ** 7
    lo, hi = edtf_bounds(edtf)
    try:
        return (datetime.date.fromisoformat(hi) - datetime.date.fromisoformat(lo)).days
    except ValueError:
        # `about 9999` or `about 1` reach past what a calendar date can hold:
        # rank it vaguest rather than stop the whole import on one typo.
        return 10 ** 7


def _best_event_date(dated: list[tuple[str, int]]) -> str | None:
    """The date the new record shows when a tree has several facts for one
    event: the most exact one, and on a tie the first in the file (exporters
    put the preferred fact first). `dated` is (EDTF, line) for the facts that
    carry a readable date, in file order; a fact with no date is never in it,
    so it can never wipe a dated one."""
    if not dated:
        return None
    # An `about` or `maybe` day spans no wider than an exact day once read,
    # so the hedge breaks the tie: `14 Jan 1850` beats `about 12 Jan 1850`.
    return min(dated, key=lambda d: (_date_span_days(d[0]),
                                     any(ch in d[0] for ch in '~?%')))[0]


def _unread_date_may_be_recent(raw: str | None) -> bool:
    """True when a birth date the importer could not read holds a year within
    the living cutoff (`05/06/1995`, `INT 1995 (per cousin)`): it might be her
    real birth, so it keeps `living: unknown`. A dual-dated `1750/51`, a
    Julian `1750` or a bare `Unknown` cannot make anyone young, so it is
    treated like a fact with no date."""
    if not raw:
        return False
    runs = re.findall(r'\d+', raw)
    if not runs:
        return False                    # `Unknown`: no year at all
    years = [int(r) for r in runs if 3 <= len(r) <= 4]
    if '@#DHEBREW@' in raw.upper():
        # Hebrew years run 3,760 ahead (5600 is 1840), so compare the real year.
        years = [y - 3760 for y in years]
    if not years or any(len(r) > 4 for r in runs):
        # `5/6/95`, `Jun 95`, `19950506`: a year is there but can't be told
        # apart, so it may be recent.
        return True
    this_year = datetime.date.today().year
    return any(this_year - y <= _LIVING_CUTOFF_YEARS for y in years)


def _latest_birth_reading(dated: list[tuple[str, int]]) -> str | None:
    """The birth date the living guess reads when a tree has several: the one
    that could make the person youngest. One of the tree's own facts saying
    she may be young is doubt, and doubt keeps `living: unknown` (SPEC §19),
    so she is marked not living only when every dated fact says she was born
    more than 110 years ago. An open-ended date (no latest year) wins outright."""
    best: tuple[int, str] | None = None
    for edtf, _line in dated:
        year = _birth_year_upper(edtf)
        if year is None:
            return edtf
        if best is None or year > best[0]:
            best = (year, edtf)
    return best[1] if best else None


def _dates_disagree(dated: list[tuple[str, int]]) -> bool:
    """True when two of the dates cannot both be right (their possible spans
    do not overlap). `1878` and `12 Jan 1878` agree: one only adds detail."""
    spans = [edtf_bounds(e) for e, _ in dated if is_valid_edtf(e)]
    if len(spans) < len(dated):
        return len({e for e, _ in dated}) > 1
    return any(a[1] < b[0] or b[1] < a[0]
               for i, a in enumerate(spans) for b in spans[i + 1:])


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
    # The NAME's `/…/` when it says something the name alone would not:
    # written as `surname:` (SPEC §13); None when the automatic split of the
    # display name already gives that surname (see _explicit_surname).
    recorded_surname: str | None = None


def _explicit_surname(raw_name: str, display: str, surname: str) -> str | None:
    """The `surname:` line a GEDCOM NAME calls for, or None.

    A slash-delimited surname is an explicit decision - the exporting app
    (or person) said which words are the surname. It is recorded only where
    it differs from what `_lib.stub_slug_name` reads from the display name
    on its own: `Jinping /XI/` (the automatic split takes XI for a regnal
    suffix) gives 'XI', `Jon Thorsson //` gives '' (no surname, where the
    split would pick Thorsson), and the ordinary `Henry /Tudor/` gives None,
    so thousands of imported records do not carry a line that only repeats
    their file name. Without the record, a re-export could not tell such a
    file name from one the old slugger guessed (`gedcom._gedcom_name`). A
    NAME with no slashes made no decision, so it records nothing.
    """
    if '/' not in (raw_name or '') or not display:
        return None
    automatic = stub_slug_name(display)[0]
    parsed = _name_slot(surname) if surname else ''
    return None if parsed == automatic else surname


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
    # A statement without the people's names or the record number ("born 12
    # JAN 1878, Springfield, Illinois", "married 5 JUN 1897, Fairview"); ''
    # on parent-child claims. A newer download's statement is compared with
    # the earlier one's by these words, so a renamed or renumbered person, or
    # a spouse whose name the tree respelled, still said the same thing.
    said: str = ''
    # The date as the file wrote it, when it could not be read into `date`
    # ("INT 1878 (guess)"): the only way to tell two such dates apart.
    raw_date: str = ''


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
    loose_dates: int = 0                   # dates read by the free-form fallback (#293)
    loose_date_reads: list[str] = field(default_factory=list)   # every one, uncapped
    date_downgrades: list[str] = field(default_factory=list)    # unreadable, uncapped
    date_conflicts: list[str] = field(default_factory=list)     # disagreeing births/deaths
    joined_lines: int = 0                  # level-less lines folded into the line above
    markup_cleaned: int = 0                # values that had web-page codes cleaned
    photo_links: list['_PhotoLink'] = field(default_factory=list)  # photos the old program linked
    photo_catalog: str = 'ready'           # 'ready' or why the photo catalog could not be used
    # 'update' when the file is a newer download of a tree already imported.
    # Then `stubs` holds only the people who are new and `claims` only the
    # statements that would be added; no id is minted (new people carry a
    # `new:@I11@` placeholder, `sid` is '' and the two destination paths name
    # their folders only), so the same archive and file always give the same
    # plan. Everything below is empty in a first import.
    mode: str = 'new'
    tree_note: str = ''                    # first import beside an earlier one: why it is not an update
    file_people: int = 0                   # individuals in the file
    prior: list['_PriorImport'] = field(default_factory=list)    # newest first
    other_copies: list[str] = field(default_factory=list)        # same tree, kept as a separate copy
    unread_copies: list[str] = field(default_factory=list)       # earlier filed copies not readable
    unreadable_records: list[str] = field(default_factory=list)  # earlier import records not readable
    matches: dict[str, '_Match'] = field(default_factory=dict)   # by the newer file's record number
    held: dict[str, '_Held'] = field(default_factory=dict)       # not matched for certain, with the reason
    facts: list['_Fact'] = field(default_factory=list)           # every statement of matched and new people
    questions: list['_Question'] = field(default_factory=list)   # one per changed statement
    held_statements: int = 0               # statements left out with a held or removed person
    gone_facts: list[str] = field(default_factory=list)          # plain lines, as are the four below
    gone_people: list[str] = field(default_factory=list)
    removed_people: list[str] = field(default_factory=list)
    name_changes: list[str] = field(default_factory=list)
    death_now: list[str] = field(default_factory=list)


# ── Mapping ───────────────────────────────────────────────────────────────────

def _event_value(display: str, verb: str, node: _Node, xref: str) -> str:
    """Human-readable claim value: assertion first, GEDCOM xref for traceability.

    The date goes in AS WRITTEN (SPEC §11: the value keeps the source's
    wording; `date:` carries the best EDTF), so an unparseable `AFT 1900` or
    interpreted date is preserved even when the date field is omitted."""
    words = _event_words(verb, node)
    head = f'{display}: {words}' if display else words
    return f'{head} (GEDCOM {xref})'


def _event_words(verb: str, node: _Node) -> str:
    """What one event line says, without the person's name: the verb, the
    line's own value, the date as written and the place."""
    head = verb
    if node.value.strip() and node.value.strip().upper() != 'Y':
        head += f' {node.value.strip()}'
    date_raw = node.child_value('DATE')
    if date_raw:
        head += f' {date_raw}'
    place = node.child_value('PLAC')
    bits = [head]
    if place:
        bits.append(place)
    return ', '.join(bits)


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
    consumed: set[int], date_downgrades: list[str], date_loose: list[str],
    date_conflicts: list[str] | None = None,
) -> tuple[_PersonStub, list[_Claim], dict[str, str]]:
    """One INDI record → its person stub, its claims, and its FAMC pedigree map.

    Returns (stub, claims, {fam_xref: PEDI value}) - the pedigree map lets the
    FAM pass stamp `subtype: adoptive`/`foster` on the right parent-child claim.
    Claims take ids from `cid_pool` in order (pre-minted in one batch).
    A person whose tree gives two birth (or death) dates that cannot both be
    right gets one line in `date_conflicts`, for the plan to show."""
    consumed.add(id(indi))
    names = indi.children_tagged('NAME')
    for n in names:
        consumed.add(id(n))
        # The suffix sub-tag IS read (see `_parse_gedcom_name`), so it is
        # marked consumed and never counted in the not-read tally. Its
        # siblings (GIVN/SURN/SPFX/NPFX) genuinely are not read, and stay
        # in that tally where the human can see them.
        nsfx_node = n.child('NSFX')
        if nsfx_node:
            consumed.add(id(nsfx_node))
    display, given, surname = ('', '', '')
    variants: list[str] = []
    if names:
        display, given, surname = _parse_gedcom_name(
            names[0].value, names[0].child_value('NSFX'))
        for extra in names[1:]:
            v = _parse_gedcom_name(extra.value, extra.child_value('NSFX'))[0]
            if v and v != display:
                variants.append(v)

    sex = indi.child_value('SEX').upper()
    sex_node = indi.child('SEX')
    if sex_node:
        consumed.add(id(sex_node))
    if sex not in ('M', 'F'):
        sex = ''

    claims: list[_Claim] = []
    # Every dated birth and death fact, (EDTF, line) in file order (#449).
    births: list[tuple[str, int]] = []
    deaths: list[tuple[str, int]] = []
    unread_births: list[tuple[str, int]] = []   # a DATE nobody could read
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
        edtf, loose = _translate_gedcom_date(date_raw)
        if date_raw and not edtf:
            date_downgrades.append(f'{indi.xref} {node.tag}: {date_raw!r}')
        elif loose:
            date_loose.append(f'{indi.xref} {node.tag}: {date_raw!r}')
        cites = _source_citation_bits(node, sour_titles, sour_use)
        notes = f'GEDCOM cites: {"; ".join(cites)}' if cites else None
        return _Claim(
            cid=cid_pool.pop(0), claim_type=claim_type,
            value=_event_value(display, verb, node, indi.xref),
            persons=[pid], date=edtf,
            place_text=node.child_value('PLAC') or None,
            confidence='medium' if cites else 'low',
            anchor=f'line {node.line}', subtype=subtype, notes=notes,
            said=_event_words(verb, node),
            raw_date=date_raw if date_raw and not edtf else '',
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
                said=text,
            ))
            continue
        if tag in _EVENT_MAP:
            claim_type, verb = _EVENT_MAP[tag]
            claim = _event_claim(node, claim_type, verb)
            # A second birth fact with no date (Ancestry's alternate facts)
            # must not blank the dated one on the new record: collect the
            # dated ones and choose after the loop.
            if tag == 'BIRT' and claim.date:
                births.append((claim.date, node.line))
            elif tag == 'BIRT' and _unread_date_may_be_recent(node.child_value('DATE')):
                unread_births.append((node.child_value('DATE'), node.line))
            elif tag == 'DEAT':
                has_death = True
                if claim.date:
                    deaths.append((claim.date, node.line))
            claims.append(claim)
            continue
        if tag in _ADMIN_TAGS:
            # CHAN/CREA carry a DATE but are record-audit metadata, not life
            # events - they stay unread (the _count_unread docstring's own
            # example), never a suggested `event: chan` claim.
            continue
        if tag == 'EVEN' or node.child('DATE') is not None:
            # §8.2: nothing stalls for lack of a category - a typed or dated
            # structure this importer doesn't know becomes `event` + subtype.
            subtype = node.child_value('TYPE') or tag.lower()
            claims.append(_event_claim(node, 'event', subtype, subtype=subtype))
            continue
        # anything else: unread; the filed copy preserves it.

    birth_edtf = _best_event_date(births)
    death_edtf = _best_event_date(deaths)
    # The living guess: when the dated births agree, the most exact one
    # stands for them all (`AFT 1850` adds nothing to `12 Jan 1878`). When
    # they disagree, or a birth date could not be read, one of the tree's own
    # facts may say she is young, and doubt keeps `unknown` (SPEC §19).
    if unread_births:
        living = living_flag_for_import(has_death, None)
    elif _dates_disagree(births):
        living = living_flag_for_import(has_death, _latest_birth_reading(births))
    else:
        living = living_flag_for_import(has_death, birth_edtf)
    # Say why someone born in 1878 is still hidden as living.
    hidden_by_doubt = (living == 'unknown'
                       and living_flag_for_import(has_death, birth_edtf) == 'false')
    if date_conflicts is not None:
        who = f'{indi.xref} {display or "(no name)"}'
        for label, dated, chosen in (('birth', births, birth_edtf),
                                     ('death', deaths, death_edtf)):
            if _dates_disagree(dated):
                listed = ', '.join(f'{e} (line {ln})' for e, ln in dated)
                line = f'{who} {label}: {listed}; the record shows {chosen}'
                if label == 'birth' and hidden_by_doubt and not unread_births:
                    line += (', and keeps living: unknown because another of '
                             'these dates could make the person living')
                date_conflicts.append(line)
        if hidden_by_doubt and unread_births:
            unread = ', '.join(f'{raw!r} (line {ln})' for raw, ln in unread_births)
            date_conflicts.append(
                f'{who} birth: the record shows {birth_edtf}, but {unread} could '
                'not be read, so it keeps living: unknown in case that date '
                'makes the person living')
    stub = _PersonStub(
        xref=indi.xref, pid=pid, name=display, given=given, surname=surname,
        variants=variants, sex=sex, living=living,
        birth=birth_edtf, death=death_edtf,
        recorded_surname=_explicit_surname(
            names[0].value if names else '', display, surname),
    )
    return stub, claims, pedi_map


def _map_family(
    fam: _Node, xref_pid: dict[str, str], names: dict[str, str],
    cid_pool: list[str], sour_titles: dict[str, str], sour_use: Counter,
    pedi: dict[tuple[str, str], str], consumed: set[int],
    date_downgrades: list[str], date_loose: list[str], warnings: list[str],
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
        edtf, loose = _translate_gedcom_date(date_raw)
        if date_raw and not edtf:
            date_downgrades.append(f'{fam.xref} {node.tag}: {date_raw!r}')
        elif loose:
            date_loose.append(f'{fam.xref} {node.tag}: {date_raw!r}')
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
            said=_event_words(phrase, node),
            raw_date=date_raw if date_raw and not edtf else '',
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


_DEDUPE_YEAR_SLACK = 2     # birth windows match within this many years


def _birth_window(value) -> tuple[int, int] | None:
    """(earliest, latest) birth year a `birth:` value allows, or None if undated.

    Dedupe compares windows, not first-four-digit years: since #293 an
    imported birth can be `[..1900]`, `1840/1860` or `[1840..]`, and reading
    each as 1900/1840/1840 dropped true duplicates of an existing `1849~`.
    `edtf_bounds` gives the real span (open ends reach its 0001/9999
    sentinels, so "before 1900" overlaps anything earlier). A hand-written
    value that is not EDTF is read with `normalize_date`, and failing that by
    its first year, so an existing record is never compared more strictly
    than before."""
    raw = str(value or '').strip()
    if not raw:
        return None
    edtf = raw if is_valid_edtf(raw) else normalize_date(raw)
    if edtf:
        lo, hi = edtf_bounds(edtf)
        return int(lo[:4]), int(hi[:4])
    year = _year_of(raw)
    return (year, year) if year is not None else None


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
            'birth_window': _birth_window(meta.get('birth')),
            'birth_raw': str(meta.get('birth') or ''),
            'path': path,
            # Read by the newer-download plan, which matches people by sex
            # too and follows a merged record to its survivor.
            'sex': str(meta.get('sex') or '').strip().upper(),
            'meta': meta,
        })
    return out


def _find_duplicates(archive_root: Path, stubs: list[_PersonStub],
                     existing: list[dict] | None = None) -> list[str]:
    """Report lines for incoming people who look like someone already filed.

    Match rule (plan §dedupe): normalized name tokens equal AND birth windows
    (`_birth_window`) overlapping once widened by ±2 years - or either birth
    absent and the names match exactly. Two plain years therefore still match
    within ±2, as before. Import
    proceeds regardless; merging identities is a human decision (the
    merge-identities skill, and plan 16's confirmed write-back).

    `existing` is a people scan the caller already made (the newer-download
    plan reads people/ once and shares it)."""
    if existing is None:
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
                in_win = _birth_window(stub.birth)
                ex_win = person['birth_window']
                if (in_win is not None and ex_win is not None
                        and (in_win[0] - _DEDUPE_YEAR_SLACK > ex_win[1]
                             or ex_win[0] > in_win[1] + _DEDUPE_YEAR_SLACK)):
                    continue
                # Plain wording ("after 1700", "about 1849"), not EDTF codes;
                # a hand-written non-EDTF birth is shown as typed.
                in_b = f' b.{humanize_edtf(stub.birth)}' if stub.birth else ''
                ex_raw = person['birth_raw']
                ex_b = (f' b.{humanize_edtf(ex_raw) if is_valid_edtf(ex_raw) else ex_raw}'
                        if ex_raw else '')
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


# ── A newer download of a tree already imported ───────────────────────────────
#
# He downloads his tree again a year later, after a winter of adding cousins.
# Importing that file the way the first one was imported would make every
# person a second time. So a newer download is planned as an update: who in
# the file is already a record here, who is new, and which statements the
# newer file makes that the archive does not already have.
#
# Three rules shape all of it:
#
# * Show, never guess. A person is matched only on evidence that cannot
#   reasonably point at someone else (see `_match_people`). Everyone else is
#   listed with the reason, and nothing is planned for them: no second record,
#   no statement attached to a person who may be the wrong one.
# * What he did stands. The comparison is download against download: a
#   statement the newer file words exactly as the earlier one did is never
#   planned again, whatever he has since done with it here (corrected its
#   date, reworded it, rejected it, deleted it). Only a statement the tree
#   itself changed is looked at, and then against the archive as it is now.
# * The records are the memory. Which earlier person a record number stood for
#   is read from the earlier import's own claims (their `(GEDCOM @I1@)` and
#   `persons:`) and from its filed copy, so it survives a lost `.cache` and a
#   new computer. The audit CSV only fills in people who had no statement.
#
# Nothing here writes, and no id is minted: the same archive and the same file
# always give the same plan.

_GEDCOM_TAIL_RE = re.compile(r'\s*\(GEDCOM (@[^@\s]+@)\)\s*$')
_GEDCOM_SUBTYPE_RE = re.compile(rb'^subtype:[ \t]*[\'"]?gedcom\b', re.M | re.I)
_SINGLE_TYPES = frozenset({'birth', 'death', 'burial'})     # one per person
_COUPLE_TYPES = frozenset({'marriage', 'divorce'})          # one per couple
_FAMILY_TYPES = _COUPLE_TYPES | {'relationship'}            # value ends in a FAM number
_SETTLED_STATUSES = frozenset({'rejected', 'superseded'})   # he already decided these
_SAME_TREE_SHARE = 0.5     # share of an earlier import's people a newer download must hold
_NEW_PERSON = 'new:'       # stands in for the id of a person the plan would add
_OLD_PERSON = 'old:'       # stands in for a person of an earlier file until the records name them
_US_NAMES = frozenset({'us', 'usa', 'u.s.', 'u.s.a.', 'united states',
                       'united states of america'})
_QUESTION_HEADINGS = {
    'birth': 'When and where was {a} born?',
    'death': 'When and where did {a} die?',
    'burial': 'When and where was {a} buried?',
    'marriage': 'When and where did {a} and {b} marry?',
    'divorce': 'When did {a} and {b} divorce?',
}
_PARENT_HEADINGS = {'adoptive': 'Who adopted {a}?', 'foster': 'Who fostered {a}?'}


def _already_imported(imported: str | None, sid: str | None) -> str:
    """The one refusal for a file the archive already holds byte for byte."""
    return (f'this GEDCOM was already imported on {imported or "an earlier date"} as '
            f'{sid or "a source record"}. Importing the same file again would add '
            'nothing. If you have downloaded a newer copy of your tree, run the import '
            'on that file: the plan shows only what changed.')


@dataclass(eq=False)
class _PriorPerson:
    """One person of an earlier download, and what the archive says became of them.

    `state` is 'present' (the record is on file, or its merge survivor is),
    'removed' (he deleted the record), 'unknown' (no statement says which
    record this person became) or 'split' (the earlier statements about this
    record number now name more than one person). Only a present or removed
    person can be matched; the other two are reported.

    Names, sexes and births hold everything known: what the earlier download
    said and what the record says today. A name he corrected by hand must not
    cost the person their match, and neither must a name the tree still spells
    the old way."""

    sid: str
    xref: str                      # '' when only a family statement names them
    pid: str                       # '' for 'unknown' and 'split'
    state: str
    label: str                     # the name to show: the record's, else the file's
    names: set[tuple[str, ...]] = field(default_factory=set)
    labels: list[str] = field(default_factory=list)      # names as written, for wording
    sexes: set[str] = field(default_factory=set)
    births: list[str] = field(default_factory=list)      # as stored (EDTF or as typed)
    windows: list[tuple[int, int]] = field(default_factory=list)
    uid: str = ''
    note: str = ''                 # 'split': the ids the statements name

    @property
    def ident(self) -> str:
        return self.pid or f'{self.sid}:{self.xref}'


@dataclass(eq=False)
class _OldFact:
    """One statement the earlier download made, as the archive holds it now.

    `text` and `orig_parents` are the tree's own words (taken from the filed
    copy when it can be read), while `date`, `place` and `parents` are the
    record's fields today, hand edits included. `live` is False for a claim he
    rejected or superseded, and for a statement of the earlier file that has no
    claim any more because he deleted it (`cid` is '' then)."""

    kind: str                      # 'single' | 'couple' | 'parents' | 'multi'
    key: tuple
    text: str
    date: str
    place: str
    live: bool
    cid: str
    people: frozenset[str]
    value: str = ''
    parents: frozenset[str] = frozenset()
    orig_parents: frozenset[str] = frozenset()
    joined: bool = False           # tied to its line in the filed copy
    # The key under the people the filed copy named, when he has since
    # re-pointed the claim to someone else (a corrected spouse, a statement
    # moved to a brother). A newer file that repeats the tree's old words is
    # found by it and judged by those words only, never by the fields of a
    # claim that is now about someone else.
    orig_key: tuple | None = None
    orig_only: bool = False        # this copy is filed under `orig_key`
    # A repeatable statement (an occupation, a residence) he reworded: also
    # filed under its wording now, so a tree that makes the same correction
    # is not planned as a second claim.
    now_key: tuple | None = None
    raw_date: str = ''             # the filed copy's unread date wording


@dataclass(eq=False)
class _PriorImport:
    """One earlier `fha gedcom import` of this archive, read from its source record."""

    sid: str
    record_path: Path
    file_name: str                 # the download's own name
    file_alias: str                # the filed copy, alias form
    imported: str                  # the record's created: date
    people: list[_PriorPerson]
    facts: list[_OldFact]
    copy_read: bool                # the filed copy was there to compare with
    copy_hash: str                 # its sha256, '' when unreadable
    # The folder the filed copy lives in, when that whole folder is not here
    # (a documents drive not plugged in), as opposed to the copy alone being
    # gone. '' otherwise.
    copy_folder_missing: str = ''


@dataclass
class _Incoming:
    """One person of the newer file, as matching sees them."""

    xref: str
    label: str
    names: set[tuple[str, ...]]
    sex: str
    birth: str | None
    window: tuple[int, int] | None
    uid: str


@dataclass
class _Match:
    xref: str
    pid: str
    how: str                       # 'record number' | 'program id'
    person: _PriorPerson


@dataclass
class _Held:
    """A person of the newer file who could not be matched for certain.

    Nothing is planned for them. `likely` is the one earlier person the name
    and birth date fit, when exactly one does. `resembles` says they look like
    someone of the earlier download at all, which is what tells a renumbered
    copy of the same tree from a different tree."""

    xref: str
    label: str
    reason: str
    likely: _PriorPerson | None = None
    about: list[str] = field(default_factory=list)       # ids the reason names
    resembles: bool = True


@dataclass(eq=False)
class _Fact:
    """One statement of the newer file about people who are matched or new."""

    claim: _Claim
    kind: str
    key: tuple
    text: str
    parents: frozenset[str] = frozenset()
    outcome: str = ''              # 'same' | 'decided' | 'new' | 'more detail' | 'changed'
    against: list[str] = field(default_factory=list)     # earlier claims it disagrees with
    was: list[_OldFact] = field(default_factory=list)    # what it was judged against
    # Plain words for a changed or fuller statement, by the names his records
    # use: "Rose Hartley, birth", what the archive has, what the newer file says.
    subject: str = ''
    before: str = ''
    now: str = ''
    # A couple's statement with the spouses' names in it, for an earlier
    # claim whose filed copy could not be read (its words carry the names).
    named_text: str = ''
    # Parents of a tie he rejected or deleted, left out of the planned tie.
    turned_down: frozenset[str] = frozenset()


@dataclass(eq=False)
class _Question:
    """The open question a changed statement will get (SPEC §17b).

    Planned here and written by the apply step. The new claim has no id until
    then, so `refs` holds the earlier claims and the people only."""

    heading: str
    refs: list[str]
    fact: _Fact
    new_file: str


# ── Reading the earlier imports ──────────────────────────────────────────────

def _name_key(name: str) -> tuple[str, ...]:
    """`_norm_name_key`, with a nameless person reading as the empty key.

    A person the file gave no NAME is written to the archive as "unknown", so
    both spellings of "no name" compare equal."""
    key = _norm_name_key(name)
    return () if key == ('unknown',) else key


def _norm_text(text: object) -> str:
    return ' '.join(str(text or '').casefold().split())


def _statement_text(value: object, labels: list[str] | tuple[str, ...] = ()) -> str:
    """A claim value as plain comparable words: no trailing `(GEDCOM @x@)`,
    and no leading `Name: ` when the name is one the person is known by."""
    text = _GEDCOM_TAIL_RE.sub('', str(value or ''))
    for label in sorted(labels, key=len, reverse=True):
        if label and text.startswith(f'{label}: '):
            text = text[len(label) + 2:]
            break
    return _norm_text(text)


def _place_key(text: object) -> str:
    """A place for comparison: `normalize_place_text`, with the spellings of
    the United States as a last part counted as one. Nothing else is dropped
    or reordered, so "Springfield, Illinois" and "Springfield, Ohio" differ."""
    key = normalize_place_text(str(text or ''))
    if not key:
        return ''
    parts = [p.strip() for p in key.split(',')]
    if parts[-1] in _US_NAMES:
        parts[-1] = 'usa'
    return ', '.join(parts)


def _gedcom_source_paths(archive_root: Path) -> list[Path]:
    """Source records an earlier `fha gedcom import` wrote.

    Found by what the record says (`subtype: gedcom`), with a byte check first
    so a large archive's other sources are not parsed. The caller confirms each
    one with a full read."""
    root = archive_root / 'sources'
    if not root.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(root.rglob('*.md')):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if _GEDCOM_SUBTYPE_RE.search(raw):
            out.append(path)
    return out


def _audit_people(archive_root: Path) -> dict[str, dict[str, tuple[str, str]]]:
    """{source id: {record number: (person id, name)}} from the audit CSVs.

    The cache is a help, never the memory: it is read only to place people the
    earlier download gave no statement of their own (a name and nothing else),
    whom no claim can place. A missing or unreadable file is simply absent."""
    out: dict[str, dict[str, tuple[str, str]]] = {}
    folder = archive_root / _AUDIT_DIR
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob('*.csv')):
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        header: dict[str, str] = {}
        body: list[str] = []
        for line in lines:
            if line.startswith('#'):
                key, _, value = line.lstrip('# ').partition(':')
                header[key.strip()] = value.strip()
            elif line.strip():
                body.append(line)
        sid = normalize_id(header.get('source_id', ''))
        if not sid:
            continue
        people: dict[str, tuple[str, str]] = {}
        try:
            for row in csv.DictReader(body):
                xref = (row.get('gedcom_xref') or '').strip()
                pid = normalize_id(row.get('minted_id') or '')
                if (row.get('kind') or '').strip() == 'person' and xref and pid:
                    people[xref] = (pid, (row.get('note') or '').strip())
        except csv.Error:
            continue
        out[sid] = people
    return out


def _read_prior_imports(archive_root: Path, fha_config: dict, record_paths: list[Path],
                        existing: list[dict]) -> tuple[list[_PriorImport], list[Path]]:
    """(every earlier GEDCOM import on file, newest first; records not readable).

    `existing` is the one scan of people/ the plan makes. It says which
    records are still here and which were merged, so a claim naming a merged
    record reads as its survivor (SPEC §9) the way `fha index` reads it.

    A record that will not read, or whose claims will not parse, is returned
    in the second list so the plan can say so: skipped silently, the tree it
    holds would look like one never imported and every person in it would be
    planned again."""
    by_pid = {normalize_id(p['pid']): p for p in existing if p['pid']}
    alias_map = person_alias_map({pid: (p['path'], p['meta']) for pid, p in by_pid.items()})
    audits = _audit_people(archive_root)
    priors: list[_PriorImport] = []
    unreadable: list[Path] = []
    for path in record_paths:
        try:
            rec = read_record(path)
        except Exception:
            unreadable.append(path)
            continue
        meta = rec.get('meta') or {}
        sid = normalize_id(str(meta.get('id') or ''))
        if rec.get('parse_errors') and (not meta or not rec.get('claims')):
            unreadable.append(path)
            continue
        if (str(meta.get('source_type') or '').strip().lower() != 'other'
                or str(meta.get('subtype') or '').strip().lower() != 'gedcom'):
            continue
        if not sid:
            # Its id line lost in a hand edit: still an earlier import of a
            # tree, and skipped quietly its people would all be planned again.
            unreadable.append(path)
            continue
        priors.append(_read_prior_import(
            archive_root, fha_config, path, rec, sid, alias_map, by_pid,
            audits.get(sid, {})))
    priors.sort(key=lambda p: (p.imported, p.sid), reverse=True)
    return priors, unreadable


def _read_filed_copy(archive_root: Path, fha_config: dict,
                     file_alias: str) -> tuple[str, list[_Node], _Mapped] | None:
    """The earlier download itself: (sha256, its INDI records, its statements).

    The filed copy is byte for byte what he downloaded then, so it says what
    the tree said, whatever has been edited here since. People carry `old:@I1@`
    placeholders until the records say who they became. None when the copy
    cannot be read (a documents drive not plugged in, a working copy)."""
    if not file_alias:
        return None
    try:
        raw = resolve_path(file_alias, fha_config, archive_root).read_bytes()
        text = _decode_gedcom_bytes(raw, Path(file_alias))
    except (OSError, GedcomImportError):
        return None
    records, _malformed, _joined = parse_gedcom(text)
    _clean_vendor_markup(text, records)
    indis = [r for r in records if r.tag == 'INDI' and r.xref]
    if len({i.xref for i in indis}) != len(indis):
        return None
    fams = [r for r in records if r.tag == 'FAM']
    mapped = _map_file(indis, fams, {i.xref: _OLD_PERSON + i.xref for i in indis}, {}, set())
    return hashlib.sha256(raw).hexdigest(), indis, mapped


def _read_prior_import(archive_root: Path, fha_config: dict, path: Path, rec: dict,
                       sid: str, alias_map, by_pid: dict[str, dict],
                       audit: dict[str, tuple[str, str]]) -> _PriorImport:
    """One earlier import: its people (who each record number became) and its
    statements, each tied to its line in the filed copy when that can be read."""
    meta = rec.get('meta') or {}
    claims = [c for c in (rec.get('claims') or []) if isinstance(c, dict)]
    file_alias = file_name = ''
    for entry in (meta.get('files') or []):
        if isinstance(entry, dict) and entry.get('file'):
            file_alias = str(entry['file']).strip()
            file_name = str(entry.get('original_filename') or '').strip() \
                or Path(file_alias).name
            break
    copy = _read_filed_copy(archive_root, fha_config, file_alias)
    copy_hash, old_indis, old = copy if copy else ('', [], None)
    folder_missing = ''
    if copy is None and file_alias:
        try:
            folder = resolve_path(file_alias, fha_config, archive_root).parent
            if not folder.is_dir():
                folder_missing = str(folder)
        except Exception:
            pass
    old_stubs = {s.xref: s for s in old.stubs} if old else {}
    old_uids = {i.xref: i.child_value('_UID') for i in old_indis}

    def _type(claim: dict) -> str:
        return str(claim.get('type') or '').strip().lower()

    def _anchor(claim: dict) -> str:
        return str(claim.get('anchor') or '').strip()

    # A person's own statement in the filed copy sits on its own line, so
    # (line, type) finds it again whatever the claim's wording says today.
    old_own = {(c.anchor, c.claim_type): c for c in (old.claims if old else [])
               if not c.fam_xref}

    # Which person each record number became: read from every statement the
    # earlier import filed about that number. The number is written at the end
    # of the value, and the line in the filed copy says it again.
    seen: dict[str, set[str]] = {}
    raw_of: dict[str, set[str]] = {}    # the records the claims name, before merges
    joined: dict[int, _Claim] = {}
    order: list[str] = [i.xref for i in old_indis] + list(audit)
    for i, claim in enumerate(claims):
        if _type(claim) in _FAMILY_TYPES:
            continue
        tail = _GEDCOM_TAIL_RE.search(str(claim.get('value') or ''))
        own = old_own.get((_anchor(claim), _type(claim)))
        by_tail = tail.group(1) if tail else ''
        by_line = own.persons[0][len(_OLD_PERSON):] if own else ''
        if by_tail and by_line and by_tail != by_line:
            continue        # its words and its line disagree about whose it is
        if own:
            joined[i] = own
        pids = claim_person_pids(claim, alias_map)
        if (by_tail or by_line) and pids:
            seen.setdefault(by_tail or by_line, set()).add(pids[0])
            raw = claim_person_pids(claim)
            raw_of.setdefault(by_tail or by_line, set()).add(raw[0] if raw else pids[0])
            order.append(by_tail or by_line)

    # One record named by the statements of two record numbers: he moved a
    # statement from one person to another (an occupation that was really the
    # brother's). The record is the person whose name it carries in the
    # earlier download; the other number is not known to be anyone, so it is
    # never matched to that record. Without the filed copy's names, neither is.
    owners: dict[str, list[str]] = {}
    for xref, pids in seen.items():
        if len(pids) == 1:
            owners.setdefault(next(iter(pids)), []).append(xref)
    for pid, xrefs in owners.items():
        if len(xrefs) < 2:
            continue
        raws = [raw_of.get(x, set()) for x in xrefs]
        if all(not (a & b) for i, a in enumerate(raws) for b in raws[i + 1:]):
            continue        # each number's own record, merged here into one: fine
        record = by_pid.get(pid)
        names = ({_name_key(n) for n in [record['name']] + record['variants']}
                 if record else set())
        keep = [x for x in xrefs if x in old_stubs and names & {
            _name_key(n) for n in [old_stubs[x].name] + old_stubs[x].variants}]
        for xref in xrefs:
            if keep != [xref]:
                seen[xref] = set()

    people: list[_PriorPerson] = []
    for xref in dict.fromkeys(order):
        pids = seen.get(xref, set())
        audit_pid, audit_name = audit.get(xref, ('', ''))
        note = ''
        if len(pids) > 1:
            pid, state = '', 'split'
            note = ', '.join(fmt_id_display(p) for p in sorted(pids))
        else:
            pid = next(iter(pids), '') or (through_survivors(audit_pid, alias_map)
                                           if audit_pid else '')
            state = 'unknown' if not pid else ('present' if pid in by_pid else 'removed')
        people.append(_prior_person(sid, xref, pid, state, note, old_stubs.get(xref),
                                    audit_name, by_pid.get(pid), old_uids.get(xref, '')))
    # People only a family statement names (a spouse or child the tree gave a
    # name and nothing else). Their record number is not written anywhere in
    # the records, so they can be suggested as a likely match, never matched.
    named = {p.pid for p in people if p.pid}
    for claim in claims:
        if _type(claim) not in _FAMILY_TYPES:
            continue
        for pid in claim_person_pids(claim, alias_map):
            if pid not in named:
                named.add(pid)
                people.append(_prior_person(
                    sid, '', pid, 'present' if pid in by_pid else 'removed', '',
                    None, '', by_pid.get(pid), ''))

    # The earlier file's statements now name real people where the records say
    # who they are, and each claim finds the statement it was made from.
    labels = {p.pid: p.labels for p in people if p.pid}
    old_fam: dict[tuple, _Claim] = {}
    if old:
        became = {_OLD_PERSON + p.xref: p.pid for p in people if p.xref and p.pid}
        for old_claim in old.claims:
            _rename_people(old_claim, became)
            if old_claim.claim_type == 'relationship':
                old_fam[(old_claim.anchor, 'relationship', old_claim.roles['child'])] = old_claim
            elif old_claim.fam_xref:
                old_fam[(old_claim.anchor, old_claim.claim_type)] = old_claim
    facts: list[_OldFact] = []
    used: set[int] = set()
    for i, claim in enumerate(claims):
        ctype = _type(claim)
        if ctype == 'relationship':
            child = sorted(claim_role_pids(claim, 'child', alias_map))
            source = old_fam.get((_anchor(claim), ctype, child[0])) if len(child) == 1 else None
        elif ctype in _COUPLE_TYPES:
            source = old_fam.get((_anchor(claim), ctype))
        else:
            source = joined.get(i)
        fact = _old_fact(claim, alias_map, labels, source)
        if fact is None:
            continue
        if source is not None:
            used.add(id(source))
        facts.append(fact)
    for old_claim in (old.claims if old else []):
        # A statement of the earlier file with no claim today: he deleted it.
        # It is remembered as settled, so the newer file cannot bring it back.
        if id(old_claim) in used or any(p.startswith(_OLD_PERSON) for p in old_claim.persons):
            continue
        new = _new_fact(old_claim)
        facts.append(_OldFact(
            kind=new.kind, key=new.key, text=new.text, date=old_claim.date or '',
            place=old_claim.place_text or '', live=False, cid='',
            people=frozenset(old_claim.persons), value=old_claim.value,
            parents=new.parents, orig_parents=new.parents, joined=True,
            raw_date=old_claim.raw_date))

    return _PriorImport(
        sid=sid, record_path=path, file_name=file_name or path.name, file_alias=file_alias,
        imported=str(meta.get('created') or '').strip(), people=people, facts=facts,
        copy_read=old is not None, copy_hash=copy_hash, copy_folder_missing=folder_missing)


def _prior_person(sid: str, xref: str, pid: str, state: str, note: str,
                  old_stub: _PersonStub | None, audit_name: str,
                  record: dict | None, uid: str) -> _PriorPerson:
    """Gather what is known of one earlier person: the earlier file's words,
    the audit's name, and the record as it reads today."""
    person = _PriorPerson(sid=sid, xref=xref, pid=pid, state=state, label='',
                          uid=uid.strip(), note=note)

    def add_name(name: str) -> None:
        person.names.add(_name_key(name))
        if name and name != 'unknown' and name not in person.labels:
            person.labels.append(name)

    def add_birth(raw: object) -> None:
        text = str(raw or '').strip()
        window = _birth_window(text)
        if text and text not in person.births:
            person.births.append(text)
        if window is not None and window not in person.windows:
            person.windows.append(window)

    if record is not None:
        for name in [record['name']] + record['variants']:
            add_name(name)
        if record['sex'] in ('M', 'F'):
            person.sexes.add(record['sex'])
        add_birth(record['birth_raw'])
    if old_stub is not None:
        for name in [old_stub.name] + old_stub.variants:
            add_name(name)
        if old_stub.sex:
            person.sexes.add(old_stub.sex)
        add_birth(old_stub.birth)
    if audit_name:
        add_name(audit_name)
    person.label = person.labels[0] if person.labels else '(no name)'
    return person


def _rename_people(claim: _Claim, became: dict[str, str]) -> None:
    """Swap placeholder people for the ids the plan settled on, in place."""
    claim.persons = [became.get(p, p) for p in claim.persons]
    if claim.roles:
        claim.roles = {
            role: ([became.get(p, p) for p in who] if isinstance(who, list)
                   else became.get(who, who))
            for role, who in claim.roles.items()}


def _new_fact(claim: _Claim) -> _Fact:
    """The comparison shape of one mapped statement.

    Birth, death and burial are one per person, marriage and divorce one per
    couple, and a child's parents one set per kind of tie; a newer download
    that says one of these differently disagrees with the earlier one. Every
    other statement (a residence, an occupation, a note) can be made many
    times, so it is told apart by its own words."""
    ctype = claim.claim_type
    if ctype == 'relationship':
        return _Fact(claim, 'parents', (claim.roles['child'], _norm_text(claim.subtype)),
                     text='', parents=frozenset(claim.roles['parent']))
    if ctype in _COUPLE_TYPES:
        return _Fact(claim, 'couple', (ctype, frozenset(claim.roles['spouse'])),
                     text=_norm_text(claim.said), named_text=_statement_text(claim.value))
    text = _norm_text(claim.said)
    if ctype in _SINGLE_TYPES:
        return _Fact(claim, 'single', (ctype, claim.persons[0]), text=text)
    return _Fact(claim, 'multi', (ctype, claim.persons[0], _norm_text(claim.subtype), text),
                 text=text)


def _old_fact(claim: dict, alias_map, labels: dict[str, list[str]],
              source: _Claim | None) -> _OldFact | None:
    """One claim of an earlier import in the same shape as `_new_fact`.

    `source` is the statement of the filed copy the claim was made from, when
    that is known: its words are what the tree said, so a claim he has reworded
    still compares as what it was. None for a claim that names nobody."""
    ctype = str(claim.get('type') or '').strip().lower()
    status = str(claim.get('status') or '').strip().lower()
    persons = claim_person_pids(claim, alias_map)
    subtype = _norm_text(claim.get('subtype'))
    value = str(claim.get('value') or '')
    parents = orig_parents = frozenset()
    orig_key: tuple | None = None
    now_key: tuple | None = None
    if ctype == 'relationship':
        child = sorted(claim_role_pids(claim, 'child', alias_map))
        if len(child) != 1:
            return None
        parents = frozenset(claim_role_pids(claim, 'parent', alias_map))
        orig_parents = frozenset(source.roles['parent']) if source else parents
        # Biological is the unwritten default, so a hand-written
        # `subtype: biological` is the same tie.
        tie = '' if subtype == 'biological' else subtype
        kind, key, text = 'parents', (child[0], tie), ''
        if source is not None:
            orig_tie = _norm_text(source.subtype)
            orig_key = (source.roles['child'], '' if orig_tie == 'biological' else orig_tie)
        people = frozenset(child) | parents
    elif ctype in _COUPLE_TYPES:
        spouses = frozenset(claim_spouse_pids(claim, alias_map))
        if not spouses:
            return None
        kind, key, people = 'couple', (ctype, spouses), spouses
        # The filed copy's words leave the spouses' names out, as the newer
        # file's do; without the copy only the claim's own value is left.
        text = _norm_text(source.said) if source else _statement_text(value)
        if source is not None:
            orig_key = (ctype, frozenset(source.roles['spouse']))
    else:
        if not persons:
            return None
        who = persons[0]
        if source is not None:
            text = _norm_text(source.said)
        else:
            text = _statement_text(value, labels.get(who, []))
            if ctype == 'note' and claim.get('notes'):
                text = _norm_text(f'{text} {claim.get("notes")}')
        people = frozenset(persons)
        if ctype in _SINGLE_TYPES:
            kind, key = 'single', (ctype, who)
        else:
            kind, key = 'multi', (ctype, who, subtype, text)
            if source is not None:
                now = _statement_text(value, labels.get(who, []))
                if ctype == 'note' and claim.get('notes'):
                    now = _norm_text(f'{now} {claim.get("notes")}')
                if now != text:
                    now_key = (ctype, who, subtype, now)
        if source is not None:
            orig_key = (key[:1] + (source.persons[0],)
                        + ((_norm_text(source.subtype), text) if kind == 'multi' else ()))
    if orig_key == key or (orig_key is not None and any(
            isinstance(p, str) and p.startswith(_OLD_PERSON)
            for part in orig_key for p in (part if isinstance(part, frozenset) else [part]))):
        orig_key = None             # not re-pointed, or no record says who they became
    return _OldFact(
        kind=kind, key=key, text=text, date=str(claim.get('date') or '').strip(),
        place=str(claim.get('place_text') or '').strip(),
        live=status not in _SETTLED_STATUSES,
        cid=normalize_id(str(claim.get('id') or '')), people=people, value=value,
        parents=parents, orig_parents=orig_parents, joined=source is not None,
        orig_key=orig_key, now_key=now_key,
        raw_date=source.raw_date if source is not None else '')


# ── Who is who ───────────────────────────────────────────────────────────────

def _incoming_people(indis: list[_Node], stubs: list[_PersonStub]) -> list[_Incoming]:
    out: list[_Incoming] = []
    for indi, stub in zip(indis, stubs):
        out.append(_Incoming(
            xref=stub.xref, label=stub.name or '(no name)',
            names={_name_key(n) for n in [stub.name] + stub.variants},
            sex=stub.sex, birth=stub.birth, window=_birth_window(stub.birth),
            uid=indi.child_value('_UID')))
    return out


def _who(person: _PriorPerson) -> str:
    """An earlier person in plain words: the name, the record id, and whether
    he has since removed the record."""
    name = 'a person with no name' if person.label == '(no name)' else person.label
    if not person.pid:
        return name
    removed = ', a record you removed' if person.state == 'removed' else ''
    return f'{name} ({fmt_id_display(person.pid)}{removed})'


def _sex_differs(new: _Incoming, old: _PriorPerson) -> bool:
    return bool(new.sex and old.sexes and new.sex not in old.sexes)


def _birth_differs(new: _Incoming, old: _PriorPerson) -> bool:
    """Both dated, and more than `_DEDUPE_YEAR_SLACK` years apart whichever of
    the earlier dates (the file's, the record's) is used."""
    if new.window is None or not old.windows:
        return False
    lo, hi = new.window
    return all(lo - _DEDUPE_YEAR_SLACK > w[1] or w[0] > hi + _DEDUPE_YEAR_SLACK
               for w in old.windows)


def _birth_fits(new: _Incoming, old: _PriorPerson) -> bool:
    """Both dated, and within `_DEDUPE_YEAR_SLACK` years of each other."""
    return new.window is not None and bool(old.windows) and not _birth_differs(new, old)


def _stays(new: _Incoming, old: _PriorPerson) -> bool:
    """The person at this number shows they are the one who was: a name they
    had, the sex not differing, and a birth that agrees outright (or is
    missing on both sides)."""
    return bool(new.names & old.names) and not _sex_differs(new, old) and (
        _birth_fits(new, old) or (new.window is None and not old.windows))


def _name_shared(new: _Incoming, old: _PriorPerson) -> bool:
    """Part of the name is the same: the first given name or the last word."""
    return any(a and b and (a[0] == b[0] or a[-1] == b[-1])
               for a in new.names for b in old.names)


def _number_doubt(new: _Incoming, old: _PriorPerson,
                  by_name: dict[tuple[str, ...], list[_PriorPerson]],
                  moved: frozenset[str] = frozenset(),
                  gone: frozenset[str] = frozenset()) -> str:
    """Why the person holding this record number may not be the same person,
    or '' when they certainly are.

    Ancestry keeps a person's number for good, but other programs renumber
    everyone on export or hand a deleted person's number to the next one
    added, often a brother or sister with the same surname. So the number
    counts only with the name behind it:

    * same number and same name is the same person, unless the birth years
      disagree, or one side has none, and another earlier person of that
      name fits better (a father and son of one name, shifted by one);
    * same number with part of the name changed is the same person only when
      the birth date is unchanged (a respelled or newly found surname);
    * a different sex, or a wholly different name, is never the same person
      on the number alone."""
    was = f'record number {new.xref} belonged to {_who(old)} in the earlier download'
    if _sex_differs(new, old):
        return f'{was}, and the sex recorded here differs'
    if new.names & old.names:
        # Another earlier person of this name fits better when the births
        # here disagree and theirs does not, or when a birth is missing on one
        # side here and their own number is now held by someone else (an
        # undated father whose number his dated son now carries). A cousin
        # still at his own number, one deleted from the tree, or one who never
        # had a number is no rival. A dated newcomer needs their birth to agree.
        apart = _birth_differs(new, old)
        if apart or not _birth_fits(new, old):
            # A namesake could have moved here when someone else now holds
            # their number, or when their number is gone from this file and
            # nothing here shows this is the person who was at this number.
            could_move = moved | (frozenset() if _stays(new, old) else gone)
            for key in sorted(new.names):
                for other in by_name.get(key, []):
                    if other.ident == old.ident or _sex_differs(new, other):
                        continue
                    if apart:
                        rival = not _birth_differs(new, other)
                    elif new.window is not None:
                        rival = _birth_fits(new, other) and other.ident in could_move
                    else:
                        # Undated here: nothing can show which of the two he
                        # is, so a namesake who could have moved is doubt.
                        rival = other.ident in could_move
                    if rival:
                        return (f'{was}, but another earlier person of this name '
                                f'({_who(other)}) may be this one'
                                if new.window is None else
                                f'{was}, but the birth year here fits '
                                f'{_who(other)} better')
        return ''
    if _name_shared(new, old) and new.birth and new.birth in old.births:
        return ''
    return f'{was} and is {new.label} in this one'


def _match_people(incoming: list[_Incoming],
                  people: list[_PriorPerson]) -> tuple[dict[str, _Match], dict[str, _Held]]:
    """Match the newer file's people to the earlier download's, or say why not.

    Certain, in this order: the same record number with the name behind it
    (`_number_doubt`), then the same program id (`_UID`, which a program gives
    one person for good). Nothing else is a match.

    Everyone left is looked up by name and birth among the earlier people
    nobody matched: the same name with birth years that do not disagree, or
    part of the name with the very same birth date. One fit on both sides is
    reported as the likely person; more than one is reported as such. Neither
    is acted on. A person who fits nobody and whose number is unused is new.

    `people` is ordered newest import first, so a number or id two imports
    both know is read from the newer one."""
    by_xref: dict[str, _PriorPerson] = {}
    by_uid: dict[str, _PriorPerson] = {}
    by_name: dict[tuple[str, ...], list[_PriorPerson]] = {}
    by_birth: dict[str, list[_PriorPerson]] = {}
    for old in people:
        if old.xref:
            by_xref.setdefault(old.xref, old)
        if old.uid:
            by_uid.setdefault(old.uid, old)
        for key in old.names:
            by_name.setdefault(key, []).append(old)
        for birth in old.births:
            by_birth.setdefault(birth, []).append(old)

    found: dict[str, tuple[_PriorPerson, str]] = {}
    doubts: dict[str, tuple[str, _PriorPerson]] = {}
    # Earlier people still at their own number, under a name they had.
    # Moved: earlier people whose number someone in this file holds without
    # showing they are them. Gone: earlier people whose number is not in this
    # file at all (deleted, or shifted past its end).
    moved = frozenset(by_xref[n.xref].ident for n in incoming
                      if n.xref in by_xref and not _stays(n, by_xref[n.xref]))
    here = {n.xref for n in incoming}
    gone = frozenset(old.ident for xref, old in by_xref.items() if xref not in here)
    for new in incoming:
        old = by_xref.get(new.xref)
        if old is not None:
            doubt = _number_doubt(new, old, by_name, moved, gone)
            if not doubt and new.uid and old.uid and new.uid != old.uid:
                # The program's own id for a person never changes; a number
                # can. When they disagree, the number is not trusted.
                doubt = (f'record number {new.xref} belonged to {_who(old)} in the '
                         'earlier download, but the program\'s own id for this person '
                         'is a different one')
            if not doubt:
                found[new.xref] = (old, 'record number')
                continue
            doubts[new.xref] = (doubt, old)
        same_id = by_uid.get(new.uid) if new.uid else None
        if same_id is not None and not _sex_differs(new, same_id):
            found[new.xref] = (same_id, 'program id')
            doubts.pop(new.xref, None)

    matches: dict[str, _Match] = {}
    held: dict[str, _Held] = {}
    labels = {n.xref: n.label for n in incoming}
    # Counted by record, not by earlier entry: two earlier imports, or two
    # numbers of one import, can lead to the same record.
    claimed = Counter(old.ident for old, _how in found.values())
    for xref, (old, how) in found.items():
        if claimed[old.ident] > 1:
            held[xref] = _Held(xref, labels[xref],
                               f'{claimed[old.ident]} people in the newer file both fit '
                               f'{_who(old)}, so more than one person could be this one',
                               about=[old.pid] if old.pid else [])
        elif old.state == 'split':
            held[xref] = _Held(xref, labels[xref],
                               f'the earlier download\'s statements about {old.xref} now '
                               f'name more than one person in your archive ({old.note})',
                               about=old.note.split(', '))
        elif old.state == 'unknown':
            doubts[xref] = (f'{old.label} was in the earlier download, but your archive '
                            'does not record which person record that became', old)
        else:
            matches[xref] = _Match(xref, old.pid, how, old)

    # The likely person, for everyone not matched: looked for among earlier
    # people who have a record id and whom nobody matched for certain.
    taken = {m.pid for m in matches.values()}

    def fits(new: _Incoming) -> list[_PriorPerson]:
        out: dict[str, _PriorPerson] = {}
        for key in sorted(new.names):
            for old in by_name.get(key, []):
                if not _birth_differs(new, old):
                    out.setdefault(old.pid, old)
        for old in (by_birth.get(new.birth, []) if new.birth else []):
            if _name_shared(new, old):
                out.setdefault(old.pid, old)
        return [old for pid, old in out.items()
                if pid and pid not in taken and not _sex_differs(new, old)]

    open_people = [n for n in incoming if n.xref not in matches and n.xref not in held]
    candidates = {n.xref: fits(n) for n in open_people}
    rivals = Counter(old.pid for found_for in candidates.values() for old in found_for)
    for new in open_people:
        doubt, doubted = doubts.get(new.xref, ('', None))
        about = [doubted.pid] if doubted is not None and doubted.pid else []
        fit = candidates[new.xref]
        if len(fit) == 1 and rivals[fit[0].pid] == 1:
            lead = doubt or 'it has a different record number in this download'
            what = 'The name' if new.names & fit[0].names else 'Part of the name'
            held[new.xref] = _Held(
                new.xref, new.label,
                f'{lead}. {what} and the birth date fit {_who(fit[0])}',
                likely=fit[0], about=about + [fit[0].pid])
        elif fit:
            shown = ', '.join(_who(old) for old in fit[:3])
            more = f', and {len(fit) - 3} more' if len(fit) > 3 else ''
            why = ((f'more than one person in your archive fits this name'
                    f'{" and birth date" if new.birth else ""}')
                   if len(fit) > 1 else
                   'more than one person in the newer file fits the same record')
            lead = f'{doubt}. Also, ' if doubt else ''
            held[new.xref] = _Held(new.xref, new.label, f'{lead}{why} ({shown}{more})',
                                   about=about + [old.pid for old in fit])
        elif doubt:
            held[new.xref] = _Held(
                new.xref, new.label,
                f'{doubt}. This may be the same person renamed, or someone new '
                'given a used number' if doubted is not None and doubted.state != 'unknown'
                else doubt,
                about=about,
                resembles=doubted is not None and doubted.state == 'unknown')
    return matches, held


def _same_tree(incoming: list[_Incoming], priors: list[_PriorImport],
               ) -> tuple[list[_PriorImport], list[_PriorImport], str]:
    """(the earlier imports this file updates, same-tree copies set aside, note).

    The file is a newer download of an earlier import's tree when at least half
    of that import's people are in it: matched, or at least recognisable by
    name and birth date (which is all a renumbered copy offers). Half is enough
    because a tree only grows or is pruned between downloads, while a different
    family's tree shares almost nobody.

    Earlier imports that share people (a first download and its later updates)
    are read together. An import of the same tree made as a separate copy
    (`--as-new-tree`) shares no person with them; the newest is used and the
    rest are named in the plan. The note explains a first import beside an
    earlier one: why it is not being treated as an update."""
    qualifying: list[_PriorImport] = []
    share: dict[int, int] = {}
    closest: tuple[float, int, int, _PriorImport] | None = None
    for prior in priors:
        matches, held = _match_people(incoming, prior.people)
        fits = len(matches) + sum(1 for h in held.values() if h.resembles)
        size = (len({p.xref for p in prior.people if p.xref})
                or len({p.pid for p in prior.people if p.pid}))
        if not size:
            continue
        if fits and fits >= _SAME_TREE_SHARE * size:
            qualifying.append(prior)
            share[id(prior)] = fits
        if closest is None or fits / size > closest[0]:
            closest = (fits / size, fits, size, prior)
    if not qualifying:
        if closest is None:
            return [], [], ''
        _share, fits, size, prior = closest
        return [], [], (
            f'This does not look like a newer download of a tree you imported before '
            f'({fits:,} of the {size:,} people from {prior.file_name}, '
            f'{fmt_id_display(prior.sid)}, are in this file), so every person is '
            'planned as new.')
    # The newest import first. `created:` is a day, so two copies filed the
    # same day are told apart by how many of this file's people each one
    # holds, never by their random ids.
    first = max(qualifying, key=lambda p: (p.imported, share[id(p)]))
    lineage = [first]
    pids = {p.pid for p in first.people if p.pid}
    grew = True
    while grew:
        grew = False
        for prior in qualifying:
            if prior not in lineage and pids & {p.pid for p in prior.people if p.pid}:
                lineage.append(prior)
                pids |= {p.pid for p in prior.people if p.pid}
                grew = True
    lineage = [p for p in priors if p in lineage]
    return lineage, [p for p in qualifying if p not in lineage], ''


# ── What changed ─────────────────────────────────────────────────────────────

def _date_relation(old: str, new: str) -> str:
    """How a newer date stands to an earlier one: 'eq', 'adds' (fills a gap or
    narrows it: about 1878 to 12 Jan 1878), 'less' (says no more than before)
    or 'conflict' (the two cannot both be right, or cannot be compared)."""
    if not old or not new:
        return 'eq' if old == new else ('adds' if new else 'less')
    if old == new:
        return 'eq'
    spans = []
    for raw in (old, new):
        edtf = raw if is_valid_edtf(raw) else normalize_date(raw)
        if not edtf or not is_valid_edtf(edtf):
            return 'conflict'
        spans.append(edtf_bounds(edtf))
    (old_lo, old_hi), (new_lo, new_hi) = spans
    if (old_lo, old_hi) == (new_lo, new_hi):
        return 'eq'
    if new_hi < old_lo or old_hi < new_lo:
        return 'conflict'
    if old_lo <= new_lo and new_hi <= old_hi:
        return 'adds'
    if new_lo <= old_lo and old_hi <= new_hi:
        return 'less'
    return 'conflict'


def _place_relation(old: str, new: str) -> str:
    old_key, new_key = _place_key(old), _place_key(new)
    if old_key == new_key:
        return 'eq'
    if not old_key or not new_key:
        return 'adds' if new_key else 'less'
    return 'conflict'


def _fields_relation(old: _OldFact, claim: _Claim) -> str:
    """'changed', 'more detail' or 'same' for a newer statement against one the
    archive holds, by date and place as the record reads today."""
    relations = (_date_relation(old.date, claim.date or ''),
                 _place_relation(old.place, claim.place_text or ''))
    if (old.joined and not old.date and not claim.date and old.raw_date and claim.raw_date
            and _norm_text(old.raw_date) != _norm_text(claim.raw_date)):
        # Neither date could be read, and the wording changed: it cannot be
        # compared, so it goes to him as a change rather than being lost.
        # (Without the filed copy the earlier wording is not known.)
        return 'changed'
    if (old.joined and not old.date and not old.raw_date and claim.raw_date
            and 'conflict' not in relations):
        return 'more detail'        # a date wording where there was none
    if 'conflict' in relations:
        return 'changed'
    return 'more detail' if 'adds' in relations else 'same'


def _judge(fact: _Fact, olds: list[_OldFact], parents_now: frozenset[str]) -> None:
    """Decide what one statement of the newer file is, against what the earlier
    downloads said about the same thing (`olds`, same key).

    First the tree's own words. If an earlier download said exactly this, the
    tree has not changed and nothing is planned: 'same' while a claim for it is
    live, 'decided' when he rejected, superseded or deleted it. This comes
    before any look at dates, so a date he corrected by hand is never argued
    with by a download that only repeats what it said last year.

    Only a statement the tree changed is compared with the archive as it reads
    now: 'same' if the archive already says it (he made the same correction
    himself), 'changed' if it cannot be true together with a live claim (it
    will `contradicts:` those and get one question), 'more detail' if it only
    fills in or narrows, 'new' if nothing live speaks to it.

    `parents_now` is every parent the newer file gives this child in this kind
    of tie, across all its families: a parent the earlier download named is
    only contradicted when the newer file no longer names them at all.

    A claim he re-pointed to someone else is in `olds` twice: under its people
    now, and (`orig_only`) under the people the tree named. The second copy
    counts only for the tree's own words, so the tree repeating what it said
    is not planned again, and a claim now about someone else is never argued
    with by dates that were not about them."""
    live = [o for o in olds if o.live]
    settled = [o for o in olds if not o.live]
    if fact.kind == 'multi':
        fact.outcome = 'same' if live else ('decided' if settled else 'new')
        return
    if fact.kind == 'parents':
        def said(o: _OldFact) -> bool:
            return o.orig_parents == fact.parents

        def turned_down(o: _OldFact) -> bool:
            named = o.orig_parents if o.orig_only else o.parents
            return bool(named) and named <= fact.parents

        if any(said(o) or (not o.orig_only and o.parents >= fact.parents
                           and not o.parents - parents_now) for o in live):
            fact.outcome = 'same'
        elif any(said(o) or (not o.orig_only and o.parents == fact.parents) for o in settled):
            fact.outcome = 'decided'
        elif any(turned_down(o) for o in settled):
            # Every parent of a tie he rejected or deleted is named again, now
            # beside a parent the tree added. Those parents stay turned down;
            # only the parents left are judged (and, if new, planned).
            gone = frozenset().union(*((o.orig_parents if o.orig_only else o.parents)
                                       for o in settled if turned_down(o)))
            fact.turned_down |= gone
            fact.parents = fact.parents - gone
            if not fact.parents:
                fact.outcome = 'decided'
                return
            _judge(fact, olds, parents_now)
        else:
            live = [o for o in live if not o.orig_only]
            against = [o for o in live if o.parents - parents_now]
            fact.outcome = 'changed' if against else 'new'
            fact.was = against
            fact.against = [o.cid for o in against]
        return
    def worded(o: _OldFact) -> bool:
        return o.text == (fact.text if o.joined or not fact.named_text else fact.named_text)

    if any(worded(o) for o in live):
        fact.outcome = 'same'
        return
    if any(worded(o) for o in settled):
        fact.outcome = 'decided'
        return
    # Dates and places are compared only with claims still about these people.
    live = [o for o in live if not o.orig_only]
    settled = [o for o in settled if not o.orig_only]
    if not live:
        decided = any(_fields_relation(o, fact.claim) == 'same' for o in settled)
        fact.outcome = 'decided' if decided else 'new'
    else:
        relations = [(_fields_relation(o, fact.claim), o) for o in live]
        if any(rel == 'same' for rel, _o in relations):
            fact.outcome = 'same'
        elif any(rel == 'changed' for rel, _o in relations):
            fact.outcome = 'changed'
            fact.was = [o for rel, o in relations if rel == 'changed']
            fact.against = [o.cid for o in fact.was]
        else:
            fact.outcome = 'more detail'
            fact.was = [o for _rel, o in relations]


def _when_where(date: str | None, place: str | None) -> str:
    bits = []
    if date:
        bits.append(humanize_edtf(date) if is_valid_edtf(date) else date)
    if place:
        bits.append(place)
    return ', '.join(bits) or 'no date or place'


def _plan_update(plan: ImportPlan, incoming: list[_Incoming], lineage: list[_PriorImport],
                 others: list[_PriorImport], existing: list[dict]) -> None:
    """Turn a freshly mapped plan into an update of the earlier imports in
    `lineage`, in place: match the people, keep only what is new or changed.

    After this `plan.stubs` holds the new people (ids are `new:@I11@`
    placeholders), `plan.claims` the statements that would be added, and
    `plan.facts` every statement of a matched or new person with its outcome.
    A statement naming anyone held or removed is left out and counted, so no
    tie is planned to a person who may be the wrong one."""
    by_pid = {normalize_id(p['pid']): p for p in existing if p['pid']}
    matches, held = _match_people(incoming, [p for prior in lineage for p in prior.people])
    became: dict[str, str] = {}
    dropped: set[str] = set()
    for new in incoming:
        match = matches.get(new.xref)
        if match is not None and match.person.state == 'present':
            became[_NEW_PERSON + new.xref] = match.pid
        elif match is not None or new.xref in held:
            dropped.add(_NEW_PERSON + new.xref)
    new_stubs = [s for s in plan.stubs if s.xref not in matches and s.xref not in held]
    file_names = {s.pid: s.name or '(no name)' for s in new_stubs}

    def name_of(pid: str) -> str:
        if pid in file_names:
            return file_names[pid]
        record = by_pid.get(pid)
        name = record['name'] if record else ''
        return name if name and name != 'unknown' else fmt_id_display(pid)

    def names_of(pids) -> str:
        return ' and '.join(sorted(name_of(p) for p in pids)) or 'unknown parents'

    facts: list[_Fact] = []
    held_statements = 0
    for claim in plan.claims:
        if dropped & set(claim.persons):
            held_statements += 1
            continue
        _rename_people(claim, became)
        facts.append(_new_fact(claim))

    olds: dict[tuple, list[_OldFact]] = {}
    for prior in lineage:
        for old in prior.facts:
            olds.setdefault(old.key, []).append(old)
            if old.orig_key is not None:
                olds.setdefault(old.orig_key, []).append(
                    replace(old, key=old.orig_key, orig_only=True))
            if old.now_key is not None:
                olds.setdefault(old.now_key, []).append(replace(old, key=old.now_key))
    parents_now: dict[tuple, frozenset[str]] = {}
    for fact in facts:
        if fact.kind == 'parents':
            parents_now[fact.key] = parents_now.get(fact.key, frozenset()) | fact.parents
    for fact in facts:
        _judge(fact, olds.get(fact.key, []), parents_now.get(fact.key, frozenset()))
        if fact.turned_down and fact.outcome != 'decided':
            # The tie is planned with only the parents he has not turned down.
            claim = fact.claim
            keep = [p for p in claim.roles['parent'] if p in fact.parents]
            claim.roles = {**claim.roles, 'parent': keep}
            claim.persons = [claim.roles['child']] + keep
            nature = f' ({claim.subtype})' if claim.subtype else ''
            claim.value = (f'{name_of(claim.roles["child"])} is a child{nature} of '
                           f'{names_of(keep)} (GEDCOM {claim.fam_xref})')

    questions: list[_Question] = []
    for fact in facts:
        if fact.outcome not in ('changed', 'more detail'):
            continue
        claim = fact.claim
        if fact.kind == 'parents':
            people = [claim.roles['child']]
            fact.subject = f'{name_of(people[0])}, parents'
            heading = _PARENT_HEADINGS.get(claim.subtype or '', "Who were {a}'s parents?")
            fact.before = '; or '.join(dict.fromkeys(names_of(o.parents) for o in fact.was))
            fact.now = names_of(fact.parents)
        else:
            people = list(claim.roles['spouse']) if fact.kind == 'couple' else [claim.persons[0]]
            fact.subject = f'{" and ".join(name_of(p) for p in people)}, {claim.claim_type}'
            heading = _QUESTION_HEADINGS[claim.claim_type]
            fact.before = '; or '.join(dict.fromkeys(
                _when_where(o.date, o.place) for o in fact.was))
            fact.now = _when_where(claim.date, claim.place_text)
        if fact.outcome == 'changed':
            questions.append(_Question(
                heading=heading.format(a=name_of(people[0]), b=name_of(people[-1])),
                refs=fact.against + list(dict.fromkeys(people)),
                fact=fact, new_file=plan.ged_path.name))

    matched_here = {m.pid for m in matches.values() if m.person.state == 'present'}
    spoken_for = matched_here | {pid for h in held.values() for pid in h.about}
    keys_now = {fact.key for fact in facts}
    gone_facts: list[str] = []
    gone_people: dict[str, str] = {}
    for prior in lineage:
        for old in prior.facts:
            # Only a claim still on file and tied to its line can be said to be
            # gone; without the filed copy every claim of the record is tried.
            if (old.live and old.cid and (old.joined or not prior.copy_read)
                    and old.people <= matched_here and old.key not in keys_now
                    and old.orig_key not in keys_now and old.now_key not in keys_now):
                gone_facts.append(_GEDCOM_TAIL_RE.sub('', old.value))
        for person in prior.people:
            if person.state == 'present' and person.pid not in spoken_for:
                gone_people.setdefault(person.pid, _who(person))

    name_changes: list[str] = []
    death_now: list[str] = []
    stub_of = {s.xref: s for s in plan.stubs}
    has_death = {f.claim.persons[0] for f in facts if f.claim.claim_type == 'death'}
    for xref, match in matches.items():
        if match.person.state != 'present':
            continue
        pid, record = fmt_id_display(match.pid), by_pid.get(match.pid)
        stub = stub_of[xref]
        if stub.name and _name_key(stub.name) not in match.person.names:
            name_changes.append(f'{pid} {name_of(match.pid)}: the newer file says {stub.name}')
        for variant in stub.variants:
            if variant and _name_key(variant) not in match.person.names:
                name_changes.append(f'{pid} {name_of(match.pid)}: the newer file also gives '
                                    f'the name {variant}')
        living = str((record or {}).get('meta', {}).get('living', '')).strip().lower()
        if match.pid in has_death and living != 'false':
            death_now.append(f'{pid} {name_of(match.pid)}: to change, run '
                             f'fha person set-living {pid} false')

    plan.mode = 'update'
    plan.prior, plan.other_copies = lineage, [fmt_id_display(p.sid) for p in others]
    plan.unread_copies = [p.file_alias or p.file_name for p in lineage if not p.copy_read]
    plan.matches, plan.held = matches, held
    plan.facts, plan.questions = facts, questions
    plan.held_statements = held_statements
    plan.gone_facts, plan.gone_people = gone_facts, list(gone_people.values())
    plan.removed_people = [
        f'{xref} {stub_of[xref].name or "(no name)"} (was {fmt_id_display(m.pid)})'
        for xref, m in matches.items() if m.person.state == 'removed']
    plan.name_changes, plan.death_now = name_changes, death_now
    plan.stubs = new_stubs
    # "The new record shows the most exact one" is about records the plan
    # would make: a matched person keeps the record he already has.
    new_xrefs = {s.xref for s in new_stubs}
    plan.date_conflicts = [c for c in plan.date_conflicts
                           if c.split(' ', 1)[0] in new_xrefs]
    plan.claims = [f.claim for f in facts if f.outcome in ('new', 'more detail', 'changed')]
    # Every record is a candidate, matched ones too: a second Rose Hartley
    # added to the tree beside the one already matched is the duplicate he
    # most needs to hear about.
    plan.duplicates = _find_duplicates(plan.archive_root, new_stubs, existing)


def _reimport_question_block(question: _Question, *, new_cid: str, today: str) -> str:
    """The `notes/questions.md` entry for one changed statement (SPEC §17b).

    Both claims and the person are in `refs:`, so lint E009 finds the question
    for the `contradicts:` pair and the question shows on the person's page.
    The words say what is true: the archive has one thing and the newer
    download says another. They do not say "the earlier download said", since
    he may have corrected the earlier claim himself."""
    earlier = ', '.join(fmt_id_display(cid) for cid in question.fact.against)
    refs = ', '.join(fmt_id_display(r) for r in [new_cid] + question.refs)
    return '\n'.join([
        f'## Q: {question.heading}',
        '- origin: tool',
        '- status: open',
        f'- refs: [{refs}]',
        '- context:',
        f'  - {today} (tool): The newer download of your tree ({question.new_file}) says '
        f'{question.fact.now}. Your archive has {question.fact.before} ({earlier}). Neither was '
        'changed: the newer one is filed beside it as a suggested statement. When you '
        'review them, accept the one the records support.',
        '',
    ])
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


# ── Photo links (what his old program linked to each person) ──────────────────
#
# For him: a tree from another program often knows which photo is whose. This
# reads those links, looks for the same photos in his photo folders, and only
# LISTS them (plan + source record). Putting a name on a photo stays with
# `fha photoindex tag-person`, one photo at a time, with his yes. Nothing here
# opens a photo file for writing; the catalog is opened read-only.

_PHOTO_EXTS = PHOTO_EXTENSIONS   # the catalog's own list: a HEIF or raw photo it holds is matched too
_MISSING_PREFIX = 'MISSING:'     # photoindex's key for a vanished file (own copy: tools never import tools)
_SCHEME_RE = re.compile(r'^([A-Za-z][A-Za-z0-9+.\-]+):')   # 2+ letters: `C:` is a drive, not a scheme
_FOLDER_HINT_MIN = 5


@dataclass
class _PhotoLink:
    person_xref: str
    pid: str
    name: str
    raw_file: str            # as the GEDCOM wrote it (never printed whole: it can carry a home folder)
    title: str
    primary: bool
    line: int
    kind: str = 'local'      # 'local' | 'web'
    host: str = ''           # web links: host only (the rest can carry account tokens)
    basename: str = ''
    folder: str = ''         # last folder name of the old path
    status: str = ''         # 'matched' | 'ambiguous' | 'not found' | 'already tagged'
    how: str = ''            # 'same file' | 'same file name'
    alias: str = ''          # catalog alias path when matched
    candidates: list[str] = field(default_factory=list)


def _split_photo_value(raw: str) -> tuple[str, str, str, str] | None:
    """FILE value -> (kind, path, host, basename), or None to ignore it.

    Windows and POSIX separators both read as '/'; a `file://` URL is decoded to
    the path it names; any other `scheme:` is a web link, reduced to its host."""
    value = raw.strip().strip('"')
    if not value:
        return None
    m = _SCHEME_RE.match(value)
    if m and m.group(1).lower() != 'file':
        host = (urlsplit(value).hostname or '').lower().removeprefix('www.')
        return 'web', '', host or m.group(1).lower(), ''
    if m:                                    # file:///C:/x.jpg, file:///home/x.jpg
        path = unquote(urlsplit(value).path)
        if re.match(r'^/[A-Za-z]:', path):
            path = path[1:]
    else:
        path = value
    path = path.replace('\\', '/')
    base = path.rstrip('/').rsplit('/', 1)[-1]
    if Path(base).suffix.lower() not in _PHOTO_EXTS:
        return None                          # a document, a video, a bare id: not a photo link
    return 'local', path, '', base


def _collect_photo_links(records: list[_Node], xref_pid: dict[str, str],
                         names: dict[str, str], consumed: set[int]) -> list[_PhotoLink]:
    """Every image link under each person (and under their events), in file order.

    Inline `OBJE` carries `FILE` children; a pointer `OBJE @O1@` names a level-0
    record that does. The OBJE/FILE/FORM/TITL/_PRIM lines read are marked
    consumed so the plan stops calling them unread. Media under a source
    citation (on the person or on one of their events) or a family is not
    read."""
    objects = {r.xref: r for r in records if r.tag == 'OBJE' and r.xref}
    links: list[_PhotoLink] = []
    seen: set[tuple[str, str]] = set()

    def eat(node: _Node) -> None:
        consumed.add(id(node))
        for c in node.children:
            if c.tag in ('FILE', 'FORM', 'TITL', '_PRIM', 'MEDI', '_TYPE', 'TYPE'):
                eat(c)

    for indi in (r for r in records if r.tag == 'INDI'):
        pid = xref_pid.get(indi.xref)
        if not pid:
            continue
        photo_ptr = indi.child('_PHOTO')
        primary_target = photo_ptr.value.strip() if photo_ptr else ''
        if photo_ptr is not None:
            consumed.add(id(photo_ptr))
        # The person and their events/attributes. A source citation's media
        # (`1 SOUR` / `2 OBJE`) is a scan of the record that supports them,
        # not a photo of them, so it is never offered for tagging.
        holders = [indi] + [c for c in indi.children if c.tag not in ('OBJE', 'SOUR')]
        for holder in holders:
            for obje in holder.children_tagged('OBJE'):
                ref = obje.value.strip()
                pointer = bool(re.fullmatch(r'@[^@\s]+@', ref))
                body = objects.get(ref) if pointer else obje
                if body is None:
                    continue
                eat(obje)
                if body is not obje:
                    eat(body)
                prim = (pointer and ref == primary_target) or any(
                    n.value.strip().upper() == 'Y'
                    for n in body.children_tagged('_PRIM') + obje.children_tagged('_PRIM'))
                for f in body.children_tagged('FILE'):
                    parsed = _split_photo_value(f.value)
                    if parsed is None:
                        continue
                    kind, path, host, base = parsed
                    if _media_type(f, body, obje) in _NOT_A_PORTRAIT:
                        continue                 # a scanned document or gravestone, not their photo
                    key = (pid, f.value.strip().replace('\\', '/').casefold())
                    if key in seen:
                        continue
                    seen.add(key)
                    title = (f.child_value('TITL') or body.child_value('TITL')
                             or obje.child_value('TITL')).strip()
                    prim_f = prim or any(n.value.strip().upper() == 'Y'
                                         for n in f.children_tagged('_PRIM'))
                    links.append(_PhotoLink(
                        person_xref=indi.xref, pid=pid, name=names.get(indi.xref, ''),
                        raw_file=path, title=title, primary=prim_f, line=f.line,
                        kind=kind, host=host, basename=base,
                        folder=_old_folder_name(path)))
    return links


# A media type the old program gave that says "not a photo of the person"
# (GEDCOM MEDI values, and the words Ancestry / Family Tree Maker use).
# No type, or `photo` / `electronic` / `image`, is still read as a photo.
_NOT_A_PORTRAIT = frozenset({
    'document', 'census', 'certificate', 'record', 'book', 'card', 'fiche',
    'film', 'magazine', 'manuscript', 'map', 'newspaper', 'obituary', 'story',
    'tombstone', 'headstone', 'gravestone', 'audio', 'video',
})


def _media_type(*nodes: _Node) -> str:
    """The media type the old program gave a FILE/OBJE (`FORM`/`TYPE`,
    `FORM`/`MEDI`, `MEDI`, `_TYPE`), lowercased; '' when none."""
    for node in nodes:
        for c in node.children:
            if c.tag == 'FORM':
                for cc in c.children:
                    if cc.tag in ('TYPE', 'MEDI') and cc.value.strip():
                        return cc.value.strip().casefold()
            elif c.tag in ('MEDI', '_TYPE', 'TYPE') and c.value.strip():
                return c.value.strip().casefold()
    return ''


def _old_folder_name(path: str) -> str:
    """The last folder name of an old photo path, for the plan's "most were
    in a folder called ..." hint. Never a home folder: under `Users/` or
    `home/` that name is his account name."""
    parts = [p for p in path.rstrip('/').split('/')[:-1] if p]
    if not parts:
        return ''
    if len(parts) >= 2 and parts[-2].casefold() in ('users', 'home', 'documents and settings'):
        return ''
    return parts[-1]


def _exact_photo_alias(archive_root: Path, fha_config: dict, raw: str, ged_dir: Path,
                       by_alias: dict[str, str]) -> str:
    """The catalog alias of the file the GEDCOM names, when that very path exists
    here (absolute, or beside the .ged); else ''. Only existence is checked."""
    try:
        cand = Path(raw)
        if not cand.is_absolute():
            cand = ged_dir / raw
        if not cand.is_file():
            return ''
        alias = path_to_alias(cand, 'photos', fha_config, archive_root)
    except (OSError, ValueError, RuntimeError):
        return ''
    return by_alias.get(alias.casefold(), '')


def _match_photo_links(archive_root: Path, fha_config: dict, links: list[_PhotoLink],
                       ged_dir: Path) -> str:
    """Fill each local link's match, read-only. Returns the catalog state.

    'ready' when the catalog was used, else the `photoindex_status` word
    ('absent', 'unreadable', 'old-schema') so the plan can say how to fix it.
    A stale catalog is still used: this only suggests, and `tag-person`
    re-checks freshness itself before it writes anything."""
    local = [lk for lk in links if lk.kind == 'local']
    if not local:
        return 'ready'
    state, _lag = photoindex_status(archive_root, fha_config)
    if state in ('absent', 'unreadable', 'old-schema'):
        return state
    db = archive_root / '.cache' / 'photos.sqlite'
    try:
        conn = sqlite3.connect(sqlite_readonly_uri(db), uri=True)
    except (sqlite3.Error, OSError, ValueError):
        return 'unreadable'
    try:
        rows = conn.execute(
            'SELECT path, group_id, is_primary FROM photos WHERE path NOT LIKE ?',
            (f'{_MISSING_PREFIX}%',)).fetchall()
        tagged = {(p, (k or '').lower()) for p, k in conn.execute(
            "SELECT path, keyword FROM photo_keywords WHERE LOWER(keyword) LIKE 'p-%'")}
    except sqlite3.Error:
        return 'unreadable'
    finally:
        conn.close()

    by_alias = {r[0].casefold(): r[0] for r in rows}
    group_of = {r[0]: (r[1] or f'#{r[0]}') for r in rows}
    groups: dict[str, list[tuple[str, int]]] = {}
    for path, _gid, prim in rows:
        groups.setdefault(group_of[path], []).append((path, int(prim or 0)))
    by_name: dict[str, set[str]] = {}
    for path, gid in group_of.items():
        by_name.setdefault(path.rsplit('/', 1)[-1].casefold(), set()).add(gid)

    def group_rep(gid: str) -> str:
        """One photo per group: the chosen primary, so fronts/backs/copies count once."""
        return sorted(groups[gid], key=lambda m: (-m[1], m[0]))[0][0]

    for lk in local:
        alias = _exact_photo_alias(archive_root, fha_config, lk.raw_file, ged_dir, by_alias)
        if alias:
            lk.how, lk.alias = 'same file', alias
            gid = group_of[alias]
        else:
            gids = sorted(by_name.get(lk.basename.casefold(), ()))
            if not gids:
                lk.status = 'not found'
                continue
            if len(gids) > 1:
                lk.status = 'ambiguous'
                lk.candidates = sorted(group_rep(g) for g in gids)   # every one: his photo may be the 5th
                continue
            gid = gids[0]
            lk.how, lk.alias = 'same file name', group_rep(gid)
        lk.status = 'matched'
        want = lk.pid.lower()
        if any((p, want) in tagged for p, _ in groups[gid]):
            lk.status = 'already tagged'
    return 'ready'


def _photo_folder_hint(links: list[_PhotoLink]) -> str:
    """The old photo folder's name when many missing photos came from one folder
    (the name only: the full path carries his Windows user name)."""
    folders = Counter(lk.folder for lk in links
                      if lk.kind == 'local' and lk.status == 'not found' and lk.folder)
    if not folders:
        return ''
    name, n = folders.most_common(1)[0]
    return name if n >= _FOLDER_HINT_MIN else ''


def _photo_web_lines(links: list[_PhotoLink]) -> list[str]:
    hosts = Counter(lk.host for lk in links if lk.kind == 'web')
    if not hosts:
        return []
    total = sum(hosts.values())
    return [f'    {total:,} {"is a web link" if total == 1 else "are web links"} '
            f'({", ".join(sorted(hosts))}), not photos on this computer.']


def _photo_plan_lines(plan: 'ImportPlan', *, capped: bool) -> list[str]:
    """The 'Photos your old program linked' plan section ([] when there are none)."""
    links = plan.photo_links
    if not links:
        return []
    cap = _PLAN_DETAIL_CAP if capped else len(links)
    people = len({lk.person_xref for lk in links})
    lines = ['']
    if plan.photo_catalog != 'ready':
        fix = ("Your photo catalog isn't built yet, so they can't be looked for."
               if plan.photo_catalog == 'absent' else
               "Your photo catalog can't be read, so they can't be looked for.")
        lines.append(f'  Photos your old program linked: {len(links):,}. {fix} '
                     'Run fha photoindex, then run this plan again.')
        return lines + _photo_web_lines(links)
    lines.append(f'  Photos your old program linked: {len(links):,} '
                 f'({people:,} {"person" if people == 1 else "people"})')
    by = {s: [lk for lk in links if lk.kind == 'local' and lk.status == s]
          for s in ('matched', 'ambiguous', 'not found', 'already tagged')}
    if by['matched']:
        n = len(by['matched'])
        lines.append(f'    {n:,} {"is" if n == 1 else "are"} already in your photo folders:')
        for lk in by['matched'][:cap]:
            lines.append(f'      {lk.name or lk.person_xref}   {lk.alias} ({lk.how})')
        if len(by['matched']) > cap:
            lines.append(f'      ... and {len(by["matched"]) - cap:,} more '
                         '(use --plan-out FILE to write the full plan)')
    if by['already tagged']:
        lines.append(f'    {len(by["already tagged"]):,} already carry that person\'s '
                     'name in your photo folders (nothing to do).')
    if by['ambiguous']:
        n = len(by['ambiguous'])
        lines.append(f'    {n:,} {"matches" if n == 1 else "match"} more than one photo by name - '
                     'pick the right one when you tag:')
        for lk in by['ambiguous'][:cap]:
            shown = lk.candidates[:3] if capped else lk.candidates
            more = len(lk.candidates) - len(shown)
            lines.append(f'      {lk.name or lk.person_xref}   {lk.basename} could be '
                         + ' or '.join(shown)
                         + (f' (or {more:,} more, listed in the full plan and the '
                            "source's Notes)" if more else ''))
        if len(by['ambiguous']) > cap:
            lines.append(f'      ... and {len(by["ambiguous"]) - cap:,} more')
    if by['not found']:
        hint = _photo_folder_hint(links)
        tail = (f' Most were in a folder called "{hint}"; if you still have it, copy '
                'it into your photo folders, run fha photoindex, then run this plan '
                'again.') if hint else ''
        lines.append(f'    {len(by["not found"]):,} were not found in your photo '
                     f'folders.{tail}')
    return lines + _photo_web_lines(links)


def _photo_notes_lines(plan: 'ImportPlan', links_writer) -> list[str]:
    """The source record's durable photo list: the find-photos skill's work queue.

    Matched photos are named by alias path; ambiguous and not-found ones by file
    name only; web links are counted, never listed. It survives a cache wipe and
    reads without any tool."""
    unchecked = plan.photo_catalog != 'ready'
    pending = [lk for lk in plan.photo_links
               if lk.kind == 'local' and (unchecked or lk.status in (
                   'matched', 'ambiguous', 'not found'))]
    web = sum(1 for lk in plan.photo_links if lk.kind == 'web')
    if not pending and not web:
        return []
    lines = ['', 'Photos the old program linked to people. Each is a question for '
                 'someone who knows the faces, answered one photo at a time with '
                 '`fha photoindex tag-person`. Nothing was written to any photo.']
    for lk in pending:
        who = link_entry(lk.pid, lk.name, links_writer)
        title = f' "{lk.title}"' if lk.title else ''
        if lk.status == 'matched':
            lines.append(f'- {who}: {lk.alias}{title} (found by its '
                         f'{lk.how.removeprefix("same ")})')
        elif lk.status == 'ambiguous':
            lines.append(f'- {who}: {lk.basename}{title} (matches more than one '
                         'photo: ' + ', '.join(lk.candidates) + ')')
        elif unchecked:
            why = ('was not built' if plan.photo_catalog == 'absent'
                   else 'could not be read')
            lines.append(f'- {who}: {lk.basename}{title} (not looked for: the photo '
                         f'catalog {why} when imported)')
        else:
            lines.append(f'- {who}: {lk.basename}{title} (not in the photo folders '
                         'when imported)')
    if web:
        lines.append(f'- {web:,} more {"was a web link" if web == 1 else "were web links"}'
                     ', not photos on this computer.')
    return lines


_PHOTO_HANDOFF = """\
{n:,} {photos} your old program linked {are} already in your photo folders. To put each
person's name on their photo (one photo at a time, each with your yes):
  "go through the photos my old tree linked"
or run, for example (the first line brings the photo catalog up to date
with the people just added; tag-person needs that):
  {launcher} photoindex
  {command}{caution}
The full list is in the new source's Notes."""


def _photo_handoff_text(plan: 'ImportPlan') -> str:
    """The after-apply hand-off ('' when no photo matched). Printed only after
    apply: a dry-run p-id is minted but not reserved, so a re-plan mints others."""
    matched = [lk for lk in plan.photo_links if lk.status == 'matched']
    if not matched:
        return ''
    first = matched[0]
    command = (fha_launcher() + ' photoindex tag-person ' + shell_quote(fmt_id_display(first.pid))
               + ' --paths ' + shell_quote(first.alias))
    n = len(matched)
    caution = ('\n(That photo matched by its file name only: look at it before you say yes.)'
               if first.how == 'same file name' else '')
    return _PHOTO_HANDOFF.format(caution=caution, launcher=fha_launcher(),
        n=n, photos='photo' if n == 1 else 'photos', are='is' if n == 1 else 'are',
        command=command)


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


@dataclass
class _Mapped:
    """Everything one pass over a file's INDI and FAM records produced."""

    stubs: list[_PersonStub]
    claims: list[_Claim]
    claim_count: int               # how many C-ids the claims still need
    consumed: set[int]
    sour_use: Counter
    date_downgrades: list[str]
    date_loose: list[str]          # read by the free-form fallback (#293)
    warnings: list[str]
    date_conflicts: list[str] = field(default_factory=list)   # disagreeing births/deaths (#449)


class _DeferredPool(list):
    """Hands out placeholders and counts them; real C-ids land afterward."""

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def pop(self, index: int = 0) -> str:  # type: ignore[override]
        self.count += 1
        return f'__C{self.count}__'


def _map_file(indis: list[_Node], fams: list[_Node], xref_pid: dict[str, str],
              sour_titles: dict[str, str], consumed: set[int]) -> _Mapped:
    """Map every INDI, then every FAM, into stubs and claims (no ids minted).

    One function for the three files the importer reads the same way: a first
    import, a newer download, and the filed copy of an earlier download that a
    newer one is compared with. Claims carry `__C1__` placeholders; the caller
    mints real C-ids only when it will write. `consumed` is added to in place."""
    cid_pool = _DeferredPool()
    out = _Mapped(stubs=[], claims=[], claim_count=0, consumed=consumed,
                  sour_use=Counter(), date_downgrades=[], date_loose=[], warnings=[])
    pedi: dict[tuple[str, str], str] = {}
    for indi in indis:
        stub, indi_claims, pedi_map = _map_individual(
            indi, xref_pid[indi.xref], cid_pool, sour_titles, out.sour_use,
            consumed, out.date_downgrades, out.date_loose, out.date_conflicts)
        out.stubs.append(stub)
        out.claims.extend(indi_claims)
        for fam_xref, pedi_value in pedi_map.items():
            pedi[(indi.xref, fam_xref)] = pedi_value

    display_names = {s.xref: s.name for s in out.stubs if s.name}
    for fam in fams:
        out.claims.extend(_map_family(
            fam, xref_pid, display_names, cid_pool, sour_titles, out.sour_use,
            pedi, consumed, out.date_downgrades, out.date_loose, out.warnings))
    out.claim_count = cid_pool.count
    return out


def build_plan(archive_root: Path, fha_config: dict, ged_path: Path, *,
               as_new_tree: bool = False) -> ImportPlan:
    """Parse the GEDCOM and plan every record to write. No filesystem writes.

    Guard order matters: file-exists → encoding → looks-like-GEDCOM →
    self-import → no-individuals → duplicate-xref → already-imported sentinel,
    all BEFORE any minting, so every refusal is cheap and stateless. Minting
    is exactly three `mint_ids` calls (S, P, C) - one tree scan each, never
    one per record - which is what keeps a 2,000-person import tractable.

    A file that is a newer download of a tree already imported gets an update
    plan instead (`mode='update'`, see `_plan_update`): only the new people and
    the statements that changed, and no ids minted. `as_new_tree` skips that
    check and plans every person as new."""
    if not ged_path.is_file():
        raise GedcomImportError(
            f'{ged_path} does not exist. Check the path (the file Ancestry gives '
            'you is usually called something like "family-tree.ged" in your Downloads).')

    raw = ged_path.read_bytes()
    ged_hash = hashlib.sha256(raw).hexdigest()
    text = _decode_gedcom_bytes(raw, ged_path)
    records, malformed, joined = parse_gedcom(text)
    markup_cleaned = _clean_vendor_markup(text, records)

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
            # _oneline: a CONT-folded program name must never carry a newline
            # into the rendered record's citation scalar (see _oneline).
            repository = (_oneline(name_node.value) if name_node else '') \
                or _oneline(head_sour.value)
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
            export_date_raw = _oneline(date_node.value)
            # Strict grammar only: HEAD DATE is written by the exporting
            # program, never typed, and is not counted as a free-form read.
            source_date = _strict_gedcom_date_to_edtf(export_date_raw)
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

    # A file that defines the same xref twice is malformed, and the damage is
    # quiet: both INDIs would map to one minted P-id (one id burned, two stub
    # writes to one path, the second truncating the first) and FAM/SOUR
    # lookups would tangle. A malformed GEDCOM is refused, never guessed.
    dup_xrefs = sorted(
        xref for xref, n in Counter(r.xref for r in records if r.xref).items()
        if n > 1)
    if dup_xrefs:
        shown = ', '.join(dup_xrefs[:5])
        if len(dup_xrefs) > 5:
            shown += f', ... ({len(dup_xrefs)} total)'
        raise GedcomImportError(
            f'{ged_path.name} defines the same record id more than once ({shown}) '
            '- the file is malformed, and importing it would silently merge '
            'different people into one. Re-export the tree from your genealogy '
            'program (or fix the duplicate lines in a copy of the file), then '
            're-run the import.')

    audit_path = archive_root / _AUDIT_DIR / f'{ged_hash[:12]}.csv'
    if audit_path.exists():
        header = _read_audit_header(audit_path)
        raise GedcomImportError(
            _already_imported(header.get('imported'), header.get('source_id')))

    # Has this tree been imported before? The answer comes from the source
    # records themselves, so it survives a lost `.cache` or a new computer.
    # people/ is only read when an earlier GEDCOM source exists.
    existing: list[dict] | None = None
    priors: list[_PriorImport] = []
    unreadable: list[Path] = []
    record_paths = _gedcom_source_paths(archive_root)
    if record_paths:
        existing = _scan_existing_persons(archive_root)
        priors, unreadable = _read_prior_imports(
            archive_root, fha_config, record_paths, existing)
    same_file = next((p for p in priors if p.copy_hash == ged_hash), None)
    if same_file is not None:
        raise GedcomImportError(_already_imported(same_file.imported, same_file.sid))
    if as_new_tree:
        priors, unreadable = [], []

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
    for path in unreadable:
        # Said out loud: an earlier import the plan cannot read is one it
        # cannot compare with, and the people in it would be planned twice.
        warnings.append(
            f'{_rel_display(path, archive_root)} is the record of an earlier GEDCOM '
            'import, but it could not be read, so this plan could not check whether '
            'the file is a newer download of that tree. Run fha lint to see what is '
            'wrong with the record, fix it, then run this plan again.')

    # With an earlier import on file, read the people of this file first (no
    # ids yet) and ask whether it is the same tree. If it is, the plan is an
    # update and mints nothing at all.
    update: tuple[list[_PriorImport], list[_PriorImport]] | None = None
    incoming: list[_Incoming] = []
    tree_note = ''
    mapped: _Mapped | None = None
    if priors:
        probe = _map_file(indis, fams, {i.xref: _NEW_PERSON + i.xref for i in indis},
                          sour_titles, set(consumed))
        incoming = _incoming_people(indis, probe.stubs)
        lineage, others, tree_note = _same_tree(incoming, priors)
        away = [p.copy_folder_missing for p in lineage if p.copy_folder_missing]
        if away and not is_working_copy(archive_root):
            # Without the filed copy the plan cannot see the statements he
            # deleted, and would offer them all again as new. A drive that is
            # only unplugged is worth plugging in first.
            raise GedcomImportError(
                f'the earlier download of this tree is filed in {away[0]}, and that '
                'folder is not here (usually a documents drive that is not plugged '
                'in). Without it this plan cannot tell the statements you deleted '
                'from new ones. Nothing was written. Plug the drive back in, then '
                'run this again.')
        if lineage:
            update, mapped, consumed = (lineage, others), probe, probe.consumed

    sid = ''
    xref_pid: dict[str, str] = {}
    if mapped is None:
        # Pass 1 (count) → mint in exactly three batches → pass 2 (assign) would
        # mean mapping twice; instead map with a placeholder pool sized by an exact
        # pre-count. Counting claims without mapping is error-prone, so: mint P-ids
        # first (count is just len(indis)), map INDIs/FAMs against a *deferred* C-id
        # pool, then mint the C batch and fill the ids in one zip.
        sid = mint_ids('S', 1, archive_root)[0]
        pid_list = mint_ids('P', len(indis), archive_root)
        xref_pid = {indi.xref: pid for indi, pid in zip(indis, pid_list)}
        mapped = _map_file(indis, fams, xref_pid, sour_titles, consumed)
        if mapped.claim_count:
            real_cids = mint_ids('C', mapped.claim_count, archive_root)
            for claim, cid in zip(mapped.claims, real_cids):
                claim.cid = cid
    stubs, claims = mapped.stubs, mapped.claims
    date_downgrades, date_loose = mapped.date_downgrades, mapped.date_loose
    date_conflicts = mapped.date_conflicts
    warnings.extend(mapped.warnings)

    if date_downgrades:
        shown = '; '.join(date_downgrades[:3])
        # Count the free-form dates that WERE read here too, so this number
        # reads as "truly unreadable", not "everything not in GEDCOM form".
        also_read = (f' {len(date_loose)} other date(s) were typed as free-form '
                     'wording and were read anyway (see the plan).'
                     if date_loose else '')
        warnings.append(
            f'{len(date_downgrades)} date(s) could not be translated to the '
            f'archive\'s date form and were left out of the date field '
            f'({shown}{", ..." if len(date_downgrades) > 3 else ""}). The original '
            'wording is kept in each claim\'s text, so nothing is lost.'
            f'{also_read}')

    cited_sources = sorted(
        ((sour_titles.get(ref, ref.removeprefix('(inline) ')), n)
         for ref, n in mapped.sour_use.items()),
        key=lambda kv: (-kv[1], kv[0]))

    slug = _slugify(ged_path.stem)
    documents_root = resolve_path('documents', fha_config, archive_root)
    if update is None:
        doc_dest = documents_root / _GEDCOM_DOC_SUBDIR / f'{slug}_{sid}.ged'
        record_path = archive_root / 'sources' / 'other' / f'{slug}_{sid}.md'
    else:
        # No source id exists for a plan that writes nothing, so the two
        # destinations name their folders only.
        doc_dest = documents_root / _GEDCOM_DOC_SUBDIR
        record_path = archive_root / 'sources' / 'other'
    doc_alias = path_to_alias(doc_dest, 'documents', fha_config, archive_root)
    title = f'GEDCOM export: {ged_path.name} ({len(indis)} people'
    title += f', exported {export_date_raw})' if export_date_raw else ')'

    plan = ImportPlan(
        archive_root=archive_root, ged_path=ged_path, ged_hash=ged_hash,
        line_count=len(text.splitlines()),
        sid=sid, slug=slug, doc_dest_path=doc_dest, doc_dest_alias=doc_alias,
        record_path=record_path, title=title,
        repository=repository or 'unknown genealogy program',
        source_date=source_date, export_date_raw=export_date_raw,
        stubs=stubs, claims=claims, family_count=len(fams),
        cited_sources=cited_sources,
        duplicates=[],
        warnings=warnings, unread_lines=0, unread_top=[],
        loose_dates=len(date_loose), loose_date_reads=list(date_loose),
        date_downgrades=list(date_downgrades),
        date_conflicts=list(date_conflicts),
        joined_lines=len(joined), markup_cleaned=markup_cleaned,
        tree_note=tree_note, file_people=len(indis),
        unreadable_records=[_rel_display(path, archive_root) for path in unreadable],
    )
    display_names = {s.xref: s.name for s in stubs if s.name}
    if update is None:
        plan.duplicates = _find_duplicates(archive_root, stubs, existing)
    else:
        _plan_update(plan, incoming, update[0], update[1], existing or [])
        # Photos are looked for only for people the plan placed: someone
        # matched for certain (under the record they already have) or someone
        # new. A person held or removed gets nothing planned, photos included.
        xref_pid = {m.xref: m.pid for m in plan.matches.values()
                    if m.person.state == 'present'}
        xref_pid.update({s.xref: s.pid for s in plan.stubs})
    plan.photo_links = _collect_photo_links(records, xref_pid, display_names, consumed)
    plan.photo_catalog = _match_photo_links(archive_root, fha_config, plan.photo_links,
                                            ged_path.parent)
    plan.unread_lines, plan.unread_top = _count_unread(records, consumed)
    return plan


# ── Rendering ─────────────────────────────────────────────────────────────────

def _person_stub_text(stub: _PersonStub, today: str) -> str:
    """Render one person stub (the stubs.py grammar, extended per the plan).

    The provisional `birth:`/`death:` lines are what make an imported tree
    immediately legible - dates show on every stub before any claim is
    reviewed, and the linter's existing needs-a-source tracking is exactly the
    right nudge (SPEC §9, §8.6). No `relationships:` blocks are written: the
    suggested claims are the durable home and the review path (plan decision).

    `aliases:` lists the P-id, the name and each name variant (SPEC §9's
    mirror), so an imported tree is findable by name in Obsidian at once."""
    mirror = person_alias_mirror({'name': stub.name or 'unknown',
                                  'name_variants': list(stub.variants or [])})
    alias_entries = [stub.pid] + [yaml_alias_entry(a) for a in mirror]
    lines = [
        '---',
        f'id: {stub.pid}',
        f'aliases: [{", ".join(alias_entries)}]',
        f'name: {_yaml_inline(stub.name or "unknown")}',
    ]
    if stub.recorded_surname is not None:
        lines.append(surname_frontmatter_line(stub.recorded_surname))
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
    # The same body every other tool gives a new person record (#76): title,
    # purpose block, ## Sources and the hand-written sections - an imported
    # stub reads like any profile, not as bare frontmatter.
    return '\n'.join(lines) + '\n' + render_person_body_scaffold(stub.name or 'unknown')


def _render_claim(claim: _Claim, names: dict[str, str] | None = None) -> list[str]:
    """Render one suggested claim as block-style YAML for the ## Claims fence."""
    lines = [
        f'- value: {_yaml_inline(claim.value)}',
        f'  id: {claim.cid}',
        f'  type: {claim.claim_type}',
    ]
    if claim.subtype:
        lines.append(f'  subtype: {_yaml_inline(claim.subtype)}')
    # persons: one per line, the name beside the ID (SPEC §8.7); roles: bare.
    lines += link_list_lines('persons', claim.persons, names, indent='  ')
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
        '    is_primary: true',
        f'    original_filename: {_yaml_inline(plan.ged_path.name)}',
        f'created: {today}',
        '---',
        '',
        '## Claims',
        '```yaml',
    ]
    names = {normalize_id(st.pid): st.name for st in plan.stubs if st.name}
    for i, claim in enumerate(plan.claims):
        if i:
            lines.append('')
        lines += _render_claim(claim, names)
    lines.append('```')
    lines += [
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
    lines.extend(_photo_notes_lines(plan, _planned_link_writer(plan)))
    lines.append('')
    return '\n'.join(lines)


def _planned_link_writer(plan: ImportPlan) -> LinkWriter:
    """The link writer for the source record, which is rendered before any
    person file exists: in Obsidian mode a `[[stem|Name]]` link has to name
    the file this import is about to write, so the planned stems are given
    rather than scanned. Every person the record links is one of these stubs."""
    links = link_writer(plan.archive_root)
    if not links.obsidian:
        return links
    return LinkWriter(plan.archive_root, links.mode,
                      stems={s.pid: _planned_stub_path(plan, s).stem for s in plan.stubs})


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
    that default is the one privacy-relevant thing this import decides.

    A newer download of a tree already imported has its own plan text
    (`_update_plan_lines`); the notes about the file itself are shared."""
    if plan.mode == 'update':
        return _update_plan_lines(plan, capped=capped) + _file_note_lines(plan, capped)
    # Uncapped means every line: one person can add several date conflicts,
    # and one new person can match several existing ones.
    cap = _PLAN_DETAIL_CAP if capped else sys.maxsize
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
    if plan.tree_note:
        lines.append(f'  {plan.tree_note}')
    lines.append(f'  Person stubs (people/stubs/): {len(plan.stubs):,}')
    for stub in plan.stubs[:cap]:
        lines.append(f'    {_stub_plan_line(stub)}')
    if len(plan.stubs) > cap:
        lines.append(f'    ... and {len(plan.stubs) - cap:,} more '
                     '(use --plan-out FILE to write the full plan)')
    return lines + _duplicate_lines(plan, cap) + _file_note_lines(plan, capped)


def _duplicate_lines(plan: ImportPlan, cap: int) -> list[str]:
    """New people who look like someone already on file (reported, never merged)."""
    lines: list[str] = []
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
    return lines


def _file_note_lines(plan: ImportPlan, capped: bool) -> list[str]:
    """What the plan says about the file itself, whatever kind of plan it is:
    dates typed loosely, lines joined or cleaned, and tags not read."""
    cap = _PLAN_DETAIL_CAP if capped else sys.maxsize
    lines: list[str] = []
    if plan.loose_dates:
        # Three examples on screen; --plan-out (capped=False) lists every one.
        reads = plan.loose_date_reads if not capped else plan.loose_date_reads[:3]
        shown = '; '.join(reads)
        more = ', ...' if plan.loose_dates > len(reads) else ''
        lines.append('')
        lines.append(f'  {plan.loose_dates:,} date(s) were typed as free-form wording '
                     f'rather than GEDCOM\'s date form ({shown}{more}) and were read '
                     'the way `fha claim --date` reads a typed date. The wording '
                     'stays in each claim\'s text, to check when you review it.')
    if plan.date_conflicts:
        # Shown even on screen: the record shows one date and the living
        # guess may hang on it, so he should see that the tree disagrees.
        shown = plan.date_conflicts[:cap]
        lines.append('')
        lines.append(f'  {len(plan.date_conflicts):,} person(s) have birth or death '
                     'dates in your tree that disagree or could not be read. The new '
                     'record shows the most exact one, and every date is still filed '
                     'as a suggested claim, '
                     'to settle when you review them:')
        lines.extend(f'    {c}' for c in shown)
        if len(plan.date_conflicts) > len(shown):
            lines.append(f'    ... and {len(plan.date_conflicts) - len(shown):,} more '
                         '(use --plan-out FILE to write the full plan)')
    if plan.joined_lines:
        lines.append('')
        lines.append(f'  {plan.joined_lines:,} line(s) had no level number (a MyHeritage '
                     'habit) and were joined to the line above, so their words are kept.')
    if plan.markup_cleaned:
        lines.append('')
        lines.append(f'  {plan.markup_cleaned:,} value(s) carried web-page codes (like '
                     '<br>) and were cleaned; the original file is kept as it was.')
    lines.extend(_photo_plan_lines(plan, capped=capped))
    if plan.date_downgrades and not capped:
        # The warning names three; the full plan names every date left out.
        lines.append('')
        lines.append(f'  Dates left out of the date field ({len(plan.date_downgrades):,}; '
                     'the wording is kept in each claim\'s text):')
        lines.extend(f'    {d}' for d in plan.date_downgrades)
    if plan.unread_lines:
        top = ', '.join(f'{tag} x{n}' for tag, n in plan.unread_top)
        lines.append('')
        lines.append(f'  {plan.unread_lines:,} line(s) carried tags this importer does '
                     f'not read ({top}) - the original file is kept, nothing is lost.')
    return lines


def _update_plan_lines(plan: ImportPlan, *, capped: bool) -> list[str]:
    """The plan for a newer download of a tree already imported.

    The counts come first, then what he most needs to look at: what the newer
    file says differently, then who could not be matched and why. Every list
    is cut at `_PLAN_DETAIL_CAP` on screen and whole in `--plan-out`. Nothing
    here names a freshly made id or today's date, so the same archive and the
    same file print the same text every time."""
    cap = _PLAN_DETAIL_CAP if capped else None

    def section(title: str, rows: list[str]) -> list[str]:
        if not rows:
            return []
        out = ['', f'  {title}'] + [f'    {row}' for row in rows[:cap]]
        if cap is not None and len(rows) > cap:
            out.append(f'    ... and {len(rows) - cap:,} more '
                       '(use --plan-out FILE to write the full plan)')
        return out

    def count(outcome: str) -> int:
        return sum(1 for f in plan.facts if f.outcome == outcome)

    def said(fact: _Fact) -> str:
        return _GEDCOM_TAIL_RE.sub('', fact.claim.value)

    def compared(fact: _Fact) -> str:
        return (f'{fact.subject}: your archive has {fact.before}; '
                f'the newer download says {fact.now}')

    earlier = '; '.join(
        f'{p.imported or "an earlier date"} ({p.file_name}, {fmt_id_display(p.sid)})'
        for p in reversed(plan.prior))
    matched = sum(1 for m in plan.matches.values() if m.person.state == 'present')
    people = f'  People: {matched:,} already in your archive, {len(plan.stubs):,} new'
    if plan.held:
        people += f', {len(plan.held):,} not matched for certain (listed below)'
    lines = [
        'GEDCOM import plan (dry-run: nothing is written)',
        f'  File: {plan.ged_path.name} ({plan.line_count:,} lines, '
        f'sha256 {plan.ged_hash[:12]})',
        f'  This is a newer download of the tree you imported on {earlier}.',
        '  Only what changed would be added. Nothing already in your archive is',
        '  changed, including everything you have reviewed or edited.',
        people,
        f'  Statements: {count("same"):,} the same as before (not added again)',
    ]
    if count('decided'):
        lines.append(f'              {count("decided"):,} you already rejected, replaced or '
                     'removed (not added again)')
    lines.append(f'              {count("new"):,} new, {count("more detail"):,} adding detail '
                 '(a place, or a more exact date)')
    lines.append(f'              {count("changed"):,} changed'
                 + (': each would get an open question in notes/questions.md so you '
                    'can decide' if count('changed') else ''))
    if plan.held_statements:
        lines.append(f'              {plan.held_statements:,} left out because they name '
                     'someone not matched for certain, or someone you removed')
    if plan.other_copies:
        lines.append(f'  You also hold this tree as a separate copy '
                     f'({", ".join(plan.other_copies)}). This plan compares with the '
                     'newest import only.')
    for alias in plan.unread_copies:
        lines.append(f'  The filed copy of the earlier download ({alias}) could not be '
                     'read, so this plan compares the newer file with your records only. '
                     'A statement you deleted from your records may be listed as new; '
                     'check the new statements against what you removed.')
    if not (plan.stubs or plan.claims or plan.held):
        lines.append('  Nothing new: this download says the same as what your archive '
                     'already has from this tree. Nothing would be written.')
    else:
        lines += [
            f'  Filed copy:    {plan.doc_dest_alias}/ (a copy of this file; your '
            'original is not touched)',
            f'  Source record: {_rel_display(plan.record_path, plan.archive_root)}/ (one '
            'new record holding only the new and changed statements)',
            '  Adding these is not switched on in this version yet, so --apply will '
            'refuse. This plan is for checking.',
        ]

    changed = [f for f in plan.facts if f.outcome == 'changed']
    lines += section(f'Changed ({len(changed):,}):', [compared(f) for f in changed])
    detail = [f for f in plan.facts if f.outcome == 'more detail']
    lines += section(f'Adding detail ({len(detail):,}):', [compared(f) for f in detail])
    new = [f for f in plan.facts if f.outcome == 'new']
    lines += section(f'New statements ({len(new):,}):', [said(f) for f in new])
    lines += section(
        f'New people ({len(plan.stubs):,}):',
        [f'{s.xref} {s.name or "(no name)"} ('
         + ', '.join(([f'b.{s.birth}'] if s.birth else []) + [f'living: {s.living}']) + ')'
         for s in plan.stubs])
    if plan.held:
        title = (f'Not matched for certain ({len(plan.held):,}). Nothing is added for '
                 f'these people, or for the statements that name them:')
        lines += section(title, [f'{h.xref} {h.label}: {h.reason}'
                                 for h in plan.held.values()])
        if len(plan.held) > matched:
            lines.append('    Most people could not be matched by their record numbers. '
                         'That usually means the program that made this file gave '
                         'everyone a new number.')
    lines += section(f'No longer in the newer file ({len(plan.gone_facts):,}), kept in '
                     'your archive as they are:', plan.gone_facts)
    lines += section(f'People no longer in the newer file ({len(plan.gone_people):,}), '
                     'kept in your archive as they are:', plan.gone_people)
    lines += section(f'People you removed that the earlier import had added '
                     f'({len(plan.removed_people):,}); they are not added back:',
                     plan.removed_people)
    lines += section(f'Names that differ in the newer file ({len(plan.name_changes):,}); '
                     'your records keep their names:', plan.name_changes)
    lines += section(f'The newer file records a death for {len(plan.death_now):,} '
                     'person(s) your archive lists as possibly living:', plan.death_now)
    return lines + _duplicate_lines(plan, cap if cap is not None else len(plan.duplicates))


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
            / _person_filename(stub.given, stub.surname, stub.pid,
                               no_surname=stub.recorded_surname == ''))


def _missing_ancestors(path: Path, root: Path) -> list[Path]:
    """Ancestor directories of `path` that do not exist yet, nearest first.

    Feeds apply's undo stack so a rollback can remove directories it created.
    The walk stops at the archive root OR the filesystem root: `parent !=
    parent.parent` is the dead-drive guard - on an unplugged Windows drive
    nothing on the walk ever exists and the drive root's parent is itself, so
    without that clause the walk never terminates."""
    missing: list[Path] = []
    parent = path.parent
    while parent != root and parent != parent.parent and not parent.exists():
        missing.append(parent)
        parent = parent.parent
    return missing


def _unreachable_anchor(path: Path) -> str | None:
    """The path's drive/anchor when it does not exist on this machine, else None.

    A missing anchor (`Q:\\` with no Q: drive mounted) is the one condition
    mkdir cannot fix and the ancestor walk must not chase - it means the
    resolved documents root points at hardware that is not here right now.
    POSIX anchors (`/`) always exist, so this returns None there."""
    anchor = path.anchor
    if anchor and not Path(anchor).exists():
        return anchor
    return None


def _preflight_apply(plan: ImportPlan) -> None:
    """Refuse the apply before ANY write when it could collide, repeat, or
    target an unreachable documents root.

    The same-hash audit sentinel was already checked at plan time; it is
    re-checked here (cheap) so a plan object can never be applied twice in one
    process either. Destination collisions list the first five - a collision
    usually means a resumed partial run or a hand-placed file, both of which
    deserve eyes, not an overwrite. The reachability check raises
    GedcomEnvironmentError (exit 3, an environment problem) rather than
    GedcomImportError (exit 2, a request problem)."""
    anchor = _unreachable_anchor(plan.doc_dest_path)
    if anchor:
        raise GedcomEnvironmentError(
            f'the documents folder for the filed copy ({plan.doc_dest_path.parent}) '
            f'is not reachable: {anchor} does not exist on this machine (usually '
            'an unplugged or unmounted drive). Nothing was written. Plug the '
            'drive back in, or point the documents entry under roots: in '
            'fha.yaml at a folder this machine can reach, then re-run the import.')

    audit_path = plan.archive_root / _AUDIT_DIR / f'{plan.ged_hash[:12]}.csv'
    if audit_path.exists():
        header = _read_audit_header(audit_path)
        raise GedcomImportError(
            _already_imported(header.get('imported'), header.get('source_id')))

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


def _require_valid_frontmatter(record_text: str) -> None:
    """The render guard: the source record's frontmatter must parse as YAML.

    Every interpolated field is sanitized upstream (_yaml_inline, _oneline),
    so a failure here means a renderer bug slipped through - and the one
    unacceptable outcome is a completed --apply that filed a corrupt record.
    Cheap (one yaml parse of one document) and it refuses BEFORE any write
    (GedcomImportError, exit 2)."""
    m = re.match(r'^---\n(.*?)\n---\n', record_text, re.DOTALL)
    parsed = None
    if m is not None:
        try:
            parsed = safe_load_yaml(m.group(1))
        except yaml.YAMLError:
            parsed = None
    if not isinstance(parsed, dict):
        raise GedcomImportError(
            'the source record this import would write did not come out as a '
            'valid record (a value from the GEDCOM header broke the record '
            'format). Nothing was written. This is a tool bug worth reporting; '
            'as a workaround, simplify the odd header line (the program name '
            'or export date near the top) in a copy of the .ged file and '
            're-run the import.')


def apply_plan(plan: ImportPlan, progress=print) -> list[str]:
    """Write everything, or nothing: every write registers its undo first.

    The source record is rendered and its frontmatter yaml-checked BEFORE the
    first write (_require_valid_frontmatter), so a render bug is a clean
    refusal, never a filed corrupt record. Write order: filed .ged copy →
    person stubs (one progress line per {_PROGRESS_EVERY}) → the source
    record → the audit CSV LAST (so a rolled-back run leaves no re-run
    sentinel). Any exception unwinds every registered undo in reverse; the
    caller translates the failure. Returns the list of written paths for
    Result.changed."""
    root = plan.archive_root
    _preflight_apply(plan)
    today = _today()
    record_text = _render_source_record(plan, today)
    _require_valid_frontmatter(record_text)
    undo: list = []
    written: list[str] = []

    def ensure_parent(path: Path) -> None:
        missing = _missing_ancestors(path, root)
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

        write_new(plan.record_path, record_text)
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


def _write_plan_out(out_path: Path, text: str) -> None:
    """The one --plan-out write seam (tests inject failure here).

    Called BEFORE the apply (so a bad destination is a clean refusal while
    nothing has been written) and again after a successful apply to refresh
    the header from plan-form to applied-form."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding='utf-8')


def run_import(archive_root: Path, fha_config: dict, ged_path: Path, *,
               apply: bool = False, plan_out: str | None = None,
               as_new_tree: bool = False) -> Result:
    """Plan (and optionally apply) a GEDCOM import; return a Result.

    A newer download of a tree already imported is planned as an update: only
    the new people and the statements that changed (`data['mode'] ==
    'update'`, with the counts and plain lines listed where `data` is built).
    That plan can be read but not applied yet: `--apply` refuses it with exit
    2 before anything is written. `as_new_tree` plans every person as new.

    Like convert-mining, the plan is printed inline - the human preview IS this
    intake tool's surface - and the Result carries the structured outcome:
    `data` = {'applied', 'persons', 'families', 'claims', 'cited_sources',
    'duplicates', 'warnings', 'source_id', 'audit_csv'}; `changed` lists every
    written file on apply (stub paths, source record, filed .ged copy, audit
    CSV) and is empty on dry-run.

    Working-copy machines (TOOLING §13d): the dry-run plan still runs (read-
    only, printed under the one-line working-copy banner - the dispatcher
    intercept bypasses fha.py's shared banner, so it is printed here), but
    --apply is in the asset-mutating refusal class: it refuses warning-level
    (ok=True, exit 0, data.status='working-copy') pointing at the main
    archive, exactly like fha process / fha packet.

    --plan-out is written BEFORE the apply: a bad destination refuses the
    whole run while nothing has been written (exit 2, honest) instead of
    failing AFTER a completed apply and mislabeling it a refusal. After a
    successful apply the file is refreshed with the applied header; if that
    refresh fails, the apply is still reported truthfully (changed[] intact,
    exit 1 with a warning).

    Exit arms (the CLI returns Result.exit_code unchanged):
      0 clean plan/apply, and the working-copy --apply refusal · 1 completed
      with warnings · 2 refusal before/without writes (GedcomImportError, or
      a failed pre-apply --plan-out) · 3 environment or write failure - an
      unreachable documents root refuses before any write
      (GedcomEnvironmentError), and a mid-apply failure rolls everything back
      (the catch-all arm: the one thing the user must know is that nothing
      needs cleanup)."""
    if is_working_copy(archive_root):
        if apply:
            wc_msg = (
                'fha gedcom import --apply is not available in working-copy '
                'mode - the filed copy of the .ged belongs with the documents '
                'on the main machine. Run the import there (the dry-run plan '
                'still works here).')
            print(wc_msg, file=sys.stderr)
            # Warning-level refusal, not a failure (TOOLING §13d): it succeeded
            # at the only thing it can do here - declining safely.
            # data.status='working-copy' is the machine discriminator.
            return Result(
                ok=True, exit_code=EXIT_CLEAN,
                data={'status': 'working-copy', 'applied': False},
            ).add('warning', wc_msg)
        print(
            '[working copy] photos and documents live on the main machine - '
            'this plan is a preview; run the import itself there',
            file=sys.stderr)

    try:
        out_path = _check_plan_out(plan_out, archive_root) if plan_out else None
        plan = build_plan(archive_root, fha_config, ged_path, as_new_tree=as_new_tree)
    except GedcomImportError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return Result(ok=False, exit_code=EXIT_ERRORS)

    if apply and plan.unreadable_records:
        # An earlier import the plan could not read is one it could not
        # compare with: applying would add that tree's people a second time.
        print(
            'ERROR: the record of an earlier GEDCOM import could not be read ('
            + ', '.join(plan.unreadable_records) + '), so this import cannot check '
            'whether the file is a newer download of that tree. Nothing was written. '
            'Run fha lint to see what is wrong with the record and fix it, then run '
            'this again. (To import the file as a separate tree anyway, add '
            '--as-new-tree.)', file=sys.stderr)
        return Result(ok=False, exit_code=EXIT_ERRORS)

    if apply and plan.mode == 'update':
        # The plan is safe to read; the write path for it is not built yet,
        # and a half-built one must never touch his records.
        earlier = plan.prior[0]
        print(
            'ERROR: this file is a newer download of a tree you already imported (on '
            f'{earlier.imported or "an earlier date"}, {fmt_id_display(earlier.sid)}). '
            'Adding only what changed is not switched on in this version yet. Nothing '
            'was written. Run the same command without --apply to see what it would '
            'add, and keep the file: a later version of the tools adds it. (To import '
            'it as a separate tree anyway, with every person a second time, add '
            '--as-new-tree.)', file=sys.stderr)
        return Result(ok=False, exit_code=EXIT_ERRORS)

    def _plan_out_text(as_applied: bool) -> str:
        return '\n'.join(_plan_lines(plan, applied=as_applied, capped=False)
                         + [f'WARNING: {w}' for w in plan.warnings]) + '\n'

    if out_path is not None:
        try:
            _write_plan_out(out_path, _plan_out_text(False))
        except OSError as e:
            print(
                f'ERROR: could not write --plan-out {out_path}: {e}. Nothing '
                'was imported - fix the destination (or drop --plan-out) and '
                're-run.', file=sys.stderr)
            return Result(ok=False, exit_code=EXIT_ERRORS)

    changed: list[str] = []
    applied = False
    plan_out_warning: str | None = None
    if apply:
        try:
            changed = apply_plan(plan)
            applied = True
        except GedcomImportError as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return Result(ok=False, exit_code=EXIT_ERRORS)
        except GedcomEnvironmentError as e:
            print(f'ERROR: {e}', file=sys.stderr)
            return Result(ok=False, exit_code=EXIT_FAILURE)
        except Exception as e:  # noqa: BLE001 - the rolled-back catch-all arm
            print(
                f'ERROR: import failed and every write was rolled back: {e}. '
                'Nothing needs cleanup - fix the cause and re-run (the re-run '
                'guard will not trip; the audit file is written last).',
                file=sys.stderr)
            return Result(ok=False, exit_code=EXIT_FAILURE)

    print_plan(plan, applied=applied)

    if out_path is not None:
        if applied:
            try:
                _write_plan_out(out_path, _plan_out_text(True))
            except OSError as e:
                # The apply itself succeeded; a failed refresh of the already-
                # written plan file must not be reported as a refusal (that
                # would discard changed[] and lie about the writes). Warn.
                plan_out_warning = (
                    f'the import completed, but the --plan-out file could not '
                    f'be refreshed with the applied plan: {e}. Every record '
                    'was written; re-run without --apply to regenerate the '
                    'plan text if you need it.')
                print(f'WARNING: {plan_out_warning}', file=sys.stderr)
        if plan_out_warning is None:
            print(f'Full plan written: {out_path}')

    if applied:
        first = next((s.name for s in plan.stubs if s.name), 'your closest ancestor')
        print()
        print(_CLOSING_POSTURE.format(claims=len(plan.claims), first_person=first))
        handoff = _photo_handoff_text(plan)
        if handoff:
            print()
            print(handoff)

    exit_code = EXIT_WARNINGS if (plan.warnings or plan_out_warning) else EXIT_CLEAN
    result = Result(
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
            'mode': plan.mode,
        })
    if plan.mode == 'update':
        # In an update `persons` and `claims` above count only what would be
        # added, and `source_id` is '' (no id is minted for a plan).
        def count(outcome: str) -> int:
            return sum(1 for f in plan.facts if f.outcome == outcome)

        result.data.update({
            'update_of': [fmt_id_display(p.sid) for p in plan.prior],
            'matched': sum(1 for m in plan.matches.values()
                           if m.person.state == 'present'),
            'new_people': len(plan.stubs),
            'unmatched': [f'{h.xref} {h.label}: {h.reason}' for h in plan.held.values()],
            'unmatched_likely': sum(1 for h in plan.held.values() if h.likely is not None),
            'same': count('same'),
            'decided': count('decided'),
            'new': count('new'),
            'more_detail': count('more detail'),
            'changed': count('changed'),
            'questions': len(plan.questions),
            'held_statements': plan.held_statements,
            'gone_facts': list(plan.gone_facts),
            'gone_people': list(plan.gone_people),
            'removed_people': list(plan.removed_people),
            'name_changes': list(plan.name_changes),
            'death_now': list(plan.death_now),
        })
    if plan_out_warning:
        result.add('warning', plan_out_warning)
    return result


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
        as_new_tree=bool(getattr(args, 'as_new_tree', False)),
    ).exit_code


# User-facing --help (the persona-B help pattern: what it does for you, in
# plain words, with copyable examples - the module docstring above is for
# tool-builders). No --spec-root: this command reads nothing from a spec root
# (persona-E posture: no decorative flags).
_CLI_DESCRIPTION = """Bring a tree from Ancestry (or any genealogy program) into your archive.

  fha gedcom import family-tree.ged            Show the plan; writes nothing
  fha gedcom import family-tree.ged --apply    Do the import

Download your tree as a GEDCOM (.ged) file, then run this. The file itself is
filed as one source (your original untouched); every person becomes a record
with their dates; every statement becomes a suggested fact for you to review
later, family by family. Nothing imported is treated as proven. Photos your
old program linked are looked for in your photo folders. Re-importing
the same file is refused, and a failed import cleans up after itself.

Downloaded your tree again later? Run this on the newer file. The plan shows
only what changed since the earlier download: the new people, the new
statements, and each one the newer file says differently. People it cannot
match for certain are listed with the reason. (This plan cannot be applied
yet; it is for checking.)

  --plan-out FILE    Also write the full, uncut plan text to FILE.
  --as-new-tree      Treat a newer download as a separate tree instead."""


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('ged_file', metavar='FILE.ged',
                   help='The GEDCOM file to import (e.g. an Ancestry tree download).')
    p.add_argument('--apply', action='store_true',
                   help='Write the records (default: print the dry-run plan only).')
    p.add_argument('--plan-out', metavar='FILE', dest='plan_out',
                   help='Also write the full (uncapped) plan text to FILE; refused '
                        'inside the archive except its out/ folder.')
    p.add_argument('--as-new-tree', action='store_true', dest='as_new_tree',
                   help='Import every person as new even if this looks like a newer '
                        'download of a tree already imported. Every person arrives a '
                        'second time, which is usually not what you want.')
    p.add_argument('--root', metavar='PATH',
                   help='Archive root (auto-detected if omitted).')


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha gedcom import',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(parser)
    parser.set_defaults(func=_cmd_import)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
