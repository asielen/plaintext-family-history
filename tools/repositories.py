#!/usr/bin/env python3
"""
repositories.py - fha repositories: where the records are

  fha repositories [--place L-ID|TOWN] [--country NAME|CC] [--year YYYY]
                   [--record birth|death|...] [--topic jewish|african-american]
                   [--access free|paid|any] [--limit N] [--json] [--root PATH]

Answers "what holds death records for Fairview in 1911?" from the list of
archives and record sites that ships with the tools (TOOLING §7c). A
Kansas Territory birth and a State of Kansas death were made by different
governments, so the place's dated history (SPEC A.9) says which government
held the place in the year asked, and each repository is labelled as holding
records made by the government of the day, records kept where the place is
today, or both. Every line says what it costs and how to reach it, and every
entry is "(check this)": the archive did not check any of them.

Read-only. The index is needed only for --place.

CODE MAP
--------
  _country_codes          - a place name's last part -> catalog country codes
  _resolve_place          - L-id or name -> one registered place, or the choices
  run_repositories        - engine: filter and rank the catalog, return a Result
  _cmd_repositories       - render a Result as text or --json -> exit code
  register / _run_repositories / _standalone_main - CLI wiring
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    _CENSUS_STATE_NAMES,
    _CENSUS_STATE_NAMES_LEFT_OUT,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    Result,
    configure_utf8_stdout,
    id_type_of,
    is_valid_id,
    load_repositories,
    normalize_id,
    open_index_db,
    place_hierarchy_in_year,
    resolve_root_arg,
)

configure_utf8_stdout()

# ── country names ────────────────────────────────────────────────────────────

# English names and abbreviations people write as the last part of a place,
# for every country the catalog holds, and the governments that no longer
# exist (a Russian Empire birth is looked up under Russia, not Poland).
_COUNTRY_CODES: dict[str, tuple[str, ...]] = {}


def _add(codes: tuple[str, ...], *names: str) -> None:
    for n in names:
        _COUNTRY_CODES[n] = codes


_add(('US',), 'usa', 'us', 'u s a', 'u s', 'united states', 'united states of america',
     'america', 'dakota territory', 'indian territory', 'oregon territory',
     'orleans territory', 'washington territory', 'colorado territory',
     'nebraska territory', 'utah territory', 'new mexico territory',
     'arizona territory', 'idaho territory', 'montana territory',
     'wyoming territory', 'nevada territory', 'minnesota territory',
     'wisconsin territory', 'michigan territory', 'territory of hawaii')
_add(('GB',), 'england', 'wales', 'scotland', 'united kingdom', 'uk', 'great britain',
     'britain', 'northern ireland', 'england and wales', 'kingdom of great britain')
_add(('IE',), 'ireland', 'republic of ireland', 'irish free state', 'eire', 'éire')
_add(('CA',), 'canada', 'upper canada', 'lower canada', 'dominion of canada')
_add(('RU',), 'russia', 'russian empire', 'russian federation', 'soviet union',
     'ussr', 'u s s r', 'imperial russia', 'tsarist russia')
_add(('UA',), 'ukraine', 'ukrainian ssr')
_add(('BY',), 'belarus', 'byelorussia', 'belorussia', 'white russia')
_add(('PL',), 'poland', 'kingdom of poland', 'congress poland', 'congress kingdom',
     'polish republic', 'second polish republic', 'galicia')
_add(('DE',), 'germany', 'prussia', 'kingdom of prussia', 'german empire', 'deutschland',
     'bavaria', 'kingdom of bavaria', 'saxony', 'kingdom of saxony', 'wurttemberg',
     'württemberg', 'baden', 'hesse', 'hanover', 'kingdom of hanover',
     'west germany', 'east germany', 'german democratic republic')
_add(('AT', 'HU'), 'austria-hungary', 'austro-hungarian empire', 'austria hungary')
_add(('AT',), 'austria', 'austrian empire', 'empire of austria', 'bohemia')
_add(('HU',), 'hungary', 'kingdom of hungary')
_add(('CZ',), 'czech republic', 'czechia', 'czechoslovakia', 'moravia')
_add(('SK',), 'slovakia')
_add(('TR',), 'ottoman empire', 'turkey')
_add(('FR',), 'france')
_add(('IT',), 'italy', 'kingdom of italy', 'kingdom of sardinia', 'two sicilies')
_add(('ES',), 'spain')
_add(('PT',), 'portugal')
_add(('NL',), 'netherlands', 'the netherlands', 'holland')
_add(('BE',), 'belgium')
_add(('CH',), 'switzerland')
_add(('SE',), 'sweden')
_add(('NO',), 'norway')
_add(('DK',), 'denmark')
_add(('FI',), 'finland', 'grand duchy of finland')
_add(('EE',), 'estonia')
_add(('LV',), 'latvia')
_add(('LT',), 'lithuania')
_add(('MD',), 'moldova', 'bessarabia')
_add(('RO',), 'romania', 'rumania')
_add(('BG',), 'bulgaria')
_add(('RS',), 'serbia', 'yugoslavia')
_add(('HR',), 'croatia')
_add(('SI',), 'slovenia')
_add(('GE',), 'georgia republic', 'republic of georgia', 'sakartvelo')
_add(('KZ',), 'kazakhstan')
_add(('KG',), 'kyrgyzstan')
_add(('TJ',), 'tajikistan')
_add(('TM',), 'turkmenistan')
_add(('UZ',), 'uzbekistan')
_add(('AU',), 'australia')
_add(('NZ',), 'new zealand')
_add(('MX',), 'mexico')
_add(('BR',), 'brazil')
_add(('AR',), 'argentina')
_add(('CL',), 'chile')
_add(('CO',), 'colombia')
_add(('CR',), 'costa rica')
_add(('CU',), 'cuba')
_add(('EC',), 'ecuador')
_add(('GT',), 'guatemala')
_add(('PE',), 'peru')
_add(('PY',), 'paraguay')
_add(('UY',), 'uruguay')
_add(('VE',), 'venezuela')
_add(('BO',), 'bolivia')

_CODE_NAMES = {
    'US': 'the United States', 'GB': 'Great Britain', 'IE': 'Ireland', 'CA': 'Canada',
    'RU': 'Russia', 'UA': 'Ukraine', 'BY': 'Belarus', 'PL': 'Poland', 'DE': 'Germany',
    'AT': 'Austria', 'HU': 'Hungary', 'TR': 'Turkey',
}

# U.S. states and Canadian provinces stand for their country (the same list the
# census check uses). A state that is also a country is left out on purpose.
_STATE_CODES = {s: ('US' if country == 'United States' else 'CA')
                for country, states in _CENSUS_STATE_NAMES.items() for s in states}


def _norm(text: str) -> str:
    return unicodedata.normalize('NFC', text).lower().replace('.', '').strip()


def _country_codes(name: str) -> tuple[str, ...] | None:
    """Catalog country codes for one name as written, or None when it is not
    known (or is a name that means more than one place)."""
    norm = _norm(name)
    if not norm or norm in _STATE_CODES_LEFT_OUT:
        return None
    if norm in _COUNTRY_CODES:
        return _COUNTRY_CODES[norm]
    if norm in _STATE_CODES:
        return (_STATE_CODES[norm],)
    # A period state: "Kansas Territory".
    if norm.endswith(' territory') and norm[:-len(' territory')] in _STATE_CODES:
        return (_STATE_CODES[norm[:-len(' territory')]],)
    return None


_STATE_CODES_LEFT_OUT = set(_CENSUS_STATE_NAMES_LEFT_OUT) - {
    'united kingdom', 'uk', 'great britain', 'britain'}


def hierarchy_countries(hierarchy: str | None) -> tuple[tuple[str, ...] | None, str | None]:
    """(catalog codes, None) for a place's hierarchy, read from its last part
    (or the one before, for "...Poland, Congress Poland"-style tails); or
    (None, the last part) when no part is a name this tool knows."""
    parts = [p.strip() for p in (hierarchy or '').split(',') if p.strip()]
    for part in reversed(parts):
        codes = _country_codes(part)
        if codes:
            return codes, part
    return None, (parts[-1] if parts else None)


def _catalog_codes(rows: list[dict]) -> set[str]:
    return {c for r in rows for c in (r.get('countries') or []) if c != '*'}


def country_arg_codes(text: str, rows: list[dict]) -> tuple[str, ...] | None:
    """`--country NAME|CC`: a two-letter catalog code, or a name."""
    t = text.strip()
    if len(t) == 2 and t.upper() in _catalog_codes(rows):
        return (t.upper(),)
    return _country_codes(t)


# ── record kinds ─────────────────────────────────────────────────────────────

_RECORD_KINDS: dict[str, tuple[str, ...]] = {
    'birth': ('vital', 'church'),
    'baptism': ('vital', 'church'),
    'marriage': ('vital', 'church'),
    'death': ('vital', 'church', 'graves', 'newspapers'),
    'burial': ('graves', 'church'),
    'grave': ('graves', 'church'),
    'census': ('census', 'revision', 'household'),
    'military': ('military',),
    'immigration': ('emigration',),
    'probate': ('probate', 'books'),
    'land': ('land',),
    'newspaper': ('newspapers',),
    'church': ('church',),
}
_RECORD_TITLES = {
    'birth': 'Birth records', 'baptism': 'Baptism records', 'marriage': 'Marriage records',
    'death': 'Death records', 'burial': 'Burial records', 'grave': 'Grave records',
    'census': 'Census records', 'military': 'Military records',
    'immigration': 'Immigration records', 'probate': 'Probate records',
    'land': 'Land records', 'newspaper': 'Newspapers', 'church': 'Church records',
}
_TOPIC_GUIDES = {'jewish': 'jewish-genealogy', 'african-american': 'african-american'}

_ACCESS_RANK = {'free': 0, 'free_account': 1, 'mixed': 2, 'request': 3, 'paid': 4}
_ACCESS_WORDS = {
    'free': 'free', 'free_account': 'free with an account',
    'mixed': 'part free, part paid', 'request': 'by request', 'paid': 'paid',
}
_THEN_WORDS = 'records made by the government of the day'
_NOW_WORDS = 'where the records are likely kept today'


def _tools_prefix() -> str:
    """How the tools folder is written in this layout: `tools` in the project
    repo, `.fha/tools` in an installed archive. Never absolute."""
    return '.fha/tools' if Path(__file__).resolve().parent.parent.name == '.fha' else 'tools'


# ── places ───────────────────────────────────────────────────────────────────

def _present_hierarchy(conn, place_id: str) -> str | None:
    """The place's own `hierarchy:`, else the first one up its `within:` chain."""
    seen: set[str] = set()
    pid: str | None = place_id
    while pid and pid not in seen:
        seen.add(pid)
        row = conn.execute('SELECT hierarchy, within FROM places WHERE id = ?', (pid,)).fetchone()
        if row is None:
            return None
        if (row[0] or '').strip():
            return row[0].strip()
        pid = row[1]
    return None


