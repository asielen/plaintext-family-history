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
     question is asked of the anchors that rescue a thin segment, and of the
     mispair gate, which measures the match against BOTH transcripts (matched
     words over the longer stream) and refuses a rate resting on fewer than
     MIN_MATCH_TOKENS words. Dividing by the shorter stream asked "is the small
     file contained in the big one" and let a 22-word app export take one
     whisper segment at confidence 1.00.
  4. OUTPUT SAFETY - no destination may collide with an input or with the other
     destination; a run that refuses or attributes nothing writes neither output
     and leaves an earlier result byte for byte; an existing --out or --report is
     refused rather than replaced, because the deterministic `<stem>.md` output
     name means a second run aims at a file a human may have corrected, and
     nothing here can tell that copy from the tool's own earlier output; the two
     overrides stay separate (--force is the mispair gate, --replace is the
     overwrite, and neither buys the other); nothing written to disk carries an
     absolute machine path (AGENTS_TOOLING.md §11).
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
from unittest import mock

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
            # Same rule for the overwrite override: the standard command must
            # not carry the flag that authorises replacing a corrected
            # transcript, or every rerun quietly destroys the last one.
            self.assertNotIn('--replace', line)

    def test_minimum_matched_words_floor_is_the_documented_twenty(self):
        self.assertEqual(attribute_speakers.MIN_MATCH_TOKENS, 20)

    def test_skill_md_states_the_two_sided_mispair_gate(self):
        self.assertIn('both', self.skill.lower())
        self.assertIn('20 matching words', self.skill)
        self.assertIn('--min-match-rate`, default 0.50', self.skill)

    def test_skill_md_documents_the_replace_refusal(self):
        """A flag the script needs and the prose never mentions is a trap."""
        self.assertIn('--replace', self.skill)
        self.assertIn('does not overwrite the first one', self.skill)

    def test_tooling_interface_states_the_replace_flag_and_the_gate(self):
        entry = [ln for ln in self.tooling.splitlines()
                 if ln.startswith('- `import-recordings`')]
        self.assertEqual(len(entry), 1, 'the import-recordings design entry moved')
        self.assertIn('--replace', entry[0])
        self.assertIn('20 matched words', entry[0])

    def test_replace_and_force_are_separate_flags_in_the_parser(self):
        """The decision itself: one flag must not mean two safety overrides."""
        flags = set()
        for action in attribute_speakers.build_parser()._actions:
            flags.update(action.option_strings)
        self.assertIn('--force', flags)
        self.assertIn('--replace', flags)

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


class MispairGateCoverageTest(unittest.TestCase):
    """The gate asks whether these are the same recording, of BOTH files.

    Dividing the matched tokens by the SMALLER stream asked a weaker question -
    "is the small file contained in the big one?" - and a tiny app export
    sharing one phrase with one whisper segment answered it perfectly: match
    rate 1.00, gate cleared, that segment published at confidence 1.00 with
    somebody else's name on it. The rate is now measured against the longer
    stream (equivalently: both coverages must clear the gate), and it must rest
    on a minimum number of matched words, because a percentage over five common
    words is not a measurement.
    """

    def test_a_short_subset_no_longer_scores_a_perfect_match(self):
        ev = attribute_speakers.mispair_evidence(25, 25, 250)
        self.assertAlmostEqual(ev['app_coverage'], 1.0)
        self.assertAlmostEqual(ev['whisper_coverage'], 0.1)
        self.assertAlmostEqual(ev['match_rate'], 0.1)
        self.assertFalse(attribute_speakers.mispair_gate_ok(ev, 0.50))

    def test_the_rate_is_the_matched_tokens_over_the_longer_stream(self):
        for matched, n_app, n_wh in ((30, 40, 100), (30, 100, 40), (50, 50, 50)):
            ev = attribute_speakers.mispair_evidence(matched, n_app, n_wh)
            self.assertAlmostEqual(ev['match_rate'],
                                   matched / float(max(n_app, n_wh)))

    def test_a_genuine_pair_still_passes(self):
        """70-83% on both sides is what a correct pairing measures."""
        ev = attribute_speakers.mispair_evidence(800, 1000, 1050)
        self.assertTrue(attribute_speakers.mispair_gate_ok(ev, 0.50))

    def test_exactly_the_gate_passes(self):
        ev = attribute_speakers.mispair_evidence(30, 30, 60)
        self.assertAlmostEqual(ev['match_rate'], 0.50)
        self.assertTrue(attribute_speakers.mispair_gate_ok(ev, 0.50))

    def test_a_rate_resting_on_too_few_words_is_refused(self):
        """Two identical five-word files are 100% matched and prove nothing."""
        floor = attribute_speakers.MIN_MATCH_TOKENS
        thin = attribute_speakers.mispair_evidence(floor - 1, floor - 1, floor - 1)
        self.assertAlmostEqual(thin['match_rate'], 1.0)
        self.assertFalse(attribute_speakers.mispair_gate_ok(thin, 0.50))
        enough = attribute_speakers.mispair_evidence(floor, floor, floor)
        self.assertTrue(attribute_speakers.mispair_gate_ok(enough, 0.50))

    def test_the_refusal_reports_both_coverages_with_their_word_counts(self):
        """The one thing that tells a mispair from half an interview."""
        ev = attribute_speakers.mispair_evidence(25, 25, 250)
        said = attribute_speakers.mispair_sentence(ev, 0.50)
        self.assertIn('100.0%', said)
        self.assertIn('10.0%', said)
        self.assertIn('25', said)
        self.assertIn('250', said)

    def test_a_too_thin_refusal_says_it_cannot_tell_rather_than_quoting_a_rate(self):
        ev = attribute_speakers.mispair_evidence(4, 4, 400)
        said = attribute_speakers.mispair_sentence(ev, 0.50)
        self.assertIn('cannot tell', said)
        self.assertIn(str(attribute_speakers.MIN_MATCH_TOKENS), said)


