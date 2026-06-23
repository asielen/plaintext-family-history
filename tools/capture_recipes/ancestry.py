"""Ancestry recipe for `fha capture` (TOOLING §13b).

Recognizes an Ancestry record/image page and recovers the collection title, the
record date, the persons listed in the household/index table, and the record's
image URL. Detection is by host (`ancestry.*`), with `og:site_name` as the
fallback when the page is captured without a `--url`.
"""

from __future__ import annotations

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
    people_from_table,
    source_type_from_text,
)

SOURCE_NAME = 'Ancestry'
PRIORITY = 10


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
    title = first_nonempty(
        meta_content(page, 'og:title'), page.h1, page.title,
    ) or 'Ancestry record'
    collection = first_nonempty(
        meta_content(page, 'og:title'), page.h1,
    ) or title

    # The first table with named first-column rows is the household/index table.
    people: list[str] = []
    for rows in extract_tables(html):
        people = people_from_table(rows)
        if people:
            break

    source_date = harvest_date(collection, title, meta_content(page, 'og:description'))
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
