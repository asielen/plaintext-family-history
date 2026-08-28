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
                   *, slug: str = 'thing', record_dir: str | None = None,
                   copies: list[str | None] | None = None) -> Path:
    """A minimal but complete source record naming the given documents-root
    aliases. `copies` (one per alias, or None) sets each `files:` entry's
    `copy:` field, which the reorganize eligibility check (#188 P1 audit:
    hand-renamed exclusion) uses to derive that entry's own machine-shaped
    basename - `{slug}-{copy}_{sid}.ext` - so a fixture wanting more than
    one ELIGIBLE file per record must give each one a distinct `copy` that
    matches its alias's own basename suffix (a real multi-file record never
    has two files both wearing the bare `{slug}_{sid}` primary name)."""
    subdir = record_dir if record_dir is not None else reorganize._record_subdir(source_type)
    rec_dir = archive / 'sources' / subdir
    rec_dir.mkdir(parents=True, exist_ok=True)
    copies = copies if copies is not None else [None] * len(aliases)
    lines = ''
    for a, c in zip(aliases, copies):
        lines += f'  - file: {a}\n    role: primary\n'
        if c:
            lines += f'    copy: {c}\n'
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
        # Each file needs its OWN machine-shaped basename (#188 P1 audit: a
        # `copy:` value distinguishes them) - four files all wearing the
        # bare `thing_{sid}` primary name never happens for one real
        # record, and the eligibility check would (rightly) treat any that
        # don't match as hand-renamed.
        copies = [f'p{i}' for i in range(4)]
        aliases = [f'documents/thing-{c}_{SID_A}.pdf' for c in copies]
        for a in aliases:
            _write_file(self.archive, a)
        _write_record(self.archive, SID_A, 'census', aliases, copies=copies)

        result = self._run(group_threshold=3)

        self.assertEqual(result.data['planned'], 4)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn(f'documents/census/thing_{SID_A}/thing-{copies[0]}_{SID_A}.pdf', text)

    def test_group_threshold_not_exceeded_stays_in_shared_type_folder(self) -> None:
        copies = [f'p{i}' for i in range(3)]
        aliases = [f'documents/thing-{c}_{SID_A}.pdf' for c in copies]
        for a in aliases:
            _write_file(self.archive, a)
        _write_record(self.archive, SID_A, 'census', aliases, copies=copies)

        result = self._run(group_threshold=3)

        text = ' '.join(m.text for m in result.messages)
        self.assertIn(f'documents/census/thing-{copies[0]}_{SID_A}.pdf', text)
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

    def test_missing_sources_dir_refuses_not_false_success(self) -> None:
        """P2 audit finding (PR #188): a MISSING sources/ folder used to
        fall all the way through to "nothing to reorganize - already
        tidy" (exit 0), because `walk_files`/`_iter_source_records` both
        silently yield nothing for a root that will not even list. Must
        refuse plainly instead, mirroring the documents-root check above."""
        import shutil
        shutil.rmtree(self.archive / 'sources')

        result = self._run()

        self.assertNotEqual(result.exit_code, EXIT_CLEAN)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('sources', text.lower())
        self.assertNotIn('Nothing to reorganize', text)

    def test_unparseable_record_refuses_the_whole_survey(self) -> None:
        """P1 audit finding (PR #188, third pass): a record with malformed
        frontmatter/Claims YAML used to be warned about and SKIPPED,
        letting the survey continue planning moves for every OTHER
        record - but that means the malformed record's OWN files:
        ownership claims never enter `canonical_owners`, so the duplicate-
        ownership guard is working from an incomplete picture. Fix: fail
        the WHOLE survey closed - a perfectly clean, unrelated record's
        move must not be planned either while ANY record in the archive
        cannot be parsed."""
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])
        # A second, unrelated record with malformed frontmatter (an
        # unterminated quote breaks the YAML block) - same shape
        # `reconcile.py`'s own malformed-record tests use.
        rec_b = _write_record(self.archive, SID_B, 'letter',
                               [f'documents/other_{SID_B}.pdf'], slug='other')
        text_b = rec_b.read_text(encoding='utf-8').replace(
            f'title: Title for {SID_B}', 'title: "unterminated')
        rec_b.write_text(text_b, encoding='utf-8')

        result = self._run()

        # Fail closed: NOTHING is planned, including for the clean record.
        self.assertEqual(result.data['planned'], 0)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn(rec_b.name, text)
        self.assertNotIn(f'documents/thing_{SID_A}.pdf ->', text,
                          "the clean record's move must not sneak through either")

    def test_unparseable_record_blocks_a_different_records_move_of_a_shared_file(self) -> None:
        """The literal failure mode the audit finding names: a malformed
        record and a DIFFERENT, parseable record both (in reality) list
        the SAME physical file. Before this fix, skipping the malformed
        record silently dropped its ownership claim from
        `canonical_owners`, so the parseable record's move went ahead as
        if there were no conflict at all - leaving the malformed record's
        own `files:` entry pointing at a file that had just moved out from
        under it. Now: fail closed, the shared file is never touched."""
        shared = f'documents/thing_{SID_A}.pdf'
        asset = _write_file(self.archive, shared)
        _write_record(self.archive, SID_A, 'census', [shared], slug='thing')
        rec_b = _write_record(self.archive, SID_B, 'letter', [shared], slug='thing-dup')
        text_b = rec_b.read_text(encoding='utf-8').replace(
            f'title: Title for {SID_B}', 'title: "unterminated')
        rec_b.write_text(text_b, encoding='utf-8')

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertTrue(asset.exists(), 'the shared file must never have been moved')

    def test_invalid_source_type_refuses_the_whole_record(self) -> None:
        """P1 audit finding (PR #188): a hand-edited record's source_type
        must be validated against `_lib.SOURCE_TYPES` before it is trusted
        as a path component - refuse planning ANY move for the record
        rather than building a destination from an unknown value."""
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        rec = _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])
        text = rec.read_text(encoding='utf-8').replace(
            'source_type: census', 'source_type: not-a-real-type')
        rec.write_text(text, encoding='utf-8')

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertGreaterEqual(result.data['problems'], 1)
        msg = ' '.join(m.text for m in result.messages)
        self.assertIn('not-a-real-type', msg)

    def test_malicious_source_type_cannot_escape_documents_root(self) -> None:
        """Adversarial (P1 audit finding, PR #188): a source_type crafted
        to read as a path-traversal segment must never compute a
        destination outside the documents root - the file must stay
        exactly where it was, nothing outside documents/ must appear."""
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        rec = _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])
        text = rec.read_text(encoding='utf-8').replace(
            'source_type: census', 'source_type: ../../escaped')
        rec.write_text(text, encoding='utf-8')

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertGreaterEqual(result.data['problems'], 1)
        self.assertTrue(asset.exists())
        self.assertFalse((self.archive.parent / 'escaped').exists())

    def test_frontmatter_id_filename_mismatch_refuses_the_record(self) -> None:
        """P1 audit finding (PR #188): the S-id used to plan a record's
        moves must not be trusted from the record's FILENAME alone - a
        record whose frontmatter `id:` disagrees (a real, lint-flagged
        E003 drift) must be refused, never silently treated as whichever
        identity the filename happens to imply (mirrors `site.py`'s
        `_origin_frontmatter_id_mismatches`, #117 audit)."""
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        rec = _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])
        text = rec.read_text(encoding='utf-8').replace(f'id: {SID_A}', f'id: {SID_B}')
        rec.write_text(text, encoding='utf-8')

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertGreaterEqual(result.data['problems'], 1)
        msg = ' '.join(m.text for m in result.messages)
        self.assertIn('E003', msg)

    def test_hand_renamed_file_is_excluded_even_with_valid_shape(self) -> None:
        """P1 audit finding (PR #188): a human can rename a file IN PLACE,
        keeping its `_S-id` suffix and its eligible folder depth - path
        SHAPE alone is not enough to call it "still sitting exactly where
        a machine put it"; the actual basename must match what `fha
        process` would have produced."""
        _write_file(self.archive, f'documents/my-own-name_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census',
                       [f'documents/my-own-name_{SID_A}.pdf'], slug='thing')

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertEqual(result.data['excluded_human'], 1)

    def test_bundle_attachment_original_filename_still_makes_it_eligible(self) -> None:
        """A bundle-dissolved attachment is machine-named off ITS OWN
        original filename (`process_bundle`'s per-asset `base =
        _slugify(asset.stem)`, stored back as `original_filename` since
        #59), not the record's shared slug - the eligibility check must
        accept THAT convention too, not just the lone-file one, or a real
        archived bundle attachment would be wrongly excluded as
        "hand-renamed" (P1 audit finding, PR #188)."""
        _write_file(self.archive, f'documents/scan0007_{SID_A}.pdf')
        rec = _write_record(self.archive, SID_A, 'census',
                             [f'documents/scan0007_{SID_A}.pdf'], slug='wedding-portrait')
        text = rec.read_text(encoding='utf-8').replace(
            '    role: primary\n', '    role: primary\n    original_filename: scan0007.pdf\n')
        rec.write_text(text, encoding='utf-8')

        result = self._run()

        self.assertEqual(result.data['planned'], 1)
        self.assertEqual(result.data['excluded_human'], 0)

    def test_two_aliases_resolving_to_the_same_file_in_one_record_refuses_both(self) -> None:
        """Adversarial (P1 audit finding, PR #188 second pass): the SAME
        record lists the SAME physical file twice under two lexically
        different aliases (a redundant './' segment) - canonicalization
        must catch this as duplicate ownership too, not just the
        cross-record case above, or applying the plan would move the
        shared file for one alias while leaving the other pointing at a
        now-missing original."""
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census',
                       [f'documents/thing_{SID_A}.pdf', f'documents/./thing_{SID_A}.pdf'])

        result = self._run()

        self.assertEqual(result.data['planned'], 0)
        self.assertGreaterEqual(result.data['problems'], 1)

    def test_limit_skips_an_oversized_record_but_still_plans_a_smaller_one(self) -> None:
        """P2 audit finding (PR #188, third pass): the trimming loop used to
        `break` the instant ONE record did not fit under --limit, stopping
        the scan entirely instead of skipping that record and checking
        whether a LATER, smaller record would fit. Concrete failure this
        reproduces: a three-file record first, a one-file record second,
        --limit 1 - the old code broke on the first (3 > 1) and planned
        NOTHING, even though the second record (1 file) fits the cap
        exactly. The whole record is still skipped (never split) - only
        the "stop scanning entirely" part was the bug."""
        aliases = [f'documents/thing-{c}_{SID_A}.pdf' for c in ('a', 'b', 'c')]
        for a in aliases:
            _write_file(self.archive, a)
        _write_record(self.archive, SID_A, 'census', aliases, copies=['a', 'b', 'c'])
        _write_file(self.archive, f'documents/other_{SID_B}.pdf')
        _write_record(self.archive, SID_B, 'letter', [f'documents/other_{SID_B}.pdf'], slug='other')

        result = self._run(limit=1)

        # The 3-file record (first in record order, 'census' < 'letters')
        # is skipped whole - it would cross the cap on its own - but the
        # scan continues past it and the 1-file record that follows still
        # fits and IS planned.
        self.assertEqual(result.data['planned'], 1)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn(f'documents/other_{SID_B}.pdf', text)
        self.assertNotIn(f'documents/thing-a_{SID_A}.pdf', text)
        self.assertIn('--limit 1', text)

    def test_limit_too_small_for_every_record_plans_nothing(self) -> None:
        """The genuinely-nothing-fits case must still come back empty and
        say so by name - continuing past an oversized record must not
        paper over a --limit that is too small for every remaining record
        too (distinct from the case above, where a LATER record fits)."""
        aliases = [f'documents/thing-{c}_{SID_A}.pdf' for c in ('a', 'b', 'c')]
        for a in aliases:
            _write_file(self.archive, a)
        _write_record(self.archive, SID_A, 'census', aliases, copies=['a', 'b', 'c'])
        other_aliases = [f'documents/other-{c}_{SID_B}.pdf' for c in ('a', 'b')]
        for a in other_aliases:
            _write_file(self.archive, a)
        _write_record(self.archive, SID_B, 'letter', other_aliases, slug='other', copies=['a', 'b'])

        result = self._run(limit=1)

        self.assertEqual(result.data['planned'], 0)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('smaller than the smallest remaining', text)

    def test_limit_includes_multiple_records_when_they_fit_together(self) -> None:
        """The hard-cap fix must not become OVER-conservative: several
        whole records that together still fit under --limit are all
        planned, not just the first one."""
        aliases = [f'documents/thing-{c}_{SID_A}.pdf' for c in ('a', 'b')]
        for a in aliases:
            _write_file(self.archive, a)
        _write_record(self.archive, SID_A, 'census', aliases, copies=['a', 'b'])
        _write_file(self.archive, f'documents/other_{SID_B}.pdf')
        _write_record(self.archive, SID_B, 'letter', [f'documents/other_{SID_B}.pdf'], slug='other')

        result = self._run(limit=3)

        self.assertEqual(result.data['planned'], 3)


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
        # P2 audit finding (PR #188, third pass): `Result.changed` is this
        # codebase's established "what did this operation create/write/
        # rename/embed into" contract (see e.g. `confirm.py`'s dismiss-pair
        # rename, `source.py extract`'s asset+record pair) - a headless
        # caller reading `as_dict()` must be able to see the MOVED DOCUMENT
        # itself, not just the rewritten record.
        changed = [str(p) for p in result.changed]
        self.assertIn(str(record), changed)
        self.assertIn(str(new_asset), changed)

    def test_apply_reports_every_moved_document_in_result_changed(self) -> None:
        """The multi-file case: every document a record moves must show up
        in `Result.changed`, not just the record it belongs to."""
        a1 = _write_file(self.archive, f'documents/thing_{SID_A}.pdf', b'aaa')
        a2 = _write_file(self.archive, f'documents/thing-b_{SID_A}.pdf', b'bbb')
        record = _write_record(
            self.archive, SID_A, 'census',
            [f'documents/thing_{SID_A}.pdf', f'documents/thing-b_{SID_A}.pdf'],
            copies=[None, 'b'])

        result = self._run()

        self.assertEqual(result.data['moved'], 2)
        new_a1 = self.archive / 'documents' / 'census' / f'thing_{SID_A}.pdf'
        new_a2 = self.archive / 'documents' / 'census' / f'thing-b_{SID_A}.pdf'
        self.assertTrue(new_a1.exists())
        self.assertTrue(new_a2.exists())
        changed = [str(p) for p in result.changed]
        self.assertIn(str(record), changed)
        self.assertIn(str(new_a1), changed)
        self.assertIn(str(new_a2), changed)

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

    def test_apply_one_record_refuses_when_destination_appeared_after_planning(self) -> None:
        """TOCTOU guard (P1 audit finding, PR #188): `_apply_one_record`
        must re-check the destination is still free immediately before
        each move, not just trust `_plan`'s one-time check - real time
        passes (the [y/N] prompt, earlier batches, `fha reconcile` shelling
        out between them) before a batch actually runs, and something else
        can create a file at the planned destination in that window. This
        must be refused - never silently clobbered by `Path.rename`."""
        src = _write_file(self.archive, f'documents/thing_{SID_A}.pdf', b'original')
        record = _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])
        dest = self.archive / 'documents' / 'census' / f'thing_{SID_A}.pdf'
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b'unrelated, appeared after planning')
        before_text = record.read_text(encoding='utf-8')

        pairs = [(f'documents/thing_{SID_A}.pdf', f'documents/census/thing_{SID_A}.pdf', src, dest)]
        ok, msg, unrecoverable = reorganize._apply_one_record(record, pairs)

        self.assertFalse(ok)
        self.assertFalse(unrecoverable)
        self.assertIn('rolled back', msg)
        self.assertTrue(src.exists())
        self.assertEqual(src.read_bytes(), b'original')
        self.assertEqual(dest.read_bytes(), b'unrelated, appeared after planning')
        self.assertEqual(record.read_text(encoding='utf-8'), before_text)

    def test_unrecoverable_rollback_failure_halts_the_whole_run(self) -> None:
        """P1 audit finding (PR #188): an UNRECOVERABLE rollback (the
        move-back itself fails too, leaving a record's files/text
        genuinely inconsistent) must stop the ENTIRE run right there - not
        be folded into an ordinary clean failure that lets the run keep
        changing OTHER records. Exit code 3 (the documented "left
        inconsistent" contract) must actually be reachable."""
        a1 = _write_file(self.archive, f'documents/thing-a_{SID_A}.pdf', b'aaa')
        a2 = _write_file(self.archive, f'documents/thing-a-b_{SID_A}.pdf', b'bbb')
        _write_record(
            self.archive, SID_A, 'census',
            [f'documents/thing-a_{SID_A}.pdf', f'documents/thing-a-b_{SID_A}.pdf'],
            slug='thing-a', copies=[None, 'b'])
        b1 = _write_file(self.archive, f'documents/thing-b_{SID_B}.pdf')
        _write_record(self.archive, SID_B, 'letter', [f'documents/thing-b_{SID_B}.pdf'], slug='thing-b')

        real_move = reorganize._move_file
        calls = {'n': 0}

        def flaky_move(src, dest):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError('simulated failure moving the second file')
            if calls['n'] == 3:
                raise OSError('simulated failure UNDOING the first move too')
            return real_move(src, dest)

        reorganize._move_file = flaky_move

        result = self._run()

        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertTrue(result.data['halted'])
        # The undo itself failed, so a1 never made it home - it is
        # stranded at its half-moved destination instead. This is exactly
        # the "genuinely inconsistent" state the whole run must halt for.
        self.assertFalse(a1.exists())
        stranded = self.archive / 'documents' / 'census' / f'thing-a_{SID_A}.pdf'
        self.assertTrue(stranded.exists())
        self.assertTrue(b1.exists(), 'the second record must never have been attempted')
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('INCONSISTENT', text)
        self.assertIn('fha doctor', text)
        self.assertIn('fha lint after reorganizing', text,
                       'the promised final lint pass must still run after a halt')

    def test_reconcile_crash_after_batch_halts_and_preserves_moved_count(self) -> None:
        """P2 audit finding (PR #188, second pass): if the post-batch fha
        reconcile call itself RAISES (not just reports warnings/errors
        through its normal Result - a real crash, e.g. a SQLite error
        reading the photo catalog), the exception must not escape uncaught
        and lose the report of what already moved before it.

        Exit code 1, not 3 (P2 audit finding, PR #188 third pass): TOOLING
        §9a and tools/README.md both reserve exit 3 for a record left
        genuinely inconsistent after a failed rollback, or for `fha
        reconcile` failing to run BEFORE the first batch. This crash
        happens AFTER batch 1 completed cleanly (each of its moves was its
        own verified atomic transaction) - the archive is self-consistent,
        verification just could not finish - so it is the documented exit-1
        "mid-run halt, archive left consistent either way" case."""
        _write_file(self.archive, f'documents/thing-a_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing-a_{SID_A}.pdf'], slug='thing-a')
        a2 = _write_file(self.archive, f'documents/thing-b_{SID_B}.pdf')
        _write_record(self.archive, SID_B, 'letter', [f'documents/thing-b_{SID_B}.pdf'], slug='thing-b')

        clean = Result(data={})
        calls = {'n': 0}

        def crashing_reconcile(archive_root, fha_config, **kw):
            calls['n'] += 1
            if calls['n'] == 1:
                return clean
            raise RuntimeError('simulated sqlite error reading the photo catalog')
        reconcile.run_reconcile = crashing_reconcile

        result = self._run(batch_size=1)

        self.assertTrue(result.data['halted'])
        self.assertEqual(result.data['moved'], 1)
        self.assertTrue(a2.exists(), 'batch 2 must never have been attempted')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('crashed', text)
        self.assertIn('fha lint after reorganizing', text,
                       'the promised final lint pass must still run after a halt')

    def test_reconcile_error_after_batch_halts_at_exit_one_not_three(self) -> None:
        """P2 audit finding (PR #188, third pass): distinct from the crash
        case above - here the post-batch `fha reconcile` call returns
        CLEANLY but its own Result carries an error-level message (e.g. an
        unreadable photo catalog it detected, not an exception). This used
        to add an 'error'-level message here too, pushing `_finalize` to
        exit 3 - the same documented-contract violation as the crash case,
        fixed the same way: the batch that already ran is safe, so this is
        an exit-1 halt, not an exit-3 unrecoverable state."""
        _write_file(self.archive, f'documents/thing-a_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing-a_{SID_A}.pdf'], slug='thing-a')
        a2 = _write_file(self.archive, f'documents/thing-b_{SID_B}.pdf')
        _write_record(self.archive, SID_B, 'letter', [f'documents/thing-b_{SID_B}.pdf'], slug='thing-b')

        clean = Result(data={})
        broken = Result(data={}, ok=False)
        broken.add('error', 'photos: the photo catalog is unreadable')
        calls = {'n': 0}

        def flaky_reconcile(archive_root, fha_config, **kw):
            calls['n'] += 1
            return clean if calls['n'] == 1 else broken
        reconcile.run_reconcile = flaky_reconcile

        result = self._run(batch_size=1)

        self.assertTrue(result.data['halted'])
        self.assertEqual(result.data['moved'], 1)
        self.assertTrue(a2.exists(), 'batch 2 must never have been attempted')
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('could not run cleanly after batch', text)
        self.assertIn('fha lint after reorganizing', text,
                       'the promised final lint pass must still run after a halt')

    def test_reconcile_baseline_crash_refuses_to_start(self) -> None:
        """Symmetric with the post-batch case above: a baseline `fha
        reconcile` crash (before any batch runs) must also be caught
        cleanly rather than escape as a raw traceback."""
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        def crashing_reconcile(archive_root, fha_config, **kw):
            raise RuntimeError('simulated crash establishing baseline')
        reconcile.run_reconcile = crashing_reconcile

        result = self._run()

        self.assertEqual(result.data['moved'], 0)
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('crashed', text)

    def test_rollback_on_mid_record_move_failure(self) -> None:
        """A record with two files where the SECOND move fails: the first
        one, already moved, must be moved straight back, and the record's
        text must never be touched - true per-record atomicity."""
        a1 = _write_file(self.archive, f'documents/thing_{SID_A}.pdf', b'aaa')
        a2 = _write_file(self.archive, f'documents/thing-b_{SID_A}.pdf', b'bbb')
        record = _write_record(
            self.archive, SID_A, 'census',
            [f'documents/thing_{SID_A}.pdf', f'documents/thing-b_{SID_A}.pdf'],
            copies=[None, 'b'])
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
        a1 = _write_file(self.archive, f'documents/thing-a_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing-a_{SID_A}.pdf'], slug='thing-a')
        a2 = _write_file(self.archive, f'documents/thing-b_{SID_B}.pdf')
        _write_record(self.archive, SID_B, 'letter', [f'documents/thing-b_{SID_B}.pdf'], slug='thing-b')

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
        # P2 audit finding (PR #188): the summary must count only records
        # that actually SUCCEEDED - `len(record_order)` used to include the
        # failed-and-rolled-back one too, overstating what happened.
        self.assertEqual(result.data['moved_records'], 1)
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('Moved 1 file(s) across 1 record(s)', text)

    def test_batch_boundary_halts_when_reconcile_finds_a_new_issue(self) -> None:
        """Two records, batch-size 1, so each gets its own batch. The FIRST
        reconcile call (the baseline, before any batch) reports zero issues;
        the call after batch 1 reports MORE - the run must halt before
        batch 2 even starts, and batch 2's file must be untouched."""
        _write_file(self.archive, f'documents/thing-a_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing-a_{SID_A}.pdf'], slug='thing-a')
        a2 = _write_file(self.archive, f'documents/thing-b_{SID_B}.pdf')
        _write_record(self.archive, SID_B, 'letter', [f'documents/thing-b_{SID_B}.pdf'], slug='thing-b')

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
        # P2 audit finding (PR #188): TOOLING §9a promises a final fha lint
        # pass runs after EITHER completion OR a halt - this halt branch
        # used to return before ever reaching it.
        self.assertIn('fha lint after reorganizing', text)

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
        # P2 audit finding (PR #188): the refusal used to point at reconcile's
        # "own messages" without ever rendering any of them, and named no
        # concrete next command - both must be present now.
        text = ' '.join(m.text for m in result.messages)
        self.assertIn('the photo catalog is unreadable', text)
        self.assertIn('fha reconcile', text)


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

    def test_negative_limit_is_rejected_at_the_cli(self) -> None:
        """P2 audit finding (PR #188, second pass): a mistyped `--limit -1`
        used to exit cleanly and claim "every eligible document is already
        tidy" - the trimming loop's own boundary check made a negative
        limit look like zero files fit. Reject it plainly instead."""
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        rc, out, err = self._cli(['--limit', '-1'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('--limit', err)
        self.assertNotIn('already tidy', out)
        self.assertTrue(asset.exists())

    def test_batch_size_zero_is_rejected_not_silently_defaulted(self) -> None:
        """P2 audit finding (PR #188, third pass): `--batch-size 0` used to
        be silently rewritten to the default (25) rather than validated -
        a human who typed 0 got a completely different batch size with no
        warning. Reject it plainly at the CLI boundary instead."""
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        rc, out, err = self._cli(['--batch-size', '0'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('--batch-size', err)
        self.assertTrue(asset.exists())

    def test_batch_size_negative_is_rejected_not_silently_clamped(self) -> None:
        """Same finding as above: a negative --batch-size used to be
        silently clamped to 1 rather than refused."""
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        rc, out, err = self._cli(['--batch-size', '-5'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('--batch-size', err)
        self.assertTrue(asset.exists())

    def test_group_threshold_negative_is_rejected(self) -> None:
        """P2 audit finding (PR #188, third pass): a negative
        --group-threshold has no coherent meaning of its own (it would
        behave identically to 0) - reject it rather than silently
        substituting the default."""
        asset = _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        rc, out, err = self._cli(['--group-threshold', '-1'])

        self.assertEqual(rc, EXIT_FAILURE)
        self.assertIn('--group-threshold', err)
        self.assertTrue(asset.exists())

    def test_group_threshold_zero_is_honored_as_always_subfolder(self) -> None:
        """P2 audit finding (PR #188, third pass): `--group-threshold 0`
        used to be silently rewritten to the default (3), even though 0
        has its own coherent, DIFFERENT meaning - every non-empty source
        group exceeds the threshold, i.e. always give a source its own
        subfolder, never leave a couple of files loose in the shared type
        folder. A record with exactly ONE eligible file would normally
        stay in the shared type folder (1 is not > the default 3 - see
        `test_group_threshold_not_exceeded_stays_in_shared_type_folder`);
        with --group-threshold 0 it must get its own subfolder instead,
        proving 0 was actually honored rather than silently replaced."""
        _write_file(self.archive, f'documents/thing_{SID_A}.pdf')
        _write_record(self.archive, SID_A, 'census', [f'documents/thing_{SID_A}.pdf'])

        rc, out, err = self._cli(['--group-threshold', '0'])

        self.assertEqual(rc, EXIT_CLEAN)
        self.assertIn(f'documents/census/thing_{SID_A}/thing_{SID_A}.pdf', out)


if __name__ == '__main__':
    unittest.main()
