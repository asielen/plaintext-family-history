"""FamilySearch recipe for `fha capture` (TOOLING §13b).

Recognizes a FamilySearch record or tree-person page and recovers the title,
the event date, the collection, and the persons named in the record. Detection
is by host (`familysearch.org`), with `og:site_name` as the fallback.

FamilySearch is an Ancestry-class outlier (no `og:`/JSON-LD; React detail
panels) and is inconsistent across record types: a *content/record* page renders
facts as "Label: Value" text with the collection as the title, while an
*index/landing* page uses label/value tables with the *person* as the title.
So the recipe reads people three ways - the guarded table path, the React
content panel ("Given Name:/Surname:" + the "Others on This Record" list), and
the title-person of an index page - and separates a person-as-title from the
collection so the date/type still re-derive.
"""

from __future__ import annotations

import re

from capture import (
    domain_of,
    extract_tables,
    first_nonempty,
    meta_content,
    parse_html,
    visible_text,
)
from capture_recipes._common import (
    date_to_edtf,
    harvest_date,
    is_field_label,
    looks_like_name,
    people_from_table,
    record_hints,
    source_type_from_text,
    strip_seen_it,
)

SOURCE_NAME = 'FamilySearch'
PRIORITY = 20

# An index/landing page titles itself `Person, "Collection"` (no og: meta) - the
# quoted tail is the collection, the head is the record's subject person.
_TITLE_COLLECTION_RE = re.compile(r'^(?P<person>.+?),\s*["“\'](?P<collection>.+?)["”\']\s*$')

# The React content panel renders the subject as "Given Name: <v> … Surname: <v>"
# in whitespace-collapsed text. The given-name capture is restricted to name
# characters (no digits/colons) so it can't span an intervening fact like
# "Sex: Male Birth Date: 1905". The surname continues past one token so a
# multi-word surname (Van Buren, De La Cruz) is kept whole, but stops at the
# next "Label:" boundary or a known fact-label word so it doesn't swallow the
# following field.
_FS_NEXT_LABEL = (
    r"birth|death|event|census|marriage|burial|baptism|christening|residence|"
    r"immigration|emigration|sex|age|race|gender|born|died|place|date|"
    r"relationship|relation|marital|occupation|nativity|citizenship|others|"
    r"events?|sources?|record"
)
# A possessive relationship label ("Mother's Name:", "Father's Name:") is also a
# surname boundary - the bare `[A-Za-z]+:` stop doesn't see the `'s`, so match it
# explicitly (straight or curly apostrophe).
_GIVEN_SURNAME_RE = re.compile(
    r"Given Name:\s*([A-Za-z][A-Za-z .'\-]*?)\s+"
    r"Surname:\s*([A-Za-z][A-Za-z.'\-]*"
    # Continue past at most 3 more tokens (covers De La Cruz / Van Der Berg)
    # while stopping at a Label:/possessive/known-fact-word boundary, so the
    # surname can't run away into trailing free text in the collapsed panel.
    r"(?:\s+(?![A-Za-z]+:)(?![A-Za-z]+['’]s\b)(?!(?:" + _FS_NEXT_LABEL + r")\b)"
    r"[A-Za-z][A-Za-z.'\-]*){0,3})",
    re.I)

# "Others on/in This Record" lists the household; the names sit in data-testid
# attributes (a stabler hook than FamilySearch's hashed CSS-module classes).
_OTHERS_RE = re.compile(r'Others (?:on|in) This Record', re.I)
_SECTION_END_RE = re.compile(r'\b(?:Events?|Sources?|Record Information|New Event)\b')
_TESTID_RE = re.compile(r'data-testid="([^"]+)"')


