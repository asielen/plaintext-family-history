"""Tests for the `import-recordings` skill's own scripts and its SKILL.md contract.

A SKILL.md is prose and has no unit test (`_STANDARD.md` §9) - but the two Python
scripts it shells DO, and so does the agreement between what the prose promises
and what those scripts default to. That agreement is where this skill went wrong
before: the doc advertised a 0.90 speaker-confidence gate while the script shipped
0.35, so anyone following the documented command got segments labelled that the
safety contract promised to leave unknown.

What is covered here:

  1. DOC/CODE AGREEMENT - the confidence gate, the mispair gate and the timestamp
     coverage gate that SKILL.md and TOOLING_INTERFACE.md advertise are the
     scripts' actual defaults, and the documented Stage B command really does
     rely on those defaults (it passes no --min-confidence).
  2. DOC/CLI AGREEMENT - every `fha` verb and every script flag written in a
     SKILL.md code fence exists. A workflow that stops at a parser error is a
     broken workflow.
  3. THE GATES THEMSELVES - the 80% timestamp gate is a true fraction (4 of 6
     turns must fail it); an untimed turn between two timed ones blanks its span
     instead of being absorbed into the previous speaker's interval; and a
     transcript that stops halfway through the recording cannot label the half
     it never reached. Coverage is about WHERE, not only how much: the same
     question is asked of the anchors that rescue a thin segment.
  4. OUTPUT SAFETY - no destination may collide with an input or with the other
     destination; a run that refuses or attributes nothing writes neither output
     and leaves an earlier result byte for byte; nothing written to disk carries
     an absolute machine path (AGENTS_TOOLING.md §11).
  4b. THE READ-ONLY PROMISE - the dedupe check writes exactly one path,
     `--json`, and that path is refused before a single byte is hashed if it
     resolves onto an incoming recording, an archived one, or `fha.yaml`. Its
     temporary file never outlives a failed write.
  4c. VALIDATE, THEN DERIVE - the workflow settles the recording's timezone
     before it converts a UTC instant into a calendar date, and writes an
     uncertain interval rather than an exact day it cannot justify.
  5. THE DEDUPE SCRIPT - size-then-SHA-256 finds a byte-identical twin under a
     configured (including external) documents root, clears a file that only
     shares a size, and never modifies the archive. It is a safety gate, so it
     fails closed: anything it could not read comes back `indeterminate` and
     nonzero rather than "new", and every path it reports is written under the
     name of the root holding it - never an absolute path, never a `../..` climb,
     never a bare filename that cannot say which file matched.

Run: python -m unittest tests.test_import_recordings -v   (from the repo root)
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / '.claude' / 'skills' / 'import-recordings'
SKILL_MD = SKILL_DIR / 'SKILL.md'
SCRIPTS = SKILL_DIR / 'scripts'


def _load(name, path):
    """Import a skill script by path - `.claude/skills/` is not on sys.path."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


attribute_speakers = _load('attribute_speakers', SCRIPTS / 'attribute_speakers.py')
find_duplicate_media = _load('find_duplicate_media', SCRIPTS / 'find_duplicate_media.py')


def run_script(module, argv):
    """Call a script's main() in-process, returning (exit_code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = module.main(argv)
    return code, out.getvalue(), err.getvalue()


def whisper_text(rows):
    """A whisper transcript: (clock, text) pairs in the `**[HH:MM:SS]**` shape."""
    return '\n'.join('**[%s]** %s' % (clock, text) for clock, text in rows) + '\n'


def app_numbered(turns):
    """The app's numbered export: index / Speaker N / MM:SS / text.

    `clock=None` writes the turn with no timestamp line - the export shape that
    used to let the previous speaker's interval swallow the missing turn.
    """
    lines = []
    for n, (speaker, clock, text) in enumerate(turns, start=1):
        lines.append(str(n))
        lines.append('Speaker %d' % speaker)
        if clock is not None:
            lines.append(clock)
        lines.append(text)
        lines.append('')
    return '\n'.join(lines) + '\n'


def app_bracket(turns):
    """The app's bracket export: [Speaker N] then that turn's text."""
    lines = []
    for speaker, text in turns:
        lines.append('[Speaker %d]' % speaker)
        lines.append(text)
        lines.append('')
    return '\n'.join(lines) + '\n'


def fenced_blocks(markdown_text):
    """Every ``` fenced block body in a markdown file.

    Fences inside a numbered step are indented, so the opener and closer are
    matched with leading whitespace allowed - without that this returns nothing
    and every drift guard built on it passes vacuously.
    """
    return re.findall(r'^[ \t]*```[^\n]*\n(.*?)^[ \t]*```',
                      markdown_text, re.S | re.M)


class Turn(object):
    """The minimum of attribute_speakers.Turn that the interval maths reads."""

    def __init__(self, speaker, start):
        self.speaker = speaker
        self.start = start
        self.tokens = []


# ---------------------------------------------------------------------------
# 1. Documented gates are the shipped defaults
# ---------------------------------------------------------------------------
class DocumentedGatesTest(unittest.TestCase):
    """The numbers in the prose are the numbers in the code."""

    def setUp(self):
        self.skill = SKILL_MD.read_text(encoding='utf-8')
        self.tooling = (ROOT / 'TOOLING_INTERFACE.md').read_text(encoding='utf-8')

    def test_confidence_gate_default_is_the_documented_090(self):
        self.assertAlmostEqual(attribute_speakers.DEFAULT_MIN_CONFIDENCE, 0.90)

    def test_mispair_gate_default_is_the_documented_050(self):
        self.assertAlmostEqual(attribute_speakers.DEFAULT_MIN_MATCH_RATE, 0.50)

    def test_timestamp_coverage_gate_is_the_documented_080(self):
        self.assertAlmostEqual(attribute_speakers.TIME_MIN_TURN_COVERAGE, 0.80)

    def test_skill_md_states_the_090_gate(self):
        self.assertIn('0.90', self.skill)
        self.assertIn('--min-confidence`, default 0.90', self.skill)

    def test_tooling_interface_states_the_090_gate(self):
        entry = [ln for ln in self.tooling.splitlines()
                 if ln.startswith('- `import-recordings`')]
        self.assertEqual(len(entry), 1, 'the import-recordings design entry moved')
        self.assertIn('0.90', entry[0])

    def test_documented_stage_b_command_relies_on_the_default(self):
        """The worked invocation must not pass --min-confidence.

        This is the exact drift the reviewer caught: as long as the standard
        command omits the flag, the script's default IS the safety contract, so
        a future edit that lowers the default has to fail a test rather than
        quietly re-open the gate.
        """
        calls = [line
                 for block in fenced_blocks(self.skill)
                 for line in block.splitlines()
                 if 'attribute_speakers.py' in line]
        self.assertTrue(calls, 'SKILL.md no longer shows the attribute_speakers call')
        for line in calls:
            self.assertNotIn('--min-confidence', line)
            self.assertNotIn('--min-match-rate', line)
            self.assertNotIn('--force', line)

    def test_decide_labels_at_the_gate_and_refuses_just_below_it(self):
        """The gate value is enforced where it is read, not just where it is set."""
        for weight, expect_label in ((9.5, True), (9.0, True), (8.5, False)):
            seg = attribute_speakers.Segment()
            seg.idx = 0
            seg.tokens = ['w'] * 10
            seg.t0, seg.t1 = 0, 10
            votes = [attribute_speakers.Counter({'Speaker 1': weight})]
            attribute_speakers.decide(
                [seg], votes, None, [(0, 0), (9, 9)],
                ['Speaker 1'] * 10, attribute_speakers.DEFAULT_MIN_CONFIDENCE)
            self.assertEqual(seg.speaker is not None, expect_label,
                             'weight %.1f/10 decided wrongly (reason %r)'
                             % (weight, seg.reason))


