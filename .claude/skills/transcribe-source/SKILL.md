---
name: transcribe-source
description: >
  Run when a source's evidence is pictures and the archive holds none of its words — "transcribe this
  scan", "type out what this letter says", "lint says these sources have no searchable text", "back-fill
  transcripts for the image-only sources". Reads the images and writes out what the document says as a
  `role: transcript` companion attached with `fha process --more`, marked as an unreviewed AI reading
  until a human checks it against the image. Drafts and edits no claims: a reading that contradicts an
  accepted claim becomes an open question, and unclaimed facts hand off to `mine-transcript`.
---

# transcribe-source

A source whose files are all scans, photographs or image-only PDFs puts **nothing** into the archive as
text. Its claim values are the only words about it anyone can search, so a text search over such an
archive answers for what one earlier pass chose to write down while looking exactly like a search of the
evidence (#46). This skill closes that gap the only way it can be closed — somebody reads the pictures
and types out what they say — and it is model work, so it lives here and not in `tools/`.

The split it honours is `fha xref`'s: **the tools detect the gap** (`fha lint`'s W124, and the coverage
note `fha find --text` prints under every result), **this skill produces the text**, and **an existing
deterministic tool writes it** (`fha process --more <file> transcript`). No new verb is invented here.
See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Two entry points, one procedure:

1. **Inside `process-source`, between Stage A and Stage B.** When `fha process` has just filed an item
   whose files are all images, the transcript is written **before** any claim is drafted, so the claims
   come from text somebody read out line by line rather than from a single pass over pictures. That
   ordering is the whole win, and `process-source` is the only place it can be enforced — see its Stage
   A½.
2. **Standalone, for backfill.** Already-ingested sources never pass through `process-source` again.
   "Transcribe this scan", "type out grandma's letter", "lint says twelve sources have no searchable
   text — fix them" all land here. The batch discipline is in **Backfill** below.

Explicitly invoked either way. Nothing transcribes silently.

## The contract for this skill (state it before you start)

- **The image remains the evidence of record. The transcript is an index into it, never a substitute.**
  Say this to the human in those words when you hand back. Nothing downstream — a claim, a biography, a
  packet — cites the transcript as though it were the document; it cites the source, and the transcript
  is how a reader *finds the spot*.
- **A wrong transcript is worse than no transcript.** #46's failure was a null result read as a negative
  finding. A misread transcript produces confident *hits*: the same error with the evidence apparently on
  your side, and nobody re-examines a question they believe is answered. Everything below — the marker,
  the uncertainty conventions, the refusal to resolve ambiguity — follows from that one sentence.
- **This skill drafts and edits no claims.** Not `suggested`, not corrected, not re-anchored. It produces
  text and hands off. (`mine-transcript` drafts from the new text; `review-claims` gates; a contradiction
  with an existing claim is a question, not an edit.)
- **Uncertainty survives into the transcript.** An unreadable word is `[illegible]`; a doubtful reading
  is marked doubtful; a scribal error is transcribed as written. A transcript that quietly resolves
  ambiguity is how a slip of a nineteenth-century pen becomes a family fact.
- **Every transcript is marked as an unreviewed AI reading** until a human has compared it to the image —
  see **The marker** below. Record the pass in the source's `## AI Passes` block
  (`{date, model, harness, task, outputs, human_reviewed}`), `human_reviewed: false`.
- **Never modify an original** (AGENTS.md §"The contract" 3). The transcript is a new companion file; the
  scan, the photograph and the PDF are untouched, and nothing under the photos root is ever renamed.
- **No machine-specific absolute paths** in anything written into the archive (AGENTS_TOOLING §11): the
  transcript header names the file it was read from **by filename**, never by the path you typed.

## Flow

### 1. Find out what text the source already has — do not re-read what the archive can already read

```
fha find <S-id>            # the record, its files: inventory, and every attached companion
```

A source is **image-only** when none of its `files:` entries holds text a search can read. Use the tools'
own rule, so this skill and `fha lint`'s W124 and `fha find --text`'s coverage note always count the same
sources (`_lib.file_entry_carries_text`): an entry carries text when its `role:` is `transcript`,
`transcription` or `extracted-text`, **or** its file is a `.md` or `.txt` (whatever its role). Everything
else is opaque — a `.jpg`, a `.tif`, a `.pdf`, an `.m4a` — however much text a human eye can see in it.
A source with no `files:` at all is not image-only; there is nothing to read.

