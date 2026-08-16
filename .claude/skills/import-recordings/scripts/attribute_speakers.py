#!/usr/bin/env python3
"""
attribute_speakers.py — transfer phone-app speaker turns onto a Whisper transcript.

WHAT THIS DOES (and what it emphatically does not do)
=====================================================
Input A: a Whisper transcript (`**[HH:MM:SS]** text`) — accurate words, accurate
         times, no speakers.
Input B: a phone-app transcript (`[Speaker 1]` on its own line, then that turn's
         text) — speaker turns, but heavily corrupted words and (usually) no times.

Output:  a NEW markdown file, identical to the Whisper transcript except that
         segments we can attribute carry `**[HH:MM:SS] Speaker 2:** text`.
         Segments we cannot attribute keep the plain unlabeled form.

This is *label transfer*, not diarization. The ceiling is the app's own speaker
segmentation, which is demonstrably wrong in places (it splits mid-sentence and
mints phantom speaker IDs). Never describe the output as "diarized", and never
promote a `Speaker N` label to a person's name automatically — this tool emits
only the literal `Speaker N` strings that exist in the app transcript.


WHY THIS ALIGNER, AND NOT ONE BIG SequenceMatcher
=================================================
The obvious implementation is a single `difflib.SequenceMatcher` over the two
whole token streams. I rejected it as the primary mechanism for three reasons:

1. COST. `SequenceMatcher.find_longest_match` is O(n·m) in the number of
   *occurrences* of shared elements. On a 70-minute transcript (~12k tokens per
   side) the connective tissue ("the", "and", "I") occurs hundreds of times on
   each side, so the match search degenerates toward quadratic. Python's
   `autojunk` heuristic exists precisely to paper over this — but autojunk
   discards elements appearing in >1% of a ≥200-element sequence, i.e. exactly
   the function words that carry alignment through a garbled proper name. So
   the fast setting destroys robustness and the robust setting risks blowing up.

2. FRAGILITY. A single global diff has a single global failure mode: one
   confident long spurious match in the wrong place drags the whole alignment
   with it, and there is no mechanism that notices.

3. NO STRUCTURE. It gives no natural place to spend extra effort where the text
   is corrupt and no effort where it is clean.

APPROACH USED HERE: recursive unique-n-gram anchoring (patience-style),
monotonicity enforced by longest-increasing-subsequence, bounded local diff,
bounded fuzzy repair.

  Stage 1 — ANCHOR. Over the current token range, hash every n-gram (n = 5, then
    3, 2, 1) on both sides. Keep only grams that occur EXACTLY ONCE on each side.
    Uniqueness does not require rare words: "twice a week the" is unique in a
    12k-token document while every word in it is common. This is the key reason
    the method survives heavy corruption — anchors are built out of the ordinary
    vocabulary that both transcribers get right, not out of the proper nouns that
    both transcribers mangle.

  Stage 2 — ENFORCE ORDER. Candidate anchors are reduced to their longest
    strictly-increasing subsequence (patience sort, O(k log k)). Any anchor that
    would require the two transcripts to cross is discarded, not trusted. Then a
    greedy pass drops overlapping anchors. This is the guard the global diff
    lacks: a spurious match cannot survive unless it is *order-consistent* with
    every other match around it.

  Stage 3 — RECURSE. Each surviving anchor splits the problem. The gaps between
    consecutive anchors are re-anchored independently, where uniqueness is
    recomputed *locally* — a gram that was ambiguous document-wide is often
    unique inside a 200-token window. Errors stay contained inside the gap that
    produced them; there is no global cascade.

  Stage 4 — LOCAL DIFF. Once a gap is small (≤64 tokens per side) it goes to
    `difflib.SequenceMatcher(..., autojunk=False)`, which is now both optimal and
    cheap because the input is tiny. Gaps that yield no anchors at any n and are
    too large to diff safely (>4M cells) are left unmatched — deliberately. An
    unmatched region produces unlabeled output, which is the honest result.

  Stage 5 — FUZZY REPAIR. Inside small leftover gaps only (≤12 tokens per side),
    tokens are paired on character similarity (ratio ≥ 0.78, same initial, length
    within 3). This recovers "phonograph"/"pornograph"-class corruption without
    ever being able to reorder anything.

Complexity: each recursion depth touches each token O(1) times per n-level, and
depth is capped, so the whole alignment is O(N · levels) with small constants —
linear-ish in practice, and hard-capped against quadratic blowup by the cell gate.

OPPORTUNISTIC TIMESTAMP PATH
============================
Some app exports carry per-turn timestamps (`1 / Speaker 1 / 00:01 / text…`).
When at least 80% of turns have monotone timestamps, the problem collapses to an
interval lookup, which is strictly better than any text alignment. Rather than
choosing between the two, both are run and their evidence is pooled PER WORD:
each of a segment's words has one vote to give, and it gives that vote once
however many methods claim it. Two methods agreeing about a word are one covered
word, not two - agreement is confidence about that word, not more of the
segment. When they claim the same word for different speakers, its vote splits
and cancels, and the segment goes out unlabeled. Nothing is assumed about which
method is right.

The 80% gate is a real fraction, not a truncated one: 4 of 6 turns is 66.7% and
fails it. And an interval only carries a speaker when the turn that ends it is
the very next turn in the app's own order. A turn with no timestamp sitting
between two timed turns leaves the boundary between those speakers unknown, so
that whole span is treated as BLIND and casts no vote at all - otherwise a
single dropped middle turn would silently stretch the previous speaker's
interval across segments that belong to somebody else.

A count of timed turns cannot see WHERE the timing stops, so it is not the only
gate. An app transcript that ends at 51 seconds of a 100-second recording has
100% timed turns and covers half the audio. Two rules answer that: the last
timed turn's interval is capped at its own estimated length instead of running
on to the end of the audio, and the timestamp path is switched off entirely
unless the last span naming a speaker reaches TIME_MIN_TAIL_COVERAGE of the way
through. An uncovered tail casts no timestamp votes and goes out unlabelled.

CONFIDENCE
==========
Per Whisper segment, every word has ONE vote and gives it once, however many
methods claim that word. The alignment names exact words; the timestamp path
names time spans, which become words at the file's own speaking rate.

    confidence = (winner_votes - all_other_votes) / segment_token_count

which is algebraically identical to  coverage x (2 x agreement - 1), where
coverage is the share of the segment's words carrying any evidence at all. It
penalises thin coverage and contested segments in one number: a fully covered
segment split 60/40 scores 0.20; a 40%-covered unanimous segment scores 0.40.

One vote per word is the whole reason `pool_votes` exists, and it is a
correction. Until it did exist, the two methods' per-speaker TOTALS were added,
in a space where each method could already contribute a full vote per word: a
ten-word segment matched on five words by the alignment (5.0) and overlapping a
Speaker 1 interval for 45% of its duration (4.5) scored 9.5/10 = 0.95 - even
when both numbers were describing the same five words and the real coverage was
50%. The gate then promised a coverage nothing measured, and every document
describing it said something that was not true of the code.

The gate is 0.65, and it is the safety contract this skill advertises rather
than a tuning knob (SKILL.md "Transfer speaker turns...", TOOLING_INTERFACE.md
§import-recordings). Read it at both ends: a fully covered segment needs
>= 82.5% agreement - that is (1 + gate) / 2 - and a unanimous segment needs
>= 65% coverage. Everything below stays unlabeled, which is what "an unlabeled
turn means unknown, not unimportant" promises the reader.

WHY 0.65 AND NOT THE 0.90 THIS FILE SHIPPED BEFORE. 0.90 was chosen against the
doubled score, where on a timestamped export it was really asking for about 0.45
of honest coverage. Correcting the arithmetic and leaving the gate where it was
would have kept the sentence and thrown away a quarter of the labels the tool
was already getting right, which is fixing a documentation error by breaking a
working tool. 0.65 is the value MEASURED to reproduce the old operating point
under the corrected count, over 32 synthetic interview pairs (about 2,800
segments) with known speakers, corrupted app text, mid-sentence app splits,
blind spans and phantom speaker IDs:

    old arithmetic, gate 0.90    72.7% of segments labeled, 92.6% of those right
    new arithmetic, gate 0.90    61.5% labeled, 94.9% right
    new arithmetic, gate 0.65    72.1% labeled, 93.2% right

and, on a held-out set of the same shapes built from different seeds, 72.4% /
95.5% before against 71.4% / 95.6% after. Same operating point, honest number.

Note what moved underneath, because it is a real consequence and not a rounding
detail: an app export with no usable timestamps was never double counted, so
none of its scores changed. Lowering the gate does loosen that path - on the
same fixtures it goes from 56.9% labeled at 99.0% right to 68.1% at 97.5%. That
is the price of one number covering both paths instead of two, and two gates
would be a second knob on a contract that is meant to be one.

These are fixture measurements, not field measurements. The precision plateau
quoted under HONEST LIMITS was measured on a real interview under the OLD
arithmetic and has NOT been re-measured; see that section before quoting it.

Lowering the gate further with --min-confidence is possible and is the caller's
decision to defend; nothing downstream may treat a lowered run as if it met the
contract.

SAFETY RULES (all mandatory, none tunable away)
==============================================
* Hard mispair gate. The gate asks one question - ARE THESE TWO FILES THE SAME
  RECORDING? - so it is measured against BOTH streams, not the friendlier one.
  The match rate is the smaller of (matched / app tokens) and (matched / whisper
  tokens), which is the same as dividing by the larger stream. Dividing by the
  smaller stream answered a weaker question ("is the small file contained in the
  big one"), and a tiny app export that shared one five-word phrase with one
  whisper segment scored a perfect 1.00, sailed through the gate, and had that
  segment published at confidence 1.00 with somebody else's name on it. A
  minimum of MIN_MATCH_TOKENS matched words sits underneath the rate, because a
  ratio computed over a handful of words is noise whichever way you divide it.
  Below either bar the tool refuses to label anything and exits 2. A transcript
  paired with the wrong audio still matches ~6% of tokens and will happily label
  most segments with confident nonsense; correct pairs match 70-85%. The
  separation is enormous, so the gate is cheap and the failure it prevents is not.
  A genuinely partial app export - half the interview - now fails the gate too,
  because from here it is indistinguishable from a mispair; the refusal prints
  both coverage figures so a human can tell which he is looking at, and
  `--force` labels the part that does line up. `--force` overrides this gate and
  nothing else; the override is recorded in the run's warnings and in the JSON
  report, never silent.
* Refused means nothing is written. A refusal writes neither --out nor --report:
  the earlier version of a run that refused still published an unlabelled copy of
  the whisper input, so naming the wrong app transcript by mistake destroyed a
  perfectly good attributed transcript while the tool said it had refused. The
  same holds for a run that could attribute nothing at all (no speaker labels in
  the app export, no segments in the whisper file): if either destination already
  holds a file, it is left alone and the run exits 1 saying why.
* Gap interpolation only between anchors that agree on the speaker, and only
  across ≤25 tokens. Never interpolate across a speaker change.
* A tie is contested, and contested is unlabeled. There is no "same as previous
  speaker" fallback — that rule manufactures false continuity.
* Never invent a speaker. Only literal `Speaker N` strings from the app file.
* Never overwrite a label a human already wrote: any segment already carrying a
  non-`Speaker N` label is left byte-for-byte alone.
* Inputs are opened read-only and never written. The tool refuses to run if
  --out or --report resolves to either input path, OR to each other - a mistyped
  command must not let the JSON report quietly replace the transcript it
  reported on.
* Both outputs are written through a temporary file and then moved into place,
  so an interrupted or failing run leaves the previous file intact rather than a
  half-written one.
* An existing --out or --report is REFUSED, not warned about. The skill's Stage B
  command names its output deterministically (`<stem>.md`), so the second run of
  a session lands on the first run's file - and by then a human may have gone
  through it correcting speaker labels, which is the whole point of publishing a
  proposal. This tool cannot tell its own earlier output from a corrected copy of
  it: the AI marker it writes survives a human's edits, so the marker proves
  nothing, and no cheap test distinguishes the two. Rather than guess, both cases
  are refused the same way, and `--replace` is how the human says the file may go.
  `--force` does NOT authorise this: forcing past a mispair suspicion is not
  consent to destroy a corrected transcript, and one flag must not mean two
  unrelated things.
* Every destination is asked whether it could be written BEFORE the alignment
  runs and before either file is created - a folder sitting on the name, an
  ancestor that is a file, a folder that cannot be written to. The cheapest
  failure is the one that happens first, and a `--report` that was never
  saveable should cost a stat call, not a minute of alignment followed by half a
  result. That check runs ahead of the existing-file refusal, because a folder
  standing on the output name is not an earlier run's transcript and answering
  it with "add --replace" would name a fix that cannot work.
* When the transcript is written and the report then fails anyway - a disk that
  fills, a drive pulled out mid-run - THE TRANSCRIPT IS KEPT, the message says
  so, and it prints the complete command that saves the report too, `--replace`
  included. The transcript is the expensive artifact and the one that was asked
  for; `--report` is optional and diagnostic, and throwing away a complete
  correct transcript to keep the two outputs symmetrical spends the human's work
  to buy nothing. Under `--replace` it would spend more than nothing: rolling
  back would mean restoring the previous file on the same filesystem that just
  refused a write. What the message must NOT do is what it used to do - say
  "re-run with a writable --report path" and leave the human to discover that
  the rerun is refused, because `--out` now exists. Any message here that asks
  for another run states the whole command, and that command has been run in a
  test (`tests/test_import_recordings.py`).

HONEST LIMITS
=============
* THE FIELD PRECISION FIGURE NEEDS RE-MEASURING. "Precision plateaus around 96%
  at ~75% coverage" was measured on a real interview under the OLD pooled
  arithmetic and its 0.90 gate, and neither of those is what ships now. The
  fixture calibration under CONFIDENCE says the operating point is unchanged,
  but a synthetic pair is not a recording: until somebody runs the corrected
  tool over a real pair with known speakers, read 96% / 75% as the shape of the
  answer and not as a measurement of what ships today. What has NOT changed is
  where the residual sits: within a few seconds of turn boundaries, where
  neither the app's turn starts nor Whisper's segment starts are authoritative.
  Tightening the threshold does not fix that.
* Both transcribers mangle proper names, so a speaker label on a name-bearing
  segment is the *least* trustworthy kind — and names are what genealogy wants.
* Mid-sentence app splits and phantom app speakers are inherited, not repaired.
* Attribution must never flow into a claim automatically. It is a proposal for a
  human to confirm against the recording.

Exit codes: 0 = ok (including honest no-ops), 2 = refused to label (mispair
gate), 1 = usage/IO error - which includes a refusal to replace an existing
--out or --report, because the fix is a different command line and not a
different pairing of files, and the one partial result this tool can produce
(transcript written, JSON report not). 1 therefore means "not everything you
asked for happened"; the message says which part did, and names the file.

CODE MAP
========
  Normalisation      tokenize                  text to matchable word tokens
  File IO            read_text                 encoding/newline-preserving read
                     canonical_path            one spelling of a path, for collisions
                     destination_problem       why a name could never be written
                     atomic_write / write_text write via temp file, then move
  Parsing            parse_clock               MM:SS / HH:MM:SS to seconds
                     parse_whisper             segments + flat token stream
                     parse_app                 app turns (bracket/numbered/plain)
  Alignment          _lis                      order filter for candidate anchors
                     _unique_ngram_anchors     patience-style unique-gram anchors
                     _fuzzy_pairs              character-similarity repair
                     _local_match              bounded difflib on a small range
                     align_tokens              the recursive driver + stats
  Mispair gate       mispair_evidence          both streams' coverage, measured
                     mispair_gate_ok           the same-recording question
                     mispair_sentence          the numbers in plain words
  Evidence           collect_align_votes       votes from token pairs, gap-filled
                     timestamp_coverage_ok     the 80% timed-turn gate
                     turn_span_estimate        how long one turn plausibly lasts
                     speaker_intervals         turn spans, blind where unknowable
                     collect_time_votes        votes from the interval lookup
                     pool_votes                one vote per word, however many
                                               methods claim it
  Decision           enclosing_agree           do the anchors around a segment agree
                     decide                    the gate: label, or say why not
  Rendering          render                    rewrite segment header lines
  Reporting          speaker_evidence          per-speaker share / role signals
                     confidence_bucket         histogram bucket for a score
                     decile_coverage/_skew     where the alignment thins out
  CLI                build_parser / fail / main
                     rerun_command             this run, plus a flag, copyable
                     alternative_name          a free "-2" name for a taken one
"""