# A date as FamilySearch writes it on a record page: "21 Nov 1927",
# "3 June 1913", "June 1913", "1920", with an optional "abt".
_FS_DATE = r'(?:(?:abt\.?|about)\s+)?(?:\d{1,2}\s+[A-Z][a-z]+\.?\s+\d{4}|[A-Z][a-z]+\.?\s+\d{1,2},?\s+\d{4}|[A-Z][a-z]+\.?\s+\d{4}|\d{4})'
# Both layouts FamilySearch draws: "Event Date 21 Nov 1927" (label and value in
# their own elements) and "Event Date: 21 Nov 1927" (one line, a colon).
_FS_EVENT_TYPE_RE = re.compile(r'\bEvent Type:?\s+([A-Z][A-Za-z]+)\b')
_FS_EVENT_DATE_RE = re.compile(r'\bEvent Date:?\s+(' + _FS_DATE + r')\b')
_FS_NAMED_DATE_RE = re.compile(
    r'\b(Birth|Death|Burial|Marriage|Christening|Baptism|Residence|Census|Divorce)\s+Date:?\s+(' + _FS_DATE + r')\b')
_FS_ANY_DATE_RE = re.compile(r'(?<![\d-])(' + _FS_DATE + r')(?![\d-])')
_RANGE_TEXT_RE = re.compile(r'\b\d{4}s?\s*[-\u2013/]\s*(?:current|present|\d{2,4}s?)\b', re.I)


def _description_date(description: str | None, collection: str | None) -> str | None:
    """The record's own date as its og:description gives it ("Marriage on 14
    June 1898 in Fairview. Collection: County Marriages, 1865-1950."), in EDTF.

    The fallback when the record's facts on the page give none. The
    collection's name and any year range are cut out first: they say when
    the collection runs, never when the event happened (TI-066).
    """
    text = description or ''
    if collection:
        text = text.replace(collection, ' ')
    text = re.sub(r'Collection:.*$', ' ', text, flags=re.I | re.S)
    text = _RANGE_TEXT_RE.sub(' ', text)
    for m in _FS_ANY_DATE_RE.finditer(text):
        v = date_to_edtf(m.group(1))
        if v:
            return v
    return None


def _record_events(html: str, text: str) -> list[tuple[str, str]]:
    """The record's own dates as `(label, value)` - "Death Date", "21 Nov 1927" -
    from a record page: its label/value table when it has one, else its text.

    FamilySearch shows the record's event as "Event Type Death ... Event Date
    21 Nov 1927", and other facts as "Birth Date 26 May 1887". The event's
    date is the one the record is about (TI-066): it is what `record_hints`
    asks for, never a year from the collection's name.
    """
    fields: list[tuple[str, str]] = []
    pairs: dict[str, str] = {}
    for rows in extract_tables(html):
        for row in rows:
            if len(row) == 2 and row[0] and row[1]:
                pairs.setdefault(row[0].strip().lower(), row[1].strip())
    event = pairs.get('event type')
    event_date = pairs.get('event date')
    if not event:
        m = _FS_EVENT_TYPE_RE.search(text or '')
        event = m.group(1) if m else None
    if not event_date:
        m = _FS_EVENT_DATE_RE.search(text or '')
        event_date = m.group(1) if m else None
    if event and event_date:
        fields.append((f'{event} Date', event_date))
    for label, value in pairs.items():
        if label.endswith(' date') and label != 'event date':
            fields.append((label.title(), value))
    for m in _FS_NAMED_DATE_RE.finditer(text or ''):
        fields.append((f'{m.group(1)} Date', m.group(2)))
    return fields


def _fields_date_of(fields: list[tuple[str, str]]):
    def date_of(word: str):
        for label, value in fields:
            if label.lower() == f'{word} date'.lower():
                v = date_to_edtf(value)
                if v:
                    return v
        return None
    return date_of


_ARK_RECORD_RE = re.compile(r'familysearch\.org/(?:[a-z]{2}/)?ark:/61903/1:1:[A-Z0-9-]+', re.I)


def _ark_record(url: str | None) -> bool:
    """Is `url` a FamilySearch record entry (ark:/61903/1:1:{id})?"""
    return bool(url and _ARK_RECORD_RE.search(url))


def detect(html: str, url: str | None) -> bool:
    if 'familysearch.org' in domain_of(url):
        return True
    page = parse_html(html)
    return (meta_content(page, 'og:site_name') or '').strip().lower().startswith('familysearch')


