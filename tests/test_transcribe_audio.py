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
