"""West Virginia Archives recipe for `fha capture --ingest` (TOOLING §13b).

Recognizes a Vital Research Records image page on the West Virginia Archives'
free site (archive.wvculture.org/vrr/va_view2.aspx?FilmNumber=…&ImageNumber=…)
and files it as one record image: its film and image number, the citation a
stranger can follow back to that exact page, and the page's own address.

The image the page shows is the one the add-on saved (role `record`); this
recipe only reads the numbers from the address and the image's address from
the page's own <img>, so a capture filed by hand still gets the right title
and citation. It never builds an image URL (the padding and extension vary by
film). The browser add-on reads the same rule (browser-companion/src/lib/
wvculture-record.js); the two must agree.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from capture import _today, domain_of
from capture_recipes._common import record_hints

SOURCE_NAME = 'WVCulture'
PRIORITY = 35

_VIEW_PATH = '/vrr/va_view2.aspx'
_IMG_SRC_RE = re.compile(r'<img[^>]+src=["\'](https?://images\.wvculture\.org/\d+/\d+\.(?:gif|jpe?g))["\']',
                         re.IGNORECASE)
_REPOSITORY = 'West Virginia Archives and History'


def detect(html: str, url: str | None) -> bool:
    return 'wvculture.org' in domain_of(url)


def _numbers(url: str | None) -> tuple[str | None, str | None]:
    """The film and image numbers from a view-page address (query keys in any case)."""
    if not url:
        return None, None
    parts = urlsplit(url)
    if parts.path.lower() != _VIEW_PATH:
        return None, None
    query = {k.lower(): v for k, v in parse_qs(parts.query).items()}
    film = (query.get('filmnumber') or [None])[0]
    image = (query.get('imagenumber') or [None])[0]
    ok = (lambda v: v is not None and v.isdigit())
    return (film if ok(film) else None), (image if ok(image) else None)


def _image_src(html: str) -> str | None:
    m = _IMG_SRC_RE.search(html or '')
    if not m:
        return None
    return 'https://' + m.group(1).split('://', 1)[1]


def extract(html: str, url: str | None) -> dict:
    film, image = _numbers(url)
    image_src = _image_src(html)
    if film and image:
        title = f'West Virginia Vital Research Records, film {film}, image {image}'
        citation = (f'West Virginia Archives and History, Vital Research Records ({url} : accessed '
                    f'{_today()}), film {film}, image {image}.')
    else:
        # A page filed with no usable address: the title the page gave, and no
        # invented numbers.
        title = 'West Virginia Vital Research Records, image page'
        citation = f'West Virginia Archives and History, Vital Research Records ({url or "page"}).'
    hints = record_hints('West Virginia Vital Research Records', lambda word: None)
    body_lines = []
    if film and image:
        body_lines.append(f'Film {film}, image {image} of the Vital Research Records site.')
    if image_src:
        body_lines.append(f'Image address on the site: {image_src}')
    return {
        'title': title,
        'source_type': hints['source_type'],
        'source_class': hints['source_class'],
        'citation': citation,
        'repository': _REPOSITORY,
        'source_date': None,
        'external_links': [{'url': url}] if url else [],
        'people': [],
        'body': '\n\n'.join(body_lines),
        'collection': 'West Virginia Vital Research Records',
        'terms': '',
    }