def _split_title_collection(page) -> tuple[str, str, str | None]:
    """`(title, collection, title_person)`.

    On an index/landing page the `<title>` is the person with the collection in
    a quoted tail (`Warren B Hartley, "California, Birth Index, 1905-1995"`);
    recover the collection (so date/type re-derive) and surface the person.
    Otherwise the title is the og:title/h1 and the collection comes from the
    meta/description sources.
    """
    m = _TITLE_COLLECTION_RE.match((page.title or '').strip())
    if m and looks_like_name(m.group('person')):
        collection = m.group('collection').strip()
        return collection, collection, m.group('person').strip()
    raw_title = first_nonempty(
        meta_content(page, 'og:title'), page.h1, page.title,
    ) or 'FamilySearch record'
    collection = first_nonempty(
        meta_content(page, 'fs:collection', 'collection'),
        meta_content(page, 'og:description'),
        raw_title,
    ) or raw_title
    return raw_title, collection, None


def _subject_from_text(text: str) -> str | None:
    """The content panel's subject, from its "Given Name: … Surname: …" text."""
    m = _GIVEN_SURNAME_RE.search(text or '')
    if not m:
        return None
    name = f'{m.group(1).strip()} {m.group(2).strip()}'
    return name if looks_like_name(name) else None


def _household_from_html(html: str) -> list[str]:
    """Names from the "Others on This Record" list (their data-testid values)."""
    m = _OTHERS_RE.search(html)
    if not m:
        return []
    rest = html[m.end():]
    end = _SECTION_END_RE.search(rest)
    if end:
        rest = rest[:end.start()]
    names: list[str] = []
    for tid in _TESTID_RE.findall(rest):
        tid = tid.strip()
        if looks_like_name(tid) and not is_field_label(tid) and tid not in names:
            names.append(tid)
    return names


def extract(html: str, url: str | None) -> dict:
    html = strip_seen_it(html)
    page = parse_html(html)
    title, collection, title_person = _split_title_collection(page)

    # People, in order of reliability: a guarded label/value table, then the
    # React content panel (subject + household), then the index-page title
    # person. Never the labels (the shared _common.py guard).
    people: list[str] = []
    for rows in extract_tables(html):
        people = people_from_table(rows)
        if people:
            break
    if not people:
        subject = _subject_from_text(page.text) or title_person
        if subject and looks_like_name(subject):
            people.append(subject)
        for member in _household_from_html(html):
            if member not in people:
                people.append(member)
    if not people:
        subject = first_nonempty(page.h1, meta_content(page, 'og:title'))
        if subject and looks_like_name(subject):
            people = [subject]

    # A record page names its collection (in its title's quoted tail, or its
    # meta); the record's own event then types and dates it, and its index
    # class follows its collection (TI-066: "California, County Marriages,
    # 1850-1952" dated an 1866 marriage 1850). Elsewhere - a tree person, a
    # page with no collection - the old reading stands.
    hints = None
    if title_person or _ark_record(url):
        hints = record_hints(collection, _fields_date_of(_record_events(html, page.text)))
    if hints is not None:
        source_date = hints['source_date'] or _description_date(
            meta_content(page, 'og:description'), collection)
        source_type = hints['source_type'] or source_type_from_text(f'{collection} {title}', 'website')
    else:
        source_date = harvest_date(
            meta_content(page, 'og:description'), collection, title,
        )
        source_type = source_type_from_text(f'{collection} {title}', 'website')

    citation_bits = [title.rstrip('.')]
    if source_date:
        citation_bits.append(source_date)
    citation = '. '.join(citation_bits) + '. FamilySearch (accessed via familysearch.org).'
    if url:
        citation += f' {url}'

    external_links = [{'url': url}] if url else []
    return {
        'title': title,
        'source_type': source_type,
        'citation': citation,
        'repository': 'FamilySearch',
        'source_date': source_date,
        'external_links': external_links,
        'people': people,
        'body': visible_text(page),
        'collection': collection,
        'terms': '; '.join(people) if people else '',
        'source_class': hints['source_class'] if hints else None,
    }