def _resolve_place(conn, text: str) -> tuple[list[tuple[str, str]], bool]:
    """([(l-id, label)], by_id): the registered places a `--place` value names,
    by id or by name or alternate name (exact first, then containing the text)."""
    ref = normalize_id(text)
    if is_valid_id(ref) and id_type_of(ref) == 'L':
        row = conn.execute('SELECT id, name, hierarchy FROM places WHERE id = ?', (ref,)).fetchone()
        return ([(row[0], row[2] or row[1] or row[0])] if row else []), True
    needle = text.strip().lower()
    out: dict[str, str] = {}
    for sql, arg in (
        ('SELECT id, name, hierarchy FROM places WHERE lower(name) = ?', needle),
        ('SELECT p.id, p.name, p.hierarchy FROM place_names n JOIN places p ON p.id = n.place_id '
         'WHERE lower(n.alt_name) = ?', needle),
    ):
        for row in conn.execute(sql, (arg,)):
            out.setdefault(row[0], row[2] or row[1] or row[0])
    if not out:
        like = '%' + needle.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        for row in conn.execute(
                "SELECT id, name, hierarchy FROM places WHERE lower(name) LIKE ? ESCAPE '\\'", (like,)):
            out.setdefault(row[0], row[2] or row[1] or row[0])
    return list(out.items()), False


