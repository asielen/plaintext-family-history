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
import os
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
            # #76: no auto-created research companion; the profile's own body
            # is backfilled with the full section set instead.
            self.assertFalse(
                (self.root / 'people' / FOLDER / f'deep__{slug}_research_{pid}.md').exists())
            self.assertIn('## Sources', rec.read_text(encoding='utf-8'))
        gpa_rec = self.root / 'people' / '004 Gpa Deep' / f'deep__gpa_{GPA}.md'
        self.assertTrue(gpa_rec.exists())
        # The index cache was dropped (moves are mtime-invisible) and the
        # follow-ups name the reindex and the views regeneration.
        self.assertFalse((self.root / '.cache' / 'index.sqlite').exists())
        self.assertIn('fha index', out)
        self.assertIn('fha views refresh', out)

    def test_a_record_deleted_since_the_last_index_build_refuses_upfront(self) -> None:
        # #48: a record vanishing since `fha index` last ran is exactly the
        # staleness the freshness manifest exists to catch (deleting a file
        # raises no other file's mtime, so before #48 this was invisible and
        # the batch proceeded on an index it should not have trusted). A
        # mutating, generating command like --fix-promote asks for a STRICT
        # index (`open_index_db(..., strict=True)`, a pre-#48 contract:
        # "generating/mutating commands can't safely act on stale data") -
        # so this now refuses the whole batch up front, before promoting
        # anyone, rather than discovering the gap mid-apply. Nothing is
        # half-done: the human reindexes and re-runs, exactly as the error
        # names.
        (self.root / 'people' / 'stubs' / f'deep__ma_{MA}.md').unlink()
        with mock.patch('builtins.input', return_value='y'):
            res, _out, err = self._run(fix_promote=True)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertIn('index is stale', err)
        self.assertIn('fha index', err)
        # Nothing promoted - the refusal is upfront, not partial. (MA's own
        # file is genuinely gone, by this test's own unlink() above; PA and
        # GPA staying put is what proves the batch never got to apply.)
        self.assertIn(f'deep__pa_{PA}.md', self._stub_names())
        self.assertIn(f'deep__gpa_{GPA}.md', self._stub_names())

    def test_a_record_that_vanishes_mid_apply_is_skipped_not_fatal(self) -> None:
        # The race #48's fix does NOT (and should not) catch: a record still
        # present at the freshness check moments ago, but gone by the time
        # THIS run's own apply loop reaches it (a human deletes it while
        # --fix-promote is mid-batch). _apply_promotions re-checks each
        # candidate against disk via find_person_record_path for exactly this
        # reason - the batch must still promote everyone else and report one
        # failure, not abort or silently skip the count.
        real_lookup = views.find_person_record_path

        def vanished_for_ma(archive_root, pid, unreadable=None):
            if normalize_id(pid) == normalize_id(MA):
                return None
            return real_lookup(archive_root, pid, unreadable=unreadable)

        with mock.patch('views.find_person_record_path', side_effect=vanished_for_ma), \
             mock.patch('builtins.input', return_value='y'):
            res, _out, err = self._run(fix_promote=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertEqual(res.data.get('failures'), 1)
        self.assertIn('ERROR', err)
        self.assertIn('Ma Deep', err)
        # PA and GPA still promoted; MA's file never moved (the mock only
        # hides it from the lookup, it is never actually deleted).
        self.assertTrue(
            (self.root / 'people' / FOLDER / f'deep__pa_{PA}.md').exists())
        self.assertTrue(
            (self.root / 'people' / 'stubs' / f'deep__ma_{MA}.md').exists())

    def test_plain_fix_does_not_promote(self) -> None:
        # --fix owns W103/W110 only; with only W119 present it says so plainly.
        with mock.patch('builtins.input', return_value='y'):
            res, out, _err = self._run(fix=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertIn('--fix-promote', out)
        self.assertEqual(len(self._stub_names()), 4)

    def test_yes_skips_the_gate_without_a_tty(self) -> None:
        # --yes (#38) must apply with stdin closed - a script, background job
        # or agent harness has no TTY, and EOF at the prompt used to be the
        # only outcome. input() raising here proves the prompt is never read.
        def no_stdin(prompt=''):
            raise EOFError

        with mock.patch('builtins.input', side_effect=no_stdin):
            res, out, _err = self._run(fix_promote=True, yes=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertIn('--yes', out)
        self.assertEqual(self._stub_names(), [f'far__frank_{FRIEND}.md'])

    def test_without_yes_a_closed_stdin_still_declines(self) -> None:
        # The safe default is unchanged: no --yes + no answer = no writes.
        with mock.patch('builtins.input', side_effect=EOFError):
            res, out, _err = self._run(fix_promote=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertIn('Aborted - no changes written.', out)
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
        # dest_folder, and - both partners being in the batch - that folder is
        # named for the couple from the start (SPEC 12.2's illustrated
        # convention), even-slot (GPA) partner first.
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
        self.assertEqual(by_pid[normalize_id(GPA)]['dest_folder'].name,
                         '004 Gpa Deep + Gma Deep')

    def test_fix_promote_lands_the_couple_in_one_folder(self) -> None:
        with mock.patch('builtins.input', return_value='y'):
            res, _out, _err = self._run(fix_promote=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        # Exactly one 004-prefixed folder, named for the couple (both partners
        # are in the batch), holding BOTH partners' records.
        prefixed = [d for d in self._root_dirs() if d.startswith('004')]
        self.assertEqual(prefixed, ['004 Gpa Deep + Gma Deep'], prefixed)
        shared = self.root / 'people' / '004 Gpa Deep + Gma Deep'
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
        self.assertIn('004 Gpa Deep + Gma Deep', out)
        self.assertNotIn('004 Gma Deep + Gpa Deep', out)


class UnnamedPartnerFolderTests(BracketsPromoteBase):
    """A couple folder invented for a batch promotion must never carry a
    placeholder name.

    `fha stubs` mints an unnamed reference as `name: unknown`. When the even
    (father-slot) member is such a stub and the odd-slot partner has a real
    name, naming the folder after the even slot produced `004 unknown + Gma
    Deep` - and because that base already carries a `+`, W103's add-only spouse
    rule would never revisit it once the unnamed person was finally named. The
    known partner becomes the temporary base instead: out of the conventional
    order, but a name W103 can finish later.
    """

    def setUp(self) -> None:
        super().setUp()
        # GPA has no recorded name yet; GMA does. PA is their child, so GPA
        # derives to position 4 (sex M) and GMA to 5.
        (self.root / 'people' / 'stubs' / f'deep__gpa_{GPA}.md').write_text(
            _ptext(GPA, 'unknown', 'M'), encoding='utf-8')
        (self.root / 'people' / 'stubs' / f'deep__gma_{GMA}.md').write_text(
            _ptext(GMA, 'Gma Deep', 'F'), encoding='utf-8')
        claims = (_rel_claim('C-2aaaaaaaaa', KID, [PA, MA])
                  + _rel_claim('C-2bbbbbbbbb', PA, [GPA, GMA]))
        (self.root / 'sources' / 'notes' / f'rel_{SID.lower()}.md').write_text(
            f'---\nid: {SID}\ntitle: Rel\nsource_type: other\n---\n\n'
            f'## Claims\n```yaml\n{claims}```\n', encoding='utf-8')
        shutil.rmtree(self.root / '.cache', ignore_errors=True)
        self._reindex()

    def _root_dirs(self) -> list[str]:
        return sorted(p.name for p in (self.root / 'people').iterdir() if p.is_dir())

    def test_the_known_partner_names_the_new_folder(self) -> None:
        with mock.patch('builtins.input', return_value='y'):
            res, out, _err = self._run(fix_promote=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        prefixed = [d for d in self._root_dirs() if d.startswith('004')]
        self.assertEqual(prefixed, ['004 Gma Deep'], prefixed)
        self.assertNotIn('004 unknown', out)
        shared = self.root / 'people' / '004 Gma Deep'
        self.assertTrue((shared / f'deep__gpa_{GPA}.md').exists())
        self.assertTrue((shared / f'deep__gma_{GMA}.md').exists())

    def test_w103_can_finish_the_name_once_the_partner_is_named(self) -> None:
        with mock.patch('builtins.input', return_value='y'):
            self._run(fix_promote=True)
        rec = self.root / 'people' / '004 Gma Deep' / f'deep__gpa_{GPA}.md'
        rec.write_text(rec.read_text(encoding='utf-8').replace(
            'name: unknown', 'name: Gpa Deep'), encoding='utf-8')
        shutil.rmtree(self.root / '.cache', ignore_errors=True)
        self._reindex()
        res, out, _err = self._run()
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertIn('names only one partner - add Gpa Deep', out)

    def test_two_unnamed_partners_keep_the_placeholder(self) -> None:
        # Nothing better exists: this is exactly what promoting one unnamed
        # stub produces today, and the folder reads as unfinished, which it is.
        (self.root / 'people' / 'stubs' / f'deep__gma_{GMA}.md').write_text(
            _ptext(GMA, 'unknown', 'F'), encoding='utf-8')
        shutil.rmtree(self.root / '.cache', ignore_errors=True)
        self._reindex()
        res, _out, _err = self._run(fix_promote=True, dry_run=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        conn = views.open_index_db(self.root, ('persons',))
        try:
            pid_to_pos = views._build_ahnentafel_map(conn, normalize_id(KID))
            issues = views._check_w119_direct_line_stubs(conn, self.root, pid_to_pos)
        finally:
            conn.close()
        dests = {i['dest_folder'].name for i in issues
                 if normalize_id(i['pid']) in (normalize_id(GPA), normalize_id(GMA))}
        self.assertEqual(dests, {'004 unknown'})


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


class RealignTests(BracketsPromoteBase):
    """--realign: renames/moves and promotions in one previewed pass, with
    promotion destinations recomputed against the POST-rename tree.

    The trap this class pins down: PA + MA's couple folder is hand-misnamed to
    prefix 004 - exactly the prefix GPA's promotion needs. The naive answer
    (follow the folder) would promote GPA into PA + MA's folder; the naive
    opposite (keep the pre-fix destination) would do the same. Realign must
    rename the folder back to 002 AND send GPA to a fresh `004 Gpa Deep`.
    """

    WRONG = '004 Pa Deep + Ma Deep'
    FIXED = '002 Pa Deep + Ma Deep [Kid]'   # the rename also refreshes the bracket

    def setUp(self) -> None:
        super().setUp()
        # Promote PA + MA the ordinary way (GPA is generation 2, stays a stub),
        # then hand-misname their couple folder onto GPA's prefix.
        with mock.patch('builtins.input', return_value='y'):
            self._run(fix_promote=True, generations=1)
        (self.root / 'people' / FOLDER).rename(self.root / 'people' / self.WRONG)
        shutil.rmtree(self.root / '.cache', ignore_errors=True)
        self._reindex()

    def _people_dirs(self) -> list[str]:
        return sorted(p.name for p in (self.root / 'people').iterdir() if p.is_dir())

    def test_realign_refused_with_either_fix_flag(self) -> None:
        for extra in ({'fix': True}, {'fix_promote': True}):
            res, _out, err = self._run(realign=True, **extra)
            self.assertEqual(res.exit_code, EXIT_FAILURE, extra)
            self.assertIn('--realign', err)
        self.assertIn(self.WRONG, self._people_dirs())   # nothing written

    def test_dry_run_previews_both_halves_and_writes_nothing(self) -> None:
        res, out, _err = self._run(realign=True, dry_run=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        # One combined preview: the rename half AND the promotion half.
        self.assertIn('Ahnentafel folder renames (W110):', out)
        self.assertIn('Stub promotions (W119):', out)
        self.assertIn('dry-run: no changes written', out)
        # The promotion preview already points at the fresh 004 folder, never
        # at the misnamed couple folder that is about to be renamed away.
        self.assertIn('004 Gpa Deep', out)
        # Nothing moved, nothing renamed, cache intact.
        self.assertIn(self.WRONG, self._people_dirs())
        self.assertIn(f'deep__gpa_{GPA}.md', self._stub_names())
        self.assertTrue((self.root / '.cache' / 'index.sqlite').exists())

    def test_declined_gate_writes_nothing(self) -> None:
        with mock.patch('builtins.input', return_value='n'):
            res, out, _err = self._run(realign=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        self.assertIn('Aborted - no changes written.', out)
        self.assertIn(self.WRONG, self._people_dirs())
        self.assertIn(f'deep__gpa_{GPA}.md', self._stub_names())

    def test_apply_realigns_renames_and_promotions_together(self) -> None:
        with mock.patch('builtins.input', return_value='y'):
            res, out, _err = self._run(realign=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        dirs = self._people_dirs()
        # The misnamed couple folder went back to its derived prefix (bracket
        # refreshed in the same rename), and GPA got his own fresh folder -
        # NOT a berth in the folder that used to carry his prefix.
        self.assertIn(self.FIXED, dirs)
        self.assertNotIn(self.WRONG, dirs)
        self.assertIn('004 Gpa Deep', dirs)
        gpa_rec = self.root / 'people' / '004 Gpa Deep' / f'deep__gpa_{GPA}.md'
        self.assertTrue(gpa_rec.exists())
        self.assertEqual(str(read_record(gpa_rec)['meta'].get('tier')), 'curated')
        self.assertNotIn(f'deep__gpa_{GPA}.md', self._stub_names())
        # PA + MA's records traveled with their folder's rename.
        self.assertTrue((self.root / 'people' / self.FIXED
                         / f'deep__pa_{PA}.md').exists())
        # One cache drop, the usual follow-ups named once.
        self.assertFalse((self.root / '.cache' / 'index.sqlite').exists())
        self.assertIn('fha index', out)
        self.assertIn('fha views refresh', out)

    def test_failed_rename_skips_the_promotions(self) -> None:
        # The promotion destinations were rebased onto the POST-rename tree.
        # If the rename fails non-fatally (Windows: the folder is open in
        # Explorer), promoting GPA into '004 Gpa Deep' would create it BESIDE
        # the un-renamed '004 Pa Deep + Ma Deep' - two canonical folders on
        # prefix 004, the split the two-pass flow existed to prevent. The
        # promotions must be skipped and the human told to re-run.
        real_rename = os.rename

        def failing_rename(src, dst, *a, **k):
            if Path(src).name == self.WRONG:
                raise PermissionError(13, 'The process cannot access the file')
            return real_rename(src, dst, *a, **k)

        with mock.patch('builtins.input', return_value='y'), \
                mock.patch('os.rename', failing_rename):
            res, _out, err = self._run(realign=True)
        self.assertEqual(res.exit_code, EXIT_WARNINGS)
        dirs = self._people_dirs()
        self.assertIn(self.WRONG, dirs)          # the rename did not land
        self.assertNotIn('004 Gpa Deep', dirs)   # and no folder was invented beside it
        self.assertIn(f'deep__gpa_{GPA}.md', self._stub_names())
        self.assertIn('NOT applied', err)
        self.assertIn('--realign', err)
        self.assertEqual(len([d for d in dirs if d.startswith('004')]), 1)

    def test_realign_converges_after_bracketing_the_fresh_folder(self) -> None:
        with mock.patch('builtins.input', return_value='y'):
            self._run(realign=True)
        self._reindex()
        # A promotion names the new folder but never brackets it (same as
        # `fha person promote`); the NEXT pass adds the child list - the one
        # remaining finding - and after that the tree is fully converged.
        with mock.patch('builtins.input', return_value='y'):
            res, out, _err = self._run(realign=True)
        self.assertEqual(res.data['w110'], 0)
        self.assertEqual(res.data['w119'], 0)
        self.assertEqual(res.data['w103'], 1)
        self.assertIn('004 Gpa Deep [Pa]', out)
        self._reindex()
        res, out, _err = self._run(realign=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        self.assertIn('no issues found', out)


class FileMoveConflictTests(BracketsPromoteBase):
    """A W110 file move whose destination is already occupied must stop the
    whole pass BEFORE the first rename.

    The trap: the preflight used to check folder-rename destinations only. So
    the folders were renamed, the blocked move was skipped as a failure, and -
    because a failed rename/move makes --realign skip the promote half - every
    promotion was skipped too. One command, presented as one whole-tree
    realignment, left a half-applied tree.

    Fixture: GPA is curated in a hand-misnamed `006 Gpa Deep` (his derived
    prefix is 004) that also holds his timeline companion, and a stray copy of
    that same companion sits in KID's couple folder. So the plan is a composed
    folder rename (prefix 004 + the [Pa] bracket) plus a file move into the
    folder that rename creates - a destination that does not exist on disk yet,
    which is exactly why checking `dst.exists()` at apply time was not enough.
    PA and MA stay stubs, so there are promotions to lose as well.
    """

    MISNAMED = '006 Gpa Deep'
    COMPANION = f'deep__gpa_timeline_{GPA}.md'

    def setUp(self) -> None:
        super().setUp()
        home = self.root / 'people' / self.MISNAMED
        home.mkdir()
        (self.root / 'people' / 'stubs' / f'deep__gpa_{GPA}.md').unlink()
        (home / f'deep__gpa_{GPA}.md').write_text(
            _ptext(GPA, 'Gpa Deep', 'M', 'curated'), encoding='utf-8')
        self.stray = self.root / 'people' / FOLDER / self.COMPANION
        self.stray.write_text('<!-- GENERATED -->\nstray\n', encoding='utf-8')
        shutil.rmtree(self.root / '.cache', ignore_errors=True)
        self._reindex()

    def _settle_destination(self) -> Path:
        """Put a file where the move wants to land (it travels with the rename)."""
        settled = self.root / 'people' / self.MISNAMED / self.COMPANION
        settled.write_text('<!-- GENERATED -->\nsettled\n', encoding='utf-8')
        shutil.rmtree(self.root / '.cache', ignore_errors=True)
        self._reindex()
        return settled

    def _people_dirs(self) -> list[str]:
        return sorted(p.name for p in (self.root / 'people').iterdir() if p.is_dir())

    def test_taken_destination_stops_the_whole_pass(self) -> None:
        settled = self._settle_destination()
        with mock.patch('builtins.input', return_value='y'):
            res, out, err = self._run(realign=True)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertIn('already', err)
        self.assertIn(self.COMPANION, err)
        self.assertNotIn('Traceback', err)
        # Nothing was applied: no rename, no move, no promotion, no cache drop.
        self.assertIn(self.MISNAMED, self._people_dirs())
        self.assertNotIn('004 Gpa Deep [Pa]', self._people_dirs())
        self.assertTrue(self.stray.exists())
        self.assertEqual(settled.read_text(encoding='utf-8'),
                         '<!-- GENERATED -->\nsettled\n')
        self.assertIn(f'deep__pa_{PA}.md', self._stub_names())
        self.assertIn(f'deep__ma_{MA}.md', self._stub_names())
        self.assertTrue((self.root / '.cache' / 'index.sqlite').exists())

    def test_plain_fix_refuses_the_same_plan(self) -> None:
        self._settle_destination()
        with mock.patch('builtins.input', return_value='y'):
            res, _out, err = self._run(fix=True)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertIn(self.COMPANION, err)
        self.assertIn(self.MISNAMED, self._people_dirs())
        self.assertTrue(self.stray.exists())

    def test_dry_run_reports_the_same_refusal(self) -> None:
        # --dry-run must say what the apply would: same refusal, same exit
        # code, never a preview promising a run that cannot work.
        self._settle_destination()
        res, out, err = self._run(fix=True, dry_run=True)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertIn(self.COMPANION, err)
        self.assertNotIn('dry-run: no changes written', out)
        self.assertIn(self.MISNAMED, self._people_dirs())

    def test_a_free_destination_still_applies(self) -> None:
        # The mirror: the same plan with nothing in the way must still run, so
        # the preflight is not simply refusing every move.
        with mock.patch('builtins.input', return_value='y'):
            res, _out, _err = self._run(fix=True)
        self.assertEqual(res.exit_code, EXIT_CLEAN)
        dirs = self._people_dirs()
        self.assertIn('004 Gpa Deep [Pa]', dirs)
        self.assertNotIn(self.MISNAMED, dirs)
        self.assertFalse(self.stray.exists())
        moved = self.root / 'people' / '004 Gpa Deep [Pa]' / self.COMPANION
        self.assertTrue(moved.exists())


class SplitCoupleRenameTests(BracketsPromoteBase):
    """Two folders that would both end up carrying one couple number.

    PA and MA are the same couple filed in two folders (`006 Pa Deep` and
    `007 Ma Deep`), so each claims couple number 002 from its own occupant.
    Applying both renames leaves two `002 …` folders - precisely the ambiguous
    tree the next bracket run refuses outright (`couple_folder_for_prefix`), so
    the run would fix the tree into a state it can no longer read. The
    preflight names it while nothing has moved.
    """

    def setUp(self) -> None:
        super().setUp()
        people = self.root / 'people'
        # KID moves to his own folder so nothing else holds prefix 002.
        kid_home = people / '000 Kid Deep'
        kid_home.mkdir()
        (people / FOLDER / f'deep__kid_{KID}.md').rename(
            kid_home / f'deep__kid_{KID}.md')
        (people / FOLDER).rmdir()
        for pid, name, sex, folder in ((PA, 'Pa Deep', 'M', '006 Pa Deep'),
                                       (MA, 'Ma Deep', 'F', '007 Ma Deep')):
            home = people / folder
            home.mkdir()
            (people / 'stubs' / f'deep__{name.split()[0].lower()}_{pid}.md').unlink()
            (home / f'deep__{name.split()[0].lower()}_{pid}.md').write_text(
                _ptext(pid, name, sex, 'curated'), encoding='utf-8')
        shutil.rmtree(self.root / '.cache', ignore_errors=True)
        self._reindex()

    def _people_dirs(self) -> list[str]:
        return sorted(p.name for p in (self.root / 'people').iterdir() if p.is_dir())

    def test_a_pre_existing_duplicate_number_does_not_block_a_bracket_fix(self) -> None:
        # The guard must fire only for a duplicate the PLAN creates. An archive
        # that already has two folders on one number (and no root_person, so
        # the Ahnentafel checks that would name it are off) must still be able
        # to apply an ordinary W103 bracket refresh.
        (self.root / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        (self.root / 'people' / '006 Pa Deep').rename(
            self.root / 'people' / '007 Pa Deep')
        shutil.rmtree(self.root / '.cache', ignore_errors=True)
        self._reindex()
        with mock.patch('builtins.input', return_value='y'):
            res, out, err = self._run(fix=True)
        self.assertNotEqual(res.exit_code, EXIT_FAILURE, err)
        self.assertIn('007 Pa Deep [Kid]', self._people_dirs())

    def test_two_folders_claiming_one_number_are_refused(self) -> None:
        with mock.patch('builtins.input', return_value='y'):
            res, _out, err = self._run(fix=True)
        self.assertEqual(res.exit_code, EXIT_FAILURE)
        self.assertIn('couple 002', err)
        self.assertIn('006 Pa Deep', err)
        self.assertIn('007 Ma Deep', err)
        self.assertNotIn('Traceback', err)
        dirs = self._people_dirs()
        self.assertIn('006 Pa Deep', dirs)
        self.assertIn('007 Ma Deep', dirs)
        self.assertFalse([d for d in dirs if d.startswith('002')])


if __name__ == '__main__':
    unittest.main()
