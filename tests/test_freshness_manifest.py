"""
test_freshness_manifest.py - #48: a deletion is invisible to a mtime watermark.

The archive answers "has anything changed since I last looked?" by comparing
timestamps: the newest mtime under sources/people/notes against
`.cache/index.sqlite`'s own mtime (`_lib.newest_record_mtime`). Deleting a
file raises no OTHER file's mtime, so that comparison cannot see a deletion -
the reported bug: file a source twice, delete the duplicate in Finder, and
`fha find`/`fha report` go on confidently serving the stale summary for
weeks, because nothing else in the tree got newer.

The fix (`_lib.py`'s "Freshness manifests" section) tracks the SET of paths a
cache was built from, not just their max, in a small JSON file beside the
cache it describes, and compares a fresh listing against it: a stored path
missing from disk is a deletion, caught regardless of what any remaining
file's mtime says. It is ADDITIVE - every reader ORs the manifest check onto
its existing mtime comparison, never replacing it.

This file proves:
  - the actual reported bug, end to end: build an archive, index it, delete a
    source, show the OLD watermark alone stays blind (a pinned regression
    baseline) and the NEW manifest-backed check catches it, on every reader
    that touches `.cache/index.sqlite` (`open_index_db`, `find`, `doctor`).
  - the "eleven signals disagreeing" bug is fixed: two different entry points
    (`find` and `doctor`) asking about the same state now agree, both before
    and after the deletion.
  - the bootstrapping requirement: a missing or corrupt manifest (an archive
    upgrading from before #48, or a bare `.cache/` wipe) must fail the same
    direction a missing `index.sqlite` already does - "everything changed" -
    never "nothing to compare, assume fresh".
  - the same fix on the photo catalog: a deleted person profile or
    sources/photos record stales `photos.sqlite` too, not just a deleted
    photo file (which a pre-existing directory-mtime trick already caught
    some of the time, unlike person/source records - see `photoindex_status`).
  - the incremental half (`index.upsert_source`) keeps the manifest exactly
    as current as the row it just wrote - the symmetric counterpart to the
    full-rebuild coverage above.

Synthetic tmp archives only - the real archive is never a test bed.
"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import doctor
import find
import index as index_mod
import photoindex
from _lib import (
    index_manifest_path,
    load_fha_yaml,
    manifest_diff,
    manifest_has_changed,
    newest_record_mtime,
    open_index_db,
    person_profile_path_manifest,
    photoindex_manifest_path,
    photoindex_status,
    read_path_manifest,
    record_manifest_is_stale,
)


def _archive_with_two_sources(root: Path) -> tuple[str, str]:
    """A minimal archive with two source records - a genuine duplicate, the
    issue's own story - and one person. Returns (kept_sid, duplicate_sid)."""
    (root / 'fha.yaml').write_text('roots:\n  documents: documents\n', encoding='utf-8')
    (root / 'sources' / 'other').mkdir(parents=True)
    (root / 'people').mkdir(parents=True)
    # Lowercase: the index normalizes every ID to lowercase on write
    # (`_lib.normalize_id`), and the tests below query the `sources` table
    # directly - writing the id already-normalized here avoids a case
    # mismatch turning a real "row found" check into an accidental "not
    # found" pass for the wrong reason.
    kept, dup = 's-aaaaaaaaaa', 's-bbbbbbbbbb'
    for sid, name in ((kept, 'kept'), (dup, 'duplicate')):
        (root / 'sources' / 'other' / f'{name}_{sid}.md').write_text(
            f'---\nid: {sid}\ntitle: Funeral notice\nsource_type: other\n---\n\n'
            '## Claims\n', encoding='utf-8')
    (root / 'people' / 'hartley__margaret_P-aaaaaaaaaa.md').write_text(
        '---\nid: P-aaaaaaaaaa\nname: Margaret Hartley\nliving: false\ntier: stub\n'
        '---\n\n', encoding='utf-8')
    return kept, dup