Two consequences worth stating plainly to the human:

- **A scanned PDF with no text layer is image-only.** "PDF" says nothing about whether the words are in
  the file; a page photographed and wrapped in a PDF is a picture.
- **A source that already has a transcript is done**, even a partial one. Say what is covered and stop;
  extending a partial transcript is a fresh decision, not a default.

### 2. Try the mechanical path first — `fha source extract`

If any of the source's files is a PDF, try the text layer before asking a model to re-read what the file
already contains:

```
fha source extract <S-id> --dry-run     # what it would produce, and how many pages carry text
fha source extract <S-id>               # writes the [Page N]-labeled companion, attaches it
```

It dumps the PDF's embedded text into a `role: extracted-text` companion labeled `[Page N]` in the PDF's
own pagination, and refuses honestly on an all-image PDF. If it succeeds, **the source is no longer
image-only and this skill is finished** — hand straight to `mine-transcript`. An extract dump is a
mechanical copy of the file's own words, not a model reading, so it carries **no** AI marker.

If it refuses, or covers only some pages, the rest is yours to read.

### 3. Read the images, in the original's pagination

Look at the files — this is the irreducible part. If your harness cannot view images, **say so plainly
and stop**: an image-only source with no viewer is a halt, not a place to infer from filenames, keywords
or existing claim values. Inferring the text from the claims is precisely the circularity this skill
exists to break.

- Work in **page windows** for anything long (~20 pages at a time), and log coverage as you go with
  `fha source note <S-id> --text "…"` so an interrupted session resumes instead of restarting.
- **Preserve page boundaries.** Every page gets its own `[Page N]` label using the *original's* numbering
  — the same convention `fha source extract` writes — so a claim can carry `anchor: "page 3"` (SPEC §8.4)
  and land in the right place in any copy. A single-sheet document is `[Page 1]`. For a photographed
  object with no pages, label the side or the region instead (`[Front]`, `[Back]`, `[Chart, left branch]`)
  and keep those labels stable, because they are what anchors will cite.
- Transcribe **what the page says**, not what it means. Keep the original spelling, capitalisation,
  abbreviations and line breaks where they carry information (a column, a list, a signature block).
  Layout that matters — a table, a hand-drawn chart, a marginal note — is described in a `[…]` editorial
  note and then transcribed, so a reader knows the shape before reading the words.

### 4. Mark every uncertainty — never a silent guess

This vocabulary is the point of the whole file. Use it, and use nothing else:

| Written | Means |
|---|---|
| `[illegible]` | A word or passage you could not read at all. `[illegible: 3 words]` when the length is worth recording. |
| `[Hartlee?]` | Your best reading, flagged as doubtful. The `?` is inside the brackets and always present. |
| `[unclear: Hartley or Hartlee]` | Two candidate readings you cannot choose between. Give both; never pick for the reader. |
| `[torn]` / `[cut off]` / `[faded]` | The page is physically missing or unrecoverable here — a fact about the document, not about your eyes. |
| `[struck: Margaret]` | Text crossed out on the page. Struck text is evidence; it is never dropped. |
| `[sic]` | The page really does say that. Transcribe the error, mark it, and move on. |
| `[note: …]` | An editorial observation about layout, ink, hand, or a stamp — clearly yours, never mistakable for the document's words. |

Two rules that follow, and that the hand-transcription test behind this skill found being broken in real
archives:

- **A scribal error is transcribed as written, with `[sic]`** — not "corrected", and not recorded as a
  reading ambiguity. "The clerk appears to have written the wrong year" is an observation about the
  document; it goes to `notes/questions.md` (step 7), never into the transcript as a fixed value. A
  reading ambiguity (`[Hartley?]`) says *I could not read this*. A `[sic]` says *I read it fine and the
  page is wrong*. Collapsing the two loses the only distinction that matters later.
- **Never expand, normalise or modernise.** No spelling out abbreviations, no converting dates to EDTF,
  no resolving "do." or "ditto", no supplying a surname the column omits. Those are readings, and
  readings belong in claims, where a human gates them.

### 5. Write the file, attach it, index it

Write the transcript as markdown, in this shape (it mirrors `fha source extract`'s dump so the two read
alike):

