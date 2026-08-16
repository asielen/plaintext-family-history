---
name: import-recordings
description: >
  Run when the human hands over recordings — "import these interviews", "here's a zip of voice memos
  off my phone", "get grandma's recording into the archive", "I recorded a conversation with Dad",
  "there's a video from the family reunion". Content-hashes every incoming audio or video file
  against what is already archived and skips the duplicates, reads the real recording date out of
  the container instead of trusting the filename, groups the recordings from one sitting into a
  single session source under `documents/interviews/{interviewee}-{yyyy-mm-dd}/` beside the transcripts they
  shipped with, always adds a fresh local
  whisper pass (never replacing the app transcript), and proposes — never assumes — which speaker is
  which person. Interactive by default: it confirms each recording and hands off to process-source
  and mine-transcript. Automatic mode carries straight on into claim drafting, and even then every
  claim lands `suggested` and every speaker name waits for the human's yes. Never edits, renames, or
  deletes an original recording or transcript outside `fha process`.
---

# import-recordings

The recordings on-ramp. It stands in front of `fha process` the way `import-notes` stands in front of
it for paper: pair a recording with the transcript that shipped beside it, keep both originals
byte-for-byte intact, add a machine pass of its own, and land the whole set in one folder under one
S-id. Intake is not extraction — the interview earns its claims later, through `mine-transcript` and
`review-claims`. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Run when the human puts recordings in front of you: a `.zip` bundle exported from a phone app (audio
or video plus the app's own transcript), a loose `.m4a` / `.mp3` / `.wav` / `.mp4` / `.mov`, a folder
of them, or an inbox that `fha capture --ingest` already swept. Audio and video are the same job —
video is a recording like any other, and whisper reads its audio track.

Two modes, and the human picks:

- **Interactive** (the default, and what you use unless told otherwise). One recording at a time.
  Every destination, every slug, every speaker proposal is confirmed before it is written. Ends by
  handing off to `process-source` / `mine-transcript`; drafts nothing.
- **Automatic** — only on an explicit up-front say-so: *"import all of these, don't ask me per
  file"*, *"do the whole zip and mine them"*. That one sentence is the batch approval that
  per-item confirmation would otherwise collect, and it is also the explicit ask `mine-transcript`
  requires — nothing mines silently. Automatic mode still previews the whole plan first, still stops
  dead on a duplicate or a low-confidence speaker map, and still leaves every claim `suggested`.

This is **migration mode** (AGENTS.md §"Operating modes") — bulk intake of existing material, the
highest-risk mode. PLAN → DRY-RUN → human approval → bounded batches (≤200 files) → report. Say so in
your first reply and don't drift out of it.

## The contract for this skill (state it before you start)

- Every claim this skill's continuation drafts is `status: suggested`. **Nothing** reaches `accepted`
  here — the human is the only gate, and only through `fha claim … --status accepted`.
- **No original content is altered.** Audio and video are never transcoded, trimmed, or re-encoded;
  transcript text is never rewritten, condensed, or "cleaned up". The one sanctioned touch is the
  documents-root processing rename (SPEC §5.1, §12.1) via `fha process`.
- **Both transcripts survive, always.** The app transcript and the whisper pass sit side by side as
  separate `files:` entries. Never replace, detach, or supersede one with the other, even when
  whisper is obviously better — they disagree in exactly the spots a reviewer needs to compare, and
  provenance beats tidiness.
- **Speaker names are proposals.** A machine may transfer labels; only the human may say who is
  speaking. Unknown stays unknown.
- Every pass over a source — whisper, label transfer, mining — is recorded in that source's
  `## AI Passes` block before you hand back (_STANDARD.md §3.3, SPEC §6.3). Nothing lints for it.
- Living people: a recording of someone alive is ordinary archive material, but the person record's
  `living:` flag must be right, and anything the human flags as sensitive gets `restricted: true` on
  the source (SPEC §19).

Two things in this flow are **not** `fha` verbs and are enacted here only under the owner exception
in `_STANDARD.md` §6 — recorded in this folder's [`GAP.md`](GAP.md), never silently: the content-hash
duplicate check (wanted: `fha media dedupe`, enacted by `scripts/find_duplicate_media.py`) and the
container-metadata date read (wanted: `fha media probe`, enacted by `ffprobe`). The local whisper run
and the speaker-label transfer are owned by this skill's own `scripts/`. All three scripts are
read-only on the archive: they report, and `fha process` is still the only thing that writes.
If a further capability turns out to be missing, **stop and name it** — do not hand-roll it in prose.

## Flow

Invoke `fha` through the archive-root launcher (`./fha` / `.\fha`, AGENTS.md §Tools). When the
documents root sits **outside** the archive, CWD-based auto-detection can fail from a scratch
directory — pass `--root <archive>` on every call rather than guessing.

### Stage A — plan and dedupe (before any write)

1. **State the mode and lay out the plan.** Count what arrived: how many bundles, how many media
   files, how many carry a transcript, total duration if you know it. Extract every `.zip` to the
   scratchpad — **the incoming bundle is never consumed, emptied, or deleted** (AGENTS.md §"Don'ts";
   even `fha capture --ingest` only parks swept bundles in `.ingested/`). Show the human the list
   before you touch the documents root.

2. **Pair each recording with the transcript that shipped beside it.** Inside a bundle that is
   usually one media file plus one `.txt`/`.md`. Pair on stem, then on timestamp, then ask. A
   transcript with no media is not an intake — say so and leave it staged. A bundle with two recordings from the same
   sitting is one source with two recordings (step 5); from different days, two sources.

3. **Content-hash every incoming media file against what is already archived, and skip the
   duplicates.** This is not hypothetical: in a real 16-zip phone export, **6 were byte-identical to
   audio already filed**. Compare byte size first and hash only on a size collision — a whole-root
   hash sweep is wasteful and a whole-root read is discouraged (_STANDARD.md §8). That is exactly
   what this skill's own script does, and it is the only thing that answers the question:

   ```
   python ".claude/skills/import-recordings/scripts/find_duplicate_media.py" "<incoming file or folder>" --root "<archive>"
   ```

   It reads sizes from the directory entries of the archive's configured media roots, opens nothing
   unless a size collides, then SHA-256s both sides. Read-only on both sides — it renames, moves and
   imports nothing. Exit **0** means no incoming file has a byte twin; exit **2** means at least one
   does, and it prints which archived file (and its S-id, when the filename carries one). Confirm
   the twin's record with `fha find <S-id>` before you say anything to the human.

   **What a `new` verdict promises, and why that is the whole contract.** This gate authorises
   imports, so `new` is not "I found no twin" — it is *"I examined everything I claim to have
   examined, and none of it was this recording."* Five review rounds each found a different way for
   the old script to examine less than that and still answer confidently, so the promise is now
   written as five parts, and the script fails closed on any of them:

   - **Roots** — every media root `fha.yaml` names was resolved and is readable. A configured root
     that is not there right now (external drive unplugged, folder renamed) refuses the run; it is
     not an empty root, its recordings are simply unread.
   - **Enumeration** — every folder under those roots was listed, hidden ones like
     `documents/.private/` and folders reached through a directory symlink included. A folder the
     human made for the material he is most careful about, or a library kept where it already lives,
     is the last place this check may skip.
   - **Domain** — the same audio/video rule applies to both sides, and every path you name on the
     command line is either checked or printed as `SKIPPED` with the reason. "Archived" means
     **filed**: the inbox is staging even when it sits inside the photo library, so a recording
     waiting there is never reported as already filed.
   - **Candidates** — every archived recording of exactly the same byte size was opened and hashed.
   - **Batch** — the incoming files were compared against each other too. One afternoon exported
     twice under two names is two honest `new` verdicts that are together wrong; the script clears
     the first and marks the rest `DUPLICATE … in the same batch`. Import the one it names.

   Two more things follow from that. A file you hand over that already lives in a media root is the
   archive's own copy: it comes back `DUPLICATE … already filed in the archive`, never `new`, so
   selecting a filed recording (or handing over `documents/interviews/`) cannot authorise a second
   import of it. And if you ever find yourself narrowing the check to make it answer — pointing
   `--media-root` at a subfolder, skipping a root that will not mount — stop: that is examining less
   and reporting the same confidence, which is precisely the failure this contract exists to close.

   Exit **1** is usage or configuration: a path that is not there, PyYAML not installed, or a media
   root `fha.yaml` names that is not there on this machine. The script needs PyYAML to read which
   folders hold the recordings (those folders are allowed to sit on another drive) and refuses rather
   than guessing — a guess searches the wrong folder and calls an already-filed recording new. It is
   the same PyYAML every `fha` command needs, so the fix is `python -m pip install pyyaml`; the
   message says so. A named-but-absent root is the same kind of refusal for the same reason: the
   message tells the human to reconnect the drive, create the folder, or fix the path, and any of the
   three is a one-step fix. Do not work around either by pointing `--media-root` at a folder you
   picked yourself.

   `--json <path>` saves the same findings as a file, and that path is the one thing in this step
   that writes: give it a name of its own in the scratchpad, never a recording's name and never a
   path inside a media root. The script refuses the run - before it hashes anything - if that path
   lands on an incoming recording, on an archived one, on `fha.yaml`, or anywhere inside a media
   root, because a report written over a recording would destroy it *after* clearing it as safe to
   import. It asks the filesystem rather than comparing the spelling, so `./`, a symlink, a hard
   link and a second mount of the same disk are all one file to it; and where the report path does
   not exist yet - the ordinary case - it falls back to comparing the folder plus the name with
   capitalisation and accent spelling folded away. That fold is deliberately over-eager: `--json
   Interview.m4a` is refused beside an incoming `interview.m4a` on every machine, not only on the
   macOS and Windows volumes where the two names really are one file, because being told to pick
   another filename costs one word and being wrong costs the recording. (A different name is still
   a different file: `dedupe-report.json` beside `dedupe-report.m4a` writes normally.)

   Exit **3** is the third answer: **the check could not finish**. Nothing it printed as `UNCHECKED`
   is cleared, and the move is to fix what it names and re-run the whole bundle, not to import the
   part that happened to pass. It fires when something could not be read — the drive holding the
   archive's `documents` root is not plugged in, a folder cannot be listed, `fha.yaml` will not
   parse, an archived recording of exactly the same size could not be opened. Those files are printed
   as `UNCHECKED`, and an unchecked file is not a new file: the twin, if there is one, is precisely
   the recording nobody could read. Tell the human in his own terms — *"I can't tell yet whether
   these are already filed: the folder holding your interviews isn't readable right now"* — name the
   thing to fix, and re-run the same command afterwards. Never fall back to filenames, never fall
   back to `fha search`, and never narrow `--media-root` onto the part that happens to be readable
   just to get a clean exit; that is the gate answering a question you did not ask.

   `fha search "<distinctive phrase from the transcript's first minute>"` is a *different* question
   and a weaker answer: it finds a transcript that reads alike, which is a lead, not proof of an
   identical recording. Use it only to explain a near-miss (same sitting re-exported, trimmed, or
   re-encoded, so the bytes differ) — never in place of the hash check. Until `fha media dedupe`
   ships (GAP.md, project issue #43) the script is the check.

   On a match: **report and stop for that item** — *"this is the same file as the recording already
   filed at `documents/interviews/…` — skipping it, nothing imported."* Do not re-process it:
   `fha process` refuses a file already carrying an S-id, and forcing a second pass would mint a
   duplicate S-id for one recording. Where the match is another file in the same batch, say so in
   those words — *"these two files are the same recording under two names; I'll import the one and
   leave the other"* — and import exactly the file the script names, because one sitting is one
   source record.

   If the duplicate's bundle carries a **transcript the archive lacks** — the phone's *timestamped*
   export where the archive only has the bracket form, say — that is an ordinary attach onto the
   **existing** source, and `fha process --more` is exactly the verb for it (step 9's second form,
   pointed at the already-filed primary). Attaching is not importing: it mints no S-id and creates
   no second source. Confirm with the human first, since it adds a file to a record he already
   reviewed. Compare the two transcripts before offering — the archive's copy may be the richer one,
   in which case there is nothing to add and you say so.

4. **Read the real recording date out of the container, not the filename - and settle the
   recording's timezone before you convert anything.** Filenames lie; app exports are named for
   when the *file* was written or for nothing at all. The container's `creation_time` is the honest
   field - and it carries a trap that goes in the record, not in your head:

   > QuickTime/MP4 writes `creation_time` in **UTC, at the moment the recording stopped**. The
   > clock in an app-written filename is **local time at the moment it started**. The two disagree
   > by the recording's own length plus the UTC offset - enough to cross midnight on a long evening
   > interview.

   ```
   ffprobe -v quiet -print_format json -show_format "<file>"
   ```

   That trap has a second half, and it is the one that produces a wrong date in silence: **a UTC
   instant is not a calendar date until you know where the clock was standing.** Converting
   `creation_time` with *your* machine's timezone answers a question nobody asked - a Sunday
   afternoon recorded two zones east converts to the wrong day and looks perfectly ordinary doing
   it. So establish the recording's offset FIRST, before any arithmetic and before you name a date.
   Three places to get it, strongest first:

   1. **The container's own local timestamp.** Phones and most modern cameras write
      `com.apple.quicktime.creationdate` alongside `creation_time` - it is already in the
      `format.tags` of the command above, and it is local time *with* its offset
      (`1998-06-14T20:15:00-0500`). That is the recording's own timezone, written by the device
      that was in the room. It settles the question outright; ask nothing.
   2. **The filename clock, solved for the offset.** When the app filename carries a clock time,
      the free cross-check runs in the useful direction: local stop is `filename_time + duration`
      and UTC stop is `creation_time`, so `offset = filename_time + duration − creation_time`.
      Round to the nearest quarter hour. A fit that misses by more than a couple of minutes means
      the filename clock is not what you took it for, so it answers nothing - go to 3.
   3. **Ask him, before you say any date.** *"Where was this recorded - the same timezone you're in
      now?"* He was there; he knows. One short question up front costs a sentence, and it is the
      only thing that reaches the truth when the container carries nothing. Asking it *after*
      converting is the bug: by then a wrong exact date is already on the page.

   Then, and only then, the arithmetic: local start is `creation_time` − duration rendered in
   **that** zone, and its day is the date - `source_date: 1998-06-14`. If 1 and 2 both answered,
   the date is confirmed from two independent directions; say so. If the sitting itself runs past
   midnight, the day it *started* is still the `source_date`; note the overrun rather than
   splitting one interview across two dates.

   **If the timezone is still unknown, do not write an exact date.** An unconfirmed value must
   never be presented as exact (AGENTS.md §"The contract"), and the archive's date vocabulary
   already carries the honest form: write the interval spanning the days the recording could have
   started on - `source_date: 1998-06-14/1998-06-15` - say in one plain sentence why it is two days
   instead of one, and move on. A skill that stalls on a fuzzy date has failed the human; so has
   one that invents a precise one. The interval is a standing invitation to anyone who can narrow
   it later, while a wrong exact day is invisible forever.

   Either way the caveat above is copied verbatim into the source's `## Notes` - the next reader
   will hit it too - **together with the timezone you used and where it came from**: *"container
   offset -0500"*, *"derived from the filename clock"*, *"confirmed with the human"*, or *"not
   established, so the date is an interval"*. That one line is what lets the next reader redo the
   arithmetic instead of re-guessing it.

5. **Group by sitting, not by file, then pre-file into one folder per session.** Phone apps split a
   single afternoon into one file per topic. **Those are one source, not many** — one real archive's
   sessions carry a dozen topic recordings under one S-id, and a session is the unit a reader
   actually looks for. One session, one folder, one S-id:
   `documents/interviews/{interviewee}-{yyyy-mm-dd}/`. Recordings on *different* days are different
   sources even when the topic continues.

   Group on the **local** dates from step 4 - the ones you converted with a settled timezone - not
   on filenames, which carry relative weekday labels that collapse different days onto the same
   word. Ask if a sitting genuinely straddles midnight or a day holds two unrelated visits. A
   recording whose date is still an interval is not grouped by guess: ask which sitting it belongs
   to, or leave it staged and say so.

   The folder name and the slug take **one** day even when `source_date` is an interval. That is
   not a contradiction and it is not a licence to round: the folder is human convenience with no
   machine meaning (see below), so it takes the earlier candidate day, you say out loud that you
   did, and the record keeps the honest interval. Never let a tidy folder name talk you into a tidy
   `source_date` - the filename is a label, the `source_date` is a claim about the world.

   Copy the files in **before** processing: a file pre-filed into a subfolder is renamed **in place**
   by `fha process`, while a file sitting at the documents-root top level gets relocated into
   `documents/{type}/` (TOOLING §6, owner decision 2026-07-22; SPEC §12.1, folders are the human's
   projection).

   Naming follows one rule that makes everything sort itself: **name every file on disk for the
   session, and let the `--more` role carry the topic.** `fha process` derives its slug from the
   *title* unless you pass `--slug`, and `--more` renames to `{stem}-{role}_{S-id}` — so a session
   stem in means a topic-clustered folder out. The session's first recording is the `primary` and
   takes the bare session slug (which also names the source record); every later recording rides
   along as `{topic}-audio`:

   ```
   documents/interviews/hartley-thomas-1998-06-14/          # the assets; the record it shares an S-id with
                                                            # is sources/interview/hartley-thomas-1998-06-14_S-wb91h3hjrr.md
     hartley-thomas-1998-06-14-farm-audio_S-wb91h3hjrr.m4a
     hartley-thomas-1998-06-14-farm-transcript_S-wb91h3hjrr.txt
     hartley-thomas-1998-06-14-farm-whisper-transcript_S-wb91h3hjrr.md
     hartley-thomas-1998-06-14-school-audio_S-wb91h3hjrr.m4a
     …
     hartley-thomas-1998-06-14-wedding-transcript_S-wb91h3hjrr.txt      # wedding is the primary:
     hartley-thomas-1998-06-14-wedding-whisper-transcript_S-wb91h3hjrr.md
     hartley-thomas-1998-06-14_S-wb91h3hjrr.m4a                         # …so its audio is the bare stem
   ```

   Because each topic's three files share the `{session}-{topic}` prefix, they cluster together in a
   directory listing even though the folder holds a whole afternoon.

   Say plainly what the folder is and is not: **it is human convenience with no machine meaning.**
   The shared S-id is the binding; the folder could be reshuffled tomorrow and `fha reconcile` would
   heal the paths. In interactive mode, confirm the slug with the human — he is the one who will
   read it in five years. No new top-level archive folders, ever; subfolders under an existing asset
   root are free.

### Stage B — transcribe and attribute (judgment)

6. **Run whisper on every recording — even the ones that already shipped with a transcript.** App
   transcripts garble exactly what genealogy needs: proper names. Whisper's text and timings are far
   better; the app's turn structure is better than whisper's (which has none). You keep both because
   each is right about something the other is wrong about.

   ```
   python ".claude/skills/transcribe-audio/scripts/transcribe_audio.py" "<audio-or-video>" --model medium --outdir "<scratch>" --name "<stem>"
   ```

   `--outdir` is the **scratchpad**, always: never the incoming bundle, never a media root. The
   three whisper outputs are `{name}.txt`, `{name}.srt` and `{name}.md`. That script will not write
   over files it cannot prove it wrote — a complete set of its own three is a no-op it reports, and
   anything else already sitting under those names stops the run rather than being replaced (only
   its own `--force` overrides that) — so a `--name` matching the app transcript's stem in the folder
   the app transcript lives in now stops the run instead of destroying an original. Do not lean on
   that: pick the session-and-topic stem, write it into scratch, and let `fha process --more` be the
   only thing that puts a file into the archive.

   `medium` is the floor when the goal is recovering garbled proper names — and it usually is.
   Budget roughly half of realtime per file on CPU. Run recordings **one at a time**: faster-whisper
   already spreads across cores, so parallel jobs contend for the same cores, finish no sooner, and
   make the machine unusable meanwhile. Long queues belong in a background script that logs per file
   and simply re-runs the command for every recording, so a reboot costs one file and not the batch:
   the transcribe script already answers "is this one done?" itself — a complete set of its three
   outputs is a no-op it reports, and a half-written or interrupted set is redone. Do not have the
   wrapper skip a recording because *some* output file exists; that is the test that makes a
   half-published transcript permanent.

   Video needs no special handling — whisper reads the audio track in place. If a container is
   unreadable, the fix is a **new derived audio extraction filed beside the video** with
   `role: audio-extract`; it is never an edit to the video file.

7. **Transfer speaker turns onto the whisper text — under gates, or not at all.** The app knows who
   spoke; whisper knows what was said and when. Merging them produces a **third, machine-synthesized
   artifact** — legitimate only because both originals stay untouched beside it.

   Before any alignment, **ask whether the app can re-export with timestamps.** The same app that
   writes `[Speaker 1]` blocks also writes a numbered form carrying `00:01`-style turn times, and the
   two are byte-identical in text and labels. With timestamps this stops being alignment and becomes
   an interval lookup — no fuzzy matching, no failure mode. One question saves the whole gamble.

   Falling back to text alignment:

   ```
   python ".claude/skills/import-recordings/scripts/attribute_speakers.py" --whisper "<whisper.md>" --app-transcript "<app.txt>" --out "<stem>.md" --report "<stem>.speakers.json"
   ```

   All four paths must be different — the two inputs, `--out`, and `--report`. The script refuses
   the run rather than let a mistyped filename overwrite a transcript (including `--report` landing
   on `--out`), and both outputs are written through a temporary file, so an interrupted run never
   leaves a half-written transcript behind. **Both destinations are checked before the alignment
   starts**, not after it: a folder standing on the output name, or a path whose parent is a file,
   is refused for the price of a glance at the folder, so the ordinary typo costs nothing and leaves
   nothing behind.

   **If the report cannot be saved, the transcript is kept.** The two outputs are not all-or-nothing,
   and deliberately so: `--out` is the artifact the run exists to produce and the expensive one to
   remake, while `--report` is an optional JSON record of *how* the labels were decided. If the
   transcript is written and then the report write fails anyway — a disk that fills, a drive
   unplugged mid-run — the script keeps the transcript, says so in plain words, names the file you
   now have, and prints the **complete** command that saves the report too. That command carries
   `--replace`, because the run has just created `--out` and the next run would otherwise be stopped
   by the rule below; replacing it costs nothing while it is still the file this run wrote, and stops
   being safe the moment you correct speaker labels in it by hand. Exit code 1 here means "not
   everything you asked for happened", not "nothing happened" — read the message, and check the
   transcript is where it says.

   **A second run does not overwrite the first one.** `--out` names the same `<stem>.md` every time,
   and by the second run that file may be the copy somebody went through fixing speaker labels by
   hand — which is exactly what publishing a proposal is for. The script cannot tell its own earlier
   output from a corrected copy of it (the AI marker it writes survives a human's edits, so the
   marker proves nothing), so it refuses both cases the same way: if `--out` or `--report` already
   exists the run stops with exit 1, names the file, and writes nothing. The refusal offers a free
   name for **every** destination that is in the way (`--out "<stem>-2.md" --report
   "<stem>-2.speakers.json"`) so that following it does not walk into the same refusal one file
   later, and it prints the whole `--replace` command as a second option — copy one or the other
   rather than retyping four paths. Send the new run somewhere else and compare, or — only once you
   know that file can go — use the `--replace` line. `--replace` is the *only* thing that authorises
   an overwrite: `--force` overrides the mispair gate and nothing else, and a run forced past a
   mispair suspicion still refuses to write over an existing transcript.

   These gates are not tuning knobs, and the script enforces them as its defaults — you do not pass
   `--min-confidence` or `--min-match-rate` on the standard run:

   - **Hard abort below a 50% token match rate on *both* transcripts.** Correctly paired files
     measure 70–83%; a deliberately mispaired transcript measured 5.9% — and, ungated, still
     confidently labeled 80% of segments. This guard is what stands between the archive and fluent
     nonsense. The question it asks is *are these two files the same recording?*, so the rate is
     measured against both sides — the matched words over the **longer** stream, which is the same as
     requiring 50% of the app transcript and 50% of the whisper transcript. Measuring against the
     shorter one answers a weaker question ("is the small file inside the big one?"): a five-word app
     export sharing one phrase with one whisper segment scored a perfect 100%, passed, and had that
     segment published at full confidence with the wrong speaker on it. Underneath the rate sits a
     floor of **20 matching words** — a percentage taken over a handful of common words is noise
     whichever way you divide it, so below that the answer is "cannot tell", and a gate that cannot
     tell refuses. (`--min-match-rate`, default 0.50; below either bar the script refuses to label
     anything and exits 2.) The refusal prints both percentages with the word counts they came from,
     because that pair is what distinguishes a mispair (6% against 5%) from a genuinely partial app
     export (83% of the app against 41% of whisper — half the interview). **A partial export fails
     this gate too**, deliberately: from here it is indistinguishable from a mispair. If you know the
     app file covers only part of the sitting, `--force` labels the part that does line up and the
     override is recorded in the report. **A refusal writes nothing** — not the transcript, not the
     report — so an attributed transcript from an earlier run survives a mistyped `--app-transcript`
     byte for byte. The same holds for a run that can attribute nothing at all (a paragraph-only app
     export): if `--out` or `--report` already holds a file, it is left alone and the script exits 1
     rather than replace good work with an unlabeled copy — and `--replace` does not buy past that
     one either, because it says a file may go, not that an unlabeled copy is worth what it replaces.
   - **Label a whisper segment only at a confidence of 0.65 or better, and never when contested.**
     The score is `coverage × (2 × agreement − 1)` — the winner's votes minus everybody else's, over
     the segment's token count. Every word of a segment has **one** vote and gives it once, however
     many methods claim that word: the text alignment and the app's timestamps agreeing about a word
     make it *one covered word, not two*, and when they claim the same word for different speakers
     its vote splits and cancels. So 0.65 reads at both ends — a fully covered segment needs ≥ 82.5%
     agreement, and a unanimous segment needs ≥ 65% coverage. On the fixture set used to calibrate
     it (32 synthetic interview pairs, ~2,800 segments with known speakers, corrupted app text,
     mid-sentence app splits, blind spans and phantom speaker IDs) that labels ~72% of segments and
     leaves **~28% honestly unlabeled**. (`--min-confidence`, default 0.65. Lowering it is a decision
     you own and must say out loud, and nothing downstream may treat a lowered run as if it met this
     contract.)

     **Why 0.65, when this skill used to advertise 0.90.** The old score let the two methods each
     claim the same word: a ten-word segment matched on five words by the alignment (5.0) and
     overlapping one speaker's interval for 45% of its duration (4.5) scored 9.5/10 = 0.95 even when
     both numbers described the same five words. So 0.90 of a doubled number was really asking for
     about 0.45 of honest coverage, and every sentence promising "nearly fully covered" — this one
     included — was untrue of the code. The count was corrected so the published formula means what
     it says; 0.65 is the gate **measured** to keep the labelling the tool was already producing
     (72.7% of segments labelled before, 72.1% after, with the same or a slightly higher share of
     them correct). One consequence to know: an app export with no usable timestamps was never
     double-counted, so nothing about its scores changed and the lower gate genuinely does loosen
     that case — on the same fixtures, from ~57% of its segments labelled to ~68%.
   - **Never interpolate across a speaker change**, and never across a gap wider than ~25 tokens.
   - **Timestamp evidence needs real coverage, and coverage means *where*, not just *how many*.**
     The interval path only switches on when at least 80% of the app's turns actually carry a
     timestamp, and a turn with no timestamp sitting between two timed ones blanks that whole span
     rather than letting the earlier speaker's interval run over it. Counting timed turns cannot see
     where the timing *stops*, so two more rules cover the end of the file: the last turn's interval
     ends with that turn's own words rather than running on to the end of the audio, and if the timed
     turns stop short of the recording's end (an app export that gives up at 51 seconds of a
     100-second interview) the timestamp path is switched off for the whole file. A gappy or
     truncated export gets the text alignment alone, not a confident wrong answer, and the uncovered
     tail goes out unlabeled.
   - A tie is contested; contested is unlabeled. Never break it with "same as the previous speaker" —
     that manufactures false continuity.

   State the ceiling rather than hiding it: **this is label transfer, not diarization.** It can only
   inherit the app's own segmentation, including its mid-sentence splits and its phantom speakers
   (one real 3-person conversation exported with **ten** speaker IDs). The residual error sits within
   3–5 seconds of a turn boundary where neither method is authoritative, and no threshold fixes it.
   Never call the output "diarized". **One published number is currently unverified:** the "precision
   plateaus around 96% at ~75% coverage" figure was measured on a real interview under the older,
   over-counting confidence score and its 0.90 gate. It has not been re-measured against what ships
   now, and the calibration above is fixtures rather than field. Quote it as the shape of the answer,
   not as a measurement of this build, until somebody runs the current script over a real pair whose
   speakers are known.

   The merged file opens with an AI marker naming the model, the date, the gates, and the plain
   sentence that **an unlabeled turn means unknown, not unimportant** (SPEC §6.1: AI-written text is
   marked as AI wherever it is stored). If the gates fail, attach the plain whisper transcript
   instead and say why — two transcripts is a complete, correct result.

8. **Propose speaker → person; never assign it.** Print a table and stop:

   ```markdown
   ## Q: who is who in this recording?
   | Speaker | Share of words | Evidence | Proposal |
   |---|---|---|---|
   | Speaker 2 | 78% | carries the long first-person narrative; addressed as "Grandpa" 3× by Speaker 1 | Thomas Hartley? |
   | Speaker 1 | 19% | asks the questions | Ruth Hartley? |
   | Speaker 5 | 0.9% | 11 words, one interjection | trace — merge into Speaker 2, or drop? |
   ```

   Evidence ranked: **vocative address** (the person who says a name is usually not that person —
   strong but noisy, never fires alone), **word share and discourse role** (the interviewee carries
   the narrative; the interviewer asks), **question/answer polarity**, then **external context**
   (filename, who was present, the archive record). Two facts kill any shortcut: speaker numbering is
   **not stable across files** from the same app — the same person is Speaker 2 in one recording and
   Speaker 1 in the next, so a cross-file mapping table is a bug, not an optimization — and speaker
   count is not person count. Trace speakers under ~2% of words are proposed as *"merge or drop?"*,
   never silently folded.

   Nothing is written until he answers, per file. Confirmed names go into the merged artifact only;
   the two original transcripts stay byte-for-byte unchanged, and any later claim cites the
   attribution as a proposal (*"the whisper transcript proposes Thomas at 00:41:22"*) rather than as
   evidence.

### Optional — the pyannote upgrade path, and why it is not the default

True acoustic diarization (`pyannote.audio`) is the only route that gives speaker identity
*independent of the app*, and it fixes precisely what label transfer cannot: mid-sentence splits and
the 3–5 second boundary zone. The barrier is not compatibility — it is that every prerequisite is
**a human action you cannot take on his behalf**: a multi-gigabyte install (torch and ~70 packages —
recommend a dedicated venv for blast radius), a HuggingFace account and token, and **acceptance of
the gated model's conditions on its model card**, which cannot be automated. CPU-only runs are well
over realtime on a long interview. Mention it once, as an offer, when a recording's attribution
genuinely matters and the app labels failed the gates; a timestamped re-export gets most of the
value for zero install. Even with pyannote, its speakers are still `Speaker A / B / C` — step 8's
proposal-and-confirm is unchanged.

### Stage C — process, then hand off or continue

9. **File the session's primary recording first, then attach every companion in its own call.** `--more` takes exactly
   one `FILE ROLE` pair (`nargs=2`, no `append`), and it attaches to an **already-filed** source —
   so repeating the flag in one command silently keeps only the last pair, leaving the other files
   on disk with an `_S-id` in their name but no `files:` entry, which is lint **E011** in both
   directions. One call per file, primary first.

   Two names are in play and mixing them up is how the folder stops sorting: `<session>` is the
   session slug (`hartley-thomas-1998-06-14`), and `<session>-<topic>` is what each file is called on
   disk before processing (`hartley-thomas-1998-06-14-wedding.m4a`). The primary's `--slug` is
   `<session>`, so its audio loses the topic and takes the bare stem; every companion keeps its own
   stem and gains the role.

   ```
   fha process "<folder>/<session>-<topic>.m4a" --type interview --title "<title>" --date <edtf> --slug "<session>" --dry-run
   fha process "<folder>/<session>-<topic>.m4a" --type interview --title "<title>" --date <edtf> --slug "<session>"
   ```

   That renames the primary in place to `<session>_S-id.m4a` and scaffolds the record at
   `sources/interview/<session>_S-id.md`. Then, pointing at the **renamed** primary, one call per
   companion — `--more` renames each attachment to `{its own stem}-{role}_{S-id}`, which is why the
   topic survives on the companions and not on the primary's audio:

   ```
   fha process "<folder>/<session>_S-id.m4a" --more "<folder>/<session>-<topic>.txt" transcript
   fha process "<folder>/<session>_S-id.m4a" --more "<folder>/<session>-<topic>.md" whisper-transcript
   fha process "<folder>/<session>_S-id.m4a" --more "<folder>/<session>-<other-topic>.m4a" audio
   ```

   Worked through with the step 5 example: the primary is `hartley-thomas-1998-06-14-wedding.m4a`
   with `--slug hartley-thomas-1998-06-14`, so it lands as
   `hartley-thomas-1998-06-14_S-wb91h3hjrr.m4a`; `--more …-wedding.txt transcript` lands as
   `hartley-thomas-1998-06-14-wedding-transcript_S-wb91h3hjrr.txt`; and `--more …-farm.m4a audio`
   lands as `hartley-thomas-1998-06-14-farm-audio_S-wb91h3hjrr.m4a`.

   Show him the dry-run's rename/scaffold plan before applying — every mutating verb previews first
   (_STANDARD.md §8). Check the exit code; never proceed past a 2 or a 3 silently. In automatic mode,
   preview the whole batch at once, apply in bounded batches, and report counts at the end.

10. **Fill in the source record `fha process` scaffolded.** `source_type: interview` (there is no
    `audio` or `video` type — the media is the recording inside an interview), `source_class:
    original`, `source_date` from step 4 in EDTF - an interval like `1998-06-14/1998-06-15` is a
    legitimate value here when the recording's timezone could not be settled, and a single day you
    cannot justify is not - `aliases` carrying the pre-processing filename, a
    `citation` sentence in plain words saying what this recording actually is, and `people:` for
    whoever is actually in it. Every `files:` path is in **alias form** — `documents/interviews/…`,
    never `D:/FamilyDocuments/…` (SPEC §12.4).

    Every file on disk whose name parses an `_S-id` must appear in `files:` or lint throws **E011**
    in both directions — which is exactly what a hand-placed transcript does if it skips `--more`.

    The timezone caveat from step 4 goes in `## Notes`, together with the timezone you actually used
    and where it came from, and one sentence saying the folder is human projection and the S-id is
    the binding. Where the folder name's day and the `source_date` differ - a settled folder name
    over an interval date - say that too, in one line, so nobody later reads the filename as the
    date.

11. **Record the passes.** One entry per machine pass, before you hand back:

    ```yaml
    - {date: {today}, model: faster-whisper-medium, harness: {your-harness},
       task: "local whisper transcription of the recording",
       outputs: [], human_reviewed: false}
    - {date: {today}, model: {your-model-id}, harness: {your-harness},
       task: "speaker-label transfer from the app transcript onto the whisper text (gate >= 0.65)",
       outputs: [], human_reviewed: false}
    ```

    Use your real model and harness identifiers — those braces are placeholders, not values to
    copy — and record the whisper model the run **actually** used, which is not always the one you
    asked for. `outputs: []` is valid when a pass produced no claims.

12. **Fork on the mode.**

    - **Interactive** — stop here and hand off. Name the source as `[[S-…|the 1998-06-14 interview
      with Thomas]]`, say what landed, and offer the next step: *"`process-source` can read this and
      draft facts, or `mine-transcript` if you just want the interview mined."* Don't duplicate their
      work; the reindex, xref, view refresh, and lint belong to that close-out.
    - **Automatic** — carry straight on into `mine-transcript`, reading the whisper pass and the app
      transcript **side by side** and mining from the comparison (see that skill's step 1: whisper wins
      on names, the app wins on turn structure, and a divergence in coverage usually means the app
      truncated or mis-attached a file) — anchoring claims to the whisper timestamps and naming the
      transcript when the source has several, `anchor: "cars-whisper, 00:14:32"`. Then park everything in `review-claims`' queue
      and tell him how many claims are waiting per recording. Every claim is `status: suggested` with
      `information: primary` and honest `confidence`; `fha claim new` defaults to `--status accepted`,
      so **pass `--status suggested` explicitly, every time**.

13. **Close out.** Only when you are not handing off to a skill that owns the close-out:

    ```
    fha index
    fha lint
    ```

    Then report: imported, skipped as duplicate, left staged and why, speaker maps confirmed and
    unresolved. End with one concrete next step.

## Guardrails

- Any record ID written into prose is `[[ ]]`-wrapped, `[[ID|Name]]` preferred; bare IDs only in
  structured slots (claims-block YAML, frontmatter lists, tool arguments) — _STANDARD.md §11.
- Every drafted claim is `status: suggested`; **nothing** is `accepted` in this skill, and silence is
  never consent.
- **Never rename or move an archived asset by hand.** Documents-root renames happen only through
  `fha process`; a cross-root correction is `fha process refile --dry-run` first. Nothing under the
  photos root is ever renamed. Moves *within* a root are the human's to make; `fha reconcile` heals
  the paths.
- **Every path a script here writes gets a filename of its own.** `--json`, `--out`, `--report`,
  `--outdir`/`--name`: none may resolve onto an incoming recording, an archived original, another
  output of the same run, or `fha.yaml` - including through `./`, a differing case, or a symlink.
  The dedupe report and the two attribution outputs refuse the run on a collision; the whisper
  outputs do **not** check, so they go to the scratchpad and nowhere else.
- **A date is not derived until the evidence for it is in hand.** Read the container, settle the
  timezone, *then* convert (step 4). The same ordering binds anything else computed from evidence:
  get the information first, and where it cannot be got, write the uncertain form rather than an
  exact one - a wrong exact value is invisible forever, an honest interval invites correction.
- **Never alter a byte of any original.** No transcoding, no trimming, no re-encoding, no rewriting,
  condensing, or spell-fixing of transcript text — including the garbled names. Extraction is
  indexing, not preservation (SPEC §6.3).
- **Never replace, detach, or supersede the app transcript**, even when whisper is plainly better.
  Both stay as their own `files:` entries. Existing claims keep their anchors to the transcript they
  were mined from; a new derived file must not silently invalidate them.
- **Speaker labels are machine-inferred proposals**, hedged in the artifact itself, confirmed per
  file by the human, and never written into the two original transcripts. Never invent a name, a
  label, or a line of dialogue — an invented detail in a family story becomes family truth in one
  generation (AGENTS.md §"Speculation and storytelling").
- **Never delete or empty an incoming bundle**, and never treat staged copies as disposable without
  saying so. Nothing is deleted without explicit instruction.
- **Never re-process a file carrying an S-id**, and never mint a second S-id for a recording already
  in the archive. A duplicate is reported, not imported.
- **Never narrow the duplicate check to get a clean answer.** `new` means the gate examined
  everything it claims to have examined — every configured root, every folder inside it, every
  same-size candidate, and the batch against itself. Pointing `--media-root` at the part that
  happens to be readable, or carrying on past an `UNCHECKED` line, keeps the confidence while
  removing what earned it. A gate that answers a smaller question in the same words is worse than no
  gate at all.
- Nothing mines silently. Intake is not extraction — in interactive mode this skill drafts zero
  claims and hands off.
- Only `fha` and this skill's own `scripts/` are shelled from here. The two interim enactments
  (dedupe hash, container probe) are the owner-decided exception recorded in [`GAP.md`](GAP.md); a
  further missing capability is a halt-and-report, not an improvisation (_STANDARD.md §6).

## Done when

- A mixed bundle imports in a session on `example-archive`: recordings from one sitting grouped into
  a single `documents/interviews/{interviewee}-{yyyy-mm-dd}/` folder under one S-id, the first renamed in place by
  one `fha process` call and every sibling attached as `{topic}-audio`, each app transcript and
  whisper transcript attached by one `--more` call each under the session stem, the source record
  carrying `source_type: interview`, an EDTF `source_date` derived from the container **after** the
  recording's timezone was settled, with the UTC/local caveat and the timezone's provenance in
  `## Notes`, alias-form `files:` paths, and an `## AI Passes` entry per machine pass.
- A recording whose timezone cannot be established lands with an honest interval `source_date`
  (`1998-06-14/1998-06-15`) and a sentence saying why - never a single exact day the evidence does
  not support, and never a stall.
- A `--json` report path that resolves onto an incoming recording, an archived one, or `fha.yaml`
  is refused before anything is hashed, and every one of those files is byte-identical afterwards -
  including when the collision is only in the spelling (`Interview.m4a` for `interview.m4a`, an
  accent typed the other way, a hard link), which is a refusal on every platform and not only on the
  volumes that fold names themselves.
- A byte-identical repeat of an already-archived recording is detected by hash, **skipped**, and
  reported with the path of the file it duplicates — no second S-id, no second folder, and the
  bundle left intact on disk. The same holds for a repeat *within one batch* (one afternoon under
  two filenames): one is imported, the other reported and skipped.
- A recording the archive already holds, handed back as the incoming argument — a filed file picked
  from the documents root, or the whole `documents/interviews/` folder — is reported as already
  filed, never cleared for import.
- An export with no speaker labels, or a transcript/audio pair that fails the 50% match gate,
  degrades gracefully: no speaker labels are written, the reason is said in one plain sentence, and
  the recording still lands with its two transcripts.
- Speaker → person is presented as a table with evidence and word shares, and **no name is written
  anywhere** until the human answers for that file; unresolved speakers stay unresolved.
- Interactive mode ends at the hand-off with zero claims drafted; automatic mode reaches
  `review-claims`' queue with every claim `status: suggested` and none accepted.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9).
