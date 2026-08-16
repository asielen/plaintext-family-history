"""
test_roots_change.py - a `roots:` change that orphans filed assets (#36).

Narrowing `roots: photos` from a library root to one subfolder instantly
orphans every source `files:` entry filed under the alias (alias paths are
relative to the root, so even entries inside the new subfolder stop
resolving). Before
this check, the first sign was a wall of lint E011s whose suggested remedy
(`fha reconcile`) cannot apply - nothing moved. The signal that misled the
human was `fha photoindex reconcile`'s "0 tracked" catalog count, which reads
as "no photos are filed" and is a different question entirely.

Contracts locked here:

  - `_lib.roots_change_orphans` seeds its stamp silently on first sight,
    accepts a harmless change, and reports (stickily) a change that leaves
    filed entries resolving under the OLD value but not the NEW one.
  - Entries that were already broken before the change are not blamed on it.
  - lint (W121 on fha.yaml, ahead of the E011 fallout), doctor (a warn line
    + check), and index (a warning Message on fha.yaml, EXIT_WARNINGS) all
    surface it. Reverting the value clears it everywhere.

Synthetic tmp archives only.
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import _lib
import doctor
import index
import lint
from _lib import EXIT_CLEAN, EXIT_WARNINGS, ROOTS_STAMP_NAME


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding='utf-8')


def _make_archive(tmp: Path) -> Path:
    """An archive with a wide photos root and two filed photos in sibling folders."""
    root = tmp / 'archive'
    _write(root / 'fha.yaml', 'roots:\n  photos: photos\n  documents: documents\n')
    (root / 'people').mkdir(parents=True)
    (root / 'places').mkdir(parents=True)
    _write(root / 'photos' / 'Woodbury' / 'portrait.jpg', 'x')
    _write(root / 'photos' / 'Church' / 'wedding.jpg', 'x')
    _write(root / 'photos' / 'Church' / 'gone.jpg', 'x')
    _write(root / 'sources' / 'photos' / 'portrait_S-0000000001.md',
           '---\nid: S-0000000001\ntitle: Portrait\nsource_type: photo\n'
           'created: 2026-01-01\nfiles:\n'
           '  - file: photos/Woodbury/portrait.jpg\n    role: front\n---\n')
    _write(root / 'sources' / 'photos' / 'wedding_S-0000000002.md',
           '---\nid: S-0000000002\ntitle: Wedding\nsource_type: photo\n'
           'created: 2026-01-01\nfiles:\n'
           '  - file: photos/Church/wedding.jpg\n    role: front\n'
           '  - file: photos/Church/gone.jpg\n    role: back\n---\n')
    return root


def _cfg(photos: str) -> dict:
    return {'roots': {'photos': photos, 'documents': 'documents'}}


class RootsChangeCoreTests(unittest.TestCase):
    def test_first_sight_seeds_the_stamp_and_reports_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = _make_archive(Path(d))
            self.assertEqual(_lib.roots_change_orphans(root, _cfg('photos')), [])
            stamp = json.loads((root / '.cache' / ROOTS_STAMP_NAME).read_text())
            self.assertEqual(stamp['photos'], 'photos')

    def test_narrowing_the_root_reports_the_orphans_and_stays_sticky(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = _make_archive(Path(d))
            _lib.roots_change_orphans(root, _cfg('photos'))          # seed

            # `gone.jpg` vanishes BEFORE the change - it must not be counted
            # against the change (it was already broken).
            (root / 'photos' / 'Church' / 'gone.jpg').unlink()

            report = _lib.roots_change_orphans(root, _cfg('photos/Woodbury'))
            self.assertEqual(len(report), 1)
            item = report[0]
            self.assertEqual(item['alias'], 'photos')
            self.assertEqual(item['old'], 'photos')
            self.assertEqual(item['new'], 'photos/Woodbury')
            # Both filed photos: alias paths are relative to the root, so
            # narrowing to photos/Woodbury makes even photos/Woodbury/portrait.jpg
            # resolve to .../Woodbury/Woodbury/portrait.jpg - EVERYTHING filed
            # under the alias orphans, which is precisely why the change is a
            # trap. gone.jpg was broken before the change and is not counted.
            self.assertEqual(item['orphaned'], 2)
            self.assertEqual(sorted(item['sample']),
                             ['photos/Church/wedding.jpg', 'photos/Woodbury/portrait.jpg'])

            # Sticky: the stamp still remembers the old value, so a second
            # look reports the same thing instead of accepting the damage.
            stamp = json.loads((root / '.cache' / ROOTS_STAMP_NAME).read_text())
            self.assertEqual(stamp['photos'], 'photos')
            self.assertEqual(len(_lib.roots_change_orphans(root, _cfg('photos/Woodbury'))), 1)

            # Reverting clears it.
            self.assertEqual(_lib.roots_change_orphans(root, _cfg('photos')), [])

    def test_a_harmless_change_is_accepted_and_the_stamp_moves(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = _make_archive(Path(d))
            _lib.roots_change_orphans(root, _cfg('photos'))
            # Move the library and every filed entry still resolves.
            (root / 'photos').rename(root / 'library')
            self.assertEqual(_lib.roots_change_orphans(root, _cfg('library')), [])
            stamp = json.loads((root / '.cache' / ROOTS_STAMP_NAME).read_text())
            self.assertEqual(stamp['photos'], 'library')

    def test_warning_names_cause_and_the_photos_ignore_alternative(self) -> None:
        item = {'alias': 'photos', 'old': 'D:/Photos', 'new': 'D:/Photos/Branch',
                'orphaned': 20, 'sample': ['photos/A/x.jpg', 'photos/B/y.jpg', 'photos/C/z.jpg']}
        text = _lib.format_roots_orphan_warning(item, Path('/arch'))
        self.assertIn("changed from 'D:/Photos' to 'D:/Photos/Branch'", text)
        self.assertIn('20 filed file(s)', text)
        self.assertIn('and 17 more', text)
        self.assertIn('fha reconcile', text)
        self.assertIn('photos_ignore', text)


class RootsChangeSurfacesTests(unittest.TestCase):
    def _lint_codes(self, root: Path, cfg: dict) -> list[str]:
        findings, _registry = lint._run_lint_core(root, cfg)
        return [f.code for f in findings]

    def test_lint_is_read_only_about_the_stamp(self) -> None:
        # A linter pointed at a fixture or a read-only checkout must not
        # create files there: lint compares and reports, index/doctor record.
        with tempfile.TemporaryDirectory() as d:
            root = _make_archive(Path(d))
            self.assertNotIn('W121', self._lint_codes(root, _cfg('photos')))
            self.assertFalse((root / '.cache' / ROOTS_STAMP_NAME).exists())
            # With no stamp there is nothing to compare, so no W121 either -
            # even on a value that WOULD orphan. Recording is what arms it.
            self.assertNotIn('W121', self._lint_codes(root, _cfg('photos/Woodbury')))

    def test_lint_emits_w121_on_fha_yaml_ahead_of_the_e011s(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = _make_archive(Path(d))
            _lib.roots_change_orphans(root, _cfg('photos'))   # what fha index / doctor do
            self.assertNotIn('W121', self._lint_codes(root, _cfg('photos')))

            findings, _r = lint._run_lint_core(root, _cfg('photos/Woodbury'))
            codes = [f.code for f in findings]
            self.assertIn('W121', codes)
            self.assertIn('E011', codes)
            self.assertLess(codes.index('W121'), codes.index('E011'))
            w121 = next(f for f in findings if f.code == 'W121')
            self.assertEqual(Path(w121.path).name, 'fha.yaml')
            self.assertIn('photos_ignore', w121.message)
            # And the E011 now points at W121 instead of only at reconcile.
            e011 = next(f for f in findings if f.code == 'E011')
            self.assertIn('W121', e011.message)

            self.assertNotIn('W121', self._lint_codes(root, _cfg('photos')))   # revert clears

    def test_doctor_warns_with_a_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = _make_archive(Path(d))
            _lib.roots_change_orphans(root, _cfg('photos'))
            with redirect_stdout(io.StringIO()):
                res = doctor.run_doctor(root, _cfg('photos/Woodbury'))
            ids = [c['id'] for c in res.data['checks']]
            self.assertIn('root_change:photos', ids)
            check = next(c for c in res.data['checks'] if c['id'] == 'root_change:photos')
            self.assertEqual(check['status'], 'warn')
            self.assertTrue(any('changed from' in ln for ln in res.data['lines']))
            self.assertGreaterEqual(res.exit_code, EXIT_WARNINGS)

    def test_index_build_carries_the_warning_and_exits_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = _make_archive(Path(d))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                first = index.build_index(root, _cfg('photos'))
                self.assertEqual(first.exit_code, EXIT_CLEAN)
                second = index.build_index(root, _cfg('photos/Woodbury'))
            self.assertEqual(second.exit_code, EXIT_WARNINGS)
            texts = [m.text for m in second.messages if m.path == 'fha.yaml']
            self.assertEqual(len(texts), 1)
            self.assertIn('photos/Church/wedding.jpg', texts[0])


if __name__ == '__main__':
    unittest.main()
