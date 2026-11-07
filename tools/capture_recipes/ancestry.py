"""Ancestry recipe for `fha capture` (TOOLING §13b).

Recognizes an Ancestry record/image page and recovers the collection title, the
record date, the persons listed in the household/index table, and the record's
image URL. Detection is by host (`ancestry.*`), with `og:site_name` as the
fallback when the page is captured without a `--url`.

Two record layouts are read:

- the older one, where the collection is the `og:title` / first `<h1>`;
- today's (`/search/collections/{dbId}/records/{recordId}`, with the index
  fields in `#recordFieldsContainer`), where the `<h1>` is the PERSON's name
  and the collection is only in `<title>` ("{collection} - Ancestry.com").
  Reading that `<h1>` as the collection is what typed census and death-index
  records `website` and dated a 1957 death from its index's first year, 1940
  (TI-066). On this layout the type, date and class come from the record
  itself - its collection and its own event-date field - by the same rule the
  capture companion applies at capture (`_record_hints`, mirrored from
  browser-companion/src/lib/ancestry-record.js `ancHints`; one shared case
  table, tests/fixtures/capture-samples/ancestry-hints.json, runs both).

The Seen It add-on injects its own badges into these pages (TI-067); a page
saved with them is read as if they were not there (`_strip_seen_it`).
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

from capture import (
    domain_of,
    extract_tables,
    first_nonempty,
    meta_content,
    parse_html,
    visible_text,
)
from capture_recipes._common import (
    harvest_date,
    looks_like_name,
    people_from_table,
    source_type_from_text,
)

SOURCE_NAME = 'Ancestry'
PRIORITY = 10

# A hint/evaluate context puts an "Does <name> match the person in your tree?"
# prompt in the first <h1>; it is UI chrome, not the record title.
_HINT_RE = re.compile(r'match the person in your tree|^does\b.+\?$', re.I)

# A durable public Newspapers.com clip URL embedded in an Ancestry index record
# (the off-site pointer to the real upstream source).
_OFFSITE_RE = re.compile(r'https?://(?:www\.)?newspapers\.com/clip/\d+/[^\s"\'<>]*')


class _GridParser(HTMLParser):
    """Read Ancestry's image-viewer detail panel: a CSS grid of `grid-cell`
    divs grouped into `grid-row`s (a header row of labels, then data rows of
    aligned values). Returns the same `list[list[str]]` shape as a table so the
    people reader can map the Surname/Given Name columns."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._depth = 0
        self._cell_depth: int | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag != 'div':
            return
        self._depth += 1
        classes = ''
        for k, v in attrs:
            if k == 'class':
                classes = v or ''
        names = classes.split()
        if 'grid-row' in names:
            self._row = []
            self.rows.append(self._row)
        elif 'grid-cell' in names and self._cell is None:
            self._cell = []
            self._cell_depth = self._depth

    def handle_endtag(self, tag: str) -> None:
        if tag != 'div':
            return
        if self._cell is not None and self._depth == self._cell_depth:
            text = ' '.join(''.join(self._cell).split())
            if self._row is not None:
                self._row.append(text)
            self._cell = None
            self._cell_depth = None
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _grid_rows(html: str) -> list[list[str]]:
    parser = _GridParser()
    try:
        parser.feed(html or '')
    except Exception:  # noqa: BLE001 - a pathological page still yields what parsed
        pass
    return [r for r in parser.rows if r]


def _people_from_grid(rows: list[list[str]]) -> list[str]:
    """People from a grid: a `Name` column, else `Given Name` + `Surname`.

    The header row (located by its name columns) defines the columns; each data
    row's name-bearing cells become a person. Labels never leak - they are the
    header, which is skipped.
    """
    header_i = None
    for i, row in enumerate(rows):
        low = [c.strip().lower() for c in row]
        if 'name' in low or 'given name' in low or 'surname' in low:
            header_i = i
            break
    if header_i is None:
        return []
    idx = {c.strip().lower(): i for i, c in enumerate(rows[header_i])}
    name_i, given_i, surname_i = idx.get('name'), idx.get('given name'), idx.get('surname')

    def _cell(row: list[str], i: int | None) -> str:
        return row[i].strip() if i is not None and i < len(row) else ''

    people: list[str] = []
    for row in rows[header_i + 1:]:
        if name_i is not None:
            name = _cell(row, name_i)
        else:
            name = f'{_cell(row, given_i)} {_cell(row, surname_i)}'.strip()
        if name and looks_like_name(name) and name not in people:
            people.append(name)
    return people


