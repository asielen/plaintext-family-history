"""
test_reorganize.py - fha reorganize, the documents-root bulk-tidy tool (#107).

The engine reads source records and the documents tree directly (no index
needed), so the fixture here is a tiny on-disk archive: one or more source
records with `files:` inventories, plus documents-root files placed flat,
one level down in a type folder, or nested inside a hand-made folder - the
three shapes the eligibility rule (`_plan`) has to tell apart. Photos are
out of scope for this tool by design (see `reorganize.py`'s module
docstring), so no photos fixture is needed here.

Run: py -3.14 -m unittest tests.test_reorganize -v   (from the repo root)
"""

import contextlib
import io
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import reconcile
import reorganize
from _lib import EXIT_CLEAN, EXIT_FAILURE, EXIT_WARNINGS, Result, load_fha_yaml, read_record

SID_A = 'S-2b3c4d5e6f'
SID_B = 'S-3c4d5e6f7g'
SID_C = 'S-4d5e6f7g8h'
SID_D = 'S-5e6f7g8h9j'


def _make_archive(tmp: Path) -> Path:
    """A minimal archive root: internal documents/ and photos/ roots."""
    archive = tmp / 'archive'
    (archive / 'documents').mkdir(parents=True)
    (archive / 'sources').mkdir(parents=True)
    (archive / 'fha.yaml').write_text(
        'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
    return archive


def _write_record(archive: Path, sid: str, source_type: str, aliases: list[str],
                   *, slug: str = 'thing', record_dir: str | None = None) -> Path:
    """A minimal but complete source record naming the given documents-root aliases."""
    subdir = record_dir if record_dir is not None else reorganize._record_subdir(source_type)
    rec_dir = archive / 'sources' / subdir
    rec_dir.mkdir(parents=True, exist_ok=True)
    lines = ''.join(f'  - file: {a}\n    role: primary\n' for a in aliases)
    text = (
        '---\n'
        f'id: {sid}\n'
        f'title: Title for {sid}\n'
        f'source_type: {source_type}\n'
        f'files:\n{lines}'
        '---\n\n## Claims\n```yaml\n[]\n```\n'
    )
    rec = rec_dir / f'{slug}_{sid}.md'
    rec.write_text(text, encoding='utf-8')
    return rec


def _write_file(archive: Path, rel: str, content: bytes = b'bytes') -> Path:
    p = archive / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


class ReorganizeSurveyTests(unittest.TestCase):
    """Dry-run / plan correctness - nothing is ever written by these tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = _make_archive(self.tmp)
        self.config = load_fha_yaml(self.archive)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, **kw):
        return reorganize.run_reorganize(self.archive, self.config, **kw)

    def _snapshot(self) -> set:
        return {p.relative_to(self.archive).as_posix()
                for p in (self.archive / 'documents').rglob('*') if p.is_file()}

    def test_flat_at_root_is_eligible_and_planned(self) -> None:
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])
        before = self._snapshot()

        result = self._run()

        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['planned'], 1)
        self.assertEqual(result.data['moved'], 0)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn(f'documents/thing_{SID_A}.pdf -> documents/census/thing_{SID_A}.pdf', text)
        self.assertEqual(self._snapshot(), before, 'dry-run must not touch the filesystem')
        self.assertEqual(read_record(self.archive / 'sources' / 'census' / f'thing_{SID_A}.md')
                          ['meta']['files'][0]['file'], f'documents/thing_{SID_A}.pdf',
                          'dry-run must not touch the record either')

    def test_already_in_type_folder_is_eligible(self) -> None:
        """One level down, matching the record's OWN source_type folder, is
        still "where fha process put it" - eligible, not human-organized."""
        _write_file(self.archive, f'documents/census/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/census/thing_{SID_A}.pdf'])

        result = self._run()

        self.assertEqual(result.data['no_op'], 1)
        self.assertEqual(result.data['planned'], 0)
        self.assertEqual(result.data['excluded_human'], 0)

    def test_nested_two_levels_is_excluded_as_human_organized(self) -> None:
        _write_file(self.archive, f'documents/letters/1890s/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'letter',
                       [f'documents/letters/1890s/thing_{SID_A}.pdf'])

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertEqual(result.data['excluded_human'], 1)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('already organized by hand', text)
        self.assertIn(f'documents/letters/1890s/thing_{SID_A}.pdf', text)

    def test_one_level_in_a_differently_named_folder_is_excluded(self) -> None:
        """One level down, but NOT the type folder - a human named it something
        else on purpose, so it is excluded exactly like a deeper nesting."""
        _write_file(self.archive, f'documents/my-own-box/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'letter', [f'documents/my-own-box/thing_{SID_A}.pdf'])

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertEqual(result.data['excluded_human'], 1)

    def test_group_threshold_gives_a_busy_source_its_own_subfolder(self) -> None:
        aliases = [f'documents/thing-{i}_{SID_A}.pdf' for i in range(4)]
        for a in aliases:
            _write_file(self.archive, a)
        _write_record(self.archive, SID_A, 'census', aliases)

        result = self._run(group_threshold=3)

        self.assertEqual(result.data['planned'], 4)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn(f'documents/census/thing_{SID_A}/thing-0_{SID_A}.pdf', text)

    def test_group_threshold_not_exceeded_stays_in_shared_type_folder(self) -> None:
        aliases = [f'documents/thing-{i}_{SID_A}.pdf' for i in range(3)]
        for a in aliases:
            _write_file(self.archive, a)
        _write_record(self.archive, SID_A, 'census', aliases)

        result = self._run(group_threshold=3)

        text = ' '.join(m.text for m in result.messages)
        self.assertIn(f'documents/census/thing-0_{SID_A}.pdf', text)
        self.assertNotIn(f'documents/census/thing_{SID_A}/', text)

    def test_two_records_claiming_the_same_path_refuses_both(self) -> None:
        """Adversarial: pre-existing corruption where two DIFFERENT records'
        files: entries point at the same physical path. Neither is moved,
        and the survey must SAY SO rather than silently pick one."""
        shared = f'documents/thing_{SID_A}.pdf'
        _write_file(self.archive, shared)
        _write_record(self.archive, SID_A, 'census', [shared], slug='thing')
        _write_record(self.archive, SID_B, 'letter', [shared], slug='thing-dup')

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertGreaterEqual(result.data['problems'], 1)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('more than one source record', text)

    def test_destination_collision_refuses_that_file_not_overwrite(self) -> None:
        """Adversarial: something unrelated already sits at the computed
        destination path - refuse that one move, do not silently clobber it
        or merge into it."""
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])
        # An unrelated file already occupying the exact destination path.
        blocker = _write_file(self.archive, f'documents/census/thing_{SID_A}.pdf', b'unrelated')

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertGreaterEqual(result.data['problems'], 1)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('already', text)
        self.assertEqual(blocker.read_bytes(), b'unrelated')

    def test_filename_sid_drift_is_refused_not_moved(self) -> None:
        """Adversarial: the files: entry resolves to a real file, but that
        file's OWN name carries a DIFFERENT (or no) source id - inventory
        drift, not a plain reorganize candidate (mirrors process_refile's
        own identity-drift guard)."""
        # This file's name carries SID_B, but SID_A's record lists it.
        _write_file(self.archive, f'documents/thing_{SID_B}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_B}.pdf'])

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertGreaterEqual(result.data['problems'], 1)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('inventory drift', text)

    def test_missing_on_disk_is_reported_not_moved(self) -> None:
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertGreaterEqual(result.data['problems'], 1)

    def test_missing_fixture_status_entry_is_skipped_silently(self) -> None:
        rec = _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])
        text = rec.read_text(encoding='utf-8').replace(
            '    role: primary\n', '    role: primary\n    status: missing-fixture\n')
        rec.write_text(text, encoding='utf-8')

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertEqual(result.data['problems'], 0)

    def test_empty_archive_reports_nothing_to_do(self) -> None:
        result = self._run()

        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['planned'], 0)
        self.assertIn('Nothing to reorganize', ' '.join(m.text for m in result.messages))

    def test_limit_caps_files_planned_this_run(self) -> None:
        for sid in (SID_A, SID_B, SID_C):
            _write_file(self.archive, f'documents/thing_{sid}.pdf')
            _write_record(self.archive, sid, 'census', [f'documents/thing_{sid}.pdf'], slug='thing')

        result = self._run(limit=1)

        self.assertEqual(result.data['planned'], 1)

    def test_working_copy_is_a_clean_noop(self) -> None:
        (self.archive / 'WORKING_COPY').write_text('', encoding='utf-8')
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        result = self._run()

        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['status'], 'working-copy')
        self.assertEqual(result.data['planned'], 0)

    def test_unreachable_documents_root_warns_and_plans_nothing(self) -> None:
        import shutil
        shutil.rmtree(self.archive / 'documents')

        result = self._run()

        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertIn('not reachable', ' '.join(m.text for m in result.messages))


class ReorganizeApplyTests(unittest.TestCase):
    """Atomic apply, rollback-on-failure, and batch-boundary re-verification."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = _make_archive(self.tmp)
        self.config = load_fha_yaml(self.archive)
        self._orig_move_file = reorganize._move_file
        self._orig_write_text_exact = reorganize.write_text_exact_atomic
        self._orig_run_reconcile = reconcile.run_reconcile

    def tearDown(self) -> None:
        reorganize._move_file = self._orig_move_file
        reorganize.write_text_exact_atomic = self._orig_write_text_exact
        reconcile.run_reconcile = self._orig_run_reconcile
        self._tmp.cleanup()

    def _run(self, **kw):
        kw.setdefault('apply', True)
        kw.setdefault('assume_yes', True)
        return reorganize.run_reorganize(self.archive, self.config, **kw)

    def test_apply_moves_file_and_updates_record_atomically(self) -> None:
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf', b'hello')
        record = _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        result = self._run()

        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['moved'], 1)
        self.assertFalse(asset.exists())
        new_asset = self.archive / 'documents' / 'census' / f'thing_{SID_A}.pdf'
        self.assertTrue(new_asset.exists())
        self.assertEqual(new_asset.read_bytes(), b'hello')
        meta = read_record(record)['meta']
        self.assertEqual(meta['files'][0]['file'], f'documents/census/thing_{SID_A}.pdf')

    def test_apply_is_a_noop_without_apply_flag(self) -> None:
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        result = reorganize.run_reorganize(self.archive, self.config, apply=False)

        self.assertEqual(result.data['moved'], 0)
        self.assertTrue(asset.exists())

    def test_dry_run_overrides_apply_true(self) -> None:
        """The CLI layer's own dry-run-wins rule, exercised at the engine
        boundary too: `apply=True` alone is what the CLI passes only when
        --dry-run was NOT given (see `_cmd_reorganize`)."""
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        rc, out, err = self._cli(['--apply', '--dry-run', '--yes'])

        self.assertTrue(asset.exists())
        self.assertIn('dry-run', out)

    def _cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = reorganize._standalone_main(argv + ['--root', str(self.archive)])
        return rc, out.getvalue(), err.getvalue()

    def test_rollback_on_mid_record_move_failure(self) -> None:
        """A record with two files where the SECOND move fails: the first
        one, already moved, must be moved straight back, and the record's
        text must never be touched - true per-record atomicity."""
        a1 = _write_file(self.archive, f'documents/thing-a_{SID_A}.pdf', b'aaa')
        a2 = _write_file(self.archive, f'documents/thing-b_{SID_A}.pdf', b'bbb')
        record = _write_record(
            self.archive, SID_A, 'census',
            [f'documents/thing-a_{SID_A}.pdf', f'documents/thing-b_{SID_A}.pdf'])
        before_text = record.read_text(encoding='utf-8')

        real_move = reorganize._move_file
        calls = {'n': 0}

        def flaky_move(src, dest):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError('simulated permission denied')
            return real_move(src, dest)

        reorganize._move_file = flaky_move

        result = self._run()

        self.assertEqual(result.data['moved'], 0)
        self.assertEqual(result.data['failed'], 1)
        self.assertTrue(a1.exists(), 'the first file must be moved back')
        self.assertTrue(a2.exists())
        self.assertEqual(record.read_text(encoding='utf-8'), before_text)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('rolled back', text)

    def test_rollback_on_record_write_failure(self) -> None:
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf', b'hello')
        record = _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])
        before_text = record.read_text(encoding='utf-8')

        real_write = reorganize.write_text_exact_atomic
        calls = {'n': 0}

        def flaky_write(path, text):
            calls['n'] += 1
            if calls['n'] == 1:
                raise OSError('simulated disk full')
            return real_write(path, text)

        reorganize.write_text_exact_atomic = flaky_write

        result = self._run()

        self.assertEqual(result.data['moved'], 0)
        self.assertEqual(result.data['failed'], 1)
        self.assertTrue(asset.exists(), 'the file must be moved back')
        self.assertFalse((self.archive / 'documents' / 'census' / f'thing_{SID_A}.pdf').exists())
        self.assertEqual(record.read_text(encoding='utf-8'), before_text)
        self.assertFalse((self.archive / 'documents' / 'census').exists(),
                          'a folder this run created is removed again on full rollback')

    def test_other_records_still_apply_after_one_record_fails(self) -> None:
        """A clean, contained per-record failure must not stop the rest of
        the SAME batch - only a reconcile regression halts forward progress."""
        a1 = _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'], slug='thing-a')
        a2 = _write_file(self.archive, f'documents/thing_{SID_B}.pdf')
        _write_record(self.archive, SID_B, 'letter', [f'documents/thing_{SID_B}.pdf'], slug='thing-b')

        real_move = reorganize._move_file
        def failing_for_a(src, dest):
            if SID_A in src.name:
                raise OSError('simulated failure for A only')
            return real_move(src, dest)
        reorganize._move_file = failing_for_a

        result = self._run()

        self.assertEqual(result.data['moved'], 1)
        self.assertEqual(result.data['failed'], 1)
        self.assertTrue(a1.exists())
        self.assertFalse(a2.exists())

    def test_batch_boundary_halts_when_reconcile_finds_a_new_issue(self) -> None:
        """Two records, batch-size 1, so each gets its own batch. The FIRST
        reconcile call (the baseline, before any batch) reports zero issues;
        the call after batch 1 reports MORE - the run must halt before
        batch 2 even starts, and batch 2's file must be untouched."""
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'], slug='thing-a')
        a2 = _write_file(self.archive, f'documents/thing_{SID_B}.pdf')
        _write_record(self.archive, SID_B, 'letter', [f'documents/thing_{SID_B}.pdf'], slug='thing-b')

        clean = Result(data={})
        dirty = Result(data={})
        dirty.add('warning', 'a brand-new problem this batch caused')
        calls = {'n': 0}

        def fake_reconcile(archive_root, fha_config, **kw):
            calls['n'] += 1
            return clean if calls['n'] == 1 else dirty
        reconcile.run_reconcile = fake_reconcile

        result = self._run(batch_size=1)

        self.assertTrue(result.data['halted'])
        self.assertEqual(result.data['moved'], 1)
        self.assertTrue(a2.exists(), 'batch 2 must never have been attempted')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('halting', text)

    def test_preexisting_reconcile_issue_does_not_halt_or_get_fixed(self) -> None:
        """Adversarial: fha reconcile already has something to complain about
        BEFORE this run starts (pre-existing corruption elsewhere in the
        archive, unrelated to anything reorganize touches). The count must
        stay flat across every call in this fixture - reorganize must not
        treat that as a reason to halt, and must not try to fix it either
        (it never calls anything but reconcile's own --dry-run report)."""
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        stable = Result(data={})
        stable.add('warning', 'some unrelated pre-existing problem')

        def fake_reconcile(archive_root, fha_config, **kw):
            return stable
        reconcile.run_reconcile = fake_reconcile

        result = self._run()

        self.assertFalse(result.data['halted'])
        self.assertEqual(result.data['moved'], 1)

    def test_reconcile_error_before_first_batch_refuses_to_start(self) -> None:
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        broken = Result(data={}, ok=False)
        broken.add('error', 'photos: the photo catalog is unreadable')

        def fake_reconcile(archive_root, fha_config, **kw):
            return broken
        reconcile.run_reconcile = fake_reconcile

        result = self._run()

        self.assertEqual(result.data['moved'], 0)
        self.assertEqual(result.exit_code, EXIT_FAILURE)


