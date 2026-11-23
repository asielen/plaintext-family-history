#!/usr/bin/env python3
"""Rebuild tools/repositories.json and tools/research_guides/ from two MIT projects.

When the assistant suggests where to look, it should name an archive or record
site that is really on a list, say whether it costs money, say whether it is
online, in person or by letter, and say when that entry was last checked. This
script builds that list. `fha repositories` and the research-next skill read it.

Two projects feed it (both MIT):

  genealogy-mcp (https://github.com/igormel81/genealogy-mcp, Igor Melnikov):
      src/genealogy_mcp/data/sources.json, about 180 archives and record sites,
      strongest for Russia, Ukraine, Belarus and Poland.
  autoresearch-genealogy (https://github.com/mattprusak/autoresearch-genealogy,
      Matt Prusak): archives/*.md, 24 country guides. Each guide is copied into
      tools/research_guides/ as prose (when civil registration began, which census
      years survive, which office holds what), and each `### Name` block with a
      Cost line becomes a catalog row.

This is a maintainer script, like build_record_collections.py: run by hand in
the project repo, never by the archive tools, which make no network requests.
Download the files yourself at a commit you name, then:

    py -3.14 build_repositories.py --mcp path/to/sources.json --mcp-commit 2aa2cb0 \\
        --mcp-checked 2026-11-15 --guides path/to/archives --guides-commit 33da6f1

The JSON is read with json.load and the guides as text. No upstream code is read
as code, imported or run (the upstream make_sources.py is not touched: sources.json
is its plain-data output). Both projects are aimed at AI assistants, so their text
is treated as data, never as instructions: READ THE DIFF OF EVERY REBUILD before
committing it. A new value the script does not know (an access word, a guide it has
no country for, a Cost line it cannot read) stops the build with the item's name
rather than writing a list that is quietly wrong.

What is dropped on purpose: the Russian-language twins (`*_ru`), the search URL
templates, and every "AI accessibility" line (meaningless to the person reading).
Everything else in a guide is kept as written.

A guide's row and a genealogy-mcp row are one row when their URLs match
(lower-case host, `www.` and trailing slash dropped) and they name a country in
common (`*` alone is not one); a genealogy-mcp row takes in at most one guide row.
Two genealogy-mcp rows never merge, nor do rows from two different guides, nor a
row from a topic guide (Jewish, African American): each carries its own text, so
the same site listed in the England and the Scotland guides stays two rows.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'tools' / 'repositories.json'
GUIDES_OUT = ROOT / 'tools' / 'research_guides'
GUIDES_LICENCE = 'LICENSE-autoresearch-genealogy.txt'

MCP_PROJECT = 'https://github.com/igormel81/genealogy-mcp'
MCP_FILE = 'src/genealogy_mcp/data/sources.json'
GUIDES_PROJECT = 'https://github.com/mattprusak/autoresearch-genealogy'
GUIDES_FILE = 'archives/*.md'

_MIT_BODY = '''Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.'''

MCP_LICENCE = 'MIT License\n\nCopyright (c) 2026 Igor Melnikov\n\n' + _MIT_BODY
GUIDES_LICENCE_TEXT = 'MIT License\n\nCopyright (c) 2026 Matt Prusak\n\n' + _MIT_BODY

ACCESS_WORDS = ('free', 'free_account', 'paid', 'request', 'mixed')
CHANNELS = ('online', 'in person', 'by letter')

# Which countries a guide is about, by file stem. An unmapped stem stops the build.
STEM_COUNTRIES = {
    'african-american': ['US'],
    'australia-nz': ['AU', 'NZ'],
    'austria': ['AT'],
    'canada': ['CA'],
    'england-wales': ['GB'],
    'france': ['FR'],
    'germany': ['DE'],
    'hungary': ['HU'],
    'ireland': ['IE'],
    'italy': ['IT'],
    'jewish-genealogy': ['*'],
    'mexico-latin-america': ['MX'],
    'netherlands': ['NL'],
    'norway': ['NO'],
    'poland': ['PL', 'BY'],
    'russia-ukraine': ['RU', 'UA'],
    'scotland': ['GB'],
    'spain-portugal': ['ES', 'PT'],
    'sweden': ['SE'],
    'usa-census': ['US'],
    'usa-colonial': ['US'],
    'usa-immigration': ['US'],
    'usa-military': ['US'],
    'usa-vital-records': ['US'],
}
STEM_TOPICS = {'jewish-genealogy': ['jewish'], 'african-american': ['african-american']}
STEM_RECORDS = {
    'usa-census': ['census'],
    'usa-vital-records': ['vital'],
    'usa-military': ['military'],
    'usa-immigration': ['emigration'],
}
# Countries a Latin America row's heading can name, beyond Mexico.
_LATIN_NAMES = {
    'mexico': 'MX', 'mexican': 'MX', 'brazil': 'BR', 'brazilian': 'BR',
    'spain': 'ES', 'spanish': 'ES', 'portugal': 'PT', 'portuguese': 'PT',
    'argentina': 'AR', 'chile': 'CL', 'colombia': 'CO', 'peru': 'PE', 'cuba': 'CU',
    'venezuela': 'VE', 'uruguay': 'UY', 'guatemala': 'GT',
}
# A guide for a catalog row's country, when one guide is the obvious one.
_RECORD_GUIDE_US = {'census': 'usa-census', 'military': 'usa-military',
                    'emigration': 'usa-immigration', 'vital': 'usa-vital-records'}

# Upstream web addresses that no longer work or have moved to https, checked
# 2026-11-22: the upstream address and the one that answers now.
URL_FIXES = {
    'https://www.linc.tas.gov.au/': 'https://libraries.tas.gov.au/',
    'https://www.antenati.san.beniculturali.it/': 'https://antenati.cultura.gov.it',
    'https://www.gens.info/italia/': 'http://www.gens.info/italia/',
    'http://poznan-project.psnc.pl': 'https://poznan-project.psnc.pl',
}
# `###` headings that name a part of a section, not a site: the row takes the
# `##` section's heading instead ("Overview" under "NARA Access to Archival Databases").
_GENERIC_HEADINGS = {'overview', 'summary', 'about', 'details'}
# Text written to an AI assistant rather than to the person researching.
_AI_WORDS = re.compile(r'\b(?:Claude|ChatGPT|GPT(?:-\d+)?|LLMs?|AI)\b')

# Guide text keywords that add a record kind (heading and Coverage text).
_RECORD_KEYWORDS = (
    (r'\b(?:wills?|probate)\b', 'probate'),
    (r'\b(?:deeds?|land)\b', 'land'),
    (r'\b(?:parish|church|baptism|burial)', 'church'),
    (r'\bnewspapers?\b', 'newspapers'),
    (r'\b(?:cemeter|graves?\b)', 'graves'),
    (r'\bcensus', 'census'),
    (r'\b(?:births?|marriages?|deaths?|BMD|civil registration)\b', 'vital'),
)


class BuildError(ValueError):
    """The upstream data holds something this script does not know."""


def _slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-') or 'row'


def normalize_url(url: str) -> str:
    """Host and path of a URL, for telling two listings of one site apart."""
    u = re.sub(r'^[a-z]+://', '', (url or '').strip().lower())
    u = u.split('#', 1)[0].split('?', 1)[0]
    host, _, path = u.partition('/')
    if host.startswith('www.'):
        host = host[4:]
    return host + '/' + path.rstrip('/') if path.rstrip('/') else host


# ---- genealogy-mcp -------------------------------------------------------

def mcp_rows(entries: list, checked: str) -> list[dict]:
    """Catalog rows for genealogy-mcp's sources.json (a list of objects)."""
    if not isinstance(entries, list):
        raise BuildError('genealogy-mcp sources.json is not a list')
    out = []
    for e in entries:
        eid = e.get('id') if isinstance(e, dict) else None
        if not eid:
            raise BuildError(f'a genealogy-mcp entry has no id: {str(e)[:60]!r}')
        access = e.get('access')
        if access not in ACCESS_WORDS:
            raise BuildError(f'genealogy-mcp entry {eid!r}: unknown access {access!r}')
        years = e.get('years')
        if (e.get('coverage_known') is False or not isinstance(years, list) or len(years) != 2
                or years == [0, 9999]):
            years = None
        records = [str(r) for r in (e.get('records') or [])]
        row = {
            'id': f'mcp:{eid}',
            'name': e.get('name') or eid,
            'url': URL_FIXES.get(e.get('url') or '', e.get('url') or ''),
            'countries': [str(c) for c in (e.get('countries') or ['*'])],
        }
        if 'holocaust' in records:
            row['topics'] = ['jewish']
        row.update({
            'years': years,
            'records': records,
            'access': access,
            'channel': ['by letter'] if access == 'request' else ['online'],
            'about': e.get('about') or '',
            'how': e.get('how') or '',
            'from': ['genealogy-mcp'],
            'checked': checked,
            'guide': None,
        })
        if e.get('availability_note'):
            row['note'] = e['availability_note']
        out.append(row)
    return out


