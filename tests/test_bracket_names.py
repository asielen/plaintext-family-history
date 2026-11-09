"""
test_bracket_names.py - what a child is CALLED in a couple-folder bracket list
(SPEC §12.2 "folder names are free-form human convenience", TOOLING §7 W103).

The bracket refresh decides WHO is listed from accepted parent claims. Before
this change it also overwrote what each child was called with the first word of
their recorded name. A live archive hit both failures below (names changed
here; the shapes are real):

    [Ethel + Louise + Grace + Henry]  ->  [Ethel + Grace + Harold + Ada]
    [... + Ruth + ... + Mrs. Doe]     ->  [... + Mrs. + Mrs.]

"Louise" was Ada Louise Hartley's own (middle) name, "Henry" a recorded
name variant of Harold Cole Hartley, and "Mrs." is not anyone's name.

Two shared `_lib` rules fix it, used by both W103 backends (lint's in-memory
registry and views' SQLite index), which must agree:

1. `bracket_given_name` - the default token skips a leading title; a wife
   known only as "Mrs. <husband's name> <surname>" reads "Mrs. <surname>".
2. `reconcile_bracket_entries` - a hand-written token is kept when every word
   of it belongs to that child's name or public name variants.

Fixtures only.
"""

import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod
import lint
import views
from _lib import (
    bracket_given_name,
    load_fha_yaml,
    public_name_variants,
    reconcile_bracket_entries,
)

PA = 'P-4aaaaaaaaa'
MA = 'P-4bbbbbbbbb'
LOUISE = 'P-4ccccccccc'
HAROLD = 'P-4ddddddddd'
ETHEL = 'P-4eeeeeeeee'
WALTER_DOE = 'P-4fffffffff'
SID = 'S-4aaaaaaaaa'


class BracketGivenNameTests(unittest.TestCase):
    """The default token for a child with no hand-written name to keep."""

    def test_plain_name_takes_the_first_word(self) -> None:
        self.assertEqual(bracket_given_name('Ada Louise Hartley'), 'Ada')
        self.assertEqual(bracket_given_name('Ethel'), 'Ethel')

    def test_a_wife_known_by_her_husbands_name_reads_mrs_surname(self) -> None:
        # The given name after "Mrs." is the husband's; it was never hers.
        self.assertEqual(bracket_given_name('Mrs. Walter Doe'), 'Mrs. Doe')
        self.assertEqual(bracket_given_name('Mrs. Samuel Pike'), 'Mrs. Pike')

    def test_a_title_with_only_a_surname_keeps_both(self) -> None:
        self.assertEqual(bracket_given_name('Mrs. Lowe'), 'Mrs. Lowe')
        self.assertEqual(bracket_given_name('Mr. Lowe'), 'Mr. Lowe')

    def test_other_titles_are_skipped_for_the_given_name(self) -> None:
        self.assertEqual(bracket_given_name('Sister Mary Agnes'), 'Mary')
        self.assertEqual(bracket_given_name('Dr. John Smith'), 'John')
        self.assertEqual(bracket_given_name('Miss Mary Pike'), 'Mary')

    def test_title_match_ignores_case_and_the_dot(self) -> None:
        self.assertEqual(bracket_given_name('MRS Walter Doe'), 'MRS Doe')
        self.assertEqual(bracket_given_name('dr john smith'), 'john')

    def test_a_name_that_is_only_a_title_is_returned_whole(self) -> None:
        self.assertEqual(bracket_given_name('Mrs.'), 'Mrs.')

    def test_empty_name_comes_back_unchanged(self) -> None:
        self.assertEqual(bracket_given_name(''), '')

    def test_a_wifes_surname_skips_a_suffix_and_keeps_its_particles(self) -> None:
        # "Mrs. Jr." is nobody: the last word of her name is not always the
        # surname.
        self.assertEqual(bracket_given_name('Mrs. John Smith Jr.'), 'Mrs. Smith')
        self.assertEqual(bracket_given_name('Mrs. John Smith, Jr.'), 'Mrs. Smith')
        self.assertEqual(bracket_given_name('Mrs. John Doe III'), 'Mrs. Doe')
        self.assertEqual(bracket_given_name('Mrs. Jan van der Berg'), 'Mrs. van der Berg')
        self.assertEqual(bracket_given_name('Mrs. John De Witt'), 'Mrs. De Witt')

    def test_a_kin_title_with_one_word_stays_whole(self) -> None:
        # Two nuns recorded as "Sister Mary" and "Sister Anne" must not both
        # read "Sister"; "Sister Hartley" stays whole for the same reason.
        self.assertEqual(bracket_given_name('Sister Mary'), 'Sister Mary')
        self.assertEqual(bracket_given_name('Sister Anne'), 'Sister Anne')
        self.assertEqual(bracket_given_name('Sister Hartley'), 'Sister Hartley')
        self.assertEqual(bracket_given_name('Sister Mary Agnes'), 'Mary')


