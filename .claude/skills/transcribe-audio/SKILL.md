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
   transcript; if a whisper timestamp resolves a disputed reading, cite it in
   that claim's `notes:` (e.g. "whisper transcript 00:41:22 reads ...").

6. **Offer the audit — a whisper pass over an already-mined source is not
   finished when the file is attached.** Claims drafted from the app transcript
   inherited its garbles, and those errors are now *accepted facts* in the
   archive. Ask whether to audit them, and if the human says yes, work claim by
   claim rather than re-mining (a fresh mining pass would duplicate what is
   already there):

   - For each accepted claim anchored to this recording, find its anchor point
     in the whisper text and read a minute either side. Compare against the
     claim's `value` **and** its `notes` — the notes usually carry the quoted
     evidence, and a garble quoted there is what a future reader will cite.
   - The bar for a correction is *factually wrong*, not *could be fuller*: a
     wrong name, place, number, relationship, or a misheard word that changed
     the meaning. Missing detail is new material or nothing at all.
   - Watch for the two failure modes that recur. **Speaker misattribution:**
     whisper has no diarization, so a first-person recollection can be credited
     to whoever was named nearby — check who is actually talking from content
     (whose mother, whose grandfather, who asks the questions). **Quoted
     evidence silently normalised:** a drafter who misread a word tends to
     "tidy" the quote to match, so the notes then appear to support the error.
   - Apply a value fix with `fha claim <C-id> --value "…" --dry-run` then
     apply; it changes the field without touching `status:` or `reviewed:`,
     which is right — the human's original acceptance stands and only the
     wording is being repaired. A claim's `notes:` has no flag of its own, so
     that one is edited in the source record's `## Claims` block by hand (a
     record, never an archived original or a `files:` entry) — say so when you
     do it, and run `fha lint` afterwards. Every correction carries its whisper
     timestamp and says what the app transcript had instead, so the reasoning
     survives.
   - Where whisper recovers material the app transcript mangled beyond use,
     draft it as a **new `status: suggested` claim** and hand it to
     `review-claims` — never fold new facts into an accepted claim.
   - Record the audit in `## AI Passes` with the corrected C-ids as `outputs`.

   A claim that turns out to be right is a valuable result; say so rather than
   manufacturing a finding. And whisper is better, not perfect — where both
   transcripts garble a name, record it as unresolved instead of picking the
   nicer-sounding guess.

## Guardrails

- Whisper output is a WORKING DRAFT of what was said — it still mishears
  names. It resolves disputes only when the human agrees it does.
- Speaker attribution is not provided (no diarization); never invent labels.
- Writes only to the given `--outdir` and, on attach, through `fha process
  --more`. Archived assets are never renamed, moved, or overwritten — not by
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
