"""
test_workbench_rings_calendar.py - the completeness ring on every workbench
person page and "This week in the family" on the workbench home page
(TOOLING_INTERFACE §1b).

The workbench (`fha serve`) is the owner's private view, so:
  - the ring needs no fha.yaml switch, shows the owner's full detail (as
    `fha metrics` does), and under each gap says what kind of record would
    fill it, pointing at the review queue when a suggested claim already
    would. The score stays accepted-only (TOOLING §7a).
  - the week panel lists today and the six days after it from accepted
    claims dated to the day (`_lib.family_calendar_week`, the week `fha
    report` shows, TOOLING §7b), names living relatives like anyone else,
    and links each person to their workbench page.
None of the workbench additions may reach a shared or plain linked build.
Expected answers come from those rules, not from the code's output.

Builds a synthetic .cache/index.sqlite from index.py's DDL, the way
tests/test_site_rings_calendar.py does; the serve-level tests copy
example-archive into a temp dir. Temp fixtures only.
"""

import datetime
import importlib.util
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

EXAMPLE = ROOT / 'example-archive'

from index import _DDL as INDEX_DDL  # noqa: E402
from _lib import index_manifest_path, record_path_manifest, write_path_manifest  # noqa: E402

# site.py's stem collides with the stdlib `site` module, so it is loaded by
# path under a private name (as tests/test_site.py and fha.py do).
_spec = importlib.util.spec_from_file_location('fha_site_wb_rings', ROOT / 'tools' / 'site.py')
site = importlib.util.module_from_spec(_spec)
sys.modules['fha_site_wb_rings'] = site
_spec.loader.exec_module(site)

ME = 'p-aaaaaaaaaa'       # deceased, curated
DAD = 'p-bbbbbbbbbb'      # deceased, curated
LIVE = 'p-cccccccccc'     # living
OTHER = 'p-ffffffffff'    # deceased, curated

POLAND = 'l-aaaaaaaaaa'

_CTX = {'port': 8765, 'csrf_token': 'abc123', 'review_count': 0, 'inbox_count': 0}


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
        self.config('')

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    # - seeding -

    def config(self, text: str) -> None:
        (self.root / 'fha.yaml').write_text('roots: {}\n' + text, encoding='utf-8')

    def person(self, pid, name, *, living='false', tier='curated'):
        rel = f'people/{name.split()[-1].lower()}_{pid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nid: {pid}\nname: "{name}"\n---\n# {name}\n', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO persons(id, name, surname, sex, living, tier, status, path) '
            'VALUES (?,?,?,?,?,?,?,?)',
            (pid, name, name.split()[-1], 'F', living, tier, 'active', rel))

    def source(self, sid, title, *, source_type='vital-record'):
        rel = f'sources/{source_type}/src_{sid}.md'
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'---\nid: {sid}\ntitle: {title}\nsource_type: {source_type}\n'
                        'citation: "A citation."\n---\n', encoding='utf-8')
        self.conn.execute(
            'INSERT INTO sources(id, title, source_type, restricted, status, path) '
            'VALUES (?,?,?,?,?,?)', (sid, title, source_type, 0, 'active', rel))

    def claim(self, cid, sid, ctype, date, persons, *, status='accepted', roles=None,
              place_id=None):
        lo = hi = None
        if date:
            lo = (date + '-01-01')[:10] if len(date) == 4 else (date + '-01')[:10]
            hi = lo
        self.conn.execute(
            'INSERT INTO claims(id, source_id, type, value, status, date_edtf, date_min, '
            'date_max, place_id, negated) VALUES (?,?,?,?,?,?,?,?,?,0)',
            (cid, sid, ctype, f'{ctype} record', status, date, lo, hi, place_id))
        for pos, pid in enumerate(persons):
            self.conn.execute(
                'INSERT INTO claim_persons(claim_id, person_id, position, role) '
                'VALUES (?,?,?,?)', (cid, pid, pos, (roles or {}).get(pid)))

    def place(self, lid, name, hierarchy):
        self.conn.execute('INSERT INTO places(id, name, hierarchy) VALUES (?,?,?)',
                          (lid, name, hierarchy))

    def build(self, *, workbench=True, linked=True, today='2026-06-15'):
        self.conn.commit()
        future = time.time() + 5
        os.utime(self.root / '.cache' / 'index.sqlite', (future, future))
        write_path_manifest(index_manifest_path(self.root), record_path_manifest(self.root))
        with unittest.mock.patch.object(site, '_today', return_value=today):
            res = site.run_site(self.root, self.out, linked=linked, workbench=workbench,
                                workbench_context=_CTX if workbench else None)
        self.assertTrue(res.ok, res['messages'])
        return res

    def page(self, rel):
        return (self.out / rel).read_text(encoding='utf-8')