def _non_hint(text: str | None) -> str | None:
    """The text unless it is the tree-hint prompt (then `None`)."""
    return None if (text and _HINT_RE.search(text)) else text


# ── Seen It's injected badges (TI-067) ─────────────────────────────────────────

# Seen It's own elements: its pills, marks and tree chips (by class) and its
# badges and banners (ids starting "nsi-"). The "nsi-" modifier classes it
# toggles on the site's own rows (nsi-ruled-out, nsi-was-seen, nsi-has-clip)
# are NOT here: those rows are the record. Same list as the companion's
# ANC_SEEN_IT_CLASSES (browser-companion/src/lib/ancestry-record.js).
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


def _strip_seen_it(html: str) -> str:
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


# ── Today's record layout: the index fields (TI-066) ───────────────────────────

class _RecordFieldsParser(HTMLParser):
    """The `dt`/`dd` pairs inside `#recordFieldsContainer`, as (label, value)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fields: list[tuple[str, str]] = []
        self._depth = 0      # depth inside the container (0 = outside)
        self._skip = 0       # inside <script>/<style>/<button>
        self._cell: list[str] | None = None
        self._kind: str | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _VOID_TAGS:
            if tag == 'br' and self._cell is not None:
                self._cell.append(' ')
            return
        if self._depth:
            self._depth += 1
        elif dict(attrs).get('id') == 'recordFieldsContainer':
            self._depth = 1
            return
        if not self._depth:
            return
        if tag in ('script', 'style', 'button'):
            self._skip += 1
        elif tag in ('dt', 'dd'):
            self._cell, self._kind = [], tag

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS or not self._depth:
            return
        if tag in ('script', 'style', 'button') and self._skip:
            self._skip -= 1
        elif tag in ('dt', 'dd') and self._cell is not None and self._kind == tag:
            text = ' '.join(''.join(self._cell).split())
            if tag == 'dt':
                self.fields.append((text, ''))
            elif self.fields and not self.fields[-1][1]:
                self.fields[-1] = (self.fields[-1][0], text)
            self._cell = self._kind = None
        self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._depth and not self._skip and self._cell is not None:
            self._cell.append(data)


def _record_fields(html: str) -> list[tuple[str, str]]:
    parser = _RecordFieldsParser()
    try:
        parser.feed(html or '')
    except Exception:  # noqa: BLE001 - keep what parsed
        pass
    return parser.fields


# The site's name at the end of a tab title, on every Ancestry site and in both
# forms ("… - Ancestry.com", "… - Ancestry.co.uk", "… | Ancestry"). Same rule as
# the companion's ANC_TITLE_SUFFIX_RE.
_TITLE_SUFFIX_RE = re.compile(r'\s*[-|\u2013]\s*Ancestry(\.com)?(\.[a-z.]+)?\s*$', re.I)


def _collection_from_title(title: str | None) -> str | None:
    """"1870 United States Federal Census - Ancestry.com" -> the collection."""
    if not title:
        return None
    t = _TITLE_SUFFIX_RE.sub('', title).strip()
    return t or None


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


def _date_to_edtf(value: str | None) -> str | None:
    """An Ancestry index date in EDTF, or None for anything but the plain shapes.

    "7 Jan 1950" -> "1950-01-07", "Jan 1950" -> "1950-01", "1950", and
    "abt 1845" -> "1845~". "bef 1900", ranges and stray text give None: no date
    beats a wrong one. Mirrors the companion's `ancDateToEdtf`.
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


def _record_hints(collection: str | None, fields: list[tuple[str, str]]) -> dict:
    """What a record says about itself: source type, date and class (TI-066).

    From the collection name and the record's own index fields, never the rest
    of the page: census -> census; a birth/marriage/death/burial/funeral/
    baptism/divorce collection -> vital-record; an obituary or newspaper index
    -> newspaper. The date is the record's own event date ("Death Date" in a
    death index; "Burial Date", else "Death Date", for a burial, cemetery or
    funeral record; "Death Date" for an obituary), else a census's "Residence
    Date", else its year, else the collection's one year when it names exactly
    one - never a year of a range, closed or open ("Death Index, 1940-1997" or
    "Obituary Collection, 1930-Current" says when the collection runs, not when
    she died). An index is `derivative`. Each is None when the record does not
    say. Mirrors the companion's `ancHints`.
    """
    c = collection or ''
    census = bool(re.search(r'\bcensus\b', c, re.I))
    event = None if census else next((names for rx, names in _EVENTS if rx.search(c)), None)
    date_words = event or (('Death',) if not census and _OBITUARY_RE.search(c) else ())

    source_type = None
    if census:
        source_type = 'census'
    elif event:
        source_type = 'vital-record'
    elif re.search(r'\bnewspapers?\b|\bobituar(y|ies)\b', c, re.I):
        source_type = 'newspaper'

    def field_value(pattern: str) -> str | None:
        rx = re.compile(pattern, re.I)
        return next((v for label, v in fields if rx.match(label.strip())), None)

    source_date = None
    for word in date_words:
        source_date = _date_to_edtf(field_value(rf'^{word} (date|year)$'))
        if source_date:
            break
    years = re.findall(r'\b(1[5-9]\d{2}|20\d{2})\b', c)
    is_range = bool(_RANGE_RE.search(c))
    if not source_date and census:
        source_date = _date_to_edtf(field_value(r'^residence (date|year)$'))
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


