"""The repository list that ships with the tools: where to look, what it costs.

research-next names archives from this list instead of from memory, so every
row must say what it costs and how it is reached, and say who checked it and
when. The list (tools/repositories.json) and the country guides
(tools/research_guides/) are rebuilt by build_repositories.py from two MIT
projects. Synthetic names only; nothing here reads a real archive.

    py -3.14 -m unittest tests.test_repositories_catalog
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import build_repositories as br  # noqa: E402

CATALOG = ROOT / 'tools' / 'repositories.json'
GUIDES = ROOT / 'tools' / 'research_guides'

SAMPLE_MCP = [
    {'id': 'fsearch', 'name': 'Fairview Family Search', 'url': 'https://www.example-search.org/search',
     'countries': ['*'], 'years': [1500, 2000], 'records': ['church', 'vital', 'census'],
     'access': 'free_account', 'about': 'A big free index.', 'about_ru': 'Большая база.',
     'how': 'Open the image, not just the index.', 'how_ru': 'Откройте снимок.',
     'search': 'https://www.example-search.org/results?q={surname}'},
    {'id': 'breton_letters', 'name': 'Breton County Clerk', 'url': 'https://clerk.breton.example/',
     'countries': ['US'], 'years': [0, 9999], 'coverage_known': False, 'records': ['vital', 'holocaust'],
     'access': 'request', 'about': 'Marriage licences.', 'about_ru': 'x', 'how': 'Write to the clerk.',
     'how_ru': 'y', 'availability_note': 'Closed for repairs this year.'},
]

SAMPLE_GUIDE = '''---
type: archive-guide
title: "USA: Vital Records"
last_verified: 2026-05-19
access_checked: manual
tags: [genealogy, archives]
---

# USA: Vital Records

Some intro text.

## Access

### Easton State Records Office
Every state has an office.
- **Cost**: $10 to $30 per certificate (varies by state)
- **Searchable**: Index online; certificates ordered by post
- **AI accessibility**: Browser-only

### Fairview Search
- **URL**: https://www.EXAMPLE-search.org/search/
- **Coverage**: Wills and deeds for Breton County, with a church index.
- **Cost**: Free (account required)
- **Searchable**: Yes
- **AI accessibility**: Browser-only
- **Note**: Open the image of every hit.

### Reed Reading Room
- **URL**: https://reedroom.example/
- **Coverage**: Local newspapers and cemetery lists.
- **Cost**: Free for on-site research
- **Searchable**: In-person only
- **AI accessibility**: In-person only

### No cost line here
- **URL**: https://nocost.example/
- **Coverage**: Skipped, because there is no Cost.

## Common Pitfalls
- **Assuming records exist.** They may not.
'''


def guide_text(cost='Free', stem_extra=''):
    return SAMPLE_GUIDE.replace('Free (account required)', cost) + stem_extra


class MCPRowTests(unittest.TestCase):
    def setUp(self):
        self.rows = {r['id']: r for r in br.mcp_rows(SAMPLE_MCP, '2026-11-15')}

    def test_a_row_carries_what_the_genealogist_is_told(self):
        r = self.rows['mcp:fsearch']
        self.assertEqual(r['access'], 'free_account')
        self.assertEqual(r['channel'], ['online'])
        self.assertEqual(r['how'], 'Open the image, not just the index.')
        self.assertEqual(r['checked'], '2026-11-15')
        self.assertEqual(r['from'], ['genealogy-mcp'])
        self.assertEqual(r['years'], [1500, 2000])

    def test_russian_twins_and_search_templates_are_dropped(self):
        for r in self.rows.values():
            self.assertFalse([k for k in r if k.endswith('_ru')])
            self.assertNotIn('search', r)

    def test_a_request_only_archive_is_reached_by_letter(self):
        self.assertEqual(self.rows['mcp:breton_letters']['channel'], ['by letter'])

    def test_unknown_coverage_years_are_null_and_holocaust_is_a_jewish_topic(self):
        r = self.rows['mcp:breton_letters']
        self.assertIsNone(r['years'])
        self.assertEqual(r['topics'], ['jewish'])

    def test_an_availability_note_is_kept(self):
        self.assertIn('Closed for repairs', self.rows['mcp:breton_letters']['note'])

    def test_an_unknown_access_word_stops_the_build_naming_the_entry(self):
        bad = [dict(SAMPLE_MCP[0], access='sometimes')]
        with self.assertRaises(br.BuildError) as cm:
            br.mcp_rows(bad, '2026-11-15')
        self.assertIn('fsearch', str(cm.exception))
        self.assertIn('sometimes', str(cm.exception))


class GuideRowTests(unittest.TestCase):
    def setUp(self):
        self.rows = {r['name']: r for r in br.guide_rows('usa-vital-records', SAMPLE_GUIDE)}

    def test_only_blocks_with_a_cost_line_become_rows(self):
        self.assertEqual(sorted(self.rows), ['Easton State Records Office', 'Fairview Search', 'Reed Reading Room'])

    def test_cost_text_maps_to_an_access_word(self):
        self.assertEqual(self.rows['Easton State Records Office']['access'], 'paid')
        self.assertEqual(self.rows['Fairview Search']['access'], 'free_account')
        self.assertEqual(self.rows['Reed Reading Room']['access'], 'free')

    def test_cost_wording_table(self):
        for cost, want in (
            ('Free', 'free'), ('Free (account required)', 'free_account'),
            ('Subscription', 'paid'), ('Subscription (some collections free)', 'mixed'),
            ('Free (basic) / Premium subscription for advanced features', 'mixed'),
            ('Free for on-site research; fees for reproductions', 'mixed'),
            ('Free online access to some collections', 'mixed'),
            ('£11 per certificate', 'paid'), ('$65 per request', 'paid'),
            ('Varies (many on FamilySearch for free)', 'mixed'),
            ('Free (varies by region)', 'free'), ('On-site access', 'free'),
        ):
            with self.subTest(cost):
                self.assertEqual(br.classify_access(cost), want)

    def test_channel_comes_from_how_the_records_are_reached(self):
        self.assertEqual(self.rows['Easton State Records Office']['channel'], ['online', 'by letter'])
        self.assertEqual(self.rows['Fairview Search']['channel'], ['online'])
        self.assertEqual(self.rows['Reed Reading Room']['channel'], ['in person'])

    def test_ai_accessibility_text_never_reaches_a_row(self):
        for r in self.rows.values():
            self.assertNotIn('AI accessibility', json.dumps(r))

    def test_row_fields(self):
        r = self.rows['Fairview Search']
        self.assertEqual(r['id'], 'ar:usa-vital-records:fairview-search')
        self.assertEqual(r['countries'], ['US'])
        self.assertEqual(r['url'], 'https://www.EXAMPLE-search.org/search/')
        self.assertIsNone(r['years'])
        self.assertEqual(r['checked'], '2026-05-19')
        self.assertEqual(r['guide'], 'usa-vital-records')
        self.assertEqual(r['how'], 'Open the image of every hit.')
        self.assertEqual(r['from'], ['autoresearch-genealogy'])
        for kind in ('vital', 'probate', 'land', 'church'):
            self.assertIn(kind, r['records'])
        self.assertIn('newspapers', self.rows['Reed Reading Room']['records'])
        self.assertIn('graves', self.rows['Reed Reading Room']['records'])

    def test_a_block_without_a_url_still_says_what_it_is(self):
        r = self.rows['Easton State Records Office']
        self.assertEqual(r['url'], '')
        self.assertEqual(r['about'], 'Every state has an office.')

    def test_an_unreadable_cost_stops_the_build_naming_guide_and_heading(self):
        with self.assertRaises(br.BuildError) as cm:
            br.guide_rows('usa-vital-records', guide_text(cost='Ask nicely'))
        self.assertIn('usa-vital-records', str(cm.exception))
        self.assertIn('Fairview Search', str(cm.exception))

    def test_an_unmapped_guide_stem_stops_the_build_naming_it(self):
        with self.assertRaises(br.BuildError) as cm:
            br.guide_rows('atlantis', SAMPLE_GUIDE)
        self.assertIn('atlantis', str(cm.exception))

    def test_ireland_rows_naming_northern_ireland_also_serve_gb(self):
        text = SAMPLE_GUIDE.replace('Easton State Records Office', 'PRONI (Public Record Office of Northern Ireland)')
        rows = {r['name']: r for r in br.guide_rows('ireland', text)}
        self.assertEqual(rows['PRONI (Public Record Office of Northern Ireland)']['countries'], ['IE', 'GB'])
        self.assertEqual(rows['Fairview Search']['countries'], ['IE'])

    def test_latin_america_rows_take_the_countries_their_heading_names(self):
        text = SAMPLE_GUIDE.replace('Reed Reading Room', 'Archive of Brazil and Spain')
        rows = {r['name']: r for r in br.guide_rows('mexico-latin-america', text)}
        self.assertEqual(rows['Archive of Brazil and Spain']['countries'], ['MX', 'BR', 'ES'])

    def test_a_web_address_inside_text_is_kept_and_the_rest_is_the_note(self):
        # He is told the site exists online, not "no website", when the guide wrote
        # the address inside a sentence; an email or a town is kept as the note.
        text = SAMPLE_GUIDE.replace('- **URL**: https://reedroom.example/',
                                    '- **URL**: Part of the State Archive (https://sok.reedroom.example/)')
        text = text.replace('- **URL**: https://www.EXAMPLE-search.org/search/',
                            '- **URL**: Contact via email (clerk@fairview.example)')
        rows = {r['name']: r for r in br.guide_rows('usa-vital-records', text)}
        self.assertEqual(rows['Reed Reading Room']['url'], 'https://sok.reedroom.example/')
        self.assertIn('Part of the State Archive', rows['Reed Reading Room']['note'])
        self.assertEqual(rows['Fairview Search']['url'], '')
        self.assertEqual(rows['Fairview Search']['note'], 'Contact via email (clerk@fairview.example)')
        self.assertNotIn('note', rows['Easton State Records Office'])

    def test_a_dead_upstream_address_is_replaced_by_the_one_that_answers(self):
        text = SAMPLE_GUIDE.replace('https://reedroom.example/', 'https://www.linc.tas.gov.au/')
        rows = {r['name']: r for r in br.guide_rows('usa-vital-records', text)}
        self.assertEqual(rows['Reed Reading Room']['url'], 'https://libraries.tas.gov.au/')

    def test_an_overview_heading_takes_its_sections_name(self):
        # "Look in Overview" names nothing; the section heading names the database.
        text = SAMPLE_GUIDE.replace('### Reed Reading Room', '## Reed Reading Room Databases\n\n### Overview')
        rows = {r['name']: r for r in br.guide_rows('usa-vital-records', text)}
        self.assertIn('Reed Reading Room Databases', rows)
        self.assertNotIn('Overview', rows)

    def test_sentences_about_ai_tools_never_reach_a_row(self):
        text = SAMPLE_GUIDE.replace('- **Note**: Open the image of every hit.',
                                    '- **Note**: Open the image of every hit. Claude can read it for you.')
        rows = {r['name']: r for r in br.guide_rows('usa-vital-records', text)}
        self.assertEqual(rows['Fairview Search']['how'], 'Open the image of every hit.')

    def test_topics_by_stem(self):
        rows = br.guide_rows('jewish-genealogy', SAMPLE_GUIDE)
        self.assertEqual(rows[0]['countries'], ['*'])
        self.assertEqual(rows[0]['topics'], ['jewish'])


class GuideFileTests(unittest.TestCase):
    def test_header_replaces_frontmatter_and_ai_lines_are_gone(self):
        out = br.guide_file('usa-vital-records', SAMPLE_GUIDE + '- AI-readable (HTML-based)\n', '33da6f1')
        first = out.splitlines()[0]
        self.assertTrue(first.startswith('<!-- From autoresearch-genealogy'))
        self.assertIn('archives/usa-vital-records.md at commit 33da6f1', first)
        self.assertIn('2026-05-19', first)
        self.assertIn('LICENSE-autoresearch-genealogy.txt', first)
        self.assertNotIn('type: archive-guide', out)
        self.assertNotRegex(out, r'(?m)^- \*\*AI accessibility\*\*')
        self.assertNotRegex(out, r'(?m)^- AI-readable')
        self.assertIn('Every state has an office.', out)
        self.assertIn('Assuming records exist.', out)

    def test_sentences_about_ai_tools_are_cut_and_the_rest_of_the_line_kept(self):
        # A third party's note to the assistant is not reference text for him.
        extra = ('- Handwriting: old script is hard to read. Claude multimodal can help with it.\n'
                 '- ChatGPT reads these well.\n')
        out = br.guide_file('usa-vital-records', SAMPLE_GUIDE + extra, '33da6f1')
        self.assertIn('- Handwriting: old script is hard to read.', out)
        self.assertNotIn('Claude', out.split('-->', 1)[1])
        self.assertNotIn('ChatGPT', out)

    def test_a_person_or_place_named_claude_is_kept(self):
        # Jean-Claude and Saint-Claude are people and places in his records, not the AI tool.
        for line in ('Baptism of Jean-Claude Martin, 1802, in Saint-Claude (Jura).',
                     'The parish priest, Fr. Claude Dupont, kept the register. Births are complete.'):
            self.assertEqual(br.strip_ai_sentences(line), line)

    def test_a_guide_without_a_checked_date_stops_the_build(self):
        with self.assertRaises(br.BuildError):
            br.guide_file('usa-vital-records', SAMPLE_GUIDE.replace('last_verified: 2026-05-19\n', ''), 'x')


class BuildTests(unittest.TestCase):
    def build(self, mcp=SAMPLE_MCP, guides=None):
        return br.build(mcp, '2aa2cb0', '2026-11-15', guides or {'usa-vital-records': SAMPLE_GUIDE}, '33da6f1')

    def test_one_site_listed_twice_for_one_country_is_one_row(self):
        mcp = [dict(SAMPLE_MCP[0], countries=['US', 'CA'])]
        catalog, _ = self.build(mcp=mcp)
        rows = [r for r in catalog['repositories'] if 'example-search.org' in r['url'].lower()]
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['from'], ['genealogy-mcp', 'autoresearch-genealogy'])
        self.assertEqual(r['how'], 'Open the image, not just the index.')
        self.assertEqual(r['checked'], '2026-11-15')
        self.assertEqual(r['countries'], ['US', 'CA'])
        self.assertEqual(r['guide'], 'usa-vital-records')
        for kind in ('church', 'probate', 'land'):
            self.assertIn(kind, r['records'])

    def test_a_worldwide_row_keeps_each_countrys_listing_apart(self):
        # The England guide's text about a worldwide site is not folded under it.
        catalog, _ = self.build()
        rows = [r for r in catalog['repositories'] if 'example-search.org' in r['url'].lower()]
        self.assertEqual(sorted(r['id'] for r in rows), ['ar:usa-vital-records:fairview-search', 'mcp:fsearch'])

    def test_two_guides_for_one_country_keep_their_own_rows(self):
        # England's Ancestry row must not carry Scotland's advice, nor Scotland lose its row.
        eng = SAMPLE_GUIDE.replace('- **Note**: Open the image of every hit.', '- **Note**: English advice.')
        sco = SAMPLE_GUIDE.replace('- **Note**: Open the image of every hit.', '- **Note**: Scottish advice.')
        catalog, _ = br.build([], 'c', '2026-11-15', {'england-wales': eng, 'scotland': sco}, 'd')
        rows = {r['id']: r for r in catalog['repositories'] if 'example-search.org' in r['url'].lower()}
        self.assertEqual(rows['ar:england-wales:fairview-search']['how'], 'English advice.')
        self.assertEqual(rows['ar:scotland:fairview-search']['how'], 'Scottish advice.')

    def test_two_rows_of_one_guide_for_one_site_stay_apart(self):
        # The Portugal collections row (Azores, Madeira) must not fold into the Spain row.
        text = SAMPLE_GUIDE.replace('### Reed Reading Room\n- **URL**: https://reedroom.example/',
                                    '### Reed Reading Room\n- **URL**: https://www.example-search.org/search')
        catalog, _ = br.build([], 'c', '2026-11-15', {'spain-portugal': text}, 'd')
        rows = {r['id'] for r in catalog['repositories'] if 'example-search.org' in r['url'].lower()}
        self.assertEqual(rows, {'ar:spain-portugal:fairview-search', 'ar:spain-portugal:reed-reading-room'})

    def test_a_topic_guide_row_and_a_second_mcp_row_stay_apart(self):
        aa = SAMPLE_GUIDE.replace('- **Note**: Open the image of every hit.', '- **Note**: Freedmen records.')
        mcp = [dict(SAMPLE_MCP[0], countries=['US']),
               dict(SAMPLE_MCP[0], id='fsearch_latam', countries=['US', 'MX'], about='Latin America.')]
        catalog, _ = br.build(mcp, 'c', '2026-11-15', {'african-american': aa}, 'd')
        rows = {r['id']: r for r in catalog['repositories'] if 'example-search.org' in r['url'].lower()}
        self.assertEqual(sorted(rows), ['ar:african-american:fairview-search', 'mcp:fsearch', 'mcp:fsearch_latam'])
        self.assertNotIn('topics', rows['mcp:fsearch'])
        self.assertEqual(rows['mcp:fsearch_latam']['about'], 'Latin America.')

    def test_two_listings_disagreeing_on_cost_never_say_free(self):
        mcp = [dict(SAMPLE_MCP[0], countries=['US'], access='free')]
        catalog, _ = self.build(mcp=mcp, guides={'usa-vital-records': guide_text(cost='Subscription')})
        r = [r for r in catalog['repositories'] if r['id'] == 'mcp:fsearch'][0]
        self.assertEqual(r['from'], ['genealogy-mcp', 'autoresearch-genealogy'])
        self.assertEqual(r['access'], 'mixed')
        self.assertEqual(br._access('free', 'free_account'), 'free_account')

    def test_the_same_site_for_two_countries_stays_two_rows(self):
        other = SAMPLE_GUIDE.replace('USA: Vital Records', 'France')
        catalog, _ = br.build([], '2aa2cb0', '2026-11-15',
                              {'usa-vital-records': SAMPLE_GUIDE, 'france': other}, '33da6f1')
        rows = [r for r in catalog['repositories'] if 'example-search.org' in r['url'].lower()]
        self.assertEqual(sorted(r['countries'] for r in rows), [['FR'], ['US']])

    def test_url_normalizing(self):
        for a, b in (('https://www.Example.org/a/', 'http://example.org/a'),
                     ('https://example.org', 'https://example.org/')):
            self.assertEqual(br.normalize_url(a), br.normalize_url(b))
        self.assertNotEqual(br.normalize_url('https://example.org/a'), br.normalize_url('https://example.org/b'))

    def test_both_licences_travel_in_the_catalog(self):
        catalog, _ = self.build()
        by = {s['project']: s for s in catalog['sources']}
        mcp = by['https://github.com/igormel81/genealogy-mcp']
        ar = by['https://github.com/mattprusak/autoresearch-genealogy']
        self.assertIn('Copyright (c) 2026 Igor Melnikov', mcp['licence'])
        self.assertIn('Copyright (c) 2026 Matt Prusak', ar['licence'])
        self.assertEqual((mcp['commit'], ar['commit']), ('2aa2cb0', '33da6f1'))

    def test_rows_get_a_guide_when_one_guide_is_the_obvious_one(self):
        catalog, _ = self.build()
        clerk = [r for r in catalog['repositories'] if r['id'] == 'mcp:breton_letters'][0]
        self.assertEqual(clerk['guide'], 'usa-vital-records')

    def test_a_worldwide_site_is_not_pointed_at_the_jewish_guide(self):
        # FamilySearch for an Irish family must not send him to "Jewish genealogy".
        guides = {'usa-vital-records': SAMPLE_GUIDE, 'jewish-genealogy': SAMPLE_GUIDE,
                  'russia-ukraine': SAMPLE_GUIDE}
        mcp = [SAMPLE_MCP[0], dict(SAMPLE_MCP[0], id='rodo', url='https://rodo.example/',
                                   countries=['*', 'RU', 'UA'], records=['trees']),
               dict(SAMPLE_MCP[0], id='shoah', url='https://shoah.example/', records=['holocaust'])]
        catalog, _ = br.build(mcp, 'c', '2026-11-15', guides, 'd')
        rows = {r['id']: r for r in catalog['repositories']}
        self.assertIsNone(rows['mcp:fsearch']['guide'])
        self.assertEqual(rows['mcp:rodo']['guide'], 'russia-ukraine')
        self.assertEqual(rows['mcp:shoah']['guide'], 'jewish-genealogy')

    def test_the_script_reads_files_only(self):
        src = (ROOT / 'build_repositories.py').read_text(encoding='utf-8')
        for banned in (r'exec\(', r'eval\(', r'^\s*(?:import|from)\s+(?:importlib|urllib|requests|socket|subprocess|http)'):
            self.assertIsNone(re.search(banned, src, re.MULTILINE), banned)


class ShippedCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads(CATALOG.read_text(encoding='utf-8'))

    def test_every_row_has_the_fields_research_next_reads(self):
        for r in self.catalog['repositories']:
            with self.subTest(r['id']):
                for key in ('id', 'name', 'url', 'countries', 'access', 'channel', 'from', 'checked'):
                    self.assertIn(key, r)
                self.assertIn(r['access'], br.ACCESS_WORDS)
                self.assertTrue(r['channel'] and set(r['channel']) <= set(br.CHANNELS))
                self.assertTrue(r['countries'])
                self.assertRegex(r['checked'], r'^\d{4}-\d{2}-\d{2}$')
                self.assertTrue(r['from'])

    def test_ids_are_unique_and_no_key_is_a_russian_twin(self):
        ids = [r['id'] for r in self.catalog['repositories']]
        self.assertEqual(len(ids), len(set(ids)))
        for r in self.catalog['repositories']:
            self.assertFalse([k for k in r if k.endswith('_ru')], r['id'])

    def test_both_licence_texts_and_commits_are_recorded(self):
        srcs = self.catalog['sources']
        self.assertEqual(len(srcs), 2)
        text = ' '.join(s['licence'] for s in srcs)
        self.assertIn('Igor Melnikov', text)
        self.assertIn('Matt Prusak', text)
        for s in srcs:
            self.assertTrue(s['licence'].startswith('MIT License'))
            self.assertTrue(re.fullmatch(r'[0-9a-f]{7,40}', s['commit']), s)

    def test_a_catalog_row_exists_for_every_vendored_guide(self):
        stems = {p.stem for p in GUIDES.glob('*.md')}
        self.assertEqual(stems, set(br.STEM_COUNTRIES))
        have = {r['guide'] for r in self.catalog['repositories'] if r['guide']}
        self.assertEqual(have, stems)

    def test_known_rows_say_what_a_genealogist_needs(self):
        rows = {r['id']: r for r in self.catalog['repositories']}
        fs = rows['mcp:familysearch']
        self.assertEqual(fs['access'], 'free_account')
        self.assertEqual(fs['countries'], ['*'])
        office = rows['ar:usa-vital-records:state-vital-records-offices']
        self.assertEqual(office['access'], 'paid')
        self.assertIn('by letter', office['channel'])


class ShippedGuideTests(unittest.TestCase):
    def test_every_guide_starts_with_its_header_and_has_no_ai_lines(self):
        files = sorted(p for p in GUIDES.glob('*.md'))
        self.assertEqual(len(files), 24)
        for p in files:
            with self.subTest(p.name):
                text = p.read_text(encoding='utf-8')
                self.assertTrue(text.startswith('<!-- From autoresearch-genealogy ('), p.name)
                self.assertIn(f'archives/{p.name} at commit ', text.splitlines()[0])
                self.assertRegex(text.splitlines()[0], r'last checked by that project \d{4}-\d{2}-\d{2}')
                self.assertNotRegex(text, r'(?m)^- \*\*AI accessibility\*\*')
                self.assertNotRegex(text, r'(?m)^- AI-(readable|searchable)')
                self.assertNotRegex(text.split('-->', 1)[1], br._AI_WORDS)

    def test_no_shipped_row_names_overview_or_points_a_worldwide_site_at_a_topic_guide(self):
        catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
        for r in catalog['repositories']:
            with self.subTest(r['id']):
                self.assertNotIn(r['name'].lower(), br._GENERIC_HEADINGS)
                if r['guide'] == 'jewish-genealogy':
                    self.assertIn('jewish', r.get('topics', []))
                self.assertNotIn(r['url'], br.URL_FIXES)

    def test_the_licence_text_sits_beside_the_guides(self):
        text = (GUIDES / br.GUIDES_LICENCE).read_text(encoding='utf-8')
        self.assertTrue(text.startswith('MIT License'))
        self.assertIn('Copyright (c) 2026 Matt Prusak', text)

    def test_guide_headers_name_the_commit_the_catalog_names(self):
        catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
        commit = [s['commit'] for s in catalog['sources'] if 'autoresearch' in s['project']][0]
        for p in GUIDES.glob('*.md'):
            self.assertIn(f'at commit {commit},', p.read_text(encoding='utf-8').splitlines()[0])


if __name__ == '__main__':
    unittest.main()
