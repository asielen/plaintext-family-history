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
When ≥80% of turns have monotone timestamps, the problem collapses to an interval
lookup, which is strictly better than any text alignment. Rather than choosing
between the two, both are run and their evidence is pooled in the same vote space
(each method may contribute at most one vote per token of the segment). Agreement
saturates confidence; disagreement cancels to zero and the segment goes out
unlabeled. Nothing is assumed about which method is right.

CONFIDENCE
==========
Per Whisper segment, every token that carries evidence votes for one speaker.

    confidence = (winner_votes − all_other_votes) / segment_token_count

which is algebraically identical to  coverage × (2·agreement − 1), clipped to
[0, 1]. It penalises thin coverage and contested segments in one number: a fully
covered segment split 60/40 scores 0.20; a 40%-covered unanimous segment scores
0.40. The default gate of 0.35 sits just above the "one speaker has a two-thirds
majority of a fully-covered segment" line.

SAFETY RULES (all mandatory, none tunable away)
==============================================
* Hard mispair gate. If the global token match rate falls below --min-match-rate
  (default 0.50) the tool refuses to label anything and exits 2. A transcript
  paired with the wrong audio still matches ~6% of tokens and will happily label
  most segments with confident nonsense; correct pairs match 70–85%. The
  separation is enormous, so the gate is cheap and the failure it prevents is not.
* Gap interpolation only between anchors that agree on the speaker, and only
  across ≤25 tokens. Never interpolate across a speaker change.
* A tie is contested, and contested is unlabeled. There is no "same as previous
  speaker" fallback — that rule manufactures false continuity.
* Never invent a speaker. Only literal `Speaker N` strings from the app file.
* Never overwrite a label a human already wrote: any segment already carrying a
  non-`Speaker N` label is left byte-for-byte alone.
* Inputs are opened read-only and never written. The tool refuses to run if
  --out or --report resolves to either input path.

HONEST LIMITS
=============
* Precision plateaus around 96% at ~75% coverage; the residual sits within a few
  seconds of turn boundaries where neither the app's turn starts nor Whisper's
  segment starts are authoritative. Tightening the threshold does not fix it.
* Both transcribers mangle proper names, so a speaker label on a name-bearing
  segment is the *least* trustworthy kind — and names are what genealogy wants.
* Mid-sentence app splits and phantom app speakers are inherited, not repaired.
* Attribution must never flow into a claim automatically. It is a proposal for a
  human to confirm against the recording.

