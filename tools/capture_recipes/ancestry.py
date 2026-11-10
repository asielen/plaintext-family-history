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
    _VOID_TAGS,
    date_to_edtf,
    fields_date_of,
    harvest_date,
    record_hints,
    strip_seen_it,
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
# A photo or story someone shared to their tree, in the media viewer
# (/mediaui-viewer/.../media/{mediaId}). Its date is the one its Details give,
# which the capture add-on sends; the page's other years - a linked relative's
# "1840-1910", the day it was uploaded - are never its date (PR #258 review).
_MEDIA_PAGE_RE = re.compile(r'/mediaui-viewer/(?:[^?#\s]*/)?media/[0-9a-f-]{36}', re.I)

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


# The Seen It stripper lives in _common (both recipes read pages saved with
# its badges); the name the readers below and the tests use.
_strip_seen_it = strip_seen_it


def _date_to_edtf(value: str | None) -> str | None:
    """Ancestry's index date in EDTF (the shared `_common.date_to_edtf`)."""
    return date_to_edtf(value)


def _record_hints(collection: str | None, fields: list[tuple[str, str]]) -> dict:
    """An Ancestry record's hints: `_common.record_hints` over its labelled
    fields. Mirrors the companion's `ancHints`."""
    return record_hints(collection, fields_date_of(fields))


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


# A year that is one end of a range: joined to another by a hyphen, an en or
# em dash (spaces around it or not), a slash, or " to " - "1940-1997",
# "1940 - 1997", "1908\u20141932", "1865 to 1925" (PR #258 round 4).
_RANGE_JOIN = r'(?:\s*[-\u2013\u2014/]\s*|\s+to\s+)'


def _year_already_in_title(source_date: str, title: str) -> bool:
    """True when `source_date` is a bare year that `title` names on its own -
    not as one end of a range of years."""
    if not re.fullmatch(r'\d{4}', source_date):
        return False
    for m in re.finditer(r'(?<!\d)' + source_date + r'(?!\d)', title):
        before, after = title[:m.start()], title[m.end():]
        if re.search(r'\d{2,4}' + _RANGE_JOIN + r'$', before, re.I):
            continue
        if re.match(_RANGE_JOIN + r'\d{2,4}', after, re.I):
            continue
        return True
    return False


def extract(html: str, url: str | None) -> dict:
    html = _strip_seen_it(html)
    page = parse_html(html)
    # Today's record layout: the <h1> is the person, the collection is in
    # <title> (TI-066). Recognised by its index-fields container.
    record_layout = 'id="recordFieldsContainer"' in html or "id='recordFieldsContainer'" in html
    hints = None
    # A shared photo's page (the media viewer) is read for nothing but its
    # tab title: its <h1>, og:title, tables, lists and text can name the
    # sharer's tree or the other members who saved the photo, and none of
    # them is the family's. Its title, people, date and words come from the
    # capture add-on's capture.json (PR #258 rounds 4, 10).
    shared_photo = bool(url and _MEDIA_PAGE_RE.search(url))
    if shared_photo:
        title = _TITLE_SUFFIX_RE.sub('', page.title or '').strip() or 'Photo shared on Ancestry'
        collection = title
    elif record_layout:
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
    for rows in ([] if shared_photo else extract_tables(html)):
        people = people_from_table(rows)
        if people:
            break
    if not people and not shared_photo:
        people = _people_from_grid(_grid_rows(html))

    # Body fallback for the date (Ancestry ships no og: date), but only the head
    # of the visible text: a full page.text scan tends to grab a footer copyright
    # year ("© 1996-2026") and file it as the source date.
    if hints is not None:
        # The record's own hints, or none: on this layout the page's text
        # around the record names the collection's year RANGE, never the event.
        source_date = hints['source_date']
        source_type = hints['source_type'] or source_type_from_text(collection, 'website')
    elif shared_photo:
        # A shared photo: no date unless its Details give one (sent in
        # capture.json) - unknown, never a year guessed from the page.
        source_date = None
        source_type = 'photo'
    else:
        source_date = harvest_date(
            collection, title, meta_content(page, 'og:description'), page.text[:400])
        source_type = source_type_from_text(f'{collection} {title}', 'website')

    citation_bits = [title.rstrip('.')]
    # The date, unless it is a bare year the title already names on its own:
    # "1880 United States Federal Census. 1880." is a stutter. A full date is
    # always kept, and so is a year the title gives only as one end of a range
    # ("Death Index, 1940-1997" says nothing about a death on 1940-01-07, nor
    # "1917-1918" about which year a draft card is) - PR #258 review.
    if source_date and not _year_already_in_title(str(source_date), title):
        citation_bits.append(source_date)
    citation = '. '.join(citation_bits) + '. Ancestry.com.'
    if url:
        citation += f' {url}'

    external_links: list[dict] = []
    if url:
        external_links.append({'url': url})
    image = None if shared_photo else _image_url(html, url)
    if image and image != url:
        external_links.append({'url': image})
    # An Ancestry "Index" record can point off-site (e.g. a Newspapers.com index
    # embeds the durable public clip URL); surface it so the reviewer can go
    # upstream to the real source. Generic mechanism, Newspapers-shaped for v1.
    seen = {link['url'] for link in external_links}
    for clip in ([] if shared_photo else _OFFSITE_RE.findall(html or '')):
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
        'body': '' if shared_photo else visible_text(page),
        'collection': collection,
        'terms': '; '.join(people) if people else '',
        'source_class': hints['source_class'] if hints else None,
    }
