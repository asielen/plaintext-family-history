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
    no longer read or would change another field, saying which, with
    `--dry-run` printing the same refusal;
  * the formatter lowercases a `hypothesis:` link like the other id subkeys;
  * W155: a guess beside an entry citing an ACCEPTED claim, and a
    `hypothesis:` link to an h-id no question entry defines; a suggested claim
    settles nothing; the needs-sourcing backlog agrees with W155;
  * `fha person relate --working-answer`: writes the link, refuses an id that
    no question entry defines, writes nothing when it refuses, and names the
    line to add by hand on any side whose entry was already there.
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

    def test_a_bare_dash_entry_in_a_crlf_file_folds_to_readable_yaml(self):
        bare = ('  -\r\n'
                f'    to: "[[{PARENT}|Bob Kid]]"\r\n'
                '    type: parent\r\n'
                f'    claims: ["[[{CLAIM}]]"]\r\n')
        linked = SOURCED.replace('\n', '\r\n').replace(CLAIM, 'C-eeeeeeeeee') \
            + f'    hypothesis: {H}\r\n'
        text = _front(bare + linked).replace('\n', '\r\n').replace('\r\r\n', '\r\n')
        out, removed = collapse_relationship_entries(text)
        self.assertEqual(removed, 1)
        (entry,) = _entries(out.replace('\r\n', '\n'))
        self.assertEqual(entry['hypothesis'], H)

    def test_a_free_text_link_survives_the_fold(self):
        # The owner typed a note where the id goes; the fold must copy it as
        # the same text, not write a line that breaks the whole header.
        noted = SOURCED.replace(CLAIM, 'C-eeeeeeeeee') + '    hypothesis: "see note: 1931 #2"\n'
        out, removed = collapse_relationship_entries(_front(SOURCED + noted))
        self.assertEqual(removed, 1)
        (entry,) = _entries(out)
        self.assertEqual(entry['hypothesis'], 'see note: 1931 #2')

    def test_two_identical_guesses_still_fold(self):
        out, removed = collapse_relationship_entries(_front(GUESS + GUESS))
        self.assertEqual(removed, 1)
        (entry,) = _entries(out)
        self.assertEqual((entry['status'], entry['hypothesis']), ('hypothesis', H))