class WorkbenchRingTests(_Base):
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

    def ring_section(self, pid=ME):
        html = self.page(f'persons/{pid}.html')
        self.assertIn('<details class="chart completeness"', html)
        return html.split('<details class="chart completeness"', 1)[1].split('</details>', 1)[0]

    def legend_line(self, label):
        m = re.search(r'<li class="completeness-\w+"><strong>' + re.escape(label)
                      + r'</strong>.*?</li>', self.ring_section(), re.S)
        self.assertIsNotNone(m, label)
        return m.group(0)

    def test_the_ring_shows_with_no_fha_yaml_switch(self):
        self.seed_me()
        self.build()
        html = self.page(f'persons/{ME}.html')
        states = {check: state for state, check in
                  re.findall(r'<g class="ring-(\w+)" data-check="([\w-]+)"', html)}
        self.assertEqual(states, {'birth': 'met', 'census': 'na', 'parents': 'partial',
                                  'marriage': 'missing', 'end-of-life': 'met'})
        self.assertIn('How well documented: 2 of 4 found', html)

    def test_a_plain_linked_build_still_follows_the_switch(self):
        self.seed_me()
        self.build(workbench=False)
        self.assertNotIn('completeness-ring', self.page(f'persons/{ME}.html'))

    def test_the_legend_keeps_the_owners_full_detail(self):
        self.seed_me()
        self.build()
        self.assertIn('Parents</strong>: partly found - one parent named; named in '
                      'other records, not a birth record', self.ring_section())
        self.assertIn('<title>Parents: partly found - one parent named', self.ring_section())

    def test_each_gap_says_what_would_fill_it(self):
        self.seed_me()
        self.build()
        self.assertIn('To fill it, look for a marriage licence, certificate or church '
                      'register entry.', self.legend_line('Marriage records'))
        self.assertIn('To fill it, look for a record that names the parents',
                      self.legend_line('Parents'))
        # Found and does-not-apply checks need nothing.
        for label in ('Birth record', 'Censuses', 'Death or burial'):
            self.assertNotIn('look for', self.legend_line(label), label)
        self.assertIn('<a href="../inbox">inbox</a>', self.ring_section())

    def test_a_suggested_record_points_to_the_review_queue(self):
        # A suggested marriage record would fill the marriage check; the
        # ring still counts accepted claims only, so the check stays missing
        # and the legend sends the owner to review it.
        self.seed_me()
        self.person(OTHER, 'Fern Example')
        self.source('s-4444444444', 'Marriage register')
        self.claim('c-4444444444', 's-4444444444', 'marriage', '1875-06-01', [ME, OTHER],
                   status='suggested')
        self.build()
        line = self.legend_line('Marriage records')
        self.assertIn('A suggested claim waiting for review would count here', line)
        self.assertIn('<a href="../review">review it</a>', line)
        self.assertRegex(self.page(f'persons/{ME}.html'),
                         r'<g class="ring-missing" data-check="marriage"')
        self.assertNotIn('waiting for review', self.legend_line('Parents'))

    def test_no_fill_lines_in_a_shared_or_plain_linked_build(self):
        self.seed_me()
        self.config('site:\n  completeness_rings: true\n')
        for linked in (False, True):
            self.build(workbench=False, linked=linked)
            ring = self.ring_section()
            self.assertNotIn('look for', ring)
            self.assertNotIn('inbox', ring)
            self.assertNotIn('review', ring)

    def test_a_living_person_still_gets_no_ring(self):
        # TOOLING §12: no ring and no computation for a living or unknown
        # person in any build; the workbench keeps that rule.
        self.person(LIVE, 'Cleo Example', living='true')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1990-03-14', [LIVE])
        self.build()
        self.assertNotIn('completeness-ring', self.page(f'persons/{LIVE}.html'))


