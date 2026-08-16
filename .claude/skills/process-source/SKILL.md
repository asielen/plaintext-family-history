---
name: process-source
description: >
  Run when the human says "process the inbox" / "process this file" / "turn this into a source", or after
  a `fha capture --ingest` sweep drops items in the inbox. Runs `fha process` (Stage A: mint the S-id,
  rename documents / keyword photos, scaffold the record), then drafts `suggested` claims from the
  evidence (Stage B: read the file including images, resolve people and places against the index), then
  hands off to `review-claims` (Stage C). An image-only item is transcribed first, via
  `transcribe-source`, so claims come from text rather than from one pass over pictures. Handles
  loosely-written notes without stalling.
---

# process-source

The everyday intake path: an inbox item — a scan, a photo, a capture stub, a bundle folder, a jotted note
— becomes a real source record with drafted `suggested` claims. `fha process` owns the deterministic
Stage A (ID, renames, scaffold); this skill adds Stage B, the AI draft that reads the evidence, resolves
the people and places, and drafts the claims. Then it hands the drafts to `review-claims` for the human
gate. Between the two sits Stage A½: an item whose files are all images gets transcribed **before** any
claim is drafted, so Stage B works from words rather than from one pass over pictures.
See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"Process the inbox", "process this file/photo/folder", "make a source out of this", or after an ingest
sweep. Works one item at a time; for a full inbox, triage and confirm each with the human.

## The contract for this skill (state it before you start)

- **Everything this skill drafts is `status: suggested`.** No claim is `accepted` at this stage — Stage C
  (`review-claims`) is the gate.
- **Resolve names by proposing candidates, never by silent guessing** (AGENTS.md §"Who you serve"): an
  ambiguous name gets a candidate list for the human, or a fresh stub on his confirmation.
- **Record the pass** in the source's `## AI Passes` block before hand-off.
- **Forgiving, not fussy** (_STANDARD.md §5): a loose note is the normal case, not an error — extract what
  you can, fold the rest into `## Notes`, never refuse.
- **Never rename anything under the photos root** (AGENTS.md §"Don'ts"); `fha process` keyword-tags photos
  and renames only documents-root files.

## Flow

### Stage A — deterministic (`fha process`)

