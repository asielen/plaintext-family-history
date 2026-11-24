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
