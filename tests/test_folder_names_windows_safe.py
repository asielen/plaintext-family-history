"""
test_folder_names_windows_safe.py - a person's name made safe before it becomes
folder text (SPEC §12.2 couple folders; TOOLING §7 promote / W103).

A recorded name keeps what the source says, quotes and all:
`Margaret "Peggy" Cole` is a good `name:`. Windows refuses `\\ / : * ? " < > |`
in a path, so every tool path that turned a name into a folder name - the batch
promote's new couple folder, single-person promote, the W103 `+ spouse`
add-on, the default bracket token, and `fha person rename`'s folder edit -
could only fail on that name. `_lib.folder_name_text` is the one rule they now
share. Fixtures only.
"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod
import person
import views
from _lib import (
    bracket_given_name,
    folder_name_text,
    load_fha_yaml,
    reconcile_bracket_entries,
    spouse_extended_base,
)

KID = 'P-5aaaaaaaaa'
PA = 'P-5bbbbbbbbb'
MA = 'P-5ccccccccc'
SID = 'S-5aaaaaaaaa'
PEGGY = 'Margaret "Peggy" Cole'


class FolderNameTextTests(unittest.TestCase):

    def test_double_quotes_become_single_quotes(self) -> None:
        self.assertEqual(folder_name_text(PEGGY), "Margaret 'Peggy' Cole")

    def test_slashes_become_hyphens(self) -> None:
        self.assertEqual(folder_name_text('Ann/Anna Doe'), 'Ann-Anna Doe')
        self.assertEqual(folder_name_text('Ann\\Anna Doe'), 'Ann-Anna Doe')

    def test_other_reserved_characters_are_dropped(self) -> None:
        self.assertEqual(folder_name_text('Doe: Jane? <Jr> *|'), 'Doe Jane Jr')

    def test_control_characters_are_dropped(self) -> None:
        self.assertEqual(folder_name_text('Jane\tDoe\x07'), 'Jane Doe')

    def test_trailing_dots_and_spaces_are_trimmed(self) -> None:
        self.assertEqual(folder_name_text('Thomas Hartley Jr.'), 'Thomas Hartley Jr')
        self.assertEqual(folder_name_text('Thomas Hartley . '), 'Thomas Hartley')

    def test_a_plain_name_is_unchanged(self) -> None:
        for name in ('Thomas Hartley', "Margaret 'Peggy' Cole", 'Jean-Paul Doe',
                     'Mrs. Walter Doe', 'Ethel (adopted)'):
            self.assertEqual(folder_name_text(name), name)


class SharedRuleSitesTests(unittest.TestCase):
    """The name-to-folder helpers that are pure functions."""

    NAMES = {'p-1': 'Thomas Hartley', 'p-2': PEGGY}

    def test_spouse_add_on_writes_the_safe_form(self) -> None:
        base, other = spouse_extended_base('040 Thomas Hartley', ['p-1', 'p-2'], self.NAMES)
        self.assertEqual(base, "040 Thomas Hartley + Margaret 'Peggy' Cole")
        self.assertEqual(other, PEGGY)

    def test_a_base_already_in_the_safe_form_still_matches_its_partner(self) -> None:
        base, other = spouse_extended_base(
            "040 Margaret 'Peggy' Cole", ['p-1', 'p-2'], self.NAMES)
        self.assertEqual(base, "040 Margaret 'Peggy' Cole + Thomas Hartley")
        self.assertEqual(other, 'Thomas Hartley')

    def test_default_bracket_token_is_folder_safe(self) -> None:
        self.assertEqual(bracket_given_name('"Peggy" Cole'), "'Peggy'")

    def test_rename_finds_the_safe_form_and_writes_it(self) -> None:
        new, reason = person._renamed_couple_folder_name(
            "040 Thomas Hartley + Margaret 'Peggy' Cole [Anna]", PEGGY, 'Margaret "Meg" Cole')
        self.assertIsNone(reason)
        self.assertEqual(new, "040 Thomas Hartley + Margaret 'Meg' Cole [Anna]")


class NothingAFolderCanHoldTests(unittest.TestCase):
    """A name made only of characters Windows refuses ("?", "???") has an
    empty safe form. Each site treats that like a missing name instead of
    writing an empty slot into a folder name."""

    def test_a_child_with_no_usable_first_name_is_left_out_of_the_brackets(self) -> None:
        # Before: an empty token the next read drops again, so every
        # `--fix` "renamed" the folder to [ + Kid] and lint never cleared.
        self.assertEqual(bracket_given_name('? Hartley'), '')
        kids = [('', None, ['? Hartley']), ('Kid', None, ['Kid Hartley'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Kid']), ['Kid'])
        self.assertEqual(reconcile_bracket_entries(kids, []), ['Kid'])
        # A token that still fits the unnamed child's variants is kept.
        kids = [('', None, ['? Hartley', 'Baby Hartley']), ('Kid', None, ['Kid Hartley'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['Baby', 'Kid']), ['Baby', 'Kid'])
        # A bare label token never becomes " (adopted)".
        kids = [('', 'adopted', ['? Hartley'])]
        self.assertEqual(reconcile_bracket_entries(kids, ['(adopted)']), [])

    def test_no_empty_partner_half_is_added(self) -> None:
        # Before: "040 Thomas Hartley + " - and the add-only rule never
        # revisits a base with a "+", so the real name could never be added.
        base, other = spouse_extended_base(
            '040 Thomas Hartley', ['p-1', 'p-3'], {'p-1': 'Thomas Hartley', 'p-3': '???'})
        self.assertEqual((base, other), ('040 Thomas Hartley', None))

    def test_a_new_couple_folder_never_ends_in_an_empty_name(self) -> None:
        root = Path('/archive')
        members = [{'pid': PA, 'pos': 2, 'name': 'Thomas Hartley'},
                   {'pid': MA, 'pos': 3, 'name': '???'}]
        self.assertEqual(views._new_couple_folder(root, 2, members).name, '002 Thomas Hartley')
        members = [{'pid': PA, 'pos': 2, 'name': '?'}]
        self.assertEqual(views._new_couple_folder(root, 2, members).name, f'002 {PA}')

    def test_a_rename_to_nothing_a_folder_can_hold_leaves_the_folder_alone(self) -> None:
        new, reason = person._renamed_couple_folder_name(
            '040 Thomas Hartley + Margaret Cole', 'Margaret Cole', '???')
        self.assertIsNone(new)
        self.assertIn('keeps its name', reason)


def _ptext(pid: str, name: str, sex: str) -> str:
    name_yaml = name.replace('"', '\\"')
    return (f'---\nid: {pid}\nname: "{name_yaml}"\nsex: {sex}\nliving: false\n'
            f'tier: stub\n---\n\n# {name}\n\n## Biography\n\nx\n')


def _build(ma_name: str = PEGGY) -> Path:
    root = Path(tempfile.mkdtemp())
    (root / 'people' / 'stubs').mkdir(parents=True)
    (root / 'sources' / 'notes').mkdir(parents=True)
    (root / 'fha.yaml').write_text(f'root_person: {KID}\nroots:\n  documents: documents\n',
                                   encoding='utf-8')
    claim = (
        f'- value: "Kid child of Thomas and Peggy"\n'
        f'  id: C-5111111111\n  type: relationship\n'
        f'  persons: [{KID}, {PA}, {MA}]\n  roles:\n'
        f'    child: {KID}\n    parent: [{PA}, {MA}]\n'
        f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
        f'  information: primary\n  evidence: direct\n  notes: x.\n')
    files = {
        f'people/stubs/hartley__kid_{KID}.md': _ptext(KID, 'Kid Hartley', 'F'),
        f'people/stubs/hartley__thomas_{PA}.md': _ptext(PA, 'Thomas Hartley', 'M'),
        f'people/stubs/cole__margaret_peggy_{MA}.md': _ptext(MA, ma_name, 'F'),
        f'sources/notes/{SID.lower()}.md': (
            f'---\nid: {SID}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claim}```\n'),
    }
    for rel, text in files.items():
        p = root / rel
        p.write_text(text, encoding='utf-8')
    index_mod.build_index(root, load_fha_yaml(root))
    return root


def _quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


class PromoteCreatesASafeFolderTests(unittest.TestCase):
    """End to end: the folder a promotion invents for a quoted nickname."""

    def test_batch_promote_names_the_couple_folder_safely(self) -> None:
        root = _build()
        _quiet(views.run_brackets, root, fix_promote=True, yes=True)
        folder = root / 'people' / "002 Thomas Hartley + Margaret 'Peggy' Cole"
        self.assertTrue(folder.is_dir(), sorted(p.name for p in (root / 'people').iterdir()))
        self.assertTrue((folder / f'cole__margaret_peggy_{MA}.md').is_file())

    def test_single_promote_names_the_folder_safely(self) -> None:
        root = _build()
        res = _quiet(person.run_promote, root, MA)
        self.assertEqual(res.data.get('folder'), "002 Margaret 'Peggy' Cole")
        self.assertTrue((root / 'people' / "002 Margaret 'Peggy' Cole"
                         / f'cole__margaret_peggy_{MA}.md').is_file())

    def test_single_promote_of_a_question_mark_name_falls_back_to_the_id(self) -> None:
        # Before: a folder named "002 " (Windows refuses the trailing space,
        # so the promote failed with "cannot find the path specified").
        root = _build(ma_name='?')
        res = _quiet(person.run_promote, root, MA)
        self.assertEqual(res.data.get('folder'), f'002 {MA}')
        self.assertTrue((root / 'people' / f'002 {MA}' / f'cole__margaret_peggy_{MA}.md').is_file())


if __name__ == '__main__':
    unittest.main()