class ReportedDeletionBugTests(unittest.TestCase):
    """The issue's own story, reproduced end to end."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.kept_sid, self.dup_sid = _archive_with_two_sources(self.root)
        self.fha_config = load_fha_yaml(self.root)
        index_mod.build_index(self.root, self.fha_config)
        self.dup_path = next(
            p for p in (self.root / 'sources' / 'other').glob('*.md')
            if self.dup_sid in p.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_baseline_index_opens_clean_before_anything_changes(self) -> None:
        conn = open_index_db(self.root, ('sources',), strict=True)
        self.assertIsNotNone(conn)
        conn.close()

    def test_old_watermark_alone_stays_blind_to_the_deletion(self) -> None:
        # Regression baseline: this pins the bug the issue reports. Delete
        # the duplicate, and the SINGLE-NUMBER watermark - the whole
        # mechanism before #48 - reads no differently, because removing a
        # file raises no remaining file's mtime. If this assertion ever
        # starts failing, `newest_record_mtime` itself changed shape and the
        # next test's contrast against it needs re-reading, not deleting.
        db_mtime_before = (self.root / '.cache' / 'index.sqlite').stat().st_mtime
        self.dup_path.unlink()
        self.assertLessEqual(newest_record_mtime(self.root), db_mtime_before)

    def test_new_mechanism_detects_the_deletion(self) -> None:
        self.dup_path.unlink()
        self.assertTrue(record_manifest_is_stale(self.root))

    def test_find_reports_the_index_as_not_fresh(self) -> None:
        # fha find's own freshness gate (find._index_is_fresh) - independent
        # of open_index_db, so it needs its own proof.
        self.dup_path.unlink()
        self.assertFalse(find._index_is_fresh(self.root))

    def test_open_index_db_warns_instead_of_silently_serving_stale_rows(self) -> None:
        self.dup_path.unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            conn = open_index_db(self.root, ('sources',), strict=False)
        # A read-only caller still gets an answer (a slightly stale one beats
        # none) - but it must be told, not left to trust a silently wrong one.
        self.assertIsNotNone(conn)
        self.assertIn('stale', err.getvalue())
        conn.close()

    def test_open_index_db_strict_refuses_on_the_deletion(self) -> None:
        # A generating/mutating command (strict=True) must not act on data
        # that lost a row - it refuses outright rather than building on it.
        self.dup_path.unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            conn = open_index_db(self.root, ('sources',), strict=True)
        self.assertIsNone(conn)
        self.assertIn('stale', err.getvalue())

    def test_a_rebuild_clears_the_staleness_and_the_row(self) -> None:
        # The deleted source's ROW must actually be gone too - not merely the
        # freshness flag - or the fix would just quiet the alarm.
        self.dup_path.unlink()
        index_mod.build_index(self.root, self.fha_config)
        self.assertFalse(record_manifest_is_stale(self.root))
        conn = open_index_db(self.root, ('sources',), strict=True)
        self.assertIsNotNone(conn)
        try:
            self.assertIsNone(
                conn.execute('SELECT id FROM sources WHERE id=?', (self.dup_sid,)).fetchone())
            self.assertIsNotNone(
                conn.execute('SELECT id FROM sources WHERE id=?', (self.kept_sid,)).fetchone())
        finally:
            conn.close()


class ElevenSignalsAgreeTests(unittest.TestCase):
    """The eleven-signals-disagreeing bug, directly demonstrated as fixed:
    two different entry points asking about the same archive state - `fha
    find` (via `find._index_is_fresh`) and `fha doctor` (via `doctor.
    run_doctor`) - must agree, both before and after a deletion."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.kept_sid, self.dup_sid = _archive_with_two_sources(self.root)
        self.fha_config = load_fha_yaml(self.root)
        index_mod.build_index(self.root, self.fha_config)
        self.dup_path = next(
            p for p in (self.root / 'sources' / 'other').glob('*.md')
            if self.dup_sid in p.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _doctor_index_status(self) -> str:
        result = doctor.run_doctor(self.root, self.fha_config)
        check = next(c for c in result.data['checks'] if c['id'] == 'index')
        return check['status']

    def test_both_read_fresh_before_the_deletion(self) -> None:
        self.assertTrue(find._index_is_fresh(self.root))
        self.assertEqual(self._doctor_index_status(), 'ok')

    def test_both_read_stale_after_the_deletion(self) -> None:
        self.dup_path.unlink()
        self.assertFalse(find._index_is_fresh(self.root))
        self.assertEqual(self._doctor_index_status(), 'warn')


class BootstrapMissingManifestTests(unittest.TestCase):
    """A missing manifest (an archive that built `index.sqlite` before #48
    shipped, or a bare `.cache/` wipe mid-write) must fail the same
    direction a missing `index.sqlite` already does - 'everything changed',
    never 'nothing to compare, assume fresh' - or the fix would silently
    reintroduce a worse version of the reported bug on the very first run
    after upgrading."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _archive_with_two_sources(self.root)
        self.fha_config = load_fha_yaml(self.root)
        index_mod.build_index(self.root, self.fha_config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_manifest_reads_stale_not_fresh(self) -> None:
        # Nothing on disk changed - only the manifest file itself is gone,
        # simulating an index.sqlite a pre-#48 build left behind.
        index_manifest_path(self.root).unlink()
        self.assertTrue(record_manifest_is_stale(self.root))

    def test_missing_manifest_self_heals_on_rebuild(self) -> None:
        index_manifest_path(self.root).unlink()
        index_mod.build_index(self.root, self.fha_config)
        self.assertTrue(index_manifest_path(self.root).exists())
        self.assertFalse(record_manifest_is_stale(self.root))

    def test_corrupt_manifest_reads_stale_not_fresh(self) -> None:
        index_manifest_path(self.root).write_text('not valid json', encoding='utf-8')
        self.assertTrue(record_manifest_is_stale(self.root))

    def test_wrong_schema_version_reads_stale_not_fresh(self) -> None:
        # write_path_manifest stamps schema_version for the same reason
        # sqlite_cache_schema_status gates every .sqlite cache - a manifest
        # written by a future/incompatible build must fail the same
        # direction as a corrupt one, not be trusted just because its JSON
        # happens to parse and its 'paths' dict happens to look shaped right.
        raw = json.loads(index_manifest_path(self.root).read_text(encoding='utf-8'))
        raw['schema_version'] = raw['schema_version'] + 1
        index_manifest_path(self.root).write_text(json.dumps(raw), encoding='utf-8')
        self.assertTrue(record_manifest_is_stale(self.root))

    def test_missing_manifest_index_absent_case_is_unaffected(self) -> None:
        # A genuinely absent index.sqlite must still take the pre-existing,
        # unrelated "run fha index first" path - the manifest bootstrapping
        # rule is about a PRESENT cache with no manifest, never a reason to
        # change what an absent cache itself reports.
        with tempfile.TemporaryDirectory() as d:
            bare_root = Path(d)
            _archive_with_two_sources(bare_root)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                conn = open_index_db(bare_root, ('sources',))
            self.assertIsNone(conn)
            self.assertIn('index.sqlite not found', err.getvalue())


class PhotoindexDeletionTests(unittest.TestCase):
    """The same #48 fix, on the photo catalog: a deleted person profile or
    sources/photos record must stale `photos.sqlite` too - not just a
    deleted photo file, which a pre-existing directory-mtime trick in
    `photoindex_status`'s own walk already caught some of the time (and
    which #48 explicitly does not rely on alone, since it is not reliable on
    every filesystem/sync tool)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots:\n  photos: photos\n', encoding='utf-8')
        (self.root / 'photos').mkdir()
        (self.root / 'photos' / 'grandpa.jpg').write_bytes(b'x')
        (self.root / 'people').mkdir()
        (self.root / 'sources' / 'photos').mkdir(parents=True)
        self.person_path = self.root / 'people' / 'hartley__john_P-aaaaaaaaaa.md'
        self.person_path.write_text(
            '---\nid: P-aaaaaaaaaa\nname: John Hartley\nliving: false\ntier: stub\n'
            '---\n\n', encoding='utf-8')
        self.source_path = self.root / 'sources' / 'photos' / 'album_S-1111111111.md'
        self.source_path.write_text(
            '---\nid: S-1111111111\ntitle: Album\nsource_type: photograph\n---\n\n',
            encoding='utf-8')
        self.cfg = {'roots': {'photos': 'photos'}}
        self._orig_run_exiftool = photoindex._run_exiftool
        photoindex._run_exiftool = lambda paths: [{'SourceFile': str(p)} for p in paths]
        photoindex.run_scan(self.root, self.cfg)

    def tearDown(self) -> None:
        photoindex._run_exiftool = self._orig_run_exiftool
        self._tmp.cleanup()

    def test_baseline_is_fresh(self) -> None:
        self.assertEqual(photoindex_status(self.root, self.cfg)[0], 'fresh')

    def test_deleting_a_person_profile_stales_the_photo_catalog(self) -> None:
        # newest_person_record_mtime alone cannot see this (a plain file-only
        # walk, no directory-mtime trick) - the manifest is the only thing
        # that can, since photo_people derives weak matches from person
        # face_tags/name_variants and a deleted person must not leave those
        # matches looking current.
        self.person_path.unlink()
        self.assertEqual(photoindex_status(self.root, self.cfg)[0], 'stale')

    def test_deleting_a_sources_photos_record_stales_the_photo_catalog(self) -> None:
        # Same rule for the source-people tier: a deleted sources/photos
        # record must not leave photo_people serving stale person matches.
        self.source_path.unlink()
        self.assertEqual(photoindex_status(self.root, self.cfg)[0], 'stale')

    def test_doctor_reports_the_photoindex_check_stale_too(self) -> None:
        # `_photoindex_freshness` is pure delegation to `photoindex_status`
        # (no duplicated logic) - this is the doctor-level integration proof
        # for that delegation, mirroring ElevenSignalsAgreeTests' proof for
        # the index half: `fha doctor`'s user-facing 'photoindex' check must
        # agree with the direct `photoindex_status` call above, not just in
        # principle but in what a human actually sees running `fha doctor`.
        self.person_path.unlink()
        result = doctor.run_doctor(self.root, self.cfg)
        check = next(c for c in result.data['checks'] if c['id'] == 'photoindex')
        self.assertEqual(check['status'], 'warn')
        self.assertIsNotNone(check['next_step'])

    def test_missing_photoindex_manifest_reads_stale(self) -> None:
        photoindex_manifest_path(self.root).unlink()
        self.assertEqual(photoindex_status(self.root, self.cfg)[0], 'stale')

    def test_a_rescan_clears_the_staleness(self) -> None:
        self.person_path.unlink()
        self.assertEqual(photoindex_status(self.root, self.cfg)[0], 'stale')
        photoindex.run_scan(self.root, self.cfg)
        self.assertEqual(photoindex_status(self.root, self.cfg)[0], 'fresh')


