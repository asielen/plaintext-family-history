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

`fha views draft-queue` already computes the writing backlog — a person's `accepted` claims whose source
isn't yet cited in the profile. This skill adds the *drafting*: turning those claims into prose that reads
like a careful cousin wrote it and cites every factual sentence. A bio that reads well but cites loosely is
worse than none, so citation discipline is the product. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"Draft Margaret's bio", "write up Thomas", "extend this profile now that the census is in." Always scoped
to one person.

## Voice: two styles, one citation contract

The bio's *facts* are fixed by the contract below; its *voice* is the human's choice between two named
styles (owner decision 2026-07-22 — SPEC §16 fixes structure and citation density, both style-invariant,
so voice is a skills-layer setting, not a spec matter):

- **`chronicle`** (the default) — era-by-era and fact-forward: plain, warm, terse. A careful cousin's
  record of what happened, in order. The voice this skill has always had.
- **`narrative`** — story-driven: the same cited facts woven with scene-setting and period context
  ("Kansas in 1875 was still sod-house country…"). Every factual sentence still cites its `[[S-…]]`;
  the extra texture comes ONLY from cited claims or clearly-flagged general context — **never invented
  scenes, dialogue, weather, or interior states** (AGENTS.md §"Speculation and storytelling": an
  invented detail in a family story becomes family truth in one generation).

**Resolving which to use** — first match wins:

1. **The human's ask this session.** Translate his words, never quiz him (_STANDARD.md §4): "tell it
   like a story" → narrative; "just the facts" → chronicle. An ask that fits neither maps to the
   nearest style, honoring his phrasing inside that style's rules.
2. **The archive default** — a `biography:` block in `fha.yaml` (read the file directly, the same way
   the site tools read their `site:` block; no tool call needed):
   ```yaml
   biography:
     style: narrative      # chronicle | narrative
   ```
3. **Neither?** If the profile already carries drafted prose, match its register — the AI-DRAFT /
   AI-ACCEPTED markers record which style wrote it (step 4). Otherwise ask ONE plain question —
   "straight chronicle, or more of a story?" — and default to **chronicle** if he waves it off.

Extending an existing biography keeps the register already on the page unless the human asks to change
it — a profile half chronicle, half narrative reads as two authors fighting.

## The contract for this skill (state it before you start)

- **Facts only from `accepted` claims.** A `suggested` claim is not yet a fact — it never becomes a
  biographical sentence. If the queue is thin because claims are still unreviewed, say so and offer a
  `review-claims` session first.
- **Cite every factual sentence.** Summary block: one citation per line. Body: all relevant citations.
  Anything uncited must read as *story/context*, not as asserted fact (AGENTS.md §"Write a biography").
- **Link only verified IDs.** Every `[[P-…]]`/`[[S-…]]` is checked to exist before you write it (`fha
  find`) — no dangling links (lint E004).
- **AI-DRAFT until accepted.** All new prose is wrapped in `<!-- AI-DRAFT … -->`; the human accepts via
  `fha confirm draft`, never a hand-edit.
- **Never overwrite human text; never edit below a GENERATED header.** Draft *around* existing prose.
- **Respect privacy** (AGENTS.md §"The contract" 6): don't surface a `living`/`restricted` person or
  detail into prose destined for export.

## Flow

1. **Pull the backlog and the facts.**
   ```
   fha views draft-queue <P-id>      # the uncited-accepted-claim writing backlog
   fha find <P-id>                    # the person's record, claims, and existing profile
   ```
   If the target person is still a **stub** (`tier: stub`, or their record sits in `people/stubs/` —
   the draft-queue view refuses them either way), offer `fha person promote <P-id>` first: a biography
   belongs on a curated profile, in its couple folder for a direct-line person or flat in
   `people/connections/` for anyone else (SPEC §12.3). Run it only on the human's yes (preview with
   `--dry-run`), then `fha index` and continue. If promote refuses because the person is **not on the
   direct line**, offer the connections form instead — `fha person promote <P-id> --into connections/`
   — same tier flip and body backfill, no numbering; only if the human declines that too do the
   facts stay in claims and `## Stories` for now, and you stop here rather than drafting a bio the
   views can't carry.
   Read the person `.md`: note the existing biography prose (human and any prior AI-DRAFT) so you draft
   around it, not over it. The draft queue tells you which sources' accepted facts still need prose.

2. **Draft the prose — facts only from accepted claims, each sentence cited.**
   - Write in the resolved style's voice (see "Voice" above) — always a careful cousin, never a
     machine; narrative adds cited or clearly-flagged texture, never invention.
   - **Every factual sentence carries its source** as a `[[S-…]]` link: *"He worked as a bookkeeper for
     the Plains Junction Railroad by 1880 [[S-4f5f215e60]]."*
   - **That is a citation rule, not a layout rule — write paragraphs, not lines.** Flowing paragraphs,
     hard-wrapped at ~85 columns, a blank line between them; several cited sentences share a paragraph
     whenever they share a subject, era, or episode (typically 3-6 sentences) — never one sentence per
     line. The Biography section is already "chaptered by era/place" (SPEC §16); the paragraph is that
     chaptering's working unit, not a wrapper around a single citation.
   - The **summary block** (the vitals line at the top) takes one citation per fact-line; the **body**
     takes all relevant citations.
   - Anything you can't cite to an accepted claim must read explicitly as context or story ("Family
     recollection holds that …"), never as a stated fact. When in doubt, cut it or move it to Stories.

