---
name: today
description: >
  Run at session start, or when the human asks "what should I work on?" / "where do things stand?" (some
  harnesses surface this as a /today shortcut). Reads `fha report`, narrates it discoveries-first in plain
  language, then offers one concrete next action. Read-only — it writes nothing on its own; the only writes
  are the human's explicit say-so acting on the briefing: a win via `fha confirm discovery`, or a narrated
  connection candidate answered ("yes, they were neighbors" / "no, stop suggesting that pair") via
  `fha confirm cooccur` / `fha confirm dismiss`.
---

# today

The workbench "login screen." One command — `fha report` — refreshes the archive's state and tells you
where the research stands; this skill *reads* that report and turns it into a short, plain-spoken briefing
that leads with what's new and ends with one thing to do next. It is the smallest real skill and the
reference every other one copies: invoke a tool, render it for a non-technical reader, write nothing
without an explicit human decision. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

Session start, or "what should I work on?", "what's new?", "where do things stand?" — or the harness's
shortcut for this skill, where one exists. It is safe to run anytime — it only reads.

## The contract for this skill

- **Read-only by default.** The skill computes nothing and writes nothing on its own. `fha report` does
  its own refresh (incremental photoindex + index rebuild + lint) as step one — you do **not** re-run
  those. One narrow exception lives inside that refresh (#48): if the archive still has an
  un-migrated `.cache/cooccur_dismissed.json`, §8's `fha cooccur` call carries it forward to its
  durable home (`notes/cooccur_dismissed.json`) the first time it reads it — a housekeeping move of a
  decision the human already made, not a new one, and the report always names it in §8 when it
  happens, so it is never a surprise.
- **The only writes it can make** are the human acting on the briefing, each through its deterministic
  verb and never by hand: "yes, log that win" → `fha confirm discovery`; "yes, connect those two" →
  `fha confirm cooccur` (minted `suggested` unless his answer is the review — see step 6); "stop
  suggesting that pair" → `fha confirm dismiss`. Every one is echoed with `--dry-run` first and applied
  only on his confirmation.
- **Voice is the product here** (_STANDARD.md §4): translate the report's machinery into a cousin's
  briefing. No lint codes, no C-ids, no "W101" spoken at the human without a plain gloss.

## Flow

1. **Run the report.**
   ```
   fha report
   ```
   It refreshes state and prints sections 0–8 (TOOLING.md §15a — the research feed). Read the whole thing; you
   narrate it, you don't recompute it. (`fha report --full` ignores the since-last-session snapshot if the
   human wants the complete picture, not just the diff.)

2. **Narrate discoveries-first (§0).** The report is a research *narrative* before it is a chore list, so
   lead with **Discoveries since last session** — questions answered, contradictions resolved, a claim
   that just gained its first independent second source, a profile that just became vitals-complete, a
   confirmed connection. Say these as wins, in plain words: *"Since last time: Margaret's birth year is
   now backed by a second source — the 1871 marriage notice lines up with the census."*

3. **Summarize the working state, briefly and in plain language.** Pull the few things that matter and
   skip the rest:
   - **§1 Review queue** — suggested claims waiting on the human, oldest source first. *"Three sources
     have drafted facts waiting for your yes/no — the oldest is the 1880 census."* A GEDCOM-import
     source can carry thousands of suggested claims — frame that count as normal, not alarming: it is
     reviewed person-by-person or family-by-family, gradually, never front-to-back, and nothing about
     it is urgent.
   - **§3 Vitals gaps** — people missing a birth/marriage/death. *"Thomas Hartley still has no death
     record."* (This is the archive's one known gap; don't alarm him with it.)
   - **§8 Possible connections** — co-occurrence leads, clearly flagged as *leads, never facts*.
   - Mention §1b (worth a second look — parked claims and thin facts to revisit when a new record
     surfaces; frame as "someday" leads, never chores), §2 (new since last time), §5b (answerable
     questions), §6b (place candidates — a large one here is a genuine contender for step 4's one next
     action, not just a passing mention; see step 4), §7 (hypotheses / draft queues), §7b (promotion
     candidates — people who've earned a real page: a direct-line ancestor still a stub, or a stub that
     keeps turning up in accepted facts; frame as someday-leads — *"Frank keeps showing up — nine accepted
     facts and still a stub"*. For a direct-line person the graduation is `fha person promote`, run
     only on the human's say-so; a non-direct claim-heavy stub just stays a stub for now — the report
     says so itself) only when they hold something worth acting on. Don't read empty sections aloud.

4. **Offer exactly one next action — weigh §6b honestly instead of defaulting past it.** The single best
   next step is usually a `review-claims` session on the oldest backlog, or `process-source` on the inbox
   — but don't reach for that by default before comparing what the report actually shows: if §6b's
   largest **unlinked** place-text cluster **carries more claims than §1's oldest backlog has suggested
   ones** (or §1 is empty), that cluster is the bigger opportunity sitting in front of you this
   run, and it wins the slot instead — *"'San Diego, California' turns up in 22 claims with no place link
   — want to sort that out before the next review?"*, handing off to `place-research` on a
   yes — at its **step 3**, the unlinked-cluster arm, since a cluster has no `L-id` yet for step 1's
   neighborhood pull — same as any other next-action hand-off (this skill still writes nothing itself —
   see Guardrails). Say **unlinked**, not "unregistered": §6b reports claims carrying no `place:`, which
   is not the same as a town missing from `places.yaml`, and this skill runs no registry lookup to tell
   the two apart — `place-research` does that on the other side of the hand-off.
   Compare like with like: a §6b place line counts a cluster's **`accepted`/`needs-review`** claims (a
   `suggested` draft never enters that section at all), and §6b's *GPS* lines count photos, not claims —
   they are a different lead and not this comparison's business.
   This is a comparison of the two counts the briefing already printed, not a fixed number to clear —
   a genuinely large review backlog still rightly beats a middling place cluster, and an archive with no
   place candidates at all just gets the ordinary offer. Whichever wins, name it plainly and offer to
   start it: *"Want to start with the 1880 census review? I'll walk you through each drafted fact one at a
   time."* Then hand off to that skill if he says yes.

