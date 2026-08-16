"""Tests for the transcribe-audio skill's script (BUILD_INTERFACE.md MI6.2).

The transcription itself needs `faster-whisper`, a heavy optional dependency
that is not installed in CI - and it is not what can go wrong here anyway.
What can go wrong is everything around it, so that is what these tests pin:

  1. ALL-OR-NOTHING PUBLICATION. faster-whisper hands back a lazy segment
     iterator, so a decode error, a model blow-up, or a Ctrl-C lands in the
     middle of the write loop. The skill tells the human to work long queues by
     skipping any recording whose outputs already exist, which turns a
     truncated-but-plausible transcript into a permanent one. The writer must
     therefore leave NOTHING behind on failure - not a stump, not a `.part`
     file - and must not mask the original error while cleaning up. The tests
     inject fake segments (and a fake iterator that raises partway) directly.

  1b. ...AND THAT INCLUDES PUTTING THE FILES IN PLACE. Three renames are not
     one operation. A destination that refuses the second rename, or a Ctrl-C
     between two of them, would publish one file of the new run beside two of
     the old one - a set that looks finished and is therefore skipped forever.
     So: every destination is checked before the first rename, a failure part
     way through restores the previous files, and a kill no rollback can catch
     leaves a marker that stops the next run from calling the recording done.
     These tests drive the failure by making one destination a folder and by
     making `os.replace` refuse one specific name.

  2. PORTABILITY / PRIVACY OF THE OUTPUT. The .md is written to be attached to
     a source record and kept forever, so it must name the recording by
     filename and never by the absolute path the operator happened to type
     (AGENTS_TOOLING.md privacy rule; SPEC 12.4 alias-form paths).

  3. THE DOCUMENTED COMMANDS ACTUALLY WORK. Every flag SKILL.md names exists in
     the parser, and the worked `--name` example really produces the filename
     the skill shows, checked against `process.attach_more`'s own naming rule
     rather than against a copy of it.

Run: python -m unittest tests.test_transcribe_audio -v   (from the repo root)
"""

import importlib.util
import io
import contextlib
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))

import process  # noqa: E402  (path set above, as every tool test here does)

SKILL_DIR = ROOT / '.claude' / 'skills' / 'transcribe-audio'
SCRIPT = SKILL_DIR / 'scripts' / 'transcribe_audio.py'
SKILL_MD = SKILL_DIR / 'SKILL.md'


def _load_script():
    """Import the skill script by path.

    Skill scripts deliberately live outside `tools/` and import nothing from
    it (they must run standalone on the machine holding the audio), so there is
    no package to import them from.
    """
    spec = importlib.util.spec_from_file_location('transcribe_audio', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ta = _load_script()


class FakeSegment:
    """The three attributes the writer reads off a faster-whisper segment."""

    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


def _segments(count=3):
    return [FakeSegment(i * 10, i * 10 + 5, f' line {i} ') for i in range(count)]


class Boom(RuntimeError):
    """A decode failure, standing in for whatever faster-whisper raises."""


def _exploding_segments(good=2):
    """A lazy iterator that yields a few segments and then fails, like a bad decode."""
    for seg in _segments(good):
        yield seg
    raise Boom('bad frame at 00:12:03')


def _leftovers(outdir):
    return sorted(p.name for p in Path(outdir).iterdir())


PREVIOUS = 'previous good pass'


def _plant_previous_transcript(outdir, name='stem'):
    """Put a finished transcript from an earlier run in place, and return its paths."""
    finals = ta.output_paths(outdir, name)
    for final in finals:
        final.write_text(PREVIOUS, encoding='utf-8')
    return finals


def _counted_segments(seen, count=3):
    """A lazy iterator that records every segment pulled from it.

    Lets a test prove a refusal happened BEFORE the hour of transcription
    rather than after it - the difference between a wasted second and a wasted
    afternoon.
    """
    def gen():
        for seg in _segments(count):
            seen.append(seg.start)
            yield seg
    return gen()


class _RefusingReplace:
    """os.replace, but it refuses one particular destination.

    This is the failure the round-2 review is about: not a crash in the
    transcription, but a rename that will not happen - a locked file, a full
    disk, a permission change made while the recording was being decoded. There
    is no portable way to provoke a real one on demand (running as root defeats
    most of them), so the refusal is injected at the one call that matters.

    `times=1` refuses the promotion but lets the move back through, which is the
    ordinary failure. `times=None` refuses forever, so even the rollback cannot
    put the file back - the shape a full disk leaves, and the one case where the
    folder really is left mid-change.
    """

    def __init__(self, target, error, times=1):
        self.target = Path(target)
        self.error = error
        self.times = times
        self.refused = 0
        self.real = os.replace

    def __call__(self, src, dst, *args, **kwargs):
        if Path(dst) == self.target and (self.times is None or self.refused < self.times):
            self.refused += 1
            raise self.error
        return self.real(src, dst, *args, **kwargs)


def _install_fake_whisper(testcase, segments):
    """Stand a fake `faster_whisper` module up for one test, and take it down after."""

    class FakeModel:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, *a, **k):
            return segments, None

    module = type(sys)('faster_whisper')
    module.WhisperModel = FakeModel
    sys.modules['faster_whisper'] = module
    testcase.addCleanup(sys.modules.pop, 'faster_whisper', None)


