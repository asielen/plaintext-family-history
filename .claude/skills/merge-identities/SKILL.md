---
name: merge-identities
description: >
  Run when the human asks "are these the same person?" / "this record looks like two people" / points at
  a merge candidate from the report. Pulls both persons' neighborhoods (shared sources, overlapping
  vitals, co-occurrence, any relationship path), lays out the evidence for and against, and proposes a
  merge or a split — then waits for explicit human confirmation before any write. Frontier-tier: cheap to
  attempt, expensive to get wrong.
---

# merge-identities

Genealogy constantly asks "are these two records the same human?" or "is this one record actually two
people?" The answer needs judgment over the whole neighborhood — shared sources, overlapping vitals,
co-occurrence, existing relationship paths — and it is high-stakes: a bad merge corrupts the graph, a bad
split scatters a person. So this is a **frontier-tier** skill, and it **always** ends at a human
confirmation. The skill lays out the evidence and proposes; it never silently merges. See
[`../_STANDARD.md`](../_STANDARD.md).

> **Interim enactment (read this).** SPEC §9 defines the merge write, but no `fha` verb performs it yet —
> see [`GAP.md`](GAP.md). By the owner's decision, this skill enacts a **human-confirmed** merge by a
> careful SPEC §9 hand-edit **for now**, and the gap note tracks the wanted `fha confirm merge` verb that
> will replace the hand-edit. The hand-edit is temporary; the confirmation gate is not.

## When this runs

"Are these the same person?", "this looks like two people", "the report flagged a possible duplicate", "did
Thomas Hartley and Thos. Hartley get entered twice?"

## The contract for this skill (state it before you start)

- **Human-confirmed only.** No `merged_into` is ever written without an explicit human decision — the
  specific survivor and the specific merge — stated *this session*. The skill lays out evidence, proposes,
  and stops until the human confirms.
- **Evidence before any proposal.** Never propose a merge/split before showing the neighborhood evidence
  for **and against**. "Cheap to attempt, expensive to get wrong" means the human sees the case before he
  decides.
- **The mechanical write is deterministic-tool territory** (deferred to a hand-edit only until
  `fha confirm merge` ships — [`GAP.md`](GAP.md)). Even the hand-edit is strictly SPEC §9; the skill
  invents nothing.
- **Merged persons are never referenced anew** — post-merge, lint must show no new **E016** (a new claim on
  a merged P-id) or **W107** (direct reference to a merged person).

## Flow

1. **Pull both persons' neighborhoods.**
   ```
   fha find --related <P-a>          # A's world: sources, vitals, co-occurring people, places, edges
   fha find --related <P-b>          # B's world
   fha cooccur                        # shared-source signal (do they keep appearing together?)
   fha relate <P-a> <P-b>             # is there already a relationship path between them?
   ```
   Read both records directly for their vitals, `name_variants`, and existing claims.

2. **Lay out the evidence for and against — plainly.** Give the human a two-column read he can judge:
   - **For same-person:** shared sources naming both, compatible/identical vitals (same birth year and
     place), the same associates, no relationship path that would make them distinct (you don't merge a
     father into his son).
   - **Against:** conflicting vitals (two different death dates), sources that place them in different
     places at the same time, an existing relationship edge between them.
   - **For a split (one record, two people):** claims that don't cohere — two incompatible birth years,
     two occupations that can't be one life, sources that clearly describe different individuals — and
     *which* claims belong to which person.

3. **Propose, then wait.** State your read as a recommendation with its confidence ("These look like the
   same person — same 1840 birth in New York, both in the 1880 Fairview household, no path separating them.
   I'd merge the newer stub into the curated record. Shall I?"), and **wait for explicit confirmation.** Do
   nothing to the records until he says yes to a specific decision. If he's unsure, leave both records as
   they are — a non-merge is always safe.

4a. **On a confirmed MERGE — enact it (SPEC §9; interim hand-edit per [`GAP.md`](GAP.md)).** The
   **survivor** is the one that keeps the canonical home (prefer the curated record over a stub; the
   lower-numbered couple folder when both are placed). On the **other** (merged) record:
   - set `status: merged`, `merged_into: P-survivor`, `merge_reason: "<the human's stated reason>"`,
     `merged_date: <today>`;
   - **rename the file** with the tombstone prefix — `MERGED-INTO-P-survivor__<original-filename>` (e.g.
     `MERGED-INTO-P-de957bcda1__hartley__thomas_P-old.md`) — the file **persists forever**, never deleted;
   - **fold** the merged record's `name_variants:` and external IDs into the survivor's record;
   - **relink every claim that names the merged person** — for each claim, *whatever its status*
     (`suggested`, `accepted`, `disputed`, `rejected`, `superseded`, `needs-review`), whose
     `persons:`/`roles:` includes `P-old`, change it to `P-survivor`. E016 has no status filter — it fires
     on any claim referencing a merged person, and disputed/rejected claims are kept, never deleted — so
     this is required, not optional cleanup: a claim left pointing at `P-old` both trips lint **E016** *and*
     (for accepted claims) silently drops out of the survivor's timeline/draft-queue views;
   - **relink `relationships:` edges** — any *other* profile's `relationships:` entry that names `P-old`
     (a sourced edge carrying `claim:`/`source:`) must be repointed to `P-survivor`; left stale it trips
     lint **W115** (the entry no longer reconciles with its backing claim) and the human-facing
     relationship section keeps naming the tombstoned record;
   - **relink** the remaining direct references you can reach (frontmatter `people:`, prose `[[P-old]]`);
     only loose *prose* mentions you don't reach may resolve *through* `merged_into` and appear on lint's
     W107 gradual-cleanup list — that (prose, never a claim) is expected, not an error.
   Record the reasoning where the merge is enacted (the `merge_reason:` and, if useful, a note on the
   survivor). **Never create a new claim on the merged P-id** (that is exactly E016).

4b. **On a confirmed SPLIT (conflation) — reassign deliberately (SPEC §9).** Splitting an identity is
   research judgment, so it stays guided:
   - mint the new person: `fha id mint P` (then scaffold a stub/record for them);
   - move each claim's `persons:`/`roles:` entries to the correct person, one at a time, with the human
     confirming each reassignment;
   - note the split and its date on **both** records.
   No tombstone here — both persons are real.

5. **Verify.**
   ```
   fha index
   fha lint
   ```
   Confirm **no new E016** (no claim references the merged person directly as a fresh write) and **no new
   W107 regression** beyond the expected gradual-cleanup list. Report plainly ("merged the duplicate stub
   into Thomas's record; his file is now the one home, the old one is kept as a tombstone, and nothing new
   points at the dead ID").

## Guardrails

- **Human-confirmed only** — no `merged_into` (or split reassignment) without an explicit decision in the
  transcript.
- Evidence for **and against** is laid out before any proposal.
- The merge write follows SPEC §9 exactly (tombstone rename, four fields, fold variants) — interim
  hand-edit only, per [`GAP.md`](GAP.md); no invented mechanics.
- Post-merge lint is clean of **new** E016/W107; merged files are renamed-and-kept, never deleted.

## Done when

- A merge/split proposal in a session on `example-archive` lays out the neighborhood evidence and **waits
  for explicit human confirmation** before any write.
- Post-merge, `fha lint --root example-archive` shows no new **E016/W107** and still exits 1 with only
  the documented baseline warnings (`_STANDARD.md` §9).
- No `merged_into` is set without an explicit human decision in the transcript.
