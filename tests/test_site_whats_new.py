"""
test_site_whats_new.py: the optional "What's new" page on the family site
(TOOLING 12 "Optional panels", `site.whats_new`).

What a relative sees: Cousin Ann comes back twice a year, opens "What's new on
this site" and reads, month by month, which facts were added about whom, which
people got a page and which records arrived. What she must never see: any sign
that something about a living, unknown, restricted or by-request person
changed. No count of hidden changes, no "3 more updates", no month that only
exists because of a hidden edit. And two builds of the same records, run on
different days, write the same page.

Builds a synthetic .cache/index.sqlite from index.py's DDL, the way
tests/test_site_search.py does. Temp fixtures only.
"""

import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'tools'))

import test_site_search as base   # the same fixture helpers and site module

site = base.site
MARGARET, THOMAS, LIVE, UNK, SECRET, QUIET = (
    base.MARGARET, base.THOMAS, base.LIVE, base.UNK, base.SECRET, base.QUIET)
ON = 'site:\n  whats_new: true\n'
S1, S2 = 's-aaaaaaaaaa', 's-bbbbbbbbbb'


class _Base(base._Base):
    def claim(self, cid, sid, ctype, date, persons, *, reviewed=None, status='accepted', roles=None):
        super().claim(cid, sid, ctype, date, persons, status=status, roles=roles)
        self.conn.execute('UPDATE claims SET reviewed = ? WHERE id = ?', (reviewed, cid))

    def page(self, out=None) -> str:
        return ((out or self.out) / 'whats-new.html').read_text(encoding='utf-8')

    def month(self, page: str, label: str) -> str:
        """The text of one month's section."""
        start = page.index(f'<h2>{label}</h2>')
        end = page.find('<section', start)
        return page[start:] if end < 0 else page[start:end]

    def seed(self):
        self.person(MARGARET, 'Margaret Cole')
        self.person(THOMAS, 'Thomas Edward Hartley', sex='M')
        self.source(S1, '1880 census, Elm Creek', source_type='census')
        self.source(S2, 'Margaret Cole birth register')
        self.claim('c-aaaaaaaaaa', S2, 'birth', '1845', [MARGARET], reviewed='2026-09-03')
        self.claim('c-bbbbbbbbbb', S1, 'census', '1880', [MARGARET, THOMAS], reviewed='2026-10-05')
        self.claim('c-cccccccccc', S2, 'death', '1921', [MARGARET], reviewed='2026-10-20')
        self.claim('c-dddddddddd', S2, 'marriage', '1866', [MARGARET, THOMAS], reviewed='2026-10-21')


class SwitchTests(_Base):
    def test_off_by_default_and_home_page_has_no_link(self):
        self.seed()
        self.build()
        self.assertFalse((self.out / 'whats-new.html').exists())
        self.assertNotIn('whats-new', (self.out / 'index.html').read_text(encoding='utf-8'))

    def test_on_writes_the_page_and_links_it_from_home(self):
        self.seed()
        self.config(ON)
        self.build()
        self.assertIn("<h1>What's new</h1>", self.page())
        home = (self.out / 'index.html').read_text(encoding='utf-8')
        self.assertIn("<a href=\"whats-new.html\">What's new on this site</a>", home)

    def test_switching_off_removes_the_old_page(self):
        self.seed()
        self.config(ON)
        self.build()
        self.config('')
        self.build()
        self.assertFalse((self.out / 'whats-new.html').exists())

    def test_page_counts_match(self):
        self.seed()
        for cfg in ('', ON):
            with self.subTest(cfg or 'off'):
                self.config(cfg)
                preview = self.build(dry_run=True)['pages']
                real = self.build()['pages']
                self.assertEqual(preview, real)
                self.assertEqual(real, len(list(self.out.rglob('*.html'))))

    def test_empty_archive_says_nothing_added(self):
        self.config(ON)
        self.build()
        self.assertIn('Nothing has been added yet.', self.page())

    def test_an_unreadable_value_leaves_it_off_with_a_warning(self):
        self.seed()
        self.config('site:\n  whats_new: maybe\n')
        result = self.build()
        self.assertFalse((self.out / 'whats-new.html').exists())
        self.assertTrue(any('site.whats_new' in m for m in result['messages']), result['messages'])


