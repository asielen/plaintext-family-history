"""
test_obsidian_renames.py: with `links: obsidian`, a verb that renames a
record file points the links to it at the new file in the same run (SPEC §18).

An owner who works in Obsidian corrects a name with `fha person rename`, and
every `[[hartley__thomas_edward_p-…|Tom]]` in his notes and records becomes
`[[hartley__thomas_e_p-…|Tom]]`, so Obsidian keeps following them. Covered
for each renaming verb (person rename, confirm merge, source merge, lint
--fix-ids):

  * afterwards lint W161 finds no link Obsidian cannot follow;
  * `--dry-run` writes nothing and counts what the real run writes;
  * in `id` mode the verb writes the same bytes and says the same things
    as with the rename-link step taken out;
  * a write that fails part-way leaves every file as it was;
  * the website, a packet and WikiTree withhold the same things in both
    link modes after a living person's rename (a file name carries a name).

Temp copies throughout; the example archive itself is never written.
"""

import contextlib
import importlib.util
import io
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib  # noqa: E402
import confirm  # noqa: E402
import lint  # noqa: E402
import normalize_links  # noqa: E402
import person  # noqa: E402
import source  # noqa: E402
from _lib import load_fha_yaml  # noqa: E402

# The fixtures and export helpers of the link-mode suite, loaded by path so
# its test classes are not collected a second time here.
_spec = importlib.util.spec_from_file_location(
    'obsidian_link_mode_helpers', ROOT / 'tests' / 'test_obsidian_link_mode.py')
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)
_copy_example = _helpers._copy_example
_set_mode = _helpers._set_mode
_codes = _helpers._codes
_tree = _helpers._tree
_quiet = _helpers._quiet
_private_example = _helpers._private_example
_exports = _helpers._exports

TOM = _helpers.TOM
TOM_STEM = _helpers.TOM_STEM
TOM_NEW_STEM = 'hartley__thomas_e_p-de957bcda1'
CALVIN = _helpers.CALVIN
CALVIN_STEM = _helpers.CALVIN_STEM
ALEX = _helpers.ALEX
KEEPER = 's-fa1234567b'
KEEPER_STEM = 'hartley-family-notes_s-fa1234567b'
RETIREE = 's-fb2345678c'
RETIREE_STEM = 'calvin-hartley-marriage-1898_s-fb2345678c'
RETIREE_FILE = f'sources/vital-record/{RETIREE_STEM}.md'
NOTE = 'notes/obsidian-renames.md'


def _obsidian_example(dest: Path) -> Path:
    """The example archive in Obsidian link mode, its links already in the
    file-name form, plus a note that links Tom, Calvin and the 1898
    marriage record (and one of its claims) by file name."""
    _copy_example(dest)
    _set_mode(dest, 'obsidian')
    _quiet(normalize_links.run_normalize_links, dest, load_fha_yaml(dest), write=True)
    cid = re.search(r'id: (c-\w+)', (dest / RETIREE_FILE).read_text(encoding='utf-8')).group(1)
    (dest / NOTE).write_text(
        '# Loose ends\n\n'
        f'Tom [[{TOM_STEM}|Tom]] wrote to [[{CALVIN_STEM}|his son]].\n'
        f'The 1898 record [[{RETIREE_STEM}|the 1898 record]] and its claim '
        f'[[{RETIREE_STEM}#Claims|{cid}]].\n\n'
        '| who | link |\n| --- | --- |\n'
        f'| Tom | [[{TOM_STEM}\\|Tom]] |\n', encoding='utf-8', newline='')
    return dest


def _messages(result) -> str:
    return '\n'.join(m.text for m in result.messages)


def _count_line(text: str) -> str:
    """The `N links in M files` part of the rename-link line."""
    m = re.search(r'(\d+ links? in \d+ files?)', text)
    return m.group(1) if m else ''


def _linked_text(root: Path) -> str:
    """Every record file the link rewrite reads, joined."""
    return '\n'.join(p.read_text(encoding='utf-8') for p in _lib.link_record_files(root))


def _no_rename_links(*_args, **_kwargs):
    return None


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='fha-381p4-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def obsidian(self, name='a') -> Path:
        return _obsidian_example(self.tmp / name)

    def id_twins(self) -> tuple[Path, Path]:
        """Two id-mode copies: the same links as the Obsidian fixture's note,
        written in the ID form, so the verbs have links to the renamed
        records to (not) touch."""
        roots = []
        for name in ('id-a', 'id-b'):
            root = _copy_example(self.tmp / name)
            _set_mode(root, 'id')
            (root / NOTE).write_text(
                f'# Loose ends\n\nTom [[{TOM}|Tom]], [[{CALVIN}|his son]], '
                f'[[{RETIREE}|the 1898 record]], [[{TOM_STEM}|Tom by file]].\n',
                encoding='utf-8', newline='')
            roots.append(root)
        return roots[0], roots[1]

    def assert_w161_clean(self, root: Path):
        self.assertEqual([(Path(f.path).name, f.message) for f in _codes(root, 'W161')], [])


