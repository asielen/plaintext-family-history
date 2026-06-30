"""Newspapers.com recipe for `fha capture` (TOOLING §13b).

Recognizes a Newspapers.com clipping/page and recovers the publication, date,
page, an article snippet, and a formatted citation. Detection is by host
(`newspapers.com`), with `og:site_name` as the fallback.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime

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

# A full "Mon DD, YYYY" date phrase, which og:description reliably ships.
_FULLDATE_RE = re.compile(r'\b([A-Za-z]{3,9})\s+(\d{1,2}),\s+(\d{4})\b')

# Newspapers.com's standard page-title prefix ("Aug 07, 1884, page 3 - …") is a
# navigation label, not an article headline, so it must not be quoted into the
# citation as if it were one.
_NAV_TITLE_RE = re.compile(r'^[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4},\s+page\s+\d+', re.I)


def detect(html: str, url: str | None) -> bool:
    if 'newspapers.com' in domain_of(url):
        return True
    page = parse_html(html)
    return (meta_content(page, 'og:site_name') or '').strip().lower().startswith('newspapers')


def _parse_full_date(text: str | None) -> tuple[str | None, str | None]:
    """`(iso, 'D Mon YYYY')` from a "Mon DD, YYYY" phrase, else `(None, None)`.

    The first half feeds `source_date` (ISO/EDTF, what the archive reads); the
    second is the human-readable form for the citation string (`7 Aug 1884`).
    """
    if not text:
        return None, None
    m = _FULLDATE_RE.search(text)
    if not m:
        return None, None
    for fmt in ('%b %d %Y', '%B %d %Y'):
        try:
            d = datetime.strptime(f'{m.group(1)} {m.group(2)} {m.group(3)}', fmt).date()
        except ValueError:
            continue
        return d.isoformat(), f'{d.day} {d.strftime("%b")} {d.year}'
    return None, None


def _clip_id(url: str | None) -> str | None:
    """The Newspapers.com clipping id from a `clipping_id=` param or `/clip/<id>/`."""
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    if qs.get('clipping_id'):
        return qs['clipping_id'][0]
    m = re.search(r'/clip/(\d+)', parsed.path)
    return m.group(1) if m else None


def extract(html: str, url: str | None) -> dict:
    page = parse_html(html)
    publication = first_nonempty(
        meta_content(page, 'article:publication', 'publication'),
        page.h1,
    )
    headline = first_nonempty(meta_content(page, 'og:title'), page.title)
    description = meta_content(page, 'og:description')

    # Prefer a full date parsed from og:description; fall back to year-only.
    source_date, cite_date = _parse_full_date(description)
    if not source_date:
        source_date = harvest_date(
            meta_content(page, 'article:published_time'), description, headline, page.text,
        )
        cite_date = source_date

    page_no = None
    m = _PAGE_RE.search(' '.join(filter(None, [description, page.text[:200]])))
    if m:
        page_no = m.group(1)

    title = first_nonempty(headline, publication) or 'Newspapers.com clipping'

    # The page-title is a nav label, not a headline - don't quote it into the
    # citation; a real (non-nav) og:title still earns the quoted-headline prefix.
    is_nav_title = bool(headline and _NAV_TITLE_RE.match(headline.strip()))
    citation_bits = []
    if publication:
        if headline and headline != publication and not is_nav_title:
            citation_bits.append(f'"{headline}," {publication}')
        else:
            citation_bits.append(publication)
    elif headline and not is_nav_title:
        citation_bits.append(f'"{headline}"')
    if cite_date:
        citation_bits.append(cite_date)
    if page_no:
        citation_bits.append(f'p. {page_no}')
    citation = ', '.join(citation_bits)
    citation = (citation + '. ' if citation else '') + 'Newspapers.com.'
    if url:
        citation += f' {url}'

    external_links = [{'url': url}] if url else []
    clip_id = _clip_id(url)
    if clip_id:
        clip_url = f'https://www.newspapers.com/clip/{clip_id}/'
        if not any(link['url'] == clip_url for link in external_links):
            external_links.append({'url': clip_url, 'label': 'Newspapers.com clip'})
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
