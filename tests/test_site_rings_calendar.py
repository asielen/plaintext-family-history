"""
test_site_rings_calendar.py - the two optional `fha site` panels (TOOLING §12):
completeness rings on person pages (`site: completeness_rings: true`) and
"This month in the family" on the home page (`site: calendar: true`).

Both are off unless fha.yaml turns them on. The ring draws the five checks of
`_lib.person_completeness` (TOOLING §7a) and is never shown for a living or
unknown person, in any build. The calendar lists `_lib.family_calendar_events`
(TOOLING §7b) after the site's own privacy rules (SPEC §19, §21) have dropped
every event that names a withheld person or rests on a withheld claim or
source. Expected answers come from those rules, not from the code's output.

Builds a synthetic .cache/index.sqlite from index.py's DDL, the way
tests/test_site.py does, so no full archive is needed. Temp fixtures only.
"""

import importlib.util
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from index import _DDL as INDEX_DDL
from _lib import (
    COMPLETENESS_CHECKS, completeness_ring_svg, index_manifest_path,
    record_path_manifest, write_path_manifest,
)

# site.py's stem collides with the stdlib `site` module, so it is loaded by
# path under a private name (as tests/test_site.py and fha.py do).
_spec = importlib.util.spec_from_file_location('fha_site_rings', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site_rings'] = site
_spec.loader.exec_module(site)

ME = 'p-aaaaaaaaaa'       # deceased, curated: the person whose ring is tested
DAD = 'p-bbbbbbbbbb'      # deceased, curated
LIVE = 'p-cccccccccc'     # living
UNK = 'p-dddddddddd'      # living: unknown
QUIET = 'p-eeeeeeeeee'    # deceased, restricted: by-request
OTHER = 'p-ffffffffff'    # deceased, curated

POLAND = 'l-aaaaaaaaaa'

BOTH_ON = 'site:\n  completeness_rings: true\n  calendar: true\n'


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        cache = self.root / '.cache'
        cache.mkdir(parents=True)
        self.conn = sqlite3.connect(str(cache / 'index.sqlite'))
        self.conn.executescript(INDEX_DDL)
        self.conn.row_factory = sqlite3.Row
        self.out = self.root / '.cache' / 'site'

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    # - seeding -

    def config(self, text: str) -> None:
        (self.root / 'fha.yaml').write_text('roots: {}\n' + text, encoding='utf-8')

    def person(self, pid, name, *, living='false', tier='curated', extra=''):
        rel = f'people/{name.split()[-1].lower()}_{pid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nid: {pid}\nname: {name}\n{extra}---\n# {name}\n',
                        encoding='utf-8')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, name.split()[-1], 'F', living, tier, 'active', rel))

    def source(self, sid, title, *, source_type='vital-record', restricted=0,
               restricted_claims=()):
        """A source record. `restricted_claims` lists claim ids the file's own
        `## Claims` block marks `restricted: true` - the claim-level marker
        the index does not carry, which the site reads from the file."""
        rel = f'sources/{source_type}/src_{sid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = ['---', f'id: {sid}', f'title: {title}', f'source_type: {source_type}',
                 'citation: "A citation."', '---', '', '## Claims', '```yaml']
        for cid in restricted_claims:
            lines += [f'- id: {cid}', '  type: birth', '  value: "private"',
                      '  status: accepted', '  restricted: true']
        lines.append('```')
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO sources(id, title, source_type, restricted, status, path) '
            'VALUES (?,?,?,?,?,?)', (sid, title, source_type, restricted, 'active', rel))

    def claim(self, cid, sid, ctype, date, persons, *, status='accepted', roles=None,
              place_id=None, place_text=None, negated=0):
        lo = hi = None
        if date:
            lo = (date + '-01-01')[:10] if len(date) == 4 else (date + '-01')[:10]
            hi = lo
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, date_edtf, date_min, '
            'date_max, place_id, place_text, negated) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (cid, sid, ctype, f'{ctype} record', status, date, lo, hi, place_id, place_text,
             negated))
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) '
                'VALUES (?,?,?,?)', (cid, pid, pos, (roles or {}).get(pid)))

    def place(self, lid, name, hierarchy):
        self.conn.execute('INSERT INTO places(id, name, hierarchy) VALUES (?,?,?)',
                          (lid, name, hierarchy))

    def build(self, *, linked=False, today='2026-06-15'):
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.root / '.cache' / 'index.sqlite', (future, future))
        write_path_manifest(index_manifest_path(self.root), record_path_manifest(self.root))
        with unittest.mock.patch.object(site, '_today', return_value=today):
            return site.run_site(self.root, self.out, linked=linked)

    def page(self, rel):
        return (self.out / rel).read_text(encoding='utf-8')