class MispairGateEndToEndTest(unittest.TestCase):
    """The finding, played out: a short app export against a long recording."""

    SHARED = ('we lived on the farm out past the creek for years and then the '
              'winter came down hard on all of us')
    FILLER = ('ledger harbour mineral thicket paddock quarry driftwood lantern '
              'xylophone zenith marble trellis cobbler furnace wagon basket '
              'copper ridge shutter crimson beacon fennel')

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='import-recordings-mispair-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        rows = [('00:00:00', self.SHARED)]
        rows += [('00:00:%02d' % (10 * k), 'passage%d %s' % (k, self.FILLER))
                 for k in range(1, 10)]
        self.whisper = self.tmp / 'session-whisper.md'
        self.app = self.tmp / 'session-app.txt'
        self.out = self.tmp / 'session.md'
        self.report = self.tmp / 'session.speakers.json'
        self.whisper.write_text(whisper_text(rows), encoding='utf-8')
        # The app export holds ONE turn: 22 words that really are in the
        # recording. Every other word of the recording is unrelated to it.
        self.app.write_text(app_bracket([(1, self.SHARED)]), encoding='utf-8')

    def _run(self, extra=()):
        return run_script(attribute_speakers, [
            '--whisper', str(self.whisper), '--app-transcript', str(self.app),
            '--out', str(self.out), '--report', str(self.report),
            '--quiet'] + list(extra))

    def test_a_contained_short_export_is_refused_and_publishes_nothing(self):
        code, _out, err = self._run()
        self.assertEqual(code, 2, err)
        self.assertFalse(self.out.exists(),
                         'the wrong speaker was published on a five-word coincidence')
        self.assertFalse(self.report.exists())
        self.assertIn('refused to label', err)

    def test_the_refusal_names_the_partial_export_reading_and_the_override(self):
        _code, _out, err = self._run()
        self.assertIn('--force', err)
        self.assertIn('part', err)

    def test_force_still_labels_the_part_that_lines_up(self):
        """A genuinely partial export must remain possible, loudly."""
        code, _out, _err = self._run(['--force'])
        self.assertEqual(code, 0)
        written = self.out.read_text(encoding='utf-8')
        self.assertIn('**[00:00:00] Speaker 1:**', written)
        report = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertTrue(report['settings']['forced'])
        self.assertFalse(report['alignment']['mispair_gate_passed'])
        self.assertTrue(any('overridden with --force' in w
                            for w in report['warnings']), report['warnings'])

    def test_the_report_carries_the_number_the_gate_judged_on(self):
        code, _out, _err = self._run(['--force'])
        self.assertEqual(code, 0)
        alignment = json.loads(
            self.report.read_text(encoding='utf-8'))['alignment']
        self.assertAlmostEqual(alignment['match_rate_vs_app'], 1.0, places=2)
        self.assertLess(alignment['match_rate_vs_whisper'], 0.2)
        self.assertAlmostEqual(alignment['mispair_gate_rate'],
                               alignment['match_rate_vs_whisper'])


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

    def test_an_existing_output_is_refused_not_replaced(self):
        """The finding: an existing --out was a warning, and then it was gone.

        The Stage B command names its output `<stem>.md` every time, so the
        second run of a session lands on the first run's file - which by then
        may be the copy somebody corrected the speaker labels in. A warning
        queued after the fact does not save it; only refusing does.
        """
        self.out.write_text('**[00:00:00] Thomas Hartley:** a corrected label\n',
                            encoding='utf-8')
        before = self.out.read_bytes()
        code, out, err = self._run()
        self.assertEqual(code, 1)
        self.assertEqual(self.out.read_bytes(), before)
        self.assertFalse(self.report.exists(),
                         'the report was written for a run that refused')
        self.assertIn('session-attributed.md', err)   # names the file
        self.assertIn('--replace', err)               # and the exact next step
        self.assertNotIn('wrote', out)

    def test_an_existing_report_is_refused_too(self):
        """--report is the symmetric half: a JSON file is somebody's work too."""
        self.report.write_text('{"status": "reviewed by hand"}\n', encoding='utf-8')
        before = self.report.read_bytes()
        code, _out, err = self._run()
        self.assertEqual(code, 1)
        self.assertEqual(self.report.read_bytes(), before)
        self.assertFalse(self.out.exists())
        self.assertIn('session.speakers.json', err)
        self.assertIn('--replace', err)

    def test_replace_authorises_the_overwrite_and_records_it(self):
        self.out.write_text('an earlier attributed transcript\n', encoding='utf-8')
        code, _out, err = self._run(['--replace'])
        self.assertEqual(code, 0)
        self.assertNotEqual(self.out.read_text(encoding='utf-8'),
                            'an earlier attributed transcript\n')
        self.assertIn('--replace', err)
        report = json.loads(self.report.read_text(encoding='utf-8'))
        self.assertTrue(report['settings']['replaced_existing'])

    def test_force_does_not_authorise_an_overwrite(self):
        """Two safety questions, two flags.

        `--force` says "I know the pairing looks wrong, label anyway". It has
        never said "and throw away the transcript I corrected last week", and
        one flag meaning both would make the first admission buy the second.
        """
        self.out.write_text('**[00:00:00] Thomas Hartley:** a corrected label\n',
                            encoding='utf-8')
        before = self.out.read_bytes()
        code, _out, err = self._run(['--force'])
        self.assertEqual(code, 1)
        self.assertEqual(self.out.read_bytes(), before)
        self.assertIn('--replace', err)

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
        # --replace is passed so the run gets past the "that file already
        # exists" refusal and the MISPAIR gate is the thing under test. The two
        # answer different questions: permission to overwrite is not evidence
        # that these files belong together, so an authorised overwrite of a
        # mispaired run still writes nothing.
        code, _out, err = self._run(['--replace'])
        self.assertEqual(code, 2)
        self.assertIn('nothing was written', err)
        self.assertEqual((self.out.read_bytes(), self.report.read_bytes()), before)

    def test_a_run_that_attributes_nothing_does_not_replace_a_good_result(self):
        """Same class: a paragraph-only app export used to overwrite at exit 0.

        Run with --replace, so the answer cannot come from the new existence
        refusal: even when the human has said that file may go, an unlabelled
        pass-through copy is not what he agreed to put in its place.
        """
        self.out.write_text('**[00:00:00] Speaker 2:** a good earlier result\n',
                            encoding='utf-8')
        before = self.out.read_bytes()
        self.app.write_text('just paragraphs of text with nobody named at all\n',
                            encoding='utf-8')
        code, _out, err = self._run(['--replace'])
        self.assertEqual(code, 1)
        self.assertIn('no speaker labels', err)
        self.assertIn('run the command again', err)
        self.assertIn('--replace does not cover this', err)
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
        # Both roots exist because fha.yaml names both, and archive-template
        # ships both folders. A root the config names but the disk lacks is a
        # coverage gap, not an ordinary archive - DedupeCoverageTest covers it.
        (self.archive / 'photos').mkdir()
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

        def partial(named_roots, staging=None):
            by_size, _unreadable = real(named_roots, staging)
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

    def test_without_pyyaml_the_roots_are_refused_not_guessed(self):
        """The round-5 finding: the hand-rolled fallback read a subset of YAML.

        `roots: {documents: <external>}` is an ordinary inline mapping and a
        perfectly valid archive. The line-by-line fallback did not recognise
        it, returned an empty mapping, and `resolve_media_roots` then applied
        the built-in `<archive>/documents` default - the archive's own empty
        skeleton. The byte-identical twin below sits on the external root that
        was never searched, so the gate cleared it as new on exit 0.

        There is no correct guess to make here: a parser that reads part of a
        format cannot tell "no roots configured" from "roots I could not
        read". So PyYAML is required and its absence is refused, with the
        install command in the message.
        """
        external = self.tmp / 'elsewhere' / 'FamilyDocuments'
        external.mkdir(parents=True)
        shutil.move(str(self.original), str(external / self.original.name))
        (self.archive / 'fha.yaml').write_text(
            'roots: {documents: %s}\n' % external.as_posix(), encoding='utf-8')
        with mock.patch.dict(sys.modules, {'yaml': None}):
            code, out, err = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn('pip install pyyaml', err)
        self.assertNotIn('new  ', out)

    def test_the_plain_roots_form_is_refused_without_pyyaml_too(self):
        """Even the form the old fallback DID understand is now refused.

        Reading it by hand looks harmless on this shape, but the same code path
        is what silently returns {} on every shape it does not understand -
        and a gate that authorises imports cannot have a path that guesses.
        """
        with mock.patch.dict(sys.modules, {'yaml': None}):
            with self.assertRaises(find_duplicate_media.ConfigProblem) as caught:
                find_duplicate_media._roots_from_config(str(self.archive))
        self.assertIn('pip install pyyaml', str(caught.exception))

    def test_a_recording_filed_below_a_hidden_folder_is_still_a_duplicate(self):
        """The round-5 finding: the walk pruned every dot-prefixed directory.

        `documents/.private/` is a folder a human makes for exactly the
        material he is most careful about. Pruned from the walk, its recording
        was not in the size index and did not land in the unreadable list
        either, so an identical incoming file came back `new` on exit 0 while
        the script's own docstring promised every archived recording had been
        checked.
        """
        private = self.filed / '.private'
        private.mkdir()
        shutil.move(str(self.original), str(private / self.original.name))
        code, out, _err = self._run()
        self.assertEqual(code, 2, out)
        self.assertIn('DUPLICATE', out)
        self.assertIn('.private', out)

    def test_a_hidden_folder_in_the_incoming_bundle_is_still_checked(self):
        """The same rule on the incoming side, which the same walk decides.

        A recording the gate never lists is a recording it says nothing about,
        and `fha process` files the bundle either way.
        """
        self.twin.unlink()
        (self.incoming / 'plainly-new.m4a').write_bytes(b'entirely other bytes')
        hidden = self.incoming / '.old'
        hidden.mkdir()
        (hidden / 'Thursday at 3-11 PM.m4a').write_bytes(self.original.read_bytes())
        code, out, _err = self._run()
        self.assertEqual(code, 2, out)
        self.assertIn('checked 2 recording(s)', out)
        self.assertIn('DUPLICATE', out)