# ── engine ───────────────────────────────────────────────────────────────────

def _fail(code: int, error: str, text: str, **data) -> Result:
    r = Result(ok=False, exit_code=code, data={'error': error, **data})
    r.add('error', text)
    return r


def run_repositories(
    archive_root: Path | None, *, place: str | None = None, country: str | None = None,
    year: int | None = None, record: str | None = None, topic: str | None = None,
    access: str = 'any', limit: int = 10, catalog_path: Path | None = None,
) -> Result:
    """Filter and rank the repository list for a place or country, a year and a
    kind of record. Read-only; the Result's `data` is the whole answer."""
    rows = load_repositories(catalog_path)
    if rows is None:
        return _fail(EXIT_FAILURE, 'no-catalog',
                     'The list of repositories is missing or unreadable. Run `fha update-tools` '
                     'to restore it.')
    if place is None and country is None:
        return _fail(EXIT_ERRORS, 'no-where',
                     'Say where to look: `--place <l-id or town>` for a place in the archive, or '
                     '`--country "United States"` for one that is not registered yet.')
    if place is not None and country is not None:
        return _fail(EXIT_ERRORS, 'place-and-country',
                     '`--place` already says the country. Use one of `--place` or `--country`.')
    if record is not None and record not in _RECORD_KINDS:
        return _fail(EXIT_ERRORS, 'bad-record',
                     f'I do not know the record kind {record!r}. Choose one of: '
                     + ', '.join(_RECORD_KINDS) + '.')

    place_info: dict | None = None
    then_h = now_h = None
    period = None
    if place is not None:
        if archive_root is None:
            return _fail(EXIT_FAILURE, 'no-index', 'No archive found for `--place`. Run it inside the '
                                                   'archive, or give `--root`.')
        conn = open_index_db(archive_root, ('places', 'place_names', 'place_history'))
        if conn is None:
            return Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'no-index'})
        try:
            hits, by_id = _resolve_place(conn, place)
            if not hits:
                return _fail(EXIT_ERRORS, 'no-place',
                             f'No registered place matches {place!r}. Check with '
                             f'`fha find "{place}" --kind place`, or use `--country` instead.')
            if len(hits) > 1:
                lines = '; '.join(f'{label}: `fha repositories --place {pid}`' for pid, label in hits)
                return _fail(EXIT_ERRORS, 'ambiguous-place',
                             f'{len(hits)} registered places match {place!r}. Pick one: {lines}',
                             candidates=[pid for pid, _ in hits])
            pid, label = hits[0]
            now_h = _present_hierarchy(conn, pid)
            then_h, period = place_hierarchy_in_year(conn, pid, year)
        finally:
            conn.close()
        place_info = {'id': pid, 'label': label}
        if then_h is None:
            then_h = now_h
        if not now_h:
            return _fail(EXIT_ERRORS, 'no-hierarchy',
                         f'{label} has no `hierarchy:` in the registry, so I cannot tell its '
                         'country. Run it again with `--country`.')
        then_codes, then_bad = hierarchy_countries(then_h)
        now_codes, now_bad = hierarchy_countries(now_h)
        for codes, bad in ((then_codes, then_bad), (now_codes, now_bad)):
            if codes is None:
                return _fail(EXIT_ERRORS, 'unknown-country',
                             f"I don't know which country {bad!r} is. Run it again with "
                             "`--country`." if bad else
                             "I can't tell the country. Run it again with `--country`.")
    else:
        codes = country_arg_codes(country, rows)
        if codes is None:
            return _fail(EXIT_ERRORS, 'unknown-country',
                         f"I don't know which country {country!r} is. Try its English name, or its "
                         'two-letter code such as US, GB or PL.')
        then_codes = now_codes = codes

    kinds = _RECORD_KINDS.get(record) if record else None
    topic_guide = _TOPIC_GUIDES.get(topic) if topic else None
    if topic and topic_guide is None:
        return _fail(EXIT_ERRORS, 'bad-topic',
                     f'I do not know the topic {topic!r}. Choose jewish or african-american.')
    if access not in ('free', 'paid', 'any'):
        return _fail(EXIT_ERRORS, 'bad-access', '`--access` takes free, paid or any.')

    then_set, now_set = set(then_codes), set(now_codes)
    listed = []
    for r in rows:
        cs = [c for c in (r.get('countries') or []) if isinstance(c, str)]
        specific_then = bool(then_set & set(cs))
        specific_now = bool(now_set & set(cs))
        worldwide = '*' in cs
        if not (specific_then or specific_now or worldwide):
            continue
        guide = r.get('guide')
        if topic_guide:
            if guide != topic_guide:
                continue
        elif guide in _TOPIC_GUIDES.values():
            continue
        if kinds and not set(r.get('records') or []) & set(kinds):
            continue
        if access == 'free' and r.get('access') not in ('free', 'free_account'):
            continue
        if access == 'paid' and r.get('access') not in ('paid', 'mixed'):
            continue
        yrs = r.get('years')
        year_rank = 1
        if year is not None and isinstance(yrs, list) and len(yrs) == 2:
            if yrs[0] <= year <= yrs[1]:
                year_rank = 0
            else:
                continue
        serves = []
        if specific_then or worldwide:
            serves.append('then')
        if specific_now or worldwide:
            serves.append('now')
        channel = [c for c in (r.get('channel') or []) if isinstance(c, str)]
        row = dict(r)
        row['serves'] = serves
        row['access_words'] = _ACCESS_WORDS.get(r.get('access'), 'cost not listed')
        row['channel_words'] = ' or '.join(channel) if channel else 'how to reach it is not listed'
        row['_key'] = (0 if (specific_then or specific_now) else 1, year_rank,
                       _ACCESS_RANK.get(r.get('access'), 5), str(r.get('name') or ''))
        listed.append(row)
    listed.sort(key=lambda x: x['_key'])
    for row in listed:
        del row['_key']
    total = len(listed)
    listed = listed[:max(1, limit)]

    guides = []
    for row in listed:
        g = row.get('guide')
        if g and g not in guides and (Path(__file__).resolve().parent / 'research_guides' / f'{g}.md').exists():
            guides.append(g)

    data = {
        'place': place_info,
        'country': country,
        'year': year,
        'record': record,
        'then': {'hierarchy': then_h, 'period': period, 'countries': sorted(then_set)} if place else
                {'hierarchy': None, 'period': None, 'countries': sorted(then_set)},
        'now': {'hierarchy': now_h, 'countries': sorted(now_set)} if place else
               {'hierarchy': None, 'countries': sorted(now_set)},
        'repositories': listed,
        'total': total,
        'guides': guides,
        'guide_paths': [f'{_tools_prefix()}/research_guides/{g}.md' for g in guides],
    }
    if not listed:
        hint = []
        if record:
            hint.append('drop `--record`')
        if year is not None:
            hint.append('drop `--year`')
        if place is not None:
            hint.append('use `--country` for a wider list')
        r = Result(ok=False, exit_code=EXIT_WARNINGS, data={**data, 'error': 'nothing-matched'})
        r.add('warning', 'Nothing in the list matches. Try a broader question: '
                         + ', or '.join(hint or ['`--country`']) + '.')
        return r
    return Result(ok=True, exit_code=EXIT_CLEAN, data=data)