class RingTests(_Base):
    def seed_me(self):
        """A deceased curated person whose five checks come out in four
        different states, by TOOLING §7a's rules:
          birth       met     - an own birth claim on a vital-record source
          census      na      - her records place her only in Poland, a
                                country with no census table
          parents     partial - one birth parent named by a sourced claim
          marriage    missing - died at 50, no partner on file
          end-of-life met     - an own death claim"""
        self.place(POLAND, 'Suwalki', 'Suwalki, Poland')
        self.person(ME, 'Ada Example')
        self.person(DAD, 'Bert Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-03-14', [ME],
                   place_id=POLAND)
        self.source('s-2222222222', 'Death register')
        self.claim('c-2222222222', 's-2222222222', 'death', '1900-06-02', [ME],
                   place_id=POLAND)
        self.source('s-3333333333', 'Parish letter', source_type='letter')
        self.claim('c-3333333333', 's-3333333333', 'relationship', None, [DAD, ME],
                   roles={DAD: 'parent', ME: 'child'})

    def test_off_by_default(self):
        self.seed_me()
        self.build()
        self.assertNotIn('completeness-ring', self.page(f'persons/{ME}.html'))

    def test_deceased_person_gets_a_ring_with_each_check_state(self):
        self.seed_me()
        self.config(BOTH_ON)
        self.build()
        html = self.page(f'persons/{ME}.html')
        self.assertIn('class="completeness-ring"', html)
        states = {check: state for state, check in
                  re.findall(r'<g class="ring-(\w+)" data-check="([\w-]+)"', html)}
        self.assertEqual(states, {'birth': 'met', 'census': 'na', 'parents': 'partial',
                                  'marriage': 'missing', 'end-of-life': 'met'})
        # Not colour-only: each segment names its check, state and detail.
        self.assertIn('<title>Parents: partly found - one parent named', html)
        self.assertIn('Marriage records</strong>: not found yet', html)
        self.assertIn('How well documented: 2 of 4 found', html)

    def test_no_ring_for_a_living_person_in_any_build(self):
        self.person(LIVE, 'Cleo Example', living='true')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1990-03-14', [LIVE])
        self.config(BOTH_ON)
        self.build(linked=True)      # the linked preview gives her a page
        html = self.page(f'persons/{LIVE}.html')
        self.assertNotIn('completeness-ring', html)
        self.assertNotIn('How well documented', html)

    def test_no_ring_for_a_person_of_unknown_living_status(self):
        self.person(UNK, 'Dora Example', living='unknown')
        self.config(BOTH_ON)
        self.build(linked=True)
        self.assertNotIn('completeness-ring', self.page(f'persons/{UNK}.html'))

    def test_a_restricted_birth_record_does_not_fill_the_public_ring(self):
        # The only birth record is marked private: the shared site must not
        # tell visitors one exists. The linked preview counts it.
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register', restricted_claims=('c-1111111111',))
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-03-14', [ME])
        self.config(BOTH_ON)
        self.build()
        self.assertIn('<g class="ring-missing" data-check="birth"',
                      self.page(f'persons/{ME}.html'))
        self.build(linked=True)
        self.assertIn('<g class="ring-met" data-check="birth"',
                      self.page(f'persons/{ME}.html'))

    def ring_section(self):
        html = self.page(f'persons/{ME}.html')
        return html.split('<details class="chart completeness"', 1)[1].split('</details>', 1)[0]

    def test_a_shared_build_says_what_is_missing_in_plain_words(self):
        # TOOLING §12: visitors get a few plain words per check, never the
        # owner's detail, which names a command to run (here: no record at a
        # registered place, so the census country cannot be told).
        self.person(ME, 'Ada Example')
        self.person(OTHER, 'Fern Example')
        self.source('s-1111111111', 'Marriage register')
        self.claim('c-1111111111', 's-1111111111', 'death', '1900-06-02', [ME])
        self.source('s-2222222222', 'Church letter', source_type='letter')
        self.claim('c-2222222222', 's-2222222222', 'marriage', '1875-06-01', [ME, OTHER])
        self.config(BOTH_ON)
        self.build()
        ring = self.ring_section()
        self.assertNotIn('fha ', ring)
        self.assertNotIn('`', ring)
        self.assertNotIn('remove', ring)
        self.assertIn('Censuses</strong>: not found yet - census country not known', ring)
        self.assertIn('<title>Censuses: not found yet - census country not known</title>', ring)
        self.assertIn('Marriage records</strong>: not found yet - no marriage record', ring)
        self.assertIn('Parents</strong>: not found yet - no parents named', ring)
        self.assertIn('Birth record</strong>: not found yet - no birth record', ring)
        self.assertIn('Death or burial</strong>: found</li>', ring)

    def test_the_linked_preview_keeps_the_owners_full_detail(self):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Death register')
        self.claim('c-1111111111', 's-1111111111', 'death', '1900-06-02', [ME])
        self.config(BOTH_ON)
        self.build(linked=True)
        self.assertIn('fha report --section place-candidates', self.ring_section())

    def ring_state(self, check):
        m = re.search(r'<g class="ring-(\w+)" data-check="' + check + '"',
                      self.page(f'persons/{ME}.html'))
        return m.group(1) if m else None

    def test_a_record_naming_a_living_person_does_not_fill_the_public_ring(self):
        # Her death record names a living informant. Her timeline leaves that
        # record off the shared site, so the ring must not count it either:
        # a visitor would read "found" with nothing on the page behind it.
        self.person(ME, 'Ada Example')
        self.person(LIVE, 'Cleo Example', living='true')
        self.source('s-1111111111', 'Death register')
        self.claim('c-1111111111', 's-1111111111', 'death', '1900-06-02', [ME, LIVE],
                   roles={ME: 'deceased', LIVE: 'informant'})
        self.config(BOTH_ON)
        self.build()
        self.assertEqual(self.ring_state('end-of-life'), 'missing')
        self.build(linked=True)
        self.assertEqual(self.ring_state('end-of-life'), 'met')

    def test_an_unreviewed_death_still_marks_a_death_certificate(self):
        # TOOLING §7a: a source that records her own death is her death record,
        # read from accepted and suggested claims alike, so the birth date it
        # gives is later evidence (partial), on the site as in fha metrics.
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Death certificate')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-03-14', [ME])
        self.claim('c-2222222222', 's-1111111111', 'death', '1900-06-02', [ME],
                   status='suggested')
        self.config(BOTH_ON)
        self.build()
        self.assertEqual(self.ring_state('birth'), 'partial')

    def test_never_married_by_the_flag_reads_as_confirmed(self):
        self.person(ME, 'Ada Example')
        self.conn.execute('UPDATE persons SET no_known_marriages = 1 WHERE id = ?', (ME,))
        self.config(BOTH_ON)
        self.build()
        self.assertEqual(self.ring_state('marriage'), 'met')
        self.assertIn('Marriage records</strong>: found - never married (confirmed)',
                      self.ring_section())

    def test_never_married_by_a_negated_claim_reads_as_confirmed(self):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Parish letter', source_type='letter')
        self.claim('c-1111111111', 's-1111111111', 'marriage', None, [ME], negated=1)
        self.config(BOTH_ON)
        self.build()
        self.assertEqual(self.ring_state('marriage'), 'met')
        self.assertIn('<title>Marriage records: found - never married (confirmed)</title>',
                      self.ring_section())

    def test_an_unreadable_setting_leaves_the_ring_off_with_a_warning(self):
        self.seed_me()
        self.config('site:\n  completeness_rings: maybe\n')
        res = self.build()
        self.assertNotIn('completeness-ring', self.page(f'persons/{ME}.html'))
        self.assertTrue(any('site.completeness_rings' in m for m in res['messages']),
                        res['messages'])