1. **Confirm the item's location, then process it.**
   ```
   fha process <file|folder> --dry-run     # preview the rename/keyword/scaffold plan
   fha process <file|folder>
   ```
   This mints the `S-id`, files the asset (documents-root: rename to the `{slug}_{S-id}` grammar;
   photos-root: write the `SOURCE: S-id` keyword, **never rename**), and scaffolds the `sources/…` record.

   **Photo or document?** The root is a management fact, not a content judgment: if it lives (or belongs)
   in the human's photo library, it goes to the photos root; everything else — scans, clippings, a
   photograph *of* a record — goes to the documents root. A stub note's `source_type:` hint (e.g.
   `source_type: census` on a `.jpg` scan) routes it to the documents root at intake; **without a hint the
   file extension decides**, so set the hint when filing a scanned record saved as an image, or a scanned
   letter will land in the photo library. When genuinely unsure, ask the human one plain question ("is
   this one for the photo albums, or the records drawer?"). Note that many border cases need no choice at
   all: a photos-root postcard and its documents-root transcription can share one source record (bundle or
   `--more`). A file the human pre-filed into any documents subfolder keeps its place — `fha process`
   renames in place, and folder organization inside the documents root is his to choose (SPEC §12.1:
   folders are projection); an inbox single with no chosen spot files into `documents/{type}/`. If he
   reorganizes filed documents later, `fha reconcile --dry-run` then `fha reconcile` re-ties every
   moved file to its record. If a past run filed something in the wrong root outright,
   `fha process refile <S-id> --to photos|documents` is the sanctioned cross-root correction — it
   moves the file, handles the rename/keyword at the crossing, and updates the record (preview
   with `--dry-run` first).

2. **If the item is a source stub, it seeds the record and is consumed** (SPEC §12.1):
   - A **`*.notes.md` sidecar** or a **bundle folder** carries a hint block (`source_type`, `source_date`,
     `people` name-hints, `files` roles) plus freeform prose. Its frontmatter pre-fills the §14 record;
     its parsed person/vital hints seed Stage B; its prose flows into the record's `## Notes`.
   - The stub/bundle is **promoted into the record, not left behind** — after processing, the inbox item
     is gone and the `sources/…` record is the truth.
   - A **bare file** with no sidecar starts Stage B from scratch.

### Stage A½ — if the item is image-only, transcribe it BEFORE drafting anything

3. **Check whether the archive will hold any of this source's words.** After `fha process` has filed
   the item, look at the record's `files:` inventory. A source is **image-only** when none of its
   entries holds text a search can read — no entry whose `role:` is `transcript`, `transcription` or
   `extracted-text`, and no `.md`/`.txt` file (`_lib.file_entry_carries_text`, the same rule
   `fha lint`'s W124 and `fha find --text`'s coverage note use). A scan, a photograph and a PDF with
   no text layer are all image-only, however much text a human eye can see in them.

   **If it is image-only, run [`transcribe-source`](../transcribe-source/SKILL.md) now, before step 4.**
   Not after, and not "if there's time". The transcript exists *first* so the claims you draft in
   Stage B come from text you read out line by line rather than from a single pass over pictures, and
   this skill is the only place that ordering can be enforced (issue #46 — a surname written plainly on
   a hand-drawn chart in an image-only scan was judged invented and struck, because nothing in the
   archive held the document's words). The transcript also carries the `[Page N]` labels your Stage B
   `anchor:`s will cite.

   It hands back with the source's words attached as a `role: transcript` companion, marked
   `<!-- AI-DRAFT … -->` — an unreviewed machine reading, and Stage B treats it as exactly that: the
   image is still the evidence of record, so read the transcript **beside** the file, not instead of
   it. Then carry on at step 4 with both in front of you.

   Two cases that are not this: a source that already has a transcript (nothing to do), and a PDF that
   carries its own text layer (`fha source extract` handles it mechanically — `transcribe-source` tries
   that first anyway, so just run the skill and let it decide).

### Stage B — the AI draft (judgment)

4. **Read the evidence — embedded text first, then your eyes.** The reading order is: what the file
   already says about itself, then the file itself.
   - **Embedded metadata first.** A photos-root image often already carries text: a **Caption** is a
     verbatim transcription of what is written on or with the photo — treat it as evidence; an AI
     summary (a `UserComment` beginning `AI: `) is machine-written — unverified context, never fact.
     The photo index has already scraped both — read them rather than re-deriving what an earlier pass
     captured: `fha photoindex find` prints the Caption; a prior AI summary surfaces only in
     `fha photoindex set-summary --dry-run`'s old → new preview (`find` never prints it).
   - Then read the document text, or *look at* the scan / photo if your harness can view images, or read
     the note. **If you cannot view images, say so plainly and work from the embedded metadata, sidecar
     notes, and filename/keyword hints — never guess at what a scan shows.** Query the index for context;
     **never bulk-read** the asset trees — this one file is the subject, the rest of the library is `fha`
     calls.

   **Large multi-page documents** (a long PDF, a county history, a probate file) are worked in windows,
   not swallowed whole — and never a reason to stall:
   - **Text layer first:** many archived PDFs carry an embedded text layer — `fha source extract <S-id>
     --dry-run` previews the coverage, and the live run writes a `[Page N]`-labeled companion you mine
     like a transcript in seconds instead of vision-reading hundreds of pages. An all-image PDF refuses
     honestly; fall back to the page windows below.
   - Read in **page windows** (~20 pages at a time), not the whole file at once.
   - Anchor every claim to the **original's pagination** — `anchor: "page 214"` (SPEC §8.4 sanctions
     page anchors) — so the reviewer can find the spot in any copy.
   - **Log coverage as you go** in the record's `## Notes` — `fha source note <S-id> --text "Mined pages
     1-60 for family mentions; 61 onward not yet read."` — so the work resumes cleanly next session
     instead of restarting.
   - Only family-relevant pages become claims; the rest staying un-mined is a legitimate permanent state
     (SPEC §4), not a backlog.
   - A transcription or excerpt that exists as its own file attaches to the same source — `fha process
     <primary> --more FILE transcript` — then mine *that* the way `mine-transcript` works a transcript,
     keeping page anchors.

5. **Resolve every named person and place against the index — propose, don't guess.**
   ```
   fha find "Margaret Cole"          # does this name already resolve to a P-id?
   fha find --related <P-id>         # the person's neighborhood, to disambiguate two same-named people
   ```
   - A clean single match → link that `P-id`.
   - An ambiguous name (two candidates, or a shared name) → present the candidates plainly and let the
     human pick; pin the choice to its ID.
   - A genuinely new person → mint the stub *record* on his confirmation, **by name and before you draft
     any claim about him**. Use the name-based path: plain `fha stubs` only scans claims already written
     (there are none for him yet, so it creates nothing), and a bare `fha id mint P` returns an ID with no
     record — either way the claim you draft in step 6 would reference a P-id with no stub and trip lint
     **E005**.
     ```
     fha stubs --from-names "Margaret Cole" --dry-run   # preview what will be created (does NOT reserve an ID)
     fha stubs --from-names "Margaret Cole"             # apply
     ```
     `fha stubs` mints a **fresh random** `P-…` on each run, so the dry-run's ID is illustrative only —
     use the `P-…` the **apply** command prints (not the dry-run's) when you draft the claim's `persons:`.
   Resolve places the same way (`fha find <place text>`; an unlinked place is fine — leave `place_text:`
   as written and let `place-research` / `fha confirm place` elevate a recurring one later).

6. **Draft `suggested` claims with anchors and Mills fields.** For each substantive assertion in the
   evidence, add a claim to the record's `## Claims` block:
   - `status: suggested` (always), a fresh `id:` (`fha id mint C`), the right `type:` (birth, death,
     marriage, residence, census, occupation, relationship, …), `persons:` (resolved P-ids), and a
     `value:` sentence;
   - an **`anchor:`** pointing at where in the source it came from (a page, a line, a timestamp) so the
     reviewer can check it;
   - the source's date/place as `date:` (EDTF — **you** translate his informal date: "around 1880" →
     `1880~`, "the 1880s" → `188X`) and `place:`/`place_text:`;
   - the Mills fields by default (`information:` primary/secondary, `evidence:` direct/indirect;
     `confidence:` defaulted from the source type). A relationship claim carries `roles:` and a `subtype:`.

7. **Route narrative and un-mappable prose.**
   - Story-shaped passages (an anecdote, a description) → the record's `## Stories` section, tagged with
     `[[P-…]]` refs.
   - Anything that doesn't map to a claim (a fuzzy lead, a "chase this" note, context) → `## Notes`.
     Folding it here is the correct move, not a failure — never stall because a note is loose.

8. **Record the AI pass** in the source's `## AI Passes` block:
   ```yaml
   ## AI Passes
   - {date: 2026-07-01, model: {your-model-id}, harness: {your-harness},
      # use your real model/harness identifiers - these two are placeholders, not values to copy
      task: "draft claims from 1880 census scan", outputs: [C-…, C-…], human_reviewed: false}
   ```

   **When nothing is claim-worthy, that is a complete outcome, not a failure.** Some items are part of
   the family record without asserting a checkable fact — a scenic photo, a keepsake, a clipping that
   names no one. A source with no claims is completely valid (the source template says so in as many
   words). Give it its discoverability surface instead: fill the record's `people:`/`places:` frontmatter
   links, write a `## Notes` paragraph saying what it is and why it was kept (this prose is full-text
   searched — it is where "keywords" go), delete the empty `## Claims` block or leave it out, record the
   AI pass with `outputs: []` (step 8), and **skip the `review-claims` hand-off** — there is nothing to
   review. Close with `fha index` and `fha lint` yourself, since the review close-out won't run.

   One flavor of this deserves special respect: **material kept because it was investigated and
   rejected** — a debunked lineage pamphlet, a mail-order surname history, a disproven family legend.
   Its record's title/Notes say it is not evidence, and that verdict is the human's research conclusion.
   **A later pass never re-reads such a source and drafts claims from it** — doing so would graft the
   rejected material back onto the tree. If new evidence genuinely reopens the question, that is the
   human's call to make, recorded in the Notes, before any claim is drafted.

### Stage C — hand off to the gate

9. **Hand off to `review-claims`** for this source. That skill walks each drafted claim with the human,
   captures accept/dispute/edit, and does the close-out (`fha index` — full, since intake usually minted
   new person stubs — `fha xref`, a timeline/sources-index/draft-queue refresh for the people touched, `fha lint`).
   Don't duplicate that work here — the reindex/xref/views/lint belong to the review close-out.

## Guardrails

- Every drafted claim is `status: suggested`; **nothing** is `accepted` in this skill.
- Names resolve via candidate proposals or confirmed stubs — never a silent guess written as fact.
- Informal dates/places are translated to stored forms *for* the human, in the claim; the human never
  types EDTF.
- At most **one** plain question on a genuinely ambiguous hedge — never a refusal, never a lecture.
- The stub/bundle is consumed into the record; the photos root is never renamed.
- Any record ID written into `## Notes`/`## Stories` prose is `[[ ]]`-wrapped, `[[ID|Name]]` preferred;
  bare IDs only inside the claims YAML block, frontmatter lists, and tool arguments (_STANDARD.md §11).
- Never force a claim out of an item that asserts nothing — the zero-claims exit above is the correct
  path, not a fallback.

## Done when

- Processing an inbox stub in a session on `example-archive` yields a real `sources/…` record with
  `suggested` claims + `anchor:`s, the stub/bundle **consumed**, the AI pass recorded in `## AI Passes`,
  and a hand-off into `review-claims`.
- A loosely-written note (approximate dates, informal spellings) processes without a hard refusal;
  un-mappable prose lands in `## Notes`; informal dates are translated to EDTF in the drafted claims.
- An item with nothing claim-worthy exits cleanly by the zero-claims path: `people:`/`places:` filled,
  context in `## Notes`, AI pass recorded with `outputs: []`, no review hand-off, no forced claims.
- An image-only item is transcribed at Stage A½ *before* any claim is drafted: the source carries a
  `role: transcript` companion, and the Stage B claims cite its `[Page N]` anchors.
- Every drafted claim is `suggested` (no claim is `accepted` at this stage).
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9).
