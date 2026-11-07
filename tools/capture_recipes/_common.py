"""Shared helpers for the `fha capture` site recipes.

Underscore-prefixed so `capture._load_site_recipes` does not mistake it for a
recipe module. Recipes import the page-parsing primitives from their host
(`capture`) and the small genealogy-specific helpers (person lists, source-type
heuristics, date harvesting) from here, so no two recipes re-implement the same
table-walking or keyword-mapping logic.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

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


# ── A record's own hints: type, date, class (TI-066, every site) ──────────────

_MONTHS = {'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7, 'aug': 8,
           'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12}
_FULL_MONTHS = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
                'september', 'october', 'november', 'december']


def _month(word: str) -> int | None:
    w = (word or '').lower().rstrip('.')
    n = _MONTHS.get('sept' if w == 'sept' else w[:3])
    if not n:
        return None
    # A full name must really be that month ("Marchant" is not March).
    return n if len(w) <= 4 or _FULL_MONTHS[n - 1] == w else None


def date_to_edtf(value: str | None) -> str | None:
    """An Ancestry index date in EDTF, or None for anything but the plain shapes.

    "7 Jan 1950" -> "1950-01-07", "Jan 1950" -> "1950-01", "1950", and
    "abt 1845" -> "1845~". "bef 1900", ranges and stray text give None: no date
    beats a wrong one. Mirrors the companion's `ancDateToEdtf`; shared by the
    recipes whose sites write dates this way (Ancestry, FamilySearch).
    """
    v = ' '.join(str(value or '').split())
    approx = ''
    m = re.match(r'^(?:abt\.?|about|circa|ca\.?|c\.)\s+(.+)$', v, re.I)
    if m:
        v, approx = m.group(1), '~'
    m = re.match(r'^(\d{1,2}) ([A-Za-z]{3,9}\.?),? (\d{4})$', v)
    if m:
        mo, d = _month(m.group(2)), int(m.group(1))
        return f'{m.group(3)}-{mo:02d}-{d:02d}{approx}' if mo and 1 <= d <= 31 else None
    m = re.match(r'^([A-Za-z]{3,9}\.?) (\d{1,2}),? (\d{4})$', v)
    if m:
        mo, d = _month(m.group(1)), int(m.group(2))
        return f'{m.group(3)}-{mo:02d}-{d:02d}{approx}' if mo and 1 <= d <= 31 else None
    m = re.match(r'^([A-Za-z]{3,9}\.?) (\d{4})$', v)
    if m:
        mo = _month(m.group(1))
        return f'{m.group(2)}-{mo:02d}{approx}' if mo else None
    m = re.match(r'^(\d{4})$', v)
    return f'{m.group(1)}{approx}' if m else None


# The event a collection is about, as the words its date fields start with,
# in the order to try them. A burial, cemetery or funeral-home record often
# gives only the death date; an obituary's event is the death.
_EVENTS = [
    (re.compile(r'\bdeaths?\b|\bdied\b|\bmortality\b', re.I), ('Death',)),
    (re.compile(r'\bburials?\b|\bgraves?\b|\bcemeter(y|ies)\b|\bfunerals?\b', re.I), ('Burial', 'Death')),
    (re.compile(r'\bmarriages?\b|\bmarried\b', re.I), ('Marriage',)),
    (re.compile(r'\bbirths?\b|\bborn\b', re.I), ('Birth',)),
    (re.compile(r'\bbaptisms?\b', re.I), ('Baptism',)),
    (re.compile(r'\bchristenings?\b', re.I), ('Christening',)),
    (re.compile(r'\bdivorces?\b', re.I), ('Divorce',)),
]
_OBITUARY_RE = re.compile(r'\bobituar(y|ies)\b', re.I)

# A collection that runs over years - "1940-1997", "1930-Current",
# "1800s-present" - names when it was gathered, never the record's event.
_RANGE_RE = re.compile(r'\b\d{4}s?\s*[-\u2013/]\s*(current|present|\d{2,4}s?)\b', re.I)


def record_hints(collection: str | None, date_of) -> dict:
    """What a record says about itself: source type, date and class (TI-066).

    From the collection name and `date_of(event)` - the record's own date for
    "Death", "Marriage", "Census"... in EDTF, or None - never the rest of the
    page. Census -> census; a birth/marriage/death/burial/funeral/baptism/
    divorce collection -> vital-record; an obituary or newspaper index ->
    newspaper. The date is the record's own event date, trying every event the
    collection names in turn ("Birth and Death Records": the death, else the
    birth; a burial record's "Burial", else its "Death"; an obituary's
    "Death"), for a census its "Census" then "Residence" date, else its year,
    else the collection's one year when it names exactly one - never a year of
    a range, closed or open ("Death Index, 1940-1997", "Obituary Collection,
    1930-Current"). An index is `derivative`. Each is None when the record does
    not say. One rule for every site (the companion's `recordHints`, run from
    one shared case table).
    """
    c = collection or ''
    census = bool(re.search(r'\bcensus\b', c, re.I))
    events: list[str] = []
    if not census:
        for rx, names in _EVENTS:
            if rx.search(c):
                events.extend(n for n in names if n not in events)
    if census:
        date_words: tuple | list = ('Census', 'Residence')
    elif events:
        date_words = events
    else:
        date_words = ('Death',) if _OBITUARY_RE.search(c) else ()

    source_type = None
    if census:
        source_type = 'census'
    elif events:
        source_type = 'vital-record'
    elif re.search(r'\bnewspapers?\b|\bobituar(y|ies)\b', c, re.I):
        source_type = 'newspaper'

    source_date = None
    for word in date_words:
        source_date = date_of(word) or None
        if source_date:
            break
    years = re.findall(r'\b(1[5-9]\d{2}|20\d{2})\b', c)
    is_range = bool(_RANGE_RE.search(c))
    if not source_date and census:
        lead = re.match(r'^\s*(\d{4})\b', c)
        if lead:
            source_date = lead.group(1)
        elif len(years) == 1:
            source_date = years[0]
    if not source_date and not census and len(years) == 1 and not is_range:
        source_date = years[0]

    source_class = 'derivative' if re.search(r'\bindex(es)?\b', c, re.I) else None
    return {'source_type': source_type, 'source_date': source_date, 'source_class': source_class}


def fields_date_of(fields):
    """`date_of` over a record's labelled fields: "{Event} Date" / "{Event} Year"."""
    def date_of(word: str):
        rx = re.compile(rf'^{word} (date|year)$', re.I)
        value = next((v for label, v in fields if rx.match(str(label).strip())), None)
        return date_to_edtf(value)
    return date_of


# ── Seen It's injected badges (TI-067) ─────────────────────────────────────────

# Seen It's own elements: its pills, marks and tree chips (by class) and its
# badges and banners (ids starting "nsi-"). The "nsi-" modifier classes it
# toggles on the site's own rows (nsi-ruled-out, nsi-was-seen, nsi-has-clip)
# are NOT here: those rows are the record. Same list as the companion's
# ANC_SEEN_IT_CLASSES (browser-companion/src/lib/ancestry-record.js). Used by
# every recipe whose site Seen It marks (Ancestry, FamilySearch).
_SEEN_IT_CLASSES = frozenset({'nsi-pills', 'nsi-pill', 'nsi-marks', 'nsi-mark', 'nsi-node-chip'})
_VOID_TAGS = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
                        'meta', 'param', 'source', 'track', 'wbr'})