class PhotoindexIndexIsFreshResearchCompanionTests(unittest.TestCase):
    """#106 review finding (P1, found reviewing this PR): `photoindex.
    _index_is_fresh`'s manifest check scoped the STORED side of the
    comparison by a bare 'people/' key prefix, not by the same person-
    PROFILE predicate the CURRENT side uses (`_lib.
    person_profile_path_manifest`). A person's `research` companion is
    human-authored, not generated (`GENERATED_COMPANION_KINDS` excludes it),
    so `record_path_manifest` tracks it under people/ same as a profile -
    the bare prefix filter left it in the stored side with no counterpart in
    a profile-only current listing, reading as a permanent phantom deletion.
    Any archive using the research-file feature at all had weak
    face-tag/name-variant photo resolution silently and permanently
    disabled, even immediately after a clean `fha index`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / 'fha.yaml').write_text('roots:\n  photos: photos\n', encoding='utf-8')
        (self.root / 'people').mkdir()
        self.profile_path = self.root / 'people' / 'hartley__john_P-aaaaaaaaaa.md'
        self.profile_path.write_text(
            '---\nid: P-aaaaaaaaaa\nname: John Hartley\nliving: false\ntier: curated\n'
            '---\n\n', encoding='utf-8')
        # A research companion: human-authored, never GENERATED, so it is
        # tracked by record_path_manifest exactly like the profile is.
        self.research_path = (
            self.root / 'people' / 'hartley__john_research_P-aaaaaaaaaa.md')
        self.research_path.write_text(
            '---\nid: P-aaaaaaaaaa\n---\n\n## Research Notes\n', encoding='utf-8')
        self.fha_config = load_fha_yaml(self.root)
        index_mod.build_index(self.root, self.fha_config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_guard_the_old_bare_prefix_scope_reproduces_the_bug(self) -> None:
        # Pins the pre-fix bug as a regression baseline: a bare 'people/'
        # prefix filter on the STORED manifest (instead of the same
        # profile-only predicate the current side uses) disagrees with a
        # profile-only current listing purely because of the research
        # companion - nothing was ever deleted.
        stored = read_path_manifest(index_manifest_path(self.root))
        old_buggy_stored_people = {k: v for k, v in stored.items() if k.startswith('people/')}
        current_people = person_profile_path_manifest(self.root)
        self.assertTrue(
            manifest_has_changed(manifest_diff(old_buggy_stored_people, current_people)),
            'guard: the old bare-prefix scope must reproduce the bug pre-fix')

    def test_index_is_fresh_right_after_a_clean_build(self) -> None:
        self.assertTrue(photoindex._index_is_fresh(self.root))

    def test_deleting_the_profile_is_still_detected_as_stale(self) -> None:
        self.profile_path.unlink()
        self.assertFalse(photoindex._index_is_fresh(self.root))

    def test_editing_the_research_companion_does_not_go_stale(self) -> None:
        # The research companion voting in record_path_manifest must not
        # make this narrower, profile-only check flap on every edit to it -
        # only a change to the PROFILE itself (or its absence) is in scope.
        self.research_path.write_text(
            '---\nid: P-aaaaaaaaaa\n---\n\n## Research Notes\nUpdated.\n', encoding='utf-8')
        self.assertTrue(photoindex._index_is_fresh(self.root))


class UpsertSourceManifestTests(unittest.TestCase):
    """The incremental half of the index manifest contract - `index.
    upsert_source` must keep `.cache/index_manifest.json` exactly as current
    as the row it just wrote, patching only the one path it touched
    (`_lib.update_path_manifest`) rather than a full re-walk. Symmetric with
    `ReportedDeletionBugTests`, which covers the full-rebuild half
    (`build_index`) - AGENTS_TOOLING's "two-sided rules get two-sided tests":
    a full rebuild and an incremental upsert must leave the manifest equally
    trustworthy, or a stale-after-upsert false alarm (or worse, a silently
    wrong fresh reading) would undo the fix for the one workflow - `fha claim`
    /`review-claims`, `fha serve`'s post-write reindex - that uses upsert
    instead of a full `fha index` for routine, fast, per-source updates."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.kept_sid, self.dup_sid = _archive_with_two_sources(self.root)
        self.fha_config = load_fha_yaml(self.root)
        index_mod.build_index(self.root, self.fha_config)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_upserting_an_untouched_source_does_not_go_stale(self) -> None:
        # The routine case: nothing on disk changed, but a caller (fha claim,
        # fha serve's post-write reindex) upserts one source anyway. Must not
        # leave the archive reading stale over its own no-op refresh.
        status = index_mod.upsert_source(self.root, self.fha_config, self.kept_sid)
        self.assertEqual(status, 'indexed')
        self.assertFalse(record_manifest_is_stale(self.root))

    def test_upsert_after_editing_the_source_stays_fresh(self) -> None:
        kept_path = next(
            p for p in (self.root / 'sources' / 'other').glob('*.md')
            if self.kept_sid in p.name)
        text = kept_path.read_text(encoding='utf-8')
        kept_path.write_text(text + '\n', encoding='utf-8')
        status = index_mod.upsert_source(self.root, self.fha_config, self.kept_sid)
        self.assertEqual(status, 'indexed')
        # The mtime watermark alone would already catch this (the file got
        # newer) - the point here is that the MANIFEST agrees too, so a
        # later reader comparing against it does not see a stale entry for
        # the very path upsert_source just refreshed.
        self.assertFalse(record_manifest_is_stale(self.root))
        manifest = read_path_manifest(index_manifest_path(self.root))
        rel = kept_path.relative_to(self.root).as_posix()
        self.assertAlmostEqual(manifest[rel], kept_path.stat().st_mtime, places=3)

    def test_upsert_still_detects_a_different_deleted_source(self) -> None:
        # Upserting source A must not paper over source B having vanished -
        # the manifest patch is scoped to the one path upsert_source touched,
        # never a blanket "everything is fine now".
        dup_path = next(
            p for p in (self.root / 'sources' / 'other').glob('*.md')
            if self.dup_sid in p.name)
        dup_path.unlink()
        status = index_mod.upsert_source(self.root, self.fha_config, self.kept_sid)
        self.assertEqual(status, 'indexed')
        self.assertTrue(record_manifest_is_stale(self.root))

    def test_upserting_a_brand_new_source_adds_its_manifest_entry(self) -> None:
        # The third upsert_source case the PR description names (add, edit,
        # rename) alongside the two above - a source that did not exist at
        # the last full build, upserted for the first time. The manifest
        # patch must add its key, not just leave the two pre-existing ones
        # alone.
        new_sid = 's-cccccccccc'
        new_path = self.root / 'sources' / 'other' / f'brand-new_{new_sid}.md'
        new_path.write_text(
            f'---\nid: {new_sid}\ntitle: Brand new\nsource_type: other\n---\n\n'
            '## Claims\n', encoding='utf-8')
        rel = new_path.relative_to(self.root).as_posix()
        manifest_before = read_path_manifest(index_manifest_path(self.root))
        self.assertNotIn(rel, manifest_before)

        status = index_mod.upsert_source(self.root, self.fha_config, new_sid)

        self.assertEqual(status, 'indexed')
        self.assertFalse(record_manifest_is_stale(self.root))
        manifest_after = read_path_manifest(index_manifest_path(self.root))
        self.assertIn(rel, manifest_after)
        self.assertAlmostEqual(manifest_after[rel], new_path.stat().st_mtime, places=3)

    def test_upsert_after_a_rename_drops_the_stale_old_key(self) -> None:
        # A rename (same S-id, new filename/folder) must not leave a phantom
        # "deleted" entry in the manifest forever - upsert_source reads the
        # OLD stored path from the sources table before renaming, precisely
        # so it can retire that manifest key along with the DB row.
        old_path = next(
            p for p in (self.root / 'sources' / 'other').glob('*.md')
            if self.kept_sid in p.name)
        old_rel = old_path.relative_to(self.root).as_posix()
        new_path = old_path.parent / f'renamed_{self.kept_sid}.md'
        old_path.rename(new_path)
        status = index_mod.upsert_source(self.root, self.fha_config, self.kept_sid)
        self.assertEqual(status, 'indexed')
        self.assertFalse(record_manifest_is_stale(self.root))
        manifest = read_path_manifest(index_manifest_path(self.root))
        self.assertNotIn(old_rel, manifest)
        self.assertIn(new_path.relative_to(self.root).as_posix(), manifest)


