"""
chart_cases.py - the hard family shapes every chart must survive.

Not a test module: tests/test_site.py (ChartCaseTests) and tests/test_views.py
(TreeCaseTests) import it. Anyone building a family chart, a fan, the
explorer or a GraphViz export gets the same eighteen awkward families to try
it on: a man with five wives, a niece who married her uncle, a person who is
their own ancestor by a typing slip. A chart that works on the tidy example
archive and falls over on one of these falls over on a real family, and the
person it falls over for is the one with the most interesting tree.

`build_case(conn, archive_root, name)` seeds one case into a synthetic index
(the DDL from index.py) and writes the person and source files `fha site`
reads beside it. It returns:

    {'seed':   the p-id the chart is drawn around,
     'people': {role name: p-id},
     'living': [p-ids of living people, empty unless the case has some]}

The rows are the ones `fha index` derives: per relationship claim one child
`parent` row and one parent `child` row; per marriage claim spouse rows both
ways with the marriage date. Claims are `accepted`, with explicit `confidence`
and, for non-genetic ties, `subtype`.

Every name is an example-archive name (Hartley, Cole, Webb, Reed, Marr, Ford).
Nobody here is a real person.

Case ids, shapes and what a finished chart should do with them are the table
in the chart build plan (08-2); each builder's docstring says its own part.
The expected behaviours the later chart changes will assert are named in
comments ("fan after 08-5") so the PR that flips one knows where.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

CASE_NAMES = tuple(f'C{n}' for n in range(1, 19))


class _Case:
    """One family being seeded. People are named by a role word ('husband',
    'wife2'); ids are numbered in the order the people are added so the same
    case always gets the same ids."""

    def __init__(self, conn: sqlite3.Connection, root: Path, name: str):
        self.conn = conn
        self.root = Path(root)
        self.name = name
        self.people: dict[str, str] = {}
        self.living: list[str] = []
        self._claims = 0
        self.source = 's-0000000001'
        base = int(name[1:]) * 1000
        self._base = base
        self._source_file()

    # - ids -

    def _next_claim(self) -> str:
        self._claims += 1
        return f'c-{self._base + self._claims:010d}'

    # - seeding -

    def _source_file(self) -> None:
        rel = f'sources/vital/src_{self.source}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f'---\nid: {self.source}\ntitle: Family record\nsource_type: vital\n'
            f'citation: "A family record."\n---\n\n## Claims\n', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO sources(id, title, source_type, restricted, publication_ok, status, path) '
            'VALUES (?,?,?,?,?,?,?)',
            (self.source, 'Family record', 'vital', 0, None, 'active', rel))

    def person(self, role: str, name: str, sex: str = 'M', *, living: bool = False) -> str:
        pid = f'p-{self._base + len(self.people) + 500:010d}'
        surname = name.split()[-1]
        rel = f'people/{surname.lower()}__test_{pid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nid: {pid}\nname: {name}\n---\n# {name}\n', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, surname, sex, 'true' if living else 'false', 'curated', 'active', rel))
        self.conn.execute(
            'INSERT INTO source_people(source_id, person_id) VALUES (?,?)', (self.source, pid))
        self.people[role] = pid
        if living:
            self.living.append(pid)
        return pid

    def _claim(self, ctype, value, persons, *, roles=None, confidence='high',
               subtype=None, date=None) -> str:
        cid = self._next_claim()
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, subtype, value, status, date_edtf, '
            'date_min, confidence, negated) VALUES (?,?,?,?,?,?,?,?,?,0)',
            (cid, self.source, ctype, subtype, value, 'accepted', date,
             f'{date[:4]}-01-01' if date else None, confidence))
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) VALUES (?,?,?,?)',
                (cid, pid, pos, (roles or {}).get(pid)))
        return cid

    def parent(self, child: str, parent: str, *, confidence: str = 'high',
               subtype: str | None = None) -> None:
        """`parent` is a parent of `child` (roles by role word). Both
        directions of the relationship row, as `fha index` writes them."""
        c, p = self.people[child], self.people[parent]
        cid = self._claim('relationship', f'{parent} is parent of {child}', (c, p),
                          roles={c: 'child', p: 'parent'}, confidence=confidence,
                          subtype=subtype)
        self.conn.execute(
            'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
            (c, 'parent', p, cid))
        self.conn.execute(
            'INSERT INTO relationships(person_id, rel, other_id, claim_id) VALUES (?,?,?,?)',
            (p, 'child', c, cid))

    def parents(self, child: str, *parents: str, **kw) -> None:
        for p in parents:
            self.parent(child, p, **kw)

    def marry(self, a: str, b: str, year: str = '1900', *, confidence: str = 'high') -> None:
        pa, pb = self.people[a], self.people[b]
        cid = self._claim('marriage', f'{a} married {b}', (pa, pb),
                          roles={pa: 'spouse', pb: 'spouse'}, confidence=confidence, date=year)
        for x, y in ((pa, pb), (pb, pa)):
            self.conn.execute(
                'INSERT INTO relationships(person_id, rel, other_id, claim_id, date_start) '
                'VALUES (?,?,?,?,?)', (x, 'spouse', y, cid, f'{year}-01-01'))

    def born(self, who: str, year: str) -> None:
        pid = self.people[who]
        self._claim('birth', 'born', (pid,), roles={pid: 'child'}, date=year)

    def died(self, who: str, year: str) -> None:
        pid = self.people[who]
        self._claim('death', 'died', (pid,), roles={pid: 'deceased'}, date=year)

    def result(self, seed: str) -> dict:
        return {'seed': self.people[seed], 'people': dict(self.people),
                'living': list(self.living)}


# - the cases -


def _c1(c: _Case) -> str:
    """Five spouses in sequence. Children by the 1st, 2nd and 4th; the 3rd and
    5th marriages are childless (the plan's table says one childless marriage,
    but five spouses with three mothers leaves two).
    Explorer (after 08-9): the first marriage is one couple unit, four more
    units to its right, each child under their own mother's couple. Family
    chart: later spouses dotted. DOT (after 08-11): five union nodes."""
    c.person('husband', 'Ezra Hartley')
    for n in range(1, 6):
        c.person(f'wife{n}', f'Wife{n} Cole', 'F')
        c.marry('husband', f'wife{n}', str(1900 + 10 * n))
    for who, mother in (('kid1a', 'wife1'), ('kid1b', 'wife1'), ('kid2', 'wife2'),
                        ('kid4', 'wife4')):
        c.person(who, f'{who.title()} Hartley', 'F' if who.endswith('a') else 'M')
        c.parents(who, 'husband', mother)
    return 'husband'


def _c2(c: _Case) -> str:
    """A widow with two children marries a widower with three, then they have
    two together: three step-families. Step ties are recorded as `step`
    parentage. Explorer (after 08-9): joint children under the new couple,
    each step-child under their own genetic couple and again as a repeat card
    under the step-parent. DOT (after 08-11): step edges dashed."""
    # Each step-parent is added before the genetic parent they stand beside,
    # so a chart that sorts parents by id and forgets the genetic-only rule
    # picks the step-parent and fails its test.
    c.person('widow', 'Ada Cole', 'F')
    c.person('widower', 'Tom Webb')
    c.person('late_husband', 'Walter Cole')
    c.person('late_wife', 'Nell Webb', 'F')
    c.marry('late_husband', 'widow', '1890')
    c.marry('widower', 'late_wife', '1888')
    c.marry('widower', 'widow', '1900')
    for n in (1, 2):
        c.person(f'cole{n}', f'Cole{n} Cole', 'F')
        c.parents(f'cole{n}', 'late_husband', 'widow')
        c.parent(f'cole{n}', 'widower', subtype='step')
    for n in (1, 2, 3):
        c.person(f'webb{n}', f'Webb{n} Webb')
        c.parents(f'webb{n}', 'widower', 'late_wife')
        c.parent(f'webb{n}', 'widow', subtype='step')
    for n in (1, 2):
        c.person(f'joint{n}', f'Joint{n} Webb')
        c.parents(f'joint{n}', 'widower', 'widow')
    return 'widower'


def _cousin_line(c: _Case) -> None:
    """Grandparents with a son and a daughter whose own children marry, and
    those two have a child (role `k`)."""
    c.person('gf', 'Calvin Hartley')
    c.person('gm', 'Edith Cole', 'F')
    c.marry('gf', 'gm', '1850')
    c.person('a', 'Alan Hartley')
    c.person('b', 'Beth Hartley', 'F')
    c.parents('a', 'gf', 'gm')
    c.parents('b', 'gf', 'gm')
    c.person('a_wife', 'Cora Marr', 'F')
    c.person('b_husband', 'Dan Marr')
    c.marry('a', 'a_wife', '1875')
    c.marry('b', 'b_husband', '1876')
    c.person('x', 'Xavier Hartley')
    c.parents('x', 'a', 'a_wife')
    c.person('y', 'Yvonne Marr', 'F')
    c.parents('y', 'b_husband', 'b')
    c.marry('x', 'y', '1900')
    c.person('k', 'Kit Hartley')
    c.parents('k', 'x', 'y')


def _c3(c: _Case) -> str:
    """An uncle marries his niece: her grandparents are his parents.
    Seed is their child. Fan (after 08-5): the grandparents fill two slots
    each, drawn in the same repeat colour, the title naming the other slot."""
    c.person('gf', 'Calvin Hartley')
    c.person('gm', 'Edith Cole', 'F')
    c.marry('gf', 'gm', '1850')
    c.person('uncle', 'Alan Hartley')
    c.person('aunt', 'Dora Hartley', 'F')
    c.parents('uncle', 'gf', 'gm')
    c.parents('aunt', 'gf', 'gm')
    c.person('aunt_husband', 'Hugh Ford')
    c.marry('aunt_husband', 'aunt', '1875')
    c.person('niece', 'Nora Ford', 'F')
    c.parents('niece', 'aunt_husband', 'aunt')
    c.marry('uncle', 'niece', '1900')
    c.person('child', 'Kit Hartley')
    c.parents('child', 'uncle', 'niece')
    return 'child'


def _c4(c: _Case) -> str:
    """First cousins marry; seed is their child. Fan (after 08-5): the shared
    grandparents fill two slots each."""
    _cousin_line(c)
    return 'k'


def _c5(c: _Case) -> str:
    """Double first cousins: two brothers marry two sisters and a child of each
    marriage marry. Four repeats on the fan (after 08-5)."""
    c.person('gf', 'Calvin Hartley')
    c.person('gm', 'Edith Cole', 'F')
    c.marry('gf', 'gm', '1850')
    c.person('gf2', 'Silas Webb')
    c.person('gm2', 'Ruth Reed', 'F')
    c.marry('gf2', 'gm2', '1852')
    c.person('bro1', 'Alan Hartley')
    c.person('bro2', 'Bert Hartley')
    c.parents('bro1', 'gf', 'gm')
    c.parents('bro2', 'gf', 'gm')
    c.person('sis1', 'Cora Webb', 'F')
    c.person('sis2', 'Dora Webb', 'F')
    c.parents('sis1', 'gf2', 'gm2')
    c.parents('sis2', 'gf2', 'gm2')
    c.marry('bro1', 'sis1', '1875')
    c.marry('bro2', 'sis2', '1876')
    c.person('x', 'Xavier Hartley')
    c.parents('x', 'bro1', 'sis1')
    c.person('y', 'Yvonne Hartley', 'F')
    c.parents('y', 'bro2', 'sis2')
    c.marry('x', 'y', '1900')
    c.person('k', 'Kit Hartley')
    c.parents('k', 'x', 'y')
    return 'k'


def _c6(c: _Case) -> str:
    """A single parent; the other parent was never recorded. Fan (after
    08-5): the empty slot draws no segment. Family chart: an 'Unknown'
    placeholder. DOT: a union with one parent."""
    c.person('mother', 'Ruth Reed', 'F')
    c.person('gf', 'Silas Reed')
    c.person('gm', 'Nell Reed', 'F')
    c.parents('mother', 'gf', 'gm')
    c.person('child', 'Kit Reed')
    c.parent('child', 'mother')
    return 'child'


def _c7(c: _Case) -> str:
    """Biological and adoptive parents both recorded. The adoptive parents
    never take a fan slot (genetic edges only, SPEC 12.2). Explorer (after
    08-9): the adoptive edge is a legal edge, the child under the biological
    couple and again as a repeat card under the adoptive parent."""
    # The adoptive parents are added first, so their ids sort before the
    # birth parents': a fan that dropped the genetic-only rule would pick them.
    c.person('adopter', 'Hugh Ford')
    c.person('adopter_wife', 'Dora Ford', 'F')
    c.person('father', 'Walter Cole')
    c.person('mother', 'Nell Cole', 'F')
    c.person('child', 'Kit Cole')
    c.parents('child', 'father', 'mother')
    c.parents('child', 'adopter', 'adopter_wife', subtype='adoptive')
    return 'child'


def _c8(c: _Case) -> str:
    """Twelve siblings, three undated. Explorer (after 08-9): dated children
    by birth date, undated ones after them by id."""
    c.person('father', 'Tom Webb')
    c.person('mother', 'Nell Webb', 'F')
    c.marry('father', 'mother', '1850')
    for n in range(1, 13):
        who = f'kid{n:02d}'
        c.person(who, f'Kid{n:02d} Webb', 'F' if n % 2 else 'M')
        c.parents(who, 'father', 'mother')
        if n <= 9:
            c.born(who, str(1851 + 2 * (9 - n)))   # birth order runs against id order
    return 'father'


def _c9(c: _Case) -> str:
    """A widow marries her late husband's brother. Two families, both shown.
    The brothers share recorded parents, so the widow's two sons are cousins
    and half-brothers at once, and share grandparents."""
    c.person('gf', 'Calvin Hartley')
    c.person('gm', 'Edith Cole', 'F')
    c.marry('gf', 'gm', '1850')
    c.person('brother1', 'Alan Hartley')
    c.person('brother2', 'Bert Hartley')
    c.parents('brother1', 'gf', 'gm')
    c.parents('brother2', 'gf', 'gm')
    c.person('widow', 'Cora Marr', 'F')
    c.marry('brother1', 'widow', '1880')
    c.died('brother1', '1890')
    c.person('son1', 'Sam Hartley')
    c.parents('son1', 'brother1', 'widow')
    c.marry('brother2', 'widow', '1892')
    c.person('son2', 'Seth Hartley')
    c.parents('son2', 'brother2', 'widow')
    return 'widow'


def _c10(c: _Case) -> str:
    """One line six generations deep beside a line with no parents. The
    father's father's father... line is recorded back to the 64th slot, each
    man with his wife; the seed's mother has no recorded parents. Fan (after
    08-3): sizes to depth 6 only where the line reaches, the other quarter
    stays empty. Only the one line is filled; a test that needs every slot
    of a generation seeds its own full tree."""
    c.person('seed', 'Kit Hartley')
    c.person('mother', 'Ruth Reed', 'F')
    prev = 'seed'
    for gen in range(1, 7):
        man = f'man{gen}'
        wife = 'mother' if gen == 1 else f'wife{gen}'   # gen 1's wife is the seed's mother
        c.person(man, f'Man{gen} Hartley')
        if gen > 1:
            c.person(wife, f'Wife{gen} Ford', 'F')
        c.parents(prev, man, wife)
        prev = man
    return 'seed'


def _c11(c: _Case) -> str:
    """Two recorded mothers (same-sex couple), no father. Fan slots are filled
    by elimination, so `sex_derived` is False and no father's-line or
    mother's-line highlight applies (after 08-5)."""
    c.person('mother1', 'Ruth Reed', 'F')
    c.person('mother2', 'Nell Cole', 'F')
    c.person('child', 'Kit Reed')
    c.parents('child', 'mother1', 'mother2')
    c.marry('mother1', 'mother2', '1990')
    return 'child'