# ── output ───────────────────────────────────────────────────────────────────

def _cmd_repositories(result: Result, use_json: bool = False) -> int:
    """Render a Result as text or JSON and return its exit code. The only layer
    that prints."""
    if use_json:
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
        return result.exit_code
    d = result.data
    if not result.ok:
        for m in result.messages:
            print(f"{'ERROR' if m.level == 'error' else 'WARNING'}: {m.text}", file=sys.stderr)
        return result.exit_code

    title = _RECORD_TITLES.get(d['record'], 'Record sites')
    where = d['place']['label'] if d['place'] else (d['country'] or '')
    head = f'{title} for {where}'
    if d['year'] is not None:
        head += f" in {d['year']}"
    print(head)
    differ = set(d['then']['countries']) != set(d['now']['countries'])
    if d['place'] and d['year'] is not None:
        if d['then']['period']:
            print(f"In {d['year']} the place was: {d['then']['hierarchy']} (from its history, "
                  f"period {d['then']['period']})")
        else:
            print(f"In {d['year']} the place was: {d['then']['hierarchy']} (no dated history "
                  f"covers {d['year']}, so this is its present name)")
    if differ:
        print('The government then and the country today differ, so each line says which '
              'it is for.')
    print()
    for i, r in enumerate(d['repositories'], 1):
        print(f"{i:2}. {r.get('name')}: {r['access_words']}, {r['channel_words']}")
        if r.get('url'):
            print(f"    {r['url']}")
        text = r.get('how') or r.get('about')
        if text:
            print(f'    {text}')
        if r.get('note'):
            print(f"    Note: {r['note']}")
        if differ:
            both = 'then' in r['serves'] and 'now' in r['serves']
            words = (f'{_THEN_WORDS}, and {_NOW_WORDS}' if both
                     else _THEN_WORDS if 'then' in r['serves'] else _NOW_WORDS)
            print(f'    For: {words}')
        src = ' and '.join(r.get('from') or []) or 'an unnamed list'
        stamp = f" as of {r['checked']}" if r.get('checked') else ''
        print(f'    Listed by {src}{stamp} (check this)')
    if d['total'] > len(d['repositories']):
        print(f"\n{d['total'] - len(d['repositories'])} more match; `--limit {d['total']}` shows them all.")
    print()
    for p in d['guide_paths']:
        print(f'More on what records exist there: {p}')
    print('These come from lists other projects keep. Hours, fees and addresses change: '
          'check before you go or send money.')
    return result.exit_code