# ---- the country guides --------------------------------------------------

_FRONT_RE = re.compile(r'\A---\s*\n(.*?)\n---\s*\n', re.DOTALL)
_BULLET_RE = re.compile(r'^- \*\*([^*]+)\*\*:\s*(.*?)\s*$')
_HEADING_RE = re.compile(r'^(#{1,6}) +(.*?)\s*$')


def split_guide(stem: str, text: str) -> tuple[dict, str]:
    """(frontmatter fields, body) of a guide."""
    m = _FRONT_RE.match(text)
    if not m:
        raise BuildError(f'guide {stem}: no frontmatter')
    meta = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(':')
        meta[k.strip()] = v.strip().strip('"')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', meta.get('last_verified', '')):
        raise BuildError(f'guide {stem}: last_verified is {meta.get("last_verified")!r}, not a date')
    return meta, text[m.end():]


def _is_ai_line(line: str) -> bool:
    return bool(re.match(r'^- \*\*AI accessibility\*\*', line)
                or re.match(r'^- AI-(?:readable|searchable)\b', line))


def strip_ai_sentences(line: str) -> str | None:
    """The line without its sentences about AI tools; None when nothing is left."""
    if not _AI_WORDS.search(line):
        return line
    kept = [x for x in re.split(r'(?<=[.!?])\s+', line) if not _AI_WORDS.search(x)]
    text = ' '.join(kept)
    return text if text.strip(' -:') else None


