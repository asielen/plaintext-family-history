"""
test_site_questions.py - the "Questions for the family" page of the shared
site (`site: questions_page: true`, SPEC §17b `share: family`).

A relative sees only the questions the owner marked `share: family`, only
their heading and the people and records they name, and never anyone the
site withholds. Expected answers come from SPEC §17b, §19 and §21, not from
the code's output. Temp fixtures only.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))

from test_site_rings_calendar import (   # noqa: E402
    DAD, LIVE, ME, QUIET, UNK, _Base, site,
)

ON = 'site:\n  questions_page: true\n'


def q(heading, *, refs, status='open', share='family', extra=''):
    lines = [f'## Q: {heading}', '- origin: human', f'- status: {status}',
             f'- refs: [{", ".join(refs)}]']
    if share:
        lines.append(f'- share: {share}')
    lines.append(extra or '- guess: SECRETGUESS\n- basis: SECRETBASIS\n- verify: SECRETVERIFY\n'
                 '- context:\n  - 2026-06-12 (human): SECRETCONTEXT')
    return '\n'.join(lines) + '\n\n'


class _QBase(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.person(ME, 'Anne Hartley')
        self.person(DAD, 'Carl Hartley')
        self.person(LIVE, 'Liv Hartley', living='true')
        self.person(UNK, 'Una Hartley', living='unknown')
        self.person(QUIET, 'Quinn Hartley', extra='restricted: by-request\n')
        self.source('s-aaaaaaaaaa', 'A census')

    def questions(self, *blocks):
        path = self.root / 'notes' / 'questions.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Open Questions\n\n' + ''.join(blocks), encoding='utf-8')


class QuestionsPageTests(_QBase):
    def test_a_shared_open_question_shows_with_its_person(self):
        self.config(ON)
        self.questions(q('Where was Anne born?', refs=[ME]))
        self.build()
        html = self.page('questions.html')
        self.assertIn('Questions for the family', html)
        self.assertIn('Where was Anne born?', html)
        self.assertIn(f'persons/{ME}', html.replace('\\', '/'))

    def test_an_unshared_question_stays_home(self):
        self.config(ON)
        self.questions(q('Where was Anne born?', refs=[ME], share=''))
        self.build()
        html = self.page('questions.html')
        self.assertNotIn('Where was Anne born?', html)
        self.assertIn('There are no questions to share just now.', html)

    def test_only_family_is_a_share_value(self):
        self.config(ON)
        self.questions(q('Where was Anne born?', refs=[ME], share='public'))
        self.build()
        self.assertNotIn('Where was Anne born?', self.page('questions.html'))

    def test_family_is_case_insensitive(self):
        self.config(ON)
        self.questions(q('Where was Anne born?', refs=[ME], share='Family'))
        self.build()
        self.assertIn('Where was Anne born?', self.page('questions.html'))

    def test_an_answered_shared_question_is_not_shown(self):
        self.config(ON)
        self.questions(q('Where was Anne born?', refs=[ME], status='answered [[s-aaaaaaaaaa]]'))
        self.build()
        self.assertNotIn('Where was Anne born?', self.page('questions.html'))

    def test_working_answer_and_context_never_appear(self):
        self.config(ON)
        self.questions(q('Where was Anne born?', refs=[ME, 's-aaaaaaaaaa']))
        self.build()
        html = self.page('questions.html')
        self.assertIn('Where was Anne born?', html)
        for secret in ('SECRETGUESS', 'SECRETBASIS', 'SECRETVERIFY', 'SECRETCONTEXT'):
            self.assertNotIn(secret, html)

    def test_a_ref_to_a_living_person_drops_the_question(self):
        self.config(ON)
        self.questions(q('Who married Liv?', refs=[ME, LIVE]),
                       q('Who was Una?', refs=[UNK]),
                       q('Who is Quinn?', refs=[QUIET]),
                       q('Where was Carl born?', refs=[DAD]))
        self.build()
        html = self.page('questions.html')
        for gone in ('Who married Liv?', 'Who was Una?', 'Who is Quinn?', 'Liv Hartley'):
            self.assertNotIn(gone, html)
        self.assertIn('Where was Carl born?', html)

    def test_a_heading_naming_a_living_person_drops_the_question(self):
        self.config(ON)
        self.questions(q(f'Is [[{LIVE}|Liv Hartley]] related to Carl?', refs=[DAD]))
        self.build()
        html = self.page('questions.html')
        self.assertNotIn('Liv Hartley', html)
        self.assertNotIn('Living Person', html)
        self.assertNotIn('related to Carl', html)

    def test_a_ref_to_a_source_with_no_page_drops_the_question(self):
        self.config(ON)
        self.questions(q('Whose census is this?', refs=[ME, 's-zzzzzzzzzz']))
        self.build()
        self.assertNotIn('Whose census is this?', self.page('questions.html'))

    def test_a_question_homed_with_a_by_request_person_is_dropped(self):
        self.config(ON)
        rel = f'people/hartley_quinn_research_{QUIET}.md'
        path = self.root / rel
        path.write_text(
            f'---\nid: {QUIET}\n---\n# Research\n\n## Open Questions\n\n'
            + q('Where did Quinn go?', refs=[ME]), encoding='utf-8')
        self.build()
        self.assertNotIn('Where did Quinn go?', self.page('questions.html'))

    def research_file(self, pid, surname, block):
        path = self.root / f'people/{surname}__x_research_{pid}.md'
        path.write_text(f'---\nid: {pid}\n---\n# Research\n\n## Open Questions\n\n' + block,
                        encoding='utf-8')

    def test_a_question_homed_with_a_withheld_person_is_dropped(self):
        # TOOLING §12: a question kept in a living, unknown or private
        # person's research file is about them, so it stays home even when
        # its refs name only people with pages.
        self.config(ON)
        self.person('p-ffffffffff', 'Rhea Hartley', extra='restricted: true\n')
        self.research_file(LIVE, 'hartley', q('HOME-live: where was her grandfather from?', refs=[DAD]))
        self.research_file(UNK, 'hartley', q('HOME-unk: where was her grandfather from?', refs=[DAD]))
        self.research_file('p-ffffffffff', 'hartley',
                           q('HOME-rhea: where was her grandfather from?', refs=[DAD]))
        self.research_file(ME, 'hartley', q('HOME-anne: where was her father from?', refs=[DAD]))
        self.build()
        html = self.page('questions.html')
        for gone in ('HOME-live', 'HOME-unk', 'HOME-rhea'):
            self.assertNotIn(gone, html)
        self.assertIn('HOME-anne', html)

    def test_a_markdown_link_to_a_living_person_drops_the_question(self):
        self.config(ON)
        self.questions(q(f'Is [Liv Hartley](../people/hartley_{LIVE}.md) Carl\'s niece?',
                         refs=[DAD]))
        self.build()
        html = self.page('questions.html')
        self.assertNotIn('Liv Hartley', html)
        self.assertNotIn(LIVE, html)

    def test_a_withheld_record_in_the_heading_drops_the_question(self):
        # Showing "Restricted" would tell relatives a private record exists.
        self.config(ON)
        self.source('s-bbbbbbbbbb', 'A private letter', restricted=1)
        self.questions(q('What does [[s-bbbbbbbbbb]] say about Carl?', refs=[DAD]),
                       q('Who is [[p-zzzzzzzzzz]]?', refs=[DAD]))
        self.build()
        html = self.page('questions.html')
        self.assertNotIn('say about Carl', html)
        self.assertNotIn('Restricted', html)
        self.assertNotIn('p-zzzzzzzzzz', html)

    def test_a_living_person_in_refs_as_a_wikilink_drops_the_question(self):
        self.config(ON)
        self.questions(q('REFS-WIKI: who was she?', refs=[f'[[{LIVE}]]']),
                       q('REFS-DISP: who was she?', refs=[f'[[{LIVE}|Liv Hartley]]']),
                       q('REFS-ODD: who was she?', refs=['Liv']))
        self.build()
        html = self.page('questions.html')
        for gone in ('REFS-WIKI', 'REFS-DISP', 'REFS-ODD'):
            self.assertNotIn(gone, html)

    def test_a_claim_about_a_living_person_in_refs_drops_the_question(self):
        self.config(ON)
        self.source('s-cccccccccc', 'Liv birth')
        self.claim('c-cccccccccc', 's-cccccccccc', 'birth', '1990', [LIVE])
        self.claim('c-bbbbbbbbbb', 's-aaaaaaaaaa', 'birth', '1900', [DAD])
        self.questions(q('CLMREF-LIVING: is this birth right?', refs=['c-cccccccccc']),
                       q('CLMHEAD-LIVING: is [[c-cccccccccc]] right?', refs=[DAD]),
                       q('CLMREF-DAD: is this birth right?', refs=['c-bbbbbbbbbb']))
        self.build()
        html = self.page('questions.html')
        self.assertNotIn('CLMREF-LIVING', html)
        self.assertNotIn('CLMHEAD-LIVING', html)
        self.assertIn('CLMREF-DAD', html)

    def test_questions_are_sorted_by_heading(self):
        self.config(ON)
        self.questions(q('Zed question?', refs=[ME]), q('Alpha question?', refs=[DAD]))
        self.build()
        html = self.page('questions.html')
        self.assertLess(html.index('Alpha question?'), html.index('Zed question?'))

    def test_the_nav_links_to_the_page_when_it_is_built(self):
        self.config(ON)
        self.questions(q('Where was Anne born?', refs=[ME]))
        self.build()
        self.assertIn('questions.html', self.page('index.html'))


class QuestionsSwitchTests(_QBase):
    def test_off_by_default_writes_no_page_and_no_nav_link(self):
        self.config('')
        self.questions(q('Where was Anne born?', refs=[ME]))
        self.build()
        self.assertFalse((self.out / 'questions.html').exists())
        self.assertNotIn('questions.html', self.page('index.html'))

    def test_turning_it_off_clears_a_stale_page(self):
        self.config(ON)
        self.questions(q('Where was Anne born?', refs=[ME]))
        self.build()
        self.assertTrue((self.out / 'questions.html').exists())
        self.config('')
        self.build()
        self.assertFalse((self.out / 'questions.html').exists())

    def test_the_linked_preview_has_no_page(self):
        self.config(ON)
        self.questions(q('Where was Anne born?', refs=[ME]))
        self.build(linked=True)
        self.assertFalse((self.out / 'questions.html').exists())
        self.assertNotIn('questions.html', self.page('index.html'))

    def test_a_typo_warns_and_leaves_it_off(self):
        self.config('site:\n  questions_page: maybe\n')
        self.questions(q('Where was Anne born?', refs=[ME]))
        result = self.build()
        self.assertFalse((self.out / 'questions.html').exists())
        self.assertIn('questions_page', ' '.join(result['messages']))

    def test_rebuild_is_byte_identical(self):
        self.config(ON)
        self.questions(q('Where was Anne born?', refs=[ME]), q('Where was Carl born?', refs=[DAD]))
        self.build()
        first = (self.out / 'questions.html').read_bytes()
        self.build()
        self.assertEqual(first, (self.out / 'questions.html').read_bytes())

    def test_page_count_includes_the_questions_page(self):
        self.config('')
        off = self.build()
        self.config(ON)
        on = self.build()
        self.assertEqual(on['pages'], off['pages'] + 1)


if __name__ == '__main__':
    unittest.main()
