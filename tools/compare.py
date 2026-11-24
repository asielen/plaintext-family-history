#!/usr/bin/env python3
"""
compare.py - fha compare: every record about one fact, side by side.

  fha compare <p-id | "Full Name"> [--fact birth|death|marriage|parents|name|residence|<claim type>]
              [--accepted-only] [--include-rejected] [--json] [--root PATH]

When two records give a person two different birth years, the researcher
needs one table that puts them next to each other: what each says, when it was
written down, who told it, whether that person was there, and which of the
records are really copies of one another (TOOLING §7d). Without `--fact` it
lists the facts of that person whose records disagree, so he can pick one.

A pure read over `.cache/index.sqlite` plus the frontmatter of the few sources
it shows; nothing is written (SPEC §17b: what a tool can compute is never
stored). Which claims belong to a person's own fact is `_lib.vital_subjects`,
so a son's birth never lands on his mother.

CODE MAP
--------
  _fact_claims        - the claims that are about this person's fact
  _rows               - claims -> numbered rows with every field the table shows
  _same_origin        - which rows are one origin (one source, one record on two sites, someone's tree)
  _disagree           - do two rows' dates or places rule each other out
  run_compare         - engine: returns a Result
  _cmd_compare        - render a Result as text or --json -> exit code
  register / _run_compare / _standalone_main - CLI wiring
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    CLAIM_ROLE_SEP,
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    Result,
    claim_role_pairs,
    configure_utf8_stdout,
    fmt_id_display,
    humanize_edtf,
    id_type_of,
    index_merge_survivors,
    is_valid_id,
    load_fha_yaml,
    normalize_id,
    open_index_db,
    person_name_refusal,
    persons_named,
    read_record,
    record_collection_group,
    record_collection_index,
    resolve_root_arg,
    same_record_subject,
    site_data_reader,
    source_record_subjects,
    source_dates_agree,
    source_site_collection,
    vital_subjects,
)

configure_utf8_stdout()

_REQUIRED_TABLES = ('persons', 'claims', 'claim_persons', 'claim_links', 'sources',
                    'source_people', 'aliases')

# --fact word -> the claim types it gathers.
_FACT_TYPES = {
    'birth': ('birth', 'baptism'),
    'death': ('death', 'burial'),
    'marriage': ('marriage',),
    'parents': ('relationship', 'birth', 'baptism'),
    'name': ('name',),
}
# The facts offered when none is named, in reading order.
_FACT_ORDER = ('birth', 'death', 'marriage', 'parents', 'name', 'residence')
_FACT_LABEL = {'birth': 'Birth', 'death': 'Death', 'marriage': 'Marriage',
               'parents': 'Parents', 'name': 'Name', 'residence': 'Residence'}

_INFORMATION_TEXT = {'primary': 'firsthand', 'secondary': 'secondhand'}
_EVIDENCE_TEXT = {'direct': 'It says it outright', 'indirect': 'It implies it',
                  'negative': 'It is a conclusion from absence'}
_ONE_ANSWER = ('birth', 'death', 'marriage')
_DEFAULT_STATUSES = ('accepted', 'suggested', 'disputed')
_UNDATED = ('0001-01-01', '9999-12-31')


def _year(iso: str | None) -> int | None:
    m = re.match(r'^(\d{4})', str(iso or ''))
    return int(m.group(1)) if m else None


def _dated(date_min, date_max) -> bool:
    return bool(date_min) and bool(date_max) and (date_min, date_max) != _UNDATED


def _closed(date_min, date_max) -> bool:
    """Both ends are real dates: neither is the index's open-ended stand-in."""
    return (bool(date_min) and bool(date_max)
            and date_min != _UNDATED[0] and date_max != _UNDATED[1])


def _fact_of_type(claim_type: str) -> str:
    for fact in ('birth', 'death', 'marriage', 'name'):
        if claim_type in _FACT_TYPES[fact]:
            return fact
    return claim_type


def _claim_people(conn, claim_id: str) -> list[tuple[str, str | None]]:
    rows = conn.execute(
        'SELECT person_id, role FROM claim_persons WHERE claim_id=? ORDER BY position',
        (claim_id,)).fetchall()
    return claim_role_pairs([(r[0], r[1]) for r in rows])


