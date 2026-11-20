---
name: merge-identities
description: >
  Run when the human asks "are these the same person?" / "this record looks like two people" / points at
  a merge candidate from the report. Pulls both persons' neighborhoods (shared sources, overlapping
  vitals, co-occurrence, any relationship path), lays out the evidence for and against, and proposes a
  merge or a split, then waits for explicit human confirmation before any write. Frontier-tier: cheap to
  attempt, expensive to get wrong.
---

# merge-identities

Genealogy keeps asking "are these two records the same human?" or "is this one record actually two people?" The answer needs judgment over the person's neighborhood (shared sources, overlapping vitals, co-occurrence, existing relationship paths), and the stakes are high: a bad merge corrupts the graph, and a bad split scatters a person across two records. So this is a **frontier-tier** skill, and it **always** ends at a human confirmation. The skill lays out the evidence and proposes; it never merges on its own. See [`../_STANDARD.md`](../_STANDARD.md).

## When this runs

"Are these the same person?", "this looks like two people", "the report flagged a possible duplicate", "did Thomas Hartley and Thos. Hartley get entered twice?"

Two *source* records that are one piece of evidence (an index entry and the document it indexes, one record saved from two sites) are not this skill. They merge with `fha source merge <keep> <other>`: run it with `--dry-run`, show the human what moves, and run it for real only on his yes. It refuses when the two sources' accepted claims are about different people.

## The contract for this skill (state it before you start)

- **Human-confirmed only.** No `merged_into` is ever written without an explicit human decision, naming the specific survivor and the specific merge, stated *this session*. The skill lays out evidence, proposes, and stops until the human confirms.
- **Evidence before any proposal.** Never propose a merge or split before showing the neighborhood evidence for **and against**. The human sees the case before he decides.
- **The mechanical write belongs to the tool.** A confirmed merge is enacted by `fha confirm merge`, with a dry-run preview first and then the live run. Never hand-edit the records. The skill supplies the judgment and the human's reason, the tool performs SPEC §9 exactly, and the skill invents nothing.
- **Merged persons are never referenced anew.** After a merge, lint must show no new **W107** (a claim or a prose link still naming a merged p-id; it counts for the survivor, but it is cleanup left undone).

## Flow

1. **Pull both persons' neighborhoods.**
   ```
   fha find --related <p-a>          # A's world: sources, vitals, co-occurring people, places, edges
   fha find --related <p-b>          # B's world
   fha cooccur                        # shared-source signal (do they keep appearing together?)
   fha relate <p-a> <p-b>             # is there already a relationship path between them?
   ```
   Read both records directly for their vitals, `name_variants`, and existing claims.

2. **Lay out the evidence for and against, plainly.** Give the human a two-column read he can judge:
   - **For same-person:** shared sources naming both, compatible or identical vitals (same birth year and place), the same associates, and no relationship path that would make them distinct (you don't merge a father into his son).
   - **Against:** conflicting vitals (two different death dates), sources that place them in different places at the same time, an existing relationship edge between them.
   - **For a split (one record, two people):** claims that don't fit one life (two incompatible birth years, two occupations one person couldn't hold, sources that clearly describe different individuals), and *which* claims belong to which person.

3. **Propose, then wait.** State your read as a recommendation with its confidence ("These look like the same person: the same 1840 birth in New York, both in the 1880 Fairview household, and nothing in the records separating them. I'd fold the newer short record into Thomas's full page. Shall I?"), and **wait for explicit confirmation.** Do nothing to the records until he says yes to a specific decision. If he's unsure, leave both records as they are; not merging is always safe.