def _c12(c: _Case) -> str:
    """A person who is their own ancestor: a data error (a lint finding
    elsewhere). Every chart must still finish."""
    c.person('a', 'Alan Hartley')
    c.person('b', 'Bert Hartley')
    c.person('c', 'Carl Hartley')
    c.parent('a', 'b')
    c.parent('b', 'c')
    c.parent('c', 'a')
    return 'a'


def _c13(c: _Case) -> str:
    """A living spouse and a living child in the middle of a descendant line,
    with a deceased grandchild below them. In a shared build the living people
    are never named, linked or counted, and the grandchild has no public path
    to the seed so the explorer does not reach them (the behaviour of
    `_build_tree_data` today). `fha views` is a private tool and shows all."""
    c.person('grandfather', 'Calvin Hartley')
    c.person('dead_son', 'Alan Hartley')
    c.parents('dead_son', 'grandfather')
    c.person('living_child', 'Jessamy Hartley', 'F', living=True)
    c.parent('living_child', 'grandfather')
    c.person('living_spouse', 'Fenwick Ford', living=True)
    c.marry('living_child', 'living_spouse', '1985')
    c.person('grandchild', 'Wren Hartley', 'F')
    c.parents('grandchild', 'living_child', 'living_spouse')
    return 'grandfather'