from __future__ import annotations

import argparse
import bisect
import difflib
import json
import os
import re
import shlex
import sys
import unicodedata
from collections import Counter

TOOL_NAME = "attribute_speakers.py"
TOOL_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Tunables (documented above; changing these changes the safety story)
# ---------------------------------------------------------------------------
DEFAULT_MIN_CONFIDENCE = 0.65   # the documented safety contract, not a knob.
                          # 0.90 until the pooling fix: the old vote space let
                          # the alignment and the clock each claim the same
                          # word, so 0.90 of a doubled score was really asking
                          # for about 0.45 of honest coverage. 0.65 is the
                          # value measured to reproduce the old operating point
                          # once each word votes once (see CONFIDENCE above).
DEFAULT_MIN_MATCH_RATE = 0.50
MIN_MATCH_TOKENS = 20     # and the rate must rest on at least this many matched
                          # words. A rate over five words is not a measurement:
                          # two unrelated recordings share "yeah I don't know"
                          # all day long. Below this the answer is "cannot
                          # tell", and a gate that cannot tell refuses.
GAP_CAP = 25              # max tokens interpolated between two agreeing anchors
SMALL_BLOCK = 64          # ranges this small go straight to difflib
MAX_LOCAL_CELLS = 4_000_000   # hard cap on any single difflib call (n*m)
NGRAM_LEVELS = (5, 3, 2, 1)
MAX_DEPTH = 60
FUZZY_MIN_RATIO = 0.78
FUZZY_MIN_LEN = 4
FUZZY_WINDOW = 12
TIME_MIN_TURN_COVERAGE = 0.8  # fraction of app turns that must carry a timestamp
TIME_MIN_TIMED_TURNS = 3      # and never fewer than this many, whatever the fraction
TIME_MIN_TAIL_COVERAGE = 0.9  # and the timed turns must reach this far into the
                              # audio; a count of timed turns says nothing about
                              # WHERE the timing stops (see collect_time_votes)
SPEECH_SECONDS_PER_TOKEN = 0.35   # rough speaking rate, used to guess where a
                                  # final segment or turn stops. One constant so
                                  # both sides of that comparison are measured
                                  # the same way.
MIN_TOTAL_VOTE_WEIGHT = 2.0   # below this a segment rests on one or two stray tokens

# Fractions are compared with a hair of slack so that a ratio which is exactly
# the gate (8 of 10 turns) is not pushed under it by binary floating point.
FRACTION_EPSILON = 1e-9

# Every internal status has a sentence a genealogist can act on. The status
# string itself is for the JSON report; this is what he reads when a run stops.
STATUS_IN_PLAIN_WORDS = {
    # Covers both halves of the gate. "They are not the same recording" would
    # be the wrong cause for the other half, where the honest answer is that
    # there was too little matching text to tell either way.
    "mispair_suspected": "there is not enough matching text to believe the two "
                         "files are the same recording",
    "no_whisper_segments": "the whisper transcript has no '**[HH:MM:SS]**' lines "
                           "to put labels on",
    "no_speaker_labels": "the app transcript carries no speaker labels at all "
                         "(it is a paragraph-only export)",
    "empty_transcript": "one of the two transcripts contains no words",
}

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
_NUM_WORDS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four", "5": "five",
    "6": "six", "7": "seven", "8": "eight", "9": "nine", "10": "ten",
    "11": "eleven", "12": "twelve", "13": "thirteen", "14": "fourteen",
    "15": "fifteen", "16": "sixteen", "17": "seventeen", "18": "eighteen",
    "19": "nineteen", "20": "twenty",
}

