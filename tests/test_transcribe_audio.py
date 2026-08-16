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

  4. THE AUDIT NEVER REWRITES AN ACCEPTED CLAIM ON ITS OWN AUTHORITY. Step 6
     reads a whisper pass against claims a human already accepted, so it is the
     one place in this skill where prose can authorise a write to a `reviewed:`
     fact. `fha claim <C-id> --value …` edits the value and leaves `reviewed:`
     untouched, which turns a machine's new reading of the audio into a claim
     asserting that a human accepted it on a date he was looking at different
     words. `AcceptedClaimSafetyTestCase` pins both halves of the rule against
     the real `tools/claim.py` parser and against a real edit on a fixture: a
     correction is proposed as an exact before/after and applied only on an
     explicit per-claim yes, and applying it re-stamps `reviewed:` by passing
     `--status` in the same call.

Run: python -m unittest tests.test_transcribe_audio -v   (from the repo root)
"""

import argparse
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

import claim  # noqa: E402  (the real CLI the audit step drives)
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

    def test_an_unmarked_partial_set_is_not_overwritten(self):
        """The human's own file, sharing this --name by accident, is not ours to replace.

        `--outdir` is a folder HE picks, so a `rec.md` of his own sitting in it
        with no `rec.txt` and no `rec.srt` beside it is an ordinary thing, not
        the wreckage of an earlier run: this script's own torn promotion always
        leaves a `.publishing` marker, and there is none here. With no evidence
        the files are ours, replacing them would be exactly the overwrite
        AGENTS.md forbids.
        """
        his_own = "Aunt Ruth's notes on the farm - do not delete\n"
        self.finals[2].write_text(his_own, encoding='utf-8')
        _install_fake_whisper(self, iter(_segments(3)))
        rc, out, err = self._run()

        self.assertEqual(rc, ta.EXIT_FAILED)
        # His file is byte-for-byte what it was, and nothing new appeared.
        self.assertEqual(self.finals[2].read_text(encoding='utf-8'), his_own)
        self.assertEqual(_leftovers(self.out), ['rec.md'])
        # And it did not quietly call the recording finished either.
        self.assertNotIn('already transcribed', out + err)

    def test_the_refusal_names_the_command_that_would_replace_them(self):
        """A dead end is a bug: the message has to carry the exact next command."""
        self.finals[2].write_text('his own file', encoding='utf-8')
        _install_fake_whisper(self, iter(_segments(3)))
        _rc, out, err = self._run()
        said = out + err
        # One line he can copy: the script, this recording, this folder, --force.
        offered = [line for line in said.splitlines() if '--force' in line]
        self.assertTrue(offered, f'the refusal never names --force:\n{said}')
        line = offered[0]
        for part in ('transcribe_audio.py', str(self.audio), '--outdir', str(self.out)):
            self.assertIn(part, line, f'{part!r} missing from the offered command: {line!r}')
        # And the other way out, for when the files really are his.
        self.assertIn('--name', said)

    def test_that_named_command_really_does_replace_them(self):
        """The command the refusal prints is run here, and it works."""
        self.finals[2].write_text('his own file', encoding='utf-8')
        _install_fake_whisper(self, iter(_segments(3)))
        rc, _out, _err = self._run('--force')
        self.assertEqual(rc, ta.EXIT_OK)
        self.assertEqual(_leftovers(self.out), ['rec.md', 'rec.srt', 'rec.txt'])
        self.assertEqual(self.finals[0].read_text(encoding='utf-8'),
                         'line 0\nline 1\nline 2\n')

    def test_a_marked_partial_set_is_still_redone_without_force(self):
        """The control: a torn promotion of OURS is still repaired automatically.

        This is the half the marker exists for. Two files of three plus the
        `.publishing` marker is this script's own unfinished work, evidence and
        all, so it is redone rather than refused - the batch queue keeps
        repairing itself and only genuinely unowned files ask for `--force`.
        """
        _plant_previous_transcript(self.out, 'rec')
        self.finals[2].unlink()
        ta.marker_path(self.out, 'rec').write_text('interrupted', encoding='utf-8')
        _install_fake_whisper(self, iter([]))
        rc, out, _err = self._run()
        # It transcribed (and found no speech); it did not report "already done"
        # and it did not refuse.
        self.assertEqual(rc, ta.EXIT_NO_SPEECH)
        self.assertNotIn('already transcribed', out)
        self.assertIn('mix of two runs', out)
        # Having promised to replace them, it says it did not: the run found no
        # speech, so the two old files are still the two old files - and still
        # marked, so the next run will try again.
        self.assertIn('mix of this run and the last one', out)
        self.assertEqual(self.finals[0].read_text(encoding='utf-8'), PREVIOUS)
        self.assertTrue(ta.marker_path(self.out, 'rec').exists())

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

    def test_the_skill_does_not_promise_to_replace_files_it_may_not_own(self):
        """Doc-vs-code: the retired promise, in the words it was written in.

        The skill used to tell the reader that a part-set of output files is
        simply "re-done", which described a branch that replaced whatever was
        sitting under those names - a human's own `family.md` included. The
        script now refuses that case and asks for `--force`, so the prose has
        to stop promising the old behaviour and has to name the flag.
        """
        # Whitespace-normalised, so a reflowed paragraph cannot smuggle the old
        # promise back past the guard on a line break.
        flowed = re.sub(r'\s+', ' ', self.text.lower())
        for phrase in (
            'if only some of the three files are there, or if a run was killed',
            '`--force` is the only way to replace a *finished* transcript',
        ):
            self.assertNotIn(phrase, flowed,
                             f'SKILL.md still promises the retired behaviour: {phrase!r}')
        # And it names the way out, in the same breath as the refusal.
        self.assertIn('--force', self.text)
        self.assertIn('.publishing', self.text)

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


class AcceptedClaimSafetyTestCase(unittest.TestCase):
    """Step 6's audit must not rewrite an accepted claim under the old signature.

    `reviewed:` records the day a HUMAN looked at a claim's content. `fha claim
    <C-id> --value …` on its own edits the value and leaves `reviewed:` exactly
    where it was (verified against the real CLI in
    `test_a_value_only_edit_really_does_leave_the_old_reviewed_date`), so a
    machine-decided correction applied that way produces a claim asserting "a
    human accepted this on <old date>" about wording no human has ever seen -
    an incorrect claim wearing a human's signature. Every test here pins one
    half of the fix: the correction is the human's decision (proposed as an
    exact before/after, applied only on an explicit yes), and applying it
    re-stamps `reviewed:` so the date matches the decision it records.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = SKILL_MD.read_text(encoding='utf-8')
        cls.lowered = cls.text.lower()
        # Two different things, and the difference matters. `runnable` is what
        # the skill tells the agent to type - fenced blocks only. `mentioned`
        # includes inline prose, where the skill also has to NAME the bare
        # `--value` form in order to forbid it; scanning prose for a "missing
        # --status" would flag that prohibition as the very defect it prevents.
        fenced = '\n'.join(re.findall(r'```[a-z]*\n(.*?)```', cls.text, re.S))
        cls.runnable = [m.group(0).strip() for m in
                        re.finditer(r'fha claim [^\n`]+', re.sub(r'\\\n\s*', ' ', fenced))]
        cls.mentioned = [m.group(0).strip() for m in
                         re.finditer(r'fha claim [^\n`]+', re.sub(r'\\\n\s*', ' ', cls.text))]

    def test_the_skill_shows_at_least_one_correction_command(self):
        """Guard against the other tests passing because the prose went silent."""
        self.assertTrue(self.runnable, 'SKILL.md no longer shows any runnable `fha claim` command')

    def test_no_field_edit_is_shown_as_runnable_without_a_reviewed_restamp(self):
        """A field edit on an accepted claim must carry `--status` (which re-stamps).

        `--reviewed` only takes effect together with `--status` (claim.py
        `_add_arguments`), so `--status accepted` in the same call is the ONLY
        way `fha claim` writes a fresh review date beside a changed value.
        """
        field_flags = ('--value', '--date', '--type', '--place', '--place-text',
                       '--persons', '--confidence')
        for cmd in self.runnable:
            if cmd.startswith('fha claim new'):
                continue  # a brand-new claim has no prior signature to preserve
            if any(f in cmd for f in field_flags):
                self.assertIn(
                    '--status', cmd,
                    f'SKILL.md tells the agent to run a field edit with no --status '
                    f're-stamp: {cmd!r} - the corrected value would keep the old '
                    'reviewed: date, signing wording no human has read')

    def test_the_skill_names_the_bare_value_form_as_the_wrong_command(self):
        """Forbidding it explicitly is what stops the next author re-adding it."""
        self.assertRegex(
            self.lowered,
            r'`fha claim <c-id> --value[^`]*`\*{0,2} on its own is the wrong command',
            'SKILL.md never says plainly that a bare `fha claim <C-id> --value …` is '
            'the wrong command on an accepted claim')

    def test_every_documented_claim_flag_exists_in_the_real_cli(self):
        """Doc-vs-code: no invented flag (e.g. a `--notes` no verb provides)."""
        real = set()
        parser = argparse.ArgumentParser()
        claim._add_arguments(parser)
        for action in parser._actions:
            real.update(action.option_strings)
        for cmd in self.mentioned:
            if cmd.startswith('fha claim new'):
                continue  # `claim new` has its own parser, checked below
            for flag in re.findall(r'(--[a-z][a-z-]*)', cmd):
                self.assertIn(flag, real,
                              f'SKILL.md shows `fha claim … {flag}`, which the CLI '
                              f'does not accept: {cmd!r}')

    def test_every_documented_claim_new_flag_exists_in_the_real_cli(self):
        real = set()
        parser = argparse.ArgumentParser()
        claim._add_new_arguments(parser)
        for action in parser._actions:
            real.update(action.option_strings)
        for cmd in self.mentioned:
            if not cmd.startswith('fha claim new'):
                continue
            for flag in re.findall(r'(--[a-z][a-z-]*)', cmd):
                self.assertIn(flag, real,
                              f'SKILL.md shows `fha claim new … {flag}`, which the CLI '
                              f'does not accept: {cmd!r}')

    def test_the_skill_does_not_call_a_preserved_reviewed_date_correct(self):
        """The round-2 framing this fix retires, in the words it was written in."""
        for phrase in (
            "without touching `status:` or `reviewed:`",
            "which is right — the human's original acceptance stands",
            "the human's original acceptance stands",
            'status and `reviewed:` untouched',
        ):
            self.assertNotIn(phrase.lower(), self.lowered,
                             f'SKILL.md still presents a stale reviewed: date as correct: {phrase!r}')

    def test_the_audit_requires_an_explicit_yes_per_correction(self):
        """Agreeing to RUN the audit is not agreeing to each correction it finds."""
        self.assertIn('before/after', self.lowered,
                      'the audit must show each correction as an exact before/after')
        self.assertRegex(
            self.lowered,
            r'(agreeing to run the audit|running the audit) is not',
            'the audit must say that a yes to the audit is not a yes to its corrections')

    def test_the_skill_never_tells_the_agent_to_decide_a_correction(self):
        """Agent-decides phrasings, which the archive reserves for the human."""
        for phrase in ('apply a value fix with',
                       'correct the claim and',
                       'fix the claim and move on'):
            self.assertNotIn(phrase, self.lowered,
                             f'SKILL.md still has the agent deciding a correction: {phrase!r}')

    def test_the_guardrails_name_every_kind_of_write_the_body_performs(self):
        """A guardrail list that omits the claim writes contradicts step 6."""
        guardrails = self.lowered.split('## guardrails', 1)
        self.assertEqual(len(guardrails), 2, 'SKILL.md has no ## Guardrails section')
        guardrails = guardrails[1]
        for token in ('fha claim', 'accepted', 'reviewed:'):
            self.assertIn(token, guardrails,
                          f'the Guardrails section never mentions {token!r}, though step 6 '
                          'writes to accepted claims')

    def test_a_missing_verb_is_recorded_as_a_gap_not_papered_over(self):
        """_STANDARD.md §6: a capability no `fha` verb owns is named, not hand-rolled."""
        gap = SKILL_DIR / 'GAP.md'
        self.assertTrue(gap.is_file(),
                        'the skill reaches for claim-notes editing that no verb provides '
                        'but records no GAP.md')
        gap_text = gap.read_text(encoding='utf-8').lower()
        self.assertIn('notes', gap_text)
        self.assertIn('fha claim', gap_text)

    def test_a_value_only_edit_really_does_leave_the_old_reviewed_date(self):
        """The premise of this whole class, proven against the real CLI.

        Two runs on one fixture claim: `--value` alone keeps the old
        `reviewed:`; `--status accepted --value` re-stamps it. If claim.py ever
        starts re-stamping on a bare field edit, this test fails and the prose
        rule above can be relaxed deliberately rather than by drift.
        """
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / 'archive'
            (archive / 'sources' / 'census').mkdir(parents=True)
            (archive / 'fha.yaml').write_text(
                'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
            record = archive / 'sources' / 'census' / 'c_S-wc00000001.md'
            record.write_text(
                '---\n'
                'id: S-wc00000001\n'
                'title: Fixture\n'
                'source_type: census\n'
                '---\n\n'
                '## Claims\n'
                '```yaml\n'
                '- id: C-wc00000001\n'
                '  type: birth\n'
                '  value: Sue walkie\n'
                '  status: accepted\n'
                '  reviewed: 2026-06-24\n'
                '```\n', encoding='utf-8')

            def run(argv):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    rc = claim._standalone_main(argv + ['--root', str(archive)])
                return rc, out.getvalue()

            rc, _ = run(['C-wc00000001', '--value', 'Suwalki'])
            self.assertEqual(rc, 0)
            after = record.read_text(encoding='utf-8')
            self.assertIn('value: Suwalki', after)
            self.assertIn('reviewed: 2026-06-24', after)  # the stale signature

            rc, _ = run(['C-wc00000001', '--status', 'accepted',
                         '--value', 'Suwalki', '--reviewed', '2026-08-16'])
            self.assertEqual(rc, 0)
            after = record.read_text(encoding='utf-8')
            self.assertIn('reviewed: 2026-08-16', after)
            self.assertNotIn('reviewed: 2026-06-24', after)


if __name__ == '__main__':
    unittest.main()