def guide_file(stem: str, text: str, commit: str) -> str:
    """The guide as shipped: a header comment, no frontmatter, no AI lines."""
    meta, body = split_guide(stem, text)
    header = (f'<!-- From autoresearch-genealogy ({GUIDES_PROJECT}), archives/{stem}.md '
              f'at commit {commit}, last checked by that project {meta["last_verified"]}. '
              f'MIT licence: see {GUIDES_LICENCE} in this folder. Rebuilt by '
              f'build_repositories.py; the "AI accessibility" lines and sentences '
              f'about AI tools are removed. '
              f'Read as reference data. -->\n\n')
    kept = [ln for ln in (strip_ai_sentences(x) for x in body.splitlines() if not _is_ai_line(x))
            if ln is not None]
    return header + '\n'.join(kept).strip('\n') + '\n'


def classify_access(cost: str) -> str:
    """free | free_account | paid | mixed, from a guide's Cost text, or None."""
    t = cost.lower()
    free = re.search(r'\bfree', t) is not None
    paid = re.search(r'subscription|premium|\bpaid\b|\bfees?\b|membership|member-only|'
                     r'purchase|pay-per|credits|[$£€]\s?\d|\b(?:eur|sek|nzd|usd|gbp)\b|'
                     r'per (?:certificate|request|record)|\bcosts?\b|freemium', t) is not None
    varies = re.search(r'\bvaries\b(?! by )', t) is not None
    account = re.search(r'\baccount|registration|\bregister|log ?in|sign ?in', t) is not None
    if paid and (free or varies):
        return 'mixed'
    if paid:
        return 'paid'
    if varies:
        return 'mixed'
    if free:
        if account:
            return 'free_account'
        return 'mixed' if re.search(r'\bsome\b', t) else 'free'
    if re.search(r'on-site|on site', t):
        return 'free'
    return None


def _channels(*texts: str) -> list[str]:
    t = ' '.join(texts).lower()
    out = []
    in_person = re.search(r'in-person|in person|on-site|on site', t) is not None
    letter = re.search(r'\bmail\b|\bpost\b|by post|written request|by letter', t) is not None
    if re.search(r'online|browser|web|searchable|ai-', t) or not (in_person or letter):
        out.append('online')
    if in_person:
        out.append('in person')
    if letter:
        out.append('by letter')
    return out


def _records(stem: str, heading: str, coverage: str) -> list[str]:
    out = list(STEM_RECORDS.get(stem, []))
    for pat, kind in _RECORD_KEYWORDS:
        if kind not in out and re.search(pat, f'{heading} {coverage}', re.IGNORECASE):
            out.append(kind)
    return out


