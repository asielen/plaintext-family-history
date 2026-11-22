---
name: research-next
description: >
  Run when the human asks "where should I look for X?" / "what's next on the Hartleys?" / "what should I
  research?". Checks the research log FIRST, then combines open questions, working answers, and the
  gaps `fha report` computes with historical context (which record sets exist for the era/place, where
  they're held) into concrete, ranked research leads. May write a guess as a working answer on a question
  the human asked, credited `(agent)` on its dated context line; it writes leads and working answers, never claims. Logs any executed search back to
  `notes/research-log.md`.
---

# research-next

The archive already knows what it doesn't know: open questions, working answers still to settle, and the gaps `fha report` computes (vitals, W101). This skill turns those into *concrete* leads (which record set, which repository, which search terms) steered by historical context. The research log keeps it from repeating work: never propose a search already run unless its nil has aged past the re-run horizon. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"Where should I look for Thomas's death record?", "what's next on the Hartleys?", "what should I research now?", "any leads on Margaret's parents?"

## The contract for this skill (state it before you start)

- **Leads and working answers only, never claims.** A claim requires a source by definition. This skill has no source, so it produces research directions. A guess to keep is a **working answer** (SPEC §17a rung 2): the question entry it answers, with `id:`, `guess:`, `basis:` and `verify:` added. It is never a separate object and never a claim. When the guess is a relationship and the human wants to build on it, it can also go on the person's `relationships:` with `status: hypothesis` so the tree can be followed past it (§17a rung 3), linked back to its working answer with `hypothesis: h-…`: `fha person relate <p-id> --parent <p-id2> --working-answer h-…` writes both, on the human's yes.
- **Gaps are read, never written.** A gap a tool can compute (no death date, no parents, a provisional birth with no source) is shown fresh by `fha report` every time; never write it into the questions file as a question (SPEC §17b). A question entry is written when the human asks one, or when you have a guess to keep (then the entry exists for its working answer).
- **Log-aware, always.** Check the research log **before** proposing anything. Never re-propose a search already logged unless its nil has aged past the re-run horizon (default **18 months**, because collections grow).
- **Log executed searches back.** When the human actually runs a lead you proposed, append the entry to `notes/research-log.md` (date, question, repository, collection, terms, result including nil; SPEC §17c).
- **Write it down before handing back** (_STANDARD.md §7): any working answer goes into the archive, in its question entry in the person's research file or `notes/questions.md`, and is not left in the chat.
- **A nil is a statement about one search.** A logged nil is never written as a claim with `evidence: negative` or `negated: true`, and never said as "there is no record", until the lead also knows the record would have been made, survives for that place and those years, and could have been found by the search that was run (AGENTS.md §"You cannot conclude absence from a search").
- **Mark what you have not checked.** Every repository a lead names comes from the research log, or is flagged as general knowledge. Hours, fees, addresses, web addresses and the years a collection covers carry "(check this)" unless they were confirmed this session. Every lead says free or paid, and online, in person or by letter.

## Flow

1. **Read the state, including the log.**
   ```
   fha report
   ```
   The report already carries much of what you need: **§1b** claims to look at again (parked claims, still `suggested` but carrying a `reviewed:` date, and accepted low-confidence facts; both are corroboration targets, see step 3), **§3** vitals gaps (W101), **§7** open working answers, and **§5 search-log awareness**, which annotates leads "already searched (date)" and flags nils older than the horizon as due for a re-run. Read §5 first: it is the log check, already done for you. **§5b** is *answerable* questions: open questions that already have a closeable answer (a "close this out" signal). It is not the open-research backlog. For that backlog, read `notes/questions.md` (`## Q:` entries with `status: open`) and each person's research-file `## Open Questions` directly. For a person-scoped ask, also pull their neighborhood and their research file:
   ```
   fha find --related <p-id>          # the person's world: sources, places, associated people
   fha find <p-id>                    # the person's own record, and their research file if they have
                                       # one (## Research Notes, ## Open Questions; most people never
                                       # have this file, SPEC §16b)
   ```

2. **Surface "already searched" before proposing anything.** For each gap you're about to address, state what's already been tried and when: *"You searched the 1870 Fairview census for the Coles in June 2026 and found nothing. That's recent, so I'd skip it."* Only re-propose a logged nil if it has aged past 18 months (*"…but that search is over two years old, and FamilySearch has added Kansas records since, so it's worth another pass"*).

3. **Combine gaps, questions, working answers and thin facts with historical context into ranked leads.** An **accepted claim with `confidence: low`** is a lead too (SPEC §8.5: the human is building on it without being certain). Propose the record that would corroborate or supersede it: *"Thomas's bookkeeper occupation rests on one low-confidence directory mention; the 1885 Kansas state census would confirm it."* A **parked claim** (report §1b: `suggested`, with a `reviewed:` date) works the same way: name what would settle it. For each open thread, name a concrete lead: the record set, the holding repository, and the search terms, steered by what actually existed for that time and place:
   - *"Thomas has no death record and the 1880 census puts him in Fairview about age 40, so he likely died in Breton County. Kansas kept statewide death records from 1911. Try the Kansas State Historical Society death index for Breton County, 1911-1925, searching 'Hartley, Thomas' (free, online; check this: which years the index covers). I've ranked this first because it fills a missing death date and the collection is online."*
   - Weigh eras and events: a burned county courthouse, a war that generated pension files, a migration that implies a passenger list. Rank leads by payoff (closes a vital gap or answers an open question) and by ease (is the collection online, indexed, nearby?).
   - **One lead tries to prove the favourite answer wrong.** When the leads all aim to confirm a working answer or a likely identity, add one search that would turn up the other explanation if it were true: the other man of the same name in the next county, the death record of the child the guess says grew up, the census where the family should not appear if the guess is right. Label it plainly ("this one checks we have the right man").
   - **DNA leads give ranges.** Never turn a DNA match into a percentage. Name the relationships the shared amount fits, ask whether the families married within a small community or married cousins, and look for the documents that would decide between the candidates.

4. **Optionally write a guess as a working answer, on a question he asked.** When the evidence suggests a testable belief about a question the human has asked (in this session or already written down), it climbs the research ladder (SPEC §17a): the guess goes into that **question entry** as its working answer. If the question is already written, add the four lines to it. If he is asking it in this session, write one entry in SPEC §17b's form with `origin: human`, in the person's research file (`## Open Questions`; the file is created the first time something is written to it, SPEC §16b) or in `notes/questions.md` for a question spanning people. A gap the tools can compute (no death date, no parents) is never written as a question to hang a guess on (SPEC §17b): offer the guess in the briefing instead, and write it only if he then asks the question. Mint the id with `fha id mint h`:
   ```markdown
   ## Q: Was the Thomas Hartley in the 1880 Fairview census our Thomas?
   - origin: human
   - status: open
   - refs: [p-…]
   - id: h-…
   - guess: yes, the same man
   - basis: the census age (about 40) and birthplace (New York) match his baptism ([[s-…]]); no other Thomas Hartley in Breton County
   - verify: the 1885 Kansas state census, which names his wife
   - context:
     - 2026-07-01 (agent): guess drawn from the census age and birthplace
   ```
   A working answer is a lead with a shape. It is never a claim. It settles one of three ways (§17a rung 4): a source says it (process the source; its claim carries the fact and may point back with `hypothesis: h-…`), the pieces add up (a proof-argument source), or it is wrong. When it is wrong, its `id`/`guess`/`basis`/`verify` lines leave the entry, one dated `context:` line records it (`ruled out h-…: <guess>: <why>`), and the question stays open; the next guess gets a new `h-` id. `refs:` holds bare IDs in single brackets. **Any ID cited in `basis:`, `verify:` or a context line is prose and takes the `[[s-…]]`/`[[c-…]]`/`[[p-…]]` wikilink form** (AGENTS.md §"Format quick reference" Citations). When a newly processed source turns up evidence bearing on an **open** working answer, append a dated context line to the entry in the same session, citing the new source or claim with `[[ ]]` links. Don't edit its `guess`/`basis`/`verify` in place: those stay as first written, and the context line adds the new information on top.

5. **Emit a plan-shaped list.** Present the leads as a short ranked plan the human can act on, top pick first. Each lead gives the record set, the repository, the search terms, the reason, and two labels: free or paid, and online, in person or by letter. Anything about the repository not confirmed this session carries "(check this)". End with the single best next move.

6. **Log any executed search back.** If the human runs a lead (in this session, or reports back that he did), append one entry to **`notes/research-log.md`**, the archive's one search log (SPEC §17c). Put the working answer or the person in `question:`. A person ID there makes the search count for that person, so "already searched (date)" keeps working:
   ```yaml
   - date: 2026-07-01
     question: "[[h-…]] Thomas Hartley death record"
     repository: Kansas State Historical Society
     collection: "Kansas death index, Breton County 1911-1925"
     terms: "Hartley, Thomas"
     result: nil        # or found [[s-…]] / partial (note)
   ```
   A logged nil is a result: it stops the next session from re-running the same search.

## Guardrails

- Leads and working answers only. **Never** a claim (no source, no claim).
- A computed gap is never written as a question; a question entry exists because the human asked it or because it carries a working answer.
- Log-aware: no lead duplicates a recent logged nil (within the 18-month horizon); state "already searched (date)" before proposing.
- A working answer is the question entry's `id`/`guess`/`basis`/`verify` lines on a question the human asked (`origin:` stays the question's origin; the guess is credited `(agent)` on its dated `context:` line), `status: open`, in the research file or `notes/questions.md`. It never goes into a source's `## Claims`.
- Executed searches are logged in `notes/research-log.md` (including nils), since nothing said in the chat carries over to the next session.
- Prose written into a question's `context:` lines or a working answer keeps each paragraph on one line, with a blank line between paragraphs: never hard-wrapped at a column and never one sentence per line (_STANDARD.md §4).
- A nil is never turned into negative evidence by this skill, in a claim or in speech.
- Every lead carries its cost and access labels, unchecked repository details say "(check this)", and a plan that argues for one answer includes one search that could prove it wrong.

## Done when

- "Where should I look for X?" in a session on `example-archive` produces concrete, **ranked** leads with "already searched (date)" annotations present, and **no** lead duplicates a recent logged nil.
- Every lead in the plan says free or paid and online, in person or by letter; unchecked repository details carry "(check this)"; a plan aimed at confirming a working answer includes one search that could disprove it.
- Any guess written down is a working answer on a question entry (`status: open`, with `id`/`guess`/`basis`/`verify`, and the guess credited `(agent)` on its dated `context:` line), never a claim, and no computed gap was written as a question.
- `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9).
