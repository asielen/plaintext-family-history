"""FindAGrave recipe for `fha capture` (TOOLING §13b).

Recognizes a Find a Grave memorial and recovers what it says: the person, the
birth and death dates and places, the cemetery (as a place hint) and plot, the
inscription, the family members it links, the bio, and Find a Grave's own
citation. Detection is by host (`findagrave.com`), with `og:site_name` as the
fallback.

Two layouts:

- today's memorial page (2026), recognised by its `id="bio-name"` heading: the
  facts sit in labelled elements (`#birthDateLabel`, `#deathDateLabel`,
  `#cemeteryNameLabel`, `#plotValueLabel`, `#family-grid`, `#citationInfo`, …),
  read here exactly as the capture companion reads them
  (browser-companion/src/lib/findagrave-record.js);
- an older saved page with only a title and loose text: the lifespan from the
  title, the cemetery from the text.

What the memorial says about itself (TI-066): the shared `record_hints` rule
over collection "Find a Grave" - a website (a volunteer's compilation from the
stone, records and family knowledge, not a vital record), derivative, dated by
the memorial's own death. Find a Grave gives no burial date, and a burial is
known only to be on or after the death (TI-068), so none is made up; the
birth never dates a memorial.

The Seen It add-on's injected badges are stripped first (TI-067).
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
    date_to_edtf,
    looks_like_name,
    people_from_table,
    record_hints,
    strip_seen_it,
)

SOURCE_NAME = 'FindAGrave'
PRIORITY = 40

# "1842 – 1918" / "1842–1918" / "BIRTH 12 Mar 1842 … DEATH 4 Jan 1918"
_BIRTH_DEATH_RE = re.compile(
    r'(1[5-9]\d{2}|20\d{2})\s*[–\-]\s*(1[5-9]\d{2}|20\d{2})')

# A burial-place name: capitalized words ending in a recognized burial suffix.
# "Cemetery" alone missed "Greenwood Memorial Park" and grabbed the decoy
# "Ancestry Virtual Cemetery" (a virtual collection, not a burial place).
_CEMETERY_RE = re.compile(
    r"([A-Z][\w.'’]*(?:\s+[A-Z][\w.'’]*)*\s+"
    r"(?:Cemetery|Memorial Park|Memorial Gardens?|Burial Ground|Churchyard|Columbarium))")

# The place named immediately after a "Burial"/"Plot" label is the real one.
_BURIAL_LABEL_RE = re.compile(r'\b(?:Burial|Plot)\b[:\s]*(.{0,80})', re.I)

_AGED_RE = re.compile(r'\s*\(aged[^)]*\)\s*$', re.I)
_MEMORIAL_RE = re.compile(r'/memorial/(\d+)')

_SKIP_TAGS = frozenset({'script', 'style', 'noscript', 'template', 'button', 'svg'})
_BLOCK_TAGS = frozenset({
    'address', 'article', 'aside', 'blockquote', 'dd', 'div', 'dl', 'dt', 'figcaption', 'figure',
    'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav', 'ol',
    'p', 'pre', 'section', 'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'ul'})
_VOID_TAGS = frozenset({'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
                        'meta', 'param', 'source', 'track', 'wbr'})


def detect(html: str, url: str | None) -> bool:
    if 'findagrave.com' in domain_of(url):
        return True
    page = parse_html(html)
    site = (meta_content(page, 'og:site_name') or '').strip().lower()
    return site.startswith('find a grave') or site.startswith('findagrave')


# ── a small element tree, for today's layout ───────────────────────────────────

class _El:
    __slots__ = ('tag', 'attrs', 'children', 'parent')

    def __init__(self, tag: str, attrs: dict, parent: '_El | None') -> None:
        self.tag, self.attrs, self.children, self.parent = tag, attrs, [], parent


class _TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _El('#root', {}, None)
        self.cur = self.root

    def handle_starttag(self, tag: str, attrs: list) -> None:
        el = _El(tag, {k.lower(): (v or '') for k, v in attrs}, self.cur)
        self.cur.children.append(el)
        if tag not in _VOID_TAGS:
            self.cur = el

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.cur.children.append(_El(tag, {k.lower(): (v or '') for k, v in attrs}, self.cur))

    def handle_endtag(self, tag: str) -> None:
        n = self.cur
        while n is not None and n is not self.root:
            if n.tag == tag:
                self.cur = n.parent
                return
            n = n.parent

    def handle_data(self, data: str) -> None:
        self.cur.children.append(data)


def _tree(html: str) -> _El:
    p = _TreeParser()
    try:
        p.feed(html or '')
        p.close()
    except Exception:  # noqa: BLE001 - a pathological page: read what parsed
        pass
    return p.root


def _elements(root: _El, pred) -> list:
    out = []
    for c in root.children:
        if isinstance(c, _El):
            if pred(c):
                out.append(c)
            out.extend(_elements(c, pred))
    return out


def _text(node, skip=None) -> str:
    """An element's text as a reader sees it: a line per block, no scripts or buttons."""
    parts: list[str] = []

    def walk(n) -> None:
        if isinstance(n, str):
            parts.append(re.sub(r'\s+', ' ', n))
            return
        if n is skip or n.tag in _SKIP_TAGS:
            return
        if n.tag == 'br':
            parts.append('\n')
            return
        block = n.tag in _BLOCK_TAGS
        if block:
            parts.append('\n')
        for c in n.children:
            walk(c)
        if block:
            parts.append('\n')

    if node is not None:
        walk(node)
    lines = [re.sub(r'[ \t ]+', ' ', ln).strip() for ln in ''.join(parts).split('\n')]
    return '\n'.join(ln for ln in lines if ln)