def _c14(c: _Case) -> str:
    """A couple with no children together, each with children from an earlier
    marriage. The childless marriage is still a couple unit (after 08-9)."""
    c.person('husband', 'Adam Hartley')
    c.person('first_wife', 'Mae Hartley', 'F')
    c.person('wife', 'Beth Cole', 'F')
    c.person('first_husband', 'Joel Cole')
    c.marry('husband', 'first_wife', '1880')
    c.marry('wife', 'first_husband', '1882')
    c.marry('husband', 'wife', '1910')
    for who, ps in (('hkid', ('husband', 'first_wife')), ('wkid', ('first_husband', 'wife'))):
        c.person(who, f'{who.title()} Hartley')
        c.parents(who, *ps)
    return 'husband'


def _c15(c: _Case) -> str:
    """Half-siblings sharing a father; grouped under two different couples
    (after 08-9)."""
    c.person('father', 'Tom Webb')
    c.person('wife1', 'Nell Webb', 'F')
    c.person('wife2', 'Ruth Reed', 'F')
    c.marry('father', 'wife1', '1880')
    c.marry('father', 'wife2', '1895')
    c.person('half1', 'Hal Webb')
    c.person('half2', 'Hana Webb', 'F')
    c.parents('half1', 'father', 'wife1')
    c.parents('half2', 'father', 'wife2')
    return 'father'