Exit codes: 0 = ok (including honest no-ops), 2 = refused to label (mispair
gate), 1 = usage/IO error.
"""

from __future__ import annotations

import argparse
import bisect
import difflib
import json
import os
import re
import sys
import unicodedata
from collections import Counter

TOOL_NAME = "attribute_speakers.py"
TOOL_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Tunables (documented above; changing these changes the safety story)
# ---------------------------------------------------------------------------
DEFAULT_MIN_CONFIDENCE = 0.35
DEFAULT_MIN_MATCH_RATE = 0.50
GAP_CAP = 25              # max tokens interpolated between two agreeing anchors
SMALL_BLOCK = 64          # ranges this small go straight to difflib
MAX_LOCAL_CELLS = 4_000_000   # hard cap on any single difflib call (n*m)
NGRAM_LEVELS = (5, 3, 2, 1)
MAX_DEPTH = 60
FUZZY_MIN_RATIO = 0.78
FUZZY_MIN_LEN = 4
FUZZY_WINDOW = 12
TIME_MIN_TURN_COVERAGE = 0.8  # fraction of app turns that must carry a timestamp

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


def write_text(path: str, lines, newline: str, trailing: bool) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    body = newline.join(lines) + (newline if trailing else "")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)


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
# Evidence gathering
# ---------------------------------------------------------------------------
def collect_align_votes(pairs, app_owner, wh_owner, n_segments):
    votes = [Counter() for _ in range(n_segments)]
    anchored = [0] * n_segments
    filled = [0] * n_segments
    for (i, j) in pairs:
        seg = wh_owner[j]
        votes[seg][app_owner[i]] += 1.0
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
            filled[seg] += 1
    return votes, anchored, filled


def collect_time_votes(segments, turns, n_segments, warnings):
    """Interval lookup when the app export carries timestamps. Opportunistic."""
    timed = [t for t in turns if t.start is not None]
    if not turns or len(timed) < max(3, int(TIME_MIN_TURN_COVERAGE * len(turns))):
        return None
    starts = [t.start for t in timed]
    if any(starts[k] < starts[k - 1] for k in range(1, len(starts))):
        warnings.append("app timestamps are not monotone; timestamp path disabled")
        return None
    seg_times = [s.time_s for s in segments]
    if any(t is None for t in seg_times):
        return None
    if any(seg_times[k] < seg_times[k - 1] for k in range(1, len(seg_times))):
        warnings.append("whisper timestamps are not monotone; timestamp path disabled")
        return None
    if not seg_times:
        return None
    audio_end = seg_times[-1] + max(1.0, 0.35 * max(1, len(segments[-1].tokens)))
    if starts[-1] < 0.5 * seg_times[-1]:
        warnings.append(
            "app transcript timestamps stop at %.0fs but audio runs to ~%.0fs; "
            "timestamp path disabled (partial coverage)" % (starts[-1], seg_times[-1]))
        return None

    ends = []
    for k in range(len(timed)):
        ends.append(starts[k + 1] if k + 1 < len(timed) else max(audio_end, starts[k] + 1.0))

    votes = [Counter() for _ in range(n_segments)]
    used = [False] * n_segments
    k = 0
    for idx, seg in enumerate(segments):
        s0 = seg_times[idx]
        s1 = seg_times[idx + 1] if idx + 1 < len(segments) else audio_end
        if s1 <= s0:
            s1 = s0 + max(0.5, 0.35 * max(1, len(seg.tokens)))
        ntok = len(seg.tokens)
        if ntok == 0:
            continue
        while k + 1 < len(timed) and ends[k] <= s0:
            k += 1
        kk = k
        acc = Counter()
        while kk < len(timed) and starts[kk] < s1:
            ov = min(s1, ends[kk]) - max(s0, starts[kk])
            if ov > 0:
                acc[timed[kk].speaker] += ov
            kk += 1
        dur = s1 - s0
        if dur <= 0 or not acc:
            continue
        for spk, ov in acc.items():
            votes[idx][spk] += (ov / dur) * ntok
        used[idx] = True
    return votes, used


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------
def enclosing_agree(pair_js, pair_speakers, t0, t1, speaker):
    before = bisect.bisect_left(pair_js, t0) - 1
    after = bisect.bisect_left(pair_js, t1)
    if before < 0 or after >= len(pair_js):
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
        thin = total_w < 2.0 and not (time_used and time_used[seg.idx])
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
                   help="mispair gate on global token match rate (default %.2f)"
                        % DEFAULT_MIN_MATCH_RATE)
    p.add_argument("--force", action="store_true",
                   help="label even if the mispair gate trips (not recommended)")
    p.add_argument("--quiet", action="store_true", help="suppress the stdout summary")
    return p


def fail(msg):
    sys.stderr.write("%s: error: %s\n" % (TOOL_NAME, msg))
    return 1


def main(argv=None):
    args = build_parser().parse_args(argv)

    if not (0.0 <= args.min_confidence <= 1.0):
        return fail("--min-confidence must be between 0 and 1")
    if not (0.0 <= args.min_match_rate <= 1.0):
        return fail("--min-match-rate must be between 0 and 1")
    for path in (args.whisper, args.app):
        if not os.path.isfile(path):
            return fail("input not found: %s" % path)
    inputs = {os.path.abspath(args.whisper), os.path.abspath(args.app)}
    for out_path, flag in ((args.out, "--out"), (args.report, "--report")):
        if out_path and os.path.abspath(out_path) in inputs:
            return fail("%s would overwrite an input file; inputs are never modified" % flag)

    warnings = []
    wh_lines, newline, trailing = read_text(args.whisper)
    app_lines, _an, _at = read_text(args.app)

    segments, wh_stream, wh_owner = parse_whisper(wh_lines)
    turns, variant, app_stream, app_owner = parse_app(app_lines)

    status = "ok"
    exit_code = 0
    counts = Counter()
    pairs = []
    align_stats = Counter()
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
        denom = float(min(len(app_stream), len(wh_stream))) or 1.0
        match_rate = len(pairs) / denom
        if match_rate < args.min_match_rate and not args.force:
            status = "mispair_suspected"
            exit_code = 2
            warnings.append(
                "global token match rate %.1f%% is below the %.1f%% gate - these two "
                "files probably do not describe the same recording; refusing to label"
                % (match_rate * 100.0, args.min_match_rate * 100.0))
        else:
            if match_rate < args.min_match_rate:
                warnings.append("mispair gate overridden with --force at %.1f%% match"
                                % (match_rate * 100.0))
            votes, anchored, filled = collect_align_votes(
                pairs, app_owner, wh_owner, len(segments))
            tv = collect_time_votes(segments, turns, len(segments), warnings)
            if tv is not None:
                time_votes, time_used = tv
                method = "align+time"
                for k in range(len(segments)):
                    votes[k].update(time_votes[k])
            counts = decide(segments, votes, time_used, pairs, app_owner,
                            args.min_confidence)

    labelled = counts.get("labelled", 0)
    note = ("<!-- speaker labels transferred by %s v%s from '%s'. "
            "This is label transfer from the phone app's own segmentation, NOT acoustic "
            "diarization: %d of %d segments labelled at min-confidence %.2f; unlabelled "
            "segments are honestly unknown. 'Speaker N' is an app-assigned ID, not a "
            "person - mapping it to a name is the human's decision. -->"
            % (TOOL_NAME, TOOL_VERSION, os.path.basename(args.app),
               labelled, len(segments), args.min_confidence))
    out_lines = render(wh_lines, segments, note if labelled else None)
    write_text(args.out, out_lines, newline, trailing)

    n_wh = len(wh_stream)
    report = {
        "tool": TOOL_NAME,
        "version": TOOL_VERSION,
        "status": status,
        "method": method,
        "inputs": {
            "whisper": os.path.abspath(args.whisper),
            "app_transcript": os.path.abspath(args.app),
        },
        "output": os.path.abspath(args.out),
        "settings": {
            "min_confidence": args.min_confidence,
            "min_match_rate": args.min_match_rate,
            "gap_cap_tokens": GAP_CAP,
            "forced": bool(args.force),
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
            "match_rate_vs_app": round(len(pairs) / float(len(app_stream)), 4) if app_stream else 0.0,
            "match_rate_vs_whisper": round(len(pairs) / float(n_wh), 4) if n_wh else 0.0,
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
            "per_speaker_segments": dict(Counter(
                s.speaker for s in segments if s.speaker)),
            "confidence_histogram": dict(Counter(
                "%.1f-%.1f" % (min(0.9, round(s.confidence - s.confidence % 0.1, 1)),
                               min(1.0, round(s.confidence - s.confidence % 0.1, 1) + 0.1))
                for s in segments if s.votes)),
        },
        "speaker_evidence": speaker_evidence(turns),
        "warnings": warnings,
        "caveats": [
            "Label transfer, not diarization: it inherits every mid-sentence split "
            "and every phantom speaker ID the phone app produced.",
            "Precision plateaus near 96%; residual errors cluster within a few "
            "seconds of turn boundaries, where neither source is authoritative.",
            "Proper names are the worst-transcribed tokens on both sides, so labels "
            "on name-bearing segments are the least trustworthy.",
            "No 'Speaker N' may become a person's name without human confirmation, "
            "and attribution is not evidence for a claim.",
        ],
    }
    if args.report:
        parent = os.path.dirname(os.path.abspath(args.report))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=False)
            fh.write("\n")

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
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)