# ── fha person rename ────────────────────────────────────────────────────────

class PersonRenameTests(_Base):

    def test_links_follow_the_new_file_and_lint_is_clean(self):
        root = self.obsidian()
        self.assert_w161_clean(root)
        result = person.run_rename(root, TOM, 'Thomas E. Hartley')
        self.assertEqual(result.data['status'], 'ok', _messages(result))
        note = (root / NOTE).read_text(encoding='utf-8')
        self.assertIn(f'[[{TOM_NEW_STEM}|Tom]]', note)
        self.assertIn(f'[[{TOM_NEW_STEM}\\|Tom]]', note, 'a table keeps its escaped bar')
        self.assertNotIn(f'[[{TOM_STEM}', _linked_text(root))
        self.assertIn('Pointed', _messages(result))
        self.assert_w161_clean(root)

    def test_dry_run_writes_nothing_and_counts_what_the_write_does(self):
        root = self.obsidian()
        before = _tree(root)
        preview = person.run_rename(root, TOM, 'Thomas E. Hartley', dry_run=True)
        self.assertEqual(_tree(root), before)
        wrote = person.run_rename(root, TOM, 'Thomas E. Hartley')
        self.assertTrue(_count_line(_messages(preview)))
        self.assertEqual(_count_line(_messages(preview)), _count_line(_messages(wrote)))

    def test_id_mode_is_unchanged(self):
        a, b = self.id_twins()
        ra = person.run_rename(a, TOM, 'Thomas E. Hartley')
        with mock.patch.object(person, 'rename_link_writer', _no_rename_links):
            rb = person.run_rename(b, TOM, 'Thomas E. Hartley')
        self.assertEqual(_tree(a), _tree(b))
        self.assertEqual(_messages(ra).replace(str(a), ''), _messages(rb).replace(str(b), ''))
        self.assertNotIn('Obsidian', _messages(ra))

    def test_a_failed_link_write_puts_every_file_back(self):
        root = self.obsidian()
        before = _tree(root)
        real = _lib.write_text_exact_atomic
        calls = {'n': 0}

        def flaky(path, text):
            # The rename-link writer's second file fails; its restores pass.
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError('disk full')
            return real(path, text)

        with mock.patch.object(_lib, 'write_text_exact_atomic', flaky):
            result = person.run_rename(root, TOM, 'Thomas E. Hartley')
        self.assertEqual(result.data['status'], 'refused')
        self.assertGreater(calls['n'], 2)
        self.assertEqual(_tree(root), before)


# ── fha confirm merge ────────────────────────────────────────────────────────

class ConfirmMergeTests(_Base):

    def _merge(self, root, dry_run=False):
        return confirm.run_confirm_merge(root, person_merged=CALVIN, into=TOM,
                                         reason='one person', dry_run=dry_run)

    def test_links_to_the_merged_person_go_to_the_survivor(self):
        root = self.obsidian()
        result = self._merge(root)
        self.assertIn(result.data['status'], ('ok',), _messages(result))
        note = (root / NOTE).read_text(encoding='utf-8')
        self.assertIn(f'[[{TOM_STEM}|his son]]', note)
        self.assertNotIn(CALVIN_STEM, note)
        self.assertIn("at the survivor's file", _messages(result))
        self.assert_w161_clean(root)

    def test_dry_run_writes_nothing_and_counts_what_the_write_does(self):
        root = self.obsidian()
        before = _tree(root)
        preview = self._merge(root, dry_run=True)
        self.assertEqual(_tree(root), before)
        self.assertIn(f'+Tom [[{TOM_STEM}|Tom]] wrote to [[{TOM_STEM}|his son]].',
                      _messages(preview), 'the preview diffs the note')
        wrote = self._merge(root)
        self.assertTrue(_count_line(_messages(preview)))
        self.assertEqual(_count_line(_messages(preview)), _count_line(_messages(wrote)))
        self.assertEqual(preview.data['prose_refs_remaining'], wrote.data['prose_refs_remaining'])

    def test_id_mode_is_unchanged(self):
        a, b = self.id_twins()
        ra = self._merge(a)
        with mock.patch.object(confirm, 'rename_link_writer', _no_rename_links):
            rb = self._merge(b)
        self.assertEqual(_tree(a), _tree(b))
        self.assertEqual(_messages(ra).replace(str(a), ''), _messages(rb).replace(str(b), ''))
        self.assertEqual(ra.data['prose_refs_remaining'], rb.data['prose_refs_remaining'])

    def test_a_failed_link_write_puts_every_file_back(self):
        root = self.obsidian()
        before = _tree(root)
        real = confirm.write_text_exact_atomic
        note = root / NOTE

        def flaky(path, text):
            if Path(path) == note and CALVIN_STEM not in text:
                raise OSError('disk full')
            return real(path, text)

        with mock.patch.object(confirm, 'write_text_exact_atomic', flaky):
            result = self._merge(root)
        self.assertEqual(result.data['status'], 'failed')
        self.assertEqual(_tree(root), before)


