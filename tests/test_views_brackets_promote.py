"""
test_views_brackets_promote.py - brackets check 4 (W119) and --fix-promote.

W119 flags direct-line ancestors (derived Ahnentafel position >= 2) whose
record is still a stub - `tier: stub`, or a record parked under
people/stubs/ - exactly the people the W110 machinery deliberately skips.
Report-only by default (a lead, never a defect); `--generations N` narrows
the depth; `--fix-promote` batch-applies the shared promote engine
(`_lib.promote_person_record`) under the same previewed Apply? [y/N] gate
the other bracket fixes use. Fixtures only.
"""

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import views
import index as index_mod
from _lib import (
    EXIT_CLEAN,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    load_fha_yaml,
    normalize_id,
    read_record,
)

KID = 'P-2aaaaaaaaa'
PA = 'P-2bbbbbbbbb'
MA = 'P-2ccccccccc'
GPA = 'P-2ddddddddd'
FRIEND = 'P-2eeeeeeeee'
SID = 'S-2aaaaaaaaa'
FOLDER = '002 Pa Deep + Ma Deep'


def _ptext(pid: str, name: str, sex: str = 'U', tier: str = 'stub') -> str:
    return (f'---\nid: {pid}\nname: {name}\nsex: {sex}\nliving: false\n'
            f'tier: {tier}\n---\n\n# {name}\n\n## Biography\n\nx\n')


def _rel_claim(cid: str, child: str, parents: list[str]) -> str:
    plist = ', '.join(parents)
    persons = ', '.join([child] + parents)
    return (
        f'- value: "{child} child of {plist}"\n'
        f'  id: {cid}\n  type: relationship\n  subtype: biological\n'
        f'  persons: [{persons}]\n  roles:\n'
        f'    child: {child}\n    parent: [{plist}]\n'
        f'  status: accepted\n  reviewed: 2026-01-01\n  confidence: high\n'
        f'  information: primary\n  evidence: direct\n  notes: x.\n'
    )