def _c16(c: _Case) -> str:
    """A child of a cousin marriage reached twice in a descendant explorer:
    the seed is the grandfather, and the child is a grandchild by two routes.
    The second occurrence is a repeat card pointing to the first (after 08-9)."""
    _cousin_line(c)
    return 'gf'


def _c17(c: _Case) -> str:
    """Genetic father and mother, and a stepfather married to the mother. The
    stepfather takes no fan slot; the explorer shows the child under father
    and mother, with a repeat card under mother and stepfather (after 08-9)."""
    # The stepfather is added first, so his id sorts before the father's: a
    # fan that dropped the genetic-only rule would put him in the father's slot.
    c.person('stepfather', 'Hugh Ford')
    c.person('father', 'Walter Cole')
    c.person('mother', 'Nell Cole', 'F')
    c.marry('father', 'mother', '1900')
    c.marry('stepfather', 'mother', '1910')
    c.person('child', 'Kit Cole')
    c.parents('child', 'father', 'mother')
    c.parent('child', 'stepfather', subtype='step')
    return 'child'


def _c18(c: _Case) -> str:
    """A weak line: the seed's father tie is `confidence: low` and everything
    above it is high. After 08-7 the father and all of his ancestors are drawn
    faded and the mother's side is not."""
    c.person('child', 'Kit Hartley')
    c.person('father', 'Alan Hartley')
    c.person('mother', 'Ruth Reed', 'F')
    c.parent('child', 'father', confidence='low')
    c.parent('child', 'mother')
    for side, kid in (('f', 'father'), ('m', 'mother')):
        c.person(f'{side}_gf', 'Silas Hartley' if side == 'f' else 'Silas Reed')
        c.person(f'{side}_gm', 'Edith Cole' if side == 'f' else 'Edith Webb', 'F')
        c.parents(kid, f'{side}_gf', f'{side}_gm')
    return 'child'


_BUILDERS = {f'C{n}': globals()[f'_c{n}'] for n in range(1, 19)}


def build_case(conn: sqlite3.Connection, archive_root: Path, name: str) -> dict:
    """Seed case `name` ('C1' to 'C18') into `conn` and the files under
    `archive_root`. Call once per fresh index; two cases in one index would
    share ids."""
    case = _Case(conn, archive_root, name)
    seed = _BUILDERS[name](case)
    (Path(archive_root) / 'fha.yaml').write_text(
        f'roots: {{}}\nroot_person: {case.people[seed].replace("p-", "P-", 1)}\n',
        encoding='utf-8')
    return case.result(seed)
