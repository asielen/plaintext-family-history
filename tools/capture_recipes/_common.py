"""Shared helpers for the `fha capture` site recipes.

Underscore-prefixed so `capture._load_site_recipes` does not mistake it for a
recipe module. Recipes import the page-parsing primitives from their host
(`capture`) and the small genealogy-specific helpers (person lists, source-type
heuristics, date harvesting) from here, so no two recipes re-implement the same
table-walking or keyword-mapping logic.
"""

from __future__ import annotations

import re

from capture import meta_content, parse_html  # host-provided HTML primitives

# Header-row labels that mark a table's heading rather than a person row.
_HEADER_WORDS = frozenset({
    'name', 'names', 'household members', 'household', 'member', 'members',
    'family members', 'family member', 'relationship', 'relation', 'age',
    'sex', 'gender', 'role', 'birth', 'birth year', 'birthplace',
    'event', 'events', 'event date',
})

# A plausible person name: at least two whitespace-separated word-ish tokens,
# letters/marks/hyphens/apostrophes/periods only. Filters ages, dates, blanks.
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z.'\-]*(?:\s+[A-Za-z.'\-]+)+$")

_YEAR_RE = re.compile(r'\b(1[5-9]\d{2}|20\d{2})\b')

# A label cell containing "name" (e.g. "Father's Name", "Name at Birth") marks a
# label/value row, not a person row - the value cell holds the actual name.
_NAME_LABEL_RE = re.compile(r'\bname\b', re.IGNORECASE)

# A cell whose final token is one of these is a record fact-label ("Birth Date",
# "Marital Status", "Estimated Birth Year") rather than a person. The set is
# deliberately limited to field-category terms that are not plausible as the
# *last* token of a real personal name, so the guard never silently drops a
# genuine name (TOOLING §13b: an unknown label that looks like a name is
# tolerable noise the reviewer unticks; a *known* label must never leak). Words
# that double as real surnames (Page, Ward, House, Young, Place, Race, Roll, …)
# are intentionally excluded - their labels are caught by the structural
# label/value read or, for the common "<domain> Place" form, by _PLACE_LABEL_RE.
_LABEL_TAIL_RE = re.compile(
    r"\b(name|date|year|status|type|number|age|sex|gender|"
    r"occupation|residence|relationship|relation|title|nativity|"
    r"citizenship|district|enumeration|industry|employer|grade|"
    r"school|completed|marital)\b[\s.:]*$",
    re.IGNORECASE,
)

# "<place-domain word> Place" / "… Residence" is a fact label ("Birth Place",
# "Event Place", "Residence Place"); a bare "<given> Place" (the surname
# "Place") is a real name and must not match. "Place"/"Race"/"Roll" are kept
# out of _LABEL_TAIL_RE precisely because they are attested surnames - they
# only read as a label when a place-domain word precedes them.
_PLACE_LABEL_RE = re.compile(
    r"\b(?:birth|death|event|residence|marriage|burial|baptism|christening|"
    r"census|arrival|departure|immigration|emigration|naturali[sz]ation|"
    r"origin|home|native|last|current|former)\s+(?:place|residence)\b[\s.:]*$",
    re.IGNORECASE,
)

# Whole-cell fact labels that don't end in a label-tail word (their final token
# doubles as a surname, so only the exact full-string match is safe to reject).
_FIELD_LABELS = frozenset({
    'relation to head of house', 'head of household',
    'others on this record', 'others in this record', 'others in record',
})


def is_field_label(text: str) -> bool:
    """True when `text` is a record fact-label, not a person name.

    Fact labels ("Birth Date", "Event Type", "Father's Name", "Marital Status")
    leak into the people list because they read like short alpha phrases. In a
    label/value table the value lives in the *next* cell, so a label cell is
    never itself a person. Recognized three ways, cheapest first: an exact
    header/field-label string, the word "name" anywhere, a "<domain> Place"
    label, or a field-category tail word.
    """
    t = (text or '').strip().lower().rstrip(':').strip()
    if not t:
        return False
    if t in _HEADER_WORDS or t in _FIELD_LABELS:
        return True
    if _NAME_LABEL_RE.search(t):
        return True
    if _PLACE_LABEL_RE.search(t):
        return True
    return bool(_LABEL_TAIL_RE.search(t))


def looks_like_name(text: str) -> bool:
    """True when `text` reads like a personal name (≥2 alphabetic tokens)."""
    text = (text or '').strip()
    if not text or is_field_label(text):
        return False
    return bool(_NAME_RE.match(text))


def people_from_table(rows: list[list[str]], *, name_col: int = 0, limit: int = 40) -> list[str]:
    """First-column person names from a parsed table (header rows skipped).

    `rows` is one table from `capture.extract_tables`. Cells that read like a
    name (`looks_like_name`) in column `name_col` become the person list, in
    document order, de-duplicated. A header row (its name cell is a header word)
    is skipped naturally because it never matches `looks_like_name`.

    A row shaped as a label/value pair (`["Father's Name", "William Smith"]` or
    `["Birth Date", "1850"]`, as in FamilySearch/Ancestry record-detail fact
    tables) is detected by its label cell (`is_field_label`) and the value cell
    is read instead - otherwise the label itself would pass `looks_like_name`
    and be mistaken for a person. Only a *name-bearing* label ("Father's Name")
    has a person in its value cell; a place/date/status label ("Birth Place")
    does not, so its value is dropped rather than promoted - else "Birth Place |
    New York" would leak "New York" as a person. A label with no value cell is
    likewise dropped, so a known field label can never leak as a person.
    """
    people: list[str] = []
    for row in rows:
        if len(row) <= name_col:
            continue
        cell = row[name_col].strip()
        if is_field_label(cell):
            if _NAME_LABEL_RE.search(cell) and len(row) > name_col + 1:
                cell = row[name_col + 1].strip()
            else:
                cell = ''
        if looks_like_name(cell) and cell not in people:
            people.append(cell)
        if len(people) >= limit:
            break
    return people


def source_type_from_text(text: str, default: str) -> str:
    """Map collection/title text to a controlled source_type, else `default`.

    A coarse keyword heuristic (TOOLING §13b "recipe-inferred"): census → census,
    a vital event word → vital-record, an explicit newspaper word → newspaper.
    The reviewer refines anything this gets wrong; the point is a usable guess.
    """
    t = (text or '').lower()
    if 'census' in t:
        return 'census'
    if any(w in t for w in (
        'birth', 'marriage', 'married', 'death', 'died', 'baptism',
        'christening', 'burial', 'divorce', 'vital record',
    )):
        return 'vital-record'
    if 'newspaper' in t or 'obituary' in t:
        return 'newspaper'
    return default


def harvest_date(*texts: str | None) -> str | None:
    """First four-digit year found across `texts` (the source's own date hint)."""
    for text in texts:
        if not text:
            continue
        m = _YEAR_RE.search(str(text))
        if m:
            return m.group(1)
    return None


def published_time(html: str) -> str | None:
    """`article:published_time` / `og:updated_time` meta date, if the page set one."""
    page = parse_html(html)
    return meta_content(page, 'article:published_time', 'og:updated_time', 'date')
