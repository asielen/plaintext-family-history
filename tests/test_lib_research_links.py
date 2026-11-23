"""
test_lib_research_links.py - `_lib.research_search_links`, the pre-filled
searches on the workbench person page (build plan 10, PR 10.1).

What the owner notices: a relative's page offers one link per genealogy
website, and each opens that site's own search already filled in. These tests
pin the exact text of each link from the upstream sites' own parameter names
(WikiTree's research tool, RootsDev's gensearch, WikiTree Sourcer), and the
privacy line: a subject with no years, places or relatives (the living case)
gets links that carry the name and nothing else.
"""

import sys
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

from _lib import research_search_links


THOMAS = {
    'given': 'Thomas', 'surname': 'Hartley',
    'birth_year': 1840, 'birth_place': 'New York',
    'father_given': 'Caleb', 'father_surname': 'Hartley',
    'mother_given': 'Chastina', 'mother_surname': 'Reed',
    'spouse_given': 'Margaret', 'spouse_surname': 'Cole',
    'marriage_year': 1871, 'marriage_place': 'Fairview, Kansas',
    'places': ['New York', 'Fairview, Kansas'],
}


def by_key(subject):
    return {link['key']: link['url'] for link in research_search_links(subject)}


def query(url):
    parts = urlsplit(url)
    return {k: v[0] for k, v in parse_qs(parts.query or parts.fragment).items()}