# ---------------------------------------------------------------------------
# 2. Every documented command and flag exists
# ---------------------------------------------------------------------------
class DocumentedCommandsExistTest(unittest.TestCase):
    """A workflow that stops at a parser error is a broken workflow."""

    def setUp(self):
        self.skill = SKILL_MD.read_text(encoding='utf-8')

    def test_every_fha_verb_in_a_code_fence_is_registered(self):
        verbs = set()
        for block in fenced_blocks(self.skill):
            for line in block.splitlines():
                m = re.match(r'^\s*(?:\./|\.\\)?fha\s+([a-z][a-z-]*)', line)
                if m:
                    verbs.add(m.group(1))
        self.assertTrue(verbs, 'SKILL.md shows no fha commands at all')
        for verb in sorted(verbs):
            proc = subprocess.run(
                [sys.executable, str(ROOT / 'tools' / 'fha.py'), verb, '--help'],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0,
                             'SKILL.md tells the agent to run `fha %s`, which fha '
                             'does not register:\n%s' % (verb, proc.stderr))

    def test_every_flag_on_this_skills_own_scripts_exists(self):
        parsers = {
            'attribute_speakers.py': attribute_speakers.build_parser(),
            'find_duplicate_media.py': find_duplicate_media.build_parser(),
        }
        checked = 0
        for block in fenced_blocks(self.skill):
            for line in block.splitlines():
                for name, parser in parsers.items():
                    if name not in line:
                        continue
                    known = set()
                    for action in parser._actions:
                        known.update(action.option_strings)
                    for flag in re.findall(r'(?<![\w-])--[a-z][a-z-]*', line):
                        self.assertIn(flag, known,
                                      '%s is documented with %s, which its parser '
                                      'does not accept' % (name, flag))
                        checked += 1
        self.assertGreater(checked, 0, 'no documented script flags were found to check')

    def test_dedupe_step_runs_the_hash_script_not_a_text_search(self):
        """The mandatory dedupe stage must prove bytes, not similar prose."""
        step = self.skill.split('3. **Content-hash', 1)[1].split('\n4. ', 1)[0]
        self.assertIn('find_duplicate_media.py', step)
        self.assertTrue((SCRIPTS / 'find_duplicate_media.py').is_file())

    def test_every_script_referenced_in_the_skill_exists_on_disk(self):
        for path in re.findall(r'\.claude/skills/[a-z-]+/scripts/[a-z_]+\.py', self.skill):
            self.assertTrue((ROOT / path).is_file(),
                            'SKILL.md references %s, which does not exist' % path)


# ---------------------------------------------------------------------------
# 3. The timestamp gate and the blind-span rule
# ---------------------------------------------------------------------------
class TimestampCoverageGateTest(unittest.TestCase):
    """80% means 80%, not `int(0.8 * n)`."""

    def _turns(self, timed, total):
        return ([Turn('Speaker 1', float(k)) for k in range(timed)]
                + [Turn('Speaker 2', None) for _ in range(total - timed)])

    def test_four_of_six_turns_is_sixty_seven_percent_and_fails(self):
        self.assertFalse(attribute_speakers.timestamp_coverage_ok(self._turns(4, 6)))

    def test_seven_of_nine_turns_is_seventy_eight_percent_and_fails(self):
        self.assertFalse(attribute_speakers.timestamp_coverage_ok(self._turns(7, 9)))

    def test_exactly_eighty_percent_passes(self):
        self.assertTrue(attribute_speakers.timestamp_coverage_ok(self._turns(8, 10)))
        self.assertTrue(attribute_speakers.timestamp_coverage_ok(self._turns(12, 15)))

    def test_a_handful_of_turns_still_needs_three_timestamps(self):
        self.assertFalse(attribute_speakers.timestamp_coverage_ok(self._turns(2, 2)))
        self.assertTrue(attribute_speakers.timestamp_coverage_ok(self._turns(3, 3)))

    def test_no_turns_at_all(self):
        self.assertFalse(attribute_speakers.timestamp_coverage_ok([]))


class BlindSpanTest(unittest.TestCase):
    """A dropped middle turn must not extend the previous speaker's interval."""

    def test_untimed_turn_between_two_timed_ones_blanks_the_span(self):
        turns = [
            Turn('Speaker 1', 0.0),
            Turn('Speaker 2', 10.0),
            Turn('Speaker 1', None),      # the turn with no timestamp
            Turn('Speaker 2', 30.0),
            Turn('Speaker 1', 40.0),
        ]
        intervals = attribute_speakers.speaker_intervals(turns, audio_end=50.0)
        self.assertEqual(
            intervals,
            [(0.0, 10.0, 'Speaker 1'),
             (10.0, 30.0, None),          # blind: the boundary is somewhere in here
             (30.0, 40.0, 'Speaker 2'),
             # The last turn ends when its own words run out (these test turns
             # carry no tokens, so the 1.0s floor), NOT at audio_end - see
             # UncoveredTailTest for what extending it to 50.0 used to do.
             (40.0, 41.0, 'Speaker 1')])

    def test_a_fully_timed_export_has_no_blind_spans(self):
        turns = [Turn('Speaker 1', 0.0), Turn('Speaker 2', 10.0), Turn('Speaker 1', 20.0)]
        intervals = attribute_speakers.speaker_intervals(turns, audio_end=30.0)
        self.assertEqual([spk for _s, _e, spk in intervals],
                         ['Speaker 1', 'Speaker 2', 'Speaker 1'])

    def test_a_trailing_untimed_turn_blanks_the_tail(self):
        turns = [Turn('Speaker 1', 0.0), Turn('Speaker 2', 10.0), Turn('Speaker 1', None)]
        intervals = attribute_speakers.speaker_intervals(turns, audio_end=30.0)
        self.assertIsNone(intervals[-1][2])

    def test_no_time_votes_are_cast_inside_a_blind_span(self):
        """The end-to-end consequence: nobody wins the missing turn's segment."""
        rows = [('00:00:00', 'first speaker opening the conversation'),
                ('00:00:10', 'second speaker replying at some length'),
                ('00:00:20', 'the missing middle turn nobody timestamped'),
                ('00:00:30', 'second speaker again after the gap'),
                ('00:00:40', 'first speaker closing the conversation')]
        segments, _stream, _owner = attribute_speakers.parse_whisper(
            whisper_text(rows).splitlines())
        turns = [
            Turn('Speaker 1', 0.0),
            Turn('Speaker 2', 10.0),
            Turn('Speaker 1', None),
            Turn('Speaker 2', 30.0),
            Turn('Speaker 1', 40.0),
        ]
        for seg, turn in zip(segments, turns):
            turn.tokens = list(seg.tokens)
        votes, used = attribute_speakers.collect_time_votes(
            segments, turns, len(segments), [])
        # Segment 2 (00:00:20) sits wholly inside the blind span.
        self.assertEqual(dict(votes[2]), {})
        self.assertFalse(used[2])
        # Segment 1 (00:00:10) also starts inside it, so it wins no votes either -
        # rather than being handed to Speaker 2 at full strength.
        self.assertNotIn('Speaker 2', votes[1])
        # The spans that are genuinely known still vote.
        self.assertEqual(max(votes[3], key=votes[3].get), 'Speaker 2')
        self.assertEqual(max(votes[4], key=votes[4].get), 'Speaker 1')

    def test_gappy_export_disables_the_timestamp_path_entirely(self):
        """Below the 80% gate there is no interval evidence at all."""
        rows = [('00:00:%02d' % (k * 10), 'some words for turn number %d here' % k)
                for k in range(6)]
        segments, _stream, _owner = attribute_speakers.parse_whisper(
            whisper_text(rows).splitlines())
        turns = [Turn('Speaker 1', 0.0), Turn('Speaker 2', 10.0),
                 Turn('Speaker 1', None), Turn('Speaker 2', None),
                 Turn('Speaker 1', 40.0), Turn('Speaker 2', 50.0)]
        self.assertIsNone(attribute_speakers.collect_time_votes(
            segments, turns, len(segments), []))