class BracketsPromoteBase(unittest.TestCase):
    """Direct line: KID (curated, in the 002 folder) <- PA+MA <- GPA (PA's
    father). PA, MA, GPA are stubs in people/stubs/; FRIEND is off the line."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'people' / 'stubs').mkdir(parents=True)
        (self.root / 'people' / FOLDER).mkdir(parents=True)
        (self.root / 'sources' / 'notes').mkdir(parents=True)
        (self.root / 'fha.yaml').write_text(
            f'root_person: {KID}\nroots:\n  documents: documents\n',
            encoding='utf-8')
        (self.root / 'people' / FOLDER / f'deep__kid_{KID}.md').write_text(
            _ptext(KID, 'Kid Deep', 'F', 'curated'), encoding='utf-8')
        for pid, name, sex in ((PA, 'Pa Deep', 'M'), (MA, 'Ma Deep', 'F'),
                               (GPA, 'Gpa Deep', 'M')):
            slug = name.split()[0].lower()
            (self.root / 'people' / 'stubs' / f'deep__{slug}_{pid}.md').write_text(
                _ptext(pid, name, sex), encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'far__frank_{FRIEND}.md').write_text(
            _ptext(FRIEND, 'Frank Far', 'M'), encoding='utf-8')
        claims = (_rel_claim('C-2aaaaaaaaa', KID, [PA, MA])
                  + _rel_claim('C-2bbbbbbbbb', PA, [GPA]))
        (self.root / 'sources' / 'notes' / f'rel_{SID.lower()}.md').write_text(
            f'---\nid: {SID}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claims}```\n', encoding='utf-8')
        self._reindex()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _reindex(self) -> None:
        index_mod.build_index(self.root, load_fha_yaml(self.root))

    def _run(self, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            res = views.run_brackets(self.root, **kwargs)
        return res, out.getvalue(), err.getvalue()

    def _stub_names(self) -> list[str]:
        return sorted(p.name for p in (self.root / 'people' / 'stubs').iterdir())


class W119ReportTests(BracketsPromoteBase):
    def test_report_only_flags_all_direct_line_stubs(self) -> None:
        res, out, _err = self._run()
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertEqual(res.data['w119'], 3)   # PA, MA, GPA - not FRIEND, not KID
        self.assertIn('W119', out)
        self.assertIn('still filed as a stub', out)
        self.assertIn('fha person promote', out)
        self.assertNotIn(FRIEND, out)
        # Report-only: nothing moved, nothing written.
        self.assertEqual(len(self._stub_names()), 4)

    def test_generations_cap_narrows_the_set(self) -> None:
        res, out, _err = self._run(generations=1)
        self.assertEqual(res.data['w119'], 2)   # parents only; GPA (gen 2) out
        self.assertNotIn(GPA, out)

    def test_generations_must_be_positive(self) -> None:
        res, _out, err = self._run(generations=0)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertIn('--generations', err)

    def test_fix_and_fix_promote_together_refused(self) -> None:
        res, _out, err = self._run(fix=True, fix_promote=True)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertIn('--fix-promote', err)
        self.assertEqual(len(self._stub_names()), 4)

    def test_clean_when_promoted_records_are_curated(self) -> None:
        # Hand-promote everyone (tier + location); W119 must then be silent.
        for pid, slug in ((PA, 'pa'), (MA, 'ma')):
            src = self.root / 'people' / 'stubs' / f'deep__{slug}_{pid}.md'
            dst = self.root / 'people' / FOLDER / src.name
            dst.write_text(src.read_text(encoding='utf-8').replace(
                'tier: stub', 'tier: curated'), encoding='utf-8')
            src.unlink()
        gpa_folder = self.root / 'people' / '004 Gpa Deep'
        gpa_folder.mkdir()
        src = self.root / 'people' / 'stubs' / f'deep__gpa_{GPA}.md'
        (gpa_folder / src.name).write_text(
            src.read_text(encoding='utf-8').replace('tier: stub', 'tier: curated'),
            encoding='utf-8')
        src.unlink()
        self._reindex()
        res, _out, _err = self._run()
        self.assertEqual(res.data['w119'], 0)


class FixPromoteTests(BracketsPromoteBase):
    def test_dry_run_previews_and_writes_nothing(self) -> None:
        res, out, _err = self._run(fix_promote=True, dry_run=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertIn('Stub promotions (W119):', out)
        self.assertIn('dry-run: no changes written', out)
        self.assertEqual(len(self._stub_names()), 4)
        self.assertTrue((self.root / '.cache' / 'index.sqlite').exists())

    def test_declined_gate_writes_nothing(self) -> None:
        with mock.patch('builtins.input', return_value='n'):
            res, out, _err = self._run(fix_promote=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertIn('Aborted - no changes written.', out)
        self.assertEqual(len(self._stub_names()), 4)

    def test_apply_promotes_the_whole_set(self) -> None:
        with mock.patch('builtins.input', return_value='y'):
            res, out, _err = self._run(fix_promote=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        # Only the off-line FRIEND stays a stub.
        self.assertEqual(self._stub_names(), [f'far__frank_{FRIEND}.md'])
        # PA and MA landed in the existing 002 folder; GPA got a new 004 folder.
        for pid, slug in ((PA, 'pa'), (MA, 'ma')):
            rec = self.root / 'people' / FOLDER / f'deep__{slug}_{pid}.md'
            self.assertTrue(rec.exists(), rec)
            self.assertEqual(str(read_record(rec)['meta'].get('tier')), 'curated')
            self.assertTrue(
                (self.root / 'people' / FOLDER / f'deep__{slug}_research_{pid}.md').exists())
        gpa_rec = self.root / 'people' / '004 Gpa Deep' / f'deep__gpa_{GPA}.md'
        self.assertTrue(gpa_rec.exists())
        # The index cache was dropped (moves are mtime-invisible) and the
        # follow-ups name the reindex and the views regeneration.
        self.assertFalse((self.root / '.cache' / 'index.sqlite').exists())
        self.assertIn('fha index', out)
        self.assertIn('fha views refresh', out)

    def test_partial_failure_reports_and_exits_warnings(self) -> None:
        # One record vanishes between the index build and the apply - the
        # batch must promote the others, count the failure, and exit 1.
        (self.root / 'people' / 'stubs' / f'deep__ma_{MA}.md').unlink()
        with mock.patch('builtins.input', return_value='y'):
            res, _out, err = self._run(fix_promote=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertEqual(res.data.get('failures'), 1)
        self.assertIn('ERROR', err)
        self.assertTrue(
            (self.root / 'people' / FOLDER / f'deep__pa_{PA}.md').exists())

    def test_plain_fix_does_not_promote(self) -> None:
        # --fix owns W103/W110 only; with only W119 present it says so plainly.
        with mock.patch('builtins.input', return_value='y'):
            res, out, _err = self._run(fix=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertIn('--fix-promote', out)
        self.assertEqual(len(self._stub_names()), 4)


GMA = 'P-2fffffffff'


class W119SharedCoupleFolderTests(BracketsPromoteBase):
    """Both partners of a couple are stubs and their couple folder does not
    exist yet.

    Extends the base line so PA's parents are a full couple: GPA (pos 4, the
    father slot) and GMA (pos 5, the mother slot), both stubs, with no `004`
    folder anywhere on disk. This is the split-couple regression: deriving each
    destination against the pre-promotion state used to invent `004 Gpa Deep`
    for one and `004 Gma Deep` for the other, so `--fix-promote` created two
    folders and split the couple. Both must instead land in ONE folder, named
    the way a normal one-at-a-time promote would name it.
    """

    def setUp(self) -> None:
        super().setUp()
        # Add GMA as a stub and make PA a child of GPA + GMA (was GPA only), so
        # GMA derives to Ahnentafel position 5, GPA stays at 4 - a couple whose
        # 004 folder does not exist.
        (self.root / 'people' / 'stubs' / f'deep__gma_{GMA}.md').write_text(
            _ptext(GMA, 'Gma Deep', 'F'), encoding='utf-8')
        claims = (_rel_claim('C-2aaaaaaaaa', KID, [PA, MA])
                  + _rel_claim('C-2bbbbbbbbb', PA, [GPA, GMA]))
        (self.root / 'sources' / 'notes' / f'rel_{SID.lower()}.md').write_text(
            f'---\nid: {SID}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claims}```\n', encoding='utf-8')
        # The index is incremental and keys on file mtime; rewriting the source
        # within the same second would be skipped. Drop the cache for a clean
        # full rebuild so GMA's pos-5 parent edge is picked up.
        shutil.rmtree(self.root / '.cache', ignore_errors=True)
        self._reindex()

    def _root_dirs(self) -> list[str]:
        return sorted(p.name for p in (self.root / 'people').iterdir() if p.is_dir())

    def test_check_gives_both_partners_one_shared_destination(self) -> None:
        # Unit-level: the check function must hand both pos-4 and pos-5 the same
        # dest_folder, and that folder must be the even-slot (GPA) name - what a
        # single `fha person promote GPA` would create.
        conn = views.open_index_db(self.root, ('persons',))
        try:
            # The index stores IDs lowercased; the map is keyed on those, so
            # seed with the normalized root and compare on normalized IDs.
            pid_to_pos = views._build_ahnentafel_map(conn, normalize_id(KID))
            issues = views._check_w119_direct_line_stubs(conn, self.root, pid_to_pos)
        finally:
            conn.close()
        by_pid = {normalize_id(i['pid']): i for i in issues}
        self.assertIn(normalize_id(GPA), by_pid)
        self.assertIn(normalize_id(GMA), by_pid)
        self.assertEqual(
            by_pid[normalize_id(GPA)]['dest_folder'],
            by_pid[normalize_id(GMA)]['dest_folder'])
        self.assertEqual(by_pid[normalize_id(GPA)]['dest_folder'].name, '004 Gpa Deep')

    def test_fix_promote_lands_the_couple_in_one_folder(self) -> None:
        with mock.patch('builtins.input', return_value='y'):
            res, _out, _err = self._run(fix_promote=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        # Exactly one 004-prefixed folder, holding BOTH partners' records.
        prefixed = [d for d in self._root_dirs() if d.startswith('004')]
        self.assertEqual(prefixed, ['004 Gpa Deep'], prefixed)
        shared = self.root / 'people' / '004 Gpa Deep'
        gpa_rec = shared / f'deep__gpa_{GPA}.md'
        gma_rec = shared / f'deep__gma_{GMA}.md'
        self.assertTrue(gpa_rec.exists(), gpa_rec)
        self.assertTrue(gma_rec.exists(), gma_rec)
        self.assertEqual(str(read_record(gpa_rec)['meta'].get('tier')), 'curated')
        self.assertEqual(str(read_record(gma_rec)['meta'].get('tier')), 'curated')
        # No stray split folder for the mother slot.
        self.assertNotIn('004 Gma Deep', self._root_dirs())

    def test_dry_run_preview_names_the_shared_folder_for_both(self) -> None:
        # Dry-run and live must agree: the preview names the one shared folder
        # for both partners, so no dry-run vs live divergence.
        res, out, _err = self._run(fix_promote=True, dry_run=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertIn('004 Gpa Deep', out)
        self.assertNotIn('004 Gma Deep', out)


class AmbiguousCoupleFolderTests(BracketsPromoteBase):
    """Two folders share one couple prefix - brackets must refuse, not guess."""

    def test_report_refuses_and_names_both_folders(self) -> None:
        # PA (pos 2) and MA (pos 3) share couple prefix 2. Add a second '002 …'
        # folder so the prefix maps to two folders; the Ahnentafel checks cannot
        # derive a destination without guessing, so the run is refused.
        (self.root / 'people' / '002 Dupe Folder').mkdir()
        res, _out, err = self._run()
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertIn(FOLDER, err)
        self.assertIn('002 Dupe Folder', err)
        self.assertIn('fha index', err)
        self.assertNotIn('Traceback', err)
        # Nothing was promoted.
        self.assertEqual(len(self._stub_names()), 4)


class FixPromoteCacheUnlinkTests(BracketsPromoteBase):
    """The batch --fix-promote path must survive a failed index-cache drop the
    same way the single-person path does: report the promotions that landed and
    return a non-zero warning naming the stale cache, never a traceback."""

    def _unlink_only_index_fails(self):
        real_unlink = Path.unlink

        def fake_unlink(self, *args, **kwargs):
            if self.name == 'index.sqlite':
                raise PermissionError(13, 'Permission denied')
            return real_unlink(self, *args, **kwargs)

        return mock.patch.object(Path, 'unlink', fake_unlink)

    def test_batch_unlink_failure_warns_nonzero_without_traceback(self) -> None:
        with mock.patch('builtins.input', return_value='y'):
            with self._unlink_only_index_fails():
                res, out, err = self._run(fix_promote=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertTrue(res.data.get('index_stale'))
        self.assertEqual(res.data.get('promoted'), 3)   # PA, MA, GPA
        # The records really moved on disk despite the cache-drop failure.
        self.assertTrue(
            (self.root / 'people' / FOLDER / f'deep__pa_{PA}.md').exists())
        # The stale cache is still present (the unlink failed) and the message
        # owns that, naming the file and the rebuild command - no traceback.
        self.assertTrue((self.root / '.cache' / 'index.sqlite').exists())
        combined = out + err
        self.assertIn('Promoted', combined)
        self.assertIn('index.sqlite', combined)
        self.assertIn('fha index', combined)
        self.assertNotIn('Traceback', combined)


if __name__ == '__main__':
    unittest.main()