5. **Log a win only if asked.** If the human points at a §0 discovery and says to record it, and only
   then:
   ```
   fha confirm discovery "Margaret Cole's 1849 birth year corroborated by the 1871 marriage notice" \
     --refs S-ea61339378,P-cd795c61e0 --dry-run
   ```
   Show him the previewed entry, then run it without `--dry-run`. This appends a dated line (with
   `[[S-…]]`/`[[P-…]]` refs) to `notes/discoveries.md` — the durable log the report's §0 reads next time.

6. **Act on a connection only when the human answers one.** When the briefing's §8 leads draw a
   reaction — "yes, they were neighbors", "those two were friends", "no, ignore that pair" —
   complete it, never assume it:
   - Fetch the pair's shared sources with a read-only `fha cooccur` run (the report shows the
     count, not the S-ids) and confirm with the human which source supports the bond if there
     is more than one.
   - Map his words to the subtype yourself (_STANDARD.md §4 — translate, don't quiz):
     "neighbors" → `neighbor`, "friends" → `friend`, "they knew each other / worked together
     at…" → `associate`. If none fits, one short plain question.
   - **Connect:**
     ```
     fha confirm cooccur P-aaaa P-bbbb --source S-xxxx --subtype neighbor --dry-run
     ```
     Show him the previewed claim, then run it without `--dry-run`. Minted `suggested` by
     default — it joins the review queue like any drafted fact. Only when his answer *is* the
     review — a flat "yes, they were neighbors, record it as fact" — add `--accept` (the tool's
     treat-the-confirm-as-the-review arm, which stamps the review date); say that's what
     you're doing when you echo the command. A hedged answer ("probably", "I think so") always
     mints `suggested`.
   - **Dismiss:**
     ```
     fha confirm dismiss P-aaaa P-bbbb --dry-run
     ```
     then apply. Tell him it's remembered, not deleted — the pair just won't be proposed again.
   - Either way, one plain sentence on where it landed ("that's now a drafted fact on the 1880
     census — it'll come up in your next review session" / "that pair won't come up again"),
     and the next `fha report` refresh picks it up — no manual reindex here, same as the
     discovery write.

## Guardrails

- **Never** move a claim to `accepted`, draft a claim, or edit a record — this skill only reads and
  narrates. Any acting-on-an-item is a hand-off to the skill that owns it (`review-claims`,
  `process-source`, `research-next`, …) — **except a §8 connection candidate the human answers: this
  skill owns `fha confirm cooccur` / `fha confirm dismiss`** (steps 5–6 are the whole exception list).
- **Never** confirm or dismiss a pair the human didn't explicitly rule on — silence, a topic change, or
  "interesting" is not a decision. Never use `--accept` for a hedged answer.
- **Never** hand-edit `notes/discoveries.md`; the only write path is `fha confirm discovery`, and only on
  an explicit human decision.
- Don't recompute what `fha report` already computed — the report refreshes the index and runs lint, so no
  separate `fha index` / `fha lint`; and `today` is read-only, so no `fha xref` either (that's
  `review-claims`' job, not this briefing's).
- Speak the report, don't dump it: a briefing with one clear next step, not a wall of sections.
- Any record ID in the text of a confirmed discovery is `[[ ]]`-wrapped, `[[ID|Name]]` preferred — the
  structured `--refs` list takes bare IDs, but IDs *inside the sentence* follow _STANDARD.md §11.

## Done when

- In a session on `example-archive`, invoking this skill (e.g. "what should I work on?") runs `fha report`, narrates
  sections 0–8 **discoveries-first**, and offers one concrete next action in plain language.
- It makes **zero** archive writes unless the human confirms one — a confirmed discovery lands via
  `fha confirm discovery`, never by hand-editing `notes/discoveries.md` — except the one narrow,
  idempotent §8 housekeeping move above (#48), which needs no confirmation because it decides
  nothing, and which the report always names when it happens.
- When the human answers a narrated connection ("yes, neighbors" / "no, drop it"), the skill echoes
  the exact `fha confirm cooccur`/`dismiss` command with `--dry-run`, applies only on his confirmation,
  and mints `suggested` unless he explicitly treated the answer as the review.
- When §6b's largest unlinked **place-text** cluster outsizes §1's oldest review backlog (or the
  backlog is empty), it — not the review queue — wins the one next-action slot, and the offer hands off to
  `place-research` rather than writing anything itself; the comparison is between whatever counts this
  run's `fha report` actually printed — a cluster's claim count against the backlog's suggested-claim
  count, never a GPS line's photo count and never a number hardcoded for one archive. The offer says the
  cluster is **unlinked**, which is what §6b reports; whether the town is registered at all is a registry
  question this read-only skill never asks.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9) — the skill introduced nothing new.