# Tiny, deliberate canon map: only variants where the two transcribers reliably
# disagree on spelling for the *same* sound. Not a place for cleverness.
_CANON = {
    "ok": "okay", "okey": "okay", "yep": "yeah", "yup": "yeah", "yea": "yeah",
    "mmhmm": "mhm", "mmhm": "mhm", "uhhuh": "mhm", "mmm": "mm",
}

_WORD_RE = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list:
    """Normalised word tokens. The matching unit for the whole aligner.

    Word-level (not character-level) matching is what survives phonetic garbage:
    a mangled name breaks one or two tokens while the surrounding function words
    still carry the alignment.
    """
    if not text:
        return []
    flat = unicodedata.normalize("NFKD", text)
    flat = flat.replace("\u2019", "'").replace("\u2018", "'")
    flat = flat.encode("ascii", "ignore").decode("ascii").lower()
    out = []
    for raw in _WORD_RE.findall(flat):
        w = raw.replace("'", "")
        if not w:
            continue
        w = _NUM_WORDS.get(w, w)
        w = _CANON.get(w, w)
        out.append(w)
    return out


# ---------------------------------------------------------------------------
# File IO (read-only on inputs; newline style preserved on output)
# ---------------------------------------------------------------------------
def read_text(path: str):
    with open(path, "rb") as fh:
        raw = fh.read()
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", "replace")
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    newline = "\r\n" if crlf > lf else "\n"
    trailing = text.endswith("\n") or text.endswith("\r")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if trailing and lines and lines[-1] == "":
        lines.pop()
    return lines, newline, trailing


def canonical_path(path: str) -> str:
    """One spelling of a path. A NORMALISER - not an identity test.

    Resolves `.`, `..` and symlinks, and folds case on Windows, where
    `os.path.normcase` folds. It does NOT fold case anywhere else: on POSIX -
    macOS included - `normcase` is the identity function. Do not read a
    docstring promise of macOS case equivalence into it; an earlier version of
    this docstring made exactly that promise and it was false everywhere it
    mattered.

    For "would writing here land on that file?", use `could_be_same_file`.
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


# ── Path identity ─────────────────────────────────────────────────────────────
#
# Two names can be one file, and no amount of string tidying decides which.
# A case-insensitive volume (the default on macOS and Windows) makes `X.md` and
# `x.md` one file; macOS stores names in NFD, so a name typed as NFC and the
# same name read back off disk are one file and two Python strings; a hard link
# or a second mount is one file under two unrelated paths. The filesystem is the
# only authority on any of it.
#
# That matters here because this tool's collision checks stand between a
# mistyped command line and an ARCHIVED ORIGINAL: `--out`/`--report` are checked
# against the two transcripts being read, and those are files `fha process` has
# already filed. A check that compares strings passes `--out Whisper.md` while
# reading `whisper.md`, and the atomic write then replaces the input.
#
# The same fix landed in this skill's `find_duplicate_media.py`, which carries
# the canonical version of these helpers and a longer treatment. They are
# duplicated rather than shared because a skill script must run standalone from
# any folder with no archive present - it may not import a sibling script.

def _identity_key(path: str) -> str:
    """A blunt, unconditional folding of a path, for a file that is not there yet.

    NFC-normalise, then `casefold`. Deliberately over-eager: it folds case on
    volumes that do not, and folds accent spellings that only macOS conflates.

    Blunt on purpose, and only reached from `could_be_same_file`. The two
    mistakes are not comparable - a false positive refuses a filename that was
    actually free and costs the human one more word on the command line; a false
    negative overwrites an archived transcript. Probing the volume instead is
    also weaker than it sounds: it would have to run on the volume holding each
    candidate, it needs a write to be trustworthy, and it cannot run at all on a
    read-only mount.
    """
    return unicodedata.normalize('NFC', canonical_path(path)).casefold()


def could_be_same_file(a: str, b: str) -> bool:
    """Could writing to one of these land on the other? Two arms, one question.

    * Both exist - ask the filesystem. `os.path.samefile` compares
      `(st_dev, st_ino)`, which is authoritative on every volume and catches the
      aliases no string can: a hard link, a second mount of the same disk, a
      case variant on a folding volume where only one spelling is the name on
      disk.
    * One does not - `samefile` cannot speak about a file that is not there, and
      an output path usually is not. Fall back to the folded key.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return _identity_key(a) == _identity_key(b)


def destination_problem(path: str, flag: str) -> str:
    """Why nothing could ever be saved under this name, in plain words, or None.

    The cheapest failure is the one that happens before the work. Alignment on a
    70-minute pair is a minute of CPU and, more to the point, it ends by writing
    a file: a destination that was never writable is therefore not just a wasted
    minute, it is the difference between "nothing happened" and "half of what
    you asked for happened". So both destinations are asked the question up
    front, exactly as `transcribe_audio.py` asks its three.

    What is asked is deliberately narrow - is there a FOLDER sitting on this
    name, and can the folder that must hold it exist and be written to. Those
    are the shapes no flag can get past. The file's own permission bits are NOT
    asked about: `os.replace` needs write permission on the containing folder,
    not on the file it lands on, so a read-only file inside a writable folder is
    replaced quite happily on POSIX, and refusing it here would be a refusal of
    something that works. It is a cheap early no, never a promise of a later
    yes, which is why every write still has its own error path behind it.

    It runs BEFORE the "that file already exists" refusal, because a folder on
    the name is not an earlier run's transcript: telling the human to add
    `--replace` to get past a directory would name a fix that cannot work.
    """
    if os.path.isdir(path):
        return ("%s names a FOLDER, not a file (%s), so nothing can be saved "
                "under that name. Give %s a filename inside that folder - or a "
                "different name altogether - and run the command again."
                % (flag, path, flag))
    # `atomic_write` creates a missing parent, so a parent that is simply absent
    # is fine; what is not fine is an ancestor that cannot hold a folder.
    parent = os.path.dirname(os.path.abspath(path))
    walked = parent
    while walked and not os.path.exists(walked):
        nxt = os.path.dirname(walked)
        if nxt == walked:
            break
        walked = nxt
    if walked and not os.path.isdir(walked):
        return ("the folder %s cannot hold %s, because %s is a file, not a "
                "folder. Give %s a path inside a real folder and run the "
                "command again." % (parent, flag, walked, flag))
    if walked and not os.access(walked, os.W_OK):
        return ("the folder %s cannot be written to, so %s has nowhere to go "
                "(%s). Give %s a path in a folder you can write to, or fix that "
                "folder's permissions, then run the command again."
                % (walked, flag, path, flag))
    return None


