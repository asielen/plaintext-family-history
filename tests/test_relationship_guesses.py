"""
test_relationship_guesses.py - a relationship guess and the working answer it
rests on (SPEC §9, §17a; lint W155).

Covers:
  * the fold (`_lib.collapse_relationship_entries`): a `status: hypothesis`
    entry and a claim-backed entry for the same tie become one sourced entry,
    keeping the guess's `hypothesis: h-…` link; guesses alone stay guesses;
    a different nature, or an entry citing `source:`, is not folded;
  * `fha lint --fix-reciprocal` graduating a guess in place rather than adding
    a sourced twin beside it (the duplicate a real archive turned up);
  * W155: a guess left beside a sourced entry, and a `hypothesis:` link to an
    h-id no question entry defines; the needs-sourcing backlog no longer
    calls a settled guess unsourced;
  * `fha person relate --working-answer`: writes the link, refuses an id that
    no question entry defines, writes nothing when it refuses.

Expected shapes come from SPEC §9's own wording: "graduating is the same entry,
not a second one".
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(ROOT / 'tests'))

import lint
import person
from _lib import collapse_relationship_entries, read_record
from test_lint_relationships import (
    CHILD, CLAIM, PARENT, SOURCE, _build, _codes, _person, _rel_source,
)

H = 'h-aaaaaaaaaa'
H_OTHER = 'h-bbbbbbbbbb'

QUESTIONS = (
    '# Questions\n\n'
    '## Q: Who were Ann\'s parents?\n'
    '- origin: human\n'
    '- status: open\n'
    f'- refs: [{CHILD.lower()}]\n'
    f'- id: {H}\n'
    '- guess: Bob Kid\n'
    '- basis: the only Kid household nearby\n'
    '- verify: her baptism record\n'
)


def _front(entries: str) -> str:
    return f'---\nid: {CHILD}\nname: Ann Kid\nrelationships:\n{entries}tier: stub\n---\n'


SOURCED = (f'  - to: "[[{PARENT}|Bob Kid]]"\n'
           '    type: parent\n'
           f'    claims: ["[[{CLAIM}]]"]\n')
GUESS = (f'  - to: "[[{PARENT}|Bob Kid]]"\n'
         '    type: parent\n'
         '    status: hypothesis\n'
         f'    hypothesis: {H}\n')


def _entries(text: str) -> list[dict]:
    import yaml
    return yaml.safe_load(text.split('---')[1])['relationships']


class FoldTests(unittest.TestCase):
    def test_guess_after_sourced_entry_graduates_and_keeps_its_link(self):
        out, removed = collapse_relationship_entries(_front(SOURCED + GUESS))
        self.assertEqual(removed, 1)
        (entry,) = _entries(out)
        self.assertNotIn('status', entry)
        self.assertEqual(entry['hypothesis'], H)
        self.assertEqual(entry['claims'], [f'[[{CLAIM.lower()}]]'])

    def test_guess_before_sourced_entry_graduates_too(self):
        out, removed = collapse_relationship_entries(_front(GUESS + SOURCED))
        self.assertEqual(removed, 1)
        (entry,) = _entries(out)
        self.assertNotIn('status', entry)
        self.assertEqual(entry['hypothesis'], H)
        self.assertTrue(entry['claims'])

    def test_two_guesses_stay_one_guess(self):
        out, removed = collapse_relationship_entries(_front(GUESS + GUESS))
        self.assertEqual(removed, 1)
        (entry,) = _entries(out)
        self.assertEqual(entry['status'], 'hypothesis')

    def test_a_different_nature_is_a_different_tie(self):
        adoptive = GUESS.replace('    type: parent\n', '    type: parent\n    subtype: adoptive\n')
        out, removed = collapse_relationship_entries(_front(SOURCED + adoptive))
        self.assertEqual(removed, 0)
        self.assertEqual(out, _front(SOURCED + adoptive))

    def test_guess_beside_a_source_cited_entry_is_left_for_lint(self):
        cited = (f'  - to: "[[{PARENT}|Bob Kid]]"\n    type: parent\n'
                 f'    source: "[[{SOURCE}]]"\n')
        self.assertEqual(collapse_relationship_entries(_front(cited + GUESS))[1], 0)


class FixReciprocalGraduatesTests(unittest.TestCase):
    def test_mirror_absorbs_the_guess_on_the_other_side(self):
        # Ann's record cites the claim; Bob's record still holds the guess.
        child_f, child_t = _person(CHILD, 'kid', 'ann', relationships=(
            'relationships:\n' + SOURCED))
        parent_f, parent_t = _person(PARENT, 'kid', 'bob', relationships=(
            'relationships:\n'
            f'  - to: "[[{CHILD}|Ann Kid]]"\n'
            '    type: child\n'
            '    status: hypothesis\n'
            f'    hypothesis: {H}\n'))
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT)
        root = _build({child_f: child_t, parent_f: parent_t, src_rel: src_text,
                       'notes/questions.md': QUESTIONS})
        lint.run_lint(root, {}, fix_reciprocal=True)
        (entry,) = read_record(root / parent_f)['meta']['relationships']
        self.assertEqual(entry['type'], 'child')
        self.assertNotIn('status', entry)
        self.assertEqual(entry['hypothesis'], H)
        findings, _ = lint._run_lint_core(root, {})
        self.assertEqual(_codes(findings, 'W155'), [])
        self.assertEqual(_codes(findings, 'W116'), [])


class W155Tests(unittest.TestCase):
    def _root(self, relationships: str, questions: str = QUESTIONS):
        child_f, child_t = _person(CHILD, 'kid', 'ann', relationships=relationships)
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT)
        return _build({child_f: child_t, parent_f: parent_t, src_rel: src_text,
                       'notes/questions.md': questions})

    def test_guess_beside_sourced_entry_is_reported_and_not_called_unsourced(self):
        root = self._root('relationships:\n' + SOURCED + GUESS)
        findings, registry = lint._run_lint_core(root, {})
        (w,) = _codes(findings, 'W155')
        self.assertIn('already settled', w.message)
        backlog = '\n'.join(lint._needs_sourcing_backlog(registry))
        self.assertNotIn('not yet sourced', backlog)

    def test_link_to_a_defined_working_answer_is_quiet(self):
        root = self._root('relationships:\n' + GUESS)
        findings, registry = lint._run_lint_core(root, {})
        self.assertEqual(_codes(findings, 'W155'), [])
        backlog = '\n'.join(lint._needs_sourcing_backlog(registry))
        self.assertIn(f'working answer {H}', backlog)

    def test_link_to_an_undefined_working_answer_is_reported(self):
        root = self._root('relationships:\n' + GUESS.replace(H, H_OTHER))
        findings, _ = lint._run_lint_core(root, {})
        (w,) = _codes(findings, 'W155')
        self.assertIn(f'no question entry defines {H_OTHER}', w.message)

    def test_a_ruled_out_guess_mentioned_only_in_context_still_counts_as_gone(self):
        # SPEC §17a: a ruled-out guess's lines leave the entry; only a dated
        # context line still names its id. A mention is not a definition.
        ruled_out = QUESTIONS.replace(f'- id: {H}\n- guess: Bob Kid\n', '').replace(
            '- verify: her baptism record\n',
            f'- context:\n  - 2026-06-01 (human): ruled out {H}: Bob Kid - wrong parish\n')
        root = self._root('relationships:\n' + GUESS, ruled_out)
        findings, _ = lint._run_lint_core(root, {})
        self.assertEqual(len(_codes(findings, 'W155')), 1)

    def test_a_value_that_is_not_an_h_id_is_reported(self):
        root = self._root('relationships:\n' + GUESS.replace(H, 'the Kid family'))
        findings, _ = lint._run_lint_core(root, {})
        (w,) = _codes(findings, 'W155')
        self.assertIn('not a working-answer id', w.message)


class RelateWorkingAnswerTests(unittest.TestCase):
    def setUp(self):
        child_f, child_t = _person(CHILD, 'kid', 'ann')
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        self.child_f, self.parent_f = child_f, parent_f
        self.root = _build({child_f: child_t, parent_f: parent_t,
                            'notes/questions.md': QUESTIONS})

    def test_writes_the_link_on_both_sides(self):
        r = person.run_relate(self.root, CHILD, 'parent', PARENT, reciprocal=True,
                              working_answer=H.upper().replace('H-', 'H-'))
        self.assertEqual(r.data['status'], 'ok', r.messages)
        for rel in (self.child_f, self.parent_f):
            (entry,) = read_record(self.root / rel)['meta']['relationships']
            self.assertEqual((entry['status'], entry['hypothesis']), ('hypothesis', H))

    def test_unknown_working_answer_is_refused_and_nothing_written(self):
        before = (self.root / self.child_f).read_text(encoding='utf-8')
        r = person.run_relate(self.root, CHILD, 'parent', PARENT, working_answer=H_OTHER)
        self.assertEqual(r.data['status'], 'refused')
        self.assertIn('No question entry defines', r.messages[0].text)
        self.assertEqual((self.root / self.child_f).read_text(encoding='utf-8'), before)

    def test_not_an_h_id_is_refused(self):
        r = person.run_relate(self.root, CHILD, 'parent', PARENT, working_answer='p-aaaaaaaaaa')
        self.assertEqual(r.data['status'], 'refused')
        self.assertIn('not a working-answer id', r.messages[0].text)

    def test_cli_flag(self):
        import io
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            code = person._standalone_main(
                ['relate', CHILD, '--parent', PARENT, '--working-answer', H,
                 '--root', str(self.root)])
        self.assertEqual(code, 0)
        (entry,) = read_record(self.root / self.child_f)['meta']['relationships']
        self.assertEqual(entry['hypothesis'], H)


if __name__ == '__main__':
    unittest.main()
