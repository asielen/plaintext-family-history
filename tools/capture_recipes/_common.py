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

# A label cell ending in "name" (e.g. "Father's Name", "Spouse's Name") marks a
# label/value row, not a person row — the value cell holds the actual name.
_NAME_LABEL_RE = re.compile(r'\bname\b', re.IGNORECASE)


def looks_like_name(text: str) -> bool:
    """True when `text` reads like a personal name (≥2 alphabetic tokens)."""
    text = (text or '').strip()
    if not text or text.lower() in _HEADER_WORDS:
        return False
    return bool(_NAME_RE.match(text))


def people_from_table(rows: list[list[str]], *, name_col: int = 0, limit: int = 40) -> list[str]:
    """First-column person names from a parsed table (header rows skipped).

    `rows` is one table from `capture.extract_tables`. Cells that read like a
    name (`looks_like_name`) in column `name_col` become the person list, in
    document order, de-duplicated. A header row (its name cell is a header word)
    is skipped naturally because it never matches `looks_like_name`.

    A row shaped as a label/value pair (`["Father's Name", "William Smith"]`,
    as in FamilySearch's record-detail fact tables) is detected by its label
    cell ending in "name" and the value cell is read instead — otherwise the
    label itself would pass `looks_like_name` and be mistaken for a person.
    """
    people: list[str] = []
    for row in rows:
        if len(row) <= name_col:
            continue
        cell = row[name_col].strip()
        if len(row) > name_col + 1 and _NAME_LABEL_RE.search(cell):
            cell = row[name_col + 1].strip()
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