class ReorganizeCliTests(unittest.TestCase):
    """CLI-layer confirmation gate and the dry-run-is-default posture."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.archive = _make_archive(self.tmp)
        self._orig_prompt = reorganize._prompt
        self._orig_interactive = reorganize._stdin_is_interactive

    def tearDown(self) -> None:
        reorganize._prompt = self._orig_prompt
        reorganize._stdin_is_interactive = self._orig_interactive
        self._tmp.cleanup()

    def _cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = reorganize._standalone_main(argv + ['--root', str(self.archive)])
        return rc, out.getvalue(), err.getvalue()

    def test_no_flags_previews_only(self) -> None:
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        rc, out, err = self._cli([])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn('dry-run', out)
        self.assertTrue(asset.exists())

    def test_apply_refuses_without_yes_when_noninteractive(self) -> None:
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])
        reorganize._stdin_is_interactive = lambda: False

        rc, out, err = self._cli(['--apply'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('--yes', err)

    def test_apply_prompts_and_honors_no_answer(self) -> None:
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])
        reorganize._stdin_is_interactive = lambda: True
        reorganize._prompt = lambda msg: 'n'

        rc, out, err = self._cli(['--apply'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertTrue(asset.exists())
        self.assertIn('Not reorganized', out)

    def test_apply_with_yes_applies_without_prompting(self) -> None:
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        rc, out, err = self._cli(['--apply', '--yes'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertFalse(asset.exists())
        self.assertTrue((self.archive / 'documents' / 'census' / f'thing_{SID_A}.pdf').exists())


if __name__ == '__main__':
    unittest.main()