class SearchUrlTests(unittest.TestCase):
    def test_familysearch_full_string_pins_the_encoding(self):
        self.assertEqual(
            by_key(THOMAS)['familysearch'],
            'https://www.familysearch.org/search/record/results'
            '?q.givenName=Thomas&q.surname=Hartley&q.birthLikePlace=New+York'
            '&q.fatherGivenName=Caleb&q.fatherSurname=Hartley'
            '&q.motherGivenName=Chastina&q.motherSurname=Reed'
            '&q.spouseGivenName=Margaret&q.spouseSurname=Cole'
            '&q.marriageLikePlace=Fairview%2C+Kansas'
            '&q.birthLikeDate.from=1838&q.birthLikeDate.to=1842'
            '&q.marriageLikeDate.from=1869&q.marriageLikeDate.to=1873')

    def test_ancestry_is_the_search_page_itself_with_no_affiliate_wrapper(self):
        url = by_key(THOMAS)['ancestry']
        self.assertTrue(url.startswith('https://search.ancestry.com/cgi-bin/sse.dll?'))
        self.assertEqual(query(url), {
            'rank': '1', 'gsfn': 'Thomas', 'gsln': 'Hartley',
            'msbpn__ftp': 'New York',
            'msfng0': 'Caleb', 'msfns0': 'Hartley',
            'msmng0': 'Chastina', 'msmns0': 'Reed',
            'mssng0': 'Margaret', 'mssns0': 'Cole',
            'msgpn__ftp': 'Fairview, Kansas',
            'msbdy': '1840', 'msgdy': '1871', 'gl': 'allgs'})

    def test_myheritage_hand_built_with_spaces_as_percent_2f3(self):
        url = by_key(THOMAS)['myheritage']
        self.assertTrue(url.startswith(
            'https://www.myheritage.com/research?formId=master&formMode=1'
            '&action=query&catId=1&qname=Name+fn.Thomas+ln.Hartley'))
        self.assertIn('&qevents-event1=Event+et.birth+ey.1840+ep.New%2F3York+epmo.similar', url)
        self.assertIn('+et.marriage+ey.1871+ep.Fairview%2C%2F3Kansas+epmo.similar', url)
        self.assertIn('rt.father', url)
        self.assertIn('rt.mother', url)
        self.assertIn('rt.spouse', url)

    def test_findmypast(self):
        self.assertEqual(query(by_key(THOMAS)['findmypast']), {
            'firstname': 'Thomas', 'lastname': 'Hartley',
            'yearofbirth': '1840', 'yearofbirth_offset': '2'})

    def test_findagrave_and_billiongraves_take_the_married_surname(self):
        s = {'given': 'Mary', 'surname': 'Reed', 'married_surname': 'Hartley',
             'birth_year': 1850, 'death_year': 1920}
        links = by_key(s)
        self.assertEqual(query(links['findagrave']), {
            'firstname': 'Mary', 'lastname': 'Hartley',
            'birthyear': '1850', 'birthyearfilter': '2',
            'deathyear': '1920', 'deathyearfilter': '2'})
        self.assertEqual(query(links['billiongraves'])['family_names'], 'Hartley')
        for key in ('familysearch', 'ancestry', 'findmypast', 'newspapers',
                    'chroniclingamerica', 'ukdiscovery'):
            self.assertNotIn('Hartley', links[key], key)

    def test_billiongraves_puts_the_search_after_a_hash(self):
        url = by_key({'given': 'Ann', 'surname': 'Lee', 'birth_year': 1900})['billiongraves']
        self.assertTrue(url.startswith('https://billiongraves.com/pages/search/index.php#'))
        self.assertEqual(query(url), {
            'year_range': '2', 'lim': '0', 'action': 'search', 'exact': 'false',
            'country': '0', 'state': '0', 'county': '0',
            'given_names': 'Ann', 'family_names': 'Lee', 'birth_year': '1900'})

    def test_chronicling_america_and_newspapers_year_ranges(self):
        s = {'given': 'Ann', 'surname': 'Lee', 'birth_year': 1900, 'death_year': 1960}
        links = by_key(s)
        self.assertEqual(query(links['chroniclingamerica']), {
            'dl': 'page', 'ops': 'AND', 'searchType': 'advanced', 'qs': 'Ann Lee',
            'start_date': '1900-01-01', 'end_date': '1960-12-31'})
        # Newspapers.com's own parameter names, checked by hand 2026-11-23:
        # upstream's `query`/`year-start` opened a search with the years dropped.
        self.assertEqual(query(links['newspapers']), {
            'keyword': 'Ann Lee', 'date-start': '1895', 'date-end': '1965'})
        born_only = by_key({'given': 'Ann', 'surname': 'Lee', 'birth_year': 1900})
        self.assertEqual(query(born_only['newspapers'])['date-end'], '1990')
        # With no death on file the newspaper search covers a lifetime, not
        # only the birth year (which could never find a marriage notice).
        self.assertEqual(query(born_only['chroniclingamerica'])['start_date'], '1900-01-01')
        self.assertEqual(query(born_only['chroniclingamerica'])['end_date'], '1990-12-31')
        died_only = by_key({'given': 'Ann', 'surname': 'Lee', 'death_year': 1960})
        self.assertEqual(query(died_only['chroniclingamerica'])['start_date'], '1870-01-01')
        self.assertEqual(query(died_only['chroniclingamerica'])['end_date'], '1960-12-31')

    def test_ukdiscovery_is_a_name_search(self):
        self.assertEqual(query(by_key(THOMAS)['ukdiscovery']), {'_q': 'Thomas Hartley'})

    def test_geneteka_needs_a_province_found_in_a_place(self):
        s = dict(THOMAS, places=['Suwałki, Podlaskie, Poland'])
        url = by_key(s)['geneteka']
        self.assertEqual(query(url), {
            'op': 'gt', 'lang': 'eng', 'bdm': 'B', 'rid': 'B', 'w': '10pl',
            'search_lastname': 'Hartley', 'search_name': 'Thomas',
            'from_date': '1840', 'to_date': '1841'})
        self.assertNotIn('geneteka', by_key(dict(THOMAS, places=['Suwałki, Poland'])))

    def test_geneteka_reads_the_whole_province_name(self):
        # West Pomerania is not Pomerania, and Lower Silesia is not Silesia,
        # whichever order the province list is in.
        for place, code in (('Szczecin, zachodniopomorskie, Poland', '16zp'),
                            ('Koszalin, Zachodniopomorskie', '16zp'),
                            ('Gdańsk, pomorskie', '11pm'),
                            ('Toruń, kujawsko-pomorskie', '02kp'),
                            ('Wrocław, dolnośląskie', '01ds'),
                            ('Katowice, Śląskie', '12sl')):
            with self.subTest(place):
                self.assertEqual(query(by_key(dict(THOMAS, places=[place]))['geneteka'])['w'],
                                 code)

    def test_years_are_widened_by_two_and_absent_years_add_no_date_parameter(self):
        q = query(by_key({'given': 'A', 'surname': 'B', 'birth_year': 1840})['familysearch'])
        self.assertEqual((q['q.birthLikeDate.from'], q['q.birthLikeDate.to']), ('1838', '1842'))
        bare = by_key({'given': 'A', 'surname': 'B'})
        for key in ('familysearch', 'ancestry', 'findmypast', 'findagrave', 'billiongraves'):
            self.assertNotRegex(bare[key], r'(birthLikeDate|bdy|ddy|yearofbirth|birthyear|birth_year)', key)
        self.assertNotIn('Date', bare['familysearch'])

    def test_no_url_carries_an_affiliate_parameter(self):
        for link in research_search_links(THOMAS):
            self.assertNotIn('linksynergy', link['url'])
            self.assertNotRegex(link['url'], r'[?&#](id|mid)=')

    def test_no_name_gives_no_links(self):
        self.assertEqual(research_search_links({}), [])
        self.assertEqual(research_search_links({'birth_year': 1840}), [])

    def test_a_name_only_subject_gets_a_name_and_nothing_else(self):
        # The living or unknown case: the caller passes the name alone.
        for link in research_search_links({'given': 'Jane', 'surname': 'Doe'}):
            url = link['url']
            self.assertIn('Doe', url, link['key'])
            self.assertNotRegex(url, r'\d{4}', link['key'])

    def test_order_and_labels_are_the_documented_ten(self):
        keys = [l['key'] for l in research_search_links(dict(THOMAS, places=['Podlaskie']))]
        self.assertEqual(keys, [
            'familysearch', 'ancestry', 'myheritage', 'findmypast', 'findagrave',
            'billiongraves', 'chroniclingamerica', 'newspapers', 'geneteka', 'ukdiscovery'])


if __name__ == '__main__':
    unittest.main()