def _classes(el: _El) -> list[str]:
    return el.attrs.get('class', '').split()


def memorial_from_html(html: str, url: str | None = None) -> dict | None:
    """What today's memorial page says, as the capture companion's
    fagMemorialFrom reads it; None on a page without the memorial layout.
    `url` is the memorial's address: a family link back to it is not family."""
    root = _tree(html)
    own = _MEMORIAL_RE.search(url or '')
    ids = {el.attrs['id']: el for el in _elements(root, lambda e: 'id' in e.attrs)}

    def by_id(x: str) -> str | None:
        return _text(ids.get(x)) or None

    name = by_id('bio-name')
    if not name:
        return None

    def date_of(x: str) -> str | None:
        v = by_id(x)
        return (_AGED_RE.sub('', v).strip() or None) if v else None

    def section(label: str) -> str | None:
        for h in _elements(root, lambda e: e.tag in ('h2', 'h3', 'h4')):
            if _text(h).lower() != label.lower():
                continue
            box = h.parent
            t = _text(box, skip=h)
            if not t and box is not None and box.parent is not None:
                after = box.parent.children[box.parent.children.index(box) + 1:]
                t = _text(next((n for n in after if isinstance(n, _El)), None))
            if t:
                return t[:4000]
        return None

    family = []
    grid = ids.get('family-grid')
    for ul in _elements(grid, lambda e: e.tag == 'ul' and 'member-family' in _classes(e)) if grid else []:
        relation = by_id(ul.attrs.get('aria-labelledby', '')) if ul.attrs.get('aria-labelledby') else None
        for a in _elements(ul, lambda e: e.tag == 'a' and '/memorial/' in e.attrs.get('href', '')):
            m = _MEMORIAL_RE.search(a.attrs['href'])
            if not m or (own and m.group(1) == own.group(1)) or any(f['memorial_id'] == m.group(1) for f in family):
                continue
            name_el = next(iter(_elements(a, lambda e: e.attrs.get('itemprop') == 'name')), None)
            life_el = next(iter(_elements(a, lambda e: 'life' in _classes(e))), None)
            family.append({'relation': relation, 'memorial_id': m.group(1),
                           'name': _text(name_el) or None, 'dates': _text(life_el) or None})

    burial_place = ', '.join(v for v in (
        by_id('cemeteryNameLabel'), by_id('cemeteryCityName'), by_id('cemeteryCountyName'),
        by_id('cemeteryStateName'), by_id('cemeteryCountryName')) if v)
    return {
        'name': name,
        'birth_date': date_of('birthDateLabel'), 'birth_place': by_id('birthLocationLabel'),
        'death_date': date_of('deathDateLabel'), 'death_place': by_id('deathLocationLabel'),
        'cemetery': by_id('cemeteryNameLabel'), 'burial_place': burial_place or None,
        'plot': by_id('plotValueLabel'),
        'inscription': section('Inscription'), 'gravesite_details': section('Gravesite Details'),
        'bio': by_id('fullBio') or by_id('partBio'),
        'family': family,
        'citation': by_id('citationInfo'),
    }


