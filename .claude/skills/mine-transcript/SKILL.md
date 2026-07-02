---
name: mine-transcript
description: >
  Run ONLY when explicitly asked — "mine grandpa's interview", "extract claims from this transcript".
  Never runs on its own. Reads a transcript source, drafts `suggested` claims (with anchors) for the
  substantive assertions only, resolves names against the index, routes narrative to `## Stories`, and
  records the pass — leaving the transcript text byte-for-byte unchanged. Extraction is indexing, not
  preservation.
---

# mine-transcript

An interview or long transcript holds a handful of real assertions buried in pages of narrative. This is
the *selective* extraction pass: draft claims for the assertions, route the story to `## Stories`, and
**leave the transcript intact**. It resolves names and drafts `suggested` claims — the same primitives
`process-source` uses — but over already-processed transcript text, and only on request. See
[`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Explicit invocation only: "mine grandpa's interview", "pull the claims out of this transcript", "extract
what's usable from Ethel's interview." **Nothing mines silently** (AGENTS.md, TOOLING_INTERFACE.md §2.1)
— if the human didn't ask, this skill does nothing.

## The contract for this skill (state it before you start)

- **Invoked-only.** No automatic runs, no "while I'm here" mining.
- **Suggested-only.** Every drafted claim is `status: suggested`; the human gates them later via
  `review-claims`.
- **Selective, not exhaustive.** Draft claims for *substantive* assertions (a stated birth, marriage,
  residence, occupation, relationship). Do **not** claim-ify narrative colour, feelings, or hearsay-about-
  hearsay — that's story, not fact.
- **Never alter the transcript.** Extraction reads; it does not delete, rewrite, condense, or "clean up"
  the transcript text. The transcript is preserved and searchable; mining only *indexes* it.
- **Record the pass** in the source's `## AI Passes` block.

## Flow

1. **Read the transcript source.** Locate it (`fha find <S-id>` or `fha find <text>`), open the source
   `.md` and its transcript file. Read the whole thing before drafting — the substantive assertions are
   scattered.

2. **Draft `suggested` claims for substantive assertions only.** For each real fact stated:
   - add a claim to the source's `## Claims` block: `status: suggested`, a fresh `id:` (`fha id mint C`),
     the right `type:`, resolved `persons:`, a `value:` sentence;
   - an **`anchor:`** pointing at the exact spot — for a transcript this is the timestamp or line
     (`anchor: "00:14:32"`) so the reviewer can hear/read the source words;
   - `date:`/`place_text:` translated to stored forms (a fuzzy "around the turn of the century" → `1900~`;
     you translate, he never types EDTF), plus the Mills fields (interview testimony is usually
     `information: primary` from the speaker but `confidence: low` when the memory is vague — SPEC §8.5).
   - Skip chatter, tangents, and un-anchorable generalities.

3. **Resolve each named person against the index — propose, don't guess.**
   ```
   fha find "Ethel"                  # resolve the name to a P-id
   fha find --related <P-id>         # disambiguate when a name is shared
   ```
   Ambiguous name → candidate list for the human; genuinely new person → create the stub *record* on
   confirmation with `fha stubs --from-names "Name"` (dry-run, then apply) — this mints the P-id **and**
   the `people/stubs/` record together, so a drafted claim's `persons:` never dangles. A bare
   `fha id mint P` only returns an ID with no record, so a claim using it would trip lint **E005**. Never
   write a silent guess as a claim's `persons:`.

4. **Route narrative to `## Stories`; leave the rest in the transcript.** Story-shaped passages (an
   anecdote about the railroad job, a description of the family home) go to the source's `## Stories`
   section as narrative chunks tagged with `[[P-…]]` refs — feedstock for a future biography. Everything
   else **stays in the transcript**, unchanged (it remains searchable via `transcripts_fts` when that
   surface is populated).

5. **Record the AI pass** in the source's `## AI Passes` block:
   ```yaml
   ## AI Passes
   - {date: 2026-07-01, model: claude-sonnet-4-6, harness: claude-code,
      task: "mine Ethel Hartley interview for claims", outputs: [C-…, C-…], human_reviewed: false}
   ```

6. **Offer the hand-off, then close out.** Report the result plainly ("pulled four facts and two stories
   out of the interview; the transcript itself is untouched — ready for you to review the four when you
   like") and offer to hand off to `review-claims` right away. If the human takes the hand-off, **stop
   here** — review-claims' close-out (reindex, xref, view refresh, lint) covers this source, and running
   it twice back-to-back is wasted work. Only when the session ends *without* an immediate review do you
   close out yourself:
   ```
   fha index                     # full rebuild — this pass may have minted new person stubs, and
                                 # `fha index --source <S-id>` reindexes only the source's claims, not
                                 # new person records or their aliases (index.py upsert_source)
   fha lint
   ```

## Guardrails

- Takes **no action unless explicitly invoked**.
- Suggested-only; never `accepted`.
- Selective — narrative colour becomes a Story, never a claim.
- The transcript text is **byte-unchanged** — never deleted, rewritten, or trimmed.
- Names resolve via candidate proposals or confirmed stubs, never silent guesses.

## Done when

- Mining a transcript in a session on `example-archive` drafts `suggested` claims with `anchor:`s for the
  substantive assertions, routes stories to `## Stories`, records the pass in `## AI Passes`, and leaves
  the transcript text **byte-unchanged**.
- The skill takes no action unless explicitly invoked.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9).