def atomic_write(path: str, body: str) -> None:
    """Write `body` to `path` via a temporary file in the same directory.

    A transcript half-written by an interrupted run is worse than no transcript
    at all: it looks finished, and the reader has no way to tell which turns
    went missing. Writing beside the destination and then moving into place
    means the file the human sees is always either the previous complete one or
    the new complete one. Same directory, because `os.replace` is only atomic
    within a filesystem.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    tmp = "%s.tmp-%d" % (os.path.abspath(path), os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def write_text(path: str, lines, newline: str, trailing: bool) -> None:
    body = newline.join(lines) + (newline if trailing else "")
    atomic_write(path, body)


# ---------------------------------------------------------------------------
# Whisper transcript parsing
# ---------------------------------------------------------------------------
# Non-greedy label group is load-bearing: a greedy one would latch onto a later
# "**bold**" inside the segment text and mangle the line.
SEG_LINE = re.compile(
    r"^(?P<pre>[ \t>]*)\*\*\[(?P<time>[^\]]{1,64})\](?P<label>[^*]*?)\*\*(?P<rest>.*)$"
)
TIME_RE = re.compile(r"^\s*(\d{1,3}):([0-5]?\d)(?::([0-5]?\d))?(?:[.,](\d{1,3}))?\s*$")
SPEAKER_LABEL_RE = re.compile(r"^\s*(Speaker\s+\d+)\s*:?\s*$", re.IGNORECASE)


def parse_clock(text: str):
    m = TIME_RE.match(text or "")
    if not m:
        return None
    a, b, c, frac = m.group(1), m.group(2), m.group(3), m.group(4)
    if c is None:                      # MM:SS
        secs = int(a) * 60 + int(b)
    else:                              # HH:MM:SS
        secs = int(a) * 3600 + int(b) * 60 + int(c)
    if frac:
        secs += float("0." + frac)
    return float(secs)


class Segment(object):
    __slots__ = ("idx", "line_no", "time_text", "time_s", "pre", "rest",
                 "tokens", "t0", "t1", "locked", "speaker", "confidence",
                 "reason", "votes")

    def __init__(self):
        self.idx = -1
        self.line_no = -1
        self.time_text = ""
        self.time_s = None
        self.pre = ""
        self.rest = ""
        self.tokens = []
        self.t0 = 0
        self.t1 = 0
        self.locked = False
        self.speaker = None
        self.confidence = 0.0
        self.reason = "no-evidence"
        self.votes = None


def parse_whisper(lines):
    """Segments plus the flat token stream. Preamble/front matter is ignored."""
    segments = []
    cur = None
    texts = []
    for n, line in enumerate(lines):
        m = SEG_LINE.match(line)
        if m:
            if cur is not None:
                cur.tokens = tokenize(" ".join(texts))
                segments.append(cur)
            cur = Segment()
            cur.line_no = n
            cur.time_text = m.group("time").strip()
            cur.time_s = parse_clock(cur.time_text)
            cur.pre = m.group("pre")
            cur.rest = m.group("rest")
            label = m.group("label") or ""
            if label.strip() and not SPEAKER_LABEL_RE.match(label):
                # Somebody (a human, probably) already put a real name here.
                cur.locked = True
            texts = [cur.rest]
            continue
        if cur is None:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        texts.append(line)
    if cur is not None:
        cur.tokens = tokenize(" ".join(texts))
        segments.append(cur)

    stream = []
    owner = []
    for i, seg in enumerate(segments):
        seg.idx = i
        seg.t0 = len(stream)
        stream.extend(seg.tokens)
        seg.t1 = len(stream)
        owner.extend([i] * len(seg.tokens))
    return segments, stream, owner


# ---------------------------------------------------------------------------
# App transcript parsing
# ---------------------------------------------------------------------------
APP_BRACKET = re.compile(r"^\s*\[\s*Speaker\s+(\d+)\s*\]\s*$", re.IGNORECASE)
APP_BARE = re.compile(r"^\s*Speaker\s+(\d+)\s*$", re.IGNORECASE)
APP_INDEX = re.compile(r"^\s*\d{1,5}\s*$")
APP_TIME = re.compile(r"^\s*(\d{1,3}):([0-5]\d)(?::([0-5]\d))?\s*$")


class Turn(object):
    __slots__ = ("speaker", "start", "raw", "tokens", "t0", "t1")

    def __init__(self, speaker):
        self.speaker = speaker
        self.start = None
        self.raw = []
        self.tokens = []
        self.t0 = 0
        self.t1 = 0


def parse_app(lines):
    """Handles both known export shapes, and reports when there are no labels.

    variant 'bracket'  : [Speaker 1] / text / blank
    variant 'numbered' : index / Speaker 1 / MM:SS / text   (carries timestamps)
    variant 'plain'    : paragraphs only, no speaker information at all
    """
    n_bracket = sum(1 for ln in lines if APP_BRACKET.match(ln))
    n_bare = sum(1 for ln in lines if APP_BARE.match(ln))
    if n_bracket:
        variant = "bracket"
    elif n_bare:
        variant = "numbered"
    else:
        variant = "plain"

    turns = []
    if variant == "plain":
        return turns, variant, [], []

    cur = None
    i = 0
    total = len(lines)
    while i < total:
        line = lines[i]
        m = APP_BRACKET.match(line) if variant == "bracket" else APP_BARE.match(line)
        if m is None and variant == "numbered":
            m = APP_BRACKET.match(line)
        if m:
            cur = Turn("Speaker %d" % int(m.group(1)))
            turns.append(cur)
            i += 1
            if i < total and APP_TIME.match(lines[i]) and not SEG_LINE.match(lines[i]):
                cur.start = parse_clock(lines[i].strip())
                i += 1
            continue
        if variant == "numbered" and APP_INDEX.match(line):
            # An index line only counts as an index if a speaker header follows.
            j = i + 1
            while j < total and not lines[j].strip():
                j += 1
            if j < total and (APP_BARE.match(lines[j]) or APP_BRACKET.match(lines[j])):
                i += 1
                continue
        if line.strip() and cur is not None:
            cur.raw.append(line.strip())
        i += 1

    stream = []
    owner = []
    kept = []
    for t in turns:
        t.tokens = tokenize(" ".join(t.raw))
        t.t0 = len(stream)
        stream.extend(t.tokens)
        t.t1 = len(stream)
        owner.extend([t.speaker] * len(t.tokens))
        kept.append(t)
    return kept, variant, stream, owner


# ---------------------------------------------------------------------------
# Alignment
# ---------------------------------------------------------------------------
def _lis(pairs):
    """Longest strictly-increasing-in-j subsequence of pairs sorted by i."""
    if not pairs:
        return []
    tails = []
    tails_idx = []
    prev = [-1] * len(pairs)
    for k, (_i, j) in enumerate(pairs):
        p = bisect.bisect_left(tails, j)
        prev[k] = tails_idx[p - 1] if p > 0 else -1
        if p == len(tails):
            tails.append(j)
            tails_idx.append(k)
        else:
            tails[p] = j
            tails_idx[p] = k
    out = []
    k = tails_idx[-1]
    while k != -1:
        out.append(pairs[k])
        k = prev[k]
    out.reverse()
    return out


def _unique_ngram_anchors(a, b, a0, a1, b0, b1, n):
    """Anchors from n-grams occurring exactly once on each side, order-filtered."""
    first_a = {}
    for i in range(a0, a1 - n + 1):
        g = tuple(a[i:i + n])
        if g in first_a:
            first_a[g] = -1
        else:
            first_a[g] = i
    if not first_a:
        return []
    first_b = {}
    for j in range(b0, b1 - n + 1):
        g = tuple(b[j:j + n])
        if g in first_b:
            first_b[g] = -1
        else:
            first_b[g] = j
    cand = []
    for g, i in first_a.items():
        if i < 0:
            continue
        j = first_b.get(g)
        if j is None or j < 0:
            continue
        cand.append((i, j))
    if not cand:
        return []
    cand.sort()
    ordered = _lis(cand)
    out = []
    last_a = last_b = -1
    for (i, j) in ordered:
        if i > last_a and j > last_b:
            out.append((i, j, n))
            last_a = i + n - 1
            last_b = j + n - 1
    return out


def _fuzzy_pairs(sa, sb, a0, a1, b0, b1):
    """Character-similarity pairing inside a tiny, already-bracketed window."""
    res = []
    cursor = b0
    for i in range(a0, a1):
        w = sa[i]
        if len(w) < FUZZY_MIN_LEN:
            continue
        best = None
        best_r = FUZZY_MIN_RATIO
        for j in range(cursor, b1):
            v = sb[j]
            if len(v) < FUZZY_MIN_LEN or v[0] != w[0]:
                continue
            if abs(len(v) - len(w)) > 3:
                continue
            r = difflib.SequenceMatcher(None, w, v).ratio()
            if r > best_r:
                best_r = r
                best = j
        if best is not None:
            res.append((i, best))
            cursor = best + 1
    return res


def _local_match(a, b, a0, a1, b0, b1):
    sa = a[a0:a1]
    sb = b[b0:b1]
    if not sa or not sb:
        return []
    sm = difflib.SequenceMatcher(None, sa, sb, autojunk=False)
    out = []
    prev_i = prev_j = 0
    for (ai, bj, size) in sm.get_matching_blocks():
        if ai > prev_i and bj > prev_j:
            gap_a = ai - prev_i
            gap_b = bj - prev_j
            if gap_a <= FUZZY_WINDOW and gap_b <= FUZZY_WINDOW:
                for (x, y) in _fuzzy_pairs(sa, sb, prev_i, ai, prev_j, bj):
                    out.append((a0 + x, b0 + y))
        for k in range(size):
            out.append((a0 + ai + k, b0 + bj + k))
        prev_i = ai + size
        prev_j = bj + size
    out.sort()
    return out


def align_tokens(a, b):
    """Monotone token pairs (app_index, whisper_index) plus alignment stats."""
    pairs = []
    stats = Counter()
    skipped = 0
    stack = [(0, len(a), 0, len(b), 0)]
    while stack:
        a0, a1, b0, b1, depth = stack.pop()
        na = a1 - a0
        nb = b1 - b0
        if na <= 0 or nb <= 0:
            continue
        if na <= SMALL_BLOCK and nb <= SMALL_BLOCK:
            pairs.extend(_local_match(a, b, a0, a1, b0, b1))
            stats["local_diff"] += 1
            continue
        anchors = []
        if depth < MAX_DEPTH:
            for n in NGRAM_LEVELS:
                if n > na or n > nb:
                    continue
                anchors = _unique_ngram_anchors(a, b, a0, a1, b0, b1, n)
                if anchors:
                    stats["anchors_n%d" % n] += len(anchors)
                    break
        if not anchors:
            if na * nb <= MAX_LOCAL_CELLS:
                pairs.extend(_local_match(a, b, a0, a1, b0, b1))
                stats["local_diff"] += 1
            else:
                skipped += min(na, nb)
                stats["skipped_regions"] += 1
            continue
        prev_a, prev_b = a0, b0
        for (i, j, n) in anchors:
            if i > prev_a and j > prev_b:
                stack.append((prev_a, i, prev_b, j, depth + 1))
            for k in range(n):
                pairs.append((i + k, j + k))
            prev_a, prev_b = i + n, j + n
        if prev_a < a1 and prev_b < b1:
            stack.append((prev_a, a1, prev_b, b1, depth + 1))

    pairs.sort()
    clean = []
    last_i = last_j = -1
    for (i, j) in pairs:
        if i > last_i and j > last_j:
            clean.append((i, j))
            last_i = i
            last_j = j
    stats["skipped_tokens"] = skipped
    return clean, stats


# ---------------------------------------------------------------------------
# The mispair gate: are these two files the same recording?
# ---------------------------------------------------------------------------
def mispair_evidence(n_matched, n_app, n_wh):
    """Both streams' coverage, and the one number the gate reads.

    The gate's question is symmetric - same recording or not - so the measure
    has to be symmetric too. `match_rate` is the SMALLER of the two coverages,
    which is arithmetically the same as dividing the matched tokens by the
    LARGER stream.

    The earlier version divided by the smaller stream, and that quietly asked a
    different, much easier question: "is the small file contained in the big
    one?" A five-token app export sharing one phrase with one whisper segment
    covered 100% of itself, scored a perfect 1.00, passed a gate meant to catch
    exactly that file, and then handed that segment a confidence of 1.00 with
    the wrong speaker's name on it. Every other whisper segment was unrelated
    and the number could not see them, because they were not in the denominator.

    Both raw coverages are returned as well as the gate number: the refusal
    prints them, and 83% of the app against 41% of whisper reads very
    differently from 6% against 5% - the first is half an interview, the second
    is the wrong file.
    """
    app_rate = (n_matched / float(n_app)) if n_app else 0.0
    wh_rate = (n_matched / float(n_wh)) if n_wh else 0.0
    return {
        "matched_tokens": n_matched,
        "app_tokens": n_app,
        "whisper_tokens": n_wh,
        "app_coverage": app_rate,
        "whisper_coverage": wh_rate,
        "match_rate": min(app_rate, wh_rate),
    }


def mispair_gate_ok(ev, min_match_rate):
    """True when the two files look like the same recording.

    Two bars, because a rate and an amount of evidence are different questions
    (the same distinction the timestamp path draws between how many turns carry
    a stamp and where those stamps sit). The rate must clear --min-match-rate on
    BOTH streams, and it must rest on at least MIN_MATCH_TOKENS matched words so
    that a handful of common words cannot produce a confident ratio.
    """
    if ev["matched_tokens"] < MIN_MATCH_TOKENS:
        return False
    return ev["match_rate"] >= (min_match_rate - FRACTION_EPSILON)


def mispair_sentence(ev, min_match_rate):
    """The refusal, with the real numbers in it.

    A human has to be able to tell a mispair from a genuinely partial export
    without reading this file, and the only thing that tells them apart is the
    two coverages side by side. So both are printed, in words, with the counts
    they came from.
    """
    head = ("%d words line up between the two transcripts: %.1f%% of the app "
            "transcript's %d words and %.1f%% of the whisper transcript's %d"
            % (ev["matched_tokens"], 100.0 * ev["app_coverage"], ev["app_tokens"],
               100.0 * ev["whisper_coverage"], ev["whisper_tokens"]))
    if ev["matched_tokens"] < MIN_MATCH_TOKENS:
        return (head + ". That is fewer than the %d matching words this check "
                       "needs before a percentage means anything, so it cannot "
                       "tell whether these two files describe the same "
                       "recording; refusing to label"
                % MIN_MATCH_TOKENS)
    return (head + ". The gate needs %.1f%% of BOTH sides, so these two files "
                   "probably do not describe the same recording - or the app "
                   "export covers only part of it; refusing to label"
            % (100.0 * min_match_rate))


# ---------------------------------------------------------------------------
# Evidence gathering
# ---------------------------------------------------------------------------
def collect_align_votes(pairs, app_owner, wh_owner, n_segments):
    """Alignment evidence, both as per-segment votes and as WHICH token it sits on.

    The fourth return value is the load-bearing one: `claim` maps a whisper
    token's index in the flat stream to the speaker the alignment says owns it.
    Nothing about the arithmetic here changed when that map was added - each
    whisper token index appears at most once in `pairs` (align_tokens keeps the
    pairs strictly increasing in j) and gap-filling only touches the indices
    strictly between two pairs - so `votes` is exactly the per-token count it
    always was. What the map buys is the ability to ask, later, whether the
    timestamp path is describing the SAME tokens or different ones, which a
    bag of per-speaker totals cannot answer. See `pool_votes`.
    """
    votes = [Counter() for _ in range(n_segments)]
    anchored = [0] * n_segments
    filled = [0] * n_segments
    claim = {}
    for (i, j) in pairs:
        seg = wh_owner[j]
        votes[seg][app_owner[i]] += 1.0
        claim[j] = app_owner[i]
        anchored[seg] += 1
    # Interpolate only between two anchors that agree, and only over short gaps.
    for k in range(1, len(pairs)):
        i1, j1 = pairs[k - 1]
        i2, j2 = pairs[k]
        if j2 - j1 <= 1:
            continue
        s1 = app_owner[i1]
        if s1 != app_owner[i2]:
            continue                       # never interpolate across a change
        if (j2 - j1 - 1) > GAP_CAP or (i2 - i1 - 1) > GAP_CAP:
            continue
        for j in range(j1 + 1, j2):
            seg = wh_owner[j]
            votes[seg][s1] += 1.0
            claim[j] = s1
            filled[seg] += 1
    return votes, anchored, filled, claim


def timestamp_coverage_ok(turns):
    """True when enough app turns carry a timestamp to trust the interval path.

    Written as a true fraction rather than `int(0.8 * len(turns))` because the
    truncating form quietly passes coverage the gate is meant to reject: it
    admits 4 of 6 turns (66.7%) and 7 of 9 (77.8%). Timestamp evidence is the
    stronger of the two methods, so it is also the one that does the most damage
    when it is turned on for a transcript that has holes in it.
    """
    if not turns:
        return False
    timed = sum(1 for t in turns if t.start is not None)
    if timed < TIME_MIN_TIMED_TURNS:
        return False
    return (timed / float(len(turns))) >= (TIME_MIN_TURN_COVERAGE - FRACTION_EPSILON)


def turn_span_estimate(turn):
    """How long a turn plausibly lasts, guessed from its own word count.

    The app export gives a turn's start and nothing else, so the only honest end
    for the LAST timed turn is an estimate from the words inside it. The rate is
    the same one used to guess where the final whisper segment stops, so the two
    ends being compared are measured the same way rather than one of them being
    a real timestamp and the other a whole recording's length.
    """
    return max(1.0, SPEECH_SECONDS_PER_TOKEN * len(turn.tokens))


def speaker_intervals(turns, audio_end):
    """Turn timestamps as (start, end, speaker-or-None) spans, in order.

    A timed turn's interval runs to the start of the next timed turn. That end
    is only honest when the next turn in the app's OWN order is the one that
    owns it. When an untimed turn sits in between, the boundary between the two
    speakers is somewhere inside the span and nothing here knows where - so the
    span is emitted with speaker None, a BLIND interval that casts no vote.

    This is the whole point of the function. Dropping untimed turns and then
    letting the previous timed turn's interval run on to the next timestamp is
    the absorption bug: one missing middle turn hands the previous speaker
    full-strength votes over segments that are somebody else's words, and
    nothing downstream can tell that happened.

    The last timed turn is the same bug wearing a different hat, and it is the
    one with no next turn to blank it: an app transcript that stops at 51s of a
    100-second recording used to have its final interval stretched all the way
    to `audio_end`, so every segment in the 49 uncovered seconds collected a
    full-duration vote for whoever happened to speak last. The final interval is
    therefore capped at that turn's own estimated length. Past the app's real
    coverage there is no interval at all, so the tail casts no timestamp votes
    and stays unlabelled, which is the honest answer.
    """
    n = len(turns)
    next_timed_start = [None] * n
    seen = None
    for i in range(n - 1, -1, -1):
        next_timed_start[i] = seen
        if turns[i].start is not None:
            seen = turns[i].start

    out = []
    for i, turn in enumerate(turns):
        if turn.start is None:
            continue
        end = next_timed_start[i]
        if end is None:
            end = turn.start + turn_span_estimate(turn)
            if audio_end > turn.start:
                end = min(end, audio_end)
        if end <= turn.start:
            continue                       # zero-length: two turns share a stamp
        blind = (i + 1 < n and turns[i + 1].start is None)
        out.append((turn.start, end, None if blind else turn.speaker))
    return out


def collect_time_votes(segments, turns, n_segments, warnings):
    """Interval lookup when the app export carries timestamps. Opportunistic.

    Two coverage gates, because a count and a position are different questions.
    `timestamp_coverage_ok` asks HOW MANY turns carry a timestamp; it cannot see
    that all of them sit in the first half of the recording. So the second gate
    asks WHERE the timing actually reaches: the last span that names a speaker
    must land within TIME_MIN_TAIL_COVERAGE of the end of the audio, or the
    timestamp path is switched off for the whole file. An app transcript that
    stops halfway is not partial evidence about the second half, it is no
    evidence at all, and the text alignment alone is the correct answer there.
    """
    if not timestamp_coverage_ok(turns):
        return None
    starts = [t.start for t in turns if t.start is not None]
    if any(starts[k] < starts[k - 1] for k in range(1, len(starts))):
        warnings.append("app timestamps are not monotone; timestamp path disabled")
        return None
    seg_times = [s.time_s for s in segments]
    if not seg_times:
        return None
    if any(t is None for t in seg_times):
        return None
    if any(seg_times[k] < seg_times[k - 1] for k in range(1, len(seg_times))):
        warnings.append("whisper timestamps are not monotone; timestamp path disabled")
        return None
    audio_end = seg_times[-1] + max(
        1.0, SPEECH_SECONDS_PER_TOKEN * max(1, len(segments[-1].tokens)))

    intervals = speaker_intervals(turns, audio_end)
    if not intervals:
        return None
    named_ends = [end for (_start, end, spk) in intervals if spk is not None]
    if not named_ends:
        return None
    covered_end = max(named_ends)
    if covered_end < TIME_MIN_TAIL_COVERAGE * audio_end:
        warnings.append(
            "the app transcript's timed turns stop at about %.0fs but the "
            "recording runs to about %.0fs; timestamp evidence is switched off "
            "for the whole file rather than guess who is speaking in the last "
            "%.0fs" % (covered_end, audio_end, audio_end - covered_end))
        return None

    votes = [Counter() for _ in range(n_segments)]
    claim = [[] for _ in range(n_segments)]
    used = [False] * n_segments
    k = 0
    for idx, seg in enumerate(segments):
        s0 = seg_times[idx]
        s1 = seg_times[idx + 1] if idx + 1 < len(segments) else audio_end
        if s1 <= s0:
            s1 = s0 + max(0.5, SPEECH_SECONDS_PER_TOKEN * max(1, len(seg.tokens)))
        ntok = len(seg.tokens)
        if ntok == 0:
            continue
        # `s1 > s0` is guaranteed by the reset above, so `dur` is safe to
        # divide by; the cursor is advanced first either way, because it is
        # shared across segments and must not depend on this segment's shape.
        while k + 1 < len(intervals) and intervals[k][1] <= s0:
            k += 1
        dur = s1 - s0
        kk = k
        per_token = [Counter() for _ in range(ntok)]
        tok_dur = dur / float(ntok)
        while kk < len(intervals) and intervals[kk][0] < s1:
            i0, i1, spk = intervals[kk]
            kk += 1
            if spk is None:
                continue                   # blind span: no vote in either direction
            lo = max(s0, i0)
            hi = min(s1, i1)
            if hi <= lo:
                continue
            # Which of this segment's words the interval covers, at the same
            # speaking rate the rest of the file is measured with. The total
            # weight this loop hands `spk` is (overlap / duration) * ntok, to
            # the last decimal place - the per-token breakdown is a finer
            # accounting of the same evidence, not more of it. It exists so
            # `pool_votes` can tell "the alignment and the clock agree about
            # these five words" from "they are talking about different words".
            m_lo = max(0, int((lo - s0) / tok_dur))
            m_hi = min(ntok - 1, int((hi - s0 - FRACTION_EPSILON) / tok_dur))
            for m in range(m_lo, m_hi + 1):
                a = s0 + m * tok_dur
                ov = min(hi, a + tok_dur) - max(lo, a)
                if ov > 0:
                    per_token[m][spk] += ov / tok_dur
        # Overlaps that fell in a blind span are simply absent, so a
        # partly-blind segment gets proportionally less timestamp weight. That
        # is the intended behaviour: less certainty, less vote.
        got = False
        for m in range(ntok):
            for spk, w in per_token[m].items():
                votes[idx][spk] += w
                got = True
        if not got:
            continue
        claim[idx] = per_token
        used[idx] = True
    return votes, used, claim


def pool_votes(segments, align_claim, time_claim):
    """One vote per whisper token, shared out among whoever claims that token.

    THE RULE: a segment of N words has N votes to give, and each word gives its
    own vote once. Two methods that agree about a word are one covered word,
    not two.

    This function exists because the earlier version pooled the two methods'
    per-speaker totals by adding them, in a vote space where each method could
    already contribute up to one vote per token on its own. A ten-word segment
    matched on five words by the alignment (5.0) and overlapping a Speaker 1
    interval for 45% of its duration (4.5) scored 9.5 out of 10 - a confidence
    of 0.95 - even when both methods were describing the same half of the
    segment and the real coverage was 50%. The score is meant to read as
    coverage x (2 x agreement - 1), and coverage that counts the same word
    twice is not coverage. The gate then meant something looser than every
    document describing it said it meant.

    So the two methods are intersected where they can be: the alignment names
    exact token indices, and the timestamp path names time spans, which become
    token indices at the file's own speaking rate. Per token, the claims are
    summed and then scaled back to at most one vote:

      * both methods, same speaker  -> 1.0 for that speaker (corroboration is
        confidence about the word, not a second word),
      * both methods, different speakers -> 0.5 each, which cancels in
        (winner - others) exactly as a contested word should,
      * one method only -> that method's weight, unchanged.

    The alternative considered was to keep the per-speaker totals and pool them
    as max() rather than sum, which needs no positions. It is rejected because
    it is not the same measurement: max() throws away every token one method
    covers and the other does not, so two methods covering DIFFERENT halves of
    a segment would score 50% coverage when the honest answer is 100%. Taking
    the union costs one array per segment and answers the real question.

    With this in place, `decide`'s confidence is bounded by coverage, so the
    clip at 1.0 is arithmetic belt-and-braces rather than a live path, and the
    documented reading of the gate - full coverage needs (1 + gate) / 2
    agreement, unanimity needs `gate` coverage - is true rather than aspirational.
    """
    votes = [Counter() for _ in segments]
    for seg in segments:
        ntok = len(seg.tokens)
        if ntok == 0:
            continue
        per_token = time_claim[seg.idx] if time_claim is not None else None
        for m in range(ntok):
            per = Counter()
            spk = align_claim.get(seg.t0 + m)
            if spk is not None:
                per[spk] += 1.0
            if per_token:
                for s, w in per_token[m].items():
                    per[s] += w
            total = sum(per.values())
            if total <= 0:
                continue
            scale = (1.0 / total) if total > 1.0 else 1.0
            for s, w in per.items():
                votes[seg.idx][s] += w * scale
    return votes


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
def enclosing_agree(pair_js, pair_speakers, t0, t1, speaker, window=GAP_CAP):
    """Do the matched tokens either side of this segment both name `speaker`?

    The only use for this is rescuing a segment whose own evidence is thin, so
    "either side" has to mean NEARBY. Without the window it asked a question
    about distance-blind neighbours: in a transcript with one long unmatched
    stretch, the nearest matched token before a segment can be thousands of
    tokens back, and two such strangers agreeing says nothing at all about who
    is speaking here. That is the same mistake as measuring coverage without
    asking where the coverage sits. The bound is GAP_CAP, the same locality this
    file already trusts when it interpolates between two agreeing anchors.
    """
    before = bisect.bisect_left(pair_js, t0) - 1
    after = bisect.bisect_left(pair_js, t1)
    if before < 0 or after >= len(pair_js):
        return False
    if (t0 - pair_js[before]) > window or (pair_js[after] - t1) > window:
        return False
    return pair_speakers[before] == speaker and pair_speakers[after] == speaker


def decide(segments, votes, time_used, pairs, app_owner, min_confidence):
    pair_js = [j for (_i, j) in pairs]
    pair_speakers = [app_owner[i] for (i, _j) in pairs]
    counts = Counter()
    for seg in segments:
        seg.votes = votes[seg.idx]
        ntok = len(seg.tokens)
        if seg.locked:
            seg.reason = "human-labelled"
            counts["locked"] += 1
            continue
        if ntok == 0:
            seg.reason = "empty-segment"
            counts["empty"] += 1
            continue
        v = votes[seg.idx]
        if not v:
            seg.reason = "no-evidence"
            counts["no_evidence"] += 1
            continue
        ranked = v.most_common()
        top_spk, top_w = ranked[0]
        second_w = ranked[1][1] if len(ranked) > 1 else 0.0
        total_w = sum(w for _s, w in ranked)
        conf = (top_w - (total_w - top_w)) / float(ntok)
        conf = max(0.0, min(1.0, conf))
        seg.confidence = conf
        if top_w <= second_w + 1e-9:
            seg.reason = "contested"
            counts["contested"] += 1
            continue
        thin = (total_w < MIN_TOTAL_VOTE_WEIGHT
                and not (time_used and time_used[seg.idx]))
        if thin and not enclosing_agree(pair_js, pair_speakers, seg.t0, seg.t1, top_spk):
            seg.reason = "insufficient-evidence"
            counts["thin"] += 1
            continue
        if conf < min_confidence:
            seg.reason = "below-threshold"
            counts["below_threshold"] += 1
            continue
        seg.speaker = top_spk
        seg.reason = "labelled"
        counts["labelled"] += 1
    return counts


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render(lines, segments, note):
    out = list(lines)
    for seg in segments:
        if seg.locked or seg.speaker is None:
            continue
        rest = seg.rest
        if rest and not rest[:1].isspace():
            rest = " " + rest
        out[seg.line_no] = "%s**[%s] %s:**%s" % (seg.pre, seg.time_text, seg.speaker, rest)
    if note:
        idx = 0
        if out and out[0].strip() == "---":
            for k in range(1, len(out)):
                if out[k].strip() in ("---", "..."):
                    idx = k + 1
                    break
        block = [note, ""]
        if idx > 0 and idx <= len(out) and (idx - 1) < len(out) and out[idx - 1].strip():
            block = ["", note, ""]
        out[idx:idx] = block
    return out


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------
_NAME_STOP = {
    "i", "and", "the", "but", "so", "oh", "okay", "yeah", "well", "yes", "no",
    "um", "uh", "mhm", "hmm", "then", "that", "this", "there", "they", "you",
    "we", "he", "she", "it", "what", "when", "where", "how", "why",
}
_NAME_SCAN = re.compile(r"[A-Za-z']+|[.!?]")


def speaker_evidence(turns):
    per = {}
    total_tokens = sum(len(t.tokens) for t in turns) or 1
    for t in turns:
        e = per.setdefault(t.speaker, {
            "turns": 0, "tokens": 0, "questions": 0, "first_person": 0,
            "names": Counter(),
        })
        e["turns"] += 1
        e["tokens"] += len(t.tokens)
        text = " ".join(t.raw)
        if text.rstrip().endswith("?"):
            e["questions"] += 1
        for tok in t.tokens:
            if tok in ("i", "my", "me", "we", "our", "mine"):
                e["first_person"] += 1
        boundary = True
        for w in _NAME_SCAN.findall(text):
            if w in (".", "!", "?"):
                boundary = True
                continue
            if (not boundary and len(w) >= 3 and w[0].isupper()
                    and w.lower() not in _NAME_STOP):
                e["names"][w] += 1
            boundary = False
    out = []
    for spk in sorted(per, key=lambda s: -per[s]["tokens"]):
        e = per[spk]
        out.append({
            "speaker": spk,
            "turns": e["turns"],
            "tokens": e["tokens"],
            "word_share": round(e["tokens"] / float(total_tokens), 4),
            "question_turn_rate": round(e["questions"] / float(max(1, e["turns"])), 3),
            "first_person_rate": round(e["first_person"] / float(max(1, e["tokens"])), 4),
            "names_said": [{"name": n, "count": c} for n, c in e["names"].most_common(8)],
            "note": "evidence only; mapping this ID to a real person is the human's call",
        })
    return out


def confidence_bucket(conf):
    """Name the 0.1-wide bucket a confidence score belongs in.

    The obvious `conf - conf % 0.1` misfiles exact tenths: in binary floating
    point 0.3 % 0.1 is 0.0999…, so a confidence of 0.3 was reported in the
    "0.2-0.3" bucket. Scaling to an integer with a hair of slack puts every
    value in the bucket a reader of the report would name for it.
    """
    lo = min(9, max(0, int(conf * 10.0 + FRACTION_EPSILON))) / 10.0
    return "%.1f-%.1f" % (lo, lo + 0.1)


def decile_coverage(pairs, n_tokens):
    if n_tokens <= 0:
        return []
    buckets = [0] * 10
    for (_i, j) in pairs:
        b = min(9, (j * 10) // n_tokens)
        buckets[b] += 1
    size = n_tokens / 10.0
    return [round(buckets[k] / size, 3) if size else 0.0 for k in range(10)]


def decile_skew(pairs, n_tokens):
    if n_tokens <= 0 or not pairs:
        return []
    sums = [0.0] * 10
    cnts = [0] * 10
    for (i, j) in pairs:
        b = min(9, (j * 10) // n_tokens)
        sums[b] += (i - j)
        cnts[b] += 1
    return [round(sums[k] / cnts[k], 1) if cnts[k] else None for k in range(10)]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Transfer phone-app speaker turns onto a Whisper transcript. "
                    "Label transfer, not diarization; uncertain segments stay unlabeled.")
    p.add_argument("--whisper", required=True, help="Whisper .md transcript (read-only)")
    p.add_argument("--app-transcript", required=True, dest="app",
                   help="phone-app .txt transcript with [Speaker N] turns (read-only)")
    p.add_argument("--out", required=True, help="output .md to write")
    p.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE,
                   help="per-segment gate, 0..1 (default %.2f)" % DEFAULT_MIN_CONFIDENCE)
    p.add_argument("--report", default=None, help="optional JSON report path")
    p.add_argument("--min-match-rate", type=float, default=DEFAULT_MIN_MATCH_RATE,
                   help="mispair gate: the matched tokens must reach this share "
                        "of BOTH transcripts, on at least %d matched words "
                        "(default %.2f)" % (MIN_MATCH_TOKENS, DEFAULT_MIN_MATCH_RATE))
    p.add_argument("--force", action="store_true",
                   help="label even if the mispair gate trips (not recommended). "
                        "This flag means that and nothing else - it does not "
                        "authorise replacing an existing --out or --report")
    p.add_argument("--replace", action="store_true",
                   help="allow an existing --out / --report file to be replaced "
                        "(without this the run refuses rather than write over a "
                        "transcript somebody may have corrected)")
    p.add_argument("--quiet", action="store_true", help="suppress the stdout summary")
    return p


def fail(msg):
    sys.stderr.write("%s: error: %s\n" % (TOOL_NAME, msg))
    return 1


def rerun_command(argv, *extra):
    """This same run, plus `extra`, as one line the human can copy and paste.

    Naming a flag is not naming a command. The reader here is a genealogist with
    a folder open, and re-typing an invocation carrying four quoted paths at
    11pm is where the typos come from - so a message that asks for another run
    hands back the run he already made, with the flag that changes the answer
    added to it. A flag already on the line is not added twice.

    `sys.argv[0]` is echoed only when it really names THIS file, so the path he
    typed comes back to him. Under a test runner or any other embedding it names
    something else entirely (pytest's own `__main__.py`), and echoing that back
    would hand him a command that runs the wrong program; the bare filename is
    the fallback.
    """
    args = list(argv) if argv is not None else list(sys.argv[1:])
    tail = [flag for flag in extra if flag not in args]
    invoked = sys.argv[0] if sys.argv else ''
    script = invoked if invoked and os.path.basename(invoked) == TOOL_NAME else TOOL_NAME
    return 'python ' + shlex.join([script] + args + tail)


def alternative_name(flag, path):
    """`--out "session-2.md"` - the same name with a free number before its extension.

    Its own function because the refusal that uses it must offer an alternative
    for EVERY destination that is in the way, not just the first: sending the
    run to a fresh `--out` while `--report` still points at an existing file is
    refused all over again, for a reason the worked example itself created.

    The number is walked past whatever is already there. A flat "-2" is fine the
    first time and is itself a taken name on the third run of a session, which
    would hand the human a suggestion this same check refuses - the small
    version of the mistake this whole function exists to stop.
    """
    stem, ext = os.path.splitext(path)
    if not ext:
        ext = ".md" if flag == "--out" else ".json"
    n = 2
    while os.path.exists("%s-%d%s" % (stem, n, ext)) and n < 100:
        n += 1
    return '%s "%s-%d%s"' % (flag, stem, n, ext)


def main(argv=None):
    # Kept, not just parsed: several refusals hand the human back the command he
    # ran with one flag added, and reconstructing it from `args` would drop the
    # spellings he typed and print flags he never passed.
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    args = build_parser().parse_args(argv)

    if not (0.0 <= args.min_confidence <= 1.0):
        return fail("--min-confidence must be a number between 0 and 1 "
                    "(the documented gate is %.2f); try --min-confidence %.2f"
                    % (DEFAULT_MIN_CONFIDENCE, DEFAULT_MIN_CONFIDENCE))
    if not (0.0 <= args.min_match_rate <= 1.0):
        return fail("--min-match-rate must be a number between 0 and 1; "
                    "try --min-match-rate %.2f" % DEFAULT_MIN_MATCH_RATE)
    for path in (args.whisper, args.app):
        if not os.path.isfile(path):
            return fail("input not found: %s - check the path and run the command again"
                        % path)

    # Collision checks, in the order a mistyped command actually goes wrong.
    # Output-against-input was already covered; output-against-OTHER-OUTPUT was
    # not, and it is the quieter failure: the transcript is written first, the
    # JSON report lands on top of it, and the run still exits 0 announcing the
    # transcript it just destroyed.
    # `could_be_same_file`, never string equality: the inputs are archived
    # originals, and on a case-insensitive volume `--out Whisper.md` while
    # reading `whisper.md` compares unequal and passes.
    for out_path, flag in ((args.out, "--out"), (args.report, "--report")):
        if out_path and any(could_be_same_file(out_path, src)
                            for src in (args.whisper, args.app)):
            return fail("%s points at one of the two transcripts this tool reads "
                        "(%s); inputs are never modified. Give %s a new filename "
                        "and run the command again."
                        % (flag, os.path.basename(out_path), flag))
    if args.report and could_be_same_file(args.out, args.report):
        return fail("--out and --report point at the same file (%s). The JSON report "
                    "would be written over the attributed transcript. Give them "
                    "different filenames - for example --out \"%s\" --report \"%s\" - "
                    "and run the command again."
                    % (args.out,
                       os.path.splitext(args.out)[0] + ".md",
                       os.path.splitext(args.out)[0] + ".speakers.json"))

    # Both destinations are asked whether they could ever be written, BEFORE the
    # alignment and before either file is created. A --report that was never
    # going to be saveable is the whole subject of the recovery message further
    # down; catching it here means that message is reached only by the genuine
    # surprises (a disk that fills, a drive pulled out, permissions changed
    # under a running job) rather than by the ordinary typo, which is answered
    # for the price of a stat call and leaves nothing behind at all.
    #
    # Ahead of the existence refusal, deliberately: a FOLDER standing on the
    # output name is not an earlier run's transcript, and answering it with
    # "that file already exists, add --replace" would both misname the cause and
    # hand out a fix that cannot work - `os.replace` will not put a file on a
    # directory however authorised the run is.
    for out_path, flag in ((args.out, "--out"), (args.report, "--report")):
        if not out_path:
            continue
        problem = destination_problem(out_path, flag)
        if problem is not None:
            return fail(problem)

    # Refused, not warned about. The skill's Stage B command names its output
    # deterministically, so the second run of a session aims straight at the
    # first run's file - which by then may be the copy a human went through
    # correcting speaker labels. This tool cannot tell that copy from its own
    # earlier output (its AI marker survives a human's edits, so the marker
    # proves nothing), so it refuses both cases identically and lets him say
    # --replace. Checked before anything is read: the refusal costs nothing and
    # should not arrive after a minute of alignment.
    #
    # `isfile`, not `exists`: the sentence this guards says "already holds a
    # file from an earlier run", and the pre-flight above has already answered
    # for every other shape a taken name can have.
    warnings = []
    existing = [(flag, out_path)
                for out_path, flag in ((args.out, "--out"), (args.report, "--report"))
                if out_path and os.path.isfile(out_path)]
    if existing and not args.replace:
        named = " and ".join("%s (%s)" % (flag, os.path.basename(p))
                             for flag, p in existing)
        # The worked example names EVERY flag that is in the way, and keeps each
        # extension: "--out x-2.md" is no help to somebody whose --report is the
        # file at risk, and redirecting only the first of two destinations
        # produces a command this same check refuses on the second.
        example = " ".join(alternative_name(flag, p) for flag, p in existing)
        many = len(existing) > 1
        return fail(
            "%s already %s from an earlier run, and this tool will not write "
            "over %s. It cannot tell a transcript it wrote itself from one you "
            "have since corrected by hand, and a corrected speaker label cannot "
            "be got back. Either send this run somewhere else - for example %s "
            "- or, if %s really can go, run this exact command:\n    %s"
            % (named,
               "hold files" if many else "holds a file",
               "them" if many else "it",
               example,
               "those files" if many else "that file",
               rerun_command(argv, "--replace")))
    for flag, out_path in existing:
        warnings.append("%s already existed and was replaced at your request "
                        "(--replace): %s" % (flag, os.path.basename(out_path)))

    try:
        wh_lines, newline, trailing = read_text(args.whisper)
        app_lines, _an, _at = read_text(args.app)
    except OSError as e:
        return fail("could not read a transcript: %s. Check the file is readable, "
                    "then run the command again." % e)

    segments, wh_stream, wh_owner = parse_whisper(wh_lines)
    turns, variant, app_stream, app_owner = parse_app(app_lines)

    status = "ok"
    exit_code = 0
    counts = Counter()
    pairs = []
    align_stats = Counter()
    evidence = mispair_evidence(0, len(app_stream), len(wh_stream))
    time_used = None
    method = "align"

    if not segments:
        status = "no_whisper_segments"
        warnings.append("no '**[HH:MM:SS]**' segments found in the whisper transcript")
    elif variant == "plain" or not turns:
        status = "no_speaker_labels"
        warnings.append("app transcript carries no speaker labels (paragraph-only "
                        "export); nothing can be attributed - re-export with speakers")
    elif not app_stream or not wh_stream:
        status = "empty_transcript"
        warnings.append("one of the transcripts contains no words")
    else:
        pairs, align_stats = align_tokens(app_stream, wh_stream)
        evidence = mispair_evidence(len(pairs), len(app_stream), len(wh_stream))
        gate_ok = mispair_gate_ok(evidence, args.min_match_rate)
        if not gate_ok and not args.force:
            status = "mispair_suspected"
            exit_code = 2
            warnings.append(mispair_sentence(evidence, args.min_match_rate))
        else:
            if not gate_ok:
                warnings.append("mispair gate overridden with --force: "
                                + mispair_sentence(evidence, args.min_match_rate))
            _av, _anchored, _filled, align_claim = collect_align_votes(
                pairs, app_owner, wh_owner, len(segments))
            tv = collect_time_votes(segments, turns, len(segments), warnings)
            time_claim = None
            if tv is not None:
                _time_votes, time_used, time_claim = tv
                method = "align+time"
            # Pooled per TOKEN, never per speaker total: two methods that agree
            # about a word are one covered word (see pool_votes). With no
            # timestamp path this is exactly the alignment's own votes.
            votes = pool_votes(segments, align_claim, time_claim)
            counts = decide(segments, votes, time_used, pairs, app_owner,
                            args.min_confidence)

    # A run that refused publishes nothing, and a run that could attribute
    # nothing does not replace an earlier result with an unlabelled copy. Both
    # outputs are held back together: a JSON report describing a transcript that
    # was never written is its own small lie. A no-op with nothing to overwrite
    # still writes the pass-through copy, which is the documented honest no-op.
    held = [os.path.basename(p) for p in (args.out, args.report)
            if p and os.path.exists(p)]
    if status != "ok" and (status == "mispair_suspected" or held):
        plain = STATUS_IN_PLAIN_WORDS.get(status, status)
        for w in warnings:
            sys.stderr.write("%s: warning: %s\n" % (TOOL_NAME, w))
        if status == "mispair_suspected":
            sys.stderr.write(
                "%s: error: refused to label because %s, so nothing was "
                "written - anything already saved as %s is untouched. Check that "
                "the app transcript really belongs to this recording and run the "
                "command again. If you know it belongs but covers only part of "
                "the sitting - half the interview exported, the phone stopped "
                "early - the two percentages above are what that looks like, and "
                "--force labels the part that does line up. Either way it is a "
                "decision you own and must say out loud.\n"
                % (TOOL_NAME, plain, os.path.basename(args.out)))
            return exit_code
        if held:
            # --replace deliberately does not open this door. It says an
            # existing file may go; it does not say an unlabelled pass-through
            # copy is worth what is being thrown away, and nothing here produced
            # a result worth publishing.
            extra = ""
            if args.replace:
                extra = (" --replace does not cover this: it says that file may "
                         "be replaced, not that an unlabelled copy is worth "
                         "replacing it with.")
            return fail(
                "nothing could be attributed because %s, and %s already holds "
                "a file from an earlier run. Replacing it with an unlabelled "
                "copy would throw that work away, so nothing was written.%s Fix "
                "the input named in the warning above, or give --out and "
                "--report new filenames, then run the command again."
                % (plain, " and ".join(held), extra))

    labelled = counts.get("labelled", 0)
    note = ("<!-- speaker labels transferred by %s v%s from '%s'. "
            "This is label transfer from the phone app's own segmentation, NOT acoustic "
            "diarization: %d of %d segments labelled at min-confidence %.2f; unlabelled "
            "segments are honestly unknown. 'Speaker N' is an app-assigned ID, not a "
            "person - mapping it to a name is the human's decision. -->"
            % (TOOL_NAME, TOOL_VERSION, os.path.basename(args.app),
               labelled, len(segments), args.min_confidence))
    out_lines = render(wh_lines, segments, note if labelled else None)
    try:
        write_text(args.out, out_lines, newline, trailing)
    except OSError as e:
        return fail("could not write %s: %s. Check the folder exists and is "
                    "writable, then run the command again." % (args.out, e))

    n_wh = len(wh_stream)
    # Filenames only, never absolute paths. This JSON sits beside the recording
    # it describes and can end up attached to a source record; an archived file
    # must not carry the layout of one person's hard drive
    # (AGENTS_TOOLING.md §11 privacy, SPEC §12.4 alias-form paths).
    report = {
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "status": status,
        "method": method,
        "inputs": {
            "whisper": os.path.basename(args.whisper),
            "app_transcript": os.path.basename(args.app),
        },
        "output": os.path.basename(args.out),
        "paths_note": "filenames only - absolute machine paths are deliberately "
                      "not recorded, so this report is safe to file beside the "
                      "transcripts it describes",
        "settings": {
            "min_confidence": args.min_confidence,
            "min_match_rate": args.min_match_rate,
            "min_matched_tokens": MIN_MATCH_TOKENS,
            "gap_cap_tokens": GAP_CAP,
            "forced": bool(args.force),
            "replaced_existing": bool(args.replace and existing),
        },
        "app_transcript": {
            "variant": variant,
            "turns": len(turns),
            "tokens": len(app_stream),
            "distinct_speaker_ids": len({t.speaker for t in turns}),
            "has_timestamps": bool(turns) and all(t.start is not None for t in turns),
        },
        "whisper": {
            "segments": len(segments),
            "tokens": n_wh,
            "already_human_labelled": counts.get("locked", 0),
        },
        "alignment": {
            "matched_tokens": len(pairs),
            "match_rate_vs_app": round(evidence["app_coverage"], 4),
            "match_rate_vs_whisper": round(evidence["whisper_coverage"], 4),
            # The gate reads the smaller of the two above (matched / the LARGER
            # stream), so the number it judged on is in the report next to the
            # two it came from - not left to be re-derived by whoever reads this.
            "mispair_gate_rate": round(evidence["match_rate"], 4),
            "mispair_gate_passed": mispair_gate_ok(evidence, args.min_match_rate),
            "anchor_stages": {k: v for k, v in sorted(align_stats.items())},
            "coverage_by_decile": decile_coverage(pairs, n_wh),
            "mean_index_skew_by_decile": decile_skew(pairs, n_wh),
        },
        "labelling": {
            "labelled": labelled,
            "unlabelled": len(segments) - labelled - counts.get("locked", 0),
            "contested": counts.get("contested", 0),
            "below_threshold": counts.get("below_threshold", 0),
            "insufficient_evidence": counts.get("thin", 0),
            "no_evidence": counts.get("no_evidence", 0),
            "empty_segments": counts.get("empty", 0),
            "confidence_formula":
                "(winner_votes - other_votes) / segment_tokens  ==  coverage * (2*agreement - 1)",
            "vote_rule": "one vote per whisper word, shared out among whoever "
                         "claims it - two methods agreeing about a word are one "
                         "covered word, not two",
            "per_speaker_segments": dict(Counter(
                s.speaker for s in segments if s.speaker)),
            "confidence_histogram": dict(Counter(
                confidence_bucket(s.confidence) for s in segments if s.votes)),
        },
        "speaker_evidence": speaker_evidence(turns),
        "warnings": warnings,
        "caveats": [
            "Label transfer, not diarization: it inherits every mid-sentence split "
            "and every phantom speaker ID the phone app produced.",
            "Residual errors cluster within a few seconds of turn boundaries, "
            "where neither source is authoritative. The often-quoted 96% "
            "precision was measured under an earlier, over-counting version of "
            "the confidence score and has not been re-measured in the field.",
            "Proper names are the worst-transcribed tokens on both sides, so labels "
            "on name-bearing segments are the least trustworthy.",
            "No 'Speaker N' may become a person's name without human confirmation, "
            "and attribution is not evidence for a claim.",
        ],
    }
    if args.report:
        try:
            atomic_write(args.report,
                         json.dumps(report, indent=2, sort_keys=False) + "\n")
        except OSError as e:
            # The old message here said only "re-run with a --report path in a
            # writable folder", and that rerun could not work: the transcript
            # had just been written, so the next run met the existence refusal
            # and stopped. A recovery instruction the tool then refuses is worse
            # than none - it costs a round trip and it teaches the human that
            # the messages are not to be trusted.
            #
            # The transcript is KEPT rather than rolled back, and that is the
            # deliberate half of the fix. It is the expensive artifact and the
            # thing that was actually asked for; --report is optional and
            # diagnostic. Discarding a complete, correct transcript to keep a
            # symmetry with a file that may not even have been requested spends
            # the human's minute of alignment to buy nothing - and under
            # --replace it would be worse than nothing, because rolling back
            # would mean restoring the previous file on the same filesystem that
            # just refused a write, risking the corrected transcript that
            # authorising the replace was supposed to be the end of.
            #
            # So: say the transcript is there, say where, and print the whole
            # command that gets the report too - --replace included, because
            # this run created --out and the next one will be stopped by it.
            for w in warnings:
                sys.stderr.write("%s: warning: %s\n" % (TOOL_NAME, w))
            return fail(
                "the attributed transcript was written and has been KEPT - it "
                "is complete, and it is the file this run was for: %s. What "
                "failed is the JSON report, which could not be saved to %s: %s. "
                "The report only records how the labels were decided; the "
                "transcript does not need it, and %d of %d segments are "
                "labelled in it either way. To get the report too, free up that "
                "folder (or point --report somewhere else), then run this exact "
                "command:\n    %s\nIt carries --replace because %s now exists - "
                "this run wrote it a moment ago, so replacing it costs nothing. "
                "That stops being true once you have corrected speaker labels in "
                "it by hand: from then on send the new run to a different --out "
                "instead."
                % (args.out, args.report, e, labelled, len(segments),
                   rerun_command(argv, "--replace"),
                   os.path.basename(args.out)))

    for w in warnings:
        sys.stderr.write("%s: warning: %s\n" % (TOOL_NAME, w))

    if not args.quiet:
        total = len(segments)
        pct = (100.0 * labelled / total) if total else 0.0
        print("wrote %s" % args.out)
        print("status: %s (method: %s)" % (status, method))
        print("labelled %d/%d segments (%.1f%%) at min-confidence %.2f"
              % (labelled, total, pct, args.min_confidence))
        if total:
            print("unlabelled: contested %d, below-threshold %d, thin %d, none %d"
                  % (counts.get("contested", 0), counts.get("below_threshold", 0),
                     counts.get("thin", 0), counts.get("no_evidence", 0)))
        if pairs:
            print("token match rate: %.1f%% of the app transcript, %.1f%% of whisper"
                  % (100.0 * len(pairs) / max(1, len(app_stream)),
                     100.0 * len(pairs) / max(1, n_wh)))
        used = Counter(s.speaker for s in segments if s.speaker)
        if used:
            print("speaker IDs used: "
                  + ", ".join("%s (%d)" % (k, v) for k, v in sorted(used.items())))
        print("NOTE: 'Speaker N' is an app-assigned segment ID, not a person. "
              "Mapping it to a name is a human decision this tool will not make.")

    return exit_code


if __name__ == "__main__":
    # A Python traceback on the reader's screen is always a defect here
    # (AGENTS_TOOLING.md, "No traceback ever reaches the user"), so the last
    # filesystem surprise - a read-only folder, a vanished drive - is turned
    # into a plain sentence with a next step. Everything else still raises,
    # because a genuine bug should be loud during tool-building.
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\n%s: stopped before anything was written.\n" % TOOL_NAME)
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
    except OSError as exc:
        sys.exit(fail("the filesystem refused an operation: %s. Check the paths you "
                      "passed to --whisper, --app-transcript, --out and --report, "
                      "then run the command again." % exc))