def detect(html: str, url: str | None) -> bool:
    domain = domain_of(url)
    if 'ancestry.' in domain:
        return True
    page = parse_html(html)
    return (meta_content(page, 'og:site_name') or '').strip().lower().startswith('ancestry')


def _image_url(html: str, url: str | None) -> str | None:
    """The record image: og:image, else any in-page image-viewer link."""
    page = parse_html(html)
    return first_nonempty(meta_content(page, 'og:image'))


def extract(html: str, url: str | None) -> dict:
    html = _strip_seen_it(html)
    page = parse_html(html)
    # Today's record layout: the <h1> is the person, the collection is in
    # <title> (TI-066). Recognised by its index-fields container.
    record_layout = 'id="recordFieldsContainer"' in html or "id='recordFieldsContainer'" in html
    hints = None
    if record_layout:
        collection = first_nonempty(
            meta_content(page, 'og:title'), _collection_from_title(page.title),
        )
        name = _non_hint(page.h1)
        title = (f'{name} - {collection}' if name and collection
                 else first_nonempty(collection, name, page.title)) or 'Ancestry record'
        collection = collection or title
        hints = _record_hints(collection, _record_fields(html))
    else:
        # Ancestry ships no og:title on most pages; the clean document.title is
        # the best title, and the first <h1> may be a tree-hint prompt (skip it).
        title = first_nonempty(
            meta_content(page, 'og:title'), _non_hint(page.h1), page.title,
        ) or 'Ancestry record'
        collection = first_nonempty(
            meta_content(page, 'og:title'), _non_hint(page.h1),
        ) or title

    # People from the household/index table (records pages), else the
    # image-viewer's grid-cell detail panel (imageviewer pages yield [] from the
    # table path because the panel is divs, not a <table>).
    people: list[str] = []
    for rows in extract_tables(html):
        people = people_from_table(rows)
        if people:
            break
    if not people:
        people = _people_from_grid(_grid_rows(html))

    # Body fallback for the date (Ancestry ships no og: date), but only the head
    # of the visible text: a full page.text scan tends to grab a footer copyright
    # year ("© 1996-2026") and file it as the source date.
    if hints is not None:
        # The record's own hints, or none: on this layout the page's text
        # around the record names the collection's year RANGE, never the event.
        source_date = hints['source_date']
        source_type = hints['source_type'] or source_type_from_text(collection, 'website')
    else:
        source_date = harvest_date(
            collection, title, meta_content(page, 'og:description'), page.text[:400])
        source_type = source_type_from_text(f'{collection} {title}', 'website')

    citation_bits = [title.rstrip('.')]
    if source_date:
        citation_bits.append(source_date)
    citation = '. '.join(citation_bits) + '. Ancestry.com.'
    if url:
        citation += f' {url}'

    external_links: list[dict] = []
    if url:
        external_links.append({'url': url})
    image = _image_url(html, url)
    if image and image != url:
        external_links.append({'url': image})
    # An Ancestry "Index" record can point off-site (e.g. a Newspapers.com index
    # embeds the durable public clip URL); surface it so the reviewer can go
    # upstream to the real source. Generic mechanism, Newspapers-shaped for v1.
    seen = {link['url'] for link in external_links}
    for clip in _OFFSITE_RE.findall(html or ''):
        clip = clip.rstrip('"\'')
        if clip not in seen:
            external_links.append({'url': clip, 'label': 'off-site source'})
            seen.add(clip)

    return {
        'title': title,
        'source_type': source_type,
        'citation': citation,
        'repository': 'Ancestry.com',
        'source_date': source_date,
        'external_links': external_links,
        'people': people,
        'body': visible_text(page),
        'collection': collection,
        'terms': '; '.join(people) if people else '',
        'source_class': hints['source_class'] if hints else None,
    }