class DedupeCoverageTest(unittest.TestCase):
    """The question the gate must answer is a coverage question.

    Five review rounds each found a different route to a false `new`, and every
    one of them was the same shape: a path where the script examined less than
    the whole archive and still reported a positive result. "Did I find a twin?"
    is not the question - `new` is a claim about everything that was looked at,
    so the question is "did I examine everything I said I examined?".

    The dimensions of that claim, each with its own test below:

      1. ROOTS      every configured media root resolved and readable
      2. ENUMERATION every file under every root actually listed
      3. DOMAIN     the same media rule applied to both sides, and every named
                    input either checked or named as not checked
      4. IDENTITY   an input already living in a media root is already archived,
                    not a file with no twin
      5. CANDIDATES every same-size candidate hashed

    The last test in the class asserts the invariant itself over several
    sabotages at once: while any dimension is short, nothing is cleared.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='import-recordings-coverage-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.archive = self.tmp / 'archive'
        self.documents = self.archive / 'documents'
        self.photos = self.archive / 'photos'
        (self.documents / 'interviews').mkdir(parents=True)
        self.photos.mkdir(parents=True)
        self.payload = b'the same bytes as the phone still holds'
        self.original = (self.documents / 'interviews'
                         / 'hartley-1998-06-14_S-wb91h3hjrr.m4a')
        self.original.write_bytes(self.payload)
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n  photos: photos\n', encoding='utf-8')
        self.incoming = self.tmp / 'incoming'
        self.incoming.mkdir()
        self.twin = self.incoming / 'Thursday at 3-11 PM.m4a'
        self.twin.write_bytes(self.payload)

    def _run(self, *extra):
        return run_script(find_duplicate_media,
                          [str(self.incoming), '--root', str(self.archive)]
                          + list(extra))

    def _link_dir(self, target, link):
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest('this platform will not create a directory symlink')

    # -- 1. roots ----------------------------------------------------------
    def test_a_configured_internal_root_that_is_missing_refuses_the_run(self):
        """fha.yaml names `documents`; the folder was renamed; photos/ remains.

        An absent root that fha.yaml never mentions is an ordinary young
        archive - there is nothing filed there to hide. A root the archive
        explicitly configures is a statement that recordings live there, so its
        absence is a folder that moved, and the recordings in it are exactly
        what a `new` verdict claims to have ruled out.
        """
        shutil.move(str(self.documents), str(self.archive / 'documents-moved'))
        code, out, err = self._run()
        self.assertNotEqual(code, 0, out)
        self.assertNotIn('new  ', out)
        self.assertIn('documents', err)
        self.assertIn('run the command again', err)

    def test_an_unconfigured_absent_root_is_still_ordinary(self):
        """The other half: the refusal must not fire on a young archive."""
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        shutil.rmtree(str(self.photos))
        code, out, _err = self._run()
        self.assertEqual(code, 2, out)
        self.assertIn('DUPLICATE', out)

    # -- 2. enumeration ----------------------------------------------------
    def test_an_archived_recording_below_a_directory_symlink_is_still_found(self):
        """os.walk does not follow directory links, and said nothing about it.

        The subtree is not listed and does not land in the unreadable list
        either, so the twin below it is invisible and its incoming copy comes
        back `new` on exit 0 - a skipped subtree wearing a clean verdict.
        """
        real = self.archive / 'real-media'
        real.mkdir()
        shutil.move(str(self.original), str(real / self.original.name))
        self._link_dir(real, self.documents / 'linked')
        code, out, _err = self._run()
        self.assertEqual(code, 2, out)
        self.assertIn('DUPLICATE', out)

    def test_a_symlink_loop_is_walked_once_and_still_finds_the_twin(self):
        """Following links has its own failure mode; it must not be the cure."""
        real = self.archive / 'real-media'
        real.mkdir()
        shutil.move(str(self.original), str(real / self.original.name))
        self._link_dir(real, self.documents / 'linked')
        self._link_dir(self.documents, self.documents / 'loop')
        code, out, _err = self._run()
        self.assertEqual(code, 2, out)
        self.assertIn('DUPLICATE', out)

    def test_an_incoming_recording_below_a_directory_symlink_is_still_checked(self):
        """The symmetric half: the bundle walk decides what gets a verdict."""
        self.twin.unlink()
        (self.incoming / 'plainly-new.m4a').write_bytes(b'entirely other bytes')
        elsewhere = self.tmp / 'phone-export'
        elsewhere.mkdir()
        (elsewhere / 'Thursday at 3-11 PM.m4a').write_bytes(self.payload)
        self._link_dir(elsewhere, self.incoming / 'linked')
        code, out, _err = self._run()
        self.assertIn('checked 2 recording(s)', out)
        self.assertEqual(code, 2, out)

    def test_the_size_index_holds_every_media_file_the_root_can_reach(self):
        """The invariant itself: what the index holds equals what is on disk.

        Asserted as a set comparison rather than through a verdict, because a
        verdict test only catches the one file it happens to plant. Every
        awkward shape the walk has ever dropped is present here at once:
        a plain file, a dot-directory, a directory symlink, a symlink loop,
        and a non-media file that must NOT be indexed.
        """
        plain = self.original
        hidden = self.documents / '.private' / 'kept-back.m4a'
        hidden.parent.mkdir()
        hidden.write_bytes(b'a recording the human is careful about')
        real = self.archive / 'real-media'
        real.mkdir()
        linked = real / 'below-a-link.m4a'
        linked.write_bytes(b'a recording under a directory symlink')
        self._link_dir(real, self.documents / 'linked')
        self._link_dir(self.documents, self.documents / 'loop')
        (self.documents / 'interviews' / 'notes.txt').write_bytes(b'not a recording')

        by_size, unreadable = find_duplicate_media.index_sizes_by_root(
            [('documents', str(self.documents))])
        found = {find_duplicate_media.canonical_path(p)
                 for paths in by_size.values() for p in paths}
        expected = {find_duplicate_media.canonical_path(str(p))
                    for p in (plain, hidden, linked)}
        self.assertEqual(found, expected)
        self.assertEqual(unreadable, [])

    # -- 3. domain ---------------------------------------------------------
    def test_an_explicitly_named_non_media_file_gets_no_verdict(self):
        """A folder walk filters by extension; a named file did not.

        `notes.txt` was therefore counted as a checked recording and cleared as
        `new` - the gate answering for a file it has no index to answer from,
        since the archive side never lists a non-media file either.
        """
        self.twin.unlink()
        rec = self.incoming / 'plainly-new.m4a'
        rec.write_bytes(b'entirely other bytes')
        notes = self.incoming / 'notes.txt'
        notes.write_bytes(b'words about the sitting, not the sitting')
        code, out, _err = run_script(
            find_duplicate_media,
            [str(rec), str(notes), '--root', str(self.archive)])
        self.assertEqual(code, 0, out)
        self.assertIn('checked 1 recording(s)', out)
        self.assertNotIn('new        incoming/notes.txt', out)
        self.assertIn('notes.txt', out)

    def test_every_named_input_is_either_checked_or_named_as_not_checked(self):
        """The invariant: nothing the human handed over goes unmentioned."""
        self.twin.unlink()
        rec = self.incoming / 'plainly-new.m4a'
        rec.write_bytes(b'entirely other bytes')
        notes = self.incoming / 'transcript.txt'
        notes.write_bytes(b'words, not audio')
        report = self.tmp / 'dedupe.json'
        run_script(find_duplicate_media,
                   [str(rec), str(notes), '--root', str(self.archive),
                    '--json', str(report), '--quiet'])
        text = report.read_text(encoding='utf-8')
        self.assertNotIn(str(self.tmp), text, 'a machine path reached the report')
        payload = json.loads(text)
        mentioned = ' '.join([r['path'] for r in payload['results']]
                             + [str(x) for x in payload.get('not_checked', [])])
        for named in (rec, notes):
            self.assertIn(named.name, mentioned,
                          '%s was named on the command line and the report says '
                          'nothing about it' % named.name)
        # The other half of the same invariant: a verdict is only ever given to
        # a file the gate has an index to answer from, which is a media file.
        for result in payload['results']:
            self.assertTrue(find_duplicate_media.is_media(result['path']),
                            '%s was given a verdict, but the archive side never '
                            'lists a file like it, so there was nothing to '
                            'compare it against' % result['path'])

    # -- 4. identity -------------------------------------------------------
    def test_a_recording_already_filed_is_not_cleared_when_it_is_handed_back(self):
        """The self-exclusion filter could not tell two cases apart.

        A file is not its own duplicate - true - but an incoming argument that
        names a file already living in a media root is not an unmatched file
        either: it is the archived original. Excluding it left no candidates,
        and no candidates read as `new` on exit 0, authorising a second import
        of a recording that is already filed.
        """
        code, out, _err = run_script(
            find_duplicate_media,
            [str(self.original), '--root', str(self.archive)])
        self.assertEqual(code, 2, out)
        self.assertNotIn('new  ', out)
        self.assertIn('DUPLICATE', out)
        self.assertIn('already filed', out)
        self.assertIn('S-wb91h3hjrr', out)
        # Printed where it really sits. An `incoming` label for a folder that is
        # itself a media root would rename the archive's own folder on the very
        # line saying the file is already filed in it.
        self.assertIn('documents/interviews/hartley-1998-06-14_S-wb91h3hjrr.m4a',
                      out)
        self.assertNotIn('incoming/', out)

    def test_a_media_root_folder_handed_back_as_incoming_is_not_cleared(self):
        """The same mistake made with a folder instead of a file."""
        code, out, _err = run_script(
            find_duplicate_media,
            [str(self.documents), '--root', str(self.archive)])
        self.assertEqual(code, 2, out)
        self.assertNotIn('new  ', out)

    def test_a_link_into_the_archive_is_the_archives_own_copy(self):
        """Pointed at by another name, it is still the file that is filed."""
        link = self.tmp / 'shortcut.m4a'
        try:
            os.symlink(str(self.original), str(link))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest('this platform will not create a symlink')
        code, out, _err = run_script(
            find_duplicate_media, [str(link), '--root', str(self.archive)])
        self.assertEqual(code, 2, out)
        self.assertNotIn('new  ', out)

    def test_the_inbox_inside_a_media_root_is_not_the_archive(self):
        """Staged is not filed, even when staging sits inside the library.

        SPEC 12.4 allows `inbox: C:/Photos/_inbox`, and the capture flow hands
        this check the inbox itself. Reading "inside a media root" as "already
        filed" would answer the whole intake with "already filed, nothing to
        import" and stop the import that would file it.
        """
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n  photos: photos\n'
            '  inbox: photos/_inbox\n', encoding='utf-8')
        inbox = self.photos / '_inbox'
        inbox.mkdir()
        (inbox / 'new-sitting.m4a').write_bytes(b'an afternoon nobody has filed')
        code, out, _err = run_script(
            find_duplicate_media, [str(inbox), '--root', str(self.archive)])
        self.assertEqual(code, 0, out)
        self.assertIn('new', out)
        self.assertNotIn('already filed', out)

    def test_a_recording_only_staged_in_the_inbox_is_not_a_filed_twin(self):
        """The same rule on the index side: a staged file has no S-id."""
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n  photos: photos\n'
            '  inbox: photos/_inbox\n', encoding='utf-8')
        inbox = self.photos / '_inbox'
        inbox.mkdir()
        staged = b'an afternoon nobody has filed'
        (inbox / 'new-sitting.m4a').write_bytes(staged)
        self.twin.write_bytes(staged)
        code, out, _err = self._run()
        self.assertEqual(code, 0, out)
        self.assertNotIn('DUPLICATE', out)

    def test_the_inbox_is_still_the_inbox_when_the_walk_arrives_by_a_link(self):
        """Staged is not filed however the walk got there.

        The exclusion is by folder identity now rather than by path prefix, so
        this pins the half that a prefix test would have missed: a link inside
        the documents root that lands on the inbox, and a link that lands on a
        folder inside it. Reading either as filed would answer the whole
        capture workflow with "already filed, nothing to import".
        """
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n  photos: photos\n'
            '  inbox: photos/_inbox\n', encoding='utf-8')
        inbox = self.photos / '_inbox'
        (inbox / 'monday').mkdir(parents=True)
        staged = b'an afternoon nobody has filed'
        (inbox / 'monday' / 'new-sitting.m4a').write_bytes(staged)
        self._link_dir(inbox, self.documents / 'staged')
        self._link_dir(inbox / 'monday', self.documents / 'staged-monday')
        self.twin.write_bytes(staged)
        code, out, _err = self._run()
        self.assertEqual(code, 0, out)
        self.assertNotIn('DUPLICATE', out)

    def test_one_recording_handed_over_twice_in_one_batch_is_imported_once(self):
        """The bundle can repeat itself, and that is the same harm.

        The origin story of this script is a phone export that named one
        afternoon three relative-weekday names. Checked only against the
        archive, all three are honestly `new` - and importing all three gives
        one recording three source records with its claims split between them.
        """
        self.twin.unlink()
        payload = b'one afternoon, exported under two names'
        (self.incoming / 'Recording 4.m4a').write_bytes(payload)
        (self.incoming / 'Thursday at 3-11 PM.m4a').write_bytes(payload)
        code, out, _err = self._run()
        self.assertEqual(code, 2, out)
        self.assertEqual(out.count('new        '), 1, out)
        self.assertIn('in the same batch', out)
        self.assertIn('incoming/Recording 4.m4a', out)

    def test_two_different_recordings_of_one_size_are_not_repeats(self):
        """Equal size proves nothing here either - only the hash decides."""
        self.twin.unlink()
        (self.incoming / 'one.m4a').write_bytes(b'first afternoon.....')
        (self.incoming / 'two.m4a').write_bytes(b'second afternoon....')
        code, out, _err = self._run()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.count('new        '), 2, out)

    # -- 5. the invariant, over every dimension at once ---------------------
    def test_nothing_is_cleared_while_any_part_went_unexamined(self):
        """One assertion, every coverage dimension: short means not cleared.

        Each sabotage below removes a different part of what `new` claims to
        have covered. None of them may produce exit 0, and none of them may
        print a `new` verdict, no matter which dimension went short.
        """
        real_index = find_duplicate_media.index_sizes_by_root
        real_hash = find_duplicate_media.sha256_file

        def unreadable_entry():
            def partial(named_roots, staging=None):
                by_size, _ = real_index(named_roots, staging)
                return by_size, [str(self.documents / 'unreadable.m4a')]
            find_duplicate_media.index_sizes_by_root = partial
            self.addCleanup(setattr, find_duplicate_media,
                            'index_sizes_by_root', real_index)

        def unhashable_candidate():
            def offline(path, cache):
                if 'documents' in str(path):
                    raise OSError(5, 'Input/output error')
                return real_hash(path, cache)
            find_duplicate_media.sha256_file = offline
            self.addCleanup(setattr, find_duplicate_media,
                            'sha256_file', real_hash)

        def configured_root_gone():
            shutil.move(str(self.documents), str(self.archive / 'documents-moved'))

        for name, sabotage in (('an archived file nobody could read',
                                unreadable_entry),
                               ('a same-size candidate nobody could hash',
                                unhashable_candidate),
                               ('a configured root that is not there',
                                configured_root_gone)):
            with self.subTest(gap=name):
                sabotage()
                try:
                    code, out, _err = self._run()
                    self.assertNotEqual(code, 0, out)
                    self.assertNotIn('new  ', out)
                finally:
                    find_duplicate_media.index_sizes_by_root = real_index
                    find_duplicate_media.sha256_file = real_hash
                    moved = self.archive / 'documents-moved'
                    if moved.is_dir():
                        shutil.move(str(moved), str(self.documents))


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

    Probed rather than guessed from sys.platform, because a case-insensitive
    volume mounted on Linux is a real thing and a case-sensitive one on macOS
    is a supported option.

    The collision check no longer needs this: it folds case on every platform
    on purpose, so its own tests run everywhere. What still needs it is the
    other direction - asserting that `same_file` tells two genuinely distinct
    files apart requires a filesystem on which `A.m4a` and `a.m4a` can BE two
    files.
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

    def test_a_case_variant_of_a_recording_is_refused_on_every_platform(self):
        """The finding: `normcase` folds case on Windows and nowhere else.

        The canonicaliser's docstring promised macOS case equivalence and used
        an operation that does not provide it, so on a case-insensitive APFS or
        HFS+ volume `--json Interview.m4a` beside an incoming `interview.m4a`
        compared unequal, passed the collision check, and had the recording
        destroyed by the final os.replace - after that recording had been
        hashed and printed as safe to import.

        There is no skip on this test. Where the volume folds, the two names
        are one file and `samefile` says so; where it does not, the blunt key
        says so anyway, because a name this refuses that was really free costs
        one more word on the command line and a name it clears that was not
        costs the recording.
        """
        before = self.twin.read_bytes()
        shouty = str(self.twin).upper()
        code, out, err = self._run('--json', shouty, '--quiet')
        self.assertEqual(code, 1, out)
        self.assertIn('--json', err)
        self.assertEqual(self.twin.read_bytes(), before,
                         'the report was written over the recording it checked')

    def test_a_case_variant_of_an_archived_recording_is_refused_by_name(self):
        """The same fold on the archive side, where the file is an original.

        An archived path was always caught by SOMETHING, because everything
        filed is inside a media root and the last-resort net refuses the whole
        root. What the fold buys here is the message: "this is the recording
        you would destroy" instead of "not in that folder, please", which is
        the difference between a human who understands what nearly happened
        and one who picks a second name at random.
        """
        before = self.original.read_bytes()
        shouty = str(self.original.parent / self.original.name.upper())
        code, out, err = self._run('--json', shouty, '--quiet')
        self.assertEqual(code, 1, out)
        self.assertIn('already filed in the archive', err)
        self.assertIn('documents/interviews/hartley-1998-06-14_S-wb91h3hjrr.m4a',
                      err)
        self.assertEqual(self.original.read_bytes(), before)

    def test_a_hard_link_to_a_recording_is_the_same_file(self):
        """One inode, two directory entries, and no string can tell.

        A symlink is resolved by `realpath`, so the old string comparison
        happened to catch it. A hard link is the same file with no arrow to
        follow: the two paths are unrelated as text, and only (device, inode)
        - `os.path.samefile` - answers. This is the arm of the check that runs
        on a case-sensitive filesystem too, so it is a real test here rather
        than one that has to be taken on trust.
        """
        link = self.tmp / 'second-name.m4a'
        try:
            os.link(str(self.twin), str(link))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest('this platform will not create a hard link')
        before = self.twin.read_bytes()
        code, out, err = self._run('--json', str(link), '--quiet')
        self.assertEqual(code, 1, out)
        self.assertIn('--json', err)
        self.assertEqual(link.read_bytes(), before,
                         'the report was written over the recording under its '
                         'other name')

    def test_an_accent_spelled_the_other_way_is_the_same_file(self):
        """macOS stores directory entries decomposed; humans type composed.

        `Grand-mère.m4a` written NFC and read back NFD is one file and two
        Python strings. It is the same defect as the case fold wearing a
        different alphabet, and it is testable on any filesystem: where the
        volume does not fold, the decomposed name simply does not exist, which
        is precisely the prospective-path arm of the check.
        """
        # Written as escapes, not as accented source text: the whole point is
        # that the two names differ in bytes while looking identical, and a
        # future reader must be able to see which is which.
        composed = 'Grand-m\u00e8re.m4a'        # NFC: one e-grave character
        decomposed = 'Grand-me\u0300re.m4a'     # NFD: e + a combining grave
        named = self.incoming / composed
        named.write_bytes(b'an afternoon with a grandmother')
        code, out, err = self._run(
            '--json', str(self.incoming / decomposed), '--quiet')
        self.assertEqual(code, 1, out)
        self.assertIn('--json', err)
        self.assertEqual(named.read_bytes(), b'an afternoon with a grandmother',
                         'the report replaced the recording it was named after')
        self.assertEqual(
            sorted(p.name for p in self.incoming.iterdir()),
            sorted([composed, self.twin.name]),
            'the report landed beside the recording under its other spelling, '
            'which on a volume that normalises names is the recording itself')

    def test_a_report_named_after_a_recording_but_not_one_still_writes(self):
        """The refusal folds case; it must not fold everything.

        `hartley-1998-06-14_S-wb91h3hjrr.json` sits beside the recording of
        that name and is a different file. A gate that refused it would be
        refusing the obvious filename for the report.
        """
        report = self.tmp / (self.original.stem + '.json')
        code, out, _err = self._run('--json', str(report), '--quiet')
        self.assertEqual(code, 2, out)
        self.assertTrue(report.exists())

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


class PathIdentityTest(unittest.TestCase):
    """One file has many names, and a string comparison believes every one.

    The finding was in the `--json` collision check, but the defect was the
    idea underneath it: identity was decided by tidying a path into a string
    and comparing. `os.path.normcase` folds case on Windows and nowhere else,
    so the canonicaliser's promise of macOS case equivalence was never kept,
    and every containment test in the file inherited the same blind spot - a
    file inside the archive reported as outside it, which is how a filed
    recording gets cleared for a second import.

    The primitives below are what replaced it, and each is tested in BOTH
    directions, because the two mistakes cost different things in different
    places: matching too much drops a recording from the run, matching too
    little clears one that should have been stopped.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='import-recordings-identity-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.recording = self.tmp / 'interview.m4a'
        self.recording.write_bytes(b'one afternoon in June')

    def _link(self, target, link, directory=False):
        try:
            os.symlink(str(target), str(link), target_is_directory=directory)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest('this platform will not create a symlink')

    # -- same_file: exact, for where a wrong match drops something ----------
    def test_same_file_sees_through_a_hard_link(self):
        """Two directory entries, one inode, and nothing in the names to say so."""
        other = self.tmp / 'second-name.m4a'
        try:
            os.link(str(self.recording), str(other))
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest('this platform will not create a hard link')
        self.assertTrue(find_duplicate_media.same_file(self.recording, other))

    def test_same_file_sees_through_a_symlink_and_a_dot_slash(self):
        link = self.tmp / 'shortcut.m4a'
        self._link(self.recording, link)
        dotted = os.path.join(str(self.tmp), '.', self.recording.name)
        self.assertTrue(find_duplicate_media.same_file(self.recording, link))
        self.assertTrue(find_duplicate_media.same_file(self.recording, dotted))

    def test_same_file_keeps_two_genuinely_different_files_apart(self):
        """The direction that matters: it must not fold what the volume does not.

        Where `Interview.m4a` and `interview.m4a` can both exist they are two
        recordings, and calling them one would drop a handed-over file from the
        run without a word - which is why this comparison is the exact one and
        not the blunt one.
        """
        if _filesystem_folds_case(self.tmp):
            self.skipTest('this volume folds case, so the two names are one '
                          'file and there is nothing here to keep apart')
        shouty = self.tmp / 'Interview.m4a'
        shouty.write_bytes(b'a different afternoon entirely')
        self.assertFalse(find_duplicate_media.same_file(self.recording, shouty))

    # -- could_be_same_file: blunt, for where a wrong match costs a word ----
    def test_could_be_same_file_answers_for_a_path_not_yet_written(self):
        """The whole difficulty: `samefile` needs a file, and a report has none.

        `--json Interview.m4a` on a case-insensitive volume names the existing
        recording; on a case-sensitive one it names nothing yet. The second is
        the case no inode can answer, and the fold is the answer given instead.
        """
        prospective = self.tmp / 'Interview.m4a'
        self.assertTrue(
            find_duplicate_media.could_be_same_file(self.recording, prospective))

    def test_could_be_same_file_leaves_a_different_name_alone(self):
        """The refusal folds; it does not swallow every neighbouring name."""
        report = self.tmp / 'interview.json'
        sibling = self.tmp / 'interview-2.m4a'
        elsewhere = self.tmp / 'sub' / 'interview.m4a'
        elsewhere.parent.mkdir()
        for other in (report, sibling, elsewhere):
            self.assertFalse(
                find_duplicate_media.could_be_same_file(self.recording, other),
                '%s is a different file, and refusing it would refuse a report '
                'the human is entitled to write' % other.name)

    def test_canonical_path_is_a_normaliser_not_an_identity_test(self):
        """It resolves spellings; it does not claim to answer identity.

        Pinned because the docstring claiming otherwise is what carried the
        bug. Two names for one file are `same_file`'s question now, and the two
        places that still compare canonical strings do so precisely because a
        wrong match there would drop an input rather than clear one.
        """
        canonical = find_duplicate_media.canonical_path
        link = self.tmp / 'shortcut.m4a'
        self._link(self.recording, link)
        dotted = os.path.join(str(self.tmp), '.', self.recording.name)
        self.assertEqual(canonical(self.recording), canonical(dotted))
        self.assertEqual(canonical(self.recording), canonical(link))

    # -- containment -------------------------------------------------------
    def test_is_inside_resolves_a_root_reached_through_a_link(self):
        """A media root can perfectly well be named through a shortcut.

        The old prefix test compared the link's own path against the folder's,
        found nothing in common, and reported a file sitting in the archive as
        a file from outside it. Every consequence of that is a verdict: an
        `incoming` label on a filed recording, a report allowed into the
        documents root, an inbox that stops counting as the inbox.
        """
        real = self.tmp / 'library'
        (real / 'interviews').mkdir(parents=True)
        filed = real / 'interviews' / 'filed.m4a'
        filed.write_bytes(b'already in the archive')
        link = self.tmp / 'documents-link'
        self._link(real, link, directory=True)
        self.assertTrue(find_duplicate_media._is_inside(filed, link))
        self.assertTrue(find_duplicate_media._is_inside(
            link / 'interviews' / 'filed.m4a', real))

    def test_is_inside_answers_for_a_file_that_does_not_exist_yet(self):
        """A report is a plan, and the plan is inside or outside the archive.

        Answered by climbing to the folder that WILL hold it, which is the same
        question one level up - and the reason the check has two arms at all.
        """
        inside = find_duplicate_media._is_inside
        root = self.tmp / 'library'
        (root / 'interviews').mkdir(parents=True)
        outside = self.tmp / 'reports'
        outside.mkdir()
        self.assertTrue(inside(root / 'interviews' / 'not-written-yet.json', root))
        self.assertTrue(inside(root / 'no-such-folder' / 'report.json', root))
        self.assertFalse(inside(outside / 'report.json', root))
        self.assertFalse(inside(self.tmp / 'library-elsewhere' / 'report.json', root))