class RingSvgTests(unittest.TestCase):
    """`_lib.completeness_ring_svg`, the pure renderer the workbench reuses."""

    def result(self, *states):
        checks = [{'check': k, 'state': s, 'detail': f'{k} detail <x>'}
                  for k, s in zip(COMPLETENESS_CHECKS, states)]
        applicable = [c for c in checks if c['state'] != 'na']
        return {'checks': checks, 'applicable': len(applicable),
                'met': sum(1 for c in applicable if c['state'] == 'met')}

    def test_public_titles_use_the_plain_phrase_not_the_detail(self):
        r = self.result('met', 'missing', 'na', 'na', 'na')
        r['checks'][1]['countries'] = []
        svg = completeness_ring_svg(r, public=True)
        self.assertIn('<title>Censuses: not found yet - census country not known</title>', svg)
        self.assertIn('<title>Birth record: found</title>', svg)
        self.assertNotIn('detail', svg)

    def test_one_segment_per_check_with_escaped_titles_and_no_ids(self):
        svg = completeness_ring_svg(self.result('met', 'partial', 'missing', 'na', 'met'))
        self.assertEqual(svg.count('<g class="ring-'), 5)
        self.assertIn('Birth record: found - birth detail &lt;x&gt;', svg)
        self.assertIn('aria-label="How well documented: 2 of 4 checks found"', svg)
        self.assertNotIn(' id=', svg)          # safe to repeat on one page
        self.assertNotIn('<script', svg)

    def test_the_same_result_draws_the_same_markup(self):
        r = self.result('met', 'met', 'missing', 'partial', 'na')
        self.assertEqual(completeness_ring_svg(r, size=30), completeness_ring_svg(r, size=30))
        self.assertIn('width="30"', completeness_ring_svg(r, size=30))


