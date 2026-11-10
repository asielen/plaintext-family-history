#!/usr/bin/env python3
"""
xref.py - fha xref: cross-reference pass over the claim index.

  fha xref [--root PATH]

Read-only candidate-suggestion tool (TOOLING §14a). Does not write to the
archive - it only prints candidate pairs for a human (or a future skill
layer) to confirm. Confirmation, link-writing, and question-spawning are out
of scope for this tool.

ALGORITHM
---------
For every person, group their accepted/needs-review claims by claim `type`
(relationship claims are further split by `subtype` - the *nature* of the bond,
e.g. biological vs. adoptive - this person's `role`, and the other person(s)
named in the claim, since a person can be e.g. a child in one relationship claim
and a parent in another, and a biological and an adoptive parent of the same
child are co-valid edges rather than a contradiction). Within each group,
every pair of claims from *different* sources that isn't already linked via
`claim_links` is a candidate:

  - negation polarity differs (`negated`)          -> contradiction candidate,
                                                        when bounds overlap and
                                                        (non-vital types) the two
                                                        claims don't name
                                                        different places; never
                                                        for `event`/`note`
  - bounds don't overlap, vital type                -> contradiction candidate
  - bounds don't overlap, substantive type           -> not a candidate
    (residence, occupation, ... recur by design, §8.2; non-overlapping dates
    are expected, not a conflict)
  - bounds overlap, vital/relationship/divorce type  -> corroboration candidate
  - vital type AND bounds overlap AND both claims     -> also a contradiction
    carry a `place_id`/`place_text` that disagree        candidate (incompatible
                                                          value), even though the
                                                          dates don't conflict
  - bounds overlap, any other type                   -> corroboration candidate
    ONLY when something specific agrees (#245): see
    "WHAT COUNTS AS AGREEMENT" below. `event`/`note`
    never get one.

WHAT COUNTS AS AGREEMENT (#245)
-------------------------------
For a vital type, a relationship, or a divorce, the bucket a claim lands in
already says WHICH fact it is (this person's one birth; this bond with this
counterpart; this couple's divorce), so overlapping dates are enough to call
two claims a match. Every other type recurs: a person has many residences,
occupations, censuses, events. "Same person, same type, dates overlap" said
nothing there - and an undated claim gets unbounded bounds, so it overlapped
everything. One undated interview anecdote was proposed as corroborating
every other `event` on file for that man, and a bulk confirm wrote the links.

So for those types a pair is a candidate only when something specific
agrees (`_recurring_agreement`):

  - `event` / `note`: never - neither as corroborating nor, through a
    negation, as contradicting. The type says nothing about what the fact
    is; matching them up is a reading job for a person or an agent.
  - both claims carry a registered `place_id` and the ids differ: no - a
    registered place is a decision someone made. It also vetoes a town
    against its own county (two ids); that is an owner-reversible choice
    (#247 review), one line to flip in `_places_differ`.
  - both claims carry the same `place_id`, or the same normalized
    `place_text`: agrees, whatever the dates.
  - otherwise, two `place_text`s are compared by `_compare_place_texts`.
    SPEC §8 and §15 keep `place_text` as the source wrote it, so one town
    arrives as "Riverton, Ohio" and "Riverton, Hamilton Co., Ohio" - an exact
    comparison hid real pairs (two copies of one 1880 census entry), while no
    comparison at all offered "Riverton" 1880 against "Oakdale" 1880. A
    verdict of 'differ' (the towns plainly disagree: "Riverton" vs "Oakdale",
    "Riverton" vs "Riverside") vetoes. 'same' (a wording variant) or
    'no-signal' ("Ohio" vs "Riverton, Ohio": one side only names the larger
    area; "Suwałki" vs "Suwalki", "Riverton, OH", "St. Louis" vs "Saint
    Louis", "Rivertown": possibly one town spelled two ways) never admits a
    pair by itself - it falls through to the date test. The veto is kept
    gentle on purpose: wrongly calling one town two places hides a real
    match, while two same-name towns in different states ("Springfield,
    Ohio" / "Springfield, Illinois") only fall through to the date test.
  - both claims dated and the dates agree: agrees. Dates agree when both
    claims carry the IDENTICAL finite range (`1861/1865` twice - a war
    service, a long career - however wide), or when each date is no wider
    than `_CLOSE_DATE_MAX_YEARS` calendar years and they overlap. Three years
    admits "about 1880" (`1880~`, which edtf_bounds widens to 1879-1881) and
    a short range like `1905/1907`; it rules out an undated claim, an
    open-ended one (`[1906..]`, "from 1906 on"), and a decade or long span
    that is not the same span on both sides.
  - anything else: no.

The negated-vs-positive branch does NOT require agreement: a researched
absence is usually written undated and placeless ("did not serve; no roster
or pension record", "never left the old country"), and it is meant to be
weighed against every positive claim of that type. For a recurring type it is
a contradiction candidate unless the two claims name different places, read
through the same `_places_differ` test ("did not live in Topeka" and "lived in
Boston" can both be true; "never lived in Riverton" against "Riverton,
Hamilton Co., Ohio" is a real conflict). `event` and `note` never produce
one - "never went to sea" says nothing about a mortgage. Relationship and
divorce keep the original exact place-text comparison here; vital types are
unchanged.

Place comparison prefers structured `place_id` when both claims have one;
it falls back to normalized `place_text`, then to a place phrase parsed out
of free-prose `value`, since `value` itself is not reliably comparable
across claims. A claim with no `date_edtf` gets the unbounded
`('0001-01-01', '9999-12-31')` bounds from `edtf_bounds`, so an undated claim
always overlaps rather than being treated as conflicting.

MARRIAGE/DIVORCE BUCKETING (#63)
---------------------------------
`persons:` on a marriage/divorce claim is who the claim is ABOUT, not a
couple list (SPEC §8.3) - a certificate routinely names both sets of
parents alongside the couple. Bucketing by "every other named person"
(as this file once did) keys a six-person certificate by all five
bystanders, so it never matches a plain two-person claim of the *same*
marriage keyed by just the one spouse - two records of one marriage that
never get compared, and the tool's silence reads as "no contradictions"
rather than "couldn't tell". `_lib.spouse_parties` is the shared, already-
correct rule for "who does this claim say married whom" (also used by
`fha index`'s spouse edges and `fha gedcom`'s FAM grouping); every
marriage/divorce claim is read through it once, up front, before any
per-person bucketing:

  - `negated: true` (SPEC §8.6, a real "researched, did not happen") is
    always compared against every marriage/divorce claim this person has
    - checked on the claim's own `negated` field, never inferred from an
    empty party set - narrowed further to one counterpart's claims when
    `spouse_parties` did resolve a specific ex-partner.
  - A claim naming fewer than two people at all has no counterpart to be
    ambiguous ABOUT, negated or not - unchanged from before #63, it takes
    the same broad every-claim-of-this-type path a negation does.
  - A resolved couple (`spouse_parties` non-empty) buckets narrowly by that
    couple. `spouse_parties`'s serial case (`roles: spouse:` naming 3+ people,
    one claim recording successive marriages) buckets once per individual
    partner rather than once per whole remaining party set, the same way the
    `relationship` branch below already bundles several counterparts - so a
    claim naming {Jane, Mary} as this person's spouses still lands in the
    same bucket as a plain claim naming just {Mary}, instead of the two only
    matching a claim naming the exact same pair.
  - An unresolved, non-negated claim naming 2+ people (a roles-less
    certificate, or a `roles:` map that names fewer than two spouses) is
    AMBIGUOUS: comparing it against everything fabricates contradictions
    against unrelated marriages (a groom's own certificate "contradicting"
    his parents' wedding); comparing it against nothing is the original
    bug. It is excluded from every comparison bucket and listed instead in
    the Result's `unscoped` list, so the human can see it and add a
    `roles: spouse:` map to enable comparison.
  - A person named on the claim but not among its resolved parties (a
    witness, an informant, a parent on a certificate) never enters that
    claim into their own marriage/divorce bucket at all - the claim asserts
    nothing about their marital status.

OTHER VITAL-TYPE BUCKETING (#136's second deferred fix)
---------------------------------------------------------
The other vital types (`_VITAL_TYPES` minus marriage/divorce - birth, death,
baptism, burial) had the identical "everyone named shares a bucket" bug: a
mother's own birth claim and her son's birth claim - naming her under
`roles: {parent: [...]}` - landed in the same `('birth',)` bucket for her,
reading non-overlapping dates as a fabricated contradiction between two
different people's births. Fixed through `_lib.vital_subjects`: a claim is
bucketed under a named person only when they're among its resolved subjects
(the type's subject role - `child` for birth/baptism - when the claim names
one, otherwise whoever `roles:` left unroled, the ordinary shape for a death
record). A legacy claim naming AT MOST ONE person with no `roles:` map at
all (`vital_subjects` returns `None`) keeps the old broad behavior
unchanged - there is nobody else to be ambiguous about. A claim naming
TWO OR MORE people with no `roles:` map at all has not said which of them
the claim is about; `vital_subjects` returns `[]` for that shape (#126,
reopened) and the claim enters neither person's bucket - reported instead
in the Result's `unscoped` list (#172), the same treatment the ambiguous
marriage/divorce case above already gets, so the human sees it rather than
watching it vanish from every bucket with no trace. This applies EQUALLY
when that zero-role, 2+-person claim is `negated: true` (#173 follow-up,
second round). An earlier version of this fix treated a negated claim of
this shape as automatically "about everyone named" (modeled on the
marriage/divorce negation bullet above) - but that model does not transfer:
a marriage genuinely is a relationship BETWEEN the two people it names, so
treating a counterpart-less marriage claim as being about both is correct,
while a vital event (birth, death, baptism, burial) happens to exactly ONE
person. Negation flips the assertion's polarity, not which named person the
claim is about - a negated certificate naming the real subject alongside an
incidental bystander ("it wasn't A who died - B just happened to be there
too") is exactly as ambiguous as the positive version of the same claim, and
guessing "definitely about both" let a bystander's own unrelated, accepted
death claim read as "contradicting" a negation that was never about them.
So a negated case-2a claim is excluded into `unscoped` exactly like its
positive counterpart, until `roles: deceased:`/`child:` says who it is
actually about.

This scoping is deliberately NOT applied to a substantive type (`census`,
`residence`, `occupation`, ...): those claims legitimately role every
person on the record (`head`/`household_member`, ...), so `vital_subjects`
would find nobody unroled and wrongly empty every such claim's bucket
archive-wide; substantive types keep their original unscoped same-type
bucketing.

CODE MAP
--------
  DB / root helpers - open_index_db, resolve_root_arg, both shared via _lib.py

  Classification
    _place_from_vital_value     - vital-claim place extraction (uses _lib.normalize_place_text)
    _classify_pair              - corroborates/contradicts for one claim pair
    _recurring_agreement        - "same place or agreeing dates" test for recurring types (#245)
    _places_differ / _compare_place_texts
                                - "do these claims name two different places?" (ids, else
                                  as-written text compared town-first, token-subset)
    run_xref                    - group claims by person+type, pair, classify
                                   (marriage/divorce grouping uses _lib.spouse_parties,
                                   see "MARRIAGE/DIVORCE BUCKETING" above)

  CLI
    _fmt_claim                  - display formatting (uses _lib.fmt_id_display)
    _cmd_xref, register, _standalone_main
"""

