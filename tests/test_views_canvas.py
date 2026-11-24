"""
test_views_canvas.py: an Obsidian owner (`links: obsidian`) opens a family as
a board: the couple in the middle, their parents above, their children below,
each card the person's own note. `fha views canvas` writes one canvas per
direct-line couple folder into `notes/obsidian/canvas/`, beside the Bases and
outside people/, sources/ and places/ (SPEC §3: a canvas owns no data).

Covered here:
  * one canvas per couple folder, three rows, every card names a real file;
  * valid JSON Canvas 1.0 (keys, integer geometry, unique ids, edges that
    land on cards, sides from the allowed set);
  * a second run, and a run on a new day, rewrite nothing;
  * a canvas the owner made is never overwritten or removed (exit 1);
  * a canvas whose couple folder is gone is removed; `clean` removes owned
    canvases only; `--dry-run` writes nothing and prints the same lines;
  * the tables still arrive after a canvas run, and in `id` mode nothing is
    written at all.

Temp copies throughout; the example archive itself is never written.
"""

import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import index as index_mod  # noqa: E402
import views  # noqa: E402
from _lib import load_fha_yaml  # noqa: E402

FOLDER = 'notes/obsidian/canvas'
HARTLEY = '040 Thomas Edward Hartley'
SIDES = {'top', 'right', 'bottom', 'left'}


def _run(fn, *args, **kwargs):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        result = fn(*args, **kwargs)
    return result, out.getvalue(), err.getvalue()


def _set_mode(root: Path, mode) -> None:
    path = root / 'fha.yaml'
    text = re.sub(r'(?m)^links:.*\n', '', path.read_text(encoding='utf-8'))
    if mode is not None:
        text += f'\nlinks: {mode}\n'
    path.write_text(text, encoding='utf-8')


class CanvasTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / 'archive'
        shutil.copytree(ROOT / 'example-archive', self.root,
                        ignore=shutil.ignore_patterns('generated', '.cache'))
        _set_mode(self.root, 'obsidian')
        _run(index_mod.build_index, self.root, load_fha_yaml(self.root))
        self.dir = self.root / FOLDER

    def canvas(self, name=HARTLEY):
        return json.loads((self.dir / f'{name}.canvas').read_text(encoding='utf-8'))

    def test_one_canvas_per_couple_folder_in_three_rows(self):
        result, _, _ = _run(views.run_canvas, self.root)
        self.assertEqual(result.exit_code, 0)
        names = sorted(p.stem for p in self.dir.glob('*.canvas'))
        self.assertEqual(names, ['002 James Robert Bradford', HARTLEY])
        data = self.canvas()
        cards = [n for n in data['nodes'] if n['id'] != 'fha-generated']
        rows = sorted({n['y'] for n in cards})
        self.assertEqual(rows, [0, 360, 720])
        self.assertTrue(any(n['y'] == 360 for n in cards))
        for n in cards:
            if n['type'] == 'file':
                self.assertTrue((self.root / n['file']).is_file(), n['file'])
                self.assertNotIn('\\', n['file'])

    def test_the_file_is_valid_json_canvas(self):
        _run(views.run_canvas, self.root)
        data = self.canvas()
        self.assertEqual(list(data), ['nodes', 'edges'])
        self.assertEqual(data['nodes'][0]['id'], 'fha-generated')
        ids = [n['id'] for n in data['nodes']]
        self.assertEqual(len(ids), len(set(ids)))
        for n in data['nodes']:
            self.assertIn(n['type'], ('text', 'file'))
            for k in ('x', 'y', 'width', 'height'):
                self.assertIsInstance(n[k], int)
        self.assertTrue(data['edges'])
        edge_ids = [e['id'] for e in data['edges']]
        self.assertEqual(len(edge_ids), len(set(edge_ids)))
        self.assertEqual(edge_ids, sorted(edge_ids))
        for e in data['edges']:
            self.assertIn(e['fromNode'], ids)
            self.assertIn(e['toNode'], ids)
            self.assertIn(e['fromSide'], SIDES)
            self.assertIn(e['toSide'], SIDES)

    def test_parent_lines_run_down_and_spouse_lines_run_across(self):
        _run(views.run_canvas, self.root)
        data = self.canvas()
        y = {n['id']: n['y'] for n in data['nodes']}
        kinds = set()
        for e in data['edges']:
            if e['fromSide'] == 'bottom':
                self.assertLess(y[e['fromNode']], y[e['toNode']])
                kinds.add('down')
            else:
                self.assertEqual(y[e['fromNode']], y[e['toNode']])
                kinds.add('across')
        self.assertEqual(kinds, {'down', 'across'})

    def test_a_second_run_and_a_new_day_rewrite_nothing(self):
        _run(views.run_canvas, self.root)
        before = {p.name: p.read_bytes() for p in self.dir.glob('*.canvas')}
        result, out, _ = _run(views.run_canvas, self.root)
        self.assertEqual(result.exit_code, 0)
        self.assertIn('Wrote 0 canvas(es)', out)
        with mock.patch.object(views, '_today', return_value='2099-01-02'):
            result, out, _ = _run(views.run_canvas, self.root)
        self.assertIn('Wrote 0 canvas(es)', out)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.dir.glob('*.canvas')})

    def test_an_owners_canvas_is_left_alone_and_named(self):
        self.dir.mkdir(parents=True)
        mine = self.dir / f'{HARTLEY}.canvas'
        mine.write_text('{"nodes": [], "edges": []}\n', encoding='utf-8')
        result, _, err = _run(views.run_canvas, self.root)
        self.assertEqual(result.exit_code, 1)
        self.assertIn(f'{HARTLEY}.canvas', err)
        self.assertEqual(mine.read_text(encoding='utf-8'), '{"nodes": [], "edges": []}\n')
        # The other family still got its canvas.
        self.assertTrue((self.dir / '002 James Robert Bradford.canvas').is_file())

    def test_a_canvas_for_a_vanished_folder_is_removed_but_not_an_owners(self):
        _run(views.run_canvas, self.root)
        old = self.dir / '999 Gone Family.canvas'
        shutil.copy(self.dir / f'{HARTLEY}.canvas', old)
        mine = self.dir / 'my own.canvas'
        mine.write_text('{"nodes": [], "edges": []}\n', encoding='utf-8')
        _run(views.run_canvas, self.root)
        self.assertFalse(old.exists())
        self.assertTrue(mine.exists())

    def test_clean_removes_owned_canvases_only(self):
        _run(views.run_canvas, self.root)
        mine = self.dir / 'my own.canvas'
        mine.write_text('{"nodes": [], "edges": []}\n', encoding='utf-8')
        _run(views.run_clean, self.root)
        self.assertEqual([p.name for p in self.dir.glob('*.canvas')], ['my own.canvas'])

    def test_dry_run_prints_the_same_lines_and_writes_nothing(self):
        _, dry_out, _ = _run(views.run_canvas, self.root, dry_run=True)
        self.assertFalse(self.dir.exists())
        _, live_out, _ = _run(views.run_canvas, self.root)
        lines = lambda s: [l.replace('would write', 'write') for l in s.splitlines()
                           if l.strip().startswith(('would write', 'write'))]
        self.assertEqual(lines(dry_out), lines(live_out))
        self.assertTrue(lines(live_out))

    def test_one_person_canvas_is_named_for_their_profile(self):
        pid = 'p-' + next(iter(sorted(
            re.findall(r'(?m)^id: p-([0-9a-f]+)', (next(
                (self.root / 'people' / 'stubs').glob('*.md'))).read_text(encoding='utf-8')))))
        result, _, _ = _run(views.run_canvas, self.root, pid)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(list(self.dir.glob('*.canvas'))), 1)

    def test_a_malformed_id_is_refused_with_a_next_step(self):
        result, _, err = _run(views.run_canvas, self.root, 'nonsense')
        self.assertEqual(result.exit_code, 3)
        self.assertIn('fha find', err)

    def test_id_mode_writes_nothing(self):
        _set_mode(self.root, None)
        result, out, _ = _run(views.run_canvas, self.root)
        self.assertEqual(result.exit_code, 0)
        self.assertIn('links: obsidian', out)
        self.assertFalse((self.root / 'notes' / 'obsidian').exists())

    def test_nothing_lands_in_the_record_trees(self):
        _run(views.run_canvas, self.root)
        for tree in ('people', 'sources', 'places'):
            self.assertEqual(list((self.root / tree).rglob('*.canvas')), [])

    def test_the_tables_still_arrive_after_a_canvas_run(self):
        _run(views.run_canvas, self.root)
        _run(views.run_places, self.root)
        self.assertTrue((self.root / 'notes' / 'obsidian' / 'People.base').is_file())

    def test_refresh_keeps_the_canvases_current(self):
        _run(views.run_refresh, self.root)
        self.assertTrue((self.dir / f'{HARTLEY}.canvas').is_file())


if __name__ == '__main__':
    unittest.main()