def _is_seen_it(attrs: list) -> bool:
    a = {k.lower(): (v or '') for k, v in attrs}
    if a.get('id', '').startswith('nsi-'):
        return True
    return bool(_SEEN_IT_CLASSES.intersection(a.get('class', '').split()))


class _SeenItStripper(HTMLParser):
    """Re-emit a page without Seen It's elements (and everything inside them).

    Only used when the page holds an "nsi-" marker at all. The output is for
    this recipe's own readers, not a preservation copy, so re-serializing the
    tags it keeps (attributes re-quoted) is fine.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self._skip = 0  # depth inside a Seen It element

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self._skip:
            if tag not in _VOID_TAGS:
                self._skip += 1
            return
        if _is_seen_it(attrs) and tag not in _VOID_TAGS:
            self._skip = 1
            return
        self.out.append(self.get_starttag_text() or f'<{tag}>')

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if not self._skip and not _is_seen_it(attrs):
            self.out.append(self.get_starttag_text() or f'<{tag}/>')

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            if tag not in _VOID_TAGS:
                self._skip -= 1
            return
        self.out.append(f'</{tag}>')

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.out.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self._skip:
            self.out.append(f'&{name};')

    def handle_charref(self, name: str) -> None:
        if not self._skip:
            self.out.append(f'&#{name};')

    def handle_comment(self, data: str) -> None:
        if not self._skip:
            self.out.append(f'<!--{data}-->')

    def handle_decl(self, decl: str) -> None:
        self.out.append(f'<!{decl}>')


def strip_seen_it(html: str) -> str:
    """`html` without the Seen It add-on's injected badges (TI-067)."""
    if not html or 'nsi-' not in html:
        return html or ''
    parser = _SeenItStripper()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - a pathological page: read it as it came
        return html
    return ''.join(parser.out)