from __future__ import annotations

import argparse
import difflib
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    configure_utf8_stdout,
    EXIT_CLEAN,
    EXIT_FAILURE,
    Result,
    edtf_bounds,
    fmt_id_display,
    normalize_place_text,
    place_text_cluster_key,
    open_index_db,
    resolve_root_arg,
    spouse_parties,
    vital_subjects,
)

configure_utf8_stdout()

_VITAL_TYPES = {'birth', 'death', 'marriage', 'baptism', 'burial'}

# Types whose comparison bucket already pins down which single fact a claim is
# about, so overlapping dates are enough for a corroboration candidate. See
# the module docstring's "WHAT COUNTS AS AGREEMENT" section (#245).
_PINNED_TYPES = _VITAL_TYPES | {'divorce', 'relationship'}

# Incidental types (SPEC §8.2): the type says nothing about what the fact is,
# so two of them are never proposed as corroborating each other (#245).
_NO_CORROBORATION_TYPES = {'event', 'note'}

# How wide (in calendar years) each claim's date may be for a date match to
# count as agreement on a recurring type (#245). Three admits `1880~`.
_CLOSE_DATE_MAX_YEARS = 3

_REQUIRED_TABLES = ('persons', 'claims', 'sources', 'claim_persons', 'claim_links')