class CalendarTests(_Base):
    def calendar_panel(self):
        """The home page's calendar section only: the linked preview lists
        living people elsewhere on the page, by design."""
        html = self.page('index.html')
        return html.split('id="this-month"', 1)[1].split('</section>', 1)[0]

    def test_off_by_default(self):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-14', [ME])
        self.build()
        self.assertNotIn('This month in the family', self.page('index.html'))

    def test_every_month_is_rendered_and_the_build_month_shows_first(self):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-14', [ME])
        self.config(BOTH_ON)
        self.build(today='2026-06-15')
        html = self.page('index.html')
        for month in range(1, 13):
            self.assertIn(f'id="cal-month-{month}"', html)
        current = html.split('<div id="cal-current">', 1)[1].split('</div>', 1)[0]
        self.assertIn('<h3>June</h3>', current)
        self.assertIn('14 June 1850', current)
        self.assertIn('Nothing on file for January yet.', html)
        self.assertIn("new Date().getMonth()", html)

    def test_an_accepted_event_names_the_person_with_a_link(self):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-14', [ME])
        self.config(BOTH_ON)
        self.build()
        self.assertIn(f'Born: <a href="persons/{ME}.html">Ada Example</a>',
                      self.page('index.html'))

    def test_an_unreviewed_event_is_left_off(self):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-14', [ME],
                   status='suggested')
        self.config(BOTH_ON)
        self.build()
        self.assertNotIn('14 June 1850', self.page('index.html'))

    def test_a_living_persons_event_is_left_off_in_every_build(self):
        self.person(LIVE, 'Cleo Example', living='true')
        self.person(UNK, 'Dora Example', living='unknown')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1990-06-14', [LIVE])
        self.claim('c-2222222222', 's-1111111111', 'birth', '1991-06-20', [UNK])
        self.config(BOTH_ON)
        for linked in (False, True):
            self.build(linked=linked)
            panel = self.calendar_panel()
            self.assertNotIn('1990', panel)
            self.assertNotIn('1991', panel)
            self.assertNotIn('Cleo Example', panel)
            self.assertNotIn('Dora Example', panel)

    def test_an_event_whose_record_names_a_living_person_is_left_off(self):
        # Her son's birth record names her as the mother; the son is dead,
        # but the claim names a living person, as a timeline line would not
        # be shown for.
        self.person(ME, 'Ada Example')
        self.person(LIVE, 'Cleo Example', living='true')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1950-06-14', [ME, LIVE],
                   roles={ME: 'child', LIVE: 'parent'})
        self.config(BOTH_ON)
        self.build()
        self.assertNotIn('14 June 1950', self.page('index.html'))

    def test_a_restricted_claims_event_is_left_off(self):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register', restricted_claims=('c-1111111111',))
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-14', [ME])
        self.config(BOTH_ON)
        self.build()
        self.assertNotIn('14 June 1850', self.page('index.html'))

    def test_a_restricted_sources_event_is_left_off(self):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Adoption file', restricted=1)
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-14', [ME])
        self.config(BOTH_ON)
        self.build()
        self.assertNotIn('14 June 1850', self.page('index.html'))

    def test_one_private_copy_of_an_event_keeps_the_whole_event_off(self):
        # The same birth from an open register and a restricted file: a
        # second, open copy does not make the private one public.
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-14', [ME])
        self.source('s-2222222222', 'Adoption file', restricted=1)
        self.claim('c-2222222222', 's-2222222222', 'birth', '1850-06-14', [ME])
        self.config(BOTH_ON)
        self.build()
        self.assertNotIn('14 June 1850', self.page('index.html'))

    def test_a_by_request_persons_event_is_left_off(self):
        self.person(QUIET, 'Edna Example', extra='restricted: by-request\n')
        self.source('s-1111111111', 'Death register')
        self.claim('c-1111111111', 's-1111111111', 'death', '1930-06-14', [QUIET])
        self.config(BOTH_ON)
        self.build()
        html = self.page('index.html')
        self.assertNotIn('14 June 1930', html)
        self.assertNotIn('Edna Example', html)

    def test_a_stub_is_named_without_a_link(self):
        self.person(ME, 'Ada Example')
        self.person(OTHER, 'Fern Example', tier='stub')
        self.source('s-1111111111', 'Marriage register')
        self.claim('c-1111111111', 's-1111111111', 'marriage', '1875-06-01', [ME, OTHER])
        self.config(BOTH_ON)
        self.build()
        html = self.page('index.html')
        self.assertIn(f'<a href="persons/{ME}.html">Ada Example</a>', html)
        self.assertIn('<span class="stub-ref">Fern Example</span>', html)
        self.assertNotIn(f'persons/{OTHER}.html', html)


if __name__ == '__main__':
    unittest.main()
