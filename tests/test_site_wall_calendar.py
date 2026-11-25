"""
test_site_wall_calendar.py - the family site's wall calendar page
(`site: calendar: true` also writes calendar.html; TOOLING §12).

A relative opens one page and sees the whole year of the family's births,
weddings and deaths, a small face beside each name. It is held to the same
privacy rules as the home page's month panel: the living, the restricted and
the by-request never appear, and nothing hints at how many events were left
off. Expected answers come from those rules (SPEC §19, §21) and from
TOOLING §7b, not from the code's output.

Reuses the synthetic-index fixtures of tests/test_site_rings_calendar.py.
"""

import os
import sqlite3
import sys
import time
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

from test_site_rings_calendar import (   # noqa: E402
    BOTH_ON, LIVE, ME, OTHER, QUIET, UNK, _Base, site,
)

ON = 'site:\n  calendar: true\n'


class WallCalendarTests(_Base):
    def seed_ada(self, date='1850-06-14'):
        self.person(ME, 'Ada Example')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', date, [ME])

    def test_off_by_default_and_on_with_calendar_true(self):
        self.seed_ada()
        self.build()
        self.assertFalse((self.out / 'calendar.html').exists())
        self.config(ON)
        self.build()
        self.assertTrue((self.out / 'calendar.html').exists())

    def test_home_panel_links_to_the_wall_calendar(self):
        self.seed_ada()
        self.config(ON)
        self.build()
        self.assertIn('<a href="calendar.html">See the whole year as a wall calendar.</a>',
                      self.page('index.html'))

    def test_the_year_is_laid_out_with_events_in_their_day_cells(self):
        self.seed_ada()
        self.person(OTHER, 'Fern Example')
        self.claim('c-2222222222', 's-1111111111', 'death', '1921-06-14', [OTHER])
        self.config(ON)
        self.build(today='2026-06-15')
        html = self.page('calendar.html')
        self.assertIn('<title>Family calendar 2026', html)
        self.assertEqual(html.count('<table class="wall-month">'), 12)
        self.assertIn('<h2>June 2026</h2>', html)
        self.assertIn(f'b. <a href="persons/{ME}.html">Ada Example</a> (1850)', html)
        self.assertIn(f'd. <a href="persons/{OTHER}.html">Fern Example</a> (1921)', html)
        self.assertIn('Nothing on file for January yet.', html)

    def test_year_only_dates_are_left_off_and_month_only_ones_are_listed(self):
        self.seed_ada(date='1850')
        self.person(OTHER, 'Fern Example')
        self.claim('c-2222222222', 's-1111111111', 'death', '1921-06', [OTHER])
        self.config(ON)
        self.build()
        html = self.page('calendar.html')
        self.assertNotIn('Ada Example', html)
        self.assertIn('Also in June, day not recorded:', html)
        self.assertIn('Fern Example', html)

    def test_every_exclusion_the_panel_makes_the_wall_calendar_makes(self):
        self.person(LIVE, 'Cleo Example', living='true')
        self.person(UNK, 'Dora Example', living='unknown')
        self.person(QUIET, 'Edna Example', extra='restricted: by-request\n')
        self.person(ME, 'Ada Example')
        self.person(OTHER, 'Fern Example')
        self.source('s-1111111111', 'Register')
        self.source('s-2222222222', 'Adoption file', restricted=1)
        self.source('s-3333333333', 'Death register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1990-06-14', [LIVE])
        self.claim('c-2222222222', 's-1111111111', 'birth', '1991-06-20', [UNK])
        self.claim('c-3333333333', 's-1111111111', 'death', '1930-06-21', [QUIET])
        self.claim('c-4444444444', 's-2222222222', 'birth', '1850-06-22', [ME])
        self.claim('c-5555555555', 's-1111111111', 'birth', '1851-06-23', [OTHER],
                   status='suggested')
        # A death that names a by-request informant keeps the record home.
        self.claim('c-6666666666', 's-3333333333', 'death', '1930-06-24', [ME, QUIET],
                   roles={ME: 'deceased', QUIET: 'informant'})
        self.config(ON)
        self.build()
        html = self.page('calendar.html')
        for name in ('Cleo', 'Dora', 'Edna', 'Ada', 'Fern'):
            self.assertNotIn(name, html)
        for year in ('1990', '1991', '1930', '1850', '1851'):
            self.assertNotIn(f'({year})', html)
        # Nothing left to show, and nothing says how much was held back.
        self.assertIn('Nothing on file for June yet.', html)
        body = html.split('<main>', 1)[1].split('</main>', 1)[0]
        self.assertNotRegex(body, r'(?i)left off|hidden|withheld|private|living')

    def test_the_living_are_off_the_calendar_in_a_linked_preview_too(self):
        self.person(LIVE, 'Cleo Example', living='true')
        self.source('s-1111111111', 'Register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1990-06-14', [LIVE])
        self.config(ON)
        self.build(linked=True)
        self.assertNotIn('Cleo', self.page('calendar.html'))

    def test_a_record_that_also_names_a_living_person_names_only_the_dead(self):
        self.person(ME, 'Ada Example')
        self.person(LIVE, 'Cleo Example', living='true')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1950-06-14', [ME, LIVE],
                   roles={ME: 'child', LIVE: 'parent'})
        self.config(ON)
        self.build()
        html = self.page('calendar.html')
        self.assertIn('Ada Example', html)
        self.assertNotIn('Cleo', html)

    def test_feb_29_goes_on_feb_28_in_a_common_year_and_stays_in_a_leap_year(self):
        self.seed_ada(date='1852-02-29')
        self.config(ON)
        self.build(today='2027-03-01')
        common = self.page('calendar.html')
        self.assertIn('Ada Example</a> (29 February) (1852)', common)
        self.build(today='2028-03-01')
        leap = self.page('calendar.html')
        self.assertIn('Ada Example</a> (1852)', leap)
        self.assertNotIn('(29 February)', leap)

    def test_more_than_three_in_a_day_shows_and_more_and_counts_only_shown(self):
        self.person(ME, 'Ada Example')
        self.person(OTHER, 'Fern Example')
        self.person(LIVE, 'Cleo Example', living='true')
        self.source('s-1111111111', 'Register')
        for i, pid in enumerate((ME, OTHER)):
            for j in range(2):
                self.claim(f'c-{i}{j}00000000', 's-1111111111', 'birth',
                           f'18{i}{j}-06-14', [pid])
        # Four people's worth of events on 14 June, plus a living one.
        self.claim('c-9900000000', 's-1111111111', 'birth', '1990-06-14', [LIVE])
        self.config(ON)
        self.build()
        html = self.page('calendar.html')
        self.assertIn('and 1 more', html)
        self.assertNotIn('and 2 more', html)

    def test_switching_off_removes_calendar_html(self):
        self.seed_ada()
        self.config(ON)
        self.build()
        self.assertTrue((self.out / 'calendar.html').exists())
        self.config('')
        self.build()
        self.assertFalse((self.out / 'calendar.html').exists())

    def test_a_person_who_becomes_private_is_gone_after_a_rebuild(self):
        self.seed_ada()
        self.config(ON)
        self.build()
        self.assertIn('Ada Example', self.page('calendar.html'))
        self.conn.execute("UPDATE persons SET living='true' WHERE id=?", (ME,))
        self.build()
        self.assertNotIn('Ada Example', self.page('calendar.html'))

    def test_the_workbench_keeps_its_week_panel_and_writes_no_wall_page(self):
        self.seed_ada()
        self.config(ON)
        self.build()
        self.out.joinpath('calendar.html').unlink()
        with unittest.mock.patch.object(site, '_today', return_value='2026-06-15'):
            site.run_site(self.root, self.out, linked=True, workbench=True)
        self.assertFalse((self.out / 'calendar.html').exists())
        self.assertNotIn('wall calendar', self.page('index.html'))

    def test_a_rebuild_of_an_unchanged_archive_is_byte_identical(self):
        self.seed_ada()
        self.config(ON)
        self.build()
        first = (self.out / 'calendar.html').read_bytes()
        self.build()
        self.assertEqual(first, (self.out / 'calendar.html').read_bytes())

    def test_the_dry_run_page_count_matches_the_pages_written(self):
        self.seed_ada()
        self.config(ON)
        res = self.build()
        self.assertEqual(res['pages'], len(list(self.out.rglob('*.html'))))

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_a_chosen_portrait_shows_beside_the_name(self):
        from PIL import Image
        img = self.root / 'photos' / '1950' / 'ada.png'
        img.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (60, 40), (10, 20, 30)).save(img)
        self.person(ME, 'Ada Example', extra='profile_photo: ada.png\n')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-14', [ME])
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n' + ON, encoding='utf-8')
        self.build()
        self.assertIn('class="wall-face"', self.page('calendar.html'))

    @unittest.skipUnless(site._PIL_AVAILABLE, 'Pillow not installed')
    def test_a_portrait_photo_that_also_shows_a_living_person_is_not_used(self):
        # Ada has died; her chosen portrait is a group photo tagged to a
        # living cousin. The calendar shows her name and no face.
        from PIL import Image
        from photoindex import _DDL as PHOTOS_DDL
        img = self.root / 'photos' / '1950' / 'ada.png'
        img.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (60, 40), (10, 20, 30)).save(img)
        self.person(ME, 'Ada Example', extra='profile_photo: photos/1950/ada.png\n')
        self.person(LIVE, 'Cleo Example', living='true')
        self.source('s-1111111111', 'Birth register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1850-06-14', [ME])
        (self.root / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n' + ON, encoding='utf-8')
        pconn = sqlite3.connect(str(self.root / '.cache' / 'photos.sqlite'))
        pconn.executescript(PHOTOS_DDL)
        pconn.execute('INSERT INTO photos(path, group_id, is_primary, caption) '
                      'VALUES (?,?,?,?)', ('photos/1950/ada.png', 'g1', 1, ''))
        for pid in (ME, LIVE):
            pconn.execute('INSERT INTO photo_people(path, person_ref, via) VALUES (?,?,?)',
                          ('photos/1950/ada.png', pid, 'pid-keyword'))
        pconn.commit()
        pconn.close()
        future = time.time() + 10_000
        os.utime(self.root / '.cache' / 'photos.sqlite', (future, future))
        self.build()
        html = self.page('calendar.html')
        self.assertIn('Ada Example', html)
        self.assertNotIn('wall-face', html)

    def test_month_only_events_pass_the_same_privacy_filter(self):
        self.person(LIVE, 'Cleo Example', living='true')
        self.person(QUIET, 'Edna Example', extra='restricted: by-request\n')
        self.person(OTHER, 'Fern Example')
        self.source('s-1111111111', 'Register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1990-06', [LIVE])
        self.claim('c-2222222222', 's-1111111111', 'death', '1930-06', [QUIET])
        self.source('s-2222222222', 'Death register')
        self.claim('c-3333333333', 's-2222222222', 'death', '1931-06', [OTHER])
        self.config(ON)
        self.build()
        html = self.page('calendar.html')
        self.assertIn('Fern Example', html)
        for leaked in ('Cleo', 'Edna', '(1990)', '(1930)'):
            self.assertNotIn(leaked, html)

    def test_feb_29_of_a_living_person_never_reaches_the_page(self):
        self.person(LIVE, 'Cleo Example', living='true')
        self.source('s-1111111111', 'Register')
        self.claim('c-1111111111', 's-1111111111', 'birth', '1992-02-29', [LIVE])
        self.config(ON)
        self.build(today='2027-03-01')
        html = self.page('calendar.html')
        self.assertNotIn('Cleo', html)
        self.assertNotIn('(29 February)', html)

    def test_no_portrait_means_no_placeholder(self):
        self.seed_ada()
        self.config(ON)
        self.build()
        self.assertNotIn('wall-face', self.page('calendar.html'))


class BothPanelsStillWork(_Base):
    def test_rings_and_calendar_together_still_build(self):
        self.person(ME, 'Ada Example')
        self.config(BOTH_ON)
        self.build()
        self.assertTrue((self.out / 'calendar.html').exists())


if __name__ == '__main__':
    unittest.main()