class ContentTests(_Base):
    def setUp(self):
        super().setUp()
        self.seed()
        self.config(ON)

    def test_months_newest_first_with_facts_grouped_by_person(self):
        self.build()
        page = self.page()
        self.assertLess(page.index('<h2>October 2026'), page.index('<h2>September 2026'))
        self.assertRegex(page, r'Margaret Cole</a>: 3 facts added \(census, died, married\)')
        self.assertRegex(page, r'Margaret Cole</a>: 1 fact added \(born\)')
        self.assertRegex(page, r'Thomas Edward Hartley</a>: 2 facts added \(census, married\)')

    def test_new_page_and_new_record_use_the_earliest_reviewed_month(self):
        self.build()
        page = self.page()
        sep = self.month(page, 'September 2026')
        self.assertIn('New page: <a', sep)
        self.assertIn('Margaret Cole', sep)
        self.assertIn('New record: <a', sep)
        self.assertIn('Margaret Cole birth register', sep)
        octo = self.month(page, 'October 2026')
        self.assertIn('New page: <a', octo)
        self.assertIn('Thomas Edward Hartley', octo.split('New page:')[1])
        self.assertIn('1880 census, Elm Creek', octo)
        self.assertNotIn('birth register', octo)

    def test_an_accepted_claim_with_no_reviewed_date_is_ignored(self):
        self.claim('c-eeeeeeeeee', S2, 'residence', '1850', [MARGARET], reviewed=None)
        self.build()
        self.assertNotIn('residence', self.page())
        self.assertNotIn('4 facts', self.page())

    def test_an_unaccepted_claim_is_ignored(self):
        self.claim('c-eeeeeeeeee', S2, 'residence', '1850', [MARGARET],
                   reviewed='2026-12-01', status='suggested')
        self.build()
        self.assertNotIn('December 2026', self.page())

    def test_only_the_twelve_most_recent_months(self):
        for n in range(14):
            y, m = divmod(n, 12)
            self.claim(f'c-m{n:09d}', S2, 'residence', '1850', [THOMAS],
                       reviewed=f'{2020 + y}-{m + 1:02d}-10')
        self.build()
        page = self.page()
        self.assertEqual(page.count('class="wn-month"'), 12)
        self.assertIn('Earlier changes are not listed.', page)

    def test_discovery_entries_land_in_their_month(self):
        notes = self.root / 'notes'
        notes.mkdir()
        (notes / 'discoveries.md').write_text(
            '# Discoveries\n- 2026-10-02: Found the 1880 census page for the Hartleys.\n'
            '- an undated scrap\n', encoding='utf-8')
        self.build()
        page = self.page()
        self.assertIn('Found the 1880 census page', self.month(page, 'October 2026'))
        self.assertNotIn('undated scrap', page)

    def test_a_date_inside_a_discovery_does_not_become_its_month(self):
        # A hand-kept log quotes old dates in its text and in sub-bullets.
        # Cousin Ann must not find "April 1921" among the recent months; the
        # entry's month is the date it starts with, and its sub-bullets and
        # continuation lines stay with it, as on the discoveries page.
        notes = self.root / 'notes'
        notes.mkdir()
        (notes / 'discoveries.md').write_text(
            '# Discoveries\n'
            '- 2026-10-02: Found Margaret Cole\'s death record.\n'
            '  - Death certificate dated 1921-04-12 names her parents.\n'
            '  It was filed under her married name.\n'
            '- Found Thomas Hartley\'s baptism of 1847-05-09 in the parish book.\n',
            encoding='utf-8')
        self.build()
        page = self.page()
        self.assertNotIn('1921</h2>', page)
        self.assertNotIn('1847</h2>', page)
        october = self.month(page, 'October 2026')
        self.assertIn('names her parents', october)
        self.assertIn('filed under her married name', october)
        self.assertNotIn('parish book', page)

    def test_a_log_written_with_indented_bullets_still_lists(self):
        # The discoveries page and the home teaser accept a bullet that starts
        # with a space or a tab; What's new must not quietly drop them.
        for indent in (' ', '\t'):
            with self.subTest(indent=repr(indent)):
                notes = self.root / 'notes'
                notes.mkdir(exist_ok=True)
                (notes / 'discoveries.md').write_text(
                    f'# Discoveries\n{indent}- 2026-10-02: Found the Hartley farm deed.\n'
                    f'{indent}  - Signed 1871-03-04 by both brothers.\n'
                    f'{indent}- 2026-09-01: Found the 1880 census page.\n',
                    encoding='utf-8')
                self.build()
                page = self.page()
                self.assertIn('Hartley farm deed', self.month(page, 'October 2026'))
                self.assertIn('both brothers', self.month(page, 'October 2026'))
                self.assertIn('1880 census page', self.month(page, 'September 2026'))
                self.assertNotIn('1871</h2>', page)

    def test_uneven_bullet_indents_keep_entries_and_months_apart(self):
        # First bullet three spaces in, the rest flush; a one-space sibling.
        notes = self.root / 'notes'
        notes.mkdir()
        (notes / 'discoveries.md').write_text(
            '# Discoveries\n   - 2026-10-01: Found the Hartley farm deed.\n'
            '- 2026-09-01: Found the 1880 census page.\n'
            '  - 1921-04-12 is the date on the copy.\n'
            ' - 2026-08-01: Found the Cole family Bible.\n', encoding='utf-8')
        self.build()
        page = self.page()
        self.assertNotIn('1921</h2>', page)
        self.assertIn('Hartley farm deed', self.month(page, 'October 2026'))
        self.assertIn('date on the copy', self.month(page, 'September 2026'))
        self.assertIn('Cole family Bible', self.month(page, 'August 2026'))
        self.assertNotIn('Cole family Bible', self.month(page, 'September 2026'))

    def test_since_select_needs_script_and_page_works_without_it(self):
        self.build()
        page = self.page()
        self.assertIn('Show changes since:', page)
        label = page.split('Show changes since:')[0].rsplit('<label', 1)[1]
        self.assertIn('hidden', label)
        self.assertRegex(page, r'try \{[^<]*localStorage')
        self.assertNotIn('http', page)


