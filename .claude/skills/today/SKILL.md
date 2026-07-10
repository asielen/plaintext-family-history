---
name: today
description: >
  Run at session start, or when the human asks "what should I work on?" / "where do things stand?" (some
  harnesses surface this as a /today shortcut). Reads `fha report`, narrates it discoveries-first in plain
  language, then offers one concrete next action. Read-only — it writes nothing on its own; a confirmed win
  lands only via `fha confirm discovery` on the human's explicit say-so.
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
  those.
- **The one write it can make** is a discovery log entry, and only when the human explicitly says "yes,
  log that win" — through `fha confirm discovery`, never by hand-editing `notes/discoveries.md`.
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
     have drafted facts waiting for your yes/no — the oldest is the 1880 census."*
   - **§3 Vitals gaps** — people missing a birth/marriage/death. *"Thomas Hartley still has no death
     record."* (This is the archive's one known gap; don't alarm him with it.)
   - **§8 Possible connections** — co-occurrence leads, clearly flagged as *leads, never facts*.
   - Mention §2 (new since last time), §5b (answerable questions), §6b (place candidates), §7
     (hypotheses / draft queues) only when they hold something worth acting on. Don't read empty
     sections aloud.

4. **Offer exactly one next action.** End by naming the single best next step and offering to start it —
   usually a `review-claims` session on the oldest backlog, or `process-source` on the inbox:
   *"Want to start with the 1880 census review? I'll walk you through each drafted fact one at a time."*
   Then hand off to that skill if he says yes.

5. **Log a win only if asked.** If the human points at a §0 discovery and says to record it, and only
   then:
   ```
   fha confirm discovery "Margaret Cole's 1849 birth year corroborated by the 1871 marriage notice" \
     --refs S-ea61339378,P-cd795c61e0 --dry-run
   ```
   Show him the previewed entry, then run it without `--dry-run`. This appends a dated line (with
   `[[S-…]]`/`[[P-…]]` refs) to `notes/discoveries.md` — the durable log the report's §0 reads next time.

## Guardrails

- **Never** move a claim to `accepted`, draft a claim, or edit a record — this skill only reads and
  narrates. Any acting-on-an-item is a hand-off to the skill that owns it (`review-claims`,
  `process-source`, `research-next`, …).
- **Never** hand-edit `notes/discoveries.md`; the only write path is `fha confirm discovery`, and only on
  an explicit human decision.
- Don't recompute what `fha report` already computed — the report refreshes the index and runs lint, so no
  separate `fha index` / `fha lint`; and `today` is read-only, so no `fha xref` either (that's
  `review-claims`' job, not this briefing's).
- Speak the report, don't dump it: a briefing with one clear next step, not a wall of sections.

## Done when

- In a session on `example-archive`, invoking this skill (e.g. "what should I work on?") runs `fha report`, narrates
  sections 0–8 **discoveries-first**, and offers one concrete next action in plain language.
- It makes **zero** archive writes unless the human confirms one; a confirmed discovery lands via
  `fha confirm discovery`, never by hand-editing `notes/discoveries.md`.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9) — the skill introduced nothing new.