class UncoveredTailTest(unittest.TestCase):
    """An app transcript that stops early must not own the rest of the audio.

    Same class of bug as the blind span above, at the end of the file where
    there is no next turn to blank it: a count of timed turns cannot see WHERE
    the timing stops, and a transcript timed to 51s of a 100s recording passes
    every count-based gate there is.
    """

    def test_the_last_interval_stops_with_the_turn_not_with_the_audio(self):
        turns = [Turn('Speaker 1', 0.0), Turn('Speaker 2', 10.0),
                 Turn('Speaker 1', 51.0)]
        turns[2].tokens = ['just', 'a', 'few', 'closing', 'words']
        intervals = attribute_speakers.speaker_intervals(turns, audio_end=103.0)
        last_start, last_end, last_speaker = intervals[-1]
        self.assertEqual(last_speaker, 'Speaker 1')
        self.assertEqual(last_start, 51.0)
        self.assertLess(last_end, 60.0,
                        'the final interval was stretched to the end of the audio')

    def test_a_half_timed_export_switches_the_timestamp_path_off(self):
        """51s of a 100s recording: 100% of turns timed, half the audio covered."""
        rows = [('00:00:00', 'we lived on the farm out past the creek for years'),
                ('00:00:12', 'and how long were you there grandpa exactly'),
                ('00:00:25', 'nineteen years give or take a hard winter'),
                ('00:00:38', 'did you ever think about leaving the place'),
                ('00:00:51', 'never once not for a single day of it')]
        tail = [('00:01:%02d' % (k * 10), 'a later passage nobody timed at all '
                                          'number %d' % k) for k in range(1, 6)]
        segments, _stream, _owner = attribute_speakers.parse_whisper(
            whisper_text(rows + tail).splitlines())
        turns = []
        for speaker, (clock, text) in zip((2, 1, 2, 1, 2), rows):
            t = Turn('Speaker %d' % speaker,
                     attribute_speakers.parse_clock(clock[3:]))
            t.tokens = attribute_speakers.tokenize(text)
            turns.append(t)
        warnings = []
        self.assertIsNone(
            attribute_speakers.collect_time_votes(
                segments, turns, len(segments), warnings),
            'timestamp evidence was trusted for audio the app never reached')
        self.assertTrue(any('timed turns stop' in w for w in warnings), warnings)

    def test_the_uncovered_tail_goes_out_unlabelled(self):
        """End to end: the tail keeps the plain form, whatever the first half does."""
        tmp = Path(tempfile.mkdtemp(prefix='import-recordings-tail-'))
        self.addCleanup(shutil.rmtree, tmp, True)
        rows = [('00:00:00', 'we lived on the farm out past the creek for years'),
                ('00:00:12', 'and how long were you there grandpa exactly'),
                ('00:00:25', 'nineteen years give or take a hard winter'),
                ('00:00:38', 'did you ever think about leaving the place'),
                ('00:00:51', 'never once not for a single day of it')]
        tail = [('00:01:00', 'the mill burned down the summer after the war ended'),
                ('00:01:20', 'and your mother kept the ledgers for the whole county'),
                ('00:01:40', 'nobody else could read her handwriting afterwards')]
        whisper = tmp / 'w.md'
        app = tmp / 'a.txt'
        out = tmp / 'o.md'
        whisper.write_text(whisper_text(rows + tail), encoding='utf-8')
        app.write_text(app_numbered([
            (2, '00:00', rows[0][1]), (1, '00:12', rows[1][1]),
            (2, '00:25', rows[2][1]), (1, '00:38', rows[3][1]),
            (2, '00:51', rows[4][1]),
        ]), encoding='utf-8')
        code, _stdout, _err = run_script(attribute_speakers, [
            '--whisper', str(whisper), '--app-transcript', str(app),
            '--out', str(out), '--quiet'])
        self.assertEqual(code, 0)
        written = out.read_text(encoding='utf-8').splitlines()
        for line in written:
            if line.startswith('**[00:01:'):
                self.assertNotIn('Speaker', line,
                                 'a segment the app transcript never reached was '
                                 'labelled anyway')


class EnclosingAgreeWindowTest(unittest.TestCase):
    """A thin segment is only rescued by anchors that are actually near it."""

    def test_distant_anchors_do_not_rescue_a_thin_segment(self):
        pair_js = [0, 5000]
        pair_speakers = ['Speaker 1', 'Speaker 1']
        self.assertFalse(
            attribute_speakers.enclosing_agree(pair_js, pair_speakers,
                                               2000, 2010, 'Speaker 1'),
            'two matched tokens thousands of tokens away were read as agreement')

    def test_near_anchors_still_rescue_it(self):
        pair_js = [1995, 2015]
        pair_speakers = ['Speaker 1', 'Speaker 1']
        self.assertTrue(
            attribute_speakers.enclosing_agree(pair_js, pair_speakers,
                                               2000, 2010, 'Speaker 1'))


class ConfidenceBucketTest(unittest.TestCase):
    """Exact tenths land in the bucket a reader would name for them."""

    def test_exact_tenths_are_not_misfiled_by_float_modulo(self):
        self.assertEqual(attribute_speakers.confidence_bucket(0.3), '0.3-0.4')
        self.assertEqual(attribute_speakers.confidence_bucket(0.7), '0.7-0.8')
        self.assertEqual(attribute_speakers.confidence_bucket(0.9), '0.9-1.0')

    def test_the_ends_of_the_range(self):
        self.assertEqual(attribute_speakers.confidence_bucket(0.0), '0.0-0.1')
        self.assertEqual(attribute_speakers.confidence_bucket(1.0), '0.9-1.0')
        self.assertEqual(attribute_speakers.confidence_bucket(0.35), '0.3-0.4')


