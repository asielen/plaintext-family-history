---
name: import-notes
description: >
  Run when the human hands over a pile of freeform research notes: "import my old notes", "sort this
  notes file into the archive", "here's everything I jotted down over the years", "I found my old
  research notebook". Chunks the pile, proposes a home for each chunk under the routing rule (evidence
  someone asserted → inbox → source; a thing to find out → open question; a testable belief → a
  working answer on that question; a search already run → research log; everything else →
  notes/research/), and writes each chunk only on the human's confirmation. Drafts no claims:
  evidence gets its claims later through process-source and review-claims. Never deletes or rewrites
  the original notes; a scraps file is dissolved only on an explicit say-so after everything in it
  has landed.
---

# import-notes

Intake for legacy notes. The archive has a home for every kind of research paper (the "Where does a note go?" rule in [`docs/FILING_CABINET.md`](../../../docs/FILING_CABINET.md)), but a decades-old pile arrives with the kinds mixed together, and nobody wants to sort it alone. This skill reads the pile, proposes a routing per chunk, and writes each chunk to its destination in that home's own SPEC format once the human confirms. It drafts no claims and decides nothing alone: the skill presents the routing plan and the human rules on it. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"Import my old notes", "sort these notes into the archive", "here's a file of everything I know about the Hartleys", "I found my old research notebook, get it in". Invoked only on a pile the human names: a file, a folder of files, or pasted text. Works one pile at a time; a folder of piles is worked file by file.

## The contract for this skill (state it before you start)

- **Routing only, no claims.** This skill drafts zero claims. Evidence chunks land in the inbox and get their claims later through `process-source` → `review-claims`, the normal gate. Nothing here reaches `accepted`.
- **Every write is a confirmed routing.** The skill proposes a home per chunk and the human rules on each. A grouped reply ("file 1–6 as proposed, 7 is a question") is one decision per chunk; grouping only shortens the presentation. An unruled chunk stays unfiled.
- **The original is never modified.** The pile is never edited, rewritten, reordered, renamed or moved, not even to append a progress line (AGENTS.md §"The contract" 3: originals are never modified in content, name or location, and never deleted by tools). Dissolving applies only to a throwaway scratch file the human pasted or created as working scraps, and only after every chunk has landed and the human explicitly says to. A real archive original (anything in `inbox/`, `documents/` or another asset location) is never deleted or altered by this skill.
- **Resume state is written down** (_STANDARD.md §7). A half-imported pile leaves its dated resume state in a separate place: a `{stem}.import.md` sidecar note beside the pile, a scratch note under `notes/research/`, or the source record once it exists. It is never written into the named original. The next session picks up from there.

## Flow

1. **Read the pile, then ask the one framing question.** Read the notes file directly. The pile is the subject of this session, so this is the one case where reading a whole file is better than querying the index; context for everything around it still comes from `fha` calls (`fha find`, never a bulk read of the asset trees). Then, once per pile:

   *"Are these notes a document in their own right, something the archive should keep and cite (like your aunt's handwritten pages)? Or are they your own working scraps, fine to break up once everything in them is filed?"*

   - **Keep it:** someone else's writing, inherited research, anything the family would want to see as written. **This is the default when unsure**, because keeping is the safe choice. The file itself becomes evidence: copy it into `inbox/` (his original stays where he had it, untouched) with a `{stem}.notes.md` sidecar carrying whatever can be pre-filled with confidence: `title:`, `source_type:` from the controlled vocabulary (`letter`, `interview` for recorded memories, `book`, `other`), `source_date:` (EDTF; you translate "sometime in the nineties" → `199X`), and `people:` as plain name hints (a stub has no p-ids yet). The assertions inside **stay in the file**: they become claims when `process-source` works it, and a fat notebook is mined like a long document (section anchors, coverage notes, several sittings). Only the non-evidence chunks (questions, guesses, searches run, strategy) route out through steps 2–5. Write their provenance refs as `[[s-…]]` once the record exists, or name the file in the context line if processing is deferred.
   - **Scraps:** his own working notes with no value as written. Chunk and route everything, evidence included, and leave the original in place until the very end.