class PublishTestCase(unittest.TestCase):
    """publish_transcripts: all three files appear together, or none do."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_successful_run_publishes_all_three(self):
        count = ta.publish_transcripts(self.out, 'stem', _segments(3), 'medium', 'rec.m4a')
        self.assertEqual(count, 3)
        self.assertEqual(_leftovers(self.out), ['stem.md', 'stem.srt', 'stem.txt'])

    def test_output_contents(self):
        ta.publish_transcripts(self.out, 'stem', _segments(2), 'medium', 'rec.m4a')
        txt = (self.out / 'stem.txt').read_text(encoding='utf-8')
        srt = (self.out / 'stem.srt').read_text(encoding='utf-8')
        md = (self.out / 'stem.md').read_text(encoding='utf-8')
        # Text is stripped; one line per segment.
        self.assertEqual(txt, 'line 0\nline 1\n')
        # SRT numbering is 1-based and timestamps use the comma form.
        self.assertIn('1\n00:00:00,000 --> 00:00:05,000\nline 0\n', srt)
        self.assertIn('2\n00:00:10,000 --> 00:00:15,000\nline 1\n', srt)
        # The .md carries the HH:MM:SS anchors claim notes quote.
        self.assertIn('**[00:00:10]** line 1', md)

    def test_failure_midway_leaves_no_output_and_no_temp_files(self):
        with self.assertRaises(Boom):
            ta.publish_transcripts(self.out, 'stem', _exploding_segments(), 'medium', 'rec.m4a')
        self.assertEqual(_leftovers(self.out), [])

    def test_cleanup_does_not_mask_the_original_error(self):
        """The failure the human sees is the decode failure, not a tidy-up error."""
        with self.assertRaises(Boom) as ctx:
            ta.publish_transcripts(self.out, 'stem', _exploding_segments(), 'medium', 'rec.m4a')
        self.assertIn('bad frame', str(ctx.exception))

    def test_keyboard_interrupt_also_cleans_up(self):
        """Ctrl-C on an hour-long run must not leave a plausible stump behind."""

        def interrupted():
            yield FakeSegment(0, 1, 'hello')
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            ta.publish_transcripts(self.out, 'stem', interrupted(), 'medium', 'rec.m4a')
        self.assertEqual(_leftovers(self.out), [])

    def test_failed_rerun_does_not_damage_an_existing_transcript(self):
        """A --force retry that dies leaves the previous good transcript intact."""
        for name in ('stem.txt', 'stem.srt', 'stem.md'):
            (self.out / name).write_text('previous good pass', encoding='utf-8')
        with self.assertRaises(Boom):
            ta.publish_transcripts(self.out, 'stem', _exploding_segments(), 'medium', 'rec.m4a')
        self.assertEqual(_leftovers(self.out), ['stem.md', 'stem.srt', 'stem.txt'])
        self.assertEqual((self.out / 'stem.md').read_text(encoding='utf-8'),
                         'previous good pass')

    def test_zero_segments_publishes_nothing(self):
        """Silence must not produce an empty file a batch run would call finished."""
        count = ta.publish_transcripts(self.out, 'stem', iter([]), 'medium', 'rec.m4a')
        self.assertEqual(count, 0)
        self.assertEqual(_leftovers(self.out), [])

    def test_progress_callback_is_optional_and_gets_start_times(self):
        seen = []
        ta.publish_transcripts(self.out, 'stem', _segments(2), 'medium', 'rec.m4a',
                               progress=seen.append)
        self.assertEqual(seen, [0, 10])


class PromotionTestCase(unittest.TestCase):
    """The three renames are all-or-nothing too, not just the writing.

    Every test here starts from a FINISHED previous transcript, because that is
    what makes a mixed set dangerous: one new file beside two old ones reads as
    a complete pass to the next batch run.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.finals = _plant_previous_transcript(self.out)

    def _refuse(self, target, error, times=1):
        """Make os.replace refuse `target` (see _RefusingReplace) for this test."""
        refusing = _RefusingReplace(target, error, times)
        os.replace = refusing
        self.addCleanup(setattr, os, 'replace', refusing.real)

    def _contents(self):
        return [p.read_text(encoding='utf-8') if p.is_file() else None for p in self.finals]

    def test_a_folder_on_the_second_name_publishes_none_of_the_new_set(self):
        """Preflight: one blocked destination stops the run before anything moves."""
        self.finals[1].unlink()
        self.finals[1].mkdir()
        seen = []
        with self.assertRaises(ta.PublishError) as ctx:
            ta.publish_transcripts(self.out, 'stem', _counted_segments(seen), 'medium', 'rec.m4a')
        self.assertIn('folder', str(ctx.exception).lower())
        self.assertIn('--outdir', str(ctx.exception))
        # The previous transcript is untouched, both files of it.
        self.assertEqual(self.finals[0].read_text(encoding='utf-8'), PREVIOUS)
        self.assertEqual(self.finals[2].read_text(encoding='utf-8'), PREVIOUS)
        # And the refusal cost nothing: no segment was ever decoded.
        self.assertEqual(seen, [])
        self.assertEqual(_leftovers(self.out), ['stem.md', 'stem.srt', 'stem.txt'])

    def test_a_refused_rename_restores_every_previous_file(self):
        """The review's case: destination two rejects the rename after one has landed."""
        self._refuse(self.finals[1], OSError(13, 'Permission denied'))
        with self.assertRaises(ta.PublishError) as ctx:
            ta.publish_transcripts(self.out, 'stem', _segments(3), 'medium', 'rec.m4a')
        self.assertIn('stem.srt', str(ctx.exception))
        self.assertEqual(self._contents(), [PREVIOUS, PREVIOUS, PREVIOUS])
        self.assertEqual(_leftovers(self.out), ['stem.md', 'stem.srt', 'stem.txt'])
        self.assertEqual(ta.publication_state(self.out, 'stem'), 'complete')

    def test_a_ctrl_c_between_two_renames_restores_every_previous_file(self):
        self._refuse(self.finals[1], KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            ta.publish_transcripts(self.out, 'stem', _segments(3), 'medium', 'rec.m4a')
        self.assertEqual(self._contents(), [PREVIOUS, PREVIOUS, PREVIOUS])
        self.assertEqual(_leftovers(self.out), ['stem.md', 'stem.srt', 'stem.txt'])

    def test_a_rollback_that_cannot_finish_leaves_the_marker_behind(self):
        """The one case nothing can undo must still be one the next run repairs.

        Refusing every rename onto `stem.srt` breaks the promotion AND the move
        back, which is the shape a hard kill leaves: files from two runs and no
        way to tell by looking. The marker is what tells the next run.
        """
        self._refuse(self.finals[1], OSError(28, 'No space left on device'), times=None)
        with self.assertRaises(ta.PublishError):
            ta.publish_transcripts(self.out, 'stem', _segments(3), 'medium', 'rec.m4a')
        self.assertTrue(ta.marker_path(self.out, 'stem').exists())
        self.assertEqual(ta.publication_state(self.out, 'stem'), 'interrupted')
        # The previous .srt could not be moved back, so it is still set aside.
        self.assertFalse(self.finals[1].exists())
        self.assertTrue(any(p.name.endswith('.kept') for p in self.out.iterdir()))

    def test_a_successful_run_leaves_only_the_three_files(self):
        """No marker, no `.kept` copy of the transcript it replaced, no `.part`."""
        count = ta.publish_transcripts(self.out, 'stem', _segments(2), 'medium', 'rec.m4a')
        self.assertEqual(count, 2)
        self.assertEqual(_leftovers(self.out), ['stem.md', 'stem.srt', 'stem.txt'])
        self.assertEqual(self.finals[0].read_text(encoding='utf-8'), 'line 0\nline 1\n')
        self.assertEqual(ta.publication_state(self.out, 'stem'), 'complete')

    def test_publish_refuses_a_missing_output_folder_in_plain_words(self):
        with self.assertRaises(ta.PublishError) as ctx:
            ta.publish_transcripts(self.out / 'gone', 'stem', _segments(1), 'medium', 'rec.m4a')
        self.assertIn('--outdir', str(ctx.exception))


class PublicationStateTestCase(unittest.TestCase):
    """The "is this recording already done?" test - one a partial set must fail."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_nothing_on_disk(self):
        self.assertEqual(ta.publication_state(self.out, 'stem'), 'none')

    def test_all_three(self):
        _plant_previous_transcript(self.out)
        self.assertEqual(ta.publication_state(self.out, 'stem'), 'complete')

    def test_two_of_three_is_not_complete(self):
        finals = _plant_previous_transcript(self.out)
        finals[2].unlink()
        self.assertEqual(ta.publication_state(self.out, 'stem'), 'partial')

    def test_a_marker_beats_a_full_set(self):
        """All three present but a promotion was cut short: the files may be mixed."""
        _plant_previous_transcript(self.out)
        ta.marker_path(self.out, 'stem').write_text('x', encoding='utf-8')
        self.assertEqual(ta.publication_state(self.out, 'stem'), 'interrupted')

    def test_the_marker_is_named_for_its_run(self):
        """Two recordings sharing one scratch folder must not read each other's marker."""
        self.assertEqual(ta.marker_path(self.out, 'stem').name, '.stem.publishing')
        self.assertNotEqual(ta.marker_path(self.out, 'stem'),
                            ta.marker_path(self.out, 'other'))


class PrepareAudioTestCase(unittest.TestCase):
    """ffmpeg's exit code is a claim about the wav, not proof of one.

    A container whose audio track ffmpeg cannot read has been seen to exit 0
    and leave an empty wav; whisper then transcribes silence and the run
    "succeeds" with an empty transcript. The check is cheap and the fallback
    (PyAV on the original) is right there.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.src = self.tmp / 'rec.m4a'
        self.src.write_bytes(b'stands in for real audio')
        self.work = self.tmp / 'work'
        self.work.mkdir()

    def _fake_ffmpeg(self, wav_bytes):
        """Stand in for a successful ffmpeg run that produced `wav_bytes`."""
        def run(cmd, *a, **k):
            if wav_bytes is not None:
                (self.work / 'audio.wav').write_bytes(wav_bytes)
            return None
        real = ta.subprocess.run
        ta.subprocess.run = run
        self.addCleanup(setattr, ta.subprocess, 'run', real)

    def test_an_empty_wav_falls_back_to_the_original_file(self):
        self._fake_ffmpeg(b'')
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            got = ta.prepare_audio(self.src, self.work)
        self.assertEqual(got, self.src)
        self.assertIn('PyAV', buf.getvalue())

    def test_a_wav_that_was_never_written_falls_back_too(self):
        self._fake_ffmpeg(None)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ta.prepare_audio(self.src, self.work), self.src)

    def test_a_real_extraction_is_used(self):
        self._fake_ffmpeg(b'RIFF....WAVE and some samples')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ta.prepare_audio(self.src, self.work), self.work / 'audio.wav')


class PortablePathTestCase(unittest.TestCase):
    """Nothing machine-specific may reach a file that gets archived."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_md_header_names_the_recording_by_filename(self):
        header = ta.md_header('stem', 'rec_S-x9qcves0nm.m4a', 'medium')
        self.assertIn('Recording: rec_S-x9qcves0nm.m4a', header)

    def test_absolute_source_path_never_reaches_the_markdown(self):
        absolute = '/home/somebody/Family Archive/documents/interviews/rec_S-x9qcves0nm.m4a'
        ta.publish_transcripts(self.out, 'stem', _segments(1), 'medium',
                               Path(absolute).name)
        md = (self.out / 'stem.md').read_text(encoding='utf-8')
        self.assertNotIn('/home/somebody', md)
        self.assertNotIn('Family Archive', md)
        self.assertIn('Recording: rec_S-x9qcves0nm.m4a', md)

    def test_main_passes_only_the_basename_through(self):
        """End to end: the CLI hands publish_transcripts a bare filename."""
        captured = {}

        def fake_publish(outdir, name, segments, model, recording, progress=None):
            captured['recording'] = recording
            captured['name'] = name
            return 0

        audio = self.out / 'rec_S-x9qcves0nm.m4a'
        audio.write_bytes(b'not really audio')

        class FakeModel:
            def __init__(self, *a, **k):
                pass

            def transcribe(self, *a, **k):
                return iter([]), None

        fake_module = type(sys)('faster_whisper')
        fake_module.WhisperModel = FakeModel
        sys.modules['faster_whisper'] = fake_module
        self.addCleanup(sys.modules.pop, 'faster_whisper', None)
        real_publish = ta.publish_transcripts
        ta.publish_transcripts = fake_publish
        self.addCleanup(setattr, ta, 'publish_transcripts', real_publish)

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ta.main([str(audio), '--outdir', str(self.out / 'whisper')])
        self.assertEqual(rc, ta.EXIT_NO_SPEECH)
        self.assertEqual(captured['recording'], 'rec_S-x9qcves0nm.m4a')
        # The default --name drops the S-id so `fha process --more` can attach it.
        self.assertEqual(captured['name'], 'rec')


class NameRulesTestCase(unittest.TestCase):
    """--name is the sort key for the whole source; it is checked before the hour of work."""

    def test_default_name_strips_source_id_and_media_role(self):
        self.assertEqual(
            ta.default_output_name('hartley-thomas-interview-1998-06-14-farm-audio_S-x9qcves0nm'),
            'hartley-thomas-interview-1998-06-14-farm')

    def test_default_name_leaves_a_plain_stem_alone(self):
        self.assertEqual(ta.default_output_name('grandma-1998'), 'grandma-1998')

    def test_name_with_a_path_separator_is_refused(self):
        problem = ta.name_problem('sub/dir-stem')
        self.assertIsNotNone(problem)
        self.assertIn('--outdir', problem)

    def test_name_still_carrying_a_source_id_is_refused_with_the_fix(self):
        problem = ta.name_problem('farm-audio_S-x9qcves0nm')
        self.assertIsNotNone(problem)
        self.assertIn('--name farm-audio', problem)

    def test_empty_name_is_refused(self):
        self.assertIsNotNone(ta.name_problem('  '))

    def test_good_stem_passes(self):
        self.assertIsNone(ta.name_problem('hartley-thomas-interview-1998-06-14-farm'))


class CliTestCase(unittest.TestCase):
    """The CLI's refusals are plain, and cost nothing when the work is already done."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_missing_file_names_the_next_step(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            rc = ta.main([str(self.tmp / 'nope.m4a')])
        self.assertEqual(rc, ta.EXIT_FAILED)
        self.assertIn('no such file', buf.getvalue())

    def test_existing_outputs_are_a_no_op_success(self):
        """This is what makes 'skip anything already transcribed' safe in a batch."""
        audio = self.tmp / 'rec.m4a'
        audio.write_bytes(b'x')
        out = self.tmp / 'whisper'
        out.mkdir()
        for name in ('rec.txt', 'rec.srt', 'rec.md'):
            (out / name).write_text('done', encoding='utf-8')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ta.main([str(audio), '--outdir', str(out)])
        self.assertEqual(rc, ta.EXIT_OK)
        self.assertIn('already transcribed', buf.getvalue())
        self.assertIn('--force', buf.getvalue())
        self.assertEqual((out / 'rec.md').read_text(encoding='utf-8'), 'done')

    @unittest.skipIf(importlib.util.find_spec('faster_whisper') is not None,
                     'faster-whisper is installed here, so the missing-engine path cannot run')
    def test_missing_engine_explains_the_install(self):
        audio = self.tmp / 'rec.m4a'
        audio.write_bytes(b'x')
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(io.StringIO()):
            rc = ta.main([str(audio), '--outdir', str(self.tmp / 'w')])
        self.assertEqual(rc, ta.EXIT_FAILED)
        self.assertIn('pip install faster-whisper', buf.getvalue())


class CliRecoveryTestCase(unittest.TestCase):
    """What the CLI does about a set of files an earlier run did not finish.

    The skip-if-present rule is what makes a long queue re-runnable, and it is
    also what would make a half-published set permanent. These pin both halves:
    a finished set is still skipped, an unfinished one is redone, and whatever
    the run says about the folder is what is actually in it.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.audio = self.tmp / 'rec.m4a'
        self.audio.write_bytes(b'stands in for real audio')
        self.out = self.tmp / 'whisper'
        self.out.mkdir()
        self.finals = ta.output_paths(self.out, 'rec')

    def _run(self, *extra):
        """Run the CLI over the planted folder, returning (exit code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ta.main([str(self.audio), '--outdir', str(self.out), *extra])
        return rc, out.getvalue(), err.getvalue()

    def test_a_partial_set_is_redone_rather_than_skipped(self):
        """Two files of three is not a transcript, whatever a batch pass assumes."""
        _plant_previous_transcript(self.out, 'rec')
        self.finals[2].unlink()
        _install_fake_whisper(self, iter([]))
        rc, out, _err = self._run()
        # It transcribed (and found no speech); it did not report "already done".
        self.assertEqual(rc, ta.EXIT_NO_SPEECH)
        self.assertNotIn('already transcribed', out)
        self.assertIn('only part', out)
        # And having promised to replace them, it says it did not: the run found
        # no speech, so the two old files are still the two old files.
        self.assertIn('left exactly as they were', out)
        self.assertEqual(self.finals[0].read_text(encoding='utf-8'), PREVIOUS)

    def test_an_interrupted_promotion_is_redone_even_with_all_three_present(self):
        """The hard-kill case: three plausible files, one marker saying otherwise."""
        _plant_previous_transcript(self.out, 'rec')
        (self.out / '.rec.publishing').write_text('interrupted', encoding='utf-8')
        _install_fake_whisper(self, iter([]))
        rc, out, _err = self._run()
        self.assertEqual(rc, ta.EXIT_NO_SPEECH)
        self.assertNotIn('already transcribed', out)
        self.assertIn('mix of two runs', out)

    def test_a_blocked_destination_does_not_claim_nothing_was_written(self):
        """main's story and the folder have to agree, in this branch too."""
        _plant_previous_transcript(self.out, 'rec')
        self.finals[1].unlink()
        self.finals[1].mkdir()
        _install_fake_whisper(self, iter(_segments(3)))
        rc, _out, err = self._run('--force')
        self.assertEqual(rc, ta.EXIT_FAILED)
        self.assertIn('could not be saved', err)
        self.assertIn('left exactly as they were', err)
        self.assertNotIn('Nothing was written', err)
        # And that claim is true: the previous transcript really is untouched.
        self.assertEqual(self.finals[0].read_text(encoding='utf-8'), PREVIOUS)
        self.assertEqual(self.finals[2].read_text(encoding='utf-8'), PREVIOUS)

    def test_a_rollback_that_failed_is_reported_as_a_mixed_set(self):
        """The worst case has to be the loudest, not the quietest."""
        _plant_previous_transcript(self.out, 'rec')
        refusing = _RefusingReplace(self.finals[1], OSError(28, 'No space left on device'),
                                    times=None)
        os.replace = refusing
        self.addCleanup(setattr, os, 'replace', refusing.real)
        _install_fake_whisper(self, iter(_segments(3)))
        rc, _out, err = self._run('--force')
        self.assertEqual(rc, ta.EXIT_FAILED)
        self.assertIn('mix of this run and the last one', err)
        self.assertNotIn('Nothing was written', err)
        self.assertTrue(ta.marker_path(self.out, 'rec').exists())

    def test_a_decode_failure_still_reports_an_untouched_folder(self):
        """The other direction: when nothing was written, say so."""
        _install_fake_whisper(self, _exploding_segments())
        rc, _out, err = self._run()
        self.assertEqual(rc, ta.EXIT_FAILED)
        self.assertIn('transcription failed', err)
        self.assertIn('Nothing was written', err)
        self.assertEqual(_leftovers(self.out), [])


class SkillDocTestCase(unittest.TestCase):
    """SKILL.md's commands must be the commands the code actually accepts."""

    @classmethod
    def setUpClass(cls):
        cls.text = SKILL_MD.read_text(encoding='utf-8')
        cls.flags = set()
        for action in ta.build_parser()._actions:
            cls.flags.update(action.option_strings)

    def test_every_script_flag_the_skill_names_exists(self):
        documented = set(re.findall(r'`?(--[a-z][a-z-]*)', self.text))
        # `fha` verbs have their own flags; only the script's are checked here.
        script_flags = {'--model', '--outdir', '--name', '--language', '--force'}
        for flag in script_flags & documented:
            self.assertIn(flag, self.flags, f'SKILL.md documents {flag}, the script does not')

    def test_skill_documents_the_flags_that_change_behavior(self):
        for flag in ('--model', '--outdir', '--name', '--force', '--language'):
            self.assertIn(flag, self.text, f'{flag} exists in the script but SKILL.md is silent')

    def test_no_documented_name_carries_a_role_or_source_id(self):
        """Thread 3: `--name …-whisper` files as `…-whisper-whisper-transcript_S-….md`."""
        for stem in re.findall(r'--name ([A-Za-z0-9][A-Za-z0-9-]*)', self.text):
            self.assertFalse(stem.endswith('-whisper'),
                             f'--name {stem} would double the role suffix on attach')
            self.assertFalse(stem.endswith('-transcript'),
                             f'--name {stem} would double the role suffix on attach')
            self.assertNotRegex(stem, r'_S-', f'--name {stem} carries a source id')

    def test_worked_example_produces_the_filename_the_skill_shows(self):
        """Recompute the documented result with process.attach_more's own rule."""
        stems = re.findall(r'--name ([A-Za-z0-9][A-Za-z0-9-]*)', self.text)
        self.assertTrue(stems, 'SKILL.md no longer shows a worked --name example')
        shown = re.findall(r'([a-z0-9-]+)-whisper-transcript_(S-[0-9a-z]{10})\.md', self.text)
        self.assertTrue(shown, 'SKILL.md no longer shows the resulting filename')
        for prefix, sid in shown:
            # attach_more: f'{_slugify(stem)}-{_slugify(role)}_{sid}{suffix}'
            produced = f'{process._slugify(prefix)}-{process._slugify("whisper-transcript")}_{sid}.md'
            self.assertEqual(produced, f'{prefix}-whisper-transcript_{sid}.md')
            self.assertIn(prefix, stems,
                          f'the skill shows {prefix}-whisper-transcript_… but never tells the '
                          f'caller to pass --name {prefix}')

    def test_skill_does_not_prescribe_renaming_an_archived_file(self):
        """Thread 4: filed names and `files:` entries are tool territory only."""
        lowered = self.text.lower()
        for phrase in ('hand-edit its `files:`', 'rename the file and',
                       'change the file **and** the record'):
            self.assertNotIn(phrase, lowered, f'SKILL.md still prescribes: {phrase}')
        self.assertIn('fha reconcile', self.text)

    def test_documented_stem_really_attaches_under_the_documented_name(self):
        """End to end through `fha process --more`, not through a copy of its rule.

        This is the thread-3 regression: the skill used to say `--name …-whisper`
        while showing `…-whisper-transcript_S-….md` as the result, which the real
        attach turns into `…-whisper-whisper-transcript_S-….md`.
        """
        stems = re.findall(r'--name ([A-Za-z0-9][A-Za-z0-9-]*)', self.text)
        stem = stems[0]
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'archive'
            (archive / 'documents' / 'interviews').mkdir(parents=True)
            (archive / 'sources').mkdir()
            (archive / 'fha.yaml').write_text(
                'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
            (archive / 'photos').mkdir()

            interviews = archive / 'documents' / 'interviews'
            primary = interviews / f'{stem}-audio.m4a'
            primary.write_bytes(b'stands in for real audio')
            with contextlib.redirect_stdout(io.StringIO()):
                rc = process._standalone_main(
                    [str(primary), '--type', 'interview', '--slug', f'{stem}-audio',
                     '--root', str(archive)])
            self.assertEqual(rc, 0)
            renamed = next(interviews.glob(f'{stem}-audio_S-*.m4a'))
            sid = renamed.stem.split('_')[-1]

            whisper = interviews / f'{stem}.md'
            whisper.write_text('# Whisper transcript\n', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                rc = process._standalone_main(
                    [str(renamed), '--more', str(whisper), 'whisper-transcript',
                     '--root', str(archive)])
            self.assertEqual(rc, 0)
            attached = [p.name for p in interviews.glob('*whisper-transcript*')]
            self.assertEqual(attached, [f'{stem}-whisper-transcript_{sid}.md'])
            # And that is the shape SKILL.md shows the human.
            self.assertRegex(self.text,
                             re.escape(f'{stem}-whisper-transcript_S-') + r'[0-9a-z]{10}\.md')

    def test_attach_example_uses_a_documents_root_path(self):
        """`attach_more` refuses a file outside the documents root, so the example must obey."""
        attaches = re.findall(r'--more "([^"]+)"', self.text)
        self.assertTrue(attaches)
        for path in attaches:
            self.assertTrue(path.startswith('documents/'),
                            f'--more {path} is not under the documents root; attach_more refuses it')


if __name__ == '__main__':
    unittest.main()
