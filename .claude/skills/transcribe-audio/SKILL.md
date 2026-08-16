---
name: transcribe-audio
description: Re-transcribe an archived (or new) audio recording locally with faster-whisper, producing a timestamped transcript to attach alongside the original. Run when the human says "run this through whisper", "get a better transcription", "transcribe this recording", or when an app-generated transcript is too garbled to mine reliably.
---

# transcribe-audio

Local re-transcription for family recordings. The engine is `faster-whisper`
(`pip install faster-whisper`, once, on the machine that holds the audio);
ffmpeg is optional (PyAV fallback built in). CPU-only is fine — budget
roughly realtime for `medium`, faster for `small`. Nothing here is an `fha`
verb: transcription is model-dependent, non-portable, and produces a working
draft rather than an archive record, so it lives in this skill's own
`scripts/` (the carve-out `import-recordings/GAP.md` records).

## Flow

1. **Locate the audio.** Usually an archived asset under the documents root
   (`documents/interviews/..._S-xxxxxxxxxx.m4a`). Never move or rename it.
2. **Run the script** (long-running — background it):
   ```
   python ".claude/skills/transcribe-audio/scripts/transcribe_audio.py" \
       "<path-to-audio>" --model medium --outdir "<scratchpad>/whisper" --name <stem>
   ```
   Model choice: `small` for a quick pass; `medium`+ when the goal is
   recovering garbled proper names (it usually is, in a family archive).
   Other flags: `--language en` (or `auto` to let whisper detect it) and
   `--force`, which re-does a recording whose outputs are already there.
   Exit codes: `0` written (or already present), `1` no speech found and
   nothing written, `2` it could not run — the message names the next step.

   **`--name` decides where the file lands in a directory listing — get it
   right the first time, and pass the *shared* stem with no role of your
   own.** `fha process --more` renames what you attach to
   `{stem}-{role}_{S-id}.{ext}`, appending the role itself, so a `--name`
   ending in `-whisper` files as `…-whisper-whisper-transcript_S-….md`. Every
   file belonging to one source must share one prefix, with only the per-file
   role distinguishing it, so a reader sees each recording's audio, app
   transcript and whisper pass sitting together:

   ```
   hartley-thomas-interview-1998-06-14-farm-audio_S-x9qcves0nm.m4a        # role: audio
   hartley-thomas-interview-1998-06-14-farm-transcript_S-x9qcves0nm.txt   # role: transcript
   hartley-thomas-interview-1998-06-14-farm-whisper-transcript_S-x9qcves0nm.md
   ```

   All three grow from the one stem `hartley-thomas-interview-1998-06-14-farm`,
   so that is exactly what `--name` gets — take it from the file you are
   transcribing, dropping its `-{role}` and its `_S-id` (the script's default
   `--name` does that for you), and **not** a fresh slug of your own
   (`hartley-thomas-1998-06-14-farm` drops `interview-` and exiles every
   whisper file to its own clump, because `2` sorts before `i`). If the
   existing siblings disagree with each other, the source record's own filename
   gives the canonical slug: match it, and **leave the odd file exactly as it
   is.** A documents-root file is renamed once, by `fha process`, and no verb
   renames it again — `fha reconcile` re-ties files that *moved* by matching
   the unchanged filename, so a hand-rename breaks the tie instead of healing
   it, and hand-editing a `files:` entry is off the table for the same reason.
   Say plainly which file looks out of line and leave the reorganizing to the
   human; when he has moved things, `fha reconcile --dry-run` (then
   `fha reconcile`) re-ties the paths. This mirrors the naming rule in
   `import-recordings` step 5; the two must stay in agreement.

   Point `--outdir` at a scratch folder, **never at an asset root**: the run
   writes `<name>.txt`, `<name>.srt` and `<name>.md`, and `<name>.txt` is
   precisely the name an app transcript filed beside the recording would carry.
   The script skips a recording whose three output files are all there (it
   reports "already transcribed" and exits 0 — which is what makes a long queue
   safe to re-run after a reboot), and a run that is interrupted or fails
   publishes nothing at all. An unfinished set is never mistaken for a finished
   one: if only some of the three files are there, or if a run was killed
   outright while it was putting them in place (it leaves a `.publishing` marker
   when that happens), the next run says so plainly and re-does the recording
   instead of skipping it. Tell the human what it says; the leftover `.part`,
   `.kept` and `.publishing` files in that folder can be deleted once the new
   transcript looks right.
3. **Review the output** (`<name>.md`, timestamped) against the passages that
   mattered — especially names the original transcript garbled.