def guide_rows(stem: str, text: str) -> list[dict]:
    """Catalog rows for each `###` block of a guide that has a Cost line."""
    if stem not in STEM_COUNTRIES:
        raise BuildError(f'guide {stem!r} has no country in STEM_COUNTRIES; add it to '
                         f'build_repositories.py')
    meta, body = split_guide(stem, text)
    blocks, cur, section = [], None, ''
    for line in body.splitlines():
        h = _HEADING_RE.match(line)
        if h:
            heading = h.group(2)
            if len(h.group(1)) == 2:
                section = heading
            elif heading.lower() in _GENERIC_HEADINGS and section:
                heading = section
            cur = {'heading': heading, 'level': len(h.group(1)), 'lines': []} \
                if len(h.group(1)) in (3, 4) else None
            if cur:
                blocks.append(cur)
        elif cur is not None:
            cur['lines'].append(line)
    rows, seen = [], {}
    for blk in blocks:
        bullets = {}
        for ln in blk['lines']:
            m = _BULLET_RE.match(ln)
            if m and m.group(1) not in bullets:
                bullets[m.group(1)] = m.group(2)
        if 'Cost' not in bullets:
            continue
        heading = blk['heading']
        access = classify_access(bullets['Cost'])
        if access is None:
            raise BuildError(f'guide {stem}, "{heading}": cannot read the Cost line '
                             f'{bullets["Cost"]!r}; teach classify_access about it')
        # The address may sit inside text ("Part of Riksarkivet (https://...)");
        # a URL line with no address at all (an email, a town) is kept as the note.
        url_line = bullets.get('URL', '')
        found = re.search(r'https?://[^\s()<>\[\]]+', url_line)
        url = found.group(0).rstrip('.,;:') if found else ''
        url = URL_FIXES.get(url, url)
        coverage = bullets.get('Coverage') or next(
            (ln.strip() for ln in blk['lines'] if ln.strip() and not ln.lstrip().startswith(('-', '|', '#'))), '')
        countries = list(STEM_COUNTRIES[stem])
        if stem == 'ireland' and re.search(r'Northern Ireland|PRONI', ' '.join(blk['lines']) + heading):
            countries.append('GB')
        if stem == 'mexico-latin-america':
            for word in re.findall(r'[A-Za-z]+', heading):
                code = _LATIN_NAMES.get(word.lower())
                if code and code not in countries:
                    countries.append(code)
        slug = _slug(heading)
        seen[slug] = seen.get(slug, 0) + 1
        rid = f'ar:{stem}:{slug}' + (f'-{seen[slug]}' if seen[slug] > 1 else '')
        row = {'id': rid, 'name': heading, 'url': url, 'countries': countries}
        if stem in STEM_TOPICS:
            row['topics'] = list(STEM_TOPICS[stem])
        row.update({
            'years': None,
            'records': _records(stem, heading, coverage),
            'access': access,
            'channel': _channels(bullets.get('Searchable', ''), bullets.get('AI accessibility', ''),
                                 bullets['Cost']),
            'about': strip_ai_sentences(coverage) or '',
            'how': strip_ai_sentences(bullets.get('Research tip') or bullets.get('Note') or '') or '',
            'from': ['autoresearch-genealogy'],
            'checked': meta['last_verified'],
            'guide': stem,
        })
        if url_line.strip() and url_line.strip() != (found.group(0) if found else ''):
            row['note'] = url_line
        rows.append(row)
    return rows


# ---- putting it together -------------------------------------------------

def _shared_country(a: list, b: list) -> bool:
    return bool((set(a) & set(b)) - {'*'})


def _access(a: str, b: str) -> str:
    """One access word for two listings of one site: never 'free' when either costs."""
    if a == b:
        return a
    if {a, b} == {'free', 'free_account'}:
        return 'free_account'
    return 'mixed'


def _union(a: list, b: list, order: tuple = ()) -> list:
    out = list(a)
    out += [x for x in b if x not in out]
    return sorted(out, key=order.index) if order and all(x in order for x in out) else out


def merge_rows(rows: list[dict]) -> list[dict]:
    """One row where a guide and genealogy-mcp list one site for a shared country."""
    out, by_url, absorbed = [], {}, set()

    def joins(base: dict, row: dict) -> bool:
        if row['id'].startswith('mcp:') or row.get('topics'):
            return False
        if not _shared_country(base['countries'], row['countries']):
            return False
        if base['id'].startswith('mcp:'):
            return base['id'] not in absorbed
        return base['guide'] == row['guide']

    for row in rows:
        key = normalize_url(row['url']) if row['url'] else None
        base = None
        for cand in by_url.get(key, []) if key else []:
            if joins(cand, row):
                base = cand
                break
        if base is None:
            out.append(row)
            if key:
                by_url.setdefault(key, []).append(row)
            continue
        if base['id'].startswith('mcp:'):
            absorbed.add(base['id'])
            base['guide'] = row['guide']
        base['access'] = _access(base['access'], row['access'])
        base['from'] = _union(base['from'], row['from'])
        base['countries'] = ['*'] if '*' in base['countries'] else _union(base['countries'], row['countries'])
        base['records'] = _union(base['records'], row['records'])
        base['channel'] = _union(base['channel'], row['channel'], CHANNELS)
        if row.get('topics'):
            base['topics'] = _union(base.get('topics', []), row['topics'])
        if not base['how'] and row['how']:
            base['how'] = row['how']
        if not base['about'] and row['about']:
            base['about'] = row['about']
        base['checked'] = max(base['checked'], row['checked'])
    return out


