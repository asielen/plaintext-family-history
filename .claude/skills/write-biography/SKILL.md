---
name: write-biography
description: >
  Run when the human says "draft Margaret's bio" / "write up this person" / "extend so-and-so's
  biography". Pulls the person's draft queue (accepted claims not yet cited in the profile) and drafts
  prose that cites every factual sentence, wrapped in `<!-- AI-DRAFT … -->` markers. Facts come only from
  `accepted` claims; links only from verified IDs; existing human text is never overwritten. Acceptance is
  the human's, via `fha confirm draft`.
---

# write-biography

`fha views draft-queue` computes the writing backlog: a person's `accepted` claims whose source isn't yet cited in the profile. This skill adds the *drafting*, turning those claims into readable prose that cites every factual sentence. A bio that reads well but cites loosely is worse than none. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"Draft Margaret's bio", "write up Thomas", "extend this profile now that the census is in." Always scoped to one person.

## Voice: two styles, one citation contract

The contract below fixes the bio's *facts*. Its *voice* is the human's choice between two named styles. SPEC §16 fixes structure and citation density, and both are the same in either style, so voice is a skills-layer setting rather than a spec matter:

- **`chronicle`** (the default): era by era and fact-forward. Plain, warm and short, telling what happened in order.
- **`narrative`**: story-driven. The same cited facts, with scene-setting and period context ("Kansas in 1875 was still sod-house country…"). Every factual sentence still cites its `[[s-…]]`. The extra texture comes ONLY from cited claims or clearly flagged general context, and **never from invented scenes, dialogue, weather, or interior states** (AGENTS.md §"Speculation and storytelling": an invented detail in a family story becomes family truth in one generation).

**Resolving which to use** (first match wins):

1. **The human's ask this session.** Translate his words yourself rather than asking him to pick a term (_STANDARD.md §4): "tell it like a story" → narrative; "just the facts" → chronicle. An ask that fits neither maps to the nearest style, honoring his phrasing inside that style's rules.
2. **The archive default**: a `biography:` block in `fha.yaml` (read the file directly, the same way the site tools read their `site:` block; no tool call needed):
   ```yaml
   biography:
     style: narrative      # chronicle | narrative
   ```
3. **Neither?** If the profile already carries drafted prose, match its register; the AI-DRAFT / AI-ACCEPTED markers record which style wrote it (step 4). Otherwise ask ONE plain question ("Straight chronicle, or more of a story?") and default to **chronicle** if he waves it off.

Extending an existing biography keeps the register already on the page unless the human asks to change it. A profile that is half chronicle and half narrative reads as two authors.

## The contract for this skill (state it before you start)

- **Facts only from `accepted` claims.** A `suggested` claim is not yet a fact, so it never becomes a biographical sentence. If the queue is thin because claims are still unreviewed, say so and offer a `review-claims` session first.
- **Cite every factual sentence.** Summary block: one citation per line. Body: all relevant citations. Anything uncited must read as *story or context*, not as asserted fact (AGENTS.md §"Write a biography").
- **Link only verified IDs.** Check that every `[[p-…]]`/`[[s-…]]` exists before you write it (`fha find`), so there are no dangling links (lint E004).
- **AI-DRAFT until accepted.** All new prose is wrapped in `<!-- AI-DRAFT … -->`; the human accepts via `fha confirm draft`, never a hand-edit.
- **Never overwrite human text; never edit below a GENERATED header.** Draft *around* existing prose.
- **Respect privacy** (AGENTS.md §"The contract" 6): don't surface a `living`/`restricted` person or detail into prose destined for export.
- **Write about the family, not about the archive's research process.** A sentence explaining *why* a claim was accepted, that it "corroborates" or "independently confirms" a settled fact, or how the evidence was verified describes the review pipeline rather than a life, and it has no family event to cite (AGENTS.md §"Speculation and storytelling" → "Keep the archive's own paperwork out of the family's story"). Cite the source and state what happened. Leave *how sure we are and why* out of the prose; it already lives in the claim's own `confidence:`/`information:`/`evidence:` fields.

## Flow

