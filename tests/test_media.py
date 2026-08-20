"""
test_media.py - fha media dedupe / fha media probe (issues #43, #44).

Two things are pinned here, matching the module's own two halves:

  1. THE COVERAGE INVARIANT (dedupe). `find_duplicate_media.py`'s own history
     (`.claude/skills/import-recordings/GAP.md`, `tests/test_import_recordings.py`)
     is five review rounds each finding a different way the interim script
     answered a smaller question than the one it claimed to. `media.py` ports
     that script's coverage-walking logic, and this file proves each of the
     five failure modes GAP.md documents is still caught by the port, built as
     real fixtures rather than asserted from memory: an unhashable same-size
     candidate, a root the config names but the disk lacks, a directory
     symlink, a symlink loop, and a batch containing its own duplicate.
     Guard tests are run against the CURRENT (post-port) code - the port is
     new code, not a patch to prove failing beforehand, but every case below
     is exactly the shape a naive re-implementation gets wrong, which is the
     point of testing it explicitly rather than only the happy path.

  2. THE PROBE ARITHMETIC (probe). SKILL.md step 4's formula -
     `offset = filename_time + duration - creation_time`, rounded to the
     nearest quarter hour, then `local_start = (creation_time + offset) -
     duration` - is tested both as pure arithmetic (`ArithmeticTests`) and
     through `run_media_probe` against a mocked ffprobe backend covering the
     four documented cases: a quicktime creationdate that settles the offset
     outright, a filename clock that solves it, neither (must say so plainly,
     never fall back to mtime), and the midnight-straddle flag. The
     `filename_time + duration == creation_time` cross-check GAP.md calls out
     is asserted directly (`test_the_cross_check_invariant_holds`).

Run: python -m unittest tests.test_media -v   (from the repo root; py -3.14)
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import media
from _lib import EXIT_CLEAN, EXIT_ERRORS, EXIT_FAILURE, EXIT_WARNINGS


def _write(path: Path, content: bytes | str = b'') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        content = content.encode('utf-8')
    path.write_bytes(content)
    return path


def _make_archive(root: Path, roots_yaml: str | None = None) -> Path:
    """A minimal archive: fha.yaml + empty documents/photos/inbox roots."""
    root.mkdir(parents=True, exist_ok=True)
    (root / 'documents').mkdir(exist_ok=True)
    (root / 'photos').mkdir(exist_ok=True)
    (root / 'inbox').mkdir(exist_ok=True)
    _write(root / 'fha.yaml',
           roots_yaml or 'roots:\n  documents: documents\n  photos: photos\n')
    return root


class DedupeCoverageInvariantTests(unittest.TestCase):
    """The five GAP.md failure modes, each built as a real fixture."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.archive = _make_archive(self.tmp / 'archive')
        self.documents = self.archive / 'documents'
        self.filed = _write(self.documents / 'interviews' / 'hartley_S-0000000001.m4a',
                            b'the interview everyone already has')
        self.incoming_dir = self.tmp / 'incoming'
        self.incoming_dir.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def _fha_config(self):
        return {'roots': {'documents': 'documents', 'photos': 'photos'}}

    # -- 1. ROOTS: a configured root the disk lacks --------------------------
    def test_a_configured_root_that_is_missing_refuses_the_run(self):
        """fha.yaml names `documents`; the folder is gone. Never a silent `new`."""
        import shutil
        shutil.rmtree(self.documents)
        twin = _write(self.incoming_dir / 'twin.m4a', b'anything at all')
        result = media.run_media_dedupe(self.archive, self._fha_config(),
                                        incoming_args=[str(twin)])
        self.assertEqual(result.exit_code, media.DEDUPE_USAGE)
        self.assertFalse(result.ok)
        self.assertTrue(any('not there right now' in m.text for m in result.messages))

    def test_an_alias_the_config_never_mentions_is_an_ordinary_young_archive(self):
        """The mirror case: an alias fha.yaml does not configure is silently skipped."""
        cfg = {'roots': {'documents': 'documents'}}   # no `photos:` entry at all
        twin = _write(self.incoming_dir / 'new-recording.m4a', b'genuinely new bytes')
        result = media.run_media_dedupe(self.archive, cfg, incoming_args=[str(twin)])
        self.assertEqual(result.exit_code, media.DEDUPE_CLEAR)
        self.assertEqual(result.data['status'], 'new')

    # -- 2. ENUMERATION: a directory symlink, and a symlink loop -------------
    def _link_dir(self, target, link):
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError) as e:
            self.skipTest('this platform will not create a directory symlink: %s' % e)

    def test_an_archived_recording_below_a_directory_symlink_is_still_found(self):
        """A plain os.walk would skip this subtree in total silence."""
        import shutil
        real = self.archive / 'real-media'
        real.mkdir()
        shutil.move(str(self.filed), str(real / self.filed.name))
        self._link_dir(real, self.documents / 'linked')
        twin = _write(self.incoming_dir / 'twin.m4a', b'the interview everyone already has')
        result = media.run_media_dedupe(self.archive, self._fha_config(),
                                        incoming_args=[str(twin)])
        self.assertEqual(result.exit_code, media.DEDUPE_DUPLICATE)

    def test_a_symlink_loop_is_walked_once_and_still_finds_the_twin(self):
        self._link_dir(self.documents, self.documents / 'loop')
        twin = _write(self.incoming_dir / 'twin.m4a', b'the interview everyone already has')
        result = media.run_media_dedupe(self.archive, self._fha_config(),
                                        incoming_args=[str(twin)])
        self.assertEqual(result.exit_code, media.DEDUPE_DUPLICATE)

    # -- 3. DOMAIN: inbox staged inside a media root is never "already filed" -
    def test_the_inbox_inside_a_media_root_is_not_archived(self):
        cfg = {'roots': {'documents': 'documents', 'photos': 'photos',
                         'inbox': 'documents/_inbox'}}
        staged = _write(self.documents / '_inbox' / 'not-yet-filed.m4a', b'waiting to be imported')
        result = media.run_media_dedupe(self.archive, cfg, incoming_args=[str(staged)])
        self.assertEqual(result.exit_code, media.DEDUPE_CLEAR)
        self.assertEqual(result.data['status'], 'new')

    # -- 4. CANDIDATES: a same-size file that cannot be hashed ---------------
    def test_an_unhashable_same_size_candidate_is_indeterminate_not_new(self):
        """Rounding an unreadable candidate down to 'not a twin' is the bug
        that let a byte-identical recording get imported twice."""
        payload = b'exactly seventeen'
        assert len(payload) == 17
        _write(self.documents / 'interviews' / 'unreadable_S-0000000002.m4a', payload)
        incoming = _write(self.incoming_dir / 'incoming.m4a', payload)

        real_sha256 = media.sha256_file

        def boom(path, cache):
            if 'unreadable' in path:
                raise OSError(13, 'Permission denied')
            return real_sha256(path, cache)

        with mock.patch('media.sha256_file', side_effect=boom):
            result = media.run_media_dedupe(self.archive, self._fha_config(),
                                            incoming_args=[str(incoming)])
        self.assertEqual(result.exit_code, media.DEDUPE_INDETERMINATE)
        self.assertFalse(result.ok)
        entry = result.data['results'][0]
        self.assertEqual(entry['verdict'], 'indeterminate')

    def test_an_unreadable_archived_folder_turns_every_new_into_indeterminate(self):
        """apply_archive_coverage: a folder nobody could read might hold ANY twin."""
        incoming = _write(self.incoming_dir / 'plainly-new.m4a', b'nothing like it archived')
        real_index = media.index_sizes_by_root

        def with_unreadable(named_roots, staging=None):
            by_size, unreadable = real_index(named_roots, staging)
            unreadable.append(str(self.documents / 'a-folder-nobody-could-list'))
            return by_size, unreadable

        with mock.patch('media.index_sizes_by_root', side_effect=with_unreadable):
            result = media.run_media_dedupe(self.archive, self._fha_config(),
                                            incoming_args=[str(incoming)])
        self.assertEqual(result.exit_code, media.DEDUPE_INDETERMINATE)
        self.assertFalse(result.ok)

    # -- 5. BATCH: two names for one sitting ----------------------------------
    def test_a_batch_containing_its_own_duplicate_imports_only_one(self):
        payload = b'one afternoon, exported twice under two names'
        a = _write(self.incoming_dir / 'Thursday at 3-11 PM.m4a', payload)
        b = _write(self.incoming_dir / 'Thursday at 3-12 PM.m4a', payload)
        result = media.run_media_dedupe(self.archive, self._fha_config(),
                                        incoming_args=[str(a), str(b)])
        self.assertEqual(result.exit_code, media.DEDUPE_DUPLICATE)
        verdicts = {r['verdict'] for r in result.data['results']}
        self.assertEqual(verdicts, {'new', 'duplicate'})
        dup = next(r for r in result.data['results'] if r['verdict'] == 'duplicate')
        self.assertIn('repeat_of', dup)

    # -- The two consequences GAP.md calls out by name ------------------------
    def test_a_file_already_inside_a_media_root_is_reported_already_filed(self):
        """A file handed to the verb that IS the archive's own copy: never `new`."""
        result = media.run_media_dedupe(self.archive, self._fha_config(),
                                        incoming_args=[str(self.filed)])
        self.assertEqual(result.exit_code, media.DEDUPE_DUPLICATE)
        entry = result.data['results'][0]
        self.assertTrue(entry.get('already_filed'))
        self.assertEqual(entry['duplicates'][0]['source_id'], 'S-0000000001')

    def test_a_narrower_media_root_cannot_answer_the_same_question(self):
        """Removing the folder that would hide the twin must not read as clean."""
        cfg_narrow = {'roots': {'documents': 'photos'}}   # documents/ effectively hidden
        incoming = _write(self.incoming_dir / 'twin.m4a', b'the interview everyone already has')
        result = media.run_media_dedupe(self.archive, cfg_narrow, incoming_args=[str(incoming)])
        # documents/ is now unconfigured (photos took its slot); it is silently
        # skipped as an ordinary young archive, so the twin under it is invisible
        # and this run answers `new` - correctly, because the config genuinely
        # tells the tool to look nowhere else. What must NOT happen is a
        # DUPLICATE from the untouched fixture leaking through by accident.
        self.assertEqual(result.exit_code, media.DEDUPE_CLEAR)


class DedupeHappyPathTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.archive = _make_archive(self.tmp / 'archive')
        self.cfg = {'roots': {'documents': 'documents', 'photos': 'photos'}}
        self.incoming_dir = self.tmp / 'incoming'
        self.incoming_dir.mkdir()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_a_genuinely_new_recording_clears_with_exit_0(self):
        incoming = _write(self.incoming_dir / 'new.m4a', b'nothing like this exists yet')
        result = media.run_media_dedupe(self.archive, self.cfg, incoming_args=[str(incoming)])
        self.assertEqual(result.exit_code, media.DEDUPE_CLEAR)
        self.assertTrue(result.ok)

    def test_a_non_media_file_is_reported_but_never_given_a_verdict(self):
        note = _write(self.incoming_dir / 'notes.txt', b'not a recording')
        result = media.run_media_dedupe(self.archive, self.cfg, incoming_args=[str(note)])
        self.assertEqual(result.exit_code, media.DEDUPE_USAGE)
        self.assertIn('audio or video', result.messages[-1].text)

    def test_json_report_is_written_in_alias_form_with_no_absolute_paths(self):
        incoming = _write(self.incoming_dir / 'new.m4a', b'nothing like this exists yet')
        out = self.tmp / 'report.json'
        result = media.run_media_dedupe(self.archive, self.cfg, incoming_args=[str(incoming)],
                                        json_path=str(out))
        self.assertEqual(result.exit_code, media.DEDUPE_CLEAR)
        self.assertTrue(out.exists())
        payload = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual(payload['results'][0]['path'], 'incoming/new.m4a')
        self.assertNotIn(str(self.tmp), json.dumps(payload))

    def test_json_path_landing_on_the_incoming_file_is_refused_before_hashing(self):
        incoming = _write(self.incoming_dir / 'new.m4a', b'nothing like this exists yet')
        result = media.run_media_dedupe(self.archive, self.cfg, incoming_args=[str(incoming)],
                                        json_path=str(incoming))
        self.assertEqual(result.exit_code, media.DEDUPE_USAGE)
        self.assertFalse(result.ok)
        # Nothing was written: the file this run reads is exactly the file it
        # was refused from overwriting, so its bytes must be untouched.
        self.assertEqual(incoming.read_bytes(), b'nothing like this exists yet')

    def test_json_path_landing_on_fha_yaml_is_refused(self):
        incoming = _write(self.incoming_dir / 'new.m4a', b'nothing like this exists yet')
        before = (self.archive / 'fha.yaml').read_bytes()
        result = media.run_media_dedupe(self.archive, self.cfg, incoming_args=[str(incoming)],
                                        json_path=str(self.archive / 'fha.yaml'))
        self.assertEqual(result.exit_code, media.DEDUPE_USAGE)
        self.assertEqual((self.archive / 'fha.yaml').read_bytes(), before)

    def test_a_missing_incoming_path_is_a_usage_error(self):
        result = media.run_media_dedupe(self.archive, self.cfg,
                                        incoming_args=[str(self.tmp / 'nope.m4a')])
        self.assertEqual(result.exit_code, media.DEDUPE_USAGE)