class WorkbenchWeekTests(_Base):
    def panel(self):
        html = self.page('index.html')
        self.assertIn('id="this-week"', html)
        return html.split('id="this-week"', 1)[1].split('</section>', 1)[0]

    def test_an_event_in_the_week_is_listed_and_links_the_person(self):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-17', [ME])
        self.build(today='2026-06-15')
        panel = self.panel()
        self.assertIn('15 June to 21 June', panel)
        self.assertIn(f'<span class="cal-date">17 June 1850</span> - Born: '
                      f'<a href="persons/{ME}.html">Ada Example</a> - 176 years ago', panel)

    def test_events_outside_the_week_or_not_dated_to_the_day_are_left_off(self):
        self.person(ME, 'Ada Example')
        self.person(OTHER, 'Fern Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-22', [ME])   # day 8
        self.claim('c-2222222222', 's-1111111111', 'birth', '1851-06', [OTHER])   # month only
        self.claim('c-3333333333', 's-1111111111', 'death', '1900-06-16', [OTHER],
                   status='suggested')                                            # unreviewed
        self.build(today='2026-06-15')
        panel = self.panel()
        self.assertNotIn('1850', panel)
        self.assertNotIn('1851', panel)
        self.assertNotIn('1900', panel)
        self.assertIn('Nothing on file for 15 June to 21 June', panel)

    def test_a_living_relative_is_named_in_the_private_view(self):
        self.person(LIVE, 'Cleo Example', living='true')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1990-06-16', [LIVE])
        self.build(today='2026-06-15')
        self.assertIn(f'Born: <a href="persons/{LIVE}.html">Cleo Example</a> - 36 years ago',
                      self.panel())

    def test_no_dated_events_at_all_still_builds_the_page(self):
        self.build(today='2026-06-15')
        self.assertIn('Nothing on file for 15 June to 21 June', self.panel())

    def test_a_week_across_new_year_counts_january_against_next_year(self):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1880-01-02', [ME])
        self.build(today='2026-12-28')
        panel = self.panel()
        self.assertIn('28 December to 3 January', panel)
        self.assertIn('2 January 1880', panel)
        self.assertIn('147 years ago', panel)

    def test_names_are_html_escaped(self):
        self.person(ME, 'Ada <b>Bold</b> & Co')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-17', [ME])
        self.build(today='2026-06-15')
        panel = self.panel()
        self.assertIn('Ada &lt;b&gt;Bold&lt;/b&gt; &amp; Co', panel)
        self.assertNotIn('<b>Bold</b>', panel)

    def test_the_week_replaces_the_month_panel_and_stays_out_of_shared_builds(self):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-17', [ME])
        self.config('site:\n  calendar: true\n')
        self.build(today='2026-06-15')
        html = self.page('index.html')
        self.assertIn('This week in the family', html)
        self.assertNotIn('This month in the family', html)
        for linked in (False, True):
            self.build(workbench=False, linked=linked, today='2026-06-15')
            html = self.page('index.html')
            self.assertNotIn('This week in the family', html)
            self.assertIn('This month in the family', html)

    def test_the_week_matches_fha_report(self):
        # The report's on-this-day section and the page read one helper.
        import report
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-17', [ME])
        self.claim('c-2222222222', 's-1111111111', 'death', '1900-06-21', [ME])
        self.build(today='2026-06-15')
        lines = report._section_on_this_day(self.conn, datetime.date(2026, 6, 15))
        self.assertEqual(len(lines), 2)
        panel = self.panel()
        for text in ('17 June 1850', '21 June 1900'):
            self.assertIn(text, panel)
            self.assertTrue(any(text in ln for ln in lines), lines)


class ServeSnapshotTests(unittest.TestCase):
    """Through `fha serve` itself: the snapshot it builds carries both panels,
    and a snapshot built on an earlier day is rebuilt."""

    @classmethod
    def setUpClass(cls) -> None:
        if not EXAMPLE.is_dir():
            raise unittest.SkipTest('example-archive not present')

    def setUp(self) -> None:
        import index as index_mod
        import serve
        from _lib import load_fha_yaml
        self.serve = serve
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / 'arc'
        shutil.copytree(EXAMPLE, self.root)
        config = load_fha_yaml(self.root, strict=True)
        index_mod.build_index(self.root, config)
        self.state = serve.ServeState(self.root, config, 0)
        serve.ensure_snapshot(self.state)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_snapshot_has_the_week_panel_and_rings(self):
        snap = self.state.snapshot_dir
        self.assertIn('This week in the family', (snap / 'index.html').read_text(encoding='utf-8'))
        ringed = [p for p in (snap / 'persons').glob('*.html')
                  if 'class="completeness-ring"' in p.read_text(encoding='utf-8')]
        self.assertTrue(ringed, 'no workbench person page carries a ring')

    def test_a_snapshot_built_on_an_earlier_day_is_stale(self):
        self.assertFalse(self.serve.snapshot_is_stale(self.state))
        real_date = datetime.date

        class _Tomorrow(real_date):
            @classmethod
            def today(cls):
                return real_date.today() + datetime.timedelta(days=1)

        fake = types.SimpleNamespace(date=_Tomorrow, datetime=datetime.datetime,
                                     timedelta=datetime.timedelta)
        with unittest.mock.patch.object(self.serve, 'datetime', fake):
            self.assertTrue(self.serve.snapshot_is_stale(self.state))


if __name__ == '__main__':
    unittest.main()