```markdown
# Transcript - {source title} [S-xxxxxxxxxx]

Read and typed out from {filename} by an AI pass. The image is the evidence of record; this text is an
index into it, not a substitute for it. Uncertain readings are marked in line: `[illegible]`, `[word?]`,
`[unclear: a or b]`, `[sic]`.

[Page 1]
…the words on page 1…

[Page 2]
…the words on page 2…

<!-- AI-DRAFT 2026-08-16 {your-model-id} - transcript of {filename}, pages 1-2; not yet checked against the image by a human -->
```

Three structural rules the file must obey, because tools read it:

- **The `#` title is the only `#`/`##` heading in the file.** Page divisions are `[Page N]` labels, not
  markdown headings. A heading inside the body would split the draft block (see **The marker**).
- **The marker is the last non-blank line**, after the final page.
- **The header names the file by filename only** — never the absolute path you typed at the command
  line, which would publish your directory layout into an archived file and break the day the archive
  moves (AGENTS_TOOLING §11, SPEC §12.4).

Then file it and attach it. `fha process --more` only attaches a file already under the documents root,
and it appends the role to the stem itself, so write the file with the **source's shared stem carrying
neither the S-id nor a role suffix**, beside the primary:

```
fha process <one of the source's files> --more <documents/…/{stem}.md> transcript --dry-run
fha process <one of the source's files> --more <documents/…/{stem}.md> transcript
fha index                    # the words only become searchable once the index has read them
```

`{stem}.md` becomes `{stem}-transcript_{S-id}.md`. Two ways to get the name wrong, both avoidable: a file
that **already carries an S-id** is refused outright ("already carries an S-id"), and a file you helpfully
named `{stem}-transcript.md` is *not* refused — it files as `{stem}-transcript-transcript_{S-id}.md`,
because `--more` appends the role itself. If the resulting filename looks wrong, **say so and stop**: a
documents-root file is renamed exactly once, by `fha process`, and neither the file nor its `files:` entry
is ever hand-edited afterwards.

### 6. Record the pass

```yaml
## AI Passes
- {date: 2026-08-16, model: {your-model-id}, harness: {your-harness},
   # use your real model/harness identifiers - these two are placeholders, not values to copy
   task: "transcribe 3-page probate scan, pages 1-3", outputs: ["…-transcript_S-….md"], human_reviewed: false}
```

`outputs:` names the transcript file (this pass produced a file, not claims). `human_reviewed: false`
until a human has read it against the image — the record-side mirror of the marker.

### 7. Compare the transcript against every claim already on this source

**This is the most valuable thing the skill produces.** Hand-transcribing three image-only sources in the
archive that prompted this work turned up, in claims that were already `accepted`: a death age wrong by
seventeen years, a photograph identified as the wrong couple by an inference the document flatly
contradicts, a time of death wrong in two claims *and* in a note that purported to quote the page
verbatim, and a scribal error being carried as a reading ambiguity. The errors were not in the hard-to-read
sources — the clean typed form was fine; the mistakes were in a typed letter and a legible hand.

So: read every claim on this source against what the page actually says. `fha find <S-id>` gives you the
record's path and its claim counts by status; the claims themselves you read out of that record's
`## Claims` block.
Where the transcript contradicts a claim — **including an `accepted` one** — that is a research question,
not a correction you make. Write a `## Q:` block into `notes/questions.md` **before moving on**
(AGENTS.md §"When you cannot answer a research question"), using SPEC §17's format:

```markdown
## Q: Does the probate page support the death age recorded for Caleb Hartley?

origin: agent
status: open
refs: [[S-xxxxxxxxxx]], [[C-xxxxxxxxxx]], [[P-xxxxxxxxxx|Caleb Hartley]]
context:
- 2026-08-16 (agent): The accepted claim gives his age at death as 74. The page, transcribed this
  session, reads "aged 57 years" at [Page 2]. One of the two is wrong; the image is the evidence, so
  the page should be re-read by a human before the claim is touched.
```

Never edit or re-status the claim yourself. Offer the human the review — "there are three of these; want
to walk them?" — and if he takes it, that is a `review-claims` session on this source, where his decision
is written with `fha claim`. A contradiction you noticed and did not log is a contradiction you lost.

