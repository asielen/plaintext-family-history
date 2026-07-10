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
- **The mechanical write is deterministic-tool territory.** A confirmed merge is enacted by
  `fha confirm merge` — dry-run preview first, then live — never by hand-editing records. The skill
  supplies the judgment and the human's reason; the tool performs SPEC §9 exactly; the skill invents
  nothing.
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

4a. **On a confirmed MERGE — enact it with the verb (SPEC §9).** The **survivor** is the one that keeps
   the canonical home (prefer the curated record over a stub; the lower-numbered couple folder when both
   are placed). Then drive the deterministic write — never hand-edit the records:
   ```
   fha confirm merge <P-old> --into <P-survivor> --reason "<the human's stated reason>" --dry-run
   ```
   Walk the preview with the human (it shows every file edit as a diff plus the pending rename). When it
   matches the confirmed decision, run it live:
   ```
   fha confirm merge <P-old> --into <P-survivor> --reason "<the human's stated reason>"
   ```
   The verb performs the whole SPEC §9 write in one pass: the four tombstone fields (`status: merged`,
   `merged_into:`, `merge_reason:`, `merged_date:`), the `MERGED-INTO-P-survivor__` rename (the file
   **persists forever**, never deleted), the folds (name variants — restricted mapping forms preserved —
   external IDs, `relationships:` entries, deduped, with the tombstone's frontmatter stripped of what
   folded and its `aliases:` reduced to the bare P-id), and the relinks — every claim naming `P-old`
   *whatever its status* (E016 has no status filter), other profiles' `relationships:` edges (stale ones
   trip W115), and source `people:` lists. Loose *prose* `[[P-old]]` mentions are deliberately left; they
   resolve *through* `merged_into` and appear on lint's W107 gradual-cleanup list — expected, not an error.
   **Heed the verb's warnings — they are yours to judge, not suppress:**
   - an *external-id conflict* (exit 1) means the two records point at different WikiTree/Ancestry
     profiles: the survivor's value was kept and the tombstone keeps the other — bring the conflict back
     to the human;
   - an *existing relationship edge between the two* is evidence they may be two different people — the
     tool warns and proceeds (the decision was the human's), but re-open the conversation if this is news;
   - a *W115 heads-up* means the survivor's opted-in `relationships:` block should now apply relinked
     accepted kin claims — add those entries with the human.
   **Never create a new claim on the merged P-id** (that is exactly E016).

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
   fha views timeline <P-survivor>       # the relink moved a whole life onto the survivor, so
   fha views sources-index <P-survivor>  # regenerate its companion views - they key on person_id,
   fha views draft-queue <P-survivor>    # so the relinked claims/sources surface only after a refresh
   fha lint
   ```
   (Skip the view refresh if the survivor is a stub — stubs carry no companion views, SPEC §16.)
   Confirm **no new E016** (no claim references the merged person directly as a fresh write), **no new
   W107 regression** beyond the expected gradual-cleanup list, and **no new W115** (a `relationships:`
   entry stranded on the tombstone, or a folded kin edge missing from the survivor's block). Report plainly ("merged the duplicate stub
   into Thomas's record; his file is now the one home, the old one is kept as a tombstone, and nothing new
   points at the dead ID").

## Guardrails

- **Human-confirmed only** — no `merged_into` (or split reassignment) without an explicit decision in the
  transcript.
- Evidence for **and against** is laid out before any proposal.
- The merge write is `fha confirm merge`'s alone (SPEC §9 exactly: tombstone rename, four fields, folds,
  relinks) — always dry-run first; the skill never hand-edits a merge and invents no mechanics.
- Post-merge lint is clean of **new** E016/W107/W115; merged files are renamed-and-kept, never deleted.

## Done when

- A merge/split proposal in a session on `example-archive` lays out the neighborhood evidence and **waits
  for explicit human confirmation** before any write.
- Post-merge, `fha lint --root example-archive` shows no new **E016/W107/W115** and still exits 1 with only
  the documented baseline warnings (`_STANDARD.md` §9).
- No `merged_into` is set without an explicit human decision in the transcript.