def _about(pid: str, fact: str, claim_type: str, pairs) -> bool:
    """Is this claim about `pid`'s own `fact`? The roles decide (SPEC §8.3)."""
    named = {p for p, _ in pairs}
    if pid not in named:
        return False
    roles = {p: {r for q, r in pairs if q == p and r} for p in named}
    if fact in ('birth', 'death', 'marriage'):
        subjects = vital_subjects(claim_type, pairs)
        return pid in named if subjects is None else pid in subjects
    if fact == 'parents':
        if claim_type == 'relationship':
            return 'child' in roles[pid]
        subjects = vital_subjects(claim_type, pairs)
        in_birth = pid in named if subjects is None else pid in subjects
        return in_birth and any('parent' in r for r in roles.values())
    # name and every other claim type: the subject, else everyone the claim names.
    if any('subject' in r for r in roles.values()):
        return 'subject' in roles[pid]
    return True


def _fact_claims(conn, pid: str, fact: str, statuses: tuple[str, ...]) -> list[dict]:
    types = _FACT_TYPES.get(fact, (fact,))
    marks = ','.join('?' * len(types))
    smarks = ','.join('?' * len(statuses))
    rows = conn.execute(
        f'SELECT c.* FROM claims c JOIN claim_persons cp ON cp.claim_id=c.id '
        f'WHERE cp.person_id=? AND c.type IN ({marks}) AND c.status IN ({smarks}) '
        f'GROUP BY c.id', (pid, *types, *statuses)).fetchall()
    out = []
    for r in rows:
        if _about(pid, fact, r['type'], _claim_people(conn, r['id'])):
            out.append(dict(r))
    out.sort(key=lambda c: (c['date_min'] or '9999', c['source_id'], c['id']))
    return out


def _available_facts(conn, pid: str, statuses: tuple[str, ...]) -> list[str]:
    """Every fact this person has records for: the usual ones in reading order,
    then any other claim type they appear in (occupation, immigration, ...)."""
    smarks = ','.join('?' * len(statuses))
    types = [r[0] for r in conn.execute(
        f'SELECT DISTINCT c.type FROM claims c JOIN claim_persons cp ON cp.claim_id=c.id '
        f'WHERE cp.person_id=? AND c.status IN ({smarks}) ORDER BY c.type', (pid, *statuses))]
    candidates = list(_FACT_ORDER)
    for t in types:
        f = _fact_of_type(t)
        if f not in candidates:
            candidates.append(f)
    return [k for k in candidates if _fact_claims(conn, pid, k, statuses)]


def _recorded_after(src: dict, claim: dict) -> dict | None:
    """Years between the event and the record of it, as a range; None when
    either has no date."""
    # An open end ("before 1840", "after 1880") has no year to subtract.
    if not (_closed(src.get('date_min'), src.get('date_max'))
            and _closed(claim.get('date_min'), claim.get('date_max'))):
        return None
    s0, s1 = _year(src['date_min']), _year(src['date_max'])
    c0, c1 = _year(claim['date_min']), _year(claim['date_max'])
    if None in (s0, s1, c0, c1):
        return None
    return {'min': s0 - c1, 'max': s1 - c0}


def _after_text(span: dict | None) -> str:
    if span is None:
        return 'undated'
    mid = round((span['min'] + span['max']) / 2)
    if -1 <= mid <= 1:
        return 'at the time'
    return f'about {mid} years after' if mid > 0 else f'about {-mid} years before'


def _source_meta(archive_root: Path, src: dict) -> dict:
    try:
        return read_record(archive_root / src['path']).get('meta') or {}
    except Exception:  # an unreadable record just means no extra facts
        return {}


# "Oak Tree Cemetery" is a place, not a family tree: only a tree phrase counts.
_TREE_TITLE = re.compile(
    r'\b(?:family|ancestry|ancestor|descendant|pedigree|online|public|member|ancestral)\s+trees?\b'
    r'|\btrees?\s+(?:of|for)\b|\bpedigree\b', re.I)


def _is_someones_tree(meta: dict, title: str) -> bool:
    if str(meta.get('subtype') or '').lower() == 'gedcom':
        return True
    return (str(meta.get('source_type') or '').lower() == 'website'
            and bool(_TREE_TITLE.search(title)))