3. **Verify every link before writing it.**
   ```
   fha find <P-id>        # confirm each person link resolves
   fha find <S-id>        # confirm each source link resolves
   ```
   Prefer the `[[ID|display]]` form (`[[P-cd795c61e0|Margaret A. Cole]]`); a name-link (`[[Margaret
   Cole]]`) resolves through the alias layer but pin to the ID when the name could be shared. Never write
   an ID you haven't confirmed exists.

4. **Wrap all new prose in AI-DRAFT markers, drafting around human text.**
   ```markdown
   {new biographical prose, every factual sentence cited}

   <!-- AI-DRAFT 2026-07-01 {your-model-id} - biography drafted (chronicle) from accepted census + marriage claims -->
   ```
   Name the style in the marker note — that is how a later session matches the register without
   re-asking (_STANDARD.md §7: sessions are an interface, not memory). Place the marker at the end of
   the block you wrote. Leave any existing human-written paragraph exactly
   as it is — add your paragraphs before/after it, never edit inside it. Never touch a `<!-- GENERATED …
   -->` section (the timeline and draft-queue companion files are regenerated by their own tools, never
   hand-edited). **The profile you are drafting into also carries its own `## Sources` section** (SPEC
   §16, #76) — bounded by `<!-- GENERATED-BEGIN sources-index … -->` / `<!-- GENERATED-END sources-index
   -->` markers right there in the same file, above `## Biography`. That region is exactly like a
   GENERATED-headed companion file in spirit — machine-owned, rewritten only by `fha views sources-index
   <P-id>` — it just lives inside the file you are editing instead of beside it. Draft only into `##
   Biography` / `## Stories` / `## Research Notes` (or `## Friends & Family` by hand); never write into,
   through, or around that region, and never mistake its bracketed source list for prose you could cite
   from or extend. The purpose block at the very top of the body (`> **This person's record …`) is
   likewise not yours to edit or remove — leave it exactly as found.

5. **Record the AI pass** on the **source record(s)** you drew the accepted facts from — that is the
   spec-defined home for `## AI Passes` (SPEC §14; a biography pass is provenance for which sourced facts
   it used), per the shape ({date, model, harness, task, outputs, human_reviewed}). Name the style in
   the task text ("biography drafted (narrative) from …"), matching the marker. There is no
   person-level `## AI Passes` block, so record it on the source(s), not on the profile or research file.

6. **On the human's acceptance — and only then — flip the markers with the tool.**
   ```
   fha confirm draft <P-id> --dry-run
   fha confirm draft <P-id>
   ```
   This turns `<!-- AI-DRAFT … -->` into `<!-- AI-ACCEPTED … (accepted 2026-07-01) -->` — the original
   date/model stay in the marker, so provenance is preserved. **You never hand-edit the marker.** If he
   wants changes first, revise the AI-DRAFT prose and re-offer; don't flip until he says yes.
   Once accepted, reindex and regenerate this person's draft queue — the profile now cites those sources,
   so the pre-edit draft-queue file otherwise keeps showing them as an uncited writing backlog:
   ```
   fha index
   fha views draft-queue <P-id>
   ```

7. **Lint.**
   ```
   fha lint
   ```
   Confirm no dangling links (E004) and report plainly ("drafted three paragraphs on Thomas, each fact
   sourced; they're marked as my draft until you accept them").

## Guardrails

- No fact from a `suggested` claim; no unverified `[[P-…]]`/`[[S-…]]` link; no edit below a GENERATED
  header; no overwrite of human text.
- **The citation contract is style-invariant.** Narrative style never invents scenes, dialogue,
  weather, or interior states — its color is cited claims or clearly-flagged period context, nothing
  else. A style choice changes the voice, never what counts as a fact.
- **Layout is style-invariant too — paragraphs, not lines.** Chronicle and narrative both read as
  flowing paragraphs, blank-line separated, hard-wrapped at ~85 columns — never one sentence per line.
  These are plain-text records a human reads directly in an editor, forever; semantic linefeeds read as
  a list of assertions rather than a life, and they are the clearest tell that a block was drafted, not
  written.
- New prose stays AI-DRAFT until `fha confirm draft`; acceptance is the human's gesture, not a hand-edit.
- Uncited prose reads as context/story, never as fact.
- **AI-DRAFT prose never publishes until accepted.** `fha site` and `fha wikitree` both exclude a
  draft block — the prose back to the previous marker or section heading, plus the marker itself —
  until the human accepts it (`fha confirm draft` → AI-ACCEPTED, which makes the prose publishable).
  The exclusion is fail-closed: an unmarked paragraph sitting directly above your draft is withheld
  with it until acceptance, so keep the end-of-block marker tight against the block you wrote and
  draft around human text, never mid-run.

## Done when

- Drafting a bio in a session on `example-archive` pulls from `fha views draft-queue`, cites every factual
  sentence with a **verified** `[[S-…]]`, wraps new prose in `<!-- AI-DRAFT … -->` markers, and leaves any
  existing human text untouched.
- Every `[[P-…]]`/`[[S-…]]` resolves — post-run `fha lint` shows no **E004**.
- Acceptance flips markers via `fha confirm draft`, not by hand-editing.
- A session drafting in **each named style** keeps the identical citation density (every factual
  sentence cited) and the lint baseline; the AI-DRAFT marker and the AI-pass task both name the style
  used, and with no session ask and no `fha.yaml` `biography:` block the voice is `chronicle` —
  byte-for-byte the skill's old default behavior.
- The drafted `## Biography` prose reads as flowing, blank-line-separated paragraphs, hard-wrapped at
  ~85 columns — no run of consecutive lines each ending on a sentence boundary with its own `[[S-…]]`,
  in either named style.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9).
