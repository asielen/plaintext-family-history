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
   does, and it prints which archived file (and its S-id, when the filename carries one). Confirm the
   twin's record with `fha find <S-id>` before you say anything to the human.

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
   duplicate S-id for one recording.

   If the duplicate's bundle carries a **transcript the archive lacks** — the phone's *timestamped*
   export where the archive only has the bracket form, say — that is an ordinary attach onto the
   **existing** source, and `fha process --more` is exactly the verb for it (step 9's second form,
   pointed at the already-filed primary). Attaching is not importing: it mints no S-id and creates
   no second source. Confirm with the human first, since it adds a file to a record he already
   reviewed. Compare the two transcripts before offering — the archive's copy may be the richer one,
   in which case there is nothing to add and you say so.

4. **Read the real recording date out of the container, not the filename.** Filenames lie; app
   exports are named for when the *file* was written or for nothing at all. The container's
   `creation_time` is the honest field — and it carries a trap that goes in the record, not in your
   head:

   > QuickTime/MP4 writes `creation_time` in **UTC, at the moment the recording stopped**. The
   > clock in an app-written filename is **local time at the moment it started**. The two disagree
   > by the recording's own length plus the UTC offset — enough to cross midnight on a long evening
   > interview.

   ```
   ffprobe -v quiet -print_format json -show_format "<file>"
   ```

   So: start ≈ `creation_time` − duration, converted to local. That arithmetic is also a **free
   cross-check**: when an app filename carries a clock time, `filename_time + duration` should land
   on `creation_time`. If it does, the date is confirmed from two independent directions; say so.
   If the UTC date and the local start date land on the same day, write it: `source_date:
   1998-06-14`. If they straddle midnight, ask **one** short question — *"was this the evening of
   the 14th or after midnight on the 15th, your time?"* — and if he isn't sure, write the interval
   `1998-06-14/1998-06-15` and move on. A skill that stalls on a fuzzy date has failed the human.
   Either way the caveat above is copied verbatim into the source's `## Notes`, because the next
   reader will hit it too.

5. **Group by sitting, not by file, then pre-file into one folder per session.** Phone apps split a
   single afternoon into one file per topic. **Those are one source, not many** — one real archive's
   sessions carry a dozen topic recordings under one S-id, and a session is the unit a reader
   actually looks for. One session, one folder, one S-id:
   `documents/interviews/{interviewee}-{yyyy-mm-dd}/`. Recordings on *different* days are different
   sources even when the topic continues.

   Group on the container dates from step 4 — not on filenames, which carry relative weekday labels
   that collapse different days onto the same word. Ask if a sitting genuinely straddles midnight or
   a day holds two unrelated visits.

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

   `medium` is the floor when the goal is recovering garbled proper names — and it usually is.
   Budget roughly half of realtime per file on CPU. Run recordings **one at a time**: faster-whisper
   already spreads across cores, so parallel jobs contend for the same cores, finish no sooner, and
   make the machine unusable meanwhile. Long queues belong in a background script that logs per file
   and skips any output that already exists, so a reboot costs one file and not the batch.

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
   leaves a half-written transcript behind.

   These gates are not tuning knobs, and the script enforces them as its defaults — you do not pass
   `--min-confidence` or `--min-match-rate` on the standard run:

   - **Hard abort below a 50% global token match rate.** Correctly paired files measure 70–83%; a
     deliberately mispaired transcript measured 5.9% — and, ungated, still confidently labeled 80% of
     segments. This guard is what stands between the archive and fluent nonsense. (`--min-match-rate`,
     default 0.50; below it the script refuses to label anything and exits 2.) **A refusal writes
     nothing** — not the transcript, not the report — so an attributed transcript from an earlier run
     survives a mistyped `--app-transcript` byte for byte. The same holds for a run that can attribute
     nothing at all (a paragraph-only app export): if `--out` or `--report` already holds a file, it
     is left alone and the script exits 1 rather than replace good work with an unlabeled copy.
   - **Label a whisper segment only at a confidence of 0.90 or better, and never when contested.**
     The score is `coverage × (2 × agreement − 1)` — the winner's votes minus everybody else's, over
     the segment's token count — so 0.90 demands a segment be nearly fully covered *and* nearly
     unanimous: fully covered needs ≥ 95% agreement, unanimous needs ≥ 90% coverage. At that
     operating point ~75% of segments get a label at ~95% agreement and **25% are honestly left
     unlabeled**. (`--min-confidence`, default 0.90. Lowering it is a decision you own and must say
     out loud; below 0.9 the measured agreement falls toward a coin flip, and nothing downstream may
     treat a lowered run as if it met this contract.)
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
   (one real 3-person conversation exported with **ten** speaker IDs). The residual ~4–5% error sits
   within 3–5 seconds of a turn boundary where neither method is authoritative, and no threshold
   fixes it. Never call the output "diarized".

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
    original`, `source_date` from step 4 in EDTF, `aliases` carrying the pre-processing filename, a
    `citation` sentence in plain words saying what this recording actually is, and `people:` for
    whoever is actually in it. Every `files:` path is in **alias form** — `documents/interviews/…`,
    never `D:/FamilyDocuments/…` (SPEC §12.4).

    Every file on disk whose name parses an `_S-id` must appear in `files:` or lint throws **E011**
    in both directions — which is exactly what a hand-placed transcript does if it skips `--more`.

    The timezone caveat from step 4 goes in `## Notes`, along with one sentence saying the folder is
    human projection and the S-id is the binding.

11. **Record the passes.** One entry per machine pass, before you hand back:

    ```yaml
    - {date: {today}, model: faster-whisper-medium, harness: {your-harness},
       task: "local whisper transcription of the recording",
       outputs: [], human_reviewed: false}
    - {date: {today}, model: {your-model-id}, harness: {your-harness},
       task: "speaker-label transfer from the app transcript onto the whisper text (gate >= 0.90)",
       outputs: [], human_reviewed: false}
    ```

    Use your real model and harness identifiers — those braces are placeholders, not values to
    copy. `outputs: []` is valid when a pass produced no claims.

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
  carrying `source_type: interview`, an EDTF `source_date` derived from the container
  with the UTC/local caveat in `## Notes`, alias-form `files:` paths, and an `## AI Passes` entry per
  machine pass.
- A byte-identical repeat of an already-archived recording is detected by hash, **skipped**, and
  reported with the path of the file it duplicates — no second S-id, no second folder, and the
  bundle left intact on disk.
- An export with no speaker labels, or a transcript/audio pair that fails the 50% match gate,
  degrades gracefully: no speaker labels are written, the reason is said in one plain sentence, and
  the recording still lands with its two transcripts.
- Speaker → person is presented as a table with evidence and word shares, and **no name is written
  anywhere** until the human answers for that file; unresolved speakers stay unresolved.
- Interactive mode ends at the hand-off with zero claims drafted; automatic mode reaches
  `review-claims`' queue with every claim `status: suggested` and none accepted.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9).