def _same_origin(rows: list[dict], sources: dict[str, dict], metas: dict[str, dict],
                 conn, archive_root: Path, fha_config: dict) -> None:
    """Fill `same_origin_as` (row numbers) and `origin_notes` on every row."""
    groups: list[tuple[str, set[int]]] = []
    by_source: dict[str, list[int]] = {}
    for r in rows:
        by_source.setdefault(r['source_id'], []).append(r['n'])
    for sid, ns in by_source.items():
        if len(ns) > 1:
            groups.append(('one source', set(ns)))
    trees = {r['n'] for r in rows if _is_someones_tree(metas[r['source_id']], sources[r['source_id']]['title'])}
    if len(trees) > 1:
        groups.append(("someone's tree", trees))
    # One record saved from two sites: W162's rule, on the sources shown here.
    index = record_collection_index()
    if index and len(by_source) > 1:
        read = site_data_reader(archive_root, fha_config)
        info = {}
        for sid in by_source:
            meta = metas[sid]
            sc = source_site_collection(meta, read)
            group = sc and record_collection_group(sc[0], sc[1], index)
            subjects = source_record_subjects(meta, read) if group else set()
            if subjects:
                people = {p[0] for p in conn.execute(
                    'SELECT person_id FROM source_people WHERE source_id=?', (sid,))}
                info[sid] = (sc, group, subjects, people, str(sources[sid].get('date_edtf') or ''))
        sids = sorted(info)
        for i, a in enumerate(sids):
            for b in sids[i + 1:]:
                ia, ib = info[a], info[b]
                if (ia[0][0] != ib[0][0] and ia[1][0] == ib[1][0]
                        and same_record_subject(ia[2], ib[2]) and (ia[3] & ib[3])
                        and source_dates_agree(ia[4], ib[4], ia[1][2])):
                    groups.append(('one record saved from two sites',
                                   set(by_source[a]) | set(by_source[b])))
    for r in rows:
        r['same_origin_as'] = []
        r['origin_notes'] = []
    for note, members in groups:
        for n in members:
            row = rows[n - 1]
            row['same_origin_as'] = sorted(set(row['same_origin_as']) | (members - {n}))
            if note not in row['origin_notes']:
                row['origin_notes'].append(note)


def _rows(conn, claims: list[dict], pid: str, archive_root: Path, fha_config: dict) -> list[dict]:
    sources = {}
    for c in claims:
        if c['source_id'] not in sources:
            s = conn.execute('SELECT * FROM sources WHERE id=?', (c['source_id'],)).fetchone()
            sources[c['source_id']] = dict(s) if s else {'id': c['source_id'], 'title': c['source_id'], 'path': ''}
    metas = {sid: _source_meta(archive_root, s) if s.get('path') else {} for sid, s in sources.items()}
    people_of = {c['id']: {p for p, _ in _claim_people(conn, c['id'])} for c in claims}
    number = {c['id']: i + 1 for i, c in enumerate(claims)}
    rows = []
    for i, c in enumerate(claims):
        s = sources[c['source_id']]
        informants = []
        for p, role in conn.execute(
                'SELECT cp.person_id, cp.role FROM claim_persons cp WHERE cp.claim_id=? '
                'ORDER BY cp.position', (c['id'],)):
            if 'informant' in str(role or '').split(CLAIM_ROLE_SEP):
                name = conn.execute('SELECT name FROM persons WHERE id=?', (p,)).fetchone()
                informants.append(name[0] if name and name[0] else fmt_id_display(p))
        links = [{'rel': rel, 'target': tgt, 'row': number.get(tgt)}
                 for rel, tgt in conn.execute(
                     'SELECT rel, target_id FROM claim_links WHERE claim_id=? '
                     "AND rel IN ('corroborates','contradicts') ORDER BY rel, target_id", (c['id'],))]
        reviewed = bool(str(c.get('reviewed') or '').strip())
        rows.append({
            'n': i + 1,
            'claim_id': c['id'],
            'type': c['type'],
            'value': c['value'],
            'date_edtf': c['date_edtf'],
            'place_text': c['place_text'],
            'place_id': c['place_id'],
            'source_id': c['source_id'],
            'source_title': s['title'],
            'source_type': s.get('source_type'),
            'source_class': s.get('source_class'),
            'source_date': s.get('date_edtf'),
            'repository': s.get('repository'),
            'path': s.get('path', '').replace('\\', '/'),
            'recorded_after_years': _recorded_after(s, c),
            'informants': informants,
            'information': c['information'],
            'evidence': c['evidence'],
            'status': c['status'],
            'reviewed': c['reviewed'],
            'parked': c['status'] == 'suggested' and reviewed,
            'confidence': c['confidence'],
            'links': links,
            'anchor': c['anchor'],
            '_people': people_of[c['id']],
            '_date_min': c['date_min'], '_date_max': c['date_max'],
        })
    _same_origin(rows, sources, metas, conn, archive_root, fha_config)
    return rows