def _memorial_title(m: dict) -> str:
    """"{name} ({born}-{died}) - Find a Grave memorial", as the companion's fagTitle."""
    def year(d):
        y = re.search(r'(\d{4})', d or '')
        return y.group(1) if y else '?'
    life = [year(m['birth_date']), year(m['death_date'])]
    return m['name'] + (f' ({life[0]}-{life[1]})' if life != ['?', '?'] else '') + ' - Find a Grave memorial'


def _extract_memorial(m: dict, html: str, url: str | None) -> dict:
    hints = record_hints('Find a Grave', lambda word: date_to_edtf(m['death_date']) if word == 'Death' else None)
    people = [m['name']]
    for f in m['family']:
        if f['name'] and f['name'] not in people:
            people.append(f['name'])
    citation = m['citation'] or (f'{m["name"]}. Find a Grave memorial.' + (f' {url}' if url else ''))
    lines = []
    if m['burial_place']:
        lines.append(f'Cemetery (place_text hint): {m["burial_place"]}')
    for label, key in (('Birth', 'birth'), ('Death', 'death')):
        bits = [m[f'{key}_date'], m[f'{key}_place']]
        if any(bits):
            lines.append(f'{label}: ' + ', '.join(b for b in bits if b))
    if m['plot']:
        lines.append(f'Plot: {m["plot"]}')
    if m['inscription']:
        lines.append('Inscription: ' + m['inscription'].replace('\n', ' / '))
    if m['gravesite_details']:
        lines.append(f'Gravesite details: {m["gravesite_details"]}')
    for f in m['family']:
        lines.append(f'Family ({f["relation"] or "linked"}): {f["name"]}' + (f' ({f["dates"]})' if f['dates'] else ''))
    if m['bio']:
        lines.append(f'Bio: {m["bio"]}')
    body = '\n'.join(lines) + '\n\n' + visible_text(parse_html(html))
    return {
        'title': _memorial_title(m),
        'source_type': hints['source_type'],
        'source_class': hints['source_class'],
        'citation': citation,
        'repository': 'Find a Grave',
        'source_date': hints['source_date'],
        'external_links': [{'url': url}] if url else [],
        'people': people,
        'body': body.strip(),
        'collection': m['cemetery'] or 'Find a Grave',
        'terms': '',
    }


# ── an older saved page ────────────────────────────────────────────────────────

def _birth_death(text: str) -> tuple[str | None, str | None]:
    m = _BIRTH_DEATH_RE.search(text or '')
    return (m.group(1), m.group(2)) if m else (None, None)


def _cemetery(text: str) -> str | None:
    """The burial place, preferring the value next to a Burial/Plot label and
    never a "… Virtual Cemetery" (an online collection, not a grave)."""
    text = text or ''
    for label in _BURIAL_LABEL_RE.finditer(text):
        m = _CEMETERY_RE.search(label.group(1))
        if m and 'virtual' not in m.group(1).lower():
            return m.group(1).strip()
    for m in _CEMETERY_RE.finditer(text):
        if 'virtual' not in m.group(1).lower():
            return m.group(1).strip()
    return None


def extract(html: str, url: str | None) -> dict:
    html = strip_seen_it(html)
    memorial = memorial_from_html(html, url)
    if memorial:
        return _extract_memorial(memorial, html, url)

    page = parse_html(html)
    name = first_nonempty(meta_content(page, 'og:title'), page.h1, page.title) or 'Find a Grave memorial'
    description = first_nonempty(meta_content(page, 'og:description'), page.text) or ''

    # Find a Grave reliably puts the lifespan in the title/name, so search it
    # first; og:description and the page body are the fallbacks.
    birth, death = _birth_death(' '.join(filter(None, [
        name, meta_content(page, 'og:description'), page.text[:400],
    ])))

    cemetery = _cemetery(' '.join(filter(None, [description, page.text])))

    # The memorial subject, plus any family members in a linked table.
    people: list[str] = []
    subject = name.split('(')[0].strip()
    if looks_like_name(subject):
        people.append(subject)
    for rows in extract_tables(html):
        for member in people_from_table(rows):
            if member not in people:
                people.append(member)

    # The memorial is dated by its death, never by the birth (TI-066).
    hints = record_hints('Find a Grave', lambda word: death if word == 'Death' else None)
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
        'source_type': hints['source_type'],
        'source_class': hints['source_class'],
        'citation': citation,
        'repository': 'Find a Grave',
        'source_date': hints['source_date'],
        'external_links': external_links,
        'people': people,
        'body': body,
        'collection': cemetery or 'Find a Grave',
        'terms': '',
    }
