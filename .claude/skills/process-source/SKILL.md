---
name: process-source
description: >
  Run when the human says "process the inbox" / "process this file" / "turn this into a source", or after
  a `fha capture --ingest` sweep drops items in the inbox. Runs `fha process` (Stage A: mint the S-id,
  rename documents / keyword photos, scaffold the record), then drafts `suggested` claims from the
  evidence (Stage B: read the file including images, resolve people and places against the index), then
  hands off to `review-claims` (Stage C). Handles loosely-written notes without stalling.
---

# process-source

The everyday intake path: an inbox item — a scan, a photo, a capture stub, a bundle folder, a jotted note
— becomes a real source record with drafted `suggested` claims. `fha process` owns the deterministic
Stage A (ID, renames, scaffold); this skill adds Stage B, the AI draft that reads the evidence, resolves
the people and places, and drafts the claims. Then it hands the drafts to `review-claims` for the human
gate. See [`../_STANDARD.md`](../_STANDARD.md).

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

2. **If the item is a source stub, it seeds the record and is consumed** (SPEC §12.1):
   - A **`*.notes.md` sidecar** or a **bundle folder** carries a hint block (`source_type`, `source_date`,
     `people` name-hints, `files` roles) plus freeform prose. Its frontmatter pre-fills the §14 record;
     its parsed person/vital hints seed Stage B; its prose flows into the record's `## Notes`.
   - The stub/bundle is **promoted into the record, not left behind** — after processing, the inbox item
     is gone and the `sources/…` record is the truth.
   - A **bare file** with no sidecar starts Stage B from scratch.

### Stage B — the AI draft (judgment)

3. **Read the evidence — including vision for images.** Read the document text, or *look at* the scan /
   photo (vision), or read the note. Query the index for context; **never bulk-read** the asset trees —
   this one file is the subject, the rest of the library is `fha` calls.

4. **Resolve every named person and place against the index — propose, don't guess.**
   ```
   fha find "Margaret Cole"          # does this name already resolve to a P-id?
   fha find --related <P-id>         # the person's neighborhood, to disambiguate two same-named people
   ```
   - A clean single match → link that `P-id`.
   - An ambiguous name (two candidates, or a shared name) → present the candidates plainly and let the
     human pick; pin the choice to its ID.
   - A genuinely new person → mint a stub on his confirmation:
     ```
     fha stubs                       # create stubs for unresolved person references
     fha id mint P                   # or mint a P-id directly when adding one by hand
     ```
   Resolve places the same way (`fha find <place text>`; an unlinked place is fine — leave `place_text:`
   as written and let `place-research` / `fha confirm place` elevate a recurring one later).

5. **Draft `suggested` claims with anchors and Mills fields.** For each substantive assertion in the
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

6. **Route narrative and un-mappable prose.**
   - Story-shaped passages (an anecdote, a description) → the record's `## Stories` section, tagged with
     `[[P-…]]` refs.
   - Anything that doesn't map to a claim (a fuzzy lead, a "chase this" note, context) → `## Notes`.
     Folding it here is the correct move, not a failure — never stall because a note is loose.

7. **Record the AI pass** in the source's `## AI Passes` block:
   ```yaml
   ## AI Passes
   - {date: 2026-07-01, model: claude-opus-4-8, harness: claude-code,
      task: "draft claims from 1880 census scan", outputs: [C-…, C-…], human_reviewed: false}
   ```

### Stage C — hand off to the gate

8. **Hand off to `review-claims`** for this source. That skill walks each drafted claim with the human,
   captures accept/dispute/edit, and does the close-out (`fha index --source`, `fha xref`, a
   timeline/draft-queue refresh for the people touched, `fha lint`).
   Don't duplicate that work here — the reindex/xref/views/lint belong to the review close-out.

## Guardrails

- Every drafted claim is `status: suggested`; **nothing** is `accepted` in this skill.
- Names resolve via candidate proposals or confirmed stubs — never a silent guess written as fact.
- Informal dates/places are translated to stored forms *for* the human, in the claim; the human never
  types EDTF.
- At most **one** plain question on a genuinely ambiguous hedge — never a refusal, never a lecture.
- The stub/bundle is consumed into the record; the photos root is never renamed.

## Done when

- Processing an inbox stub in a session on `example-archive` yields a real `sources/…` record with
  `suggested` claims + `anchor:`s, the stub/bundle **consumed**, the AI pass recorded in `## AI Passes`,
  and a hand-off into `review-claims`.
- A loosely-written note (approximate dates, informal spellings) processes without a hard refusal;
  un-mappable prose lands in `## Notes`; informal dates are translated to EDTF in the drafted claims.
- Every drafted claim is `suggested` (no claim is `accepted` at this stage).
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9).
