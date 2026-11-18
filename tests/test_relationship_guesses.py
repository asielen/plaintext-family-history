"""
test_relationship_guesses.py - a relationship guess and the working answer it
rests on (SPEC §9, §17a; lint W116, W155).

The rule under test: tools never rewrite a guess (`status: hypothesis`). A
guess is the owner's own lines, often with a note saying why they believed
it, so turning it into the sourced entry is the owner's edit; lint names the
leftover (W155) and says exactly what to change.

Covers:
  * the fold (`_lib.collapse_relationship_entries`) leaves a guess beside a
    sourced entry untouched, comments and all; two guesses naming different
    working answers stay two;
  * `fha lint --fix-reciprocal` adds the sourced mirror beside a guess on the
    other record, never deleting it, and refuses to save a header that would
    no longer read;
  * W155: a guess beside an entry citing an ACCEPTED claim, and a
    `hypothesis:` link to an h-id no question entry defines; a suggested claim
    settles nothing; the needs-sourcing backlog agrees with W155;
  * `fha person relate --working-answer`: writes the link, refuses an id that
    no question entry defines, writes nothing when it refuses.
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
         '    status: hypothesis   # Aunt May remembers him at the farm\n'
         f'    hypothesis: {H}\n'
         '    note: family bible page 3 names a Robert\n')


def _entries(text: str) -> list[dict]:
    import yaml
    return yaml.safe_load(text.split('---')[1])['relationships']


class FoldLeavesGuessesAloneTests(unittest.TestCase):
    def test_guess_beside_sourced_entry_is_kept_with_its_comment_and_note(self):
        for entries in (SOURCED + GUESS, GUESS + SOURCED):
            out, removed = collapse_relationship_entries(_front(entries))
            self.assertEqual((removed, out), (0, _front(entries)))

    def test_two_guesses_naming_different_working_answers_stay_two(self):
        other = GUESS.replace(H, H_OTHER)
        self.assertEqual(collapse_relationship_entries(_front(GUESS + other))[1], 0)

    def test_sourced_entries_still_fold_and_keep_a_working_answer_link(self):
        # One entry per person and role: a link on one sourced entry must not
        # stop a second record's evidence joining it.
        linked = SOURCED + f'    hypothesis: {H}\n'
        other = SOURCED.replace(CLAIM, 'C-eeeeeeeeee')
        for entries in (linked + other, other + linked):
            out, removed = collapse_relationship_entries(_front(entries))
            self.assertEqual(removed, 1)
            (entry,) = _entries(out)
            self.assertEqual(entry['hypothesis'], H)
            self.assertEqual(len(entry['claims']), 2)

    def test_two_identical_guesses_still_fold(self):
        out, removed = collapse_relationship_entries(_front(GUESS + GUESS))
        self.assertEqual(removed, 1)
        (entry,) = _entries(out)
        self.assertEqual((entry['status'], entry['hypothesis']), ('hypothesis', H))


class FixReciprocalBesideAGuessTests(unittest.TestCase):
    BOB_GUESS = ('relationships:\n'
                 f'  - to: "[[{CHILD}|Ann Kid]]"\n'
                 '    type: child\n'
                 '    status: hypothesis   # Aunt May remembers Ann at the farm\n'
                 f'    hypothesis: {H}\n')

    def _root(self, bob_block: str):
        child_f, child_t = _person(CHILD, 'kid', 'ann', relationships='relationships:\n' + SOURCED)
        parent_f, parent_t = _person(PARENT, 'kid', 'bob', relationships=bob_block)
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT)
        root = _build({child_f: child_t, parent_f: parent_t, src_rel: src_text,
                       'notes/questions.md': QUESTIONS})
        return root, parent_f

    def test_guess_is_not_a_mirror_and_w116_says_what_will_happen(self):
        root, _ = self._root(self.BOB_GUESS)
        findings, _ = lint._run_lint_core(root, {})
        (w,) = _codes(findings, 'W116')
        self.assertIn('as a guess', w.message)
        self.assertIn('left as you wrote it', w.message)

    def test_a_guess_of_another_nature_gets_the_plain_w116(self):
        adoptive = self.BOB_GUESS.replace('    type: child\n', '    type: child\n    subtype: adoptive\n')
        root, _ = self._root(adoptive)
        findings, _ = lint._run_lint_core(root, {})
        (w,) = _codes(findings, 'W116')
        self.assertNotIn('left as you wrote it', w.message)

    def test_fix_adds_the_sourced_entry_and_keeps_the_guess_and_its_comment(self):
        root, parent_f = self._root(self.BOB_GUESS)
        lint.run_lint(root, {}, fix_reciprocal=True)
        text = (root / parent_f).read_text(encoding='utf-8')
        self.assertIn('# Aunt May remembers Ann at the farm', text)
        entries = read_record(root / parent_f)['meta']['relationships']
        self.assertEqual(sorted(bool(e.get('claims')) for e in entries), [False, True])
        findings, _ = lint._run_lint_core(root, {})
        self.assertEqual(_codes(findings, 'W116'), [])
        (w155,) = _codes(findings, 'W155')
        self.assertIn('Delete the guess', w155.message)

    def test_a_wide_dash_gap_still_saves_a_readable_record(self):
        # Valid YAML with keys at six spaces; the new entry must not break it.
        wide = ('relationships:\n'
                f'  -   to: "[[{CHILD}|Ann Kid]]"\n'
                '      type: child\n'
                '      status: hypothesis\n')
        root, parent_f = self._root(wide)
        lint.run_lint(root, {}, fix_reciprocal=True)
        meta = read_record(root / parent_f)['meta']
        self.assertIsInstance(meta.get('relationships'), list)


class W155Tests(unittest.TestCase):
    def _root(self, relationships: str, questions: str = QUESTIONS, status: str = 'accepted'):
        child_f, child_t = _person(CHILD, 'kid', 'ann', relationships=relationships)
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT, status=status)
        return _build({child_f: child_t, parent_f: parent_t, src_rel: src_text,
                       'notes/questions.md': questions})

    def test_guess_beside_sourced_entry_is_reported_and_not_called_unsourced(self):
        root = self._root('relationships:\n' + SOURCED + GUESS)
        findings, registry = lint._run_lint_core(root, {})
        (w,) = _codes(findings, 'W155')
        self.assertIn('settled it', w.message)
        backlog = '\n'.join(lint._needs_sourcing_backlog(registry))
        self.assertNotIn('not yet sourced', backlog)

    def test_a_source_only_entry_settles_nothing(self):
        cited = (f'  - to: "[[{PARENT}|Bob Kid]]"\n    type: parent\n'
                 f'    source: "[[{SOURCE}]]"\n')
        root = self._root('relationships:\n' + cited + GUESS, status='suggested')
        findings, registry = lint._run_lint_core(root, {})
        self.assertEqual(_codes(findings, 'W155'), [])
        self.assertIn('(hypothesis, working answer', '\n'.join(lint._needs_sourcing_backlog(registry)))

    def test_a_claim_of_another_nature_settles_nothing(self):
        # The sourced entry says biological, but cites an adoptive claim (W115).
        child_f, child_t = _person(CHILD, 'kid', 'ann', relationships='relationships:\n' + SOURCED + GUESS)
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT, subtype='adoptive')
        root = _build({child_f: child_t, parent_f: parent_t, src_rel: src_text,
                       'notes/questions.md': QUESTIONS})
        findings, _ = lint._run_lint_core(root, {})
        self.assertTrue(_codes(findings, 'W115'))
        self.assertEqual(_codes(findings, 'W155'), [])

    def test_a_suggested_claim_settles_nothing(self):
        for status in ('suggested', 'disputed'):
            root = self._root('relationships:\n' + SOURCED + GUESS, status=status)
            findings, registry = lint._run_lint_core(root, {})
            self.assertEqual(_codes(findings, 'W155'), [], status)
            backlog = '\n'.join(lint._needs_sourcing_backlog(registry))
            self.assertIn('(hypothesis, working answer', backlog, status)

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
