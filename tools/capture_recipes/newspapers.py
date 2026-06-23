"""Newspapers.com recipe for `fha capture` (TOOLING §13b).

Recognizes a Newspapers.com clipping/page and recovers the publication, date,
page, an article snippet, and a formatted citation. Detection is by host
(`newspapers.com`), with `og:site_name` as the fallback.
"""

from __future__ import annotations

import re

from capture import (
    domain_of,
    first_nonempty,
    meta_content,
    parse_html,
    visible_text,
)
from capture_recipes._common import harvest_date

SOURCE_NAME = 'Newspapers.com'
PRIORITY = 30

_PAGE_RE = re.compile(r'\bpage\s+(\d+)\b', re.I)


def detect(html: str, url: str | None) -> bool:
    if 'newspapers.com' in domain_of(url):
        return True
    page = parse_html(html)
    return (meta_content(page, 'og:site_name') or '').strip().lower().startswith('newspapers')


def extract(html: str, url: str | None) -> dict:
    page = parse_html(html)
    publication = first_nonempty(
        meta_content(page, 'article:publication', 'publication'),
        page.h1,
    )
    headline = first_nonempty(meta_content(page, 'og:title'), page.title)
    description = meta_content(page, 'og:description')

    source_date = harvest_date(
        meta_content(page, 'article:published_time'), description, headline, page.text,
    )
    page_no = None
    m = _PAGE_RE.search(' '.join(filter(None, [description, page.text[:200]])))
    if m:
        page_no = m.group(1)

    title = first_nonempty(headline, publication) or 'Newspapers.com clipping'

    citation_bits = []
    if publication:
        citation_bits.append(f'"{headline}," {publication}' if headline and headline != publication else publication)
    elif headline:
        citation_bits.append(f'"{headline}"')
    if source_date:
        citation_bits.append(source_date)
    if page_no:
        citation_bits.append(f'p. {page_no}')
    citation = ', '.join(citation_bits)
    citation = (citation + '. ' if citation else '') + 'Newspapers.com.'
    if url:
        citation += f' {url}'

    external_links = [{'url': url}] if url else []
    return {
        'title': title,
        'source_type': 'newspaper',
        'citation': citation,
        'repository': first_nonempty(publication, 'Newspapers.com'),
        'source_date': source_date,
        'external_links': external_links,
        'people': [],
        'body': first_nonempty(description, visible_text(page)) or '',
        'collection': publication or 'Newspapers.com',
        'terms': '',
    }