The same rule runs the other way and is worth saying out loud: a claim the transcript **supports** is not
thereby confirmed. You read the same picture the first pass read.

### 8. Hand off

- **Facts the document states that no claim covers** — the hand-transcription test found three per three
  sources — are **not** drafted here. Say how many there are and hand off to `mine-transcript`, which
  drafts them `suggested` with `anchor:`s, from the text you just wrote rather than from the pictures.
- **Called from `process-source`?** Hand back to its Stage B, which now drafts from the transcript.
- Close with `fha lint`. W124 should have cleared for this source; if it has not, the transcript did not
  attach as a text companion — check the `files:` role and the file's extension before doing anything
  else.
- End with the one plain sentence this skill owes the human: *the scan is still the record; this typing
  is how you find your way around it, and nobody has checked it against the image yet.*

## The marker — how a consumer tells an unreviewed transcript from a checked one

**This is a contract, not a suggestion.** `fha find --text` and any other consumer must be able to mark a
hit that comes from an unreviewed machine reading, so the rule is stated here precisely enough to
implement.

It reuses the archive's existing marker pair rather than inventing a third convention: the same
`<!-- AI-DRAFT … -->` / `<!-- AI-ACCEPTED … -->` comments `write-biography` puts around draft prose and
`fha confirm draft` flips, with the same grammar (`<!--`, optional whitespace, the word, anything up to
the first `-->`; the comment may span lines).

**Scope.** The rule applies to a *text companion*: a `files:` entry whose `role:` is `transcript`,
`transcription` or `extracted-text` and whose file ends `.md` or `.txt` — exactly the set `fha index`
loads into `transcripts_fts`, so a consumer can evaluate it against the indexed content with no extra
file reads.

**The four states,** decided on the companion's full text `T`:

| State | Rule |
|---|---|
| **unreviewed** | `T` contains a complete `<!-- AI-DRAFT … -->` marker. A machine read the images; no human has checked the text against them. |
| **verified** | `T` contains a complete `<!-- AI-ACCEPTED … -->` marker and **no** `AI-DRAFT` marker. A human compared it to the image. |
| **unmarked** | `T` contains neither marker word. A human typed it, or `fha source extract` dumped it mechanically from a PDF's own text layer. The archive makes no AI claim about it. |
| **damaged** | The literal text `AI-DRAFT` or `AI-ACCEPTED` appears outside any complete marker (an unterminated `<!--`, a stray mention). |

**Precedence and posture.** *unreviewed* outranks *verified* — any AI-DRAFT marker anywhere in the file
makes the whole file unreviewed. *damaged* is treated as **unreviewed**, never as verified: this fails
closed, exactly as `_lib.strip_unaccepted_drafts` does when it cannot tell draft from accepted prose.

**Placement.** Exactly one marker per companion, on its own line, as the **last non-blank line of the
file**, and no `#`/`##` heading below the title. That is not decoration: `strip_unaccepted_drafts` treats
an AI marker as sitting at the *end* of the span it covers and a `#`/`##` heading as a block boundary, so
a marker at the end of a heading-free file covers the whole transcript. Any publication path that ever
runs that function over transcript text then withholds the entire unreviewed reading, rather than
publishing everything above a top-of-file marker. Put the marker at the top and that same function
publishes the whole unchecked transcript.

**Marker body.** `<!-- AI-DRAFT {date} {model-id} - transcript of {filename}, pages {range}; not yet
checked against the image by a human -->`. The date and model are provenance and are preserved through
any flip, exactly as `fha confirm draft` preserves them.

**The record-side mirror.** The source's `## AI Passes` entry for this pass carries
`human_reviewed: false` and names the file in `outputs:`. The two must agree, and where they disagree
**the file's marker wins** — the file travels (into a packet, onto a USB stick, out of the archive
entirely) and must state its own status without its record, and it is the file whose text the index
already holds.

**Flipping it is a gap, and this skill blocks on it** (`_STANDARD.md` §6). `fha confirm draft` takes a
`<P-id>` and edits a person profile; there is no verb that flips a marker in a source's companion file,
and a skill never hand-edits a marker. So today every AI transcript stays *unreviewed*, which is at least
true. The wanted verb and what it must do are recorded in [`GAP.md`](GAP.md). Tell the human plainly:
"I can write out what it says; there is no button yet for you to sign off that you have checked it, so
searches will keep flagging this text as unchecked."

