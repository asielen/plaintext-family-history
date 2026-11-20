---
name: setup-interview
description: >
  Run once, right after `fha install`, when the human wants to do the setup interview ("let's do the
  setup interview", "I just installed this, where do I start?", "who are you and how do you want to be
  numbered?"), or whenever a session notices `root_person` is still unset in a freshly installed
  archive's `fha.yaml`. Asks who he is and how he wants the tree numbered (`root_generation: self |
  children`), then his immediate family (parents, spouse(s), children) as firsthand testimony from the
  one informant the archive is guaranteed to have. Mints his own person record directly (name, `sex:`,
  `living:`), writes one `source_class: authored` interview source, and drafts `relationship`/`marriage`
  claims from it naming everyone with explicit `roles:`, all `status: suggested`; hands off to
  `review-claims` so his own review pass is still the gate. The interview proposes; it never
  self-accepts. Also writes, on his yes, the family part of `START_HERE.md` (who the family is, who the
  archive is centred on) for a future reader with no tools. Before the family questions, also makes one
  silent, one-time check for a place-registration backlog already on file (an archive with imported or
  migrated history can reach this interview with zero registered places and a real backlog) and, only
  past a 20-claim bar, offers once to register the biggest clusters right then.
---

# setup-interview

A brand-new archive knows nothing about the one family it is guaranteed to have authoritative information on: the researcher's own. This skill asks him, once, at setup, who he is, how he wants the tree numbered, and who his immediate family is. It writes what he says the way the archive writes everything else: a real source with a real citation, and ordinary `suggested` claims that pass through the same review gate as any other evidence. It orchestrates existing `fha` verbs only; nothing here is a new tool. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Right after `fha install`, once; `fha install`'s own "Next steps" points here. Triggers: "let's do the setup interview", "I just set this up, where do I start?", or a session noticing `root_person:` is still unset (commented out, as the template ships it) in a freshly installed archive's `fha.yaml`.

**Normally once per archive.** If `root_person` is already set when this starts, the interview (or an equivalent by-hand setup) has already happened. Say so plainly, name who it points at (`fha find <the current root_person p-id>`), and ask whether he wants to add something that was missed (another child he forgot to mention, say) or is here for something else. Never silently re-run the whole flow or mint a second record for someone already on file. Adding one missed person later is a light re-entry into step 3 alone, not a repeat of steps 1-2.

## The contract for this skill

- **Every `relationship`/`marriage` claim this skill drafts is `status: suggested`.** The informant is uniquely authoritative (he cannot be wrong about who his own parents, spouse, or children are), but the human's own review pass is still the only path to `accepted` (_STANDARD.md §3). The interview proposes; it never self-accepts.
- **The person records it mints (himself and his named family) are not claims** and carry no `status:` field. Minting a stub, setting `sex:`, and setting `living:` are the same direct, confirmed-in-conversation writes `fha person new` / `set-sex` / `set-living` make everywhere else in this archive. They are gated by asking him plainly before writing, not by `fha claim`.
- **Record the AI pass** as an entry in the interview source's `ai_passes:` frontmatter list before handing off (_STANDARD.md §3).
- **Respect privacy** (_STANDARD.md §3): his immediate family is almost always living people, and `living:` defaults to `unknown` on a fresh stub. This skill sets it explicitly (true/false/unknown) for everyone it touches, because his own household is the case he can state with certainty. `sex:` gets the same treatment for the same reason, for everyone this skill touches and not only the newly minted: a `p-id` resolved via `fha find` never ran `fha person new`, so it never got `--sex` either, and needs its own `fha person set-sex` call.
- **`root_person`/`root_generation` are written straight into `fha.yaml`**, which is documented as a plain, hand-editable file (SPEC §12.4). Unlike `places.yaml` or the index, nothing here needs a dedicated verb; it needs only a shown preview before the write.

## Flow

### 0. One-time check: is there already a place backlog?

Before the family questions below, run one cheap, silent check, whether this is a brand-new archive or a return visit (`root_person` already set, "Normally once per archive" above). Most archives reach this interview with nothing to find here: a fresh install has no claims of any kind yet. But an archive that already carries imported or migrated material (a `fha gedcom import` someone ran before the personal interview, a batch of sources already processed) can have zero registered places and a real backlog of unlinked `place_text`, with nobody ever offered the one pass that would fix it. Migration mode is a sanctioned thing to do before this interview (AGENTS.md: "prefer [`fha gedcom import`] over hand-migrating one"), so this ordering is not rare.

`fha gedcom import` mints every claim it drafts at `status: suggested`, never `accepted`. A plain `fha places candidates` call excludes `suggested` claims (everywhere else in this archive, an unreviewed claim might still be disputed away), so it would see nothing from a fresh import and miss the backlog this check exists to catch. This one call is the sanctioned exception: pass `--include-suggested` to widen the scan for this one-time, informational purpose only. It is not a report of confirmed data, so counting still-`suggested` claims here is correct in a way it would not be for `review-claims`, `fha report`, or any other everyday view.

```
fha index                             # cheap; keeps the check below from reading a stale or missing cache
fha places candidates --include-suggested
```

- **`places/places.yaml` already has at least one `- id:` entry, or every cluster `fha places candidates` finds is below 20 claims** → say nothing about this and move straight to step 1. The 20-claim bar is deliberately higher than the 3-claim bar `fha places candidates` lists from. It is the same bar `fha report` uses for its lead-in escalation notice, where a cluster that large counts as an oversight. A smaller cluster is a normal, ongoing `review-claims`/`process-source` nudge and is not worth interrupting the family interview for.
- **The registry is empty AND at least one cluster is at or past 20 claims** → offer now: *"Before we get to your family: your records mention '\<biggest cluster's label>' \<N> times, and the archive has no list of places yet. Want me to add the biggest one or two now? It takes a minute, and the facts in each group get tied to that one place."* Make one offer, once, and write only on an explicit yes. "Not now" or silence means drop it and move straight to step 1; don't raise it again this session.
  - **On yes:** ask how many clusters to work through (all of them, just the top one or two, or "however many are easy") rather than assuming; some archives have one dominant cluster, others have a dozen modest ones. Register each chosen cluster exactly the way `process-source`/`place-research` do, never by hand-writing `places.yaml`:
    ```
    fha confirm place <c-id> <c-id> … --name "<label>" --dry-run
    fha confirm place <c-id> <c-id> … --name "<label>"
    ```
    The cluster's own `claim_ids`, printed by `fha places candidates`, are the ids to pass, and only those claims get linked; its majority `label` is the proposed `--name`. Work biggest-cluster-first (the list's own sort). After the chosen clusters are registered:
    ```
    fha index                 # the write reaches past this session's own claims: reindex before continuing
    fha places lint            # registry hygiene: orphan l-ids, duplicate names
    ```
  - **On no, or silence:** say nothing more about it this session and move straight to step 1. `fha places candidates`, `review-claims`'s own place nudge, and `fha report`'s §6b/lead-in escalation all still cover it later.

### 1. Who are you?

Ask his full name, and in the same breath: *"And for the tree's numbering: are you male, female, intersex, or would you rather I leave that blank?"* (`sex:` is what the Ahnentafel derivation reads to place a parent in the father or mother slot. Missing on the owner himself, it quietly throws off the whole tree's numbering.) A rough birth year is welcome but optional.

- **Resolve first, never assume a blank slate:** run `fha find "<his name>"`. A session may be resuming, or he may already have a stub from other work. A clear match → confirm it's him and reuse that `p-id` for every step below. Several candidates → show them and let him pick. No match → mint on his confirmation:
  ```
  fha person new "<Full Name>" --sex <M|F|intersex|unknown> --birth "<rough year, if given>" --dry-run
  fha person new "<Full Name>" --sex <M|F|intersex|unknown> --birth "<rough year, if given>"
  ```
  (`--surname` overrides the filename's surname split when the automatic split would get it wrong: Spanish double surnames, particles, surname-first conventions.) If `fha person new` refuses because the name **already has a record** (status `duplicate-name`, exit 3, nothing minted), that record is most likely him: show it (`fha find <p-id>`) and reuse it on his yes. Add `--force` only once he has said it is someone else, never because the refusal mentions the flag.
- **Set `sex:`/`living:`/a given birth year explicitly on the resolved `p-id`, whether it was just minted above or reused from a clear match.** A fresh mint took `--sex`/`--birth` at `fha person new` time, but a reused `p-id` never ran that command, so neither reached it. Run these unconditionally so the reuse branch isn't left without `sex:`. He is in the room, so `living: true` needs no "are you alive" question.
  ```
  fha person set-sex <p-id> <M|F|intersex|unknown>
  fha person set-living <p-id> true
  fha person estimate <p-id> --birth "<rough year, if given>"
  ```
  (Skip the `estimate` line if he gave no year; it requires at least one of `--birth`/`--death`/`--birth-place`/`--death-place`.)

### 2. How do you want to be numbered?

Ask in plain words, using the owner's own framing verbatim: *"Should you be position #1 in your family tree, or should that spot belong to your children, together, one generation in from you?"* If he has no children on record (yet, or ever), `self` (the default) is the simple right answer: he anchors the tree as position #1. If he *does* have children in the archive, `children` is usually what he wants. Without it, a `root_person` with a child on record numbers every ancestor one generation high, without any visible sign (SPEC §12.2, W127). Pointing `root_person` at one of his own children instead does not work: with more than one child, one becomes #1 arbitrarily and the rest go unnumbered, and it has no answer at all for a childless researcher. `root_generation: children` anchors the archive at *him* while still numbering *his own* parents, grandparents and so on correctly, and it works even before any child is on record. Either way is a legitimate, permanent choice, and `root_generation` is a plain `fha.yaml` line he can change later if he's not sure now.

This setting does **not** number anyone outside his own line: it numbers `root_person`'s *own* ancestor line only. A spouse's parents are never assigned an Ahnentafel position by this tool, under either setting, and this interview does not ask about them either: step 3 below covers only *his* parents, spouse(s), and children. If a spouse's parents are already on file (as stubs from other work, say), they stay stubs after this pass; tying them to her with a sourced claim is a separate, later research session.

- "Just me" / "position 1" → `root_generation: self`, the default; **do not write the key**, matching `fha.yaml`'s own convention of leaving defaults unset.
- "My kids" / "one generation in" → write `root_generation: children`.
- Either way, `root_person: <his p-id>` gets written.

Show him the exact two lines before writing them (this is a plain text-file edit, previewed the same way any other write here is), then write them into `fha.yaml` near the top, where the template already explains both keys in comments (`archive-template/fha.yaml`'s `# root_person:` / `# root_generation:` block). Don't put them down in the `roots:`/`photos_ignore:` configuration further below, which never mentions either key:
```yaml
root_person: p-xxxxxxxxxx   # <His Name>, set from the setup interview, <date>
root_generation: children   # only if he chose that
```

### 3. Immediate family

Ask, in plain sentences, and accept "I don't know" or "skip that one" at any point (_STANDARD.md §5): his **parents**, his **spouse(s)** (past or present; none is a fine answer), and his **children** (none is a fine answer). For each person: name, sex (for the same numbering reason as step 1; ask, don't assume), a rough birth year if he has one, and whether they're still living. If how he describes a parent isn't a straightforward biological tie ("she raised me but isn't my birth mother", "my dad adopted me at three"), translate that into the right `subtype:` (`adoptive`, `step`, `foster`, `social`, …, SPEC §8.2) rather than defaulting silently to `biological`.

1. **Resolve every name first**, exactly as in step 1: `fha find "<name>"` before minting anything. Several candidates go to him as a pick, the same as an ambiguous name anywhere else in this archive.
2. **Summarize the whole list back once** ("So: your parents are Richard and Linda Sample, your wife is Jane, and you have two kids, Emily and Michael. Everyone is still living except your dad, who passed away in 2015. Is that right?") and mint on that one confirmation, not a separate yes/no per person for a short list:
   ```
   fha person new "<Name>" [--surname <override>] --sex <M|F|intersex|unknown> --birth "<rough>" --dry-run
   fha person new "<Name>" [--surname <override>] --sex <M|F|intersex|unknown> --birth "<rough>"
   ```
   A `duplicate-name` refusal (exit 3, nothing minted) names the record that already carries that name. Treat it as a step-1 match: show it and ask, and use that `p-id` if it is the same person. The real exception is a **father and son** (or mother and daughter) who share a name, such as "Roy Dodson" and his son "Roy Dodson": only once he has confirmed they are two people, mint the second with `--force`, and give the son his suffix in the name if the family uses one ("Roy Dodson Jr" files beside his father and needs no `--force` at all). Then, **for every person on the list, newly minted here or already found in step 1's resolve pass**, set `sex:`/`living:`/a given birth year explicitly. A resolved match never ran `fha person new`, so it never got `--sex`/`--birth`; this closes the same gap step 1 closes for the researcher himself:
   ```
   fha person set-sex <p-id> <M|F|intersex|unknown>
   fha person set-living <p-id> true|false|unknown
   fha person estimate <p-id> --birth "<rough, if given>"
   ```
   (Skip the `estimate` line for anyone he gave no year for.)
3. **Write up what he told you** as a plain dated note. This is the evidence file, not a hint wrapper, so it is a bare `.md`, not a `*.notes.md` sidecar. Organize it under short headings (Who I am / My parents / My spouse / My children) so the claim-drafting step below can cite a section by name:
   ```
   inbox/setup-interview-<slug of his name>.md
   ```
4. **Process it like any other source**, then correct the frontmatter `fha process` cannot know on its own (it defaults every source to `source_class: original`, which is wrong here):
   ```
   fha process inbox/setup-interview-<slug>.md --type interview --title "Setup interview - <His Name>" --date <today> --dry-run
   fha process inbox/setup-interview-<slug>.md --type interview --title "Setup interview - <His Name>" --date <today>
   ```
   Then hand-edit the scaffolded record's frontmatter:
   - `source_class: authored` (he is the informant *and* the record is written up from his own account, not a captured original; SPEC §8.5).
   - `repository:` naming him as the informant (e.g. `"<His Name> (the researcher, self-reported at archive setup)"`).
   - `citation:`: a real sentence, not the bare title (who, what, when, how it was gathered).
   - `people:`: every person named this session, `[[p-id|Name]]` per SPEC §14's link-valued form.
5. **Draft the claims directly into the source's `## Claims` block.** Mint one batch of ids, then write the full shape by hand, exactly as `process-source` drafts any claim. There is no `fha claim new --type relationship`: it refuses that type outright, since a `roles:` map is too structured for a single-claim CLI mint. This is ordinary Stage-B judgment, not a missing tool:
   ```
   fha id mint c -n <count>
   ```
   - **Parentage → `type: relationship`, never `type: birth`.** A birth claim's second person is as often an informant or a doctor as a parent; only an explicit `roles:` map says who is what, and asking him directly is how this skill gets one. One claim per child (including himself, as the child of his own parents), naming every parent he gave together:
     ```yaml
     - value: <Child>'s parents are <Parent A> and <Parent B>
       id: c-xxxxxxxxxx
       type: relationship
       subtype: biological   # or adoptive/step/foster/social, per what he actually described
       persons:                # one per line, the name beside the code (SPEC §8.7)
         - "[[p-child|Child Name]]"
         - "[[p-parentA|Parent A Name]]"
         - "[[p-parentB|Parent B Name]]"
       roles:
         child: p-child
         parent: [p-parentA, p-parentB]
       status: suggested
       confidence: high       # medium/low if he hedged: translate the hedge, don't default it away
       information: primary
       evidence: direct
       anchor: "'My parents' section"   # or whichever heading it came from
       notes: >
         Self-reported by <His Name> during the setup interview.
     ```
   - **A spousal bond → `type: marriage`** (SPEC's vital-significance type for a couple, scoped by `roles: spouse:`, not `type: relationship`), one per spouse he named:
     ```yaml
     - value: <Him> and <Spouse> are married
       id: c-xxxxxxxxxx
       type: marriage
       persons:
         - "[[p-him|His Name]]"
         - "[[p-spouse|Spouse Name]]"
       roles:
         spouse: [p-him, p-spouse]
       status: suggested
       confidence: high
       information: primary
       evidence: direct
       anchor: "'My spouse' section"
       notes: >
         Self-reported by <His Name> during the setup interview.
     ```
   - `information: primary` / `evidence: direct` are not the generic interview default (TOOLING.md defaults interview claims to *low* confidence, because most interview content is hearsay about other people). They are the deliberate call for *this* narrow case: a person's own firsthand statement about who his own parents, spouse and children are is about as strong as non-documentary evidence gets. Keep `confidence: high` unless he actually hedges ("I think", "as far as I know"); then translate the hedge to `medium`/`low`, the same rule review-claims applies to any other claim.
   - Anything he says that doesn't map to a claim (how he met his spouse, a story about a parent) goes to `## Stories` or `## Notes`, the same routing rule as `process-source` step 7.
6. **Record the AI pass:**
   ```yaml
   ai_passes:
     - {date: <today>, by: {your-model-id}, task: claims,
        note: "setup-interview relationship claims from the researcher's own account"}
   ```

### 4. Write the family part of `START_HERE.md`

`START_HERE.md`, at the archive root, is the page someone opens in fifty years with no tools: a grandchild who finds the folder. `fha install` puts it there with its family part left blank, between the `<!-- FAMILY` and `<!-- /FAMILY -->` comment lines. Fill that part from what he has just told you:

1. **Draft two to four plain sentences**: whose family this is; who the archive is centred on (his name, and that he is the `root_person` in `fha.yaml`); the surnames and places it covers, as far as he has said; and who keeps it and since when. Write names as text, not as `[[p-…]]` links: the reader this page is for has no tools to follow a link with. Ask him for the surnames and places if steps 1-3 did not bring them up; "I'm not sure yet" is a fine answer, and the sentence is left out.
2. **Show him the exact text** and write it only on his yes, replacing the `*Not written yet.*` paragraph between the two comment lines and nothing else in the file.
3. **If the part already holds his own words** (anything other than the `*Not written yet.*` paragraph), never replace them: show what is there, and offer only to add a sentence he approves. If the file is missing (an archive installed before the page existed), the next `fha update-tools` delivers it; say so, and leave this step for then rather than writing the whole page by hand.

The page is his file from then on: `fha update-tools` never overwrites it, and he may rewrite the family part whenever he likes.

### 5. Hand off to the gate

Say plainly what happens next: *"That's everything. I've drafted N facts from what you told me, but they're only proposals until you say yes to each. Let's go through them."* Then hand off to [`review-claims`](../review-claims/SKILL.md) for this source, exactly as `process-source` does at its own Stage C. Don't duplicate its close-out here: the reindex, `fha xref`, the view refresh, and `fha lint` all belong to that hand-off.

## Guardrails

- Parentage is written **only** as a `relationship` claim with an explicit `roles: {child:, parent: […]}` map, never a `birth` claim.
- A spousal bond is written as a `type: marriage` claim with `roles: {spouse: […]}`, the SPEC-correct type for a couple.
- Every relationship/marriage claim drafted here is `status: suggested`; this skill **never** writes `status: accepted`, not even on facts the informant cannot be wrong about. That judgment belongs to `review-claims`, on his explicit word, the same as every other claim in this archive.
- Every named person is resolved against the index (`fha find`) before minting, so no existing stub or curated record gets a silent duplicate.
- `living:` is set explicitly for every person this skill touches, not left at the `unknown` default; his own household is the case he can state with certainty.
- `sex:` is set explicitly for every person this skill touches too, via its own `fha person set-sex` call, not only for the newly minted. A `p-id` resolved through `fha find` never ran `fha person new`, so a mint-time `--sex` never reaches it.
- A given birth year gets the same treatment, via `fha person estimate --birth` on the resolved `p-id`, for the same reason.
- `root_person`/`root_generation` are hand-edited into `fha.yaml` (a plain, documented-editable file), never into `places.yaml` or `.cache/`, which stay off-limits to hand-editing.
- `root_generation: children` is written only when he actually chooses it; the unset default (`self`) is left unwritten, matching the template's own convention.
- Any record ID written into prose (the source's `## Notes`, the hand-off sentence) is `[[ ]]`-wrapped, `[[id|Name]]` preferred; bare IDs only inside the claims-block YAML, frontmatter lists, and tool arguments (_STANDARD.md §11).
- Runs once per archive in the ordinary case; if `root_person` is already set, this skill confirms with him before minting or writing anything else.
- The step 0 place-backlog offer fires at most **once per run**, only past the 20-claim bar, only on an explicit yes, and is never repeated once declined or ignored in the same session, the same discipline `process-source`'s own place-registration offer uses. A registered cluster is written **only** via `fha confirm place`, exactly as `process-source`/`place-research` write one, never by hand-editing `places.yaml`.
- Prose written into the source's `## Notes` or the family part of `START_HERE.md` keeps each paragraph on one line, with a blank line between paragraphs: never hard-wrapped at a column and never one sentence per line (_STANDARD.md §4).

## Done when

- Running the interview in a session against a freshly `fha install`-ed archive (or an equivalent scratch archive built from `archive-template/`) produces: the researcher's own person record with `sex:` and `living: true` set; `root_person` (and, when he chose it, `root_generation: children`) written into `fha.yaml`; a person record per named family member with `sex:`/`living:` set; one `source_type: interview`, `source_class: authored` source whose `people:` links everyone named; a `relationship` claim per parent-child edge (never a `birth` claim) and a `marriage` claim per spouse, each `status: suggested` with `information: primary`, `evidence: direct`, and an `anchor:`; the AI pass recorded in `ai_passes:`; the family part of `START_HERE.md` written in his approved words (or left as it was, when he had already written it); and a hand-off into `review-claims`.
- Nothing reaches `accepted` without the human's own pass through `review-claims`.
- A loosely answered question ("I don't really know my dad's exact birth year", "skip my mom's side for now") degrades gracefully: a skipped fact is omitted, with no stall or refusal.
- On an archive that already carries a 20-plus-claim unlinked place-text cluster and zero registered places (e.g. a `fha gedcom import` run before this interview), step 0 makes exactly one offer to register the biggest cluster(s), before the family questions begin; on yes, the chosen clusters are registered via `fha confirm place` and `fha places lint` afterward shows no new orphan/duplicate findings from it. On a fresh archive with no such backlog (the ordinary case), step 0 is silent and says nothing to him about it.
- Run against a fresh scratch archive, `fha lint` afterward shows only the expected backlog: `W102` for the newly suggested claims, `W119` for any now-visible direct-line ancestors still filed as stubs, and (only if `root_generation` was left at `self` while a child was named) the `W127` anchor note. Nothing unexpected, and no error.
- `fha lint --root example-archive` is unaffected (`_STANDARD.md` §9 baseline); this skill is exercised against a separate scratch archive, never against `example-archive/` itself.
