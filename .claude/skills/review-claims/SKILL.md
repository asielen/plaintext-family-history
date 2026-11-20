---
name: review-claims
description: >
  Run when the human says "review the census claims" / "review this source" / "let's go through what you
  drafted", or right after `process-source` or `mine-transcript` hands off. Walks one source's `suggested`
  claims (guided one-by-one, in numbered batches, or self-serve), shows each in plain language with its
  evidence context and its source file link, captures the human's accept/dispute/edit decision, and writes
  it with `fha claim` (grouped same-status decisions as one batch write). Closes with a reindex, an
  `fha xref` pass, a timeline/sources-index/draft-queue refresh for the people touched, and `fha lint`.
  This is the human gate: it never accepts a claim on the human's behalf.
---

# review-claims

Stage C of the pipeline, and the **only** path a drafted claim takes to `accepted`. The deterministic half is `fha claim`: it moves a claim's status and stamps `reviewed:`, one claim at a time or several c-ids in one status-only batch. This skill adds *presentation and capture*: it shows each suggested claim with its evidence so the human can decide quickly, and turns his stated decision into the right tool call. Every skill that ends in review (`process-source`, `mine-transcript`) hands off here. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"Review the census claims", "review this source", "let's go through the Hartley notes", or automatically as the last stage of `process-source` / `mine-transcript`. Always scoped to **one source at a time**, because a source's claims share evidence. The one exception: a GEDCOM-import source (`fha gedcom import`) can hold thousands of claims and is reviewed **person-by-person or family-by-family** ("review the Hartleys"), never front-to-back. Filter its claims to the people asked about and leave the rest for later sessions.

## The contract for this skill (state it before you start)

- **The human is the only gate to `accepted`.** The skill presents; the human decides. **Never** set `status: accepted` without an explicit decision the human stated *this session*: no accepting on his behalf, no inferring a yes from silence, no batch-accepting "the obvious ones." Grouping is **presentation and capture, never judgment**: a batched reply ("accept 1–4") states one decision per numbered claim, and a claim his reply doesn't cover stays `suggested`.
- **`accepted` is written only through `fha claim <c-id> [<c-id> …] --status accepted`,** which stamps `reviewed:` (lint E006 fails on an accepted claim with no `reviewed:` date). Directing that tool *is* his accept. Whether it carries one c-id or several, the decision behind each is his.
- **Translate, don't quiz** (_STANDARD.md §4): show each claim as a sentence with its evidence, not as a YAML blob. Never say "c-id" or "EDTF" to him without a plain gloss.

## Flow

