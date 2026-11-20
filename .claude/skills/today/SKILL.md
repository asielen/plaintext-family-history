---
name: today
description: >
  Run at session start, or when the human asks "what should I work on?" / "where do things stand?" (some
  harnesses surface this as a /today shortcut). Reads `fha report`, narrates it discoveries-first in plain
  language, then offers one concrete next action, and once per run, if §0 has a real win to show, asks
  whether to log it. Writes only what the human confirms while acting on the briefing: a win via
  `fha confirm discovery`, or an answer to a narrated connection ("yes, they were neighbors" / "no, stop
  suggesting that pair") via `fha confirm cooccur` / `fha confirm dismiss`.
---

# today

The workbench's opening screen. One command, `fha report`, refreshes the archive's state and shows where the research stands. This skill *reads* that report and turns it into a short, plain-spoken briefing that leads with what's new and ends with one thing to do next. It is the smallest real skill and the one new skills copy: invoke a tool, render it for a non-technical reader, write nothing without an explicit human decision. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Session start, or "what should I work on?", "what's new?", "where do things stand?", or the harness's shortcut for this skill where one exists. It is safe to run anytime.

## The contract for this skill

- **Reads by default.** The skill computes nothing and writes nothing on its own. `fha report` does its own refresh (incremental photoindex, index rebuild, lint) as step one, so you do **not** re-run those. One narrow exception lives inside that refresh: if the archive still has an un-migrated `.cache/cooccur_dismissed.json`, §8's `fha cooccur` call moves it to `notes/cooccur_dismissed.json` the first time it reads it. That moves a decision the human already made, and the report names it in §8 whenever it happens.
- **The only writes it can make** are the human acting on the briefing, each through its deterministic verb and never by hand: "yes, log that win" → `fha confirm discovery`; "yes, connect those two" → `fha confirm cooccur` (minted `suggested` unless his answer is the review; see step 6); "stop suggesting that pair" → `fha confirm dismiss`. Each is echoed with `--dry-run` first and applied only on his confirmation.
- **Speak the report in plain words** (_STANDARD.md §4). No lint codes, no c-ids, no "W101" said to the human without a plain gloss.

## Flow

1. **Run the report.**
   ```
   fha report
   ```
   It refreshes state and prints sections 0–8 (TOOLING.md §15a, the research feed). Read all of it; you narrate it, you don't recompute it. If the human wants the complete picture instead of the changes since last session, `fha report --full` ignores the since-last-session snapshot.

2. **Narrate discoveries first (§0).** Lead with **Discoveries since last session**: questions answered, contradictions resolved, a claim that just gained its first independent second source, a profile that just became vitals-complete, a confirmed connection. Say these as wins, in plain words: *"Since last time: Margaret's birth year is backed by a second source. The 1871 marriage notice agrees with the census."*

   **Then offer to log one, once.** When §0 lists at least one real item (not the "No discoveries since last session" placeholder), close the narration with a direct offer: *"Want me to log any of these as a discovery?"* §0 is recomputed from this session's changes every run (TOOLING §15a), so a win narrated here and not logged is gone once the next report advances the snapshot. `notes/discoveries.md` (step 5) is the only place a win outlives the session, and it is what `fha site`'s discoveries page and home-page teaser show. Make the offer once, right after narrating §0, then move on to step 2b whatever the answer. A "no", or no answer at all, means log nothing and don't ask again this run (see Guardrails).

2b. **Then the week in the family (§0b), in a sentence or two.** §0b lists the family's births, marriages, deaths and arrivals that fall today and in the six days after, from accepted claims. Say the one or two that stand out, warmly and plainly: *"And this week: James Robert Bradford and Carol Anne Simmons were married on 14 June 1979 in Fairview, 47 years ago."* Say only what the line says: the date, what happened, who, the place if one is given, how long ago. Add no detail, mood or meaning the line does not carry ("a summer baby", "a happy day"), and don't inflate a quiet week. When §0b says nothing is on file, skip it or say so in one clause. If he wants more, that is the `on-this-day` skill.

3. **Summarize the working state, briefly and in plain language.** Pull the few things that matter and skip the rest:
   - **§1 Review queue:** suggested claims waiting on the human, oldest source first. *"Three sources have drafted facts waiting for your yes or no. The oldest is the 1880 census."* A GEDCOM-import source can carry thousands of suggested claims. Present that count as normal: it is reviewed gradually, person by person or family by family, never front to back, and none of it is urgent.
   - **§3 Vitals gaps:** people missing a birth, marriage or death. *"Thomas Hartley still has no death record."* (This is the example archive's one known gap; don't alarm him with it.)
   - **§8 Possible connections:** co-occurrence leads, clearly labeled as leads and never as facts.
   - Mention these only when they hold something to act on, and don't read empty sections aloud:
     - §1b, claims to look at again: parked claims and thin facts to revisit when a new record turns up. Present them as "someday" leads, not chores.
     - §2, new since last time.
     - §5b, answerable questions.
     - §6b, place candidates. A large one can win step 4's one next action; see step 4.
     - §7, working answers and draft queues.
     - §7b, promotion candidates: people who have earned a page of their own, either a direct-line ancestor who is still a stub or a stub who keeps turning up in accepted facts. Present them as someday leads: *"Frank keeps showing up: nine accepted facts and still no page of his own."* The step itself is `fha person promote`, run only on the human's say-so. A direct-line person files into their couple folder; anyone else files flat in `people/connections/` via `--into connections/` (SPEC §12.3). The report prints the exact command either way.

4. **Offer exactly one next action, after comparing §6b with §1.** The best next step is usually a `review-claims` session on the oldest backlog, or `process-source` on the inbox. Before offering that, compare the two counts the report printed. If §6b's largest **unlinked** place-text cluster **carries more claims than §1's oldest backlog has suggested ones** (or §1 is empty), that cluster wins the slot: *"'San Diego, California' turns up in 22 claims with no place attached. Want to sort that out before the next review?"* On a yes, hand off to `place-research` at its **step 3**, the unlinked-cluster path, since a cluster has no `l-id` yet for step 1's neighborhood pull. This skill still writes nothing itself (see Guardrails).

   Say **unlinked**, not "unregistered". §6b reports claims carrying no `place:`, which is different from a town missing from `places.yaml`, and this skill runs no registry lookup to tell the two apart; `place-research` does that after the hand-off. Compare like with like: a §6b place line counts a cluster's **`accepted`** claims (a `suggested` draft never enters that section), and §6b's *GPS* lines count photos, which is a different lead and not part of this comparison. There is no fixed number to clear: a large review backlog still wins over a middling place cluster, and an archive with no place candidates gets the ordinary offer.

   Whichever wins, name it plainly and offer to start it: *"Want to start with the 1880 census review? I'll walk you through each drafted fact one at a time."* Then hand off to that skill if he says yes.

5. **Log a win when he says yes**, either to step 2's offer or to a request that comes up later in the session, and only on his explicit go-ahead:
   ```
   fha confirm discovery "Margaret Cole's 1849 birth year corroborated by the 1871 marriage notice" \
     --refs s-ea61339378,p-cd795c61e0 --dry-run
   ```
   Show him the previewed entry, then run it without `--dry-run`. This appends a dated line (with `[[s-…]]`/`[[p-…]]` refs) to `notes/discoveries.md`, the log of research wins (TOOLING §15a). `fha report` never reads that file back, because §0 recomputes its own changes every run (step 2). `fha site`'s discoveries page and home-page teaser read it the next time the site is built.

6. **Act on a connection only when the human answers one.** When one of the briefing's §8 leads draws a reaction ("yes, they were neighbors", "those two were friends", "no, ignore that pair"), finish the job with him instead of assuming:
   - Fetch the pair's shared sources with a read-only `fha cooccur` run (the report shows the count, not the s-ids). If there is more than one, confirm with him which source supports the link.
   - Map his words to the subtype yourself (_STANDARD.md §4): "neighbors" → `neighbor`, "friends" → `friend`, "they knew each other / worked together at…" → `associate`. If none fits, ask one short plain question.
   - **Connect:**
     ```
     fha confirm cooccur p-aaaa p-bbbb --source s-xxxx --subtype neighbor --dry-run
     ```
     Show him the previewed claim, then run it without `--dry-run`. By default it is minted `suggested` and joins the review queue like any drafted fact. Only when his answer *is* the review (a flat "yes, they were neighbors, record it as fact") add `--accept`, which treats the confirm as the review and stamps the review date; say so when you echo the command. A hedged answer ("probably", "I think so") always mints `suggested`.
   - **Dismiss:**
     ```
     fha confirm dismiss p-aaaa p-bbbb --dry-run
     ```
     then apply. Tell him the pair is remembered and won't be proposed again; nothing is deleted.
   - Either way, say in one plain sentence where it landed ("that's a drafted fact on the 1880 census now, and it'll come up in your next review session" / "that pair won't come up again"). The next `fha report` refresh picks it up, so no manual reindex is needed, same as the discovery write.

## Guardrails

- **Never** move a claim to `accepted`, draft a claim, or edit a record; this skill reads and narrates. Acting on an item is a hand-off to the skill that owns it (`review-claims`, `process-source`, `research-next`, …). The exceptions are the two in steps 5 and 6: a discovery he asks to log, and a §8 connection candidate he answers, which this skill handles with `fha confirm cooccur` / `fha confirm dismiss`.
- **Never** confirm or dismiss a pair the human didn't explicitly rule on. Silence, a change of subject, or "interesting" is not a decision. Never use `--accept` for a hedged answer.
- The step 2 discovery offer is made **once per run**, right after narrating §0. Don't make it once per item, and don't repeat it if he already declined or ignored it earlier in the conversation. Silence on the offer is treated like silence on a §8 candidate: no decision, so nothing is written.
- **Never** hand-edit `notes/discoveries.md`; the only write path is `fha confirm discovery`, and only on an explicit human decision.
- Don't recompute what `fha report` already computed. The report refreshes the index and runs lint, so skip a separate `fha index` / `fha lint`. `fha xref` belongs to `review-claims`, not this briefing.
- Give a briefing with one clear next step instead of reading out every section.
- Any record ID in the text of a confirmed discovery is `[[ ]]`-wrapped, `[[id|Name]]` preferred. The structured `--refs` list takes bare IDs, but IDs *inside the sentence* follow _STANDARD.md §11.
- The text of a confirmed discovery keeps each paragraph on one line, with a blank line between paragraphs: never hard-wrapped at a column and never one sentence per line (_STANDARD.md §4).

## Done when

- In a session on `example-archive`, invoking this skill (e.g. "what should I work on?") runs `fha report`, narrates sections 0–8 **discoveries first**, and offers one concrete next action in plain language.
- §0b is narrated right after discoveries in one or two sentences that say nothing its lines do not say. An empty week is skipped or named in one clause.
- When §0 lists at least one real item, the discoveries narration ends with one offer to log it (*"Want me to log any of these as a discovery?"*), made once per run and not repeated once he has answered. When §0 is empty (*"No discoveries since last session."*), no offer is made this run.
- It makes **zero** archive writes unless the human confirms one, and a confirmed discovery lands via `fha confirm discovery`, never by hand-editing `notes/discoveries.md`. The one exception is the idempotent §8 housekeeping move above, which needs no confirmation because it decides nothing, and which the report names when it happens.
- When the human answers a narrated connection ("yes, neighbors" / "no, drop it"), the skill echoes the exact `fha confirm cooccur`/`dismiss` command with `--dry-run`, applies it only on his confirmation, and mints `suggested` unless he explicitly treated the answer as the review.
- When §6b's largest unlinked **place-text** cluster outsizes §1's oldest review backlog (or the backlog is empty), that cluster takes the one next-action slot and the offer hands off to `place-research` without writing anything. The comparison uses the counts this run's `fha report` printed: a cluster's claim count against the backlog's suggested-claim count, never a GPS line's photo count and never a number hardcoded for one archive. The offer calls the cluster **unlinked**, which is what §6b reports; whether the town is registered is a registry question this skill never asks.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9), so the skill added nothing new.