4. **Keep BOTH transcripts on the source — always.** This skill's end state
   is one source record carrying the audio plus every transcript of it, each
   under its own role:
   - the original app/human transcript (role `transcript`) — if the recording
     arrived with one and it isn't archived yet, attach it too; if the audio
     itself isn't archived yet, run it through `process-source` first;
   - the whisper pass (role `whisper-transcript`). `--more` attaches only files
     that already sit under the documents root, so copy the reviewed `.md` out
     of the scratch folder into the recording's own folder first — it is a new
     working file, not an archived original — and then attach it, previewing
     as always:
   ```
   fha process "<archived-audio-file>" --more "documents/interviews/<session-folder>/<name>.md" whisper-transcript --dry-run
   fha process "<archived-audio-file>" --more "documents/interviews/<session-folder>/<name>.md" whisper-transcript
   ```
   With `--name hartley-thomas-interview-1998-06-14-farm`, that files the
   attachment as
   `hartley-thomas-interview-1998-06-14-farm-whisper-transcript_S-x9qcves0nm.md`
   and appends its `files:` entry (role `whisper-transcript`) to the record.
   Check the exit code; never proceed past a 2 or a 3 silently.

   Never replace, detach, or "supersede" the original transcript, even when
   the whisper pass is clearly better — the two disagree in exactly the spots
   a reviewer needs to compare, and provenance beats tidiness.
   Note the pass (model, date) in the source's `## Notes` / `## AI Passes`.
5. **Re-anchor carefully.** Existing claims keep their anchors to the original
   transcript — a whisper pass never silently re-anchors, re-words or re-notes
   an `accepted` claim. When a whisper timestamp resolves a disputed reading,
   that is a *finding*, not an edit: carry it into step 6's audit, where it is
   put to the human as a proposal before anything is written.

6. **Offer the audit — a whisper pass over an already-mined source is not
   finished when the file is attached.** Claims drafted from the app transcript
   inherited its garbles, and those errors are now *accepted facts* in the
   archive. Ask whether to audit them, and if the human says yes, work claim by
   claim rather than re-mining (a fresh mining pass would duplicate what is
   already there).

   **Agreeing to run the audit is not agreeing to what it finds.** The audit is
   a reading pass that produces *proposals*; every write below waits for its
   own specific yes on that specific claim. This is not politeness. An
   `accepted` claim carries the human's signature and the `reviewed:` date on
   which he gave it, so an unapproved rewrite does not merely risk being wrong
   — it puts his name and his date on wording he has never seen, which is worse
   than a wrong claim, because nothing downstream can tell it apart from a
   reviewed one. Never take one blanket approval for a run of corrections, and
   never run this step unattended.

   - For each accepted claim anchored to this recording, find its anchor point
     in the whisper text and read a minute either side. Compare against the
     claim's `value` **and** its `notes` — the notes usually carry the quoted
     evidence, and a garble quoted there is what a future reader will cite.
   - The bar for proposing a correction is *factually wrong*, not *could be
     fuller*: a wrong name, place, number, relationship, or a misheard word
     that changed the meaning. Missing detail is new material or nothing at all.
   - Watch for the two failure modes that recur, and report them as readings
     rather than acting on them. **Speaker misattribution:** whisper has no
     diarization, so a first-person recollection can be credited to whoever was
     named nearby — say who you think is actually talking and what in the
     content says so (whose mother, whose grandfather, who asks the questions);
     the human decides whether the claim is about someone else. **Quoted
     evidence silently normalised:** a drafter who misread a word tends to
     "tidy" the quote to match, so the notes then appear to support the error.
   - **Put each correction to him as an exact before/after, one claim at a
     time,** with the whisper timestamp, what the app transcript had instead,
     and the `reviewed:` date now standing on the claim:

     ```
     C-x1y2z3a4b5  (birth · [[P-…|Margaret Cole]] · accepted, reviewed 2026-03-04)
       now:  value: born in Sue Walkie          <- app transcript 00:41:19
       new:  value: born in Suwałki             <- whisper 00:41:22
       why:  the app transcript spelled the town phonetically; whisper has the
             place name, and the next sentence names the province.
     ```

     Apply it only on his yes for that claim, and always with `--status` in the
     **same call**:

     ```
     fha claim <C-id> --status accepted --value "…" --dry-run
     fha claim <C-id> --status accepted --value "…"
     ```

     `--status accepted` there is not a status change — the claim is already
     accepted. It is the only thing that makes `fha claim` stamp today's
     `reviewed:` date (the tool refuses `--reviewed` without `--status`), and
     that date is the whole point: it records the day a human read *this*
     wording. **`fha claim <C-id> --value "…"` on its own is the wrong command
     here** — it changes the value and leaves the old `reviewed:` date sitting
     under it. The same rule binds `--date`, `--place`, `--place-text`,
     `--persons` and `--confidence`: on an `accepted` claim, no field moves
     without the re-stamp, and none of them moves without his yes.
   - **On a no, or a "let me think about it", write nothing.** Leave the claim
     exactly as it is. If he would rather not leave a doubtful fact looking
     settled, `fha claim <C-id> --status needs-review` drops it back into
     `review-claims`'s queue without inventing a value — offer it, don't assume
     it. Either way the declined or parked proposal goes in the pass notes: a
     reading he considered and rejected is worth as much as one he took.
   - **When the corrected reading changes WHICH fact is asserted — a different
     person, a different event, a different place — it is not a repair.** Draft
     it as a **new `status: suggested` claim** and hand it to `review-claims`;
     propose what should become of the old claim (`needs-review`, `disputed`,
     `superseded`) and let him say. `fha claim new --source <S-id> --status
     suggested …` mints it; it has no `--anchor` flag ([`GAP.md`](GAP.md)), so
     the `anchor:` naming the whisper transcript
     (`anchor: "…-whisper, 00:41:22"`) is added in the source's `## Claims`
     block the way `mine-transcript` step 2 drafts one — safe here because the
     claim is `suggested` and nothing already accepted is being touched. The
     same holds for anything whisper recovers that the app transcript mangled
     beyond use: new material is a new claim, never folded into an accepted one.
   - **A garbled quote in a claim's `notes:` needs the same yes — and no verb
     writes that field.** `fha claim` corrects `value`, `date`, `type`,
     `place`, `place_text`, `persons` and `confidence`; `notes:` and `anchor:`
     are not among them ([`GAP.md`](GAP.md)). So don't quietly hand-edit the
     record: show the before/after as above, and record the corrected reading
     with a verb that exists —
     `fha source note <S-id> --text "[[C-…]]: whisper 00:41:22 reads … where the
     app transcript had …"` — which appends to the source's `## Notes` without
     touching the claim. If he asks for the claim's own `notes:` line to be
     repaired in place, that is his call on his own record: make that one-line
     edit in the source record's `## Claims` block (a record, never an archived
     original or a `files:` entry), say exactly which line you changed, then
     re-stamp with `fha claim <C-id> --status accepted` so the review date moves
     with the content, and finish with `fha lint`.
   - Record the audit in `## AI Passes`: `outputs` lists what was actually
     written (C-ids corrected on his say-so, C-ids parked, new `suggested`
     C-ids), and proposals he declined go in the task/notes text so the reading
     survives without pretending to be a fact. `human_reviewed: false` until he
     says otherwise — the pass being *run* with him in the room is not the same
     as him having reviewed its output, and this is the field a later reader
     uses to tell them apart.

   A claim that turns out to be right is a valuable result; say so rather than
   manufacturing a finding. And whisper is better, not perfect — where both
   transcripts garble a name, record it as unresolved instead of picking the
   nicer-sounding guess.