def _guide_for(row: dict, stems_by_country: dict) -> str | None:
    """The guide to read next: the topic guide for a topic row, else the one guide
    for one of its countries. A worldwide row has none ('*' is not a country)."""
    if 'jewish' in row.get('topics', []) and 'jewish-genealogy' in stems_by_country.get('*', []):
        return 'jewish-genealogy'
    for c in row['countries']:
        if c == '*':
            continue
        stems = stems_by_country.get(c, [])
        if len(stems) == 1:
            return stems[0]
        if c == 'US':
            for kind in row['records']:
                if kind in _RECORD_GUIDE_US:
                    return _RECORD_GUIDE_US[kind]
    return None


def build(mcp_entries: list, mcp_commit: str, mcp_checked: str,
          guides: dict[str, str], guides_commit: str) -> tuple[dict, dict[str, str]]:
    """(the catalog, {guide stem: shipped guide text})."""
    rows = mcp_rows(mcp_entries, mcp_checked)
    shipped = {}
    for stem in sorted(guides):
        rows += guide_rows(stem, guides[stem])
        shipped[stem] = guide_file(stem, guides[stem], guides_commit)
    stems_by_country: dict[str, list] = {}
    for stem, cs in STEM_COUNTRIES.items():
        if stem in guides:
            for c in cs:
                stems_by_country.setdefault(c, []).append(stem)
    for row in rows:
        if row['id'].startswith('mcp:'):
            row['guide'] = _guide_for(row, stems_by_country)
    rows = merge_rows(rows)
    ids = [r['id'] for r in rows]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise BuildError(f'duplicate ids: {", ".join(dup)}')
    catalog = {
        'about': ('Archives and record sites that hold genealogical records, from genealogy-mcp and '
                  'autoresearch-genealogy. Rebuilt with build_repositories.py; do not edit by hand. '
                  'Entries were checked by those projects on the dates shown, not by this archive.'),
        'sources': [
            {'project': MCP_PROJECT, 'file': MCP_FILE, 'commit': mcp_commit,
             'checked': mcp_checked, 'licence': MCP_LICENCE},
            {'project': GUIDES_PROJECT, 'file': GUIDES_FILE, 'commit': guides_commit,
             'licence': GUIDES_LICENCE_TEXT},
        ],
        'repositories': rows,
    }
    return catalog, shipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--mcp', type=Path, required=True, help="genealogy-mcp's sources.json, downloaded")
    ap.add_argument('--mcp-commit', required=True, help='the genealogy-mcp commit it came from')
    ap.add_argument('--mcp-checked', required=True, metavar='YYYY-MM-DD',
                    help='the date of that commit (the date quoted as "listed as of")')
    ap.add_argument('--guides', type=Path, required=True, help="autoresearch-genealogy's archives/ folder, downloaded")
    ap.add_argument('--guides-commit', required=True, help='the autoresearch-genealogy commit it came from')
    args = ap.parse_args(argv)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', args.mcp_checked):
        print('ERROR: --mcp-checked must be YYYY-MM-DD', file=sys.stderr)
        return 2
    try:
        entries = json.loads(args.mcp.read_text(encoding='utf-8'))
        guides = {p.stem: p.read_text(encoding='utf-8')
                  for p in sorted(args.guides.glob('*.md')) if p.name != 'README.md'}
        if not guides:
            raise BuildError(f'no guides in {args.guides}')
        catalog, shipped = build(entries, args.mcp_commit, args.mcp_checked, guides, args.guides_commit)
    except (OSError, ValueError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 2
    OUT.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    GUIDES_OUT.mkdir(exist_ok=True)
    for stem, text in shipped.items():
        (GUIDES_OUT / f'{stem}.md').write_text(text, encoding='utf-8', newline='\n')
    (GUIDES_OUT / GUIDES_LICENCE).write_text(GUIDES_LICENCE_TEXT + '\n', encoding='utf-8', newline='\n')
    print(f'Wrote {OUT.relative_to(ROOT)}: {len(catalog["repositories"])} repositories, and '
          f'{len(shipped)} guides in {GUIDES_OUT.relative_to(ROOT)}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
