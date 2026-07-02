---
name: research-next
description: >
  Run when the human asks "where should I look for X?" / "what's next on the Hartleys?" / "what should I
  research?". Checks the research log FIRST, then combines open questions, vitals gaps, and open
  hypotheses with historical context (which record sets exist for the era/place, where they're held) into
  concrete, ranked research leads. May draft hypotheses (`origin: agent`) into research files — leads and
  hypotheses, never claims. Logs any executed search back to the research log.
---

# research-next

The archive already knows what it doesn't know: open questions, vitals gaps (W101), open hypotheses. This
skill turns those into *concrete* leads — which record set, which repository, which search terms — steered
by historical context. The research log is the guardrail against wasted effort: never propose a search
already run unless its nil has aged past the re-run horizon. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"Where should I look for Thomas's death record?", "what's next on the Hartleys?", "what should I research
now?", "any leads on Margaret's parents?"

## The contract for this skill (state it before you start)

- **Leads and hypotheses only — never claims.** A claim requires a source by definition; this skill has no
  source, so it produces research *directions*, not facts. Any belief it records is a `status: hypothesis`
  with `origin: agent`, written to a research file.
- **Log-aware, always.** Check the research log **before** proposing anything. Never re-propose a search
  already logged unless its nil has aged past the re-run horizon (default **18 months** — collections
  grow).
- **Log executed searches back.** When the human actually runs a lead you proposed, write the research-log
  entry (date, repository, collection, terms, result including nil).
- **Sessions are an interface, not memory** (_STANDARD.md §7): the plan and any hypothesis are written
  into research files, not left in the chat.

## Flow

1. **Read the state — including the log.**
   ```
   fha report
   ```
   The report already carries what you need: **§3** vitals gaps (W101), **§5b** open questions, **§7** open
   hypotheses, and — critically — **§5 search-log awareness**, which annotates leads "already searched
   (date)" and flags nils older than the horizon as worth re-running. Read §5 first: it is the log
   guardrail surfaced for you. For a person-scoped ask, also pull their neighborhood and their research
   file:
   ```
   fha find --related <P-id>          # the person's world: sources, places, associated people
   fha find <P-id>                    # locate their _research file (## Research Log, ## Hypotheses)
   ```

2. **Surface "already searched" before proposing anything.** For each gap you're about to address, state
   what's already been tried and when: *"You searched the 1870 Fairview census for the Coles in June 2026
   — came up nil. That's recent, so I'd skip it."* Only re-propose a logged nil if it has aged past 18
   months (*"…but that nil is over two years old now, and FamilySearch has added Kansas records since, so
   it's worth another pass"*).

3. **Combine gaps + questions + hypotheses with historical context into ranked leads.** For each open
   thread, name a concrete lead: the record set, the holding repository, and the search terms — steered by
   what actually existed for that time and place:
   - *"Thomas has no death record and the 1880 census puts him in Fairview about age 40, so he likely died
     in Breton County. Kansas kept statewide death records from 1911 — try the Kansas State Historical
     Society death index for Breton County, 1911–1925, terms 'Hartley, Thomas'. Ranked first: it closes a
     vitals gap and the collection is online."*
   - Weigh eras and events: a burned county courthouse, a war that generated pension files, a migration
     that implies a passenger list. Rank leads by payoff (closes a vital gap / answers an open question)
     and by ease (is the collection online, indexed, nearby?).

4. **Optionally draft hypotheses — into research files, tagged `origin: agent`.** When the evidence
   suggests a testable belief, record it under `## Hypotheses` in the person's `_research` file (mint the
   ID with `fha id mint H`):
   ```yaml
   ## Hypotheses
   - id: H-…
     hypothesis: "Thomas Hartley died in Breton County between 1911 and 1925"
     basis: "1880 census age ~40 in Fairview; no later record; Kansas death registration began 1911"
     verify: "Kansas State death index, Breton County 1911–1925"
     origin: agent
     status: open
   ```
   A hypothesis is a lead with a shape, never a claim. It carries `status: hypothesis`/`open` and
   `origin: agent`; an `H-id` never converts to a `C-id` — verification later mints a *new* claim and links
   both ways (AGENTS.md §"Format quick reference").

5. **Emit a plan-shaped list.** Present the leads as a short ranked plan the human can act on — top pick
   first, each with its record set / repository / terms / why. End with the single best next move.

6. **Log any executed search back.** If the human runs a lead (now or reports back that he did), append
   the entry under `## Research Log` in the relevant `_research` file:
   ```yaml
   ## Research Log
   - date: 2026-07-01
     question: "[[H-…]] Thomas Hartley death record"
     repository: Kansas State Historical Society
     collection: "Kansas death index, Breton County 1911–1925"
     terms: "Hartley, Thomas"
     result: nil        # or a note / an [[S-…]] when it turns up a source
   ```
   A logged nil is not a failure — it's what stops the next session from re-running the same dead end.

## Guardrails

- Leads and hypotheses only — **never** a claim (no source ⇒ no claim).
- Log-aware: no lead duplicates a recent logged nil (within the 18-month horizon); state "already searched
  (date)" before proposing.
- Hypotheses are `origin: agent`, `status: hypothesis`/`open`, written to a research file — never mixed
  into a source's `## Claims`.
- Executed searches are logged (including nils); the log is the memory, not the chat.

## Done when

- "Where should I look for X?" in a session on `example-archive` produces concrete, **ranked** leads with
  "already searched (date)" annotations present, and **no** lead duplicates a recent logged nil.
- Any drafted hypothesis is `origin: agent` / `status: hypothesis`, written to a research file — never a
  claim.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings
  (`_STANDARD.md` §9).