4a. **On a confirmed MERGE, enact it with the verb (SPEC §9).** The **survivor** is the one that keeps the canonical home (prefer the curated record over a stub; the lower-numbered couple folder when both are placed). Then run the deterministic write. Never hand-edit the records:
   ```
   fha confirm merge <p-old> --into <p-survivor> --reason "<the human's stated reason>" --dry-run
   ```
   Walk the preview with the human (it shows every file edit as a diff plus the pending rename). When it matches the confirmed decision, run it live:
   ```
   fha confirm merge <p-old> --into <p-survivor> --reason "<the human's stated reason>"
   ```
   The verb performs the full SPEC §9 write in one pass:
   - the four tombstone fields (`status: merged`, `merged_into:`, `merge_reason:`, `merged_date:`);
   - the `MERGED-INTO-p-survivor__` rename (the file **persists forever** and is never deleted);
   - the folds: name variants (restricted mapping forms preserved), external IDs, and `relationships:` entries, deduped, with the tombstone's frontmatter stripped of what folded and its `aliases:` reduced to the bare p-id;
   - the relinks: every claim naming `p-old` *whatever its status* (W107 lists claims of every status), other profiles' `relationships:` edges (stale ones trip W115), and source `people:` lists.

   Loose *prose* `[[p-old]]` mentions are left on purpose. They resolve *through* `merged_into` and appear on lint's W107 gradual-cleanup list, which is expected and not an error. **Read the verb's warnings and judge each one; don't suppress them:**
   - an *external-id conflict* (exit 1) means the two records point at different WikiTree/Ancestry profiles: the survivor's value was kept and the tombstone keeps the other. Bring the conflict back to the human;
   - an *existing relationship edge between the two* is evidence they may be two different people. The tool warns and proceeds (the decision was the human's), but reopen the conversation if he didn't know about the edge;
   - a *W115 heads-up* means the survivor's opted-in `relationships:` block should take the relinked accepted kin claims. Show him `fha lint --fix-applied --dry-run`, and on his yes run `fha lint --fix-applied` to write those entries. **Never create a new claim on the merged p-id** (that is exactly what W107 reports).

4b. **On a confirmed SPLIT (conflation), reassign deliberately (SPEC §9).** Splitting an identity is research judgment, so it stays guided:
   - mint the new person: `fha id mint p` (then scaffold a stub or record for them);
   - move each claim's `persons:`/`roles:` entries to the correct person, one at a time, with the human confirming each reassignment;
   - note the split and its date on **both** records. No tombstone here: both persons are real.

5. **Verify.**
   ```
   fha index
   fha views timeline <p-survivor>       # the relink moved a whole life onto the survivor, so
   fha views sources-index <p-survivor>  # regenerate its views; they key on person_id (sources-index
   fha views draft-queue <p-survivor>    # refreshes the survivor's own ## Sources section in place),
                                          # so the relinked claims and sources appear only after a refresh
   fha lint
   ```
   If the survivor is a stub, skip `timeline` and `draft-queue` (stubs carry no companion views for those, SPEC §16), but still run `sources-index`: it works on a stub too, so the relinked sources show up in its `## Sources` section right away instead of waiting for promotion. Confirm **no new W107** beyond the expected gradual-cleanup list (no claim names the merged person directly, because the relink moved every one, and only the loose prose mentions remain), and **no new W115** (a `relationships:` entry stranded on the tombstone, or a folded kin edge missing from the survivor's block). Report plainly: "I merged the duplicate into Thomas's record. His file is the one home for him, the old file is kept and marked as merged, and nothing new points at the old ID."

## Guardrails

- **Human-confirmed only.** No `merged_into` (or split reassignment) without an explicit decision in the transcript.
- Evidence for **and against** is laid out before any proposal.
- The merge write belongs to `fha confirm merge` alone (SPEC §9 exactly: tombstone rename, four fields, folds, relinks), always dry-run first. The skill never hand-edits a merge and invents no mechanics.
- Post-merge lint is clean of **new** W107/W115; merged files are renamed and kept, never deleted.
- Any record ID written into prose (a split note on a record, a reason narrated into `## Notes`) is `[[ ]]`-wrapped, `[[id|Name]]` preferred; bare IDs only in structured fields and tool arguments (_STANDARD.md §11).
- A split note or merge reason written into a record's `## Notes` keeps each paragraph on one line, with a blank line between paragraphs: never hard-wrapped at a column and never one sentence per line (_STANDARD.md §4).

## Done when

- A merge or split proposal in a session on `example-archive` lays out the neighborhood evidence and **waits for explicit human confirmation** before any write.
- Post-merge, `fha lint --root example-archive` shows no new **W107/W115** and still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9).
- No `merged_into` is set without an explicit human decision in the transcript.