# ---------------------------------------------------------------------------
# 4. Output safety
# ---------------------------------------------------------------------------
class AttributeSpeakersOutputSafetyTest(unittest.TestCase):
    """Nothing this script writes may destroy something it was given."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='import-recordings-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.whisper = self.tmp / 'session-whisper.md'
        self.app = self.tmp / 'session-app.txt'
        rows = [('00:00:00', 'we lived on the farm out past the creek'),
                ('00:00:06', 'and how long were you there grandpa'),
                ('00:00:11', 'nineteen years give or take a winter')]
        self.whisper.write_text(whisper_text(rows), encoding='utf-8')
        self.app.write_text(app_bracket([
            (2, 'we lived on the farm out past the creek'),
            (1, 'and how long were you there grandpa'),
            (2, 'nineteen years give or take a winter'),
        ]), encoding='utf-8')
        self.out = self.tmp / 'session-attributed.md'
        self.report = self.tmp / 'session.speakers.json'

    def _run(self, extra=()):
        return run_script(attribute_speakers, [
            '--whisper', str(self.whisper),
            '--app-transcript', str(self.app),
            '--out', str(self.out),
            '--report', str(self.report),
            '--quiet',
        ] + list(extra))

    def test_happy_path_writes_both_files_and_leaves_inputs_untouched(self):
        before = (self.whisper.read_bytes(), self.app.read_bytes())
        code, _out, _err = self._run()
        self.assertEqual(code, 0)
        self.assertTrue(self.out.is_file())
        self.assertTrue(self.report.is_file())
        self.assertEqual((self.whisper.read_bytes(), self.app.read_bytes()), before)

    def test_out_equal_to_report_is_refused_before_anything_is_written(self):
        """The finding: --out overwritten by --report, exit 0, "wrote" printed."""
        same = self.tmp / 'session.md'
        code, out, err = run_script(attribute_speakers, [
            '--whisper', str(self.whisper), '--app-transcript', str(self.app),
            '--out', str(same), '--report', str(same), '--quiet'])
        self.assertEqual(code, 1)
        self.assertIn('same file', err)
        self.assertIn('--report', err)
        self.assertFalse(same.exists(), 'a refused run must write nothing at all')
        self.assertNotIn('wrote', out)

    def test_out_equal_to_report_is_caught_through_different_spellings(self):
        same = self.tmp / 'session.md'
        code, _out, err = run_script(attribute_speakers, [
            '--whisper', str(self.whisper), '--app-transcript', str(self.app),
            '--out', str(same),
            '--report', os.path.join(str(self.tmp), '.', 'session.md'), '--quiet'])
        self.assertEqual(code, 1)
        self.assertIn('same file', err)
        self.assertFalse(same.exists())

    def test_an_output_pointed_at_an_input_is_refused(self):
        before = self.whisper.read_bytes()
        for flag in ('--out', '--report'):
            argv = ['--whisper', str(self.whisper), '--app-transcript', str(self.app),
                    '--out', str(self.out), '--report', str(self.report), '--quiet']
            argv[argv.index(flag) + 1] = str(self.whisper)
            code, _out, err = run_script(attribute_speakers, argv)
            self.assertEqual(code, 1)
            self.assertIn('inputs are never modified', err)
            self.assertEqual(self.whisper.read_bytes(), before)

    def test_refusing_leaves_a_pre_existing_transcript_byte_identical(self):
        self.out.write_text('a transcript from an earlier run\n', encoding='utf-8')
        before = self.out.read_bytes()
        code, _out, _err = run_script(attribute_speakers, [
            '--whisper', str(self.whisper), '--app-transcript', str(self.app),
            '--out', str(self.out), '--report', str(self.out), '--quiet'])
        self.assertEqual(code, 1)
        self.assertEqual(self.out.read_bytes(), before)

    def test_replacing_an_existing_output_says_so(self):
        self.out.write_text('stale\n', encoding='utf-8')
        code, _out, err = self._run()
        self.assertEqual(code, 0)
        self.assertIn('already exists and is being replaced', err)

    def test_no_temporary_files_are_left_behind(self):
        self._run()
        leftovers = [p.name for p in self.tmp.iterdir() if '.tmp-' in p.name]
        self.assertEqual(leftovers, [])

    def test_report_records_no_absolute_machine_paths(self):
        """AGENTS_TOOLING.md §11: this JSON can be filed beside the recording."""
        self._run()
        text = self.report.read_text(encoding='utf-8')
        self.assertNotIn(str(self.tmp), text)
        report = json.loads(text)
        self.assertEqual(report['inputs']['whisper'], 'session-whisper.md')
        self.assertEqual(report['inputs']['app_transcript'], 'session-app.txt')
        self.assertEqual(report['output'], 'session-attributed.md')
        for value in (report['inputs']['whisper'], report['output']):
            self.assertFalse(os.path.isabs(value))

    def test_report_settings_echo_the_documented_gate(self):
        self._run()
        report = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertAlmostEqual(report['settings']['min_confidence'], 0.90)
        self.assertAlmostEqual(report['settings']['min_match_rate'], 0.50)

    def test_a_missing_input_names_the_next_step_and_writes_nothing(self):
        code, _out, err = run_script(attribute_speakers, [
            '--whisper', str(self.tmp / 'nope.md'), '--app-transcript', str(self.app),
            '--out', str(self.out), '--quiet'])
        self.assertEqual(code, 1)
        self.assertIn('run the command again', err)
        self.assertFalse(self.out.exists())

    def _mispair(self):
        self.app.write_text(app_bracket([
            (1, 'entirely unrelated material about shipping schedules in rotterdam'),
            (2, 'nothing whatever to do with any farm or creek or winter'),
        ]), encoding='utf-8')

    def test_a_mispaired_transcript_refuses_to_label_and_exits_two(self):
        self._mispair()
        code, _out, err = self._run()
        self.assertEqual(code, 2)
        self.assertIn('refusing to label', err)
        self.assertFalse(self.out.exists(),
                         'a refused run publishes nothing at all')
        self.assertFalse(self.report.exists())

    def test_a_refused_run_leaves_an_earlier_result_byte_identical(self):
        """The finding: the mispair gate set exit 2, then published anyway.

        The user picks the wrong app transcript by mistake, is told the run was
        refused, and finds his attributed transcript replaced by an unlabelled
        copy of the whisper input.
        """
        self.out.write_text('**[00:00:00] Speaker 2:** a good earlier result\n',
                            encoding='utf-8')
        self.report.write_text('{"status": "ok"}\n', encoding='utf-8')
        before = (self.out.read_bytes(), self.report.read_bytes())
        self._mispair()
        code, _out, err = self._run()
        self.assertEqual(code, 2)
        self.assertIn('nothing was written', err)
        self.assertEqual((self.out.read_bytes(), self.report.read_bytes()), before)

    def test_a_run_that_attributes_nothing_does_not_replace_a_good_result(self):
        """Same class: a paragraph-only app export used to overwrite at exit 0."""
        self.out.write_text('**[00:00:00] Speaker 2:** a good earlier result\n',
                            encoding='utf-8')
        before = self.out.read_bytes()
        self.app.write_text('just paragraphs of text with nobody named at all\n',
                            encoding='utf-8')
        code, _out, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn('no speaker labels', err)
        self.assertIn('run the command again', err)
        self.assertEqual(self.out.read_bytes(), before)

    def test_a_timestamped_export_uses_the_interval_path_as_documented(self):
        """SKILL.md step 7: "one question saves the whole gamble"."""
        rows = [('00:00:00', 'we lived on the farm out past the creek for years'),
                ('00:00:06', 'and how long were you there grandpa exactly'),
                ('00:00:11', 'nineteen years give or take a hard winter'),
                ('00:00:17', 'did you ever think about leaving the place'),
                ('00:00:23', 'never once not for a single day of it')]
        self.whisper.write_text(whisper_text(rows), encoding='utf-8')
        self.app.write_text(app_numbered([
            (2, '00:00', 'we lived on the farm out past the creek for years'),
            (1, '00:06', 'and how long were you there grandpa exactly'),
            (2, '00:11', 'nineteen years give or take a hard winter'),
            (1, '00:17', 'did you ever think about leaving the place'),
            (2, '00:23', 'never once not for a single day of it'),
        ]), encoding='utf-8')
        code, _out, _err = self._run()
        self.assertEqual(code, 0)
        report = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertEqual(report['method'], 'align+time')
        self.assertEqual(report['app_transcript']['variant'], 'numbered')
        self.assertTrue(report['app_transcript']['has_timestamps'])
        self.assertGreater(report['labelling']['labelled'], 0)
        self.assertIn('**[00:00:00] Speaker 2:**',
                      self.out.read_text(encoding='utf-8'))

    def test_an_export_with_no_speaker_labels_degrades_gracefully(self):
        """"Two transcripts is a complete, correct result" - SKILL.md step 7."""
        self.app.write_text('just paragraphs of text with nobody named at all\n\n'
                            'and a second paragraph carrying on the same way\n',
                            encoding='utf-8')
        code, _out, err = self._run()
        self.assertEqual(code, 0)
        self.assertIn('no speaker labels', err)
        self.assertNotIn('Speaker', self.out.read_text(encoding='utf-8'))
        report = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'no_speaker_labels')

    def test_a_human_written_label_is_never_overwritten(self):
        self.whisper.write_text(
            '**[00:00:00] Thomas Hartley:** we lived on the farm out past the creek\n'
            '**[00:00:06]** and how long were you there grandpa\n'
            '**[00:00:11]** nineteen years give or take a winter\n',
            encoding='utf-8')
        code, _out, _err = self._run()
        self.assertEqual(code, 0)
        self.assertIn('**[00:00:00] Thomas Hartley:**',
                      self.out.read_text(encoding='utf-8'))


# ---------------------------------------------------------------------------
# 5. The duplicate check
# ---------------------------------------------------------------------------
class FindDuplicateMediaTest(unittest.TestCase):
    """Size first, SHA-256 second, and never a write."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='import-recordings-dedupe-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.archive = self.tmp / 'archive'
        self.filed = self.archive / 'documents' / 'interviews' / 'hartley-1998-06-14'
        self.filed.mkdir(parents=True)
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  photos: photos\n  documents: documents\n', encoding='utf-8')
        self.original = self.filed / 'hartley-1998-06-14_S-wb91h3hjrr.m4a'
        self.original.write_bytes(b'the same bytes as the phone still holds')
        self.incoming = self.tmp / 'incoming'
        self.incoming.mkdir()

    def _run(self, *extra):
        return run_script(find_duplicate_media,
                          [str(self.incoming), '--root', str(self.archive)] + list(extra))

    def test_a_byte_identical_repeat_is_reported_with_its_twin_and_exits_two(self):
        twin = self.incoming / 'Thursday at 3-11 PM.m4a'
        twin.write_bytes(self.original.read_bytes())
        code, out, _err = self._run()
        self.assertEqual(code, 2)
        self.assertIn('DUPLICATE', out)
        self.assertIn('hartley-1998-06-14_S-wb91h3hjrr.m4a', out)
        self.assertIn('S-wb91h3hjrr', out)

    def test_a_genuinely_new_recording_is_cleared_and_exits_zero(self):
        (self.incoming / 'new-sitting.m4a').write_bytes(b'quite different bytes here')
        code, out, _err = self._run()
        self.assertEqual(code, 0)
        self.assertIn('new', out)
        self.assertNotIn('DUPLICATE', out)

    def test_same_size_different_content_is_not_a_duplicate(self):
        """Equal size is common and proves nothing; only the hash decides."""
        same_size = self.incoming / 'coincidence.m4a'
        payload = self.original.read_bytes()
        same_size.write_bytes(b'X' + payload[1:])
        self.assertEqual(same_size.stat().st_size, self.original.stat().st_size)
        code, out, _err = self._run()
        self.assertEqual(code, 0)
        self.assertNotIn('DUPLICATE', out)

    def test_an_external_documents_root_is_still_searched(self):
        """`roots:` may point outside the archive - a hardcoded path finds nothing."""
        external = self.tmp / 'elsewhere' / 'FamilyDocuments'
        external.mkdir(parents=True)
        shutil.move(str(self.archive / 'documents'), str(external / 'documents'))
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  documents: %s\n' % (external / 'documents').as_posix(),
            encoding='utf-8')
        twin = self.incoming / 'Thursday at 3-11 PM.m4a'
        twin.write_bytes(b'the same bytes as the phone still holds')
        code, out, _err = self._run()
        self.assertEqual(code, 2, out)
        self.assertIn('DUPLICATE', out)

    def test_it_writes_nothing_into_the_archive(self):
        twin = self.incoming / 'Thursday at 3-11 PM.m4a'
        twin.write_bytes(self.original.read_bytes())
        before = {p.relative_to(self.archive).as_posix(): p.read_bytes()
                  for p in self.archive.rglob('*') if p.is_file()}
        incoming_before = twin.read_bytes()
        self._run()
        after = {p.relative_to(self.archive).as_posix(): p.read_bytes()
                 for p in self.archive.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(twin.read_bytes(), incoming_before)

    def test_json_output_carries_no_absolute_machine_paths(self):
        twin = self.incoming / 'Thursday at 3-11 PM.m4a'
        twin.write_bytes(self.original.read_bytes())
        report = self.tmp / 'dedupe.json'
        self._run('--json', str(report), '--quiet')
        text = report.read_text(encoding='utf-8')
        self.assertNotIn(str(self.tmp), text)
        payload = json.loads(text)
        dup = payload['results'][0]['duplicates'][0]
        self.assertEqual(dup['archived_path'],
                         'documents/interviews/hartley-1998-06-14/'
                         'hartley-1998-06-14_S-wb91h3hjrr.m4a')
        self.assertEqual(dup['source_id'], 'S-wb91h3hjrr')

    def test_no_archive_above_the_given_folder_names_the_fix(self):
        loose = self.tmp / 'loose'
        loose.mkdir()
        (loose / 'a.m4a').write_bytes(b'x')
        code, _out, err = run_script(find_duplicate_media,
                                     [str(loose), '--root', str(loose)])
        self.assertEqual(code, 1)
        self.assertIn('--root', err)
        self.assertIn('--media-root', err)

    def test_a_path_that_does_not_exist_is_refused_plainly(self):
        code, _out, err = run_script(find_duplicate_media,
                                     [str(self.tmp / 'ghost.m4a')])
        self.assertEqual(code, 1)
        self.assertIn('not found', err)

    def test_non_media_files_are_ignored(self):
        (self.incoming / 'notes.txt').write_bytes(self.original.read_bytes())
        code, _out, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn('audio or video', err)

    def test_the_source_id_reader_matches_the_filename_grammar(self):
        self.assertEqual(
            find_duplicate_media.source_id_in('hartley-1998-06-14_S-wb91h3hjrr.m4a'),
            'S-wb91h3hjrr')
        self.assertEqual(
            find_duplicate_media.source_id_in(
                'hartley-1998-06-14-farm-audio_S-wb91h3hjrr.m4a'),
            'S-wb91h3hjrr')
        self.assertIsNone(find_duplicate_media.source_id_in('Thursday at 3-11 PM.m4a'))

    def test_hashing_is_whole_file_not_a_prefix(self):
        """Two recordings can share a long header and differ only at the end."""
        head = b'H' * 4096
        a = self.incoming / 'a.m4a'
        a.write_bytes(head + b'ending one')
        b = self.filed / 'b_S-aa11bb22cc.m4a'
        b.write_bytes(head + b'ending two')
        code, out, _err = self._run()
        self.assertEqual(code, 0, out)
        self.assertNotEqual(
            find_duplicate_media.sha256_file(str(a), {}),
            find_duplicate_media.sha256_file(str(b), {}))
        self.assertEqual(
            find_duplicate_media.sha256_file(str(a), {}),
            hashlib.sha256(a.read_bytes()).hexdigest())


class DedupeFailsClosedTest(unittest.TestCase):
    """The gate authorises imports, so anything it could not read is not "new"."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='import-recordings-closed-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.archive = self.tmp / 'archive'
        self.filed = self.archive / 'documents' / 'interviews'
        self.filed.mkdir(parents=True)
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        self.original = self.filed / 'hartley-1998-06-14_S-wb91h3hjrr.m4a'
        self.original.write_bytes(b'the same bytes as the phone still holds')
        self.incoming = self.tmp / 'incoming'
        self.incoming.mkdir()
        self.twin = self.incoming / 'Thursday at 3-11 PM.m4a'
        self.twin.write_bytes(self.original.read_bytes())

    def _run(self, *extra):
        return run_script(find_duplicate_media,
                          [str(self.incoming), '--root', str(self.archive)]
                          + list(extra))

    def test_a_candidate_that_cannot_be_opened_is_not_a_cleared_file(self):
        """Unit: a same-size archived file that vanished mid-run is an open question."""
        entry = find_duplicate_media.check_one(
            str(self.twin),
            {self.twin.stat().st_size: [str(self.filed / 'gone-away.m4a')]},
            {})
        self.assertEqual(entry['verdict'], 'indeterminate')
        self.assertEqual(len(entry['unchecked']), 1)

    def test_an_unreadable_candidate_exits_nonzero_and_clears_nothing(self):
        """The finding: the drive holding the twin is offline, so import it twice."""
        real = find_duplicate_media.sha256_file

        def offline(path, cache):
            if 'documents' in str(path):
                raise OSError(5, 'Input/output error')
            return real(path, cache)

        find_duplicate_media.sha256_file = offline
        self.addCleanup(setattr, find_duplicate_media, 'sha256_file', real)
        code, out, _err = self._run()
        self.assertEqual(code, 3, out)
        self.assertIn('UNCHECKED', out)
        self.assertNotIn('new  ', out)
        self.assertIn('cleared for import', out)

    def test_an_unreadable_archived_file_stops_every_clearance(self):
        """A file nobody could size might be the twin of any of them, so none clear."""
        (self.incoming / 'something-else.m4a').write_bytes(b'entirely other bytes')
        real = find_duplicate_media.index_sizes_by_root

        def partial(named_roots):
            by_size, _unreadable = real(named_roots)
            return by_size, [str(self.filed / 'unreadable.m4a')]

        find_duplicate_media.index_sizes_by_root = partial
        self.addCleanup(setattr, find_duplicate_media,
                        'index_sizes_by_root', real)
        code, out, err = self._run()
        self.assertEqual(code, 3, out)
        self.assertIn('could not be read', err)
        self.assertIn('documents/interviews/unreadable.m4a', err)

    def test_a_media_root_that_is_not_mounted_refuses_the_run(self):
        """An external documents root that is offline is not an empty archive."""
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  documents: %s\n'
            % (self.tmp / 'not-plugged-in' / 'documents').as_posix(),
            encoding='utf-8')
        code, _out, err = self._run()
        self.assertEqual(code, 1)
        self.assertIn('not there right now', err)
        self.assertIn('run the command again', err)

    def test_an_unparseable_fha_yaml_refuses_rather_than_guessing(self):
        """Falling back to <archive>/documents searches the wrong folder.

        The archive here keeps its recordings on an external drive named in
        fha.yaml, and the file will not parse. Guessing the built-in default
        walks an empty `<archive>/documents`, finds no twin, and reports a
        byte-identical recording as new - a clean exit 0 saying "safe to import".
        """
        external = self.tmp / 'elsewhere' / 'FamilyDocuments'
        external.mkdir(parents=True)
        shutil.move(str(self.original), str(external / self.original.name))
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  documents: "%s\n   nonsense: [\n' % external.as_posix(),
            encoding='utf-8')
        code, out, err = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn('fha doctor', err)
        self.assertNotIn('new  ', out)


class DedupePathReportingTest(unittest.TestCase):
    """One representation everywhere: named root plus the path under it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='import-recordings-paths-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.payload = b'the same bytes as the phone still holds'
        self.incoming = self.tmp / 'incoming'
        (self.incoming / 'monday').mkdir(parents=True)
        (self.incoming / 'tuesday').mkdir(parents=True)

    def _json(self, argv):
        report = self.tmp / 'dedupe.json'
        code, out, err = run_script(find_duplicate_media,
                                    argv + ['--json', str(report), '--quiet'])
        text = report.read_text(encoding='utf-8')
        self.assertNotIn(str(self.tmp), text, 'a machine path reached the report')
        self.assertNotIn('..', text, 'a ../.. climb reached the report')
        return code, json.loads(text), out, err

    def test_an_external_documents_root_keeps_its_alias_not_a_dotdot_climb(self):
        """os.path.relpath succeeds for an external root - with `../../home/...`."""
        archive = self.tmp / 'archive'
        (archive / 'people').mkdir(parents=True)
        external = self.tmp / 'elsewhere' / 'FamilyDocuments'
        filed = external / 'interviews' / 'hartley-1998-06-14'
        filed.mkdir(parents=True)
        (filed / 'hartley-1998-06-14_S-wb91h3hjrr.m4a').write_bytes(self.payload)
        (archive / 'fha.yaml').write_text(
            'roots:\n  documents: %s\n' % external.as_posix(), encoding='utf-8')
        (self.incoming / 'monday' / 'rec.m4a').write_bytes(self.payload)
        code, payload, _out, _err = self._json(
            [str(self.incoming), '--root', str(archive)])
        self.assertEqual(code, 2)
        self.assertEqual(payload['media_roots'], ['documents'])
        self.assertEqual(
            payload['results'][0]['duplicates'][0]['archived_path'],
            'documents/interviews/hartley-1998-06-14/'
            'hartley-1998-06-14_S-wb91h3hjrr.m4a')

    def test_media_root_mode_keeps_the_folder_the_twin_was_found_in(self):
        """The other finding: basenames alone cannot say which file matched."""
        library = self.tmp / 'FamilyMedia'
        (library / '2019' / 'june').mkdir(parents=True)
        (library / '2021').mkdir(parents=True)
        (library / '2019' / 'june' / 'recording.m4a').write_bytes(self.payload)
        (library / '2021' / 'recording.m4a').write_bytes(b'a different afternoon')
        (self.incoming / 'monday' / 'recording.m4a').write_bytes(self.payload)
        code, payload, _out, _err = self._json(
            [str(self.incoming), '--media-root', str(library)])
        self.assertEqual(code, 2)
        self.assertEqual(payload['media_roots'], ['FamilyMedia'])
        self.assertEqual(
            payload['results'][0]['duplicates'][0]['archived_path'],
            'FamilyMedia/2019/june/recording.m4a')

    def test_two_incoming_files_with_one_name_stay_distinguishable(self):
        library = self.tmp / 'FamilyMedia'
        library.mkdir()
        (library / 'filed.m4a').write_bytes(self.payload)
        (self.incoming / 'monday' / 'New Recording 4.m4a').write_bytes(self.payload)
        (self.incoming / 'tuesday' / 'New Recording 4.m4a').write_bytes(b'other')
        code, payload, _out, _err = self._json(
            [str(self.incoming), '--media-root', str(library)])
        self.assertEqual(code, 2)
        self.assertEqual(
            sorted(r['path'] for r in payload['results']),
            ['incoming/monday/New Recording 4.m4a',
             'incoming/tuesday/New Recording 4.m4a'])

    def test_the_console_names_paths_the_same_way_the_report_does(self):
        """One representation everywhere, not one for the file and one for print."""
        library = self.tmp / 'FamilyMedia'
        (library / '2019').mkdir(parents=True)
        (library / '2019' / 'filed.m4a').write_bytes(self.payload)
        (self.incoming / 'monday' / 'rec.m4a').write_bytes(self.payload)
        (self.incoming / 'tuesday' / 'other.m4a').write_bytes(b'not a twin at all')
        code, out, _err = run_script(
            find_duplicate_media,
            [str(self.incoming), '--media-root', str(library)])
        self.assertEqual(code, 2)
        self.assertNotIn(str(self.tmp), out)
        self.assertIn('incoming/monday/rec.m4a', out)
        self.assertIn('FamilyMedia/2019/filed.m4a', out)
        self.assertIn('new        incoming/tuesday/other.m4a', out)


def _filesystem_folds_case(directory):
    """Does this filesystem treat `A.json` and `a.json` as one file?

    The collision check leans on os.path.normcase, which is a no-op on Linux and
    a fold on Windows and macOS - so the end-to-end case test can only run where
    the platform actually folds. Probed rather than guessed from sys.platform,
    because a case-insensitive volume mounted on Linux is a real thing.
    """
    probe = os.path.join(str(directory), 'CaseProbe.tmp')
    with open(probe, 'w', encoding='utf-8') as fh:
        fh.write('x')
    try:
        return os.path.exists(os.path.join(str(directory), 'caseprobe.tmp'))
    finally:
        os.remove(probe)


class DedupeReportPathSafetyTest(unittest.TestCase):
    """The one path this read-only check writes must never be a recording.

    The finding: `--json` was written with no collision check at all. It is
    written LAST, so a report path that resolved onto an incoming recording
    destroyed that recording after it had been hashed, compared, and printed as
    `new` - the run announced a file was safe to import in the same breath as it
    overwrote it. Pointed at an archived candidate, the same typo destroyed a
    filed original instead, and both exited 0 or 2 as if nothing had happened.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='import-recordings-report-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.archive = self.tmp / 'archive'
        self.filed = self.archive / 'documents' / 'interviews'
        self.filed.mkdir(parents=True)
        self.config = self.archive / 'fha.yaml'
        self.config.write_text('roots:\n  documents: documents\n', encoding='utf-8')
        self.original = self.filed / 'hartley-1998-06-14_S-wb91h3hjrr.m4a'
        self.original.write_bytes(b'the same bytes as the phone still holds')
        self.incoming = self.tmp / 'incoming'
        self.incoming.mkdir()
        self.twin = self.incoming / 'Thursday at 3-11 PM.m4a'
        self.twin.write_bytes(self.original.read_bytes())

    def _run(self, *extra):
        return run_script(find_duplicate_media,
                          [str(self.incoming), '--root', str(self.archive)]
                          + list(extra))

    def _refuse_to_hash(self):
        """Make any hashing an outright test failure, to prove ordering.

        `check_one` catches OSError, so an exception it does not catch is the
        only honest way to assert that the refusal happens BEFORE the first read.
        """
        real = find_duplicate_media.sha256_file

        def never(path, cache):
            raise AssertionError('a file was hashed before the --json path was '
                                 'checked: %s' % path)

        find_duplicate_media.sha256_file = never
        self.addCleanup(setattr, find_duplicate_media, 'sha256_file', real)

    def test_json_over_an_incoming_recording_is_refused_before_hashing(self):
        self._refuse_to_hash()
        before = self.twin.read_bytes()
        code, out, err = self._run('--json', str(self.twin), '--quiet')
        self.assertEqual(code, 1, out)
        self.assertIn('--json', err)
        self.assertIn('incoming/Thursday at 3-11 PM.m4a', err)
        self.assertIn('run the command again', err)
        self.assertEqual(self.twin.read_bytes(), before,
                         'the report was written over the recording it checked')

    def test_json_over_an_archived_recording_is_refused_before_hashing(self):
        self._refuse_to_hash()
        before = self.original.read_bytes()
        code, out, err = self._run('--json', str(self.original), '--quiet')
        self.assertEqual(code, 1, out)
        self.assertIn('documents/interviews/hartley-1998-06-14_S-wb91h3hjrr.m4a', err)
        self.assertEqual(self.original.read_bytes(), before,
                         'the report replaced a recording already filed')

    def test_json_over_fha_yaml_is_refused(self):
        before = self.config.read_bytes()
        code, out, err = self._run('--json', str(self.config), '--quiet')
        self.assertEqual(code, 1, out)
        self.assertIn('fha.yaml', err)
        self.assertEqual(self.config.read_bytes(), before)

    def test_a_dot_slash_spelling_is_the_same_file(self):
        before = self.twin.read_bytes()
        sneaky = os.path.join(str(self.incoming), '.', self.twin.name)
        code, out, err = self._run('--json', sneaky, '--quiet')
        self.assertEqual(code, 1, out)
        self.assertIn('--json', err)
        self.assertEqual(self.twin.read_bytes(), before)

    def test_a_symlink_to_a_recording_is_the_same_file(self):
        link = self.tmp / 'report-link.json'
        try:
            os.symlink(str(self.twin), str(link))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest('this platform will not create a symlink')
        before = self.twin.read_bytes()
        code, out, err = self._run('--json', str(link), '--quiet')
        self.assertEqual(code, 1, out)
        self.assertIn('--json', err)
        self.assertEqual(self.twin.read_bytes(), before)

    def test_a_case_variant_is_the_same_file_where_the_platform_folds(self):
        if not _filesystem_folds_case(self.tmp):
            self.skipTest('this filesystem is case-sensitive, so the two names '
                          'really are two different files')
        before = self.twin.read_bytes()
        shouty = str(self.twin).upper()
        code, out, err = self._run('--json', shouty, '--quiet')
        self.assertEqual(code, 1, out)
        self.assertEqual(self.twin.read_bytes(), before)

    def test_canonical_path_folds_exactly_what_the_platform_folds(self):
        """The unit under all of the above, checked on every platform."""
        canonical = find_duplicate_media.canonical_path
        plain = str(self.twin)
        dotted = os.path.join(str(self.incoming), '.', self.twin.name)
        self.assertEqual(canonical(plain), canonical(dotted))
        self.assertEqual(canonical(plain) == canonical(plain.upper()),
                         os.path.normcase('A') == 'a')

    def test_json_anywhere_inside_a_media_root_is_refused(self):
        """A filed transcript is an original too, and carries no media extension.

        It is therefore invisible to the size index, so the checks that name a
        specific recording cannot see it. The net under them is that nothing
        this script writes belongs inside a media root at all.
        """
        inside = self.filed / 'dedupe-report.json'
        code, out, err = self._run('--json', str(inside), '--quiet')
        self.assertEqual(code, 1, out)
        self.assertIn('documents', err)
        self.assertIn('never writes to the archive', err)
        self.assertFalse(inside.exists())

    def test_a_report_path_of_its_own_still_writes_normally(self):
        """The refusal must not be so broad that the flag stops working."""
        report = self.tmp / 'dedupe-report.json'
        code, out, _err = self._run('--json', str(report), '--quiet')
        self.assertEqual(code, 2, out)
        payload = json.loads(report.read_text(encoding='utf-8'))
        self.assertEqual(payload['duplicates'], 1)

    def test_a_failed_report_write_leaves_no_temporary_file_behind(self):
        """A half-written `.tmp-1234` in his folder is a mystery, not a report."""
        blocked = self.tmp / 'a-folder-not-a-file'
        blocked.mkdir()
        code, _out, err = self._run('--json', str(blocked), '--quiet')
        self.assertEqual(code, 1)
        self.assertIn('could not write', err)
        leftovers = [p.name for p in self.tmp.iterdir() if '.tmp-' in p.name]
        self.assertEqual(leftovers, [])


class RecordingDateOrderingTest(unittest.TestCase):
    """The date is derived only after the evidence for it is in hand.

    The finding: step 4 converted a UTC `creation_time` to a local calendar date
    using whatever timezone the processing machine happened to be in, and only
    then asked the human a question - so an interview recorded two zones away
    could receive a wrong exact `source_date` with nothing anywhere saying it was
    a guess. Ordering is the whole fix: establish the recording's own offset
    first, and where it cannot be established, write the uncertain form.
    """

    def setUp(self):
        self.skill = SKILL_MD.read_text(encoding='utf-8')
        self.step4 = self.skill.split(
            '\n4. **Read the real recording date', 1)[1].split('\n5. ', 1)[0]

    def _convert_at(self):
        where = self.step4.find('`creation_time` − duration')
        self.assertNotEqual(where, -1,
                            'step 4 no longer shows the creation_time arithmetic')
        return where

    def test_the_human_is_asked_for_the_timezone_before_the_conversion(self):
        ask = self.step4.lower().find('where was this recorded')
        self.assertNotEqual(ask, -1,
                            'step 4 never asks which timezone the recording was '
                            'made in, so the conversion has nothing to stand on')
        self.assertLess(ask, self._convert_at(),
                        'the timezone question is asked after the date has '
                        'already been converted - the wrong exact date is on '
                        'the page by then')

    def test_the_containers_own_offset_is_read_before_the_conversion(self):
        tag = self.step4.find('com.apple.quicktime.creationdate')
        self.assertNotEqual(tag, -1,
                            "step 4 never reads the container's local timestamp, "
                            'which is the one source that settles the timezone '
                            'without asking anybody')
        self.assertLess(tag, self._convert_at())

    def test_an_unsettled_timezone_produces_an_interval_not_an_exact_day(self):
        unknown = re.search(r'timezone is still unknown', self.step4)
        self.assertIsNotNone(
            unknown,
            'step 4 has no branch for a timezone it could not establish, so it '
            'writes an exact date either way')
        self.assertNotEqual(self.step4.find('1998-06-14/1998-06-15', unknown.start()), -1,
                            'the unknown-timezone branch does not name the EDTF '
                            'interval it should write instead')

    def test_the_old_after_the_fact_midnight_question_is_gone(self):
        """The exact sentence that asked its question too late.

        Matched across a line wrap, because the sentence it replaces was split
        over two lines - a plain substring check would pass on the broken text
        and prove nothing.
        """
        self.assertIsNone(
            re.search(r'was this the evening of\s+the 14th', self.step4),
            'step 4 still asks its one question only after converting the date')

    def test_the_record_says_which_timezone_was_used(self):
        """An exact date is only honest if the reader can redo the arithmetic."""
        notes = self.skill.split('10. **Fill in the source record', 1)[1]
        self.assertIn('timezone you actually used', notes)

    def test_an_interval_source_date_is_legitimate_in_the_record(self):
        step10 = self.skill.split('10. **Fill in the source record', 1)[1] \
                           .split('\n11. ', 1)[0]
        self.assertIn('1998-06-14/1998-06-15', step10)

    def test_grouping_waits_for_a_settled_date_and_the_slug_never_rounds_it(self):
        step5 = self.skill.split('\n5. **Group by sitting', 1)[1].split('\n6. ', 1)[0]
        self.assertIn('interval is not grouped by guess', step5)
        self.assertIn('Never let a tidy folder name talk you into a tidy', step5)

    def test_the_skill_states_the_json_report_path_rule(self):
        step3 = self.skill.split('3. **Content-hash', 1)[1].split('\n4. ', 1)[0]
        self.assertIn('--json', step3)
        self.assertIn('refuses the run', step3)


# ---------------------------------------------------------------------------
# 6. House style
# ---------------------------------------------------------------------------
class SkillScriptStyleTest(unittest.TestCase):
    """The owner's no-em-dash rule for source files (AGENTS_TOOLING.md)."""

    def test_new_script_has_no_em_dashes(self):
        """Newly authored source files use ` - `, never a dash character.

        Only the new script is checked: attribute_speakers.py carries legacy
        em dashes throughout, and the rule is to convert a line when you edit
        it, not to churn the whole file (AGENTS_TOOLING.md).
        """
        text = (SCRIPTS / 'find_duplicate_media.py').read_text(encoding='utf-8')
        self.assertNotIn('\u2014', text)   # em dash
        self.assertNotIn('\u2013', text)   # en dash


if __name__ == '__main__':
    unittest.main()