class PrivacyTests(_Base):
    def setUp(self):
        super().setUp()
        self.seed()
        self.config(ON)
        self.person(LIVE, 'Walter Livesey', living='true', sex='M')
        self.person(UNK, 'Ursula Unwin', living='unknown')
        self.person(SECRET, 'Rupert Sequester', sex='M', extra='restricted: true\n')
        self.person(QUIET, 'Prudence Quayle', extra='restricted: by-request\n')
        self.source('s-kkkkkkkkkk', 'Hidden register')
        self.claim('c-kkkkkkkkkk', 's-kkkkkkkkkk', 'birth', '1951', [LIVE], reviewed='2026-11-02')
        self.claim('c-llllllllll', 's-kkkkkkkkkk', 'birth', '1932', [UNK], reviewed='2026-11-03')
        self.claim('c-mmmmmmmmmm', 's-kkkkkkkkkk', 'birth', '1843', [SECRET], reviewed='2026-11-04')
        self.claim('c-nnnnnnnnnn', 's-kkkkkkkkkk', 'birth', '1847', [QUIET], reviewed='2026-11-05')
        # A fact naming a deceased and a living person is withheld whole.
        self.claim('c-pppppppppp', S2, 'residence', '1960', [THOMAS, LIVE], reviewed='2026-12-06')
        # A restricted claim about a person who has a page.
        self.source('s-qqqqqqqqqq', 'Sealed file', restricted_claims=('c-qqqqqqqqqq',))
        self.claim('c-qqqqqqqqqq', 's-qqqqqqqqqq', 'marriage', '1870', [THOMAS], reviewed='2026-12-07')
        # A private record: its facts stay home too.
        self.source('s-rrrrrrrrrr', 'Private ledger', restricted=1)
        self.claim('c-rrrrrrrrrr', 's-rrrrrrrrrr', 'residence', '1871', [THOMAS], reviewed='2026-12-08')

    def test_nothing_hidden_shows_or_counts_or_makes_a_month(self):
        self.build()
        page = self.page()
        for word in ('Walter', 'Livesey', 'Ursula', 'Unwin', 'Rupert', 'Sequester',
                     'Prudence', 'Quayle', 'Hidden register', 'Sealed file', 'Private ledger'):
            self.assertNotIn(word, page)
        self.assertNotIn('November 2026', page)
        self.assertNotIn('December 2026', page)
        self.assertNotRegex(page, r'\b[0-9]+ (more|other|hidden|living|private|updates)\b')

    def test_a_hidden_edit_does_not_change_the_shared_page(self):
        self.build()
        before = self.page()
        self.conn.execute("UPDATE claims SET reviewed = '2026-08-01' WHERE id = 'c-kkkkkkkkkk'")
        self.claim('c-ssssssssss', 's-kkkkkkkkkk', 'death', '2000', [LIVE], reviewed='2026-07-01')
        self.build()
        self.assertEqual(self.page(), before)

    def test_linked_build_lists_accepted_only(self):
        out = self.root / 'linked-out'
        self.claim('c-tttttttttt', S2, 'residence', '1850', [THOMAS],
                   reviewed='2027-01-05', status='suggested')
        self.build(linked=True, out=out)
        page = self.page(out)
        self.assertNotIn('January 2027', page)
        self.assertIn('Walter Livesey', page)


class DeterminismTests(_Base):
    def test_two_build_days_write_the_same_page(self):
        self.seed()
        self.config(ON)
        self.build(today='2026-06-15')
        first = self.page()
        self.build(today='2027-02-20')
        self.assertEqual(self.page(), first)


if __name__ == '__main__':
    unittest.main()
