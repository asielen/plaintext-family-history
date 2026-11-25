"""
test_site_contribute.py - the "Tell me what you know" link at the foot of
each person page of the shared site (TOOLING §12, `site: contact_email`).

A cousin reading Margaret's page can answer in one click: the email opens
addressed to the keeper, with her name and record number filled in and the
same questions the inbox notes form asks, so the reply can be saved straight
into the inbox. Expected text comes from the rule in the docs, not from the
code's output. Temp fixtures only.
"""

import sys
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib
from tests import test_site_rings_calendar as rc

ME = rc.ME
EMAIL = 'keeper@example.com'
CFG = f'site:\n  contact_email: {EMAIL}\n'


def _href(html: str) -> str:
    start = html.index('href="mailto:') + len('href="')
    return html[start:html.index('"', start)].replace('&amp;', '&')


class ContributionMailtoTests(unittest.TestCase):
    def test_subject_and_body_name_the_person_and_record(self):
        url = _lib.contribution_mailto(EMAIL, 'Margaret A. Cole', 'P-CD795C61E0')
        self.assertTrue(url.startswith(f'mailto:{EMAIL}?subject='))
        query = urllib.parse.parse_qs(url.split('?', 1)[1])
        self.assertEqual(query['subject'], ['[p-cd795c61e0] Margaret A. Cole'])
        body = query['body'][0]
        self.assertTrue(body.startswith('About: Margaret A. Cole (p-cd795c61e0)\r\n\r\n'))
        for question in ('What do you know about Margaret A. Cole?', 'How do you know it?',
                         'About when?', 'Do you have a photo or a paper to share?',
                         "Your name, and how you're related:"):
            self.assertIn(question, body)
        self.assertNotIn('\n', body.replace('\r\n', ''))

    def test_url_stays_short_enough_for_mail_programs(self):
        url = _lib.contribution_mailto(EMAIL, 'Margaret A. Cole', 'p-cd795c61e0')
        self.assertLess(len(url), 2000)

    def test_a_name_with_odd_characters_survives_the_round_trip(self):
        url = _lib.contribution_mailto(EMAIL, 'Zoë "Zo" O\'Neil & Sons', 'p-cd795c61e0')
        query = urllib.parse.parse_qs(url.split('?', 1)[1])
        self.assertEqual(query['subject'], ['[p-cd795c61e0] Zoë "Zo" O\'Neil & Sons'])


class ContributeLinkTests(rc._Base):
    def seed(self):
        self.person(ME, 'Margaret Cole')

    def _page(self):
        return self.page(f'persons/{rc.site._page_filename(ME)}')

    def test_shared_page_ends_with_the_link(self):
        self.config(CFG)
        self.seed()
        self.build()
        html = self._page()
        self.assertIn('class="contribute"', html)
        self.assertIn('Tell me what you know', html)
        href = _href(html)
        self.assertTrue(href.startswith(f'mailto:{EMAIL}?subject=%5Bp-'), href)
        query = urllib.parse.parse_qs(href.split('?', 1)[1])
        self.assertEqual(query['subject'], [f'[{ME}] Margaret Cole'])
        self.assertIn(f'About: Margaret Cole ({ME})', query['body'][0])
        self.assertLess(len(href), 2000)

    def test_no_setting_means_no_link(self):
        self.config('')
        self.seed()
        res = self.build()
        self.assertNotIn('contribute', self._page())
        self.assertFalse(any('contact_email' in m for m in res['messages']))

    def test_a_malformed_address_warns_once_and_adds_no_link(self):
        self.config('site:\n  contact_email: not an address\n')
        self.seed()
        res = self.build()
        self.assertNotIn('mailto:', self._page())
        warnings = [m for m in res['messages'] if 'contact_email' in m]
        self.assertEqual(len(warnings), 1, res['messages'])
        self.assertIn('name@example.com', warnings[0])

    def test_the_linked_preview_has_no_link(self):
        self.config(CFG)
        self.seed()
        self.build(linked=True)
        self.assertNotIn('contribute', self._page())

    def test_a_living_person_has_no_page_so_no_link(self):
        self.config(CFG)
        self.person(rc.LIVE, 'Lena Cole', living='true')
        self.seed()
        self.build()
        self.assertFalse((self.out / 'persons' / rc.site._page_filename(rc.LIVE)).exists())
        for page in (self.out / 'persons').glob('*.html'):
            self.assertNotIn(rc.LIVE, page.read_text(encoding='utf-8'))

    def test_rebuild_is_byte_identical(self):
        self.config(CFG)
        self.seed()
        self.build()
        path = self.out / 'persons' / rc.site._page_filename(ME)
        first = path.read_bytes()
        self.build()
        self.assertEqual(first, path.read_bytes())


if __name__ == '__main__':
    unittest.main()
