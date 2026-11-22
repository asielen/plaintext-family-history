#!/usr/bin/env python3
"""Rebuild tools/record_collections.json from WikiTree Sourcer's collection map.

The same record set often sits on several sites under different collection
numbers: the 1850 US census is Ancestry collection 8054 and FamilySearch
collection 1401638. WikiTree Sourcer (https://github.com/RobPavey/wikitree-sourcer,
MIT licence, Robert M. Pavey) keeps a map of about 135 such collections in
`extension/base/core/record_collections.mjs`. `fha lint` W162 reads our copy of
that map (`_lib.record_collection_group`) to tell when two filed sources are
one record saved from two sites.

This is a maintainer script, like build_commands_doc.py: it is run by hand in
the project repo when Sourcer's map changes, never by the archive tools, which
make no network requests. Download the file yourself, then:

    py -3.14 build_record_collections.py path/to/record_collections.mjs --commit <sha>

The file is read as text and never run. Only the `RecordCollectionData` array
is read, by a small reader for the literal forms that array uses: objects,
arrays, double- or single-quoted strings, numbers, true/false/null, and
comments. A bare name (one of Sourcer's own constants, such as a census's
column list) reads as null; nothing we keep uses one. Anything else stops the
build with the line it met, rather than writing a map that is quietly wrong.

What is kept, per collection: Sourcer's id (`wtsId`), title, country, the
year of a census, `partOf` (the larger collection a site's part belongs to),
and each site's collection id (and `altId`). Sourcer's search settings and
column lists are left out. The MIT notice and the commit travel in the file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'tools' / 'record_collections.json'

MIT_NOTICE = '''MIT License

Copyright (c) 2020 Robert M Pavey

Permission is hereby granted, free of charge, to any person obtaining a copy
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

_TOKEN_RE = re.compile(r'''
    (?P<ws>\s+)
  | (?P<line_comment>//[^\n]*)
  | (?P<block_comment>/\*.*?\*/)
  | (?P<string>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<name>[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)
  | (?P<punct>[{}\[\],:])
''', re.VERBOSE | re.DOTALL)


class ReadError(ValueError):
    """The map holds a form this reader does not know."""


def tokens(text: str) -> list[tuple[str, str, int]]:
    """(kind, text, line) for each token of the literal `text` starts with;
    whitespace and comments dropped. Stops where that literal closes, so
    whatever code follows it is never read."""
    out = []
    pos = 0
    line = 1
    depth = 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ReadError(f'line {line}: cannot read {text[pos:pos + 30]!r}')
        kind = m.lastgroup
        if kind not in ('ws', 'line_comment', 'block_comment'):
            out.append((kind, m.group(), line))
            if kind == 'punct' and m.group() in '[{':
                depth += 1
            elif kind == 'punct' and m.group() in ']}':
                depth -= 1
                if depth == 0:
                    return out
        line += m.group().count('\n')
        pos = m.end()
    raise ReadError(f'line {line}: the collection list never closes')


def _string(raw: str) -> str:
    """A JavaScript string literal's text (the escapes JSON shares)."""
    body = raw[1:-1]
    if raw[0] == "'":
        body = body.replace('\\\'', "'").replace('"', '\\"')
    try:
        return json.loads('"' + body + '"')
    except ValueError as e:
        raise ReadError(f'a string this reader cannot decode: {raw[:40]!r} ({e})') from None


def parse_value(toks: list, i: int):
    """The literal starting at toks[i]: (value, next index)."""
    kind, text, line = toks[i]
    if text == '{':
        obj = {}
        i += 1
        while toks[i][1] != '}':
            kkind, key, kline = toks[i]
            if kkind == 'string':
                key = _string(key)
            elif kkind != 'name' and kkind != 'number':
                raise ReadError(f'line {kline}: expected a key, found {key!r}')
            if toks[i + 1][1] != ':':
                raise ReadError(f'line {kline}: expected ":" after {key!r}')
            obj[key], i = parse_value(toks, i + 2)
            if toks[i][1] == ',':
                i += 1
            elif toks[i][1] != '}':
                raise ReadError(f'line {toks[i][2]}: expected "," or "}}", found {toks[i][1]!r}')
        return obj, i + 1
    if text == '[':
        arr = []
        i += 1
        while toks[i][1] != ']':
            v, i = parse_value(toks, i)
            arr.append(v)
            if toks[i][1] == ',':
                i += 1
            elif toks[i][1] != ']':
                raise ReadError(f'line {toks[i][2]}: expected "," or "]", found {toks[i][1]!r}')
        return arr, i + 1
    if kind == 'string':
        return _string(text), i + 1
    if kind == 'number':
        return (float(text) if '.' in text else int(text)), i + 1
    if kind == 'name':
        return {'true': True, 'false': False, 'null': None}.get(text), i + 1
    raise ReadError(f'line {line}: unexpected {text!r}')


def read_collections(text: str) -> list[dict]:
    """Sourcer's RecordCollectionData array, as Python values."""
    m = re.search(r'\bconst\s+RecordCollectionData\s*=\s*\[', text)
    if not m:
        raise ReadError('no "const RecordCollectionData = [" in this file')
    toks = tokens(text[m.end() - 1:])
    value, _ = parse_value(toks, 0)
    return value


def slim(entries: list[dict]) -> list[dict]:
    """What W162 needs of each collection, in Sourcer's order."""
    out = []
    for e in entries:
        if not isinstance(e, dict) or not e.get('wtsId'):
            continue
        sites = {}
        for site, info in (e.get('sites') or {}).items():
            if not isinstance(info, dict):
                continue
            ids = [str(info[k]) for k in ('id', 'altId') if info.get(k) not in (None, '')]
            if ids:
                sites[site] = ids
        row = {'id': e['wtsId'], 'title': e.get('title') or '', 'country': e.get('country') or ''}
        year = (e.get('dates') or {}).get('year') if isinstance(e.get('dates'), dict) else None
        if isinstance(year, int):
            row['year'] = year
        part = e.get('partOf')
        if part:
            if not isinstance(part, list):
                raise ReadError(f'{e["wtsId"]}: partOf is {part!r}, not a list of collection ids')
            row['partOf'] = [str(p) for p in part]
        row['sites'] = sites
        out.append(row)
    return out


def build(text: str, commit: str) -> dict:
    return {
        'about': ('Record collections that are one record set on several sites, from WikiTree '
                  'Sourcer\'s record_collections.mjs. Rebuilt with build_record_collections.py; '
                  'do not edit by hand.'),
        'source': {
            'project': 'https://github.com/RobPavey/wikitree-sourcer',
            'file': 'extension/base/core/record_collections.mjs',
            'commit': commit,
        },
        'licence': MIT_NOTICE,
        'collections': slim(read_collections(text)),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('mjs', type=Path, help="Sourcer's record_collections.mjs, downloaded")
    ap.add_argument('--commit', required=True, help='the Sourcer commit the file came from')
    args = ap.parse_args(argv)
    try:
        data = build(args.mjs.read_text(encoding='utf-8'), args.commit)
    except (OSError, ReadError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return 2
    OUT.write_text(json.dumps(data, indent=1, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')
    print(f'Wrote {OUT.relative_to(ROOT)}: {len(data["collections"])} collections from commit {args.commit}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