class ArchiveOwnCopyThroughALinkTest(unittest.TestCase):
    """A filed recording handed back through a shortcut is still filed.

    The end-to-end half of the containment fix. The verdict was already right
    (`filed_inside_media_root` resolved both sides), but the naming was not:
    the folder the human typed was not recognised as the archive's own, so it
    took an `incoming` label and the report announced that
    `incoming/hartley-...m4a` was already filed - renaming the archive's own
    folder on the very line that says the file lives in it.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='import-recordings-linked-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.archive = self.tmp / 'archive'
        self.interviews = self.archive / 'documents' / 'interviews'
        self.interviews.mkdir(parents=True)
        (self.archive / 'fha.yaml').write_text(
            'roots:\n  documents: documents\n', encoding='utf-8')
        self.filed = self.interviews / 'hartley-1998-06-14_S-wb91h3hjrr.m4a'
        self.filed.write_bytes(b'the same bytes as the phone still holds')

    def test_a_filed_recording_reached_through_a_link_is_named_where_it_sits(self):
        handed = self.tmp / 'handed-back'
        try:
            os.symlink(str(self.interviews), str(handed), target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest('this platform will not create a directory symlink')
        code, out, _err = run_script(
            find_duplicate_media,
            [str(handed / self.filed.name), '--root', str(self.archive)])
        self.assertEqual(code, 2, out)
        self.assertIn('already filed', out)
        self.assertIn('documents/interviews/hartley-1998-06-14_S-wb91h3hjrr.m4a',
                      out)
        self.assertNotIn('incoming/', out)
        self.assertNotIn(str(self.tmp), out)


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
