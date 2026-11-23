"""Internet Archive book recipe for `fha capture` (TOOLING §13b).

When a page of a county history or a family genealogy on archive.org reaches
the inbox without the add-on's own hints (a page saved by hand, or a capture
from a browser without the add-on), this recipe still files it as a book with a
citation that names the book and the link to the page, instead of as a generic
website. The add-on's reader (browser-companion/src/lib/archiveorg-record.js)
saves far more: the page image, its OCR text, the item's publisher and date,
and the printed page number. This recipe reads only what the page itself shows:
the tab title, which archive.org writes as "{title} : {author} : Free Download,
Borrow, and Streaming : Internet Archive".

What a page of a book says about itself (as the shared rule does for a
memorial): a printed book is `book`; a digitised copy of a published work is
`derivative`; and the date of the page copy is not the book's, so it gets no
`source_date` here (the add-on's hint supplies the publication year).
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from capture import _today, domain_of, meta_content, parse_html, visible_text

SOURCE_NAME = 'InternetArchive'
PRIORITY = 45

_DETAILS_RE = re.compile(r'/details/([A-Za-z0-9][A-Za-z0-9._-]*)(?:/page/([^/?#]+))?')
_TITLE_TAIL_RE = re.compile(r'\s*:\s*Free Download,? Borrow,? and Streaming\s*:\s*Internet Archive\s*$', re.I)


def _details(url: str | None) -> tuple[str, str | None] | None:
    """(item id, page segment) of an archive.org /details/ address, else None."""
    if not url:
        return None
    parts = urlsplit(url)
    if not re.search(r'(^|\.)archive\.org$', parts.hostname or '', re.I):
        return None
    m = _DETAILS_RE.match(unquote(parts.path))
    return (m.group(1), m.group(2)) if m else None


_MEDIATYPE_RE = re.compile(
    r'''<meta\b[^>]*\bproperty=["']mediatype["'][^>]*\bcontent=["']([^"']*)["']''', re.I)


def _is_text(html: str) -> bool:
    """The item page says it is a text (a book). A recording or a film says
    otherwise and is left to the generic recipe, never filed as a book. A page
    with no mediatype tag is read as a book."""
    m = _MEDIATYPE_RE.search(html or '')
    return m is None or m.group(1).strip().lower() == 'texts'


def detect(html: str, url: str | None) -> bool:
    return _details(url) is not None and 'archive.org' in domain_of(url) and _is_text(html)


def _creator(raw: str) -> str | None:
    """"[Mason, Jesse D] [from old catalog]" -> "Jesse D. Mason" (as the add-on's
    iaCleanCreator writes it)."""
    s = re.sub(r'\[from old catalog\]', ' ', raw or '', flags=re.I)
    s = re.sub(r'[\[\]]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    s = re.sub(r',?\s*\d{4}\??\s*-\s*(\d{4})?\??\.?$', '', s)
    s = s.strip(' .,;')
    if not s:
        return None
    bits = [b.strip() for b in s.split(',')]
    if len(bits) == 2 and all(bits) and not re.search(r'\b(inc|co|company|society|committee|association)\b', s, re.I):
        given = re.sub(r'\b([A-Z])\b(?!\.)', r'\1.', bits[1])
        s = f'{given} {bits[0]}'
    return s


def extract(html: str, url: str | None) -> dict:
    page = parse_html(html)
    item_id, segment = _details(url) or ('', None)
    raw_title = (meta_content(page, 'og:title') or page.title or '').strip()
    segments = [s.strip() for s in _TITLE_TAIL_RE.sub('', raw_title).split(' : ') if s.strip()]
    title = segments[0] if segments else (item_id or 'Internet Archive book')
    creator = _creator(segments[1]) if len(segments) > 1 else None
    link = f'https://archive.org/details/{item_id}' + (f'/page/{segment}' if segment else '') if item_id else (url or '')
    citation = (f'{creator}, ' if creator else '') + f'{title}; digital images, Internet Archive ({link} : accessed {_today()}).'
    body = f'Internet Archive item: {item_id}\n' + (f'Page: {segment}\n' if segment else '') + '\n' + visible_text(page)
    return {
        'title': title,
        'source_type': 'book',
        'source_class': 'derivative',
        'citation': citation,
        'repository': 'Internet Archive',
        'source_date': None,
        'external_links': [{'url': link}] if link else [],
        'people': [],
        'body': body.strip(),
        'collection': 'Internet Archive',
        'terms': '',
    }