## Guardrails

- Whisper output is a WORKING DRAFT of what was said — it still mishears
  names. It resolves disputes only when the human agrees it does.
- Speaker attribution is not provided (no diarization); never invent labels.
- **A whisper reading never overrides a human decision by itself.** Step 6
  produces proposals; an `accepted` claim's `value`, `date`, `place`,
  `place_text`, `persons`, `confidence`, `notes:` or `anchor:` changes only on
  the human's explicit yes to that exact before/after — a yes to running the
  audit is not one. Nothing here moves a claim *into* `accepted`; that is his
  alone (AGENTS.md, the contract). The `--status accepted` re-stamp below is
  not that move: it applies only to a claim he already accepted, and only on
  his yes to the correction it dates.
- **`reviewed:` travels with the content it signs.** Whenever an accepted
  claim's content is corrected, `--status accepted` goes in the same
  `fha claim` call so the date becomes the day he read the new wording. A bare
  `fha claim <C-id> --value …` on an accepted claim is forbidden here: it would
  leave his old review date standing over words he has never seen.
- New facts are new claims (`status: suggested`, routed to `review-claims`),
  never folded into an accepted one.
- Writes only to the given `--outdir`; on attach, through `fha process --more`;
  in the audit, through `fha claim` / `fha claim new` / `fha source note` on
  the human's per-claim yes, plus the one hand-edit he explicitly asks for (a
  claim's `notes:` line, which no verb owns — see [`GAP.md`](GAP.md)).
  Archived assets are never renamed, moved, or overwritten — not by
  the script, not by hand, not to fix a name that sorts badly. Neither is a
  record's `files:` entry: `fha process --more` writes it, `fha reconcile`
  heals it after the human moves things, and nothing else touches it.
- **Nothing machine-specific goes into the transcript.** The `.md` names the
  recording by *filename* only, never by the path you typed: an absolute path
  would publish the operator's home directory into an archived file and would
  point nowhere once the archive is copied elsewhere. The same rule binds what
  you paste from the console into a note — the run prints the local `--outdir`
  so you can find the files, and record paths are written in alias form
  (`documents/interviews/…`, SPEC §12.4).
- A run either writes all three files or none of them, so an interrupted pass
  never leaves a truncated transcript that a later batch would mistake for a
  finished one. That holds for putting the files in place as well: a
  destination that cannot be replaced stops the run before anything moves, and
  a failure part way through puts the previous transcript back. The one thing
  no program can undo - a hard kill mid-rename - is marked on disk instead, and
  the next run repairs it. Re-running is always safe; `--force` is the only way
  to replace a *finished* transcript that already exists.
