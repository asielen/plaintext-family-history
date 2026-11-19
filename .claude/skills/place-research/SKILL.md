---
name: place-research
description: >
  Run when the human says "fill in Suwałki's history" / "what was this town like then?" / "flesh out this
  place". Pulls the place's neighborhood, drafts dated `history:` entries and place notes (context prose,
  loose citations OK), links `[[l-…]]`/`[[p-…]]`, and proposes registry entries for `fha confirm place` to
  write. Never edits `places.yaml` coordinates without human confirmation.
---

# place-research

Places carry history (a town is renamed, a county forms, a parish splits), and that context makes the family story easier to follow. Place background is *context*, unlike a vital fact, so **loose citations are acceptable here** (AGENTS.md workflow note): a bare fact still wants a source, but the dated history of a town is background, not a claim. This skill drafts that history and proposes clean registry entries for the deterministic tool to write. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"Fill in Fairview's history", "what was this town called back then?", "flesh out this place", "should these three 'Fairview City' mentions be one place?"

## The contract for this skill (state it before you start)

- **Never edit `places.yaml` coordinates without explicit human confirmation** (AGENTS.md §"Don'ts"). Coordinates anchor a place's identity: you draft history and notes, and you don't move the pin.
- **Registry writes go through `fha confirm place`, not by hand.** Minting or merging an `l-id` and relinking claims is the deterministic tool's job.
- **Context prose reads as context.** Loose citations are fine for a town's background; a bare vital fact still needs a source. Write history so a reader sees it as background.
- **Respect privacy** on any person you cross-link into place notes.

## Flow

1. **Pull the place's neighborhood.**
   ```
   fha find --related <l-id>          # the place's world: claims naming it, people associated,
                                      #   micro-places (within: children), existing history:
   fha find <l-id>                    # the registry entry + every reference
   ```
   Read what's already in `places.yaml` for this `l-id` (its `hierarchy`, `alt_names`, existing `history:` entries) so you extend it and never overwrite it.

2. **Draft dated `history:` entries and place notes.** Add to the place's `history:` list the dated name and jurisdiction changes, each as a period plus what it was called or governed by:
   ```yaml
   history:
     - {period: "1858/1861", hierarchy: "Fairview, Breton Co., Kansas Territory, USA"}
     - {period: "1861/..",   hierarchy: "Fairview, Breton County, Kansas, USA"}
   ```
   Translate the human's plain words into EDTF periods yourself ("became a state in 1861" → the interval boundary). Write context prose (what the town was, the railroad boom, the parish it belonged to) into the place's `notes:`, clearly as background. Cross-link `[[l-…]]` for related or parent places and `[[p-…]]` where a person belongs in the story. A loose citation (a county history, a general reference) is acceptable for context; a hard fact still wants its `[[s-…]]`.

3. **Where a recurring unlinked place-text needs a registry entry, propose it and let the tool write it.**
   ```
   fha places candidates              # recurring unlinked place_text clusters (≥3) + GPS clusters
   ```
   When a cluster (e.g. three claims all saying "Fairview City, Breton Co.") needs its own `l-id`, or should merge into an existing place, present it plainly and act on the human's pick:
   ```
   fha confirm place <c-id> <c-id> --name "Fairview" --hierarchy "Fairview, Breton County, Kansas, USA" \
     --dry-run
   # or merge the cluster into an existing place:
   fha confirm place <c-id> <c-id> --into <l-id> --dry-run
   ```
   This mints (or merges) the `l-id` in `places.yaml` **and** relinks the named claims' `place:`, so the cluster stops showing up as unlinked. Preview first, then apply. You never hand-write the registry entry or hand-relink the claims.

4. **Reindex, then lint both surfaces.** `fha confirm place` and the hand-edited `history:` change the files but not the index, so reindex first. Otherwise `fha places lint` (and `fha places candidates` / `fha report` §6b) read a stale index: the new `l-id`, the relinked `place:` claims and the new `history:` entries are missing from it, so the cluster still shows as unlinked and the lint checks nothing about the write.
   ```
   fha index                          # fold the new l-id, relinked claims, and history: into the query surface
   fha lint
   fha places lint                    # registry hygiene: orphan l-ids, duplicates, dangling within: links
   ```
   Report plainly: "I added Fairview's history from territory to statehood and joined the three loose 'Fairview City' census mentions into one place. I left the map coordinates as they were." (Cosmetic: a relinked claim's place shows as a clickable `[[l-…]]` in an affected person's timeline only after that person's next `fha views timeline <p-id>`. The label text is unchanged until then, and a later `review-claims` or `write-biography` pass picks it up; regenerate now only if you want the link live.)

## Guardrails

- **No coordinate in `places.yaml` is changed without an explicit human confirmation.**
- Registry entries are minted or merged via `fha confirm place`, never by hand-editing `places.yaml`.
- Dated `history:` and place notes read as context; a bare fact still carries a source.
- Extend existing registry entries; never overwrite a human's `hierarchy`/`alt_names`/`notes`.
- Any record ID written into place notes or history prose is `[[ ]]`-wrapped, `[[ID|Name]]` preferred; bare IDs only in structured registry fields and tool arguments (_STANDARD.md §11).

## Done when

- Researching a place in a session on `example-archive` drafts dated `history:` with `[[l-…]]` links and proposes any registry write via `fha confirm place`.
- **No** coordinate in `places.yaml` is changed without an explicit human confirmation.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9), and `fha places lint` stays clean.
