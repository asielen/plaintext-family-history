"""Ancestry recipe for `fha capture` (TOOLING §13b).

Recognizes an Ancestry record/image page and recovers the collection title, the
record date, the persons listed in the household/index table, and the record's
image URL. Detection is by host (`ancestry.*`), with `og:site_name` as the
fallback when the page is captured without a `--url`.
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
    page = parse_html(html)
    # Ancestry ships no og:title on most pages; the clean document.title is the
    # best title, and the first <h1> may be a tree-hint prompt (skip it).
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
    }
