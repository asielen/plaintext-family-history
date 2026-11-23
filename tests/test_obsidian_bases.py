"""
test_obsidian_bases.py: an owner who works in Obsidian (`links: obsidian`)
gets three ready-made tables, everyone, sources and places, in one folder,
`notes/obsidian/`. An owner who does not use Obsidian gets nothing, and the
tables never sit inside people/, sources/ or places/ (SPEC §3: the working
interface owns no data, so a table is never the only copy of anything).

Covered here:
  * each shipped .base file parses, names only properties SPEC documents,
    and uses only table views with unique names;
  * `fha views places` and `fha views refresh` write them with
    `links: obsidian`, once, and never overwrite or remove an owner's edit;
  * in `id` mode (or no setting) nothing is written;
  * lint, the index and `fha views clean` leave them alone.

Temp copies throughout; the example archive itself is never written.
"""

import contextlib
import io
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod  # noqa: E402
import lint  # noqa: E402
import views  # noqa: E402
from _lib import load_fha_yaml  # noqa: E402

TEMPLATES = ROOT / 'tools' / 'templates' / 'obsidian'
NAMES = ('People.base', 'Sources.base', 'Places.base')
FOLDER = 'notes/obsidian'

# Keys SPEC §9 (person), §14 (source) and §15 (place note) document.
SPEC_KEYS = {
    'id', 'name', 'living', 'tier', 'birth', 'death', 'tags',
    'title', 'source_type', 'source_date', 'repository', 'source_class',
    'location',
}


def _quiet(fn, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def _copy_example(dest: Path) -> Path:
    shutil.copytree(ROOT / 'example-archive', dest,
                    ignore=shutil.ignore_patterns('generated', '.cache'))
    return dest


def _set_mode(root: Path, mode) -> None:
    path = root / 'fha.yaml'
    text = re.sub(r'(?m)^links:.*\n', '', path.read_text(encoding='utf-8'))
    if mode is not None:
        text += f'\nlinks: {mode}\n'
    path.write_text(text, encoding='utf-8')


def _index(root: Path) -> None:
    _quiet(index_mod.build_index, root, load_fha_yaml(root))


def _bases(root: Path) -> list:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob('*.base'))


class ShippedFileTests(unittest.TestCase):

    def test_the_three_files_are_shipped(self):
        self.assertEqual(sorted(p.name for p in TEMPLATES.glob('*.base')), sorted(NAMES))

    def test_each_parses_with_unique_table_views_on_documented_keys(self):
        for name in NAMES:
            with self.subTest(name):
                doc = yaml.safe_load((TEMPLATES / name).read_text(encoding='utf-8'))
                self.assertEqual(set(doc), {'filters', 'properties', 'views'})
                views_ = doc['views']
                self.assertTrue(views_)
                titles = [v['name'] for v in views_]
                self.assertEqual(len(titles), len(set(titles)))
                for v in views_:
                    self.assertEqual(v['type'], 'table')
                used = set(doc['properties'])
                for v in views_:
                    used |= set(v.get('order', []))
                    if 'groupBy' in v:
                        used.add(v['groupBy']['property'])
                # Bare names in filter expressions are note properties too: a
                # typo there (`tierr == ...`) leaves a table silently empty.
                filters = [doc['filters']] + [v['filters'] for v in views_ if 'filters' in v]
                for f in filters:
                    for expr in f['and']:
                        stripped = re.sub(r'"[^"]*"', '', expr)
                        for m in re.finditer(r'(?<![\w.])([A-Za-z_]\w*)(?=\s*(?:[.=!<>]|$))', stripped):
                            word = m.group(1)
                            if word in ('file', 'note', 'true', 'false', 'null'):
                                continue
                            used.add('note.' + word)
                for key in used:
                    if key.startswith('note.'):
                        self.assertIn(key[5:], SPEC_KEYS, key)
                    else:
                        self.assertTrue(key.startswith('file.'), key)

    def _view_filters(self, name, view):
        doc = yaml.safe_load((TEMPLATES / name).read_text(encoding='utf-8'))
        if view is None:
            return doc['filters']['and']
        return next(v for v in doc['views'] if v['name'] == view)['filters']['and']

    def test_stubs_are_found_by_tier_wherever_they_are_filed(self):
        # SPEC §12.2 lets a stub child sit in its parents' couple folder;
        # a folder filter would leave it out of the Stubs table.
        self.assertEqual(self._view_filters('People.base', 'Stubs'), ['tier == "stub"'])

    def test_no_view_claims_to_be_the_direct_line(self):
        # A curated aunt filed in her parents' couple folder (SPEC §12.2)
        # passes any tier or folder filter, so a table cannot know who is an
        # ancestor; the curated view says what it really selects.
        doc = yaml.safe_load((TEMPLATES / 'People.base').read_text(encoding='utf-8'))
        names = [v['name'] for v in doc['views']]
        self.assertFalse([n for n in names if 'direct' in n.lower()], names)
        self.assertEqual(self._view_filters('People.base', 'Curated'), ['tier == "curated"'])

    def test_id_prefixes_are_matched_without_regard_to_case(self):
        # SPEC §10: an older tool's `P-…`/`S-…` id is still a record.
        for name, prefix in (('People.base', 'p-'), ('Sources.base', 's-')):
            with self.subTest(name):
                self.assertIn(f'id.lower().startsWith("{prefix}")',
                              self._view_filters(name, None))
                text = (TEMPLATES / name).read_text(encoding='utf-8')
                self.assertNotIn('id.startsWith(', text)

    def test_a_file_needs_no_plugin(self):
        # The map view needs the Maps plugin; the shipped files must open
        # in plain Obsidian.
        for name in NAMES:
            text = (TEMPLATES / name).read_text(encoding='utf-8')
            self.assertNotIn('type: map', text)


class WriteTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='fha-bases-'))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _archive(self, mode, name='a'):
        root = _copy_example(self.tmp / name)
        _set_mode(root, mode)
        _index(root)
        return root

    def test_obsidian_mode_places_run_writes_the_three_files_in_one_folder(self):
        root = self._archive('obsidian')
        self.assertEqual(_bases(root), [])
        res = _quiet(views.run_places, root)
        self.assertEqual(res.exit_code, 0)
        self.assertEqual(_bases(root), sorted(f'{FOLDER}/{n}' for n in NAMES))
        for n in NAMES:
            self.assertEqual((root / FOLDER / n).read_bytes(), (TEMPLATES / n).read_bytes())

    def test_refresh_writes_them_too(self):
        root = self._archive('obsidian')
        _quiet(views.run_refresh, root)
        self.assertEqual(_bases(root), sorted(f'{FOLDER}/{n}' for n in NAMES))

    def test_an_owners_edit_is_never_overwritten_or_removed(self):
        root = self._archive('obsidian')
        _quiet(views.run_places, root)
        mine = root / FOLDER / 'People.base'
        mine.write_text('# mine\n' + mine.read_text(encoding='utf-8'), encoding='utf-8')
        edited = mine.read_bytes()
        _quiet(views.run_places, root)
        _quiet(views.run_refresh, root)
        _quiet(views.run_clean, root)
        self.assertEqual(mine.read_bytes(), edited)

    def test_a_file_the_owner_deleted_stays_deleted(self):
        root = self._archive('obsidian')
        _quiet(views.run_places, root)
        (root / FOLDER / 'Places.base').unlink()
        _quiet(views.run_places, root)
        self.assertEqual(len(_bases(root)), 2)

    def test_dry_run_writes_nothing(self):
        root = self._archive('obsidian')
        _quiet(views.run_places, root, dry_run=True)
        self.assertEqual(_bases(root), [])

    def test_id_mode_and_no_setting_write_nothing(self):
        for mode in ('id', None):
            with self.subTest(mode=mode):
                root = self._archive(mode, name=f'm-{mode}')
                _quiet(views.run_places, root)
                _quiet(views.run_refresh, root)
                self.assertEqual(_bases(root), [])

    def test_the_places_table_finds_coordinates_in_generated_notes(self):
        # "With coordinates" reads note.location: a place note for a place
        # with coords must carry it, or the table is always empty.
        root = self._archive('obsidian')
        _quiet(views.run_places, root)
        with_coords = 0
        for note in (root / 'places').glob('*_l-*.md'):
            fm = yaml.safe_load(note.read_text(encoding='utf-8').split('---', 2)[1])
            if fm.get('location'):
                with_coords += 1
                self.assertEqual(len(fm['location']), 2, note.name)
        self.assertGreater(with_coords, 0)

    def test_nothing_lands_in_a_record_tree(self):
        root = self._archive('obsidian')
        _quiet(views.run_refresh, root)
        for tree in ('people', 'sources', 'places'):
            self.assertEqual(list((root / tree).rglob('*.base')), [])

    def test_lint_is_unchanged_by_the_files(self):
        root = self._archive('obsidian')
        _quiet(views.run_places, root)

        def codes():
            findings, _r = _quiet(lint._run_lint_core, root, load_fha_yaml(root))
            return sorted((f.code, Path(f.path).name if f.path else '') for f in findings)
        with_files = codes()
        shutil.rmtree(root / FOLDER)
        self.assertEqual(with_files, codes())


if __name__ == '__main__':
    unittest.main()