def _place_key(row: dict) -> str | None:
    if row['place_id']:
        return 'id:' + str(row['place_id'])
    text = str(row['place_text'] or '').split(',')[0]
    text = ' '.join(re.sub(r'[^\w\s]', ' ', text.casefold()).split())
    return 'text:' + text if text else None


def _disagree(rows: list[dict]) -> bool:
    """Two rows whose date ranges do not overlap, or whose places differ."""
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            # A birth and its baptism, a death and its burial, or two different
            # weddings are different events: they may differ without a conflict.
            if a['type'] != b['type']:
                continue
            if a['type'] == 'marriage' and a['_people'] != b['_people']:
                continue
            if (_dated(a['_date_min'], a['_date_max']) and _dated(b['_date_min'], b['_date_max'])
                    and (a['_date_max'] < b['_date_min'] or b['_date_max'] < a['_date_min'])):
                return True
            pa, pb = _place_key(a), _place_key(b)
            if pa and pb and pa != pb and pa[:3] == pb[:3]:
                return True
    return False


def _public(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in r.items() if not k.startswith('_')} for r in rows]


def run_compare(archive_root: Path, fha_config: dict, *, person: str, fact: str | None = None,
                accepted_only: bool = False, include_rejected: bool = False) -> Result:
    """Every record about one fact of one person. Read-only."""
    pid = normalize_id(person)
    if is_valid_id(pid) and id_type_of(pid) != 'P':
        r = Result(ok=False, exit_code=EXIT_ERRORS, data={'error': 'bad-id'})
        r.add('error', f'{person!r} is not a person ID. Give one like `fha compare p-de957bcda1` '
                       'or a full name. `fha find "<name>"` looks an ID up by name.')
        return r
    conn = open_index_db(archive_root, _REQUIRED_TABLES)
    if conn is None:
        return Result(ok=False, exit_code=EXIT_FAILURE, data={'error': 'no-index'})
    try:
        if not is_valid_id(pid):
            matches = persons_named(conn, person)
            if len(matches) != 1:
                r = Result(ok=False, exit_code=EXIT_ERRORS,
                           data={'error': 'ambiguous-name' if matches else 'bad-id',
                                 'candidates': [m['id'] for m in matches]})
                r.add('error', person_name_refusal(person, matches, '`fha compare <p-id>`'))
                return r
            pid = normalize_id(matches[0]['id'])
        row = conn.execute('SELECT id, name, status, merged_into FROM persons WHERE id=?',
                           (pid,)).fetchone()
        if row is None:
            r = Result(ok=False, exit_code=EXIT_WARNINGS, data={'error': 'no-person'})
            r.add('warning', f'No person {fmt_id_display(pid)} in the index. Check the ID with '
                             f'`fha find {fmt_id_display(pid)}`, or run `fha index` if the record is new.')
            return r
        merged_note = None
        if row['status'] == 'merged' and row['merged_into']:
            # A merged record is read through the merge, as `fha confirm draft` does.
            # The whole chain (A into B into C) is followed, as the index files claims.
            survivor = normalize_id(index_merge_survivors(conn).get(pid) or str(row['merged_into']))
            merged_note = (f'{fmt_id_display(pid)} was merged into '
                           f'{fmt_id_display(survivor)}; showing the surviving record.')
            pid = survivor
            row = conn.execute('SELECT id, name, status, merged_into FROM persons WHERE id=?',
                               (pid,)).fetchone()
            if row is None:
                r = Result(ok=False, exit_code=EXIT_WARNINGS, data={'error': 'no-person'})
                r.add('warning', merged_note)
                return r
        name = row['name'] or fmt_id_display(pid)
        statuses = ('accepted',) if accepted_only else _DEFAULT_STATUSES
        if include_rejected:
            statuses = statuses + ('rejected', 'superseded')
        base = {'person': pid, 'name': name, 'merged_note': merged_note}

        if fact is None:
            # Only a fact that has one true answer can "disagree": a person has
            # one birth and one death, but moves house and changes jobs.
            kinds = list(_ONE_ANSWER)
            summary, existing = [], []
            for k in kinds:
                claims = _fact_claims(conn, pid, k, statuses)
                if not claims:
                    continue
                existing.append({'fact': k, 'records': len(claims)})
                rows = _rows(conn, claims, pid, archive_root, fha_config)
                if len(claims) > 1 and _disagree(rows):
                    years = sorted({_year(c['date_min']) for c in claims
                                    if _dated(c['date_min'], c['date_max'])} - {None})
                    summary.append({'fact': k, 'records': len(claims),
                                    'first_year': years[0] if years else None,
                                    'last_year': years[-1] if years else None})
            # Facts with records that are not listed above, so he can pick one.
            for k in _available_facts(conn, pid, statuses):
                if k not in kinds:
                    existing.append({'fact': k, 'records': len(_fact_claims(conn, pid, k, statuses))})
            if not existing:
                r = Result(ok=False, exit_code=EXIT_WARNINGS,
                           data={**base, 'error': 'no-claims', 'facts': []})
                r.add('warning', f'No records name {name} {fmt_id_display(pid)} yet.')
                return r
            return Result(data={**base, 'fact': None, 'disagreeing': summary, 'facts': existing})

        claims = _fact_claims(conn, pid, fact, statuses)
        if not claims:
            existing = _available_facts(conn, pid, statuses)
            r = Result(ok=False, exit_code=EXIT_WARNINGS,
                       data={**base, 'error': 'no-claims', 'fact': fact, 'facts': existing})
            have = (', '.join(existing) + '.') if existing else 'none yet.'
            r.add('warning', f'No {fact} records for {name}. Facts that have records: {have}')
            return r
        rows = _rows(conn, claims, pid, archive_root, fha_config)
        return Result(data={**base, 'fact': fact, 'rows': _public(rows)})
    finally:
        conn.close()