2. **Chunk on natural seams.** Headings, dates, blank-line breaks, topic shifts. Group what belongs together (five scattered lines of Aunt Mary's farm memories are one chunk) and number the chunks so the human can answer by number.

3. **Propose a home per chunk, using the routing rule in plain words.** One line per chunk: the chunk in his words, the proposed home, and why (docs/FILING_CABINET.md "Where does a note go?"):
   - **Something someone asserted**, such as "Aunt Mary said the farm burned in 1922", is *evidence*. It goes to the inbox and becomes a source.
   - **Something to find out**, such as "who were Margaret's parents?", is an *open question*. It goes in the person's research file (`## Open Questions`) when it is about one person, and that file is created the first time something is written to it (SPEC §16b); otherwise in `notes/questions.md`. Never in the profile itself, because questions are read from those two places only. His own questions are always welcome. A gap a tool can compute ("no death date for Thomas") is not filed as a question, because `fha report` shows it fresh every time (SPEC §17b); say so and leave it out.
   - **Something believed but unproven**, such as "I think this is the same John as the 1881 census", is a *working answer* (SPEC §17a rung 2): the question it answers, with the guess, the reason for it and what would settle it added to that same entry. It is part of the question entry, and it is never a claim.
   - **A search already run**, including one that found nothing, is a *research-log* entry in `notes/research-log.md`, the archive's one search log (SPEC §17c).
   - **Everything else** (strategy, musings about several people, draft write-ups) goes to `notes/research/`.

   Resolve every name against the index (`fha find "Margaret Cole"`) so refs pin to IDs. An ambiguous name gets a short candidate list for the human. A name with no match stays a plain name: this skill never mints person stubs (that happens at process time, when there is evidence to hang on them).

4. **Capture the ruling.** Present the numbered plan and take one reply: "file 1–6 as proposed; 7 is a question about Margaret, not a guess; skip 9." Repeat back anything he changed, then write. Ask at most one plain question per ambiguous chunk. A chunk that fits nowhere cleanly is proposed for `notes/research/`, so nothing is dropped and the session doesn't stall.

5. **Write each confirmed chunk in its home's own format.**
   - **Evidence (scraps mode):** write `inbox/{slug}.md` with the chunk verbatim under one attribution line ("From the researcher's notes, undated (~2015), imported 2026-07-22:"), plus a `{slug}.notes.md` sidecar with the same hints as step 1. The note is the document (FILING_CABINET: "the note itself is the document"), so never set `asset_elsewhere: true`; that flag means the asset lives somewhere else, and here it doesn't. Related evidence about one informant or topic shares one inbox item.
   - **Open question:** a `## Q:` entry in SPEC §17b's form, in its step-3 home (the person's research file under `## Open Questions`, else `notes/questions.md`):
     ```markdown
     ## Q: Did the Cole farm burn in 1922?
     - origin: human
     - status: open
     - refs: [p-cd795c61e0]
     - context:
       - (agent, 2026-07-22) Imported from "old-research-notes.md"; routing confirmed by the human.
     ```
     `origin: human`, because the question is his and the skill only filed it. Check the destination file first: if the same question already exists there, append a dated `context:` line to the existing block instead of writing a twin (two identical `## Q:` headings in one file shadow each other).
   - **Working answer:** the same `## Q:` entry with four more field lines: `- id:` (mint it with `fha id mint h`), `- guess:` in his words, `- basis:` (his reason, from what the note gives) and `- verify:` (what would settle it). If the question is not written yet, write it in the same go with `origin: human` (an imported belief is his, not the machine's) and `status: open`. IDs inside `guess:`/`basis:`/`verify:` are `[[ ]]`-wrapped prose. If the question already carries a working answer, append a dated `context:` line instead of writing a twin.
   - **Research log:** one entry in `notes/research-log.md` in SPEC §17c's shape: `date`, `question` (put the person's `[[p-…]]` there so the search counts for them), `repository`, `collection`, `terms`, `result`. Date it from the note itself when it names one; otherwise use today's date and say so in the entry ("imported; original search date unknown"). A logged nil is a result. It is what stops the next session from re-running the same dead end.
   - **Everything else:** a file in `notes/research/` with a provenance line at the top ("Imported from 'old-research-notes.md', 2026-07-22; routing confirmed."). IDs in the prose are `[[id|Name]]`.