class ReconcileBracketEntriesTests(unittest.TestCase):
    """Keeping the human's token for each child, and only when it still fits."""

    def test_middle_name_token_is_kept(self) -> None:
        kids = [('Ada', None, ['Ada Louise Hartley'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Louise']), ['Louise'])

    def test_name_variant_token_is_kept(self) -> None:
        kids = [('Harold', None, ['Harold Cole Hartley', 'Henry C. Hartley'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Henry']), ['Henry'])

    def test_multi_word_token_is_kept_when_every_word_fits(self) -> None:
        kids = [('Mrs. Doe', None, ['Mrs. Walter Doe'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Mrs. Doe']), ['Mrs. Doe'])

    def test_a_token_that_is_not_the_childs_name_is_replaced(self) -> None:
        kids = [('Ada', None, ['Ada Louise Hartley'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Fanny']), ['Ada'])

    def test_a_child_with_no_token_gets_the_default(self) -> None:
        kids = [('Ethel', None, ['Ethel Hartley']),
                ('Calvin', None, ['Calvin Hartley'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Ethel']),
                         ['Calvin', 'Ethel'])

    def test_a_token_naming_no_child_is_dropped(self) -> None:
        kids = [('Ethel', None, ['Ethel Hartley'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Ethel', 'Henry']),
                         ['Ethel'])

    def test_exact_default_is_claimed_before_a_looser_match(self) -> None:
        # "Pike" fits only Mrs. Samuel Pike, but "Mrs. Pike" is her exact
        # default and is claimed in the first pass; the looser "Pike" token is
        # then left over and dropped rather than crossing to her sister.
        kids = [('Mrs. Doe', None, ['Mrs. Walter Doe']),
                ('Mrs. Pike', None, ['Mrs. Samuel Pike'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Pike', 'Mrs. Pike']),
                         ['Mrs. Doe', 'Mrs. Pike'])

    def test_a_bare_title_token_is_never_kept(self) -> None:
        # "Mrs." fits both married sisters and names neither - the very
        # defect the live archive showed ("[... + Mrs. + Mrs.]").
        kids = [('Mrs. Doe', None, ['Mrs. Walter Doe']),
                ('Mrs. Pike', None, ['Mrs. Samuel Pike'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Mrs.', 'Mrs.']),
                         ['Mrs. Doe', 'Mrs. Pike'])

    def test_two_children_with_the_same_name_each_get_one_token(self) -> None:
        kids = [('Claude', None, ['Claude Doe']), ('Claude', None, ['Claude Edward Doe'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Claude', 'Claude']),
                         ['Claude', 'Claude'])

    def test_kept_token_takes_the_childs_current_label(self) -> None:
        # The label follows the claims, not the old folder name.
        kids = [('Ada', 'adopted', ['Ada Louise Hartley'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Louise']), ['Louise (adopted)'])
        kids = [('Ada', None, ['Ada Louise Hartley'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Louise (adopted)']), ['Louise'])

    def test_punctuation_and_case_do_not_block_a_match(self) -> None:
        kids = [('Mrs. Doe', None, ['Mrs. Walter Doe'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['mrs doe']), ['mrs doe'])

    def test_same_first_names_share_tokens_so_both_survive_in_any_order(self) -> None:
        # Claude Edward and Claude John both default to "Claude"; only Claude
        # Edward can be "Edward". Handing "Claude" to whichever child came
        # first gave [Claude + Claude] for one input order.
        ce = ('Claude', None, ['Claude Edward Doe'])
        cj = ('Claude', None, ['Claude John Doe'])
        for kids in ([ce, cj], [cj, ce]):
            for toks in (['Claude', 'Edward'], ['Edward', 'Claude']):
                self.assertEqual(reconcile_bracket_entries(kids, toks),
                                 ['Claude', 'Edward'], (kids, toks))

    def test_a_reused_name_keeps_the_token_that_tells_the_two_apart(self) -> None:
        # A baby who died and a later brother given his name: the human wrote
        # [John + John Henry] so the folder tells them apart.
        plain = ('John', None, ['John Hartley'])
        henry = ('John', None, ['John Henry Hartley'])
        for kids in ([plain, henry], [henry, plain]):
            self.assertEqual(reconcile_bracket_entries(kids, ['John', 'John Henry']),
                             ['John', 'John Henry'])
            self.assertEqual(reconcile_bracket_entries(kids, ['Henry', 'John']),
                             ['Henry', 'John'])

    def test_a_label_stays_with_the_child_it_was_written_for(self) -> None:
        # Both children fit both tokens; the folder's "(adopted)" already says
        # which is which, so a correct list is not reshuffled.
        bio = ('John', None, ['John Edward Doe'])
        adopted = ('John', 'adopted', ['John Edward Henry Doe'])
        for kids in ([bio, adopted], [adopted, bio]):
            self.assertEqual(
                reconcile_bracket_entries(kids, ['Edward', 'John (adopted)']),
                ['Edward', 'John (adopted)'])
            self.assertEqual(
                reconcile_bracket_entries(kids, ['Edward (adopted)', 'John']),
                ['Edward (adopted)', 'John'])
        # A label the claims no longer support is still corrected.
        self.assertEqual(
            reconcile_bracket_entries([('Ruth', None, ['Ruth Doe'])], ['Ruth (adopted)']),
            ['Ruth'])

    def test_a_bare_sister_left_by_the_old_rule_is_refreshed(self) -> None:
        # The old first-word rule wrote [Sister + Sister] for two nuns - the
        # same names-nobody shape as [Mrs. + Mrs.] - so it is never kept.
        kids = [('Mary', None, ['Sister Mary Agnes Hartley']),
                ('Anne', None, ['Sister Anne Hartley'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Sister', 'Sister']),
                         ['Anne', 'Mary'])
        kids = [('Sister Mary', None, ['Sister Mary']),
                ('Sister Anne', None, ['Sister Anne'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Sister', 'Sister']),
                         ['Sister Anne', 'Sister Mary'])


class PublicNameVariantsTests(unittest.TestCase):
    """Lint reads the same names views reads from person_variants."""

    def test_strings_and_unrestricted_mappings_pass(self) -> None:
        self.assertEqual(
            public_name_variants(['Henry C. Hartley', {'value': 'H. C. Hartley'},
                                  {'value': 'Hal H.', 'restricted': False}]),
            ['Henry C. Hartley', 'H. C. Hartley', 'Hal H.'])

    def test_restricted_variants_are_withheld(self) -> None:
        for marker in (True, 'true', 'deadname', 'by-request'):
            self.assertEqual(
                public_name_variants([{'value': 'Old Name', 'restricted': marker}]), [],
                repr(marker))

    def test_absent_or_malformed_is_empty(self) -> None:
        self.assertEqual(public_name_variants(None), [])
        self.assertEqual(public_name_variants('Henry'), [])
        self.assertEqual(public_name_variants([{'value': ''}, None]), [])


def _ptext(pid: str, name: str, sex: str = 'U', tier: str = 'stub',
           extra: str = '') -> str:
    return (f'---\nid: {pid}\nname: {name}\n{extra}sex: {sex}\nliving: false\n'
            f'tier: {tier}\n---\n\n# {name}\n\n## Biography\n\nx\n')


def _rel_claim(cid: str, child: str, parents: list[str]) -> str:
    plist = ', '.join(parents)
    persons = ', '.join([child] + parents)
    return (
        f'- value: "{child} child of {plist}"\n'
        f'  id: {cid}\n  type: relationship\n'
        f'  persons: [{persons}]\n  roles:\n'
        f'    child: {child}\n    parent: [{plist}]\n'
        f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
        f'  information: primary\n  evidence: direct\n  notes: x.\n'
    )


def _build(folder: str, harold_variants: str = "name_variants: [Henry C. Hartley]\n") -> Path:
    root = Path(tempfile.mkdtemp())
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources' / 'notes').mkdir(parents=True)
    (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
    claims = (_rel_claim('C-4111111111', LOUISE, [PA, MA])
              + _rel_claim('C-4222222222', HAROLD, [PA, MA])
              + _rel_claim('C-4333333333', ETHEL, [PA, MA])
              + _rel_claim('C-4444444444', WALTER_DOE, [PA, MA]))
    files = {
        f'{folder}/hartley__thomas_{PA}.md': _ptext(PA, 'Thomas Hartley', 'M', 'curated'),
        f'{folder}/cole__margaret_{MA}.md': _ptext(MA, 'Margaret Cole', 'F', 'curated'),
        f'people/stubs/hartley__ada_louise_{LOUISE}.md':
            _ptext(LOUISE, 'Ada Louise Hartley', 'F'),
        f'people/stubs/hartley__harold_{HAROLD}.md':
            _ptext(HAROLD, 'Harold Cole Hartley', 'M', extra=harold_variants),
        f'people/stubs/hartley__ethel_{ETHEL}.md': _ptext(ETHEL, 'Ethel Hartley', 'F'),
        f'people/stubs/doe__mrs_walter_{WALTER_DOE}.md':
            _ptext(WALTER_DOE, 'Mrs. Walter Doe', 'F'),
        f'sources/notes/{SID.lower()}.md': (
            f'---\nid: {SID}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claims}```\n'),
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding='utf-8')
    return root


def _views_w103(root: Path) -> list[dict]:
    index_mod.build_index(root, load_fha_yaml(root))
    conn = sqlite3.connect(str(root / '.cache' / 'index.sqlite'))
    conn.row_factory = sqlite3.Row
    try:
        return views._check_w103_brackets(conn, root)
    finally:
        conn.close()


def _lint_w103(root: Path) -> list:
    findings, _ = lint._run_lint_core(root, load_fha_yaml(root))
    return [f for f in findings if f.code == 'W103']


BASE = 'people/040 Thomas Hartley + Margaret Cole'


class BothBackendsKeepTheHumanNameTests(unittest.TestCase):
    """The live-archive case, end to end, in lint and views alike."""

    def test_hand_written_names_that_still_fit_are_not_stale(self) -> None:
        root = _build(f'{BASE} [Ethel + Louise + Henry + Mrs. Doe]')
        self.assertEqual(_views_w103(root), [])
        self.assertEqual(_lint_w103(root), [])

    def test_unfitting_tokens_are_refreshed_with_title_aware_defaults(self) -> None:
        root = _build(f'{BASE} [Ethel + Fanny + Mrs.]')
        issues = _views_w103(root)
        self.assertEqual(len(issues), 1)
        # "Fanny" fits no child and the bare "Mrs." names no one, so Ada,
        # Harold and Mrs. Walter Doe all take their defaults.
        self.assertEqual(issues[0]['new_name'],
                         '040 Thomas Hartley + Margaret Cole [Ada + Ethel + Harold + Mrs. Doe]')
        lint_w = _lint_w103(root)
        self.assertEqual(len(lint_w), 1)
        self.assertIn('-> [Ada + Ethel + Harold + Mrs. Doe]', lint_w[0].message)

    def test_an_empty_bracket_gets_title_aware_defaults(self) -> None:
        root = _build(BASE)
        issues = _views_w103(root)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['new_name'],
                         f'{BASE[7:]} [Ada + Ethel + Harold + Mrs. Doe]')
        self.assertIn('-> [Ada + Ethel + Harold + Mrs. Doe]', _lint_w103(root)[0].message)

    def test_a_restricted_variant_is_not_kept_in_a_folder_name(self) -> None:
        # A private former name must not survive in a folder name, in either
        # backend: views never sees it (person_variants is public-only) and
        # lint filters it the same way.
        restricted = "name_variants:\n  - {value: Henry C. Hartley, restricted: true}\n"
        root = _build(f'{BASE} [Ethel + Louise + Henry + Mrs. Doe]',
                      harold_variants=restricted)
        issues = _views_w103(root)
        self.assertEqual(len(issues), 1)
        self.assertIn('[Ethel + Harold + Louise + Mrs. Doe]', issues[0]['new_name'])
        lint_w = _lint_w103(root)
        self.assertEqual(len(lint_w), 1)
        self.assertIn('-> [Ethel + Harold + Louise + Mrs. Doe]', lint_w[0].message)

    def test_fix_renames_to_the_reconciled_list(self) -> None:
        root = _build(f'{BASE} [Ethel + Fanny + Henry + Mrs. Doe]')
        index_mod.build_index(root, load_fha_yaml(root))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            views.run_brackets(root, fix=True, yes=True)
        self.assertTrue((root / 'people' /
                         '040 Thomas Hartley + Margaret Cole [Ada + Ethel + Henry + Mrs. Doe]').is_dir())


def _build_kids(folder: str, kids: list[tuple[str, str]]) -> Path:
    """A couple folder whose children are exactly `kids` (pid, name)."""
    root = Path(tempfile.mkdtemp())
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources' / 'notes').mkdir(parents=True)
    (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
    claims = ''.join(_rel_claim(f'C-4{n:09d}', pid, [PA, MA])
                     for n, (pid, _name) in enumerate(kids, 1))
    files = {
        f'{folder}/hartley__thomas_{PA}.md': _ptext(PA, 'Thomas Hartley', 'M', 'curated'),
        f'{folder}/cole__margaret_{MA}.md': _ptext(MA, 'Margaret Cole', 'F', 'curated'),
        f'sources/notes/{SID.lower()}.md': (
            f'---\nid: {SID}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claims}```\n'),
    }
    for pid, name in kids:
        files[f'people/stubs/hartley__{pid.lower()}.md'] = _ptext(pid, name)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    return root


class SameNamedChildrenTests(unittest.TestCase):
    """Two children with one first name: lint and views agree, whatever the ids."""

    def test_a_reused_name_is_not_stale_whichever_child_has_the_lower_id(self) -> None:
        # Before, the result hung on which child's id sorted first, and lint
        # (registry order) and views (SQL order) could disagree: lint silent
        # while `views brackets --fix` renamed the folder to [John + John].
        for plain_id, henry_id in (('P-4111111111', 'P-4999999999'),
                                   ('P-4999999999', 'P-4111111111')):
            with self.subTest(plain_id=plain_id):
                root = _build_kids(f'{BASE} [John + John Henry]',
                                   [(plain_id, 'John Hartley'),
                                    (henry_id, 'John Henry Hartley')])
                self.assertEqual(_views_w103(root), [])
                self.assertEqual(_lint_w103(root), [])

    def test_two_nuns_are_told_apart(self) -> None:
        root = _build_kids(BASE, [('P-4111111111', 'Sister Mary'),
                                  ('P-4999999999', 'Sister Anne')])
        issues = _views_w103(root)
        self.assertEqual(len(issues), 1)
        self.assertTrue(issues[0]['new_name'].endswith('[Sister Anne + Sister Mary]'))
        self.assertIn('-> [Sister Anne + Sister Mary]', _lint_w103(root)[0].message)
        root = _build_kids(f'{BASE} [Sister + Sister]',
                           [('P-4111111111', 'Sister Mary Agnes Hartley'),
                            ('P-4999999999', 'Sister Anne Hartley')])
        self.assertTrue(_views_w103(root)[0]['new_name'].endswith('[Anne + Mary]'))
        self.assertIn('-> [Anne + Mary]', _lint_w103(root)[0].message)


if __name__ == '__main__':
    unittest.main()