# ── Core query ────────────────────────────────────────────────────────────────


def _place_from_vital_value(text: str | None) -> str:
    """
    Extract a conservative place phrase from a vital claim value.

    Vital `value` is free prose, so comparing whole strings would turn harmless
    wording differences into contradictions. The stable conflict signal is a
    place-like phrase introduced by common vital wording ("born in ...",
    "birthplace: ...", etc.); if no such phrase is present, the value is not
    used for contradiction classification.
    """
    if not text:
        return ''
    # The place capture stops before a trailing date/preposition clause
    # ("born in Springfield in 1840" -> "Springfield", not "Springfield in
    # 1840") as well as at sentence punctuation, since the date belongs to
    # the structured `date_edtf` field, not the place comparison.
    patterns = (
        r'\b(?:born|died|married|buried|baptized|baptised)\s+(?:in|at)\s+'
        r'([^.;\n]+?)(?:\s+(?:in|on|circa|c\.)\s+\d|[.;\n]|$)',
        r'\b(?:birthplace|deathplace|marriage place|burial place|baptism place|place)\s*:\s*'
        r'([^.;\n]+?)(?:\s+(?:in|on|circa|c\.)\s+\d|[.;\n]|$)',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return normalize_place_text(match.group(1))
    return ''


def _classify_pair(a: dict, b: dict) -> str | None:
    """
    Return 'corroborates', 'contradicts', or None (not a comparable pair) for a
    same-person, same-type pair. Overlapping dates are enough to corroborate
    only for `_PINNED_TYPES`; every other type goes through
    `_recurring_agreement` (#245).
    """
    a_min, a_max = edtf_bounds(a['date_edtf'])
    b_min, b_max = edtf_bounds(b['date_edtf'])
    bounds_overlap = a_min <= b_max and b_min <= a_max

    if not bounds_overlap:
        if a['type'] in _VITAL_TYPES:
            return 'contradicts'
        # Substantive types (residence, occupation, ...) are recurring by
        # design (§8.2) - non-overlapping dates are expected, not a conflict.
        return None

    if bool(a['negated']) != bool(b['negated']):
        # One claim asserts the fact happened, the other confirms it never
        # did, for the same place in time - that's a genuine conflict. (Vital
        # types always reach here: an undated negated claim gets unbounded
        # bounds, so it overlaps any dated positive claim of the same type.)
        # For repeatable substantive types (residence, occupation, ...) the
        # absence and the presence have to be about the *same* place - a
        # negated "did not reside in Topeka" doesn't conflict with a positive
        # "resided in Boston" the same year, since both can be true at once.
        if a['type'] in _PINNED_TYPES - _VITAL_TYPES:
            # relationship / divorce: unchanged.
            if a['place_id'] and b['place_id']:
                if a['place_id'] != b['place_id']:
                    return None
            else:
                place_a = normalize_place_text(a['place_text'])
                place_b = normalize_place_text(b['place_text'])
                if place_a and place_b and place_a != place_b:
                    return None
        elif a['type'] not in _VITAL_TYPES:
            # An undated "never went to sea" is not in conflict with every
            # other event on file (#245's contradiction-side twin).
            if a['type'] in _NO_CORROBORATION_TYPES:
                return None
            # Otherwise a researched absence is weighed against every
            # positive claim of the type - unless they name different places.
            if _places_differ(a, b):
                return None
        return 'contradicts'

    if a['type'] in _VITAL_TYPES:
        if a['place_id'] and b['place_id']:
            if a['place_id'] != b['place_id']:
                return 'contradicts'
        else:
            place_a = normalize_place_text(a['place_text']) or _place_from_vital_value(a['value'])
            place_b = normalize_place_text(b['place_text']) or _place_from_vital_value(b['value'])
            if place_a and place_b and place_a != place_b:
                return 'contradicts'
        return 'corroborates'

    if a['type'] in _PINNED_TYPES:
        return 'corroborates'

    if _recurring_agreement(a, b, a_min, a_max, b_min, b_max):
        return 'corroborates'
    return None


def _year_span(lo: str, hi: str) -> int:
    """Calendar years an edtf_bounds pair covers (1880-01-01..1880-12-31 -> 1)."""
    return int(hi[:4]) - int(lo[:4]) + 1


_OPEN_MIN, _OPEN_MAX = edtf_bounds(None)

# Words that say what kind of place a token names, not which place: dropped
# before comparing two place texts ("Riverton City" is "Riverton").
_PLACE_FILLER_WORDS = frozenset({
    'city', 'county', 'township', 'twp', 'usa', 'us', 'united', 'states', 'of',
    'america',
})


def _place_part_tokens(part: str) -> set[str]:
    return set(place_text_cluster_key(part).split()) - _PLACE_FILLER_WORDS


# Letters NFKD does not split into base + accent.
_PLACE_FOLD = str.maketrans({
    'ł': 'l', 'Ł': 'L', 'ø': 'o', 'Ø': 'O', 'æ': 'ae', 'Æ': 'AE', 'ß': 'ss',
    'đ': 'd', 'Đ': 'D',
})
_TOWN_LEADING_ABBREVIATIONS = {'st': 'saint', 'mt': 'mount', 'ft': 'fort'}
# difflib ratio at or above which two town names read as one name spelled
# two ways ("Riverton" / "Rivertown" / "Riverten"), while "Riverton" /
# "Riverside", "Fairview" / "Fairfield" and "Newton" / "Newport" stay apart.
_TOWN_VARIANT_RATIO = 0.8


def _town_key(text: str | None) -> str:
    """The town part of an as-written place text, folded for spelling
    variants: first comma part, cut before any "(", accents folded
    (Suwałki -> suwalki, München -> munchen, and "ue" -> "u" for Muenchen),
    a leading St/Mt/Ft spelled out, filler words dropped."""
    first = (text or '').split(',')[0].split('(')[0]
    folded = unicodedata.normalize('NFKD', first.translate(_PLACE_FOLD))
    folded = ''.join(c for c in folded if not unicodedata.combining(c)).lower()
    folded = folded.replace('ue', 'u')
    words = [w for w in re.sub(r'[^\w\s]', ' ', folded).split()
             if w not in _PLACE_FILLER_WORDS]
    if words:
        words[0] = _TOWN_LEADING_ABBREVIATIONS.get(words[0], words[0])
    return ' '.join(words)


def _towns_are_variants(text_a: str | None, text_b: str | None) -> bool:
    """Whether two place texts' towns could be one town written two ways:
    equal once folded, one a whole word of the other ("farm outside
    Riverton" / "Riverton"), or close spellings by difflib ratio."""
    town_a, town_b = sorted((_town_key(text_a), _town_key(text_b)))
    if not town_a or not town_b:
        return False
    if town_a == town_b or town_a in town_b.split() or town_b in town_a.split():
        return True
    return difflib.SequenceMatcher(None, town_a, town_b).ratio() >= _TOWN_VARIANT_RATIO


def _compare_place_texts(text_a: str | None, text_b: str | None) -> str:
    """
    Compare two as-written place texts: 'same', 'differ', or 'no-signal'.

    Each text is split on commas into parts (town first, then the larger
    areas), and each part into tokens through `_lib.place_text_cluster_key`
    ("Co." -> "County", punctuation dropped) minus `_PLACE_FILLER_WORDS`.

      - the towns (first parts) agree, or one side is the comma-less form of
        the other ("Riverton Ohio" / "Riverton, Ohio"), AND the shorter
        text's tokens all appear in the longer's -> 'same':
          "Riverton, Ohio" / "Riverton, Hamilton Co., Ohio"
          "Fairview City, Breton, Kansas" / "Fairview, Breton County, Kansas"
          "Fairview" / "Fairview, Breton County, Kansas"
      - one side names only a larger area the other lies in -> 'no-signal':
          "Ohio" / "Riverton, Ohio"; "Hamilton, Ohio" / "Riverton, Hamilton
          Co., Ohio" (a town called Hamilton, or the county? can't tell)
      - the towns agree but the rest of the words don't line up -> never
        'differ', only 'no-signal' ("Riverton, Ohio" / "Riverton, OH";
        "Springfield, Ohio" / "Springfield, Illinois" - same-name towns in
        two states fall through to the date test)
      - the towns disagree, but `_towns_are_variants` says they may be one
        town written two ways -> 'no-signal' (Suwałki / Suwalki; Muenchen /
        München; St. Louis / Saint Louis; Riverton / Rivertown; "farm
        outside Riverton" / "Riverton, Ohio"; "Riverton (Hamilton County),
        Ohio"; also York / New York, accepted)
      - anything else -> 'differ':
          "Riverton" / "Oakdale"; "Riverton, Ohio" / "Oakdale, Ohio";
          "Riverton" / "Riverside"; "Fairview" / "Fairfield"
      - either side empty -> 'no-signal'.

    Callers treat only 'differ' as meaningful (a veto); 'same' and
    'no-signal' never admit a pair on their own (#245).
    """
    parts_a = [t for t in (_place_part_tokens(x) for x in (text_a or '').split(',')) if t]
    parts_b = [t for t in (_place_part_tokens(x) for x in (text_b or '').split(',')) if t]
    if not parts_a or not parts_b:
        return 'no-signal'
    all_a, all_b = set().union(*parts_a), set().union(*parts_b)
    small, big = (all_a, all_b) if len(all_a) <= len(all_b) else (all_b, all_a)
    if parts_a[0] == parts_b[0]:
        # The towns agree: never a veto, whatever the rest says.
        return 'same' if small <= big else 'no-signal'
    rest_a, rest_b = set().union(*parts_a[1:]), set().union(*parts_b[1:])
    if all_a <= rest_b or all_b <= rest_a:
        return 'no-signal'
    commaless = (
        (len(parts_a) == 1 and parts_a[0] <= all_b and parts_b[0] <= parts_a[0])
        or (len(parts_b) == 1 and parts_b[0] <= all_a and parts_a[0] <= parts_b[0])
    )
    if commaless and small <= big:
        return 'same'
    return 'no-signal' if _towns_are_variants(text_a, text_b) else 'differ'


def _places_differ(a: dict, b: dict) -> bool:
    """Whether two claims name two different places: different registered
    `place_id`s (a town and its own county included - owner-reversible, #247
    review), or, short of two ids, two `place_text`s `_compare_place_texts`
    calls 'differ'."""
    if a['place_id'] and b['place_id']:
        return a['place_id'] != b['place_id']
    if normalize_place_text(a['place_text']) == normalize_place_text(b['place_text']):
        return False
    return _compare_place_texts(a['place_text'], b['place_text']) == 'differ'


def _recurring_agreement(a: dict, b: dict, a_min: str, a_max: str,
                         b_min: str, b_max: str) -> bool:
    """
    Whether two claims of a recurring type (residence, occupation, census,
    ...) whose dates already overlap say something specific in common: the
    same place, or dates that agree. Gates corroboration only. See the module
    docstring's "WHAT COUNTS AS AGREEMENT" section (#245).
    """
    if a['type'] in _NO_CORROBORATION_TYPES:
        return False

    if a['place_id'] and b['place_id']:
        # Two registered places: the ids decide. Different ids (a town vs
        # its county included) veto.
        return a['place_id'] == b['place_id']
    text_a = normalize_place_text(a['place_text'])
    text_b = normalize_place_text(b['place_text'])
    if text_a and text_a == text_b:
        return True
    if _places_differ(a, b):
        return False
    # A wording variant of one place, or no signal either way, proves
    # nothing on its own (SPEC §8, §15: place_text is as the source wrote
    # it) - the dates decide.

    if not (a['date_edtf'] and b['date_edtf']):
        return False
    if (a_min, a_max) == (b_min, b_max) and a_min != _OPEN_MIN and a_max != _OPEN_MAX:
        # The identical finite range on both sides (1861/1865 twice), however
        # wide - not an open-ended one.
        return True
    return (_year_span(a_min, a_max) <= _CLOSE_DATE_MAX_YEARS
            and _year_span(b_min, b_max) <= _CLOSE_DATE_MAX_YEARS)


def run_xref(archive_root: Path) -> Result:
    """
    Find corroboration/contradiction candidate claim pairs.

    Returns a `Result` whose `data` is {'status': 'ok'|'failed', 'groups':
    [{'person_id', 'person_name', 'pairs': [{'kind', 'claim_a', 'claim_b'}, …]},
    …], 'unscoped': [{'claim', 'persons'}, …]}.  Result exposes dict-style
    access (_lib.py), so callers and tests keep reading `result['status']` /
    `result['groups']` unchanged.

    Each claim dict embedded in a pair (or in `unscoped`) carries: id,
    source_id, source_title, type, date_edtf, place_text, value.

    `unscoped` is the secondary "here's what I couldn't do" list (matching
    `run_index`'s `unreadable_records`/warnings shape) - claims left out of
    every comparison bucket in `groups` rather than risking a fabricated
    contradiction, reported here so a human can see them instead of the
    claim just vanishing. Two shapes populate it: marriage/divorce claims
    naming 2+ people where `_lib.spouse_parties` could not tell the couple
    from everyone else named on the claim (#63; see the module docstring's
    "MARRIAGE/DIVORCE BUCKETING" section), and death/burial/baptism claims
    naming 2+ people with no `roles:` map at all - `_lib.vital_subjects`'s
    case 2a, the same zero-role-signal shape `fha lint`'s W132 reports (#172;
    see "OTHER VITAL-TYPE BUCKETING"). Each entry's `persons` is the display
    names of everyone the claim names, in claim order; `claim['type']` tells
    the two shapes apart for anyone reading the list programmatically.
    """
    conn = open_index_db(archive_root, _REQUIRED_TABLES)
    if conn is None:
        return Result(ok=False, exit_code=EXIT_FAILURE,
                      data={'status': 'failed', 'groups': [], 'unscoped': []})

    try:
        data = _run_xref_queries(conn)
        return Result(exit_code=EXIT_CLEAN, data=data)
    except sqlite3.OperationalError:
        print(
            'ERROR: .cache/index.sqlite is unreadable or has an incompatible schema. '
            'Run `fha index` to rebuild.',
            file=sys.stderr,
        )
        return Result(ok=False, exit_code=EXIT_FAILURE,
                      data={'status': 'failed', 'groups': [], 'unscoped': []})
    finally:
        conn.close()


def _run_xref_queries(conn: sqlite3.Connection) -> dict:
    claims_by_id = {
        row['id']: dict(row)
        for row in conn.execute(
            '''
            SELECT id, source_id, type, subtype, date_edtf, place_id, place_text,
                   value, negated
            FROM claims
            WHERE status IN ('accepted', 'needs-review')
            '''
        )
    }
    source_titles = {
        row['id']: row['title'] for row in conn.execute('SELECT id, title FROM sources')
    }
    for claim in claims_by_id.values():
        claim['source_title'] = source_titles.get(claim['source_id'], claim['source_id'])

    claims_by_person: dict[str, list[str]] = {}
    claim_persons: dict[str, list[str]] = {}
    claim_role: dict[tuple[str, str], str] = {}
    # ORDER BY position: _lib.spouse_parties reads "the claim's people paired
    # with their role, in the claim's own order" - it only matters for its
    # first-occurrence-wins duplicate handling, but that is the contract the
    # rest of the codebase (fha index, fha gedcom) already relies on, so xref
    # reads claim_persons the same way rather than trusting sqlite's
    # unspecified default row order.
    for row in conn.execute(
        'SELECT claim_id, person_id, role FROM claim_persons ORDER BY position'
    ):
        if row['claim_id'] not in claims_by_id:
            continue
        claims_by_person.setdefault(row['person_id'], []).append(row['claim_id'])
        claim_persons.setdefault(row['claim_id'], []).append(row['person_id'])
        # First occurrence's role wins, same as claim_persons/claims_by_person
        # just above and the same contract vital_subjects/spouse_parties
        # document for their own input - `persons: [P-a, "[[Alice Smith]]"]`
        # is one person written twice, and a plain assignment here would let
        # the LAST row silently overwrite an earlier, correct role (e.g. the
        # birth subject's `child` role) with a duplicate row's blank or
        # different one.
        claim_role.setdefault((row['claim_id'], row['person_id']), row['role'])

    linked_pairs: set[frozenset[str]] = set()
    for row in conn.execute('SELECT claim_id, target_id FROM claim_links'):
        linked_pairs.add(frozenset((row['claim_id'], row['target_id'])))

    person_names = {row['id']: row['name'] for row in conn.execute('SELECT id, name FROM persons')}

    _COUNTERPART_VITAL_TYPES = {'marriage', 'divorce'}

    # Who each marriage/divorce claim actually names as the couple, read
    # through the shared _lib.spouse_parties rule (see the module docstring's
    # "MARRIAGE/DIVORCE BUCKETING" section for the full reasoning behind
    # #63's fix). Computed once per claim, up front - who a claim names as a
    # couple does not depend on whose bucket is being built, and every
    # person named on the claim needs the same answer.
    #
    # `unscoped_claim_ids` collects the AMBIGUOUS claims: 2+ distinct people
    # named, not negated, and spouse_parties still could not tell the couple
    # from everyone else on the record (a roles-less certificate, or a
    # roles: map naming fewer than two spouses). That is deliberately a
    # narrower test than "spouse_parties resolved nothing" - a claim naming
    # fewer than two people has no counterpart to be ambiguous ABOUT (it
    # takes the same broad every-claim-of-this-type path a negation does,
    # preserved below exactly as before #63), and a genuine negation (SPEC
    # §8.6) is a real, on-purpose absence, not an unscoped certificate - so
    # it is read off the claim's own `negated` field, never inferred from an
    # empty party set. Only the true "certificate could name any of several
    # people" case is ambiguous: comparing it against everything fabricates
    # contradictions against unrelated marriages (the #63 repro: a groom's
    # own certificate "contradicting" his parents' wedding); comparing it
    # against nothing is the original #63 bug (two records of one marriage
    # that never get compared). Excluded from every bucket below and
    # reported back in the Result's `unscoped` list instead.
    claim_parties: dict[str, list[str]] = {}
    unscoped_claim_ids: set[str] = set()
    for cid, claim in claims_by_id.items():
        if claim['type'] not in _COUNTERPART_VITAL_TYPES:
            continue
        persons_with_roles = [
            (pid, claim_role.get((cid, pid))) for pid in claim_persons.get(cid, [])
        ]
        parties = spouse_parties(persons_with_roles)
        claim_parties[cid] = parties
        named = set(claim_persons.get(cid, []))
        if not parties and not claim['negated'] and len(named) >= 2:
            unscoped_claim_ids.add(cid)

    # Who each OTHER vital claim (birth/death/baptism/burial - _VITAL_TYPES
    # minus the couple types already handled above) is actually a record OF,
    # read through the shared `_lib.vital_subjects` rule (see the module
    # docstring's reasoning for #63, extended here to the same bug one level
    # over: #126 is a mother's own summary box reading `Born: 1888` off her
    # SON's birth certificate because `persons:` names her as a parent on it;
    # the identical shape here is her birth claim and his birth claim landing
    # in the same `('birth',)` bucket and being compared as if they were two
    # records of HER birth, because bucketing this branch used to key on
    # "every person named" the same way marriage/divorce did before #63.
    #
    # Deliberately scoped to _VITAL_TYPES only, NOT every non-relationship,
    # non-couple type. `vital_subjects` answers "who does this claim have no
    # OTHER role for" (VITAL_SUBJECT_ROLES, or - lacking that - whoever the
    # roles: map left unroled) - a rule written for records that name exactly
    # one kind of subject and cast everyone else as a bystander. A substantive
    # claim like census or residence has no such shape: `roles: {head: [...],
    # household_member: [...]}` legitimately roles EVERY person on the
    # record, so vital_subjects would find nobody unroled and return `[]` for
    # every one of them - silently emptying every substantive claim's bucket
    # and making `fha xref` report no candidates at all for census/occupation/
    # residence/etc. Substantive types keep their original, unscoped
    # same-type bucketing; only the four genuine vital record types (where
    # exactly one kind of "whose own X is this" question has an answer) are
    # scoped this way.
    # Computed once per claim, up front, for the same reason claim_parties is:
    # who a claim is a vital record of does not depend on whose bucket is
    # being built.
    #
    # `unscoped_vital_claim_ids` is this branch's twin of `unscoped_claim_ids`
    # above (#172): a death/burial/baptism claim naming 2+ people with NO
    # `roles:` map at all is `vital_subjects`'s case 2a - the exact
    # zero-role-signal shape `fha lint`'s W132 reports - and has not said
    # which of them it is a record of, any more than a roles-less marriage
    # certificate has said which two people married. `subjects == []` alone
    # is not enough to tell case 2a from case 5 (some role present, but
    # resolving to no subject either - a claim that DID answer the question,
    # just not in any of these people's favor); testing `not any(role for
    # _, role in persons_with_roles)` alongside it is what W132 does to tell
    # the two apart, reused here rather than re-derived a third time.
    #
    # Applies identically regardless of the claim's `negated` field (#173
    # follow-up, second round - see the module docstring's "OTHER VITAL-TYPE
    # BUCKETING" section for the full reasoning). A first pass at #173's
    # follow-up let a negated case 2a claim skip this set entirely, modeled
    # on the marriage/divorce negation bullet above - but a negated claim
    # naming a genuine subject and an incidental bystander with no `roles:`
    # map is exactly as ambiguous as the positive version of the same shape;
    # negation says the event didn't happen, not which named person it
    # didn't happen TO. Guessing "about everyone named" there let a
    # bystander's own real, accepted death claim read as contradicting a
    # negation that was never about them - so this stays a plain headcount
    # test with no polarity check, and `claim_vital_subjects[cid]` is always
    # `subjects` as `vital_subjects` returned it, never overridden to `None`.
    #
    # Covers both `accepted` and `needs-review` claims, matching every other
    # comparison `fha xref` makes (the module-level query above already
    # restricts `claims_by_id` to those two statuses) - a claim that dropped
    # out of every bucket here is a claim this tool compared nothing
    # against, whether or not it has cleared review yet. `fha lint`'s W132
    # matches this same accepted-or-needs-review, either-polarity scope for
    # its own case 2a check, so a human always has exactly one path (`roles:
    # deceased:`/`child:`) to resolve an ambiguous claim of this shape,
    # whichever tool surfaces it first.
    _OTHER_VITAL_TYPES = _VITAL_TYPES - _COUNTERPART_VITAL_TYPES
    claim_vital_subjects: dict[str, list[str] | None] = {}
    unscoped_vital_claim_ids: set[str] = set()
    for cid, claim in claims_by_id.items():
        if claim['type'] not in _OTHER_VITAL_TYPES:
            continue
        persons_with_roles = [
            (pid, claim_role.get((cid, pid))) for pid in claim_persons.get(cid, [])
        ]
        subjects = vital_subjects(claim['type'], persons_with_roles)
        claim_vital_subjects[cid] = subjects
        if subjects == [] and not any(role for _pid, role in persons_with_roles):
            unscoped_vital_claim_ids.add(cid)

    groups = []
    for person_id, claim_ids in sorted(claims_by_person.items()):
        by_group: dict[tuple, list[str]] = {}
        # Marriage/divorce claims with no specific counterpart identified -
        # a genuine negation naming no ex-partner, or (preserved from before
        # #63) a claim naming fewer than two people at all - compared
        # against every claim of that type for this person instead of just
        # one counterpart's bucket. Ambiguous claims do NOT land here (see
        # unscoped_claim_ids above); they are excluded from this loop
        # entirely.
        no_counterpart: dict[str, list[str]] = {}
        all_of_type: dict[str, list[str]] = {}
        for cid in claim_ids:
            claim = claims_by_id[cid]
            if claim['type'] == 'relationship':
                # A person can be e.g. a child in one relationship claim and a
                # parent in another - only pair claims with the same subtype
                # (nature) and this person's role, so a biological and an
                # adoptive parent of the same child never read as a contradiction.
                # A claim can bundle several
                # counterparts at once (e.g. roles: parent: [P2, P3]), so it's
                # bucketed once per individual counterpart rather than once
                # per whole counterpart set - otherwise a claim naming {P2, P3}
                # would never compare against one naming only {P2}.
                role = claim_role.get((cid, person_id))
                others = [p for p in claim_persons.get(cid, []) if p != person_id]
                for other in others:
                    key = (claim['type'], claim['subtype'], role, other)
                    by_group.setdefault(key, []).append(cid)
            elif claim['type'] in _COUNTERPART_VITAL_TYPES:
                parties = claim_parties.get(cid, [])
                if parties and person_id not in parties:
                    # Named on the claim (a witness, an informant, a parent
                    # on a certificate) but not one of the people
                    # spouse_parties actually names as married - this claim
                    # asserts nothing about THIS person's own marital
                    # status, so it does not enter their marriage/divorce
                    # bucket at all (#63's second open question). Without
                    # this skip, a certificate would still land in the
                    # parents' buckets too and could be compared against
                    # claims about their own, unrelated marriage - the same
                    # fabrication risk the ambiguous case guards against,
                    # just reached from the other direction.
                    continue
                if cid in unscoped_claim_ids:
                    # Ambiguous - already collected above, reported via
                    # `unscoped` instead of being forced into a bucket here.
                    continue
                # Everything else (a resolved couple, a genuine negation
                # with or without a specific ex-partner, or - unchanged from
                # before #63 - a claim naming fewer than two people at all)
                # buckets by counterpart the same way: narrow when
                # spouse_parties named one, broad (no_counterpart, compared
                # against every claim of this type) when it didn't.
                all_of_type.setdefault(claim['type'], []).append(cid)
                others = [p for p in parties if p != person_id]
                if others:
                    # Bucketed once per individual partner, not once per
                    # whole remaining party set - same reasoning as the
                    # relationship branch above. spouse_parties' serial case
                    # (roles: spouse: naming 3+ people, SPEC §8.3) leaves
                    # `others` with 2+ entries for this person; keying on the
                    # full frozenset would bucket that claim only against
                    # another claim naming the exact same set of partners,
                    # so a plain claim recording just one of those marriages
                    # (`roles: spouse:` naming this person and just one of
                    # the 2+) would never land in the same bucket - the same
                    # missed-comparison shape #63 fixed, reintroduced one
                    # level up.
                    for other in others:
                        key = (claim['type'], other)
                        by_group.setdefault(key, []).append(cid)
                else:
                    no_counterpart.setdefault(claim['type'], []).append(cid)
            else:
                subjects = claim_vital_subjects.get(cid)
                if subjects is not None and person_id not in subjects:
                    # Named on the claim (a parent, a witness, an informant)
                    # but not the person(s) `vital_subjects` says it's a
                    # record OF - this claim asserts nothing about THIS
                    # person's own birth/death/etc., so it does not enter
                    # their vitals bucket at all (#126's xref twin, same
                    # skip the marriage/divorce branch above already takes
                    # for spouse_parties). `None` here is the legacy vital
                    # claim naming at most one person with no roles: map (or
                    # any non-vital substantive type never looked up above),
                    # which keeps the old broad behavior unchanged - there is
                    # nobody else to be ambiguous about, negated or not
                    # (`vital_subjects` case 2). A vital claim naming two or
                    # more people with no roles: map at all - negated or
                    # positive alike (#173 follow-up, second round) - is NOT
                    # this case: `subjects` stays `[]`, so the `continue`
                    # above fires for everyone it names (#126, reopened) -
                    # the claim has not said whose vital it is, and negation
                    # only flips whether the event happened, not which named
                    # person it happened to. That zero-role-signal shape
                    # (case 2a, `unscoped_vital_claim_ids` above) is already
                    # reported in the Result's `unscoped` list rather than
                    # just dropped here (#172) - this `continue` still
                    # empties every person's bucket for it, but the claim
                    # itself is no longer untraceable.
                    continue
                key = (claim['type'],)
                by_group.setdefault(key, []).append(cid)

        pairs = []
        seen_pairs: set[frozenset[str]] = set()

        def _try_pair(cid_a: str, cid_b: str) -> None:
            pair_key = frozenset((cid_a, cid_b))
            if pair_key in seen_pairs:
                return
            seen_pairs.add(pair_key)
            claim_a, claim_b = claims_by_id[cid_a], claims_by_id[cid_b]
            if claim_a['source_id'] == claim_b['source_id']:
                return
            if pair_key in linked_pairs:
                return
            kind = _classify_pair(claim_a, claim_b)
            if kind is None:
                return
            pairs.append({
                'kind': kind,
                'claim_a': claim_a,
                'claim_b': claim_b,
            })

        for ids in by_group.values():
            ids = sorted(set(ids))
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    # A relationship claim can land in more than one
                    # per-counterpart bucket; _try_pair dedupes via seen_pairs.
                    _try_pair(ids[i], ids[j])

        for ctype, no_ids in no_counterpart.items():
            for cid_a in no_ids:
                for cid_b in all_of_type.get(ctype, []):
                    if cid_a == cid_b:
                        continue
                    _try_pair(cid_a, cid_b)

        if pairs:
            pairs.sort(key=lambda p: (p['claim_a']['type'], p['claim_a']['id'], p['claim_b']['id']))
            groups.append({
                'person_id': person_id,
                'person_name': person_names.get(person_id, person_id),
                'pairs': pairs,
            })

    groups.sort(key=lambda g: g['person_name'] or '')

    # Sorted by claim id for stable output - unscoped_claim_ids and
    # unscoped_vital_claim_ids were both built as sets while walking
    # claims_by_id, whose order sqlite does not guarantee. The two sets are
    # disjoint (marriage/divorce vs. the other four vital types never share a
    # claim `type`), so the union below adds no claim twice; the CLI tells
    # them apart afterward by each entry's own `claim['type']`.
    unscoped = [
        {
            'claim': claims_by_id[cid],
            # dict.fromkeys dedupes while keeping first-seen order - a claim
            # can name the same person twice (an alias written two ways,
            # `_lib.spouse_parties`'s own documented duplicate-handling
            # case), and claim_persons stores a row per entry with no
            # UNIQUE constraint to stop it, so without this a repeated
            # person shows up twice in the "Names ..." list below.
            'persons': [
                person_names.get(pid, pid)
                for pid in dict.fromkeys(claim_persons.get(cid, []))
            ],
        }
        for cid in sorted(unscoped_claim_ids | unscoped_vital_claim_ids)
    ]

    return {'status': 'ok', 'groups': groups, 'unscoped': unscoped}


# ── CLI ───────────────────────────────────────────────────────────────────────

def _fmt_claim(c: dict) -> str:
    date_label = c['date_edtf'] or '(no date)'
    place = f"  @ {c['place_text']}" if c.get('place_text') else ''
    return (
        f"{fmt_id_display(c['id'])}  [{c['source_title']} / {fmt_id_display(c['source_id'])}]  "
        f"{date_label}{place} - {c['value']}"
    )


def _vital_unscoped_advice(claim_type: str) -> str:
    """Repair text for one `unscoped` entry whose claim is birth/death/
    baptism/burial (#172) - the vital twin of the marriage/divorce advice in
    `_cmd_xref` below, worded per type the same way `fha lint`'s W132
    branches its message."""
    if claim_type in ('birth', 'baptism'):
        verb = 'was born' if claim_type == 'birth' else 'was baptized'
        return (f"not who {verb}, so it can't be compared against other records. "
                f"Add a roles: map - an indented `child: [P-…]` line naming who {verb}.")
    verb = 'died' if claim_type == 'death' else 'was buried'
    return (f"not which of them {verb}, so it can't be compared against other records. "
            f"Add a roles: map - either `deceased: [P-…]` naming who {verb} (more than "
            f"one id if they {verb} together), or leave that person unroled and name "
            "everyone else instead - `spouse:`, `child:`, `parent:`, or `witness:`.")


def _cmd_xref(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE

    result = run_xref(archive_root)
    if result['status'] == 'failed':
        return EXIT_FAILURE

    groups = result['groups']
    unscoped = result.get('unscoped') or []

    if not groups and not unscoped:
        print('No candidate pairs found.')
        return EXIT_CLEAN

    if groups:
        total = sum(len(g['pairs']) for g in groups)
        print(f'Found {total} candidate pair(s) across {len(groups)} person(s):')
        for group in groups:
            print(f"\n{group['person_name']}  [{fmt_id_display(group['person_id'])}]")
            for pair in group['pairs']:
                print(f"  {pair['kind']}:")
                print(f"    A: {_fmt_claim(pair['claim_a'])}")
                print(f"    B: {_fmt_claim(pair['claim_b'])}")
    else:
        print('No candidate pairs found.')

    # Two shapes share the `unscoped` list (#172): a roles-less marriage/
    # divorce certificate `_lib.spouse_parties` couldn't pair off, and a
    # death/birth/baptism/burial claim naming 2+ people with no roles: map at
    # all (`_lib.vital_subjects`'s case 2a). Each gets its own section and its
    # own repair wording - a shared sentence would either say "the couple"
    # to a birth claim or "who was born" to a marriage certificate.
    couple_items = [item for item in unscoped if item['claim']['type'] in ('marriage', 'divorce')]
    vital_items = [item for item in unscoped if item['claim']['type'] not in ('marriage', 'divorce')]

    if couple_items:
        print(f"\n{len(couple_items)} marriage/divorce claim(s) could not be scoped for comparison:")
        for item in couple_items:
            names = ', '.join(item['persons'])
            print(f"  {_fmt_claim(item['claim'])}")
            print(f"    Names {names} but not which two of them were the couple, so it "
                  "can't be compared against other records. Add a roles: map naming the "
                  'pair - `roles:` then an indented `spouse: [P-…, P-…]` line.')

    if vital_items:
        print(f"\n{len(vital_items)} birth/death/baptism/burial claim(s) could not be scoped for comparison:")
        for item in vital_items:
            names = ', '.join(item['persons'])
            print(f"  {_fmt_claim(item['claim'])}")
            print(f"    Names {names} but {_vital_unscoped_advice(item['claim']['type'])}")
    return EXIT_CLEAN


# User-facing --help text (the module docstring stays developer-facing).
_CLI_DESCRIPTION = """\
Find which of your facts back each other up and which ones conflict.

  fha xref

Read-only: it lists corroboration and contradiction candidates for you to act
on with `fha confirm xref`. It never writes to the archive."""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'xref' onto the main fha parser."""
    p = subs.add_parser(
        'xref',
        help='Cross-reference accepted/needs-review claims for corroboration/contradiction candidates',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    p.set_defaults(func=_cmd_xref)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha xref',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    parser.set_defaults(func=_cmd_xref)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