1. **Pull the backlog and the facts.**
   ```
   fha views draft-queue <p-id>      # the uncited-accepted-claim writing backlog
   fha find <p-id>                    # the person's record, claims, and existing profile
   ```
   The draft queue may end with `## Connected facts`: computed facts that tie the person to their family (lost a parent young, shared a birthday with a sibling, how many children died before them), each with the sources it rests on. They are accepted facts like any other. Use them where they help the story, in your own sentences, citing every source the line lists. Never copy the line, and never name a child it counts as living.
   If the target person is still a **stub** (`tier: stub`, or their record sits in `people/stubs/`; the draft-queue view refuses them either way), offer `fha person promote <p-id>` first. A biography belongs on a full profile: in its couple folder for a direct-line person, or flat in `people/connections/` for anyone else (SPEC §12.3). Run it only on the human's yes (preview with `--dry-run`), then `fha index` and continue. If promote refuses because the person is **not on the direct line**, offer the connections form instead, `fha person promote <p-id> --into connections/` (same tier flip and body backfill, no numbering). If the human declines that too, the facts stay in claims and `## Stories` for now, and you stop here rather than drafting a bio the views can't carry. Read the person `.md` and note the existing biography prose (human and any prior AI-DRAFT) so you draft around it, not over it. The draft queue tells you which sources' accepted facts still need prose.

2. **Draft the prose, facts only from accepted claims, each sentence cited.**
   - Write in the resolved style's voice (see "Voice" above): plain and warm, never mechanical. Narrative adds cited or clearly flagged texture and never invents.
   - **Pronouns come from the person record** (SPEC Appendix A.7): from `gender:` when it is present, else from `sex:`. When neither says, write the name and keep the prose neutral rather than guess. Never infer them from a first name, a photo, or a role in a claim.
   - **Every factual sentence carries its source** as a `[[s-…]]` link: *"He worked as a bookkeeper for the Plains Junction Railroad by 1880 [[s-4f5f215e60]]."*
   - **That is a citation rule, not a layout rule, so write paragraphs.** Flowing paragraphs, one paragraph per line in the file (the editor wraps it; never hard-wrap), with a blank line between them. Several cited sentences share a paragraph whenever they share a subject, era, or episode (typically 3-6 sentences); never write one sentence per line. The Biography section is already "chaptered by era/place" (SPEC §16), and the paragraph is that chaptering's working unit.
   - `fha views summary` writes the **summary lines** at the top (Born / Died / Married / Parents / Children) from accepted claims, so never draft them. Only where the human has taken them over by hand (no GENERATED markers) do they follow the citation rule: one citation per fact-line. The **body** takes all relevant citations.
   - Anything you can't cite to an accepted claim must read explicitly as context or story ("Family recollection holds that …"), never as a stated fact. When in doubt, cut it or move it to Stories.

3. **Verify every link before writing it.**
   ```
   fha find <p-id>        # confirm each person link resolves
   fha find <s-id>        # confirm each source link resolves
   ```
   Prefer the `[[id|display]]` form (`[[p-cd795c61e0|Margaret A. Cole]]`). A name-link (`[[Margaret Cole]]`) resolves through the alias layer, but pin to the ID when the name could be shared. Never write an ID you haven't confirmed exists.