# ── CLI ──────────────────────────────────────────────────────────────────────

_CLI_DESCRIPTION = """\
Where are the records? Archives and record sites for a place, a year and a kind
of record, from the list that ships with the tools.

  fha repositories --place l-xxxxxxxxxx --year 1911 --record death
  fha repositories --place Fairview --year 1860 --record birth
  fha repositories --country "United States" --year 1880 --record census

A place with dated history is looked up under the government that held it in
that year and under the country it is in today; each line says which. Every
line says what it costs and how to reach it. Read-only."""


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('--place', metavar='L-ID|TOWN', help='A registered place, by l-id or name')
    p.add_argument('--country', metavar='NAME|CC',
                   help='A country by name or two-letter code, for a place not registered yet')
    p.add_argument('--year', type=int, metavar='YYYY', help='The year the record was made')
    p.add_argument('--record', metavar='KIND', choices=list(_RECORD_KINDS),
                   help='Kind of record: ' + ', '.join(_RECORD_KINDS))
    p.add_argument('--topic', choices=list(_TOPIC_GUIDES), help='A topic list: jewish or african-american')
    p.add_argument('--access', choices=('free', 'paid', 'any'), default='any',
                   help='free keeps free and free-with-account; paid keeps paid and part-paid')
    p.add_argument('--limit', type=int, default=10, metavar='N', help='How many to list (default 10)')
    p.add_argument('--json', action='store_true', dest='use_json', help='Machine-readable JSON output')
    p.add_argument('--root', metavar='PATH', help='Archive root (overrides auto-detection)')


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'repositories',
        help='Where are the records? Archives and record sites for a place, year and kind of record.',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_run_repositories)


def _run_repositories(args: argparse.Namespace) -> int:
    root = None
    if args.place is not None:
        root = resolve_root_arg(args)
        if root is None:
            return EXIT_FAILURE
    result = run_repositories(
        root, place=args.place, country=args.country, year=args.year, record=args.record,
        topic=args.topic, access=args.access, limit=args.limit)
    return _cmd_repositories(result, use_json=args.use_json)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha repositories', description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_arguments(parser)
    return _run_repositories(parser.parse_args(argv))


if __name__ == '__main__':
    sys.exit(_standalone_main())