6. **Leave a resume marker if the session ends mid-pile, outside the original.** Write the dated resume state to a separate place; never append it to the named pile. Scraps mode: a `{stem}.import.md` sidecar note beside the pile, or a scratch note under `notes/research/`, carrying the marker `<!-- import-notes 2026-07-22: chunks 1-9 filed; "London letters" section onward not yet -->`. Keep mode: the coverage note lives in the source record once it exists (`fha source note <s-id> --text "…"`), following `process-source`'s own rule for long documents. Either way the original file stays byte-for-byte as the human handed it over.

7. **Close out.** `fha index`, then `fha normalize-links --dry-run` (this skill writes a lot, and the tidy pass catches a bracket slip), then `fha lint`. Tell him where everything landed in plain words ("two questions on Margaret's sheet, a guess about Thomas written beside its question, and Aunt Mary's memories are in the inbox ready to process") and name the next step, usually `process-source` on the new inbox items. Only now, and only on his explicit say-so, is a fully filed scraps file deleted.

## Guardrails

- Zero claims drafted. Evidence becomes claims only through `process-source` → `review-claims`.
- Never delete, rewrite, reorder, rename, move or append to the human's original notes. Resume state goes to a `{stem}.import.md` sidecar or a `notes/research/` scratch note (AGENTS.md §"The contract" 3). A dissolve applies only to a throwaway scratch file the human pasted as working scraps, needs an explicit instruction, and happens only after everything has landed; a real archive original is never dissolved.
- No chunk is filed without the human's ruling on it; silence or a topic change is not a ruling.
- This skill never mints person stubs and never writes to a source's `## Claims`.
- Imported questions and working answers carry `origin: human` plus a dated import `context:` line naming the pile, so the provenance travels with the chunk.
- No duplicate `## Q:` headings in one file and no twin working answers; append dated context lines to the existing entry instead.
- No computed gap is filed as a question (SPEC §17b).
- Any record ID written into prose is `[[ ]]`-wrapped, `[[id|Name]]` preferred; bare IDs only in structured slots (a `refs:` list, tool arguments), per _STANDARD.md §11. A stub sidecar's `people:` holds names, never p-ids (a stub is pre-source).
- `source_type:` hints come from the controlled vocabulary (`letter`, `interview`, `book`, `website`, `artifact`, `other`, …). `fha process` refuses an out-of-vocabulary hint, so a mistyped type would stall the hand-off to `process-source`.

## Done when

- A messy, multi-topic notes file imports in a session on `example-archive`: chunked on natural seams, each chunk proposed a home under the routing rule with names resolved to IDs, the human's grouped ruling captured, and every confirmed chunk written in its home's SPEC format.
- Evidence lands in `inbox/` (the whole file in keep mode, attributed chunk documents in scraps mode, each with sidecar hints) and is handed off to `process-source`, with **zero** claims drafted by this skill.
- Imported questions, working answers and log entries carry `origin: human`, a dated import note and `[[ ]]`-wrapped IDs in prose; nothing duplicates an existing question or working answer, and every search landed in `notes/research-log.md`.
- The original pile is byte-for-byte intact, with its resume state in a `{stem}.import.md` sidecar or a `notes/research/` scratch note, unless the human explicitly dissolved a fully filed throwaway scratch file.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9).
