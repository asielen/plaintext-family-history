"""FindAGrave recipe for `fha capture` (TOOLING §13b).

Recognizes a Find a Grave memorial and recovers the memorial name, birth/death
years, the cemetery (as a place hint), and any family members the page links.
Detection is by host (`findagrave.com`), with `og:site_name` as the fallback.
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
from capture_recipes._common import looks_like_name, people_from_table

SOURCE_NAME = 'FindAGrave'
PRIORITY = 40

# "1842 – 1918" / "1842–1918" / "BIRTH 12 Mar 1842 … DEATH 4 Jan 1918"
_BIRTH_DEATH_RE = re.compile(
    r'(1[5-9]\d{2}|20\d{2})\s*[–\-]\s*(1[5-9]\d{2}|20\d{2})')
_CEMETERY_RE = re.compile(r'([A-Z][\w.\'’]*(?:\s+[A-Z][\w.\'’]*)*\s+Cemetery)')


def detect(html: str, url: str | None) -> bool:
    if 'findagrave.com' in domain_of(url):
        return True
    page = parse_html(html)
    site = (meta_content(page, 'og:site_name') or '').strip().lower()
    return site.startswith('find a grave') or site.startswith('findagrave')


def _birth_death(text: str) -> tuple[str | None, str | None]:
    m = _BIRTH_DEATH_RE.search(text or '')
    return (m.group(1), m.group(2)) if m else (None, None)


def extract(html: str, url: str | None) -> dict:
    page = parse_html(html)
    name = first_nonempty(meta_content(page, 'og:title'), page.h1, page.title) or 'Find a Grave memorial'
    description = first_nonempty(meta_content(page, 'og:description'), page.text) or ''

    birth, death = _birth_death(' '.join(filter(None, [
        meta_content(page, 'og:description'), page.text[:400],
    ])))

    cem_m = _CEMETERY_RE.search(' '.join(filter(None, [description, page.text])))
    cemetery = cem_m.group(1) if cem_m else None

    # The memorial subject, plus any family members in a linked table.
    people: list[str] = []
    subject = name.split('(')[0].strip()
    if looks_like_name(subject):
        people.append(subject)
    for rows in extract_tables(html):
        for member in people_from_table(rows):
            if member not in people:
                people.append(member)

    source_date = death or birth
    lifespan = f'{birth or "?"}–{death or "?"}' if (birth or death) else None

    citation_bits = [subject]
    if lifespan:
        citation_bits.append(lifespan)
    if cemetery:
        citation_bits.append(cemetery)
    citation = ', '.join(citation_bits) + '. Find a Grave memorial.'
    if url:
        citation += f' {url}'

    body_lines = []
    if cemetery:
        body_lines.append(f'Cemetery (place_text hint): {cemetery}')
    if lifespan:
        body_lines.append(f'Lifespan: {lifespan}')
    body_lines.append(visible_text(page))
    body = '\n\n'.join(b for b in body_lines if b)

    external_links = [{'url': url}] if url else []
    return {
        'title': name,
        'source_type': 'website',
        'citation': citation,
        'repository': 'Find a Grave',
        'source_date': source_date,
        'external_links': external_links,
        'people': people,
        'body': body,
        'collection': cemetery or 'Find a Grave',
        'terms': '',
    }