# ── fha source merge ─────────────────────────────────────────────────────────

class SourceMergeTests(_Base):

    def _merge(self, root, dry_run=False):
        return source.run_source_merge(root, load_fha_yaml(root), KEEPER, RETIREE,
                                       dry_run=dry_run)

    def test_links_to_the_removed_record_and_its_claims_go_to_the_keeper(self):
        root = self.obsidian()
        result = self._merge(root)
        self.assertEqual(result.data['status'], 'ok', _messages(result))
        note = (root / NOTE).read_text(encoding='utf-8')
        self.assertIn(f'[[{KEEPER_STEM}|the 1898 record]]', note)
        self.assertIn(f'[[{KEEPER_STEM}#Claims|c-', note)
        self.assertNotIn(f'[[{RETIREE_STEM}', _linked_text(root))
        self.assert_w161_clean(root)

    def test_dry_run_writes_nothing_and_counts_what_the_write_does(self):
        root = self.obsidian()
        before = _tree(root)
        preview = self._merge(root, dry_run=True)
        self.assertEqual(_tree(root), before)
        wrote = self._merge(root)
        self.assertTrue(_count_line(_messages(preview)))
        self.assertEqual(_count_line(_messages(preview)), _count_line(_messages(wrote)))

    def test_id_mode_is_unchanged(self):
        a, b = self.id_twins()
        ra = self._merge(a)
        with mock.patch.object(source, 'rename_link_writer', _no_rename_links):
            rb = self._merge(b)
        self.assertEqual(ra.data['status'], 'ok', _messages(ra))
        self.assertEqual({k: v for k, v in _tree(a).items() if not k.startswith('.fha')},
                         {k: v for k, v in _tree(b).items() if not k.startswith('.fha')})
        self.assertEqual(_messages(ra).replace(str(a), ''), _messages(rb).replace(str(b), ''))

    def test_a_failed_link_write_puts_every_file_back(self):
        root = self.obsidian()
        before = _tree(root)
        real = source.write_text_exact_atomic
        note = root / NOTE

        def flaky(path, text):
            if Path(path) == note and RETIREE_STEM not in text:
                raise OSError('disk full')
            return real(path, text)

        with mock.patch.object(source, 'write_text_exact_atomic', flaky):
            result = self._merge(root)
        self.assertEqual(result.data['status'], 'failed', _messages(result))
        after = _tree(root)
        self.assertEqual({k: v for k, v in after.items() if not k.startswith('.fha')},
                         {k: v for k, v in before.items() if not k.startswith('.fha')})


# ── fha lint --fix-ids ───────────────────────────────────────────────────────