4. **Wrap all new prose in AI-DRAFT markers, drafting around human text.**
   ```markdown
   {new biographical prose, every factual sentence cited}

   <!-- AI-DRAFT 2026-07-01 {your-model-id} - biography drafted (chronicle) from accepted census + marriage claims -->
   ```
   Name the style in the marker note so a later session can match the register without asking again (_STANDARD.md §7). Place the marker at the end of the block you wrote. Leave any existing human-written paragraph exactly as it is: add your paragraphs before or after it, never inside it. If `## Biography` still holds only the scaffold's own instruction line, word for word as shipped ("Write their story in plain sentences. ..."), replace that line with your draft so the instructions are not published above it; if he changed even one word of it, it is his and stays. Never touch a `<!-- GENERATED … -->` section (the timeline and draft-queue companion files are regenerated by their own tools, never hand-edited). **The profile you are drafting into also carries its own `## Sources` section** (SPEC §16), bounded by `<!-- GENERATED-BEGIN sources-index … -->` / `<!-- GENERATED-END sources-index -->` markers in the same file, above `## Biography`. That region is machine-owned like a GENERATED-headed companion file and is rewritten only by `fha views sources-index <p-id>`; it just lives inside the file you are editing instead of beside it. Draft only into `## Biography` / `## Stories` / `## Research Notes` (or `## Friends & Family` by hand). Never write into, through, or around that region, and never treat its bracketed source list as prose you could cite from or extend. The purpose block at the very top of the body (`> **This person's record …`) is likewise not yours to edit or remove; leave it exactly as found.

5. **Record the AI pass** on the **source record(s)** you drew the accepted facts from. That is the spec-defined home for `ai_passes:` (SPEC §14; a biography pass is provenance for which sourced facts it used): one entry `{date, by, task, note}` appended to each source's frontmatter list. Name the style in the note ("biography drafted (narrative) for [[p-…|Name]]"), matching the marker. A person record has no `ai_passes:` list, so record it on the source(s), not on the profile or research file.

6. **Flip the markers with the tool, only after he accepts.**
   ```
   fha confirm draft <p-id> --dry-run
   fha confirm draft <p-id>
   ```
   This turns `<!-- AI-DRAFT … -->` into `<!-- AI-ACCEPTED … (accepted 2026-07-01) -->`. The original date and model stay in the marker, so provenance is preserved. **You never hand-edit the marker.** If he wants changes first, revise the AI-DRAFT prose and offer it again; don't flip until he says yes. Once accepted, reindex and regenerate this person's draft queue. The profile cites those sources after acceptance, and the old draft-queue file would otherwise keep listing them as uncited:
   ```
   fha index
   fha views draft-queue <p-id>
   ```

7. **Lint.** In an archive whose `fha.yaml` says `links: obsidian`, first point the new links at their files with `fha normalize-links --write` (_STANDARD.md §11).
   ```
   fha lint
   ```
   Confirm no dangling links (E004) and report plainly ("I drafted three paragraphs on Thomas, each fact sourced. They're marked as my draft until you accept them.").

8. **Close with what went in and what is missing.** Tell him plainly which accepted facts the draft used, by source ("the 1880 census for his job and household, the marriage notice for the wedding"), and which it left out because they are still waiting for his review (offer `review-claims`). Then name what would make the life fuller, from the tools rather than from memory:
   ```
   fha metrics <p-id>        # the five checks: birth record, censuses, parents, marriage, death
   ```
   Turn each check that is not met into one plain sentence and offer a next step for it ("There's no death record yet. Want me to suggest where to look?"), which hands off to `research-next`. This is said in the conversation only: nothing about the gaps goes into the biography, and nothing is written as a question unless he asks one (SPEC §17b).

## Guardrails

- No fact from a `suggested` claim; no unverified `[[p-…]]`/`[[s-…]]` link; no edit below a GENERATED header; no overwrite of human text.
- The citation contract is the same in both styles. Narrative never invents scenes, dialogue, weather, or interior states; its color is cited claims or clearly flagged period context. A style choice changes the voice, never what counts as a fact.
- Layout is the same in both styles: flowing paragraphs, blank-line separated, one paragraph per line in the file (never hard-wrapped), never one sentence per line, and no dashes used as punctuation (use a comma, a period or parentheses). These are plain-text records a person reads directly in an editor. One sentence per line reads as a list of assertions rather than a life, and it is the clearest sign a block was machine-drafted.
- A connected fact counted as living stays unnamed in the prose.
- New prose stays AI-DRAFT until `fha confirm draft`; acceptance is the human's decision, not a hand-edit.
- Uncited prose reads as context or story, never as fact.
- **AI-DRAFT prose never publishes until accepted.** `fha site` and `fha wikitree` both exclude a draft block (the prose back to the previous marker or section heading, plus the marker itself) until the human accepts it (`fha confirm draft` → AI-ACCEPTED, which makes the prose publishable). The exclusion fails closed: an unmarked paragraph directly above your draft is withheld with it until acceptance. Keep the end-of-block marker tight against the block you wrote, and draft around human text rather than in the middle of it.

## Done when

- Drafting a bio in a session on `example-archive` pulls from `fha views draft-queue`, cites every factual sentence with a **verified** `[[s-…]]`, wraps new prose in `<!-- AI-DRAFT … -->` markers, and leaves any existing human text untouched.
- Every `[[p-…]]`/`[[s-…]]` resolves: post-run `fha lint` shows no **E004**.
- Acceptance flips markers via `fha confirm draft`, not by hand-editing.
- A session drafting in **each named style** keeps the identical citation density (every factual sentence cited) and the lint baseline. The AI-DRAFT marker and the AI-pass task both name the style used. With no session ask and no `fha.yaml` `biography:` block, the voice is `chronicle`.
- The drafted `## Biography` prose reads as flowing, blank-line-separated paragraphs, each paragraph on one line with no hard wraps and no dashes used as punctuation, with no run of consecutive lines each ending on a sentence boundary with its own `[[s-…]]`, in either named style.
- The session ends by naming the accepted facts the draft used and the `fha metrics` checks still unmet, each with an offered `research-next` lead, and none of that summary is written into the profile.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9).
