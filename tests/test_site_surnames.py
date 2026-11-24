"""
test_site_surnames.py: one page per family surname on the family site
(TOOLING 12 "Pages", "Surname pages").

What a relative sees: a "Kowalski" page listing everyone who carried the name
(Kowalska too), from the first year to the last, the places they lived, the
family's own note about where the name comes from, and the records that
mention them. What must never show: a living, unknown, restricted or
by-request person, a count of them, or a page for a name only they carry.

Builds a synthetic .cache/index.sqlite the way tests/test_site_search.py does.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import test_site_search as base   # noqa: E402  (its fixture helpers)

P = [f'p-{c * 10}' for c in 'abcdefghjkmnpqrs']
ELM = 'l-aaaaaaaaaa'


class _Surnames(base._Base):
    def note(self, slug, text):
        path = self.root / 'notes' / 'surnames' / f'{slug}.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')

    def page(self, name='kowalski'):
        return (self.out / 'surnames' / f'{name}.html').read_text(encoding='utf-8')

    def files(self):
        folder = self.out / 'surnames'
        return sorted(p.name for p in folder.glob('*.html')) if folder.is_dir() else []

    def home(self):
        return (self.out / 'index.html').read_text(encoding='utf-8')

    def seed_kowalskis(self):
        self.person(P[0], 'Jan Kowalski', sex='M')
        self.person(P[1], 'Anna Kowalski')
        self.source('s-aaaaaaaaaa', 'Suwalki parish register')
        self.birth('c-aaaaaaaaaa', 's-aaaaaaaaaa', P[0], '1790')
        self.claim('c-bbbbbbbbbb', 's-aaaaaaaaaa', 'death', '1852', [P[0]])
        self.birth('c-cccccccccc', 's-aaaaaaaaaa', P[1], '1799')
        self.place(ELM, 'Elm Creek')


class GroupingTests(_Surnames):
    def test_one_page_per_surname_with_count_and_years(self):
        self.seed_kowalskis()
        self.person(P[2], 'Edith Brown')
        self.build()
        self.assertEqual(self.files(), ['brown.html', 'index.html', 'kowalski.html'])
        text = self.page()
        self.assertIn('2 people on this site carry this name, from 1790 to 1852', text)
        self.assertIn('Jan Kowalski', text)
        self.assertIn('Anna Kowalski', text)

    def test_accent_and_case_spellings_share_a_page(self):
        self.person(P[0], 'Karl Müller', sex='M')
        self.person(P[1], 'Eva Muller')
        self.build()
        self.assertEqual(self.files(), ['index.html', 'muller.html'])
        self.assertIn('Karl Müller', self.page('muller'))
        self.assertIn('Eva Muller', self.page('muller'))

    def test_kowalska_joins_kowalski_only_when_kowalski_is_present(self):
        self.person(P[0], 'Anna Kowalska')
        self.build()
        self.assertEqual(self.files(), ['index.html', 'kowalska.html'])
        self.person(P[1], 'Jan Kowalski', sex='M')
        self.build()
        self.assertEqual(self.files(), ['index.html', 'kowalski.html'])
        text = self.page()
        self.assertIn('Anna Kowalska', text)
        self.assertIn('Also written Kowalska.', text)

    def test_novakova_joins_novak(self):
        self.person(P[0], 'Marie Nováková')
        self.person(P[1], 'Josef Novak', sex='M')
        self.build()
        self.assertEqual(self.files(), ['index.html', 'novak.html'])
        self.assertIn('Also written Nováková.', self.page('novak'))

    def test_slug_collision_gets_a_suffix(self):
        # Two different surnames that fold to one file name.
        self.person(P[0], 'Ann Smith-Jones')
        self.person(P[1], 'Bob Smith Jones', sex='M')
        self.person(P[2], 'Cy Jones', sex='M')
        self.conn.execute("UPDATE persons SET surname = 'Smith-Jones' WHERE id = ?", (P[0],))
        self.conn.execute("UPDATE persons SET surname = 'Smith Jones' WHERE id = ?", (P[1],))
        self.build()
        self.assertEqual(self.files(),
                         ['index.html', 'jones.html', 'smith-jones-2.html', 'smith-jones.html'])

    def test_a_name_in_another_alphabet_keeps_its_own_page_and_note(self):
        # A Russian and a Chinese family each get a page of their own whose
        # file name never changes when another such family is added, and the
        # note written for one never wanders onto the other's page.
        self.person(P[0], 'Ivan Иванов', sex='M')
        self.person(P[1], 'Wei 李', sex='M')
        self.build()
        first = self.files()
        self.assertNotIn('place.html', first)
        self.assertEqual(len(first), 3)
        li = next(f[:-5] for f in first
                  if f != 'index.html' and '李' in self.page(f[:-5]))
        self.note(li, 'The Li family came from Guangdong.\n')
        self.person(P[2], 'Nikos Αθανασίου', sex='M')
        self.build()
        self.assertTrue(set(first) <= set(self.files()))
        self.assertIn('Guangdong', self.page(li))
        for f in self.files():
            if f not in ('index.html', f'{li}.html'):
                self.assertNotIn('Guangdong', self.page(f[:-5]))

    def test_a_family_named_place_is_not_confused_with_another_alphabet(self):
        self.person(P[0], 'Ann Place')
        self.person(P[1], 'Wei 李', sex='M')
        self.build()
        self.assertIn('Ann Place', self.page('place'))
        self.assertNotIn('李', self.page('place'))

    def test_a_name_windows_reserves_still_gets_a_page(self):
        # "Con" or "Nul" as a file name is a device on Windows: the page
        # would silently never be written and the home page link would break.
        self.person(P[0], 'Ann Con')
        self.build()
        files = self.files()
        self.assertNotIn('con.html', files)
        page = next(f for f in files if f != 'index.html')
        self.assertIn('Ann Con', self.page(page[:-5]))
        self.assertIn(f'surnames/{page}', self.home())

    def test_a_surname_written_in_the_restricted_form_is_not_printed(self):
        self.person(P[0], 'Charley Layng', sex='M')
        self.person(P[1], 'Ann Cole')
        self.conn.execute("UPDATE persons SET surname = ? WHERE id = ?",
                          ("{'value': 'Layng', 'restricted': 'true'}", P[0]))
        self.build()
        self.assertEqual(self.files(), ['cole.html', 'index.html'])
        self.assertNotIn('value', self.home())

    def test_a_surname_he_marked_private_gets_no_page_on_the_shared_site(self):
        self.person(P[0], 'Mary Layng',
                    extra='married_name: {value: Layng, restricted: true}\n')
        self.person(P[1], 'Ann Cole')
        self.build()
        self.assertEqual(self.files(), ['cole.html', 'index.html'])


class NoteTests(_Surnames):
    def test_note_also_list_merges_and_its_prose_shows(self):
        self.seed_kowalskis()
        self.person(P[2], 'Pawel Kowalsky', sex='M')
        self.note('kowalski', '---\nalso: [Kowalsky]\n---\nThe name means smith, from kowal.\n')
        self.build()
        self.assertEqual(self.files(), ['index.html', 'kowalski.html'])
        text = self.page()
        self.assertIn('The name means smith, from kowal.', text)
        self.assertIn('Pawel Kowalsky', text)
        self.assertIn('Also written Kowalsky.', text)

    def test_also_moves_only_the_spelling_it_names(self):
        # Listing Kowalska on the Kowalsky note must not drag every Kowalski
        # along and make the Kowalski page (and links to it) vanish.
        self.person(P[0], 'Jan Kowalski', sex='M')
        self.person(P[1], 'Anna Kowalska')
        self.person(P[2], 'Pawel Kowalsky', sex='M')
        self.note('kowalsky', '---\nalso: [Kowalska]\n---\nAnother spelling.\n')
        self.build()
        self.assertEqual(self.files(), ['index.html', 'kowalski.html', 'kowalsky.html'])
        self.assertIn('Jan Kowalski', self.page('kowalski'))
        self.assertNotIn('Anna Kowalska', self.page('kowalski'))
        self.assertIn('Anna Kowalska', self.page('kowalsky'))
        self.assertNotIn('Jan Kowalski', self.page('kowalsky'))

    def test_also_lists_do_not_chain(self):
        # Hartly moves to the Hartley page; the Hartly note's own also: then
        # has no page of its own left to gather onto, so Hartlee stays put.
        self.person(P[0], 'Al Hartley', sex='M')
        self.person(P[1], 'Bob Hartly', sex='M')
        self.person(P[2], 'Cy Hartlee', sex='M')
        self.note('hartley', '---\nalso: [Hartly]\n---\nThe Hartleys.\n')
        self.note('hartly', '---\nalso: [Hartlee]\n---\nThe Hartlys.\n')
        self.build()
        self.assertEqual(self.files(), ['hartlee.html', 'hartley.html', 'index.html'])
        self.assertIn('Bob Hartly', self.page('hartley'))
        self.assertIn('Cy Hartlee', self.page('hartlee'))
        self.assertNotIn('The Hartlys', self.page('hartlee'))

    def test_private_paragraph_in_the_note_is_withheld_in_a_shared_build(self):
        self.seed_kowalskis()
        self.note('kowalski',
                  'The name means smith.\n\n<!-- private -->\nA secret.\n<!-- /private -->\n')
        self.build()
        self.assertIn('The name means smith.', self.page())
        self.assertNotIn('A secret', self.page())

    def test_ai_draft_in_the_note_is_withheld(self):
        self.seed_kowalskis()
        self.note('kowalski',
                  '## One\nReal text.\n\n## Two\nGuessed text.\n\n'
                  '<!-- AI-DRAFT 2026-07-01 claude-x - drafted -->\n')
        self.build()
        text = self.page()
        self.assertIn('Real text.', text)
        self.assertNotIn('Guessed text', text)

    def test_a_note_for_a_name_only_hidden_people_carry_makes_no_page(self):
        self.person(P[0], 'Walter Livesey', living='true', sex='M')
        self.note('livesey', "Walter's name.\n")
        self.build()
        self.assertEqual(self.files(), [])


class PrivacyTests(_Surnames):
    def seed_hidden(self):
        self.seed_kowalskis()
        self.person(P[3], 'Walter Livesey', living='true', sex='M')
        self.person(P[4], 'Ursula Unwin', living='unknown')
        self.person(P[5], 'Rupert Sequester', sex='M', extra='restricted: true\n')
        self.person(P[6], 'Prudence Quayle', extra='restricted: by-request\n')
        self.person(P[7], 'Lucy Kowalski', living='true')
        self.person(P[8], 'Ruth Sequester')   # deceased and shown: the name has a page

    def test_hidden_people_are_neither_listed_nor_counted_nor_named_by_a_page(self):
        self.seed_hidden()
        self.build()
        self.assertEqual(self.files(), ['index.html', 'kowalski.html', 'sequester.html'])
        everything = ' '.join((self.out / 'surnames' / f).read_text(encoding='utf-8')
                              for f in self.files())
        for hidden in ('Livesey', 'Unwin', 'Quayle', 'Rupert', 'Lucy'):
            self.assertNotIn(hidden, everything)
        self.assertIn('2 people on this site carry this name', self.page())
        self.assertIn('1 person on this site carries this name', self.page('sequester'))
        self.assertNotRegex(everything, r'not shown|may be living|more people')
        for path in self.out.rglob('*.html'):
            self.assertNotRegex(path.read_text(encoding='utf-8'), r'(?i)livesey|unwin|quayle')

    def test_home_page_and_nav_never_link_a_hidden_surname(self):
        self.seed_hidden()
        self.build()
        self.assertIn('surnames/kowalski.html', self.home())
        self.assertNotIn('surnames/livesey.html', self.home())
        self.assertIn('surnames/index.html', self.home())

    def test_no_surnames_page_or_nav_link_when_everyone_is_hidden(self):
        self.person(P[3], 'Walter Livesey', living='true', sex='M')
        self.build()
        self.assertEqual(self.files(), [])
        self.assertNotIn('surnames/', self.home())

    def test_linked_preview_lists_everyone(self):
        self.seed_hidden()
        self.build(linked=True)
        self.assertIn('Livesey', ' '.join(p.read_text(encoding='utf-8')
                                          for p in (self.out / 'surnames').glob('*.html')))

    def test_records_list_only_published_sources(self):
        self.seed_kowalskis()
        self.source('s-bbbbbbbbbb', 'Sealed adoption file', restricted=1)
        self.claim('c-dddddddddd', 's-bbbbbbbbbb', 'residence', '1850', [P[0]])
        self.build()
        text = self.page()
        self.assertIn('Suwalki parish register', text)
        self.assertNotIn('Sealed adoption file', text)

    def test_top_places_count_each_person_once_and_skip_unpublishable_claims(self):
        self.seed_kowalskis()
        self.place('l-bbbbbbbbbb', 'Oak Hill')
        for cid, pid in (('c-eeeeeeeeee', P[0]), ('c-ffffffffff', P[0]), ('c-gggggggggg', P[1])):
            self.claim(cid, 's-aaaaaaaaaa', 'residence', '1800', [pid])
            self.conn.execute('UPDATE claims SET place_id = ? WHERE id = ?', (ELM, cid))
        self.claim('c-hhhhhhhhhh', 's-aaaaaaaaaa', 'residence', '1801', [P[1]], status='suggested')
        self.conn.execute("UPDATE claims SET place_id = 'l-bbbbbbbbbb' WHERE id = 'c-hhhhhhhhhh'")
        self.build()
        text = self.page()
        self.assertRegex(text, r'Elm Creek</a> \(2 people\)')
        self.assertNotIn('Oak Hill', text)


class SwitchTests(_Surnames):
    def test_home_page_links_surnames_under_each_letter(self):
        self.seed_kowalskis()
        self.build()
        self.assertIn('<a href="surnames/kowalski.html">Kowalski</a>', self.home())
        self.assertIn('<a href="./surnames/index.html">Surnames</a>', self.home())

    def test_switch_off_writes_no_surname_pages_and_removes_old_ones(self):
        self.seed_kowalskis()
        self.build()
        self.assertTrue((self.out / 'surnames' / 'kowalski.html').is_file())
        self.config('site:\n  surname_pages: false\n')
        self.build()
        self.assertFalse((self.out / 'surnames').exists())
        self.assertNotIn('surnames/', self.home())

    def test_page_counts_match_and_rebuild_is_identical(self):
        self.seed_kowalskis()
        self.note('kowalski', 'The name means smith.\n')
        preview = self.build(dry_run=True)['pages']
        real = self.build()['pages']
        self.assertEqual(preview, real)
        self.assertEqual(real, len(list(self.out.rglob('*.html'))))
        before = {p.name: p.read_bytes() for p in (self.out / 'surnames').glob('*.html')}
        self.build()
        after = {p.name: p.read_bytes() for p in (self.out / 'surnames').glob('*.html')}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
