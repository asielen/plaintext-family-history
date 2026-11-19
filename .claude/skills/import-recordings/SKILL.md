---
name: import-recordings
description: >
  Run when the human hands over recordings: "import these interviews", "here's a zip of voice memos
  off my phone", "get grandma's recording into the archive", "I recorded a conversation with Dad",
  "there's a video from the family reunion". Content-hashes every incoming audio or video file
  against what is already archived and skips the duplicates, reads the real recording date out of
  the container instead of trusting the filename, groups the recordings from one sitting into a
  single session source under `documents/interviews/{interviewee}-{yyyy-mm-dd}/` beside the transcripts they
  shipped with, always adds a fresh local
  whisper pass (never replacing the app transcript), and proposes which speaker is which person
  without ever assigning one. Interactive by default: it confirms each recording and hands off to process-source
  and mine-transcript. Automatic mode carries straight on into claim drafting, and even then every
  claim lands `suggested` and every speaker name waits for the human's yes. Never edits, renames, or
  deletes an original recording or transcript outside `fha process`.
---

# import-recordings

This skill brings recordings into the archive. It sits in front of `fha process` the way `import-notes` does for paper: pair a recording with the transcript that shipped beside it, keep both originals byte-for-byte intact, add a machine pass of its own, and file the set in one folder under one s-id. It files recordings and drafts no claims; the interview gets its claims later, through `mine-transcript` and `review-claims`. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Run when the human puts recordings in front of you: a `.zip` bundle exported from a phone app (audio or video plus the app's own transcript), a loose `.m4a` / `.mp3` / `.wav` / `.mp4` / `.mov`, a folder of them, or an inbox that `fha capture --ingest` already swept. Audio and video are the same job. Video is a recording like any other, and whisper reads its audio track.

Two modes, and the human picks:

- **Interactive** (the default, and what you use unless told otherwise). One recording at a time. Every destination, every slug, every speaker proposal is confirmed before it is written. Ends by handing off to `process-source` / `mine-transcript`; drafts nothing.
- **Automatic**, only on an explicit up-front say-so: *"import all of these, don't ask me per file"*, *"do the whole zip and mine them"*. That one sentence is the batch approval that per-item confirmation would otherwise collect, and it is also the explicit ask `mine-transcript` requires, since nothing mines silently. Automatic mode still previews the whole plan first, still stops on a duplicate or a low-confidence speaker map, and still leaves every claim `suggested`.

This is **migration mode** (AGENTS.md §"Operating modes"): bulk intake of existing material, the highest-risk mode. PLAN → DRY-RUN → human approval → bounded batches (≤200 files) → report. Say so in your first reply and stay in it.

## The contract for this skill (state it before you start)

- Every claim this skill's continuation drafts is `status: suggested`. **Nothing** reaches `accepted` here. The human is the only gate, and only through `fha claim … --status accepted`.
- **No original content is altered.** Audio and video are never transcoded, trimmed, or re-encoded; transcript text is never rewritten, condensed, or "cleaned up". The one sanctioned touch is the documents-root processing rename (SPEC §5.1, §12.1) via `fha process`.
- **Both transcripts survive, always.** The app transcript and the whisper pass sit side by side as separate `files:` entries. Never replace, detach, or supersede one with the other, even when whisper is obviously better. They disagree in exactly the spots a reviewer needs to compare.
- **Speaker names are proposals.** A machine may transfer labels; only the human may say who is speaking. Unknown stays unknown.
- Every pass over a source (whisper, label transfer, mining) is recorded in that source's `ai_passes:` frontmatter list before you hand back (_STANDARD.md §3.3, SPEC §14). Nothing lints for it.
- Living people: a recording of someone alive is ordinary archive material, but the person record's `living:` flag must be right, and anything the human flags as sensitive gets `restricted: true` on the source (SPEC §19).

The content-hash duplicate check and the container-metadata date read are `fha` verbs: `fha media dedupe` and `fha media probe` (TOOLING.md §6a). The local whisper run and the speaker-label transfer are this skill's own `scripts/`: whisper depends on a local model, the label transfer works only on the paired app and whisper transcripts this skill produces, and both produce working drafts rather than archive records. All four are read-only on the archive: they report, and `fha process` is the only thing that writes. If a further capability is missing, **stop and name it**; do not hand-roll it in prose (`_STANDARD.md` §6).

## Flow

Invoke `fha` through the archive-root launcher (`./fha` / `.\fha`, AGENTS.md §Tools). When the documents root sits **outside** the archive, auto-detection from the current folder can fail from a scratch directory, so pass `--root <archive>` on every call rather than guessing.

### Stage A: plan and dedupe (before any write)

1. **State the mode and lay out the plan.** Count what arrived: how many bundles, how many media files, how many carry a transcript, total duration if you know it. Extract every `.zip` to the scratchpad. **The incoming bundle is never consumed, emptied, or deleted** (AGENTS.md §"Don'ts"; even `fha capture --ingest` only parks swept bundles in `.ingested/`). Show the human the list before you touch the documents root.

2. **Pair each recording with the transcript that shipped beside it.** Inside a bundle that is usually one media file plus one `.txt`/`.md`. Pair on stem, then on timestamp, then ask. A transcript with no media is not an intake; say so and leave it staged. A bundle with two recordings from the same sitting is one source with two recordings (step 5); from different days, two sources.

3. **Content-hash every incoming media file against what is already archived, and skip the duplicates.** In one real 16-zip phone export, 6 recordings were byte-identical to audio already filed. Compare byte size first and hash only on a size collision; hashing or reading a whole root is wasteful and discouraged (_STANDARD.md §8). `fha media dedupe` does exactly that, and it is the only thing that answers the question:

   ```
   fha media dedupe "<incoming file or folder>" --root "<archive>"
   ```

   It reads sizes from the directory entries of the archive's configured media roots, opens nothing unless a size collides, then SHA-256s both sides. It is read-only on both sides and renames, moves and imports nothing. Exit **0** means no incoming file has a byte twin; exit **2** means at least one does, and it prints which archived file (and its s-id, when the filename carries one). Confirm the twin's record with `fha find <s-id>` before you say anything to the human.

   **What a `new` verdict promises.** This gate authorises imports, so `new` means more than "I found no twin". It means *"I examined everything I claim to have examined, and none of it was this recording."* The verb fails closed unless all five of these hold:

   - **Roots**: every media root `fha.yaml` names was resolved and is readable. A configured root that is missing right now (external drive unplugged, folder renamed) refuses the run. It is not an empty root; its recordings are unread.
   - **Enumeration**: every folder under those roots was listed, including hidden ones like `documents/.private/` and folders reached through a directory symlink. A folder the human keeps for the material he is most careful about is the last place this check may skip.
   - **Domain**: the same audio/video rule applies to both sides, and every path you name on the command line is either checked or printed as `SKIPPED` with the reason. "Archived" means **filed**: the inbox is staging even when it sits inside the photo library, so a recording waiting there is never reported as already filed.
   - **Candidates**: every archived recording of exactly the same byte size was opened and hashed.
   - **Batch**: the incoming files were compared against each other too. One afternoon exported twice under two names would otherwise give two `new` verdicts that are each true and together wrong; the verb clears the first and marks the rest `DUPLICATE … in the same batch`. Import the one it names.

   Two more things follow. A file you hand over that already lives in a media root is the archive's own copy: it comes back `DUPLICATE … already filed in the archive`, never `new`, so selecting a filed recording (or handing over `documents/interviews/`) cannot authorise a second import of it. And never narrow the check to make it answer, for example by pointing it at a subfolder instead of the whole bundle or skipping a root that will not mount. That examines less and reports the same confidence.

   Exit **1** is usage or configuration: a path that is not there, PyYAML not installed, or a media root `fha.yaml` names that is not there on this machine. `fha media dedupe` needs PyYAML to read which folders hold the recordings (those folders may sit on another drive) and refuses rather than guessing, because a guess searches the wrong folder and calls an already-filed recording new. A named but absent root is refused for the same reason: the message tells the human to reconnect the drive, create the folder, or fix the path, and each of those is a one-step fix.

   `--json <path>` saves the same findings as a file, and that path is the one thing in this step that writes. Give it a name of its own in the scratchpad, never a recording's name and never a path inside a media root. The verb refuses the run before it hashes anything if that path lands on an incoming recording, on an archived one, on `fha.yaml`, or anywhere inside a media root, because a report written over a recording would destroy it after clearing it as safe to import. It asks the filesystem rather than comparing the spelling, so `./`, a symlink, a hard link and a second mount of the same disk are all one file to it. Where the report path does not exist yet (the ordinary case), it compares the folder plus the name with capitalisation and accents folded away. That fold is deliberately over-eager: `--json Interview.m4a` is refused beside an incoming `interview.m4a` on every machine, not only on the macOS and Windows volumes where the two names really are one file, because picking another filename costs one word and a wrong overwrite costs the recording. (A different name is still a different file: `dedupe-report.json` beside `dedupe-report.m4a` writes normally.)

   Exit **3** means **the check could not finish**. Nothing it printed as `UNCHECKED` is cleared; fix what it names and re-run the whole bundle rather than importing the part that passed. It fires when something could not be read: the drive holding the archive's `documents` root is not plugged in, a folder cannot be listed, `fha.yaml` will not parse, or an archived recording of exactly the same size could not be opened. An unchecked file is not a new file, because the twin, if there is one, may be the recording nobody could read. Tell the human in his own terms: *"I can't tell yet whether these are already filed. The folder holding your interviews isn't readable right now."* Name the thing to fix, and re-run the same command afterwards. Never fall back to filenames or to `fha search`, and never narrow the check onto the readable part to get a clean exit.

   `fha search "<distinctive phrase from the transcript's first minute>"` answers a different and weaker question: it finds a transcript that reads alike, which is a lead and not proof of an identical recording. Use it only to explain a near-miss (the same sitting re-exported, trimmed, or re-encoded, so the bytes differ), never in place of `fha media dedupe`.

   On a match, **report and stop for that item**: *"This is the same file as the recording already filed at `documents/interviews/…`. I'm skipping it; nothing was imported."* Do not re-process it: `fha process` refuses a file already carrying an s-id, and forcing a second pass would mint a duplicate s-id for one recording. Where the match is another file in the same batch, say so: *"These two files are the same recording under two names. I'll import one and leave the other."* Import exactly the file the verb names, because one sitting is one source record.

   If the duplicate's bundle carries a **transcript the archive lacks** (say, the phone's *timestamped* export where the archive only has the bracket form), attach it to the **existing** source with `fha process --more` (step 9's second form, pointed at the already-filed primary). Attaching mints no s-id and creates no second source. Confirm with the human first, since it adds a file to a record he already reviewed. Compare the two transcripts before offering: if the archive's copy is the richer one, there is nothing to add, and you say so.

4. **Read the real recording date out of the container, not the filename, and settle the recording's timezone before you convert anything.** App exports are named for when the *file* was written, or for nothing at all. The container's `creation_time` is the reliable field, and it has a trap that belongs in the record:

   > QuickTime/MP4 writes `creation_time` in **UTC, at the moment the recording stopped**. The clock in an app-written filename is **local time at the moment it started**. The two disagree by the recording's own length plus the UTC offset, which is enough to cross midnight on a long evening interview.

   ```
   fha media probe "<file>" --root "<archive>"
   ```

   The second half of the trap produces a wrong date without any warning: **a UTC instant is not a calendar date until you know where the clock was.** Converting `creation_time` with *your* machine's timezone gives the wrong answer for a recording made elsewhere; a Sunday afternoon recorded two zones east converts to the wrong day and looks ordinary. So `fha media probe` establishes the recording's offset first, before any arithmetic and before it names a date, and always says which of three places the offset came from:

   1. **The container's own local timestamp.** Phones and most modern cameras write `com.apple.quicktime.creationdate` alongside `creation_time`. It is local time *with* its offset (`1998-06-14T20:15:00-0500`), written by the device that was in the room. The verb reports `offset_source: quicktime_creationdate` and the question is settled.
   2. **The filename clock, solved for the offset.** When the app filename carries a clock time, the verb runs the cross-check itself: local stop is `filename_time + duration` and UTC stop is `creation_time`, so `offset = filename_time + duration − creation_time`, rounded to the nearest quarter hour. A fit that misses by more than a couple of minutes means the filename clock is not what it was taken for, so the verb reports `offset_source: filename_clock` on a fit and leaves it unsolved otherwise. Then go to 3.
   3. **Ask him, before you say any date.** When the verb reports no offset at all (`offset_source: none`: no local timestamp, no solvable filename clock), it says so plainly rather than guessing, and your judgment takes over: *"Where was this recorded? Was it the same time zone you're in now?"* One short question is the only way to the right date when the container carries nothing, and it has to come before you write any date down.

   When the offset is settled, the verb has already done the arithmetic: `derived local start` is `creation_time` − duration rendered in **that** zone, and its day is the date (`source_date: 1998-06-14`). It also flags `crosses_midnight` when the local start's date differs from the UTC date, which is the sitting that runs past midnight. The day it *started* is still the `source_date`; note the overrun rather than splitting one interview across two dates. Exit **0** means the offset was settled and the date is confident. Exit **1** means duration and `creation_time` were read but no offset could be established, so go to option 3 above rather than trusting a bare `source_date`. Exit **2** means the container carries no usable creation timestamp at all, and the verb says so rather than falling back to the file's modified-on-disk time (a fact about the file, not the recording). Exit **3** is a tool failure: the file could not be read, or neither `ffprobe` nor PyAV is available (`fha doctor` reports both the way it reports exiftool).

   **If the timezone is still unknown, do not write an exact date.** An unconfirmed value must never be presented as exact (AGENTS.md §"The contract"), and the archive's date format already has the honest form: write the interval spanning the days the recording could have started on (`source_date: 1998-06-14/1998-06-15`), say in one plain sentence why it is two days instead of one, and move on. Don't stall on it, and don't invent a precise day. Someone can narrow an interval later; a wrong exact day is never noticed.

   Either way, copy the caveat above verbatim into the source's `## Notes`, **together with the timezone you used and where it came from**: *"container offset -0500"*, *"derived from the filename clock"*, *"confirmed with the human"*, or *"not established, so the date is an interval"*. That line lets the next reader redo the arithmetic instead of guessing again.

5. **Group by sitting, not by file, then pre-file into one folder per session.** Phone apps split a single afternoon into one file per topic. **Those are one source.** One real archive's sessions carry a dozen topic recordings under one s-id, and a session is the unit a reader looks for. One session, one folder, one s-id: `documents/interviews/{interviewee}-{yyyy-mm-dd}/`. Recordings on *different* days are different sources even when the topic continues.

   Group on the **local** dates from step 4 (the ones converted with a settled timezone), not on filenames, which carry relative weekday labels that put different days under the same word. Ask if a sitting straddles midnight or a day holds two unrelated visits. A recording whose date is still an interval is not grouped by guess: ask which sitting it belongs to, or leave it staged and say so.

   The folder name and the slug take **one** day even when `source_date` is an interval. The folder is a convenience for the human with no machine meaning (see below), so it takes the earlier candidate day, you say out loud that you did, and the record keeps the interval. Never let a tidy folder name talk you into a tidy `source_date`: the folder name is a label, and `source_date` is a claim about when the recording was made.

   Copy the files in **before** processing: a file pre-filed into a subfolder is renamed **in place** by `fha process`, while a file sitting at the top level of the documents root is moved into `documents/{type}/` (TOOLING §6; SPEC §12.1, folders are the human's own arrangement).

   Naming follows one rule so the folder sorts itself: **name every file on disk for the session, and let the `--more` role carry the topic.** `fha process` derives its slug from the *title* unless you pass `--slug`, and `--more` renames to `{stem}-{role}_{s-id}`, so a session stem going in gives a folder clustered by topic coming out. The session's first recording is the `primary` and takes the bare session slug (which also names the source record); every later recording is attached as `{topic}-audio`:

   ```
   documents/interviews/hartley-thomas-1998-06-14/          # the assets; the record it shares an s-id with
                                                            # is sources/interview/hartley-thomas-1998-06-14_s-wb91h3hjrr.md
     hartley-thomas-1998-06-14-farm-audio_s-wb91h3hjrr.m4a
     hartley-thomas-1998-06-14-farm-transcript_s-wb91h3hjrr.txt
     hartley-thomas-1998-06-14-farm-whisper-transcript_s-wb91h3hjrr.md
     hartley-thomas-1998-06-14-school-audio_s-wb91h3hjrr.m4a
     …
     hartley-thomas-1998-06-14-wedding-transcript_s-wb91h3hjrr.txt      # wedding is the primary:
     hartley-thomas-1998-06-14-wedding-whisper-transcript_s-wb91h3hjrr.md
     hartley-thomas-1998-06-14_s-wb91h3hjrr.m4a                         # …so its audio is the bare stem
   ```

   Because each topic's three files share the `{session}-{topic}` prefix, they sit together in a directory listing even though the folder holds a whole afternoon.

   Tell the human what the folder is: **a convenience for him, with no machine meaning.** The shared s-id ties the files to the record; the folder could be reshuffled tomorrow and `fha reconcile` would fix the paths. In interactive mode, confirm the slug with him, since he is the one who will read it in five years. Never add a new top-level archive folder; subfolders under an existing asset root are fine.

### Stage B: transcribe and attribute (judgment)

6. **Run whisper on every recording, including the ones that already shipped with a transcript.** App transcripts garble proper names, which is what genealogy needs most. Whisper's text and timings are far better; the app's turn structure is better than whisper's, which has none. Keep both, because each is right where the other is wrong.

   ```
   python ".claude/skills/transcribe-audio/scripts/transcribe_audio.py" "<audio-or-video>" --model medium --outdir "<scratch>" --name "<stem>"
   ```

   `--outdir` is always the **scratchpad**: never the incoming bundle, never a media root. The three whisper outputs are `{name}.txt`, `{name}.srt` and `{name}.md`. The script will not write over files it cannot prove it wrote. A complete set of its own three is a no-op it reports, and anything else already sitting under those names stops the run (only its own `--force` overrides that). So a `--name` matching the app transcript's stem, in the folder where the app transcript lives, stops the run instead of destroying an original. Do not rely on that: pick the session-and-topic stem, write into scratch, and let `fha process --more` be the only thing that puts a file into the archive.

   `medium` is the minimum when the goal is recovering garbled proper names, which it usually is. Budget roughly half of realtime per file on CPU. Run recordings **one at a time**: faster-whisper already spreads across cores, so parallel jobs compete for the same cores, finish no sooner, and make the machine unusable meanwhile. Put long queues in a background script that logs per file and re-runs the command for every recording, so a reboot costs one file and not the batch. The transcribe script already answers "is this one done?" itself: a complete set of its three outputs is a no-op it reports, and a half-written or interrupted set is redone. Do not have the wrapper skip a recording because *some* output file exists, or a half-published transcript becomes permanent.

   Video needs no special handling; whisper reads the audio track in place. If a container is unreadable, the fix is a **new derived audio extraction filed beside the video** with `role: audio-extract`, never an edit to the video file.

7. **Transfer speaker turns onto the whisper text, under gates or not at all.** The app knows who spoke; whisper knows what was said and when. Merging them produces a **third, machine-made file**, which is acceptable only because both originals stay untouched beside it.

   Before any alignment, **ask whether the app can re-export with timestamps.** The same app that writes `[Speaker 1]` blocks also writes a numbered form carrying `00:01`-style turn times, and the two are byte-identical in text and labels. With timestamps the transfer becomes an interval lookup, with no fuzzy matching and no failure mode, so ask that first.

   Falling back to text alignment:

   ```
   python ".claude/skills/import-recordings/scripts/attribute_speakers.py" --whisper "<whisper.md>" --app-transcript "<app.txt>" --out "<stem>.md" --report "<stem>.speakers.json"
   ```

   All four paths must be different: the two inputs, `--out`, and `--report`. The script refuses the run rather than let a mistyped filename overwrite a transcript (including `--report` landing on `--out`), and both outputs are written through a temporary file, so an interrupted run never leaves a half-written transcript behind. **Both destinations are checked before the alignment starts.** A folder standing on the output name, or a path whose parent is a file, is refused at once, so the ordinary typo costs nothing and leaves nothing behind.

   **If the report cannot be saved, the transcript is kept.** The two outputs are deliberately not all-or-nothing: `--out` is the file the run exists to produce and the expensive one to remake, while `--report` is an optional JSON record of *how* the labels were decided. If the transcript is written and then the report write fails (a disk that fills, a drive unplugged mid-run), the script keeps the transcript, says so in plain words, names the file you now have, and prints the **complete** command that saves the report too. That command carries `--replace`, because the run has just created `--out` and the next run would otherwise be stopped by the rule below. Replacing it is safe while it is still the file this run wrote, and stops being safe once you correct speaker labels in it by hand. Exit code 1 here means "not everything you asked for happened", so read the message and check the transcript is where it says.

   **A second run does not overwrite the first one.** `--out` names the same `<stem>.md` every time, and by the second run that file may be the copy somebody corrected by hand, which is what publishing a proposal is for. The script cannot tell its own earlier output from a corrected copy (the AI marker it writes survives a human's edits, so the marker proves nothing), so it refuses both cases the same way: if `--out` or `--report` already exists, the run stops with exit 1, names the file, and writes nothing. The refusal offers a free name for **every** destination in the way (`--out "<stem>-2.md" --report "<stem>-2.speakers.json"`), so following it does not hit the same refusal one file later, and it prints the whole `--replace` command as a second option. Copy one or the other rather than retyping four paths. Send the new run somewhere else and compare, or use the `--replace` line only once you know that file can go. `--replace` is the *only* thing that authorises an overwrite: `--force` overrides the mispair gate and nothing else, and a run forced past a mispair suspicion still refuses to write over an existing transcript.

   These gates are the script's defaults and are not for tuning; you do not pass `--min-confidence` or `--min-match-rate` on the standard run:

   - **Hard abort below a 50% token match rate on *both* transcripts.** Correctly paired files measure 70–83%; a deliberately mispaired transcript measured 5.9%, and without the gate it still confidently labeled 80% of segments. This gate is what keeps fluent nonsense out of the archive. It asks *are these two files the same recording?*, so the rate is the matched words over the **longer** stream, which is the same as requiring 50% of the app transcript and 50% of the whisper transcript. Measuring against the shorter one answers a weaker question ("is the small file inside the big one?"): a five-word app export sharing one phrase with one whisper segment scored 100%, passed, and published that segment at full confidence with the wrong speaker. Below the rate sits a floor of **20 matching words**, because a percentage over a handful of common words is noise either way; below that the answer is "cannot tell", and the gate refuses. (`--min-match-rate`, default 0.50; below either bar the script refuses to label anything and exits 2.) The refusal prints both percentages with the word counts behind them, because that pair separates a mispair (6% against 5%) from a partial app export (83% of the app against 41% of whisper, half the interview). **A partial export fails this gate too**, on purpose, since at this point it cannot be told from a mispair. If you know the app file covers only part of the sitting, `--force` labels the part that lines up, and the report records the override. **A refusal writes nothing**, neither transcript nor report, so an attributed transcript from an earlier run survives a mistyped `--app-transcript` byte for byte. The same holds for a run that can attribute nothing at all (a paragraph-only app export): if `--out` or `--report` already holds a file, it is left alone and the script exits 1 rather than replace good work with an unlabeled copy. `--replace` does not get past that refusal either: it says a file may go, not that an unlabeled copy is worth what it replaces.
   - **Label a whisper segment only at a confidence of 0.65 or better, and never when contested.** The score is `coverage × (2 × agreement − 1)`: the winner's votes minus everybody else's, over the segment's token count. Every word of a segment has **one** vote and gives it once, however many methods claim that word. The text alignment and the app's timestamps agreeing about a word make it one covered word, and when they claim the same word for different speakers its vote splits and cancels. So at 0.65 a fully covered segment needs ≥ 82.5% agreement, and a unanimous segment needs ≥ 65% coverage. On the calibration fixtures (synthetic interview pairs with known speakers, corrupted app text, mid-sentence app splits, blind spans and phantom speaker IDs) that labels about 72% of segments and leaves the rest unlabeled. On an app export with no usable timestamps it labels a little less, about two thirds. (`--min-confidence`, default 0.65. Lowering it is a decision you own and must say out loud, and nothing downstream may treat a lowered run as if it met this contract.)
   - **Never interpolate across a speaker change**, and never across a gap wider than ~25 tokens.
   - **Timestamp evidence needs real coverage, which means where the timestamps fall as well as how many there are.** The interval path switches on only when at least 80% of the app's turns carry a timestamp, and a turn with no timestamp between two timed ones blanks that whole span rather than letting the earlier speaker's interval run over it. Counting timed turns cannot see where the timing *stops*, so two more rules cover the end of the file: the last turn's interval ends with that turn's own words rather than running to the end of the audio, and if the timed turns stop short of the recording's end (an app export that gives up at 51 seconds of a 100-second interview), the timestamp path is switched off for the whole file. A gappy or truncated export gets the text alignment alone, and the uncovered tail goes out unlabeled.
   - A tie is contested, and contested is unlabeled. Never break it with "same as the previous speaker", which invents continuity the evidence does not show.

   Say what the method cannot do: **this is label transfer, not diarization.** It can only inherit the app's own segmentation, including its mid-sentence splits and its phantom speakers (one real 3-person conversation exported with **ten** speaker IDs). The remaining error sits within 3–5 seconds of a turn boundary where neither method is authoritative, and no threshold fixes it. Never call the output "diarized". **One published number is unverified:** the "precision plateaus around 96% at ~75% coverage" figure was measured on a real interview under an older confidence score and its 0.90 gate. It has not been re-measured against the current script, and the calibration above comes from fixtures, not field recordings. Quote it as the rough shape of the answer, not as a measurement of this build, until someone runs the current script over a real pair whose speakers are known.

   The merged file opens with an AI marker naming the model, the date, the gates, and the plain sentence that **an unlabeled turn means unknown, not unimportant** (SPEC §6.1: AI-written text is marked as AI wherever it is stored). If the gates fail, attach the plain whisper transcript instead and say why. Two transcripts is a complete, correct result.

8. **Propose speaker → person; never assign it.** Print a table and stop:

   ```markdown
   ## Q: who is who in this recording?
   | Speaker | Share of words | Evidence | Proposal |
   |---|---|---|---|
   | Speaker 2 | 78% | carries the long first-person narrative; addressed as "Grandpa" 3× by Speaker 1 | Thomas Hartley? |
   | Speaker 1 | 19% | asks the questions | Ruth Hartley? |
   | Speaker 5 | 0.9% | 11 words, one interjection | trace: merge into Speaker 2, or drop? |
   ```

   Evidence, strongest first: **vocative address** (the person who says a name is usually not that person; strong but noisy, and never enough alone), **word share and discourse role** (the interviewee carries the narrative; the interviewer asks), **question/answer polarity**, then **external context** (filename, who was present, the archive record). Two facts rule out shortcuts. Speaker numbering is **not stable across files** from the same app: the same person is Speaker 2 in one recording and Speaker 1 in the next, so a mapping table reused across files is a bug. And speaker count is not person count. Trace speakers under ~2% of words are proposed as *"merge or drop?"*, never silently folded.

   Nothing is written until he answers, per file. Confirmed names go into the merged file only; the two original transcripts stay byte-for-byte unchanged, and any later claim cites the attribution as a proposal (*"the whisper transcript proposes Thomas at 00:41:22"*) rather than as evidence.

### Optional: the pyannote upgrade path, and why it is not the default

True acoustic diarization (`pyannote.audio`) is the only route to speaker identity *independent of the app*, and it fixes what label transfer cannot: mid-sentence splits and the 3–5 second boundary zone. The barrier is that every prerequisite is **a human action you cannot take on his behalf**: a multi-gigabyte install (torch and ~70 packages; recommend a dedicated venv to contain it), a HuggingFace account and token, and **acceptance of the gated model's conditions on its model card**, which cannot be automated. CPU-only runs take well over realtime on a long interview. Mention it once, as an offer, when a recording's attribution matters and the app labels failed the gates; a timestamped re-export gets most of the value with no install. Even with pyannote, its speakers are still `Speaker A / B / C`, and step 8's proposal and confirmation are unchanged.

### Stage C: process, then hand off or continue

9. **File the session's primary recording first, then attach every companion in its own call.** `--more` takes exactly one `FILE ROLE` pair (`nargs=2`, no `append`), and it attaches to an **already-filed** source. Repeating the flag in one command silently keeps only the last pair, which leaves the other files on disk with an `_s-id` in their name but no `files:` entry: lint **E011** in both directions. One call per file, primary first.

   Two names are in play, and mixing them up stops the folder sorting: `<session>` is the session slug (`hartley-thomas-1998-06-14`), and `<session>-<topic>` is what each file is called on disk before processing (`hartley-thomas-1998-06-14-wedding.m4a`). The primary's `--slug` is `<session>`, so its audio loses the topic and takes the bare stem; every companion keeps its own stem and gains the role.

   ```
   fha process "<folder>/<session>-<topic>.m4a" --type interview --title "<title>" --date <edtf> --slug "<session>" --dry-run
   fha process "<folder>/<session>-<topic>.m4a" --type interview --title "<title>" --date <edtf> --slug "<session>"
   ```

   That renames the primary in place to `<session>_s-id.m4a` and scaffolds the record at `sources/interview/<session>_s-id.md`. Then, pointing at the **renamed** primary, make one call per companion. `--more` names an attachment for its source, `{source slug}-{role}_{s-id}`, except that a file whose name already starts with the source's slug and a hyphen (`<session>-<topic>`) keeps that stem and gains the role (`{its own stem}-{role}_{s-id}`). That is why the topic survives on the companions and not on the primary's audio. A companion named any other way loses its topic to the source slug, so name every file `<session>-<topic>` before attaching it:

   ```
   fha process "<folder>/<session>_s-id.m4a" --more "<folder>/<session>-<topic>.txt" transcript
   fha process "<folder>/<session>_s-id.m4a" --more "<folder>/<session>-<topic>.md" whisper-transcript
   fha process "<folder>/<session>_s-id.m4a" --more "<folder>/<session>-<other-topic>.m4a" audio
   ```

   Worked through with the step 5 example: the primary is `hartley-thomas-1998-06-14-wedding.m4a` with `--slug hartley-thomas-1998-06-14`, so it lands as `hartley-thomas-1998-06-14_s-wb91h3hjrr.m4a`; `--more …-wedding.txt transcript` lands as `hartley-thomas-1998-06-14-wedding-transcript_s-wb91h3hjrr.txt`; and `--more …-farm.m4a audio` lands as `hartley-thomas-1998-06-14-farm-audio_s-wb91h3hjrr.m4a`.

   Show him the dry-run's rename and scaffold plan before applying; every mutating verb previews first (_STANDARD.md §8). Check the exit code, and never proceed past a 2 or a 3 silently. In automatic mode, preview the whole batch at once, apply in bounded batches, and report counts at the end.

10. **Fill in the source record `fha process` scaffolded.** `source_type: interview` (there is no `audio` or `video` type; the media is the recording inside an interview), `source_class: original`, `source_date` from step 4 in EDTF, `aliases` carrying the pre-processing filename, a `citation` sentence in plain words saying what this recording is, and `people:` for whoever is in it. An interval like `1998-06-14/1998-06-15` is a legitimate `source_date` when the recording's timezone could not be settled; a single day you cannot justify is not. Every `files:` path is in **alias form**, `documents/interviews/…`, never `D:/FamilyDocuments/…` (SPEC §12.4).

    Every file on disk whose name parses an `_s-id` must appear in `files:`, or lint reports **E011** in both directions. A transcript placed by hand without `--more` does exactly that.

    The timezone caveat from step 4 goes in `## Notes`, with the timezone you actually used and where it came from, plus one sentence saying the folder is the human's own arrangement and the s-id is what ties the files to the record. Where the folder name's day and the `source_date` differ (a settled folder name over an interval date), say that too, in one line, so nobody later reads the filename as the date.

11. **Record the passes.** One entry per machine pass, before you hand back:

    ```yaml
    ai_passes:
      - {date: {today}, by: faster-whisper-medium, task: transcript,
         note: "local whisper transcription of the recording"}
      - {date: {today}, by: {your-model-id}, task: speaker labels,
         note: "speaker-label transfer from the app transcript onto the whisper text (gate >= 0.65)"}
    ```

    Use your real model identifier (the braces are placeholders, not values to copy), and record the whisper model the run **actually** used, which is not always the one you asked for. A pass that produced no claims is still recorded.

12. **Fork on the mode.**

    - **Interactive**: stop here and hand off. Name the source as `[[s-…|the 1998-06-14 interview with Thomas]]`, say what landed, and offer the next step in plain words: *"Want me to go through this interview now and draft the facts it mentions, for you to check?"* (that is `mine-transcript`, or `process-source` for a full intake pass). Don't duplicate their work; the reindex, xref, view refresh, and lint belong to that close-out.
    - **Automatic**: carry straight on into `mine-transcript`. Read the whisper pass and the app transcript **side by side** and mine from the comparison (see that skill's step 1: whisper wins on names, the app wins on turn structure, and a difference in coverage usually means the app truncated or mis-attached a file). Anchor claims to the whisper timestamps, naming the transcript when the source has several: `anchor: "cars-whisper, 00:14:32"`. Then leave everything in `review-claims`' queue and tell him how many claims are waiting per recording. Every claim is `status: suggested` with `information: primary` and an honest `confidence`. `fha claim new` defaults to `--status accepted`, so **pass `--status suggested` explicitly, every time**.

13. **Close out.** Only when you are not handing off to a skill that owns the close-out:

    ```
    fha index
    fha lint
    ```

    Then report: imported, skipped as duplicate, left staged and why, speaker maps confirmed and unresolved. End with one concrete next step.

## Guardrails

- Any record ID written into prose is `[[ ]]`-wrapped, `[[id|Name]]` preferred; bare IDs only in structured slots (claims-block YAML, frontmatter lists, tool arguments). See _STANDARD.md §11.
- Every drafted claim is `status: suggested`; **nothing** is `accepted` in this skill, and silence is never consent.
- **Never rename or move an archived asset by hand.** Documents-root renames happen only through `fha process`; a cross-root correction is `fha process refile --dry-run` first. Nothing under the photos root is ever renamed. Moves *within* a root are the human's to make; `fha reconcile` fixes the paths.
- **Every path a script here writes gets a filename of its own.** `--json`, `--out`, `--report`, `--outdir`/`--name`: none may resolve onto an incoming recording, an archived original, another output of the same run, or `fha.yaml`, including through `./`, a differing case, or a symlink. The dedupe report and the two attribution outputs refuse the run on a collision; the whisper outputs do **not** check, so they go to the scratchpad and nowhere else.
- **A date is not derived until the evidence for it is in hand.** Read the container, settle the timezone, *then* convert (step 4). The same order applies to anything else computed from evidence: get the information first, and where it cannot be got, write the uncertain form rather than an exact one.
- **Never alter a byte of any original.** No transcoding, no trimming, no re-encoding, no rewriting, condensing, or spell-fixing of transcript text, including the garbled names. Claims point into the transcript; they never replace it (SPEC §6.3).
- **Never replace, detach, or supersede the app transcript**, even when whisper is plainly better. Both stay as their own `files:` entries. Existing claims keep their anchors to the transcript they were mined from; a new derived file must not silently invalidate them.
- **Speaker labels are machine-inferred proposals**, hedged in the merged file itself, confirmed per file by the human, and never written into the two original transcripts. Never invent a name, a label, or a line of dialogue: an invented detail in a family story becomes family truth within a generation (AGENTS.md §"Speculation and storytelling").
- **Never delete or empty an incoming bundle**, and never treat staged copies as disposable without saying so. Nothing is deleted without explicit instruction.
- **Never re-process a file carrying an s-id**, and never mint a second s-id for a recording already in the archive. A duplicate is reported, not imported.
- **Never narrow the duplicate check to get a clean answer.** `new` means the gate examined everything it claims to have examined: every configured root, every folder inside it, every same-size candidate, and the batch against itself. Pointing the check at the part that happens to be readable, or carrying on past an `UNCHECKED` line, keeps the confidence while removing what it rests on.
- Nothing mines silently. In interactive mode this skill drafts zero claims and hands off.
- Only `fha` and this skill's own `scripts/` (whisper, speaker-label transfer) are run from here. The dedupe hash and container probe are `fha media dedupe`/`fha media probe`; a further missing capability is a halt-and-report, not an improvisation (_STANDARD.md §6).

## Done when

- A mixed bundle imports in a session on `example-archive`: recordings from one sitting grouped into a single `documents/interviews/{interviewee}-{yyyy-mm-dd}/` folder under one s-id, the first renamed in place by one `fha process` call and every sibling attached as `{topic}-audio`, each app transcript and whisper transcript attached by one `--more` call each under the session stem, the source record carrying `source_type: interview`, an EDTF `source_date` derived from the container **after** the recording's timezone was settled, with the UTC/local caveat and the timezone's provenance in `## Notes`, alias-form `files:` paths, and an `ai_passes:` entry per machine pass.
- A recording whose timezone cannot be established lands with an interval `source_date` (`1998-06-14/1998-06-15`) and a sentence saying why, never a single exact day the evidence does not support, and never a stall.
- A `--json` report path that resolves onto an incoming recording, an archived one, or `fha.yaml` is refused before anything is hashed, and every one of those files is byte-identical afterwards. That includes a collision only in the spelling (`Interview.m4a` for `interview.m4a`, an accent typed the other way, a hard link), which is refused on every platform and not only on the volumes that fold names themselves.
- A byte-identical repeat of an already-archived recording is detected by hash, **skipped**, and reported with the path of the file it duplicates: no second s-id, no second folder, and the bundle left intact on disk. The same holds for a repeat *within one batch* (one afternoon under two filenames): one is imported, the other reported and skipped.
- A recording the archive already holds, handed back as the incoming argument (a filed file picked from the documents root, or the whole `documents/interviews/` folder), is reported as already filed, never cleared for import.
- An export with no speaker labels, or a transcript/audio pair that fails the 50% match gate, degrades gracefully: no speaker labels are written, the reason is said in one plain sentence, and the recording still lands with its two transcripts.
- Speaker → person is presented as a table with evidence and word shares, and **no name is written anywhere** until the human answers for that file; unresolved speakers stay unresolved.
- Interactive mode ends at the hand-off with zero claims drafted; automatic mode reaches `review-claims`' queue with every claim `status: suggested` and none accepted.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9).
