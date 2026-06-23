"""FamilySearch recipe for `fha capture` (TOOLING §13b).

Recognizes a FamilySearch record or tree-person page and recovers the title,
the event date, the collection, and the persons named in the fact table.
Detection is by host (`familysearch.org`), with `og:site_name` as the fallback.
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

SOURCE_NAME = 'FamilySearch'
PRIORITY = 20


def detect(html: str, url: str | None) -> bool:
    if 'familysearch.org' in domain_of(url):
        return True
    page = parse_html(html)
    return (meta_content(page, 'og:site_name') or '').strip().lower().startswith('familysearch')


def extract(html: str, url: str | None) -> dict:
    page = parse_html(html)
    title = first_nonempty(
        meta_content(page, 'og:title'), page.h1, page.title,
    ) or 'FamilySearch record'
    # FamilySearch labels the collection in the description or a dedicated meta.
    collection = first_nonempty(
        meta_content(page, 'fs:collection', 'collection'),
        meta_content(page, 'og:description'),
        title,
    ) or title

    people: list[str] = []
    for rows in extract_tables(html):
        people = people_from_table(rows)
        if people:
            break
    # A tree-person page may have no fact table; the subject is the title/h1.
    if not people:
        subject = first_nonempty(page.h1, meta_content(page, 'og:title'))
        from capture_recipes._common import looks_like_name
        if subject and looks_like_name(subject):
            people = [subject]

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
    }