# ── rendering ────────────────────────────────────────────────────────────────

def _say(row: dict) -> str:
    date = humanize_edtf(row['date_edtf']) if row['date_edtf'] else 'no date'
    bits = [date.lower() if date.startswith('Undated') else date]
    if row['place_text']:
        bits.append(row['place_text'])
    return ', '.join(bits)


def _status_text(row: dict) -> str:
    s = str(row['status']).capitalize()
    if row['parked']:
        s += ' (parked)'
    return f"{s}, {row['confidence']}" if row['confidence'] else s


def _print_row(row: dict) -> None:
    src_date = humanize_edtf(row['source_date']) if row['source_date'] else 'undated'
    after = _after_text(row['recorded_after_years'])
    told = ', '.join(row['informants']) if row['informants'] else 'not named'
    info = _INFORMATION_TEXT.get(str(row['information'] or '').lower(), 'not known')
    how = _EVIDENCE_TEXT.get(str(row['evidence'] or '').lower())
    value = str(row.get('value') or '').strip()
    print(f"{row['n']:>2}. {_say(row)}")
    if value:
        print(f"    Says: {value}")
    print(f"    {row['source_title']} ({src_date}): {after}. Told by: {told}.")
    print(f"    Information: {info}. {(how + '. ') if how else ''}{_status_text(row)}.")