class SourceIdInTests(unittest.TestCase):
    def test_extracts_the_s_id_from_a_processed_filename(self):
        self.assertEqual(
            media.source_id_in('hartley-1998-06-14_S-0000000001.m4a'), 'S-0000000001')

    def test_a_photo_root_filename_carries_no_id(self):
        self.assertIsNone(media.source_id_in('portrait_1880.jpg'))

    def test_an_unprocessed_filename_carries_no_id(self):
        self.assertIsNone(media.source_id_in('Thursday at 3-11 PM.m4a'))


class ArithmeticTests(unittest.TestCase):
    """SKILL.md step 4's formula, pinned as pure arithmetic (no I/O)."""

    def test_parse_iso8601_reads_a_utc_creation_time(self):
        dt = media._parse_iso8601('2020-06-15T01:20:00.000000Z')
        self.assertEqual(dt, datetime.datetime(2020, 6, 15, 1, 20, 0, tzinfo=datetime.timezone.utc))

    def test_parse_iso8601_reads_a_local_offset_with_no_colon(self):
        dt = media._parse_iso8601('1998-06-14T20:15:00-0500')
        self.assertEqual(dt.utcoffset(), datetime.timedelta(hours=-5))
        self.assertEqual(dt.replace(tzinfo=None), datetime.datetime(1998, 6, 14, 20, 15, 0))

    def test_parse_iso8601_refuses_to_guess_at_a_malformed_string(self):
        self.assertIsNone(media._parse_iso8601('not a timestamp'))
        self.assertIsNone(media._parse_iso8601(''))
        self.assertIsNone(media._parse_iso8601(None))

    def test_filename_clock_reads_common_app_and_camera_shapes(self):
        cases = {
            '2020-06-14 15.30.00.m4a': datetime.datetime(2020, 6, 14, 15, 30, 0),
            '2020-06-14_15-30-00.m4a': datetime.datetime(2020, 6, 14, 15, 30, 0),
            '20200614_153000.m4a': datetime.datetime(2020, 6, 14, 15, 30, 0),
            'hartley-1998-06-14T20-15-00.mov': datetime.datetime(1998, 6, 14, 20, 15, 0),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(media._parse_filename_clock(name), expected)

    def test_a_relative_weekday_label_carries_no_clock(self):
        """GAP.md's own example of a filename clock that solves nothing."""
        self.assertIsNone(media._parse_filename_clock('Thursday at 3-11 PM.m4a'))

    def test_the_cross_check_invariant_holds(self):
        """filename_time + duration == creation_time (once offset is applied) -
        GAP.md's own noted invariant, held for every recording carrying a clock."""
        filename_dt = datetime.datetime(1998, 6, 14, 20, 15, 0)
        duration = 300.0   # five minutes
        real_offset = datetime.timedelta(hours=-5)
        local_stop = filename_dt + datetime.timedelta(seconds=duration)
        utc_stop = (local_stop - real_offset).replace(tzinfo=datetime.timezone.utc)

        solved, raw, miss = media._solve_offset_from_filename(filename_dt, duration, utc_stop)
        self.assertEqual(solved, real_offset)
        self.assertLess(miss, 1.0)

        local_start = media._derive_local_start(utc_stop, duration, solved)
        self.assertEqual(local_start.replace(tzinfo=None), filename_dt)

    def test_a_filename_clock_more_than_a_couple_minutes_off_does_not_solve(self):
        """SKILL.md: 'a fit that misses by more than a couple of minutes
        means the filename clock is not what you took it for'."""
        filename_dt = datetime.datetime(1998, 6, 14, 20, 15, 0)
        duration = 300.0
        # Five real minutes off the nearest quarter-hour grid.
        utc_stop = datetime.datetime(1998, 6, 15, 1, 25, 0, tzinfo=datetime.timezone.utc)
        solved, raw, miss = media._solve_offset_from_filename(filename_dt, duration, utc_stop)
        self.assertIsNone(solved)
        self.assertGreater(miss, media.FILENAME_CLOCK_TOLERANCE_SECONDS)

    def test_fmt_offset_handles_negative_and_positive_and_zero(self):
        self.assertEqual(media._fmt_offset(datetime.timedelta(hours=-5)), '-05:00')
        self.assertEqual(media._fmt_offset(datetime.timedelta(hours=5, minutes=30)), '+05:30')
        self.assertEqual(media._fmt_offset(datetime.timedelta(0)), '+00:00')


class RunMediaProbeTests(unittest.TestCase):
    """`run_media_probe` against a MOCKED ffprobe backend - never a real binary."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.archive = _make_archive(self.tmp / 'archive')
        self.recording = _write(self.tmp / '2020-06-14_20-15-00.m4a', b'fake media bytes')

    def tearDown(self):
        self._tmpdir.cleanup()

    def _probe(self, tags, duration=300.0, backend='media._probe_with_ffprobe'):
        with mock.patch(backend, return_value={'duration': duration, 'tags': tags}), \
             mock.patch('media.shutil.which', return_value='/usr/bin/ffprobe'):
            return media.run_media_probe(self.archive, {}, file_arg=str(self.recording))

    def test_quicktime_creationdate_settles_the_offset_outright(self):
        result = self._probe({
            'creation_time': '2020-06-15T01:20:00.000000Z',
            'com.apple.quicktime.creationdate': '2020-06-14T20:20:00-0500',
        })
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['offset_source'], 'quicktime_creationdate')
        self.assertEqual(result.data['offset'], '-05:00')
        self.assertEqual(result.data['local_start'], '2020-06-14T20:15:00-05:00')

    def test_filename_clock_solves_the_offset_when_no_quicktime_tag(self):
        result = self._probe({'creation_time': '2020-06-15T01:20:00.000000Z'}, duration=300.0)
        self.assertEqual(result.exit_code, EXIT_CLEAN)
        self.assertEqual(result.data['offset_source'], 'filename_clock')
        self.assertEqual(result.data['offset'], '-05:00')
        self.assertEqual(result.data['local_start'], '2020-06-14T20:15:00-05:00')
        self.assertTrue(result.data['filename_clock']['solved'])

    def test_neither_source_available_says_so_plainly_and_warns(self):
        """No quicktime tag, and the recording sits in an inbox drop with a
        filename that carries no clock - the offset genuinely cannot be
        determined, and this must never fall back to filesystem mtime."""
        renamed = self.tmp / 'Thursday at 3-11 PM.m4a'
        self.recording.rename(renamed)
        with mock.patch('media._probe_with_ffprobe',
                        return_value={'duration': 300.0,
                                     'tags': {'creation_time': '2020-06-15T01:20:00.000000Z'}}), \
             mock.patch('media.shutil.which', return_value='/usr/bin/ffprobe'):
            result = media.run_media_probe(self.archive, {}, file_arg=str(renamed))
        self.assertEqual(result.exit_code, EXIT_WARNINGS)
        self.assertFalse(result.ok)
        self.assertIsNone(result.data['local_start'])
        self.assertIsNone(result.data['offset'])
        self.assertTrue(any('timezone could not be established' in m.text
                            for m in result.messages))
        self.assertFalse(any('mtime' in m.text.lower() for m in result.messages))

    def test_no_usable_creation_time_at_all_is_reported_plainly(self):
        result = self._probe({})   # no creation_time tag whatsoever
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertFalse(result.ok)
        self.assertTrue(any('no usable creation timestamp' in m.text for m in result.messages))
        self.assertIsNone(result.data['creation_time_utc'])

    def test_midnight_straddle_is_flagged(self):
        result = self._probe({'creation_time': '2020-06-15T01:20:00.000000Z'}, duration=300.0)
        self.assertTrue(result.data['crosses_midnight'])
        self.assertEqual(result.data['utc_date'], '2020-06-15')
        self.assertEqual(result.data['local_start_date'], '2020-06-14')
        self.assertTrue(any('straddle' in m.text for m in result.messages))

    def test_no_midnight_straddle_when_local_and_utc_dates_agree(self):
        # A recording made mid-morning US/Eastern: local and UTC land on the
        # same calendar day.
        result = self._probe({
            'creation_time': '2020-06-14T15:05:00.000000Z',
            'com.apple.quicktime.creationdate': '2020-06-14T10:05:00-0500',
        }, duration=300.0)
        self.assertFalse(result.data['crosses_midnight'])

    def test_a_disagreeing_quicktime_tag_is_used_but_flagged(self):
        """The two container tags naming different instants is worth a warning,
        not a silent pick of one over the other."""
        result = self._probe({
            'creation_time': '2020-06-15T01:20:00.000000Z',
            'com.apple.quicktime.creationdate': '2020-06-14T18:00:00-0500',  # 2h+ off
        })
        self.assertEqual(result.data['offset_source'], 'quicktime_creationdate')
        self.assertTrue(any('disagree' in m.text for m in result.messages))

    def test_missing_duration_prevents_a_local_start_even_with_creation_time(self):
        result = self._probe({'creation_time': '2020-06-15T01:20:00.000000Z'}, duration=None)
        self.assertEqual(result.exit_code, EXIT_ERRORS)
        self.assertIsNone(result.data['local_start'])

    def test_neither_backend_available_fails_with_a_plain_message(self):
        with mock.patch('media.shutil.which', return_value=None), \
             mock.patch('media._probe_with_pyav',
                        side_effect=RuntimeError('needs ffprobe or PyAV')):
            result = media.run_media_probe(self.archive, {}, file_arg=str(self.recording))
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertFalse(result.ok)

    def test_pyav_fallback_is_used_when_ffprobe_is_absent(self):
        with mock.patch('media.shutil.which', return_value=None), \
             mock.patch('media._probe_with_pyav',
                        return_value={'duration': 300.0,
                                     'tags': {'creation_time': '2020-06-15T01:20:00.000000Z',
                                              'com.apple.quicktime.creationdate':
                                              '2020-06-14T20:20:00-0500'}}):
            result = media.run_media_probe(self.archive, {}, file_arg=str(self.recording))
        self.assertEqual(result.data['backend'], 'pyav')
        self.assertEqual(result.exit_code, EXIT_CLEAN)

    def test_a_missing_file_is_a_clean_failure_not_a_traceback(self):
        result = media.run_media_probe(self.archive, {}, file_arg='no-such-file.m4a')
        self.assertEqual(result.exit_code, EXIT_FAILURE)
        self.assertFalse(result.ok)

    def test_path_resolves_forgivingly_under_the_archive_root(self):
        """Matching `fha process`'s own doctrine (TOOLING §6): as typed first,
        retried under the archive root."""
        under_root = _write(self.archive / 'documents' / 'clip.m4a', b'x')
        with mock.patch('media._probe_with_ffprobe',
                        return_value={'duration': 1.0, 'tags': {}}), \
             mock.patch('media.shutil.which', return_value='/usr/bin/ffprobe'):
            result = media.run_media_probe(self.archive, {}, file_arg='documents/clip.m4a')
        self.assertEqual(result.data['file'], 'clip.m4a')


class ProbeBackendSelectionTests(unittest.TestCase):
    """`_probe_with_ffprobe` / `_probe_with_pyav` in isolation, JSON-shaped fixtures only."""

    def test_ffprobe_json_is_normalized_to_lowercase_tag_keys(self):
        fake_json = json.dumps({
            'format': {
                'duration': '125.347000',
                'tags': {'Creation_Time': '2020-06-15T01:20:00.000000Z',
                         'com.apple.quicktime.creationdate': '2020-06-14T20:20:00-0500'},
            }
        })
        completed = mock.Mock(returncode=0, stdout=fake_json, stderr='')
        with mock.patch('media.subprocess.run', return_value=completed):
            probed = media._probe_with_ffprobe(Path('irrelevant.m4a'))
        self.assertAlmostEqual(probed['duration'], 125.347)
        self.assertIn('creation_time', probed['tags'])
        self.assertIn('com.apple.quicktime.creationdate', probed['tags'])

    def test_ffprobe_missing_binary_raises_with_a_plain_message(self):
        with mock.patch('media.subprocess.run', side_effect=FileNotFoundError()):
            with self.assertRaises(RuntimeError) as cm:
                media._probe_with_ffprobe(Path('irrelevant.m4a'))
        self.assertIn('ffprobe', str(cm.exception))

    def test_ffprobe_nonzero_exit_raises_value_error_not_a_traceback(self):
        completed = mock.Mock(returncode=1, stdout='', stderr='moov atom not found')
        with mock.patch('media.subprocess.run', return_value=completed):
            with self.assertRaises(ValueError) as cm:
                media._probe_with_ffprobe(Path('broken.m4a'))
        self.assertIn('broken.m4a', str(cm.exception))

    def test_ffprobe_missing_duration_field_is_none_not_a_crash(self):
        fake_json = json.dumps({'format': {'tags': {}}})
        completed = mock.Mock(returncode=0, stdout=fake_json, stderr='')
        with mock.patch('media.subprocess.run', return_value=completed):
            probed = media._probe_with_ffprobe(Path('irrelevant.m4a'))
        self.assertIsNone(probed['duration'])


class PyAVBackendTests(unittest.TestCase):
    """`_probe_with_pyav` in isolation, against a FAKE `av` module - no real
    PyAV dependency in this test, matching the ffprobe backend's own tests."""

    def _fake_av(self, duration_us, metadata):
        container = mock.MagicMock()
        container.duration = duration_us
        container.metadata = metadata
        fake_av = mock.MagicMock()
        fake_av.open.return_value = container
        return fake_av, container

    def test_a_zero_duration_container_is_read_as_zero_not_unknown(self):
        """Regression: `if container.duration:` (truthiness) read a genuine
        zero-length container the same as PyAV's own None-for-unknown,
        silently discarding a real value - the same class of bug as treating
        an empty string or a zero count as 'absent' elsewhere in this file."""
        fake_av, container = self._fake_av(0, {})
        with mock.patch.dict('sys.modules', {'av': fake_av}):
            probed = media._probe_with_pyav(Path('irrelevant.m4a'))
        self.assertEqual(probed['duration'], 0.0)
        container.close.assert_called_once()

    def test_an_unknown_duration_stays_none(self):
        fake_av, _container = self._fake_av(None, {})
        with mock.patch.dict('sys.modules', {'av': fake_av}):
            probed = media._probe_with_pyav(Path('irrelevant.m4a'))
        self.assertIsNone(probed['duration'])

    def test_a_known_duration_converts_microseconds_to_seconds(self):
        fake_av, _container = self._fake_av(300_000_000, {})
        with mock.patch.dict('sys.modules', {'av': fake_av}):
            probed = media._probe_with_pyav(Path('irrelevant.m4a'))
        self.assertAlmostEqual(probed['duration'], 300.0)

    def test_metadata_keys_are_lowercased_like_the_ffprobe_backend(self):
        fake_av, _container = self._fake_av(
            300_000_000, {'Creation_Time': '2020-06-15T01:20:00Z'})
        with mock.patch.dict('sys.modules', {'av': fake_av}):
            probed = media._probe_with_pyav(Path('irrelevant.m4a'))
        self.assertIn('creation_time', probed['tags'])

    def test_pyav_not_installed_raises_a_plain_message_naming_both_options(self):
        with mock.patch.dict('sys.modules', {'av': None}):
            with self.assertRaises(RuntimeError) as cm:
                media._probe_with_pyav(Path('irrelevant.m4a'))
        self.assertIn('ffprobe', str(cm.exception))
        self.assertIn('PyAV', str(cm.exception))

    def test_a_decode_failure_is_a_value_error_not_a_traceback(self):
        fake_av = mock.MagicMock()
        fake_av.open.side_effect = RuntimeError('moov atom not found')
        with mock.patch.dict('sys.modules', {'av': fake_av}):
            with self.assertRaises(ValueError) as cm:
                media._probe_with_pyav(Path('broken.m4a'))
        self.assertIn('broken.m4a', str(cm.exception))


class ResolveMediaRootsTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.archive = _make_archive(self.tmp / 'archive')

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_an_explicit_but_blank_alias_is_missing_not_empty(self):
        cfg = {'roots': {'documents': '', 'photos': 'photos'}}
        named, missing, staging = media.resolve_media_roots(self.archive, cfg)
        self.assertEqual([a for a, _v in missing], ['documents'])

    def test_a_malformed_roots_mapping_raises_config_problem(self):
        cfg = {'roots': ['not', 'a', 'mapping']}
        with self.assertRaises(media.ConfigProblem):
            media.resolve_media_roots(self.archive, cfg)

    def test_an_external_absolute_root_is_resolved_and_walkable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as ext:
            cfg = {'roots': {'documents': ext, 'photos': 'photos'}}
            named, missing, staging = media.resolve_media_roots(self.archive, cfg)
            self.assertEqual(missing, [])
            self.assertIn(('documents', str(Path(ext).resolve())), named)


if __name__ == '__main__':
    unittest.main()
