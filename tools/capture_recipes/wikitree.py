"""WikiTree recipe for `fha capture` (TOOLING §13b).

A WikiTree profile filed from a saved page keeps its WikiTree ID, the day it
was last edited, the family it links (as WikiTree IDs) and its cited sources
as a list he can tick through: the linked ones with where they point, then
"To find:" with the ones that point nowhere. A free-space page
(/wiki/Space:…) is filed the same way, its title standing in for the person.

What the page shows (checked live on 2026-11-24, the shape the capture add-on
reads too, browser-companion/src/lib/wikitree-record.js): the person in the
<h1 itemprop="name">, the vitals in <time itemprop="birthDate|deathDate">
and itemprop birthPlace/deathPlace, the family as itemprop parent / spouse /
children links to /wiki/{ID}, the biography and its Sources in the
`.body-text` box (h2#Biography, h2#Sources), and the last edit in
<section id="Edits"><time datetime=…>.

A WikiTree profile is a compiled tree page written by its members: a website,
derivative whatever it cites, and undated (SPEC §14: `source_date` is the
date of the source itself, which the page does not give). Its id is in the
address, so `fha capture` notes it for the person (`site_person_ids`).
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urljoin, urlsplit

from capture import _today, domain_of, parse_html
from capture_recipes.findagrave import _elements, _El, _text, _tree

SOURCE_NAME = 'WikiTree'
PRIORITY = 36

_ORIGIN = 'https://www.wikitree.com'
_PROFILE_RE = re.compile(r'^(?:Profile:)?([^\W\d_][^:#?/]*-\d+)$')  # a letter first, in any alphabet (Åberg-55)
_SPACE_RE = re.compile(r'^Space:(.+)$', re.I)
# A sub-heading under Sources that holds thanks or research notes, not sources
# (the add-on's WT_NOT_SOURCES_RE).
_NOT_SOURCES_RE = re.compile(r'^(acknowledge?ments?|thanks|thank you|credits?|research notes?|see also|disclaimers?)\s*:?$', re.I)
_LABEL_RE = re.compile(r'^(see also|sources?|references?|notes?)\s*:?$', re.I)


def _page(url: str | None) -> dict | None:
    """{kind, key, url} for a profile or free-space address on WikiTree, else None."""
    if not url or domain_of(url) not in ('www.wikitree.com', 'wikitree.com'):
        return None
    m = re.match(r'^/wiki/([^/]+)/?$', urlsplit(url).path)
    if not m:
        return None
    name = unquote(m.group(1)).replace(' ', '_')
    space = _SPACE_RE.match(name)
    if space:
        return {'kind': 'space', 'key': 'Space:' + space.group(1), 'url': f'{_ORIGIN}/wiki/Space:{space.group(1)}'}
    prof = _PROFILE_RE.match(name)
    if prof:
        return {'kind': 'profile', 'key': prof.group(1), 'url': f'{_ORIGIN}/wiki/{prof.group(1)}'}
    return None


def _canonical(html: str) -> str | None:
    return parse_html(html).canonical or None


def detect(html: str, url: str | None) -> bool:
    return _page(url) is not None or (not url and _page(_canonical(html)) is not None)


def _attr(el: _El | None, name: str) -> str:
    return el.attrs.get(name, '') if el is not None else ''


def _first(root: _El, pred) -> _El | None:
    found = _elements(root, pred)
    return found[0] if found else None


def _hrefs(node: _El) -> list[str]:
    out = []
    for a in _elements(node, lambda e: e.tag == 'a' and e.attrs.get('href')):
        href = urljoin(_ORIGIN + '/', a.attrs['href'])
        if href.startswith('http') and not (_page(href) or {}).get('kind') == 'profile' and href not in out:
            out.append(href)
    return out


def _sources(root: _El) -> list[dict]:
    """The cited sources as the page shows them: footnotes first, then each
    paragraph or item under the Sources heading, one work once."""
    entries: list[dict] = []
    seen: set[str] = set()

    def add(node: _El) -> None:
        text = _text(node)
        if not text or _LABEL_RE.match(text):
            return
        key = re.sub(r'[.\s]+$', '', re.sub(r'\s+', ' ', re.sub(r'^\s*\d+\.\s*', '', text))).lower()
        if key in seen:
            return
        seen.add(key)
        links = [h for h in _hrefs(node) if not h.startswith(_ORIGIN + '/index.php')]
        entries.append({'text': text, 'links': links})

    for ol in _elements(root, lambda e: e.tag == 'ol' and 'references' in e.attrs.get('class', '').split()):
        for li in (c for c in ol.children if isinstance(c, _El) and c.tag == 'li'):
            ref = _first(li, lambda e: 'reference-text' in e.attrs.get('class', '').split())
            add(ref or li)
    head = _first(root, lambda e: e.tag in ('h2', 'h3') and re.match(r'^sources?$', _text(e), re.I))
    if head is not None and head.parent is not None:
        kids = [c for c in head.parent.children if isinstance(c, _El)]
        level = int(head.tag[1])
        skip = False
        for n in kids[kids.index(head) + 1:]:
            # A heading at the Sources level ends the list; a sub-heading of
            # thanks or research notes is skipped, other sub-headings continue it.
            if re.fullmatch(r'h[1-6]', n.tag):
                if int(n.tag[1]) <= level:
                    break
                skip = bool(_NOT_SOURCES_RE.match(_text(n).strip()))
                continue
            if skip:
                continue
            if n.tag == 'p':
                add(n)
            elif n.tag in ('ul', 'ol') and 'references' not in n.attrs.get('class', '').split():
                for li in (c for c in n.children if isinstance(c, _El) and c.tag == 'li'):
                    add(li)
    return entries


def _cited_sources(entries: list[dict]) -> str:
    """The "Cited sources" list the add-on writes: linked first, then "To find:"."""
    if not entries:
        return ''
    one_line = lambda t: re.sub(r'\s*\n\s*', ' ', t).strip()
    linked = [f'- [ ] {one_line(e["text"])} - ' + ' ; '.join(e['links']) for e in entries if e['links']]
    to_find = [f'- [ ] {one_line(e["text"])}' for e in entries if not e['links']]
    out = ['### Cited sources', '']
    if linked:
        out += ['Linked (open each and check it):', *linked, '']
    if to_find:
        out += ['To find:', *to_find, '']
    return '\n'.join(out).strip()


def extract(html: str, url: str | None) -> dict:
    page = _page(url) or _page(_canonical(html))
    root = _tree(html)
    h1 = _first(root, lambda e: e.tag == 'h1')
    name = (_text(h1).split('\n')[0] if h1 is not None else '') or (page['key'] if page else 'WikiTree page')
    edits = _first(root, lambda e: e.attrs.get('id') == 'Edits')
    edit_time = _first(edits, lambda e: e.tag == 'time') if edits is not None else None
    modified = _first(root, lambda e: e.attrs.get('itemprop') == 'dateModified')
    last_edited = (_attr(edit_time, 'datetime') or _attr(modified, 'content') or '').strip() or None
    page_url = page['url'] if page else url
    if page and page['kind'] == 'space':
        title = f'{name} - WikiTree free-space page'
        label = f'Space:{name}'
    else:
        wid = page['key'] if page else None
        title = f'{name} ({wid}) - WikiTree profile' if wid else f'{name} - WikiTree profile'
        label = f'{name} ({wid})' if wid else name
    citation = (f'WikiTree, "{label}," ({page_url} : ' + (f'last edited {last_edited}; ' if last_edited else '')
                + f'accessed {_today()}).')

    lines: list[str] = []
    people = [name] if page and page['kind'] == 'profile' else []
    if page and page['kind'] == 'profile':
        lines.append(f'WikiTree ID: {page["key"]}')
    if last_edited:
        lines.append(f'Last edited on WikiTree: {last_edited}')
    for label_word, date_prop, place_prop in (('Birth', 'birthDate', 'birthPlace'), ('Death', 'deathDate', 'deathPlace')):
        t = _first(root, lambda e, p=date_prop: e.attrs.get('itemprop') == p)
        pl = _first(root, lambda e, p=place_prop: e.attrs.get('itemprop') == p)
        bits = [b for b in (_attr(t, 'datetime') or (_text(t) if t is not None else ''),
                            _text(pl) if pl is not None else '') if b]
        if bits:
            lines.append(f'{label_word}: ' + ', '.join(bits))
    seen_kin: set[tuple[str, str]] = set()
    for el in _elements(root, lambda e: e.attrs.get('itemprop') in ('parent', 'spouse', 'children', 'child')):
        a = _first(el, lambda e: e.tag == 'a' and e.attrs.get('href'))
        kin = _page(urljoin(_ORIGIN + '/', a.attrs['href'])) if a is not None else None
        if not kin or kin['kind'] != 'profile':
            continue
        prop = el.attrs['itemprop']
        relation = ({'Father': 'father', 'Mother': 'mother'}.get(el.attrs.get('id', ''), 'parent') if prop == 'parent'
                    else 'spouse' if prop == 'spouse' else 'child')
        if (relation, kin['key']) in seen_kin:
            continue
        seen_kin.add((relation, kin['key']))
        kin_name = _text(a)
        lines.append(f'Family ({relation}): {kin_name} (WikiTree {kin["key"]})')
        if kin_name and kin_name not in people:
            people.append(kin_name)

    bio_head = _first(root, lambda e: e.tag in ('h2', 'h3') and e.attrs.get('id') == 'Biography')
    box = bio_head.parent if bio_head is not None else _first(
        root, lambda e: 'body-text' in e.attrs.get('class', '').split())
    bio = ''
    if box is not None:
        kids = [c for c in box.children if isinstance(c, _El)]
        start = kids.index(bio_head) + 1 if bio_head is not None else 0
        stop = next((i for i, k in enumerate(kids) if k.tag in ('h2', 'h3') and re.match(r'^sources?$', _text(k), re.I)),
                    len(kids))
        bio = '\n'.join(t for t in (_text(k) for k in kids[start:stop] if 'icon--edit' not in str(k.attrs)) if t)
    if bio:
        lines.append('Biography:\n' + bio)
    cited = _cited_sources(_sources(root))
    body = '\n\n'.join(b for b in ('\n'.join(lines), cited) if b)
    return {
        'title': title,
        'source_type': 'website',
        'source_class': 'derivative',
        'citation': citation,
        'repository': 'WikiTree',
        'source_date': None,
        'external_links': [{'url': page_url}] if page_url else [],
        'people': people,
        'body': body,
        'collection': 'WikiTree',
        'terms': '',
    }
