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
       "<path-to-audio>" --model medium --outdir "<scratchpad>/whisper" --name <slug>
   ```
   Model choice: `small` for a quick pass; `medium`+ when the goal is
   recovering garbled proper names (it usually is, in a family archive).

   **`--name` decides where the file lands in a directory listing — get it
   right the first time.** `fha process --more` renames what you attach to
   `{stem}-{role}_{S-id}.{ext}`, so the stem you pass here *is* the sort key.
   Every file belonging to one source must share one prefix, with only the
   per-file role distinguishing it, so a reader sees each recording's audio,
   app transcript and whisper pass sitting together:

   ```
   hartley-thomas-interview-1998-06-14-farm-audio_S-x9qcves0nm.m4a
   hartley-thomas-interview-1998-06-14-farm-transcript_S-x9qcves0nm.txt
   hartley-thomas-interview-1998-06-14-farm-whisper-transcript_S-x9qcves0nm.md
   ```

   So `--name hartley-thomas-interview-1998-06-14-farm-whisper`, taking the
   prefix from the file you are transcribing — **not** a fresh slug of your own
   (`hartley-thomas-1998-06-14-farm-whisper` drops `interview-` and exiles
   every whisper file to its own clump, because `2` sorts before `i`). If the
   existing siblings disagree with each other, the source record's own filename
   gives the canonical slug. Where a rename is needed to repair this, it is a
   documents-root rename: change the file **and** the record's `files:` entry in
   one operation, then `fha reconcile --dry-run` to confirm nothing came
   untied. This mirrors the naming rule in `import-recordings` step 5; the two
   must stay in agreement.
3. **Review the output** (`<name>.md`, timestamped) against the passages that
   mattered — especially names the original transcript garbled.
4. **Keep BOTH transcripts on the source — always.** This skill's end state
   is one source record carrying the audio plus every transcript of it, each
   under its own role:
   - the original app/human transcript (role `transcript`) — if the recording
     arrived with one and it isn't archived yet, attach it too; if the audio
     itself isn't archived yet, run it through `process-source` first;
   - the whisper pass (role `whisper-transcript`):
   ```
   fha process "<archived-audio-file>" --more "<name>.md" whisper-transcript
   ```
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
     wording is being repaired. Notes are hand-edited in the source record.
     Every correction carries its whisper timestamp and says what the app
     transcript had instead, so the reasoning survives.
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
  --more`. Archived assets are never renamed, moved, or overwritten.