class FormatterTests(unittest.TestCase):
    def test_a_working_answer_link_is_written_lowercase_like_other_ids(self):
        text = _front(GUESS.replace(H, H.upper())) + '\n# Ann Kid\n'
        out, _fixes, _manual = lint.format_record_text(text, 'person')
        self.assertEqual(_entries(out)[0]['hypothesis'], H)
        self.assertIn('# Aunt May remembers him at the farm', out)


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

    def _fix(self, bob_block: str, dry_run: bool):
        root, parent_f = self._root(bob_block)
        before = (root / parent_f).read_text(encoding='utf-8')
        r = lint.run_lint(root, {}, fix_reciprocal=True, dry_run=dry_run)
        self.assertEqual((root / parent_f).read_text(encoding='utf-8'), before)
        return '\n'.join(r.data.get('progress', []))

    # Items at four spaces: the mirror lands at two, and the header no longer parses.
    DEEP = ('relationships:\n'
            '    - to: "[[P-dddddddddd|Cal Kid]]"\n'
            '      type: child\n')
    # No relationships: block, but a quoted note with a line that looks like one.
    LOOKALIKE = "note: 'he wrote\nrelationships:\nnothing more'\n"

    def test_refusal_names_an_unreadable_header(self):
        for dry_run in (False, True):
            progress = self._fix(self.DEEP, dry_run)
            self.assertIn('would leave its header unreadable', progress, dry_run)

    def test_refusal_names_a_change_to_other_fields(self):
        for dry_run in (False, True):
            progress = self._fix(self.LOOKALIKE, dry_run)
            self.assertIn('would change other fields', progress, dry_run)
            self.assertNotIn('unreadable', progress, dry_run)
            self.assertNotIn('would add', progress, dry_run)

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

    CITED = (f'  - to: "[[{PARENT}|Bob Kid]]"\n    type: parent\n'
             f'    source: "[[{SOURCE}]]"\n')

    def test_a_source_entry_without_an_accepted_claim_settles_nothing(self):
        root = self._root('relationships:\n' + self.CITED + GUESS, status='suggested')
        findings, registry = lint._run_lint_core(root, {})
        self.assertEqual(_codes(findings, 'W155'), [])
        self.assertIn('(hypothesis, working answer', '\n'.join(lint._needs_sourcing_backlog(registry)))

    def test_a_source_entry_whose_source_proves_the_tie_settles_the_guess(self):
        # Otherwise the owner is told to source a parent who is sourced.
        root = self._root('relationships:\n' + self.CITED + GUESS)
        findings, registry = lint._run_lint_core(root, {})
        self.assertEqual(len(_codes(findings, 'W155')), 1)
        self.assertNotIn('not yet sourced', '\n'.join(lint._needs_sourcing_backlog(registry)))

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

    def test_an_unresolved_name_settles_nothing(self):
        # Both entries name someone the archive does not know; the accepted
        # claim is about Bob, so it settles no tie to "Robert Other".
        unknown = lambda s: s.replace(f'"[[{PARENT}|Bob Kid]]"', '"[[Robert Other]]"')
        root = self._root('relationships:\n' + unknown(SOURCED) + unknown(GUESS))
        findings, _ = lint._run_lint_core(root, {})
        self.assertEqual(_codes(findings, 'W155'), [])

    def test_a_merged_target_is_read_through_the_merge(self):
        # Bob's duplicate is merged into Bob; both of Ann's entries still name the duplicate.
        dup = 'P-dddddddddd'
        dup_f, dup_t = _person(dup, 'kid', 'bobdup')
        dup_t = dup_t.replace('tier: stub\n', f'tier: stub\nstatus: merged\nmerged_into: {PARENT}\n')
        point = lambda s: s.replace(f'[[{PARENT}|Bob Kid]]', f'[[{dup}|Bob Dup]]')
        child_f, child_t = _person(CHILD, 'kid', 'ann',
                                   relationships='relationships:\n' + point(SOURCED) + point(GUESS))
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT)
        root = _build({child_f: child_t, parent_f: parent_t, dup_f: dup_t, src_rel: src_text,
                       'notes/questions.md': QUESTIONS})
        findings, registry = lint._run_lint_core(root, {})
        self.assertEqual(len(_codes(findings, 'W155')), 1)
        self.assertNotIn('not yet sourced', '\n'.join(lint._needs_sourcing_backlog(registry)))

    def test_merge_keeps_two_guesses_naming_different_working_answers(self):
        import confirm
        g1 = {'to': f'[[{PARENT}]]', 'type': 'parent', 'status': 'hypothesis', 'hypothesis': H}
        g2 = dict(g1, hypothesis=H_OTHER)
        sourced = {'to': f'[[{PARENT}]]', 'type': 'parent', 'claims': [f'[[{CLAIM}]]'],
                   'hypothesis': H}
        key = lambda e: confirm._relationship_edge_key(e, None)
        self.assertNotEqual(key(g1), key(g2))
        self.assertEqual(key(sourced), key(dict(sourced, hypothesis=H_OTHER)))

    def test_merging_two_records_keeps_both_working_answer_links(self):
        # Each record guessed Bob as the parent for a different reason; the
        # merge must keep both reasons, not drop the duplicate's.
        import confirm
        dup = 'P-dddddddddd'
        child_f, child_t = _person(CHILD, 'kid', 'ann', relationships='relationships:\n' + GUESS)
        dup_f, dup_t = _person(dup, 'kid', 'annie',
                               relationships='relationships:\n' + GUESS.replace(H, H_OTHER))
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        root = _build({child_f: child_t, dup_f: dup_t, parent_f: parent_t,
                       'notes/questions.md': QUESTIONS})
        r = confirm.run_confirm_merge(root, person_merged=dup, into=CHILD,
                                      reason='same person: one baptism')
        self.assertNotIn(r.data.get('status'), ('refused', 'error'), r.messages)
        links = sorted(e.get('hypothesis') for e in
                       read_record(root / child_f)['meta']['relationships'])
        self.assertEqual(links, [H, H_OTHER])

    def _merge(self, survivor_block: str, dup_block: str) -> list[dict]:
        import confirm
        dup = 'P-dddddddddd'
        child_f, child_t = _person(CHILD, 'kid', 'ann', relationships='relationships:\n' + survivor_block)
        dup_f, dup_t = _person(dup, 'kid', 'annie', relationships='relationships:\n' + dup_block)
        parent_f, parent_t = _person(PARENT, 'kid', 'bob')
        src_rel, src_text = _rel_source(SOURCE, CLAIM, CHILD, PARENT)
        root = _build({child_f: child_t, dup_f: dup_t, parent_f: parent_t, src_rel: src_text,
                       'notes/questions.md': QUESTIONS})
        r = confirm.run_confirm_merge(root, person_merged=dup, into=CHILD,
                                      reason='same person: one baptism')
        self.assertNotIn(r.data.get('status'), ('refused', 'error'), r.messages)
        return read_record(root / child_f)['meta']['relationships']

    def test_merge_carries_a_sourced_link_only_the_duplicate_had(self):
        (entry,) = self._merge(SOURCED, SOURCED + f'    hypothesis: {H}\n')
        self.assertEqual((entry['hypothesis'], len(entry['claims'])), (H, 1))

    def test_merge_of_a_linked_and_an_unlinked_guess_keeps_one_linked_guess(self):
        unlinked = GUESS.replace(f'    hypothesis: {H}\n', '')
        for survivor, dup in ((unlinked, GUESS), (GUESS, unlinked)):
            (entry,) = self._merge(survivor, dup)
            self.assertEqual((entry['status'], entry['hypothesis']), ('hypothesis', H))

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

    def test_dangling_link_on_a_sourced_entry_never_says_remove_the_entry(self):
        # The tie is proved; only the link is stale. Telling the owner to
        # remove the entry would have them delete a sourced parent.
        root = self._root('relationships:\n' + SOURCED + f'    hypothesis: {H_OTHER}\n')
        findings, _ = lint._run_lint_core(root, {})
        (w,) = _codes(findings, 'W155')
        self.assertNotIn('remove this entry', w.message)
        self.assertIn('no longer written down', w.message)
        self.assertIn(f'delete only the `hypothesis: {H_OTHER}` line', w.message)

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

    def _hints(self, r) -> str:
        return '\n'.join(m.text for m in r.messages)

    def test_a_side_already_there_gets_the_add_by_hand_line(self):
        # Ann already records Bob; only Bob's mirror is written. Ann's entry
        # is left as written, and the output must say so.
        person.run_relate(self.root, CHILD, 'parent', PARENT)
        for dry_run in (True, False):
            r = person.run_relate(self.root, CHILD, 'parent', PARENT, reciprocal=True,
                                  working_answer=H, dry_run=dry_run)
            self.assertIn(r.data['status'], ('ok', 'dry-run'), r.messages)
            text = self._hints(r)
            self.assertIn("ann kid's parent entry for bob kid", text)
            self.assertIn(f'`hypothesis: {H}`', text)
            self.assertNotIn("bob kid's child entry", text)
        (bob,) = read_record(self.root / self.parent_f)['meta']['relationships']
        self.assertEqual(bob['hypothesis'], H)

    def test_both_sides_already_there_get_a_line_each(self):
        person.run_relate(self.root, CHILD, 'parent', PARENT, reciprocal=True)
        r = person.run_relate(self.root, CHILD, 'parent', PARENT, reciprocal=True,
                              working_answer=H)
        self.assertEqual(r.data['status'], 'already')
        text = self._hints(r)
        self.assertIn("ann kid's parent entry for bob kid", text)
        self.assertIn("bob kid's child entry for ann kid", text)

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