1. **Locate the source, list its suggested claims, and hand him the evidence itself.**
   ```
   fha find <s-id>        # record path, asset files, claim counts by status
   ```
   (If the human named the source in words, such as "the 1880 census", resolve it with `fha find <text>` first, then confirm you've got the right one.) Then **relay the paths** so he can look at the actual evidence while you talk: the source record's path and each asset file's on-disk path, as clickable paths in plain words: "The census scan is at `documents/census/1880-census-hartley.jpg`. Open it alongside and we'll go through what it says." He should never have to review a claim about a document he can't see. Open the source `.md` and read its `## Claims` block; the ones with `status: suggested` are the backlog. A suggested claim that already carries a `reviewed:` date was parked in an earlier session ("checked, still open", SPEC §8.1). Say so when you reach it, so he knows he has seen it before.

2. **Offer the three review styles, let him choose** (AGENTS.md §"Review claims with the human"):
   - **Guided, one-by-one**: you walk each claim in turn (the default; best for a handful, or when he wants your read on the evidence).
   - **Batched**: you present the claims in **numbered groups of about five, grouped by person**, each claim as a one-line sentence plus its anchor, a confidence flag when the evidence is shaky, and its source link (step 3). He replies with one grouped decision ("accept 1–4, edit 5: the date was June 1923"). Every number is one claim, so each claim still gets an **individually stated human decision** with less typing. **Offer Batched proactively** whenever a source has more than about six suggested claims; one-by-one pacing on a forty-claim census wears him out.
   - **Self-serve skim**: you open the source file and let him skim the whole `## Claims` block himself, then tell you the decisions. Offer this for a long backlog or when he'd rather drive.

3. **Every claim shown, in any style, carries its evidence link.** Present, in plain language:
   - the claim as a **sentence** ("Thomas Hartley, occupation bookkeeper, Plains Junction Railroad, about 1880"),
   - for a birth, baptism, death, burial, marriage or divorce, **whose event it is**, by name and separately from who else it names: "this records the DEATH of Ethel Hartley; it also names her son Calvin Hartley (as a survivor)". This is where a record put on the wrong person (a mother's obituary filed under her living son) is caught, before it becomes fact,
   - for **any other claim naming several people** (a residence, an occupation, an event, a note) that does not already say whose fact it is, **ask him before he decides**: "This names Charles, Mary and Paul. Is it about one of them, a few of them, or all of them equally, like a party they all went to?" Write his answer as `roles:`: `subject: [p-…]` for the one(s) it is about and the others by their part (`spouse`, `child`, `witness`, a plain word), or `subject:` listing everyone when he says it belongs to all of them. Never pick "everyone" for him: an unroled son on his father's job reads as the son's job on his timeline (SPEC §8.3, lint W149),
   - its **evidence context**: the `anchor:` (the exact spot in the source: a page, a line, a timestamp) and, where the source is a transcript or note, the quoted span it was drawn from,
   - its **source link**: the anchored source file path (the asset file the claim's anchor points into when you can identify it, else the record `.md`; `fha find <c-id>` prints `source: <path>:<line>`), followed by the `[[s-id]]` token in parentheses. Path first, because that is what he clicks; the token is for search. Example: "`documents/census/1880-census-hartley.jpg`, page 2, line 31 (`[[s-fa1234567b]]`)".
   - the **source, date, and place** as written,
   - the **Mills fields** in plain terms when they matter ("this is *secondary* evidence, worked out from the age column rather than a birth record"), so a shaky inference reads as shaky.

   While you're reading a claim's `value:` aloud, also check whether the draft (yours or a hand-typed one) wandered into the archive's own bookkeeping instead of the family's story: which transcription pass produced a reading, who verified something and when, a first-person note about the research process (AGENTS.md §"Speculation and storytelling" → "Keep the archive's own paperwork out of the family's story"). That content belongs in `notes:`, not `value:`. An **edit** (below) moves it there before you accept, so it never lands on the Timeline a family member reads.

   **A claim whose evidence is a photo or scan that is not a source yet** (its `notes:` name a photos-root path, `fha lint` W137 lists it, or it says "a photograph shown in the interview"): before he accepts it, offer to make the photo a source with `fha process <path> --type photo` (add `--variants one` for a front and its back), so the claim can be drafted on that photo's own record and this one linked to it with `corroborates:` (SPEC §8.4). His no is fine; the claim stays reviewable.

   Then ask for his decision: **accept / dispute / edit / reject / park**, plus any claim he wants to *add* by hand that the draft missed.

4. **Write each decision with `fha claim` (preview, then apply).**
   - **A grouped same-status decision** ("accept 1–4", "reject all three of those") is written with the batch form, one preview for the whole group, then one apply:
     ```
     fha claim c-a c-b c-c --status accepted --dry-run
     fha claim c-a c-b c-c --status accepted
     ```
     (This stamps `reviewed:` on each. Every review status batches the same way, so batch-reject and batch-park are as legitimate as batch-accept.) The tool validates every id before writing anything, so a mistyped id refuses the whole batch cleanly.
   - **A single accept:**
     ```
     fha claim <c-id> --status accepted --dry-run
     fha claim <c-id> --status accepted
     ```
   - **Edit then accept**: correct a value or date in his words, translating to stored form yourself ("he says it was really June 1923" → `--date 1923-06`). A field edit is **always an individual `fha claim <c-id> …` call**. The tool refuses field flags on a batch by design, because a correction states a new fact about one particular claim. **Preview first**, so an edited value or date never lands stamped `reviewed:` before the human has seen exactly what will be written:
     ```
     fha claim <c-id> --status accepted --value "…" --date 1923-06 --dry-run
     fha claim <c-id> --status accepted --value "…" --date 1923-06
     ```
     **Removing a field** ("there was no place on that record", "drop that note") is `--clear FIELD`, repeatable, for the optional fields only: `place`, `place_text`, `date`, `information`, `evidence`, `anchor`, `notes`. Never pass an empty value (`--place-text ""`) to mean "remove"; the tool refuses it and points at `--clear`. `value`, `type`, `persons`, `status` and `confidence` are required on every claim and cannot be cleared; correct them with their own flag. Preview first, as always:
     ```
     fha claim <c-id> --clear place_text --dry-run
     fha claim <c-id> --clear place_text
     ```
   - **Dispute** (keep it, mark it contested): `--status disputed`, with the same `--dry-run` first, then apply.
   - **Reject** (wrong, but the trail is kept; never delete): `--status rejected`, preview, then apply.
   - **Park** (looked at, not settled yet): the claim stays `suggested` and gets today's `reviewed:` date, which reads "checked, still open" (SPEC §8.1). Preview first too:
     ```
     fha claim <c-id> --status suggested --dry-run
     fha claim <c-id> --status suggested
     ```
     If `fha claim` refuses `--status suggested` (tools older than SPEC 2.1), leave the claim exactly as it is and record the look with `fha source note <s-id> --text "reviewed <c-id> <date>: still open, <why>"`. Never use another status to stand in for it. When he parks a claim, **offer to record what would settle it, and never write it unasked**: "Want me to jot down what would settle this, so it comes back up when that record shows up?" Only on his explicit yes, write one **question entry** in SPEC §17b's form: an `## Q:` heading naming the question, then one `- ` line per field: `- origin: human` (he asked for it); `- status: open`; `- refs: [c-…]` (single brackets, bare IDs, comma-separated for several; the `[[c-…]]` double-bracket form is for prose links only, and in this structured field the question parser stops at the first `]`); and a `- context:` line with a dated sub-line noting what was parked and why. If he also voices a guess ("I think it's the other Margaret"), that is the same entry's **working answer** (SPEC §17a rung 2), not a separate object: add `- id:` (mint one with `fha id mint h`), `- guess:`, `- basis:` (his reason) and `- verify:` (what would settle it). Where it goes: the person's research file (`## Open Questions`), created the first time something is written to it (SPEC §16b), or `notes/questions.md` for a question that spans people. Never the profile's own sections; questions are read from those two places only. If a same-question entry already exists, **append a context line to it** rather than writing a twin. If he says no, write nothing; a parked claim with no note is a fine outcome too.
   - **A manual addition** he dictates is drafted into the source's `## Claims` as a new `status: suggested` claim. Write the **full claim shape** `process-source` uses, not just an id: a fresh `id:` (`fha id mint c`), `type:`, `persons:`, `value:`, `confidence:`, the Mills `information:`/`evidence:` fields, and an `anchor:` to where in the source it comes from. `confidence:` is required on every claim, so an id-only draft would fail lint. Then review it like the rest; it does **not** go straight to `accepted`.

5. **Close out the batch.** If a **`death` or `burial` claim was accepted** this session, the accept itself marked the person who died as no longer living (`living: unknown` → `false`), and `fha claim` prints one line per person ("Ada Hartley is now marked as no longer living; shared exports and the site may now include her"). **Report that line to him plainly**; do not ask again or run `set-living` yourself. The accept only changes the person the claim names `deceased:` (or the one person on a claim with no roles), and only when their flag read `unknown`. When `fha claim` **refused** the accept instead (SPEC §8.3), nothing was written. Put it to him and wait:
   - someone marked `living: true`: "The record says Thomas died, but he's marked as living. Is this really Thomas's death?" Show him the record. Only on his yes, accept it again with `--confirm-deceased` (that one step accepts it and marks Thomas no longer living). On a no, the record is about someone else (an obituary indexed under a survivor's name is the usual case), so look at its persons:, roles: and title together;
   - a claim that names several people without giving each a part: ask who the event is about and what the others were, write the `roles:` map (`deceased:` / `child:` / `spouse:` plus `parent:`, `informant:`, `witness:` …) by hand, then accept again. If he wants a death accepted but the person kept out of exports anyway, accept with `--keep-living` (re-accepting that claim later never changes the flag). If he moves a death he accepted earlier to rejected/disputed, or parks it, `fha claim` warns that the person is still marked as no longer living; the flag is never put back on its own. Ask him ("Should Ada go back to unknown, so she's kept out of exports again?") and only on his yes run `fha person set-living <p-id> unknown`.

   If a **relationship, marriage or birth claim was accepted**, the accept itself wrote the matching `relationships:` entry on each person it connects whose record already keeps that list, and `fha claim` prints one line per record ("Thomas Hartley's record now lists Margaret Cole as a parent"). Rejecting, disputing or superseding one, or parking an accepted one at suggested, takes the claim back out of those entries, and never deletes an entry. When `fha claim` says a record "still lists" someone "with no evidence now", tell him, and remove that entry only on his say-so. **Report those lines plainly and never hand-edit `relationships:` for them.** A record with no `relationships:` list is left alone on purpose. If `fha claim` warns that a record could not be edited safely, show him the entry it names and add it only on his yes; `fha lint --fix-applied --dry-run` then `fha lint --fix-applied` writes any entry still missing.
   ```
   fha index                      # full rebuild: if this pass minted new people/places (a
                                  # process-source / mine-transcript hand-off usually does), `--source`
                                  # reindexes only the source's claims, NOT new person/place records or
                                  # their aliases, so xref / find --related would run on stale person
                                  # data. Reserve `fha index --source <s-id>` for a status-only pass
                                  # that created no people or places.
   fha xref                       # surface new corroboration / contradiction across sources
   ```
   If `fha xref` proposes a link, present it plainly ("This now agrees with the 1871 marriage notice. Want to record that they back each other up?") and act on his pick:
   ```
   fha confirm xref <c-a> <c-b> --as corroborates --dry-run   # preview first (writes both sources)
   fha confirm xref <c-a> <c-b> --as corroborates             # or: --as contradicts
   ```
   A `--as contradicts` confirm automatically spawns the open question that keeps lint **E009** satisfied ("a `contradicts:` link with no open question"), so you don't hand-write that question. If you confirmed **any** xref link, **reindex again** before the view refresh below. `fha confirm xref` writes the `corroborates:`/`contradicts:` links into both sources but does not reindex, so `claim_links` (read by `fha find --related`, xref dedup, and the report's corroboration/discovery sections) would otherwise stay stale for the rest of the session:
   ```
   fha index
   ```

6. **Refresh the touched people's views quietly, without asking.** The session changed what the generated views show: an accepted claim leaves the timeline's "unreviewed" tail and joins the draft-queue's writing backlog. For every **curated** person named in a claim decided this session (stubs carry no timeline/draft-queue companion views, SPEC §16, so skip them for these two):
   ```
   fha views timeline <p-id>
   fha views draft-queue <p-id>
   ```
   For **every** person named in a claim decided this session, curated or still a stub, also refresh their own `## Sources` section (the per-person form works on a stub on purpose, so their sources list is already current the moment they later graduate, instead of needing a rebuild right after):
   ```
   fha views sources-index <p-id>   # the source list gains the just-reviewed source's evidence
   fha views summary <p-id>         # the Born/Died/Married/Parents/Children lines under the name
   ```
   Refresh only the people touched, never `fha views refresh` here: it regenerates *every* curated person's views and churns their dated GENERATED headers into git noise. (A successful view write exits `0` and prints a "run `fha index` when convenient" nudge; that is advice, not a warning.) If a `relationship` claim was accepted, also run `fha views brackets` (report mode) and relay anything it flags in plain words. Applying `--fix` renames folders and moves person files, so that stays the human's explicit call.

7. **Finish with the done-gate and report it plainly.**
   ```
   fha lint
   ```
   Translate the result: "All good. Three facts accepted, the census agrees with the marriage notice, and nothing is left flagged." If lint flags something, name the fix in plain words (_STANDARD.md §4) instead of pasting the code.

   In an archive kept in git, `fha lint --changed` is the quicker check when only this session's files matter: it reports on the files changed since the last commit and the records they link to (plus anything wrong with the scans and photos on disk, and everything when `fha.yaml` or `places/places.yaml` changed), which keeps a long-standing backlog of warnings elsewhere out of the answer. A full `fha lint` stays the check before a release and after `fha update-tools`.

   Then, with the index fresh from step 5, check for **two nudges, promotion and places. Each is capped at one per session, needs the same explicit yes, and is never repeated once declined this session.**

   **Promotion**, for anyone decided on this session who has earned a real page: is any such person flagged by the `fha views brackets` run above as a **direct-line stub**, or holding accepted claims at or over the promotion threshold (`fha.yaml`'s `promotion:` → `claims_threshold`, default 5 when the key is absent; read the file directly)? If so, say one plain nudge, worded for which kind of person this is. For a **direct-line** stub: "Frank S. Bradford has 9 accepted facts and no full page of his own yet. Want me to give him one and draft a short biography?" Only on his explicit yes, run `fha person promote <p-id>` and hand off to `write-biography`. For a **non-direct** person crossing the threshold, the same nudge offers the connections form instead: "Frank keeps turning up: 5 accepted facts, though he's not in your direct line. Want me to give him a page with the family's other connections and draft a short biography?" Only on his explicit yes, run `fha person promote <p-id> --into connections/` (SPEC §12.3, no numbering) before the same `write-biography` hand-off. Ask once per person per session, then let it rest.

   **Places.** A review pass that just accepted a batch of claims is when a place-text cluster crosses its threshold, so check for it here instead of leaving it for a session that may never come back to it. It reads the index step 5 just rebuilt, and it counts exactly the statuses this pass just moved: the detector groups `accepted` claims only, so what he accepted a moment ago is in the list and anything still `suggested` is not.
   ```
   fha places candidates   # ranked unlinked place-text clusters (default threshold 3), sorted largest first
   ```
   That threshold (3) is `fha places candidates`' own bar for listing a cluster at all, the same one `place-research` and report §6b use. This nudge asks a stricter question (is this cluster big enough to interrupt the close-out for?), and it asks it **only of a cluster this pass actually moved**: one whose printed `claim_ids` list holds at least one claim decided on in this session. That matches the promotion nudge's scope ("anyone decided on this session"), and it keeps the nudge from nagging. A session remembers nothing (`_STANDARD.md` §7), so a "not now" is gone by the next run, and a nudge keyed on the archive's *largest* cluster would ask again every session. A big cluster this pass never touched is still a real candidate; it belongs to report §6b and `place-research`. Of the clusters that qualify, take the **largest**, and act only when it is at **10 or more claims** (`process-source`'s same offer bar, its step 5). Below that, leave it as a candidate.

   Before offering, look up that cluster's **town name**. The offer is about to tell him the place isn't on the archive's list of places, so check that it isn't. Unlinked does not mean unregistered: the claims carry no `place:`, which says nothing about what `places.yaml` holds.
   ```
   fha find --json "San Diego" --kind place   # the town alone: --kind place matches name:/alt_names:
                                              # and never hierarchy:, so the cluster's full wording
                                              # returns [] even for a town already registered
   ```
   Search the cluster's own town and its obvious variant (drop a "City"/"Township" suffix, expand an abbreviation). A cluster prints under whichever wording is commonest, which may not be the one you searched first. `--kind place` reads the registry tables whole, so `[]` here is a real answer. If the archive holds sources with no searchable text, this command also prints the D14 coverage caveat on stderr; that caveat is about *text* search across the archive and says nothing about the registry or this result, so don't let it turn a clean `[]` into a hedge. Then make **one** offer, naming that one cluster only (not the whole list, the same one-thing-at-a-time restraint as the promotion nudge), and say which case it is:
   - **No hit** → "'San Diego, California' appears in 22 facts, and it isn't on the archive's list of places yet. Want me to add it, so those facts point to one place?"
   - **One hit whose `detail` (that place's `hierarchy:`) is the same town** → "'San Diego, California' turns up in 22 facts that aren't tied to the San Diego already on the archive's list of places. Want me to tie them to it?"
   - **Several hits, or one whose hierarchy is a different county or state** → put the candidates to him and let him pick, exactly as for an ambiguous person name. A bare town name is not an identification.

   **Only on his explicit yes**, write it the way `place-research` does (never hand-write `places.yaml`): merge into the place that is already there, else mint one, with the settlement as `--name` and the full string as `--hierarchy`. A second `l-id` for one town is the duplicate `fha places lint` reports as **PL002**.
   ```
   fha confirm place <c-id> <c-id> … --into <l-id> --dry-run                # already registered: merge
   fha confirm place <c-id> <c-id> … --name "…" --hierarchy "…" --dry-run   # new place
   fha confirm place <c-id> <c-id> … --name "…" --hierarchy "…"
   ```
   (The cluster's own `claim_ids` list, printed by `fha places candidates`, is the id list to pass. Show him the `--dry-run` before applying, because those claims reach past this session's source into every record holding one. A **minted** place gets a fresh random `l-…` on each run, so the real id is the one the **apply** run printed, never the dry-run's; a `--into` merge mints nothing and keeps the id you passed.) This write lands *after* the `fha lint` above and `fha confirm place` does not reindex, so a yes here means the close-out isn't finished. Reopen it, exactly as `place-research` step 4 does:
   ```
   fha index                # fold the new l-id and the relinked place: claims into the query surface
   fha places lint          # registry hygiene: orphan l-ids, duplicate names, dangling within: links
   fha lint                 # the done-gate again, now covering the last write
   ```
   (Left alone on purpose: a relinked claim's place shows as a clickable `[[l-…]]` in an affected person's timeline only after that person's next `fha views timeline <p-id>`.) Ask once per place per session, then let it rest, the same as promotion.

   **If he asks to settle all of them**, not just this one, two commands do it in one reviewed step each, and the close-out reopens the same way afterwards. `fha places link` lists every claim whose place text already fits a registered place (its first part names the place and the rest is in its hierarchy); on his yes, `fha places link --apply` writes them. `fha places candidates --register --dry-run` lists a new place for every cluster with none; on his yes to that list, `echo y | fha places candidates --register` writes it (it asks one `[y/N]`).

## Guardrails

- **Never** `--status accepted` without an explicit human decision recorded in the session. If he didn't say yes to *this* claim, it stays `suggested`. A batched reply counts exactly as far as its numbers reach: "accept 1–4" decides claims 1 through 4 and nothing else.
- Every accepted claim carries `reviewed:`. That is `fha claim`'s job; never hand-edit a status in the file.
- Every claim presented, in any style, carries its source link (the anchored file path, then the `[[s-id]]` token).
- **Rejected is not deleted.** Prefer `--status rejected`/`superseded` and keep the claim; the research trail matters (AGENTS.md §"Don'ts").
- A contradiction always ends with an open question (E009-clean). Let `fha confirm xref … --as contradicts` spawn it.
- The park offer, the promotion nudge, and the place nudge are **offers**: they write (a `## Q:` entry, with a working answer when he voices a guess) or run (`fha person promote`, `fha confirm place`) only on the human's explicit yes. No yes, no write: silence or a hedge is not a yes.
- The place nudge only considers a cluster **this pass moved** (its `claim_ids` hold a claim decided on this session), names at most **one** of them (the largest), and fires at most once per session, the same cap as promotion. It never lists every returned cluster, never picks the archive's biggest untouched one, and is never repeated once declined.
- The offer states the registry situation only after checking it: the town name goes through `fha find --json … --kind place` **before** the nudge is spoken, so "isn't on the list yet" is a checked fact and an already-registered town is offered as a link, not minted a second time.
- A place nudge answered yes **reopens the close-out** (`fha index`, `fha places lint`, `fha lint`), since it writes after the done-gate and `fha confirm place` doesn't reindex. A session never ends on that write. Read `fha places lint` there for what the write *added*; a pre-existing PL finding elsewhere in the registry is the archive's standing state and not this session's to chase.
- Record no separate `ai_passes:` entry here *unless* you drafted a new claim in this session (a manual addition you formatted). Plain acceptance of existing drafts is the human's pass, not the AI's.
- Any record ID you write into prose (a note, a spawned question, a story) is `[[ ]]`-wrapped, `[[id|Name]]` preferred; bare IDs belong only inside claims-block YAML fields and tool arguments (_STANDARD.md §11).
- Prose written into a note, a spawned question or a story keeps each paragraph on one line, with a blank line between paragraphs: never hard-wrapped at a column and never one sentence per line (_STANDARD.md §4).

## Done when

- Walking a source's suggested claims in a session on `example-archive` produces **one stated human decision per claim** and the matching writes: **one `fha claim` write per individual decision, one batch `fha claim c-a c-b … --status X` write per grouped same-status decision** (field edits always one call per claim), a reindex (full `fha index` when the pass minted new people, else `--source`), an `fha xref` pass, a `fha views timeline` + `draft-queue` refresh for each curated person touched, a `fha views sources-index` and `fha views summary` refresh for every person touched (curated or stub), and a final `fha lint`.
- **Every claim presented carried its source link** (the anchored file path first, the `[[s-id]]` token in parentheses) in whichever review style the session used.
- **No** claim reaches `accepted` without an explicit human decision in the transcript; every accepted claim carries a `reviewed:` date (post-run `fha lint` shows no **E006**).
- A contradiction surfaced by xref ends in `fha confirm xref … --as contradicts`, leaving the archive **E009**-clean.
- A parked claim stays `suggested` with a `reviewed:` date; no claim is written at any other "pending" status.
- The park offer (a SPEC §17b `## Q:` entry, with `id`/`guess`/`basis`/`verify` when he voiced a guess), the promotion nudge (`fha person promote` + `write-biography` hand-off), and the place nudge (`fha confirm place`, when the largest `fha places candidates` cluster holding a claim decided on this session is at or over 10 claims, and its town has been looked up first) fired **only on explicit yeses**; without one, nothing was written or run.
- A place nudge that *did* fire ends on a reopened close-out: `fha index`, `fha places lint` adding no new finding, and `fha lint` re-run over the registry and the relinked claims.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9).