def _cmd_compare(result: Result, use_json: bool = False) -> int:
    if use_json:
        print(json.dumps(result.as_dict(), indent=2))
        return result.exit_code
    if not result.ok:
        for m in result.messages:
            print(f"{'ERROR' if m.level == 'error' else 'WARNING'}: {m.text}", file=sys.stderr)
        return result.exit_code
    d = result.data
    who = f"{d['name']} [{fmt_id_display(d['person'])}]"
    if d.get('merged_note'):
        print(d['merged_note'])
    if d['fact'] is None:
        if not d['disagreeing']:
            print(f'No fact of {who} has records that disagree.')
            print('Facts with records: ' + ', '.join(
                f"{f['fact']} ({f['records']})" for f in d['facts']) + '.')
            return result.exit_code
        print(f'Facts of {who} where the records disagree:')
        for f in d['disagreeing']:
            span = ''
            if f['first_year'] is not None:
                span = (f", years {f['first_year']} to {f['last_year']}"
                        if f['first_year'] != f['last_year'] else f", year {f['first_year']}")
            print(f"  {_FACT_LABEL.get(f['fact'], f['fact'].capitalize())}: "
                  f"{f['records']} records{span} (they disagree)")
        print('Run `fha compare ' + fmt_id_display(d['person']) + ' --fact <fact>` to see one side by side.')
        return result.exit_code
    rows = d['rows']
    label = _FACT_LABEL.get(d['fact'], d['fact'].capitalize())
    print(f"{label} of {who}: {len(rows)} record{'s' if len(rows) != 1 else ''}")
    print()
    for r in rows:
        _print_row(r)
    print()
    origin = [r for r in rows if r['same_origin_as']]
    if origin:
        print('Same origin (these are not independent evidence):')
        for r in origin:
            others = ', '.join(str(n) for n in r['same_origin_as'])
            print(f"  {r['n']} and {others}: {'; '.join(r['origin_notes'])}")
    else:
        print('Same origin: none found.')
    linked = [(r['n'], l) for r in rows for l in r['links'] if l['row']]
    if linked:
        seen, said = set(), []
        for n, l in linked:
            key = (l['rel'], frozenset((n, l['row'])))
            if key not in seen:
                seen.add(key)
                said.append(f"{n} {'agrees with' if l['rel'] == 'corroborates' else 'disagrees with'} {l['row']}")
        print('Already linked: ' + '; '.join(said) + '.')
    else:
        print('Already linked: none.')
    if any(r['information'] in (None, '', 'undetermined') or r['evidence'] == 'indirect' for r in rows):
        print('Ages in a census were often rounded; an age and the year worked out from it are one fact.')
    return result.exit_code


# ── CLI ──────────────────────────────────────────────────────────────────────

_CLI_DESCRIPTION = """\
Every record about one fact, side by side.

  fha compare p-xxxxxxxxxx               Which facts of this person have records that disagree
  fha compare "Mary Smith" --fact birth  Every birth or baptism record in one table: what it says,
                                         when it was written, who told it, firsthand or not, and
                                         which records are copies of one another
  fha compare p-xxxxxxxxxx --fact death  death, marriage, parents, name, residence, or any claim type
  fha compare p-xxxxxxxxxx --accepted-only   Leave out records not yet reviewed
  fha compare p-xxxxxxxxxx --include-rejected   Also show rejected and superseded records
  fha compare p-xxxxxxxxxx --json        The same answer as machine-readable JSON

Read-only: nothing is written."""


def _add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument('person', metavar='p-id', help='The person, by ID or by full name')
    p.add_argument('--fact', metavar='FACT',
                   help='birth, death, marriage, parents, name, residence or any claim type '
                        '(default: list the facts whose records disagree)')
    p.add_argument('--accepted-only', action='store_true', dest='accepted_only',
                   help='Only accepted claims (default also shows suggested and disputed)')
    p.add_argument('--include-rejected', action='store_true', dest='include_rejected',
                   help='Also show rejected and superseded claims')
    p.add_argument('--json', action='store_true', dest='use_json', help='Machine-readable JSON output')
    p.add_argument('--root', metavar='PATH', help='Archive root (overrides auto-detection)')


def register(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        'compare',
        help='Every record about one fact of one person, side by side.',
        description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_arguments(p)
    p.set_defaults(func=_run_compare)


def _run_compare(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args)
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print(f'ERROR: {e}', file=sys.stderr)
        return EXIT_FAILURE
    fact = args.fact.strip().lower() if args.fact else None
    if args.fact is not None and not fact:
        print('ERROR: --fact needs a word such as birth, death or marriage.', file=sys.stderr)
        return EXIT_ERRORS
    result = run_compare(archive_root, fha_config, person=args.person, fact=fact,
                         accepted_only=args.accepted_only, include_rejected=args.include_rejected)
    return _cmd_compare(result, use_json=args.use_json)


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha compare', description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_arguments(parser)
    return _run_compare(parser.parse_args(argv))


if __name__ == '__main__':
    sys.exit(_standalone_main())