class FixIdsTests(_Base):

    ALBUM = 'sources/other/grandmas-album.md'

    def _add_album(self, root: Path) -> None:
        (root / self.ALBUM).write_text(
            '---\ntitle: Grandma album\nsource_type: other\n---\n\n# Grandma album\n\n'
            '## Notes\n\nPhotos from the farm.\n', encoding='utf-8', newline='')
        with (root / NOTE).open('a', encoding='utf-8', newline='') as fh:
            fh.write('\nAlso [[grandmas-album]] and [[Grandmas-Album|her album]].\n')

    def _fix(self, root, dry_run=False):
        return _quiet(lint.run_lint, root, load_fha_yaml(root), fix_ids=True, dry_run=dry_run)

    def _album_stem(self, root: Path) -> str:
        return next((root / 'sources/other').glob('grandmas-album_s-*.md')).stem

    def test_a_link_by_the_old_file_name_follows_the_new_one(self):
        root = self.obsidian()
        self._add_album(root)
        result = self._fix(root)
        stem = self._album_stem(root)
        note = (root / NOTE).read_text(encoding='utf-8')
        self.assertIn(f'[[{stem}|grandmas-album]] and [[{stem}|her album]]', note)
        self.assertIn('pointed 2 links in 1 file', '\n'.join(result.data['progress']))
        self.assert_w161_clean(root)

    def test_dry_run_writes_nothing_and_counts_what_the_write_does(self):
        root = self.obsidian()
        self._add_album(root)
        before = _tree(root)
        preview = self._fix(root, dry_run=True)
        self.assertEqual(_tree(root), before)
        wrote = self._fix(root)
        said = '\n'.join(preview.data['progress'])
        self.assertIn('would point 2 links in 1 file', said)
        self.assertEqual(_count_line(said), _count_line('\n'.join(wrote.data['progress'])))

    def test_id_mode_is_unchanged(self):
        a, b = self.id_twins()
        for root in (a, b):
            self._add_album(root)
        with mock.patch.object(lint, 'mint_ids', side_effect=[['s-h7tsyf48ny']]):
            ra = self._fix(a)
        with mock.patch.object(lint, 'mint_ids', side_effect=[['s-h7tsyf48ny']]), \
                mock.patch.object(lint, 'rename_link_writer', _no_rename_links):
            rb = self._fix(b)
        self.assertEqual(_tree(a), _tree(b))
        self.assertIn('[[grandmas-album]]', (a / NOTE).read_text(encoding='utf-8'))
        self.assertEqual([t.replace(str(a), '') for t in ra.data['progress']],
                         [t.replace(str(b), '') for t in rb.data['progress']])

    def test_a_failed_link_write_puts_the_link_files_back_and_names_the_fix(self):
        root = self.obsidian()
        self._add_album(root)
        note_before = (root / NOTE).read_bytes()
        real = _lib.write_text_exact_atomic
        note = root / NOTE

        def flaky(path, text):
            if Path(path) == note:
                raise OSError('disk full')
            return real(path, text)

        with mock.patch.object(_lib, 'write_text_exact_atomic', flaky):
            result = self._fix(root)
        self.assertEqual((root / NOTE).read_bytes(), note_before)
        self.assertIn('fha normalize-links --obsidian --write', '\n'.join(result.data['progress']))


# ── Privacy after a rename ───────────────────────────────────────────────────

class PrivacyAfterRenameTests(unittest.TestCase):
    """A living person's new file name lands in the links to him in public
    records; every export reads those links back to the ID form, so it
    withholds the same things as an id-mode archive after the same rename."""

    NEW_NAME = 'Alexander James Bradford'

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='fha-381p4p-'))
        ids = _private_example(cls.tmp / 'ids')
        _quiet(person.run_rename, ids, ALEX, cls.NEW_NAME)
        cls.ids = _exports(ids, cls.tmp / 'out-ids')
        obs = _private_example(cls.tmp / 'obs')
        _set_mode(obs, 'obsidian')
        _quiet(normalize_links.run_normalize_links, obs, load_fha_yaml(obs), write=True)
        cls.renamed = _quiet(person.run_rename, obs, ALEX, cls.NEW_NAME)
        cls.obs_text = _linked_text(obs)
        cls.obs = _exports(obs, cls.tmp / 'out-obs')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_rename_rewrote_links_to_the_living_son(self):
        self.assertIn('Pointed', _messages(self.renamed))
        self.assertIn(f'[[bradford__alexander_james_{ALEX}|', self.obs_text)
        self.assertNotIn(f'bradford__alex_james_{ALEX}', self.obs_text)

    def test_the_website_is_byte_for_byte_the_same(self):
        self.assertEqual(sorted(self.obs['site']), sorted(self.ids['site']))
        differ = [k for k in self.ids['site'] if self.obs['site'][k] != self.ids['site'][k]]
        self.assertEqual(differ, [])

    def test_wikitree_is_the_same(self):
        self.assertEqual(self.obs['wikitree'], self.ids['wikitree'])

    def test_the_packet_leaves_out_the_same(self):
        self.assertEqual(self.obs['packet_exit'], self.ids['packet_exit'])
        self.assertEqual(sorted(self.obs['packet']), sorted(self.ids['packet']))
        for name, body in self.ids['packet'].items():
            other = self.obs['packet'][name]
            for secret in (self.NEW_NAME.encode(), b'Alex', b'bradford__alexander',
                           b'bradford__alex_james'):
                self.assertEqual(other.count(secret), body.count(secret), (name, secret))


if __name__ == '__main__':
    unittest.main()