## Backfill (the batch case)

The archive that produced #46 held **43 image-only sources carrying 135 accepted claims**. Batches are
bounded, resumable and reported — the shape AGENTS.md gives migration mode, though this is not migration
mode: nothing moves, nothing is renamed, and every write is additive.

1. **Take the worklist from the tools, never from a list you keep in your head.**
   ```
   fha lint --json          # every W124: the sources whose accepted claims rest on unreadable evidence
   fha find --text "…"      # its coverage note prints the count of sources with no searchable text
   ```
   W124 names the source and how many accepted claims rest on it. **Work highest-claim-count first** —
   that is where a misreading has already been believed the most times.

2. **Resumability is free, and must stay free.** A transcribed source stops being reported by W124, so
   re-running lint at the start of the next batch *is* the resume. Keep no queue file, no session memory,
   no "sources 12-20 next time" note (AGENTS.md §"Sessions are an interface, not memory"). Anything worth
   remembering is already in the archive: coverage inside one long source goes in its `## Notes` via
   `fha source note`, and a source you skipped and why goes in `notes/questions.md`.

3. **Bounded batches: five sources, or one long multi-page source, per confirmed batch.** This is model
   reading, not file moving — the ceiling is attention, and it is far below migration mode's 200 files.
   Confirm the batch with the human before starting it and report before proposing the next. Never offer
   to "do all 43".

4. **Report at the end of every batch**, in plain words:
   - sources transcribed, with page counts;
   - sources where `fha source extract` did the work instead (no model reading needed);
   - **contradictions found with existing accepted claims, and the questions logged for them** — lead
     with this, it is the finding;
   - facts stated in the documents that no claim covers, and the offer to hand off to `mine-transcript`;
   - sources skipped, and why (no viewer, illegible beyond use, a source already partly transcribed);
   - what remains: the W124 count after this batch, straight from lint.

5. **Every transcript in a backfill batch is unreviewed AI text.** Say so once per batch, in those words.
   A backfill produces a great deal of newly searchable text very quickly, and its whole value depends on
   the next reader knowing that nobody has checked it yet.

## Guardrails

- The image is the evidence of record; the transcript indexes it and never replaces it.
- **No claim is drafted, edited, re-anchored or re-statused by this skill** — not even an obviously wrong
  one. Contradictions become `## Q:` blocks; new facts go to `mine-transcript`; status is `review-claims`'
  and the human's.
- Never guess a word. `[illegible]` and `[word?]` are correct answers; a confident invention is not.
- Never "correct" the document. `[sic]` preserves the error; the observation goes to `notes/questions.md`.
- No harness that cannot view the images may produce a transcript from filenames, keywords or existing
  claim values. Halt and say so.
- Originals are untouched; nothing under the photos root is renamed; the transcript is a new file
  attached by `fha process --more`, and neither it nor its `files:` entry is ever hand-edited afterwards.
- Nothing written into the archive carries a machine-specific absolute path (AGENTS_TOOLING §11) — the
  transcript header names the file it read by filename.
- Any record ID written into `## Notes` or `notes/questions.md` prose is `[[ ]]`-wrapped, `[[ID|Name]]`
  preferred; bare IDs only in structured YAML fields and tool arguments (_STANDARD.md §11).
- A marker is written at creation and **never hand-flipped**; the missing flip verb is a blocked gap
  (`GAP.md`), not something to work around.

## Done when

- Transcribing an image-only source in a session on `example-archive` produces a `role: transcript`
  companion attached by `fha process --more`, `[Page N]`-labeled in the original's pagination, ending in
  a single `<!-- AI-DRAFT … -->` marker, with the pass recorded in `## AI Passes` as
  `human_reviewed: false`.
- After `fha index`, `fha find --text "<a phrase only in the images>"` returns the source — the phrase was
  unfindable before — and `fha lint` no longer reports W124 for it.
- A PDF carrying its own text layer is handled by `fha source extract` with no model reading, and its
  dump carries **no** AI marker.
- Every contradiction between the transcript and an existing claim is an open `## Q:` block in
  `notes/questions.md`, and **no claim's value, status or anchor was changed**.
- A backfill run works in confirmed batches of five sources or fewer, takes its worklist from `fha lint`
  each time rather than from session memory, and reports contradictions first.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9).