class ManifestDiffPrimitiveTests(unittest.TestCase):
    """`manifest_diff`/`manifest_has_changed` pinned directly against plain
    dicts, no filesystem or index involved - every entry-point test above
    exercises them indirectly through a real archive, but the exact
    'modified path set, same total count' shape (a plain rename with no
    upsert_source involved) is only pinned at this primitive level."""

    def test_pure_addition(self) -> None:
        diff = manifest_diff({'a': 1.0}, {'a': 1.0, 'b': 2.0})
        self.assertEqual(diff, {'added': ['b'], 'removed': [], 'modified': []})
        self.assertTrue(manifest_has_changed(diff))

    def test_pure_removal(self) -> None:
        diff = manifest_diff({'a': 1.0, 'b': 2.0}, {'a': 1.0})
        self.assertEqual(diff, {'added': [], 'removed': ['b'], 'modified': []})
        self.assertTrue(manifest_has_changed(diff))

    def test_pure_modification(self) -> None:
        diff = manifest_diff({'a': 1.0}, {'a': 2.0})
        self.assertEqual(diff, {'added': [], 'removed': [], 'modified': ['a']})
        self.assertTrue(manifest_has_changed(diff))

    def test_no_change_is_not_flagged(self) -> None:
        diff = manifest_diff({'a': 1.0, 'b': 2.0}, {'a': 1.0, 'b': 2.0})
        self.assertEqual(diff, {'added': [], 'removed': [], 'modified': []})
        self.assertFalse(manifest_has_changed(diff))

    def test_a_plain_rename_is_one_removed_and_one_added_same_total_count(self) -> None:
        # The exact shape a rename on disk produces at this primitive level,
        # with no upsert_source or index rebuild involved at all: the old
        # key vanishes, a new key with the SAME mtime appears elsewhere -
        # total key count is unchanged, so a naive len()-based staleness
        # check would miss it. manifest_diff must not.
        stored = {'sources/old-name_S-aaaaaaaaaa.md': 100.0, 'sources/other_S-bbbbbbbbbb.md': 200.0}
        current = {'sources/new-name_S-aaaaaaaaaa.md': 100.0, 'sources/other_S-bbbbbbbbbb.md': 200.0}
        diff = manifest_diff(stored, current)
        self.assertEqual(diff, {
            'added': ['sources/new-name_S-aaaaaaaaaa.md'],
            'removed': ['sources/old-name_S-aaaaaaaaaa.md'],
            'modified': [],
        })
        self.assertEqual(len(stored), len(current))   # the len()-alone trap
        self.assertTrue(manifest_has_changed(diff))

    def test_bootstrapping_reports_every_current_path_as_added(self) -> None:
        diff = manifest_diff(None, {'a': 1.0, 'b': 2.0})
        self.assertEqual(diff, {'added': ['a', 'b'], 'removed': [], 'modified': []})
        self.assertTrue(manifest_has_changed(diff))

    def test_bootstrapping_an_empty_current_listing_is_not_flagged(self) -> None:
        # A missing manifest against an empty CURRENT listing (nothing on
        # disk to report as 'added') must not be a false alarm - this is the
        # exact shape a photoindex scope with no matching records at all
        # produces (PhotoindexIndexIsFreshResearchCompanionTests-adjacent).
        diff = manifest_diff(None, {})
        self.assertEqual(diff, {'added': [], 'removed': [], 'modified': []})
        self.assertFalse(manifest_has_changed(diff))


if __name__ == '__main__':
    unittest.main()
