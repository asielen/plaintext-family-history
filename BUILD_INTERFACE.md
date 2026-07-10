# BUILD_INTERFACE.md - the AI interface (workbench skills): build sequence

**Who this is for:** developers implementing the workflow **skills** that drive the `fha` tool suite. If you just want to use the archive, start with [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

This file is the build guide for the **interface layer** - the `.claude/skills/` workflow skills and the harness conventions around them. It is the sibling of [`BUILD.md`](BUILD.md) (core `fha` tools) and [`BUILD_INGESTION.md`](BUILD_INGESTION.md) (capture / inbox on-ramp). Design rationale lives in [`TOOLING_INTERFACE.md`](TOOLING_INTERFACE.md); this file tells you the sequence and how to verify it.

**Status: session spine + drafting/inference + frontier skills authored (I1-I2 + place-research authored; merge-identities authored with an interim enactment; photo-context's core verb shipped, its SKILL.md pending).** The `.claude/skills/` directory now holds `_STANDARD.md` (the authoring contract), `today`, `review-claims`, `process-source`, `mine-transcript`, `write-biography`, `research-next`, `place-research`, and `merge-identities` SKILL.md files, plus a `photo-context/DESIGN.md`. Each SKILL.md was authored against the shipped tools (every `fha` command it invokes was verified to exist) and against `AGENTS.md` / `_STANDARD.md`; the lint invariant holds (`fha lint --root example-archive` still exits 1 on the pre-existing baseline, unchanged by the skill prose). The remaining acceptance gate for each is the **behavioral session check** (run it against `example-archive`, capture the transcript) - marked per-milestone below. Building surfaced **two core-tool gaps** (see MI3.1 and MI4).

---

## What a skill is (conventions, all phases)

- **One folder per skill:** `.claude/skills/{name}/SKILL.md`, using the open SKILL.md standard (portable beyond Claude Code).
- **Instructions + `fha` invocations only.** No harness APIs, no MCP calls, no Python. A skill orchestrates deterministic tools and adds model judgment; it never reimplements what a tool already does (TOOLING_INTERFACE.md §2).
- **The contract is law** (AGENTS.md): AI-drafted claims are `status: suggested`; only the human moves a claim to `accepted` (always via `fha claim`, which stamps `reviewed:`); every AI pass is recorded in the source's `## AI Passes` block; draft prose lives behind `<!-- AI-DRAFT … -->` markers; nothing edits below a GENERATED header or overwrites human text.
- **Sessions are an interface, not memory** (AGENTS.md): anything worth keeping is written into archive records in SPEC formats before the skill hands back.
- **Definition of done is behavioral, not unit-tested.** A SKILL.md is verified by running it in a real session against `example-archive/` and confirming it produces the documented archive writes (suggested claims, recorded passes, view refreshes) and the documented hand-offs - and that it makes **zero** writes the contract forbids. There is no automated harness for skill prose; the "Done when" blocks below are session checks.
- **Vendor-lock rules hold** (TOOLING_INTERFACE.md §1): if a second harness is added, its convention file is a one-line deferral to AGENTS.md and the same SKILL.md files drive it unchanged.
- **Discoverable without a skill loader.** `AGENTS.md`'s "Playbooks" subsection (plan 02, landed 2026-07-09) points any harness at `.claude/skills/{name}/SKILL.md` and tells it to read and follow the matching one when a request matches its trigger - closing the gap for a harness (e.g. Codex) that reads `AGENTS.md` natively but has no native skill loader.

**Dependency note.** Every skill depends only on already-shipped `fha` commands; there is no skill→skill import. The ordering below is by daily-loop centrality (build the session spine first), not by hard dependency. `process-source` *hands off* to `review-claims`, so build `review-claims` no later than `process-source`.

---

## Layer I1 - The session spine (Milestone I1 - authored; session check pending)

The three skills a genealogist touches every session: open the workbench, process new material, review what was drafted.

---

### MI1.1 - `today` skill (`/today`)

**One PR.** `.claude/skills/today/SKILL.md` + a `/today` slash wrapper.

**Status: authored** (`.claude/skills/today/SKILL.md`; the folder name *is* the `/today` wrapper - the harness surfaces the skill as `/today`). Reference skill for `_STANDARD.md`. Session check pending.

Run `fha report`, then narrate it **discoveries-first** (the report is a research narrative before a chore list), and offer to start the top item - most often a `review-claims` session on the oldest `suggested` backlog, or processing the inbox. The skill reads the report; it does not recompute it. It writes nothing on its own except, on the human's say-so, a `fha confirm discovery` entry for a confirmed win.

**Orchestrates:** `fha report` (read), `fha confirm discovery` (on confirmation).

**Done when:**
- In a session on `example-archive`, `/today` narrates report sections 0-8, leads with discoveries, and offers a concrete next action.
- It makes no archive write unless the human confirms one; a confirmed discovery lands in `notes/discoveries.md` via `fha confirm discovery`.

---

### MI1.2 - `review-claims` skill

**One PR.** `.claude/skills/review-claims/SKILL.md`.

**Status: authored** (`.claude/skills/review-claims/SKILL.md`). The reused accept-gate interaction; session check pending.

Stage C of the pipeline. Walk one source's `suggested` backlog (guided one-by-one, or open the source file for self-serve skimming - the human's choice). For each claim, show the claim plus its `anchor:` context; capture the human's accept / dispute / edit decision and any manual additions; write each decision with `fha claim` (which moves status and stamps `reviewed:` - directing the tool *is* the accept). Finish with a reindex (full `fha index` - the `process-source`/`mine-transcript` hand-off usually minted new person stubs, which `fha index --source` does not index; `--source` is fine only for a status-only pass), `fha xref` to surface new corroboration/contradiction, a reindex again if any `fha confirm xref` link was written, a `fha views timeline`/`draft-queue` refresh for each curated person touched (stubs skipped; `views brackets` checked when a relationship claim was accepted), and `fha lint`.

**Orchestrates:** `fha claim`, `fha confirm xref`, `fha index`, `fha xref`, `fha views timeline`/`draft-queue` (touched persons), `fha lint`.

**Guardrails:** never moves a claim to `accepted` without the human; `accepted` always carries `reviewed:` (E006). The skill presents judgment; the human gates.

**Done when:**
- Reviewing a source's suggested claims in a session results in `fha claim` writes for each decision, a clean incremental reindex, an `fha xref` pass, a timeline/draft-queue refresh for each curated person touched, and `fha lint` exiting on its real findings.
- No claim reaches `accepted` without an explicit human decision in the transcript.

---

### MI1.3 - `process-source` skill

**One PR.** `.claude/skills/process-source/SKILL.md`. Depends on MI1.2 (hands off to it).

**Status: authored** (`.claude/skills/process-source/SKILL.md`; hands off to the shipped `review-claims`). Session check pending.

The pipeline driver. If the inbox item is a **source stub** (`*.notes.md` sidecar or a bundle folder, SPEC §12.1), its frontmatter + notes seed Stage A (pre-filling §14 frontmatter) and its parsed-person/vital hints seed Stage B's draft; otherwise Stage A starts from the bare file. Stage A is `fha process`; Stage B is the AI draft (read the file incl. vision, resolve names/places against the index with candidate proposals, draft `suggested` claims with `anchor:`s, pull `## Stories`); then hand off to `review-claims` for Stage C, whose close-out (reindex, xref, view refresh, lint) finishes the pipeline. The stub is **consumed** - promoted into the source record, not left behind. Must handle loosely-written notes gracefully (AGENTS.md): extract what it can, fold the rest into `## Notes`, never stall on imperfect prose.

**Orchestrates:** `fha process`, `fha id mint` (via process), `fha stubs` (unresolved names), then `review-claims` (whose close-out owns the reindex/xref/views/lint).

**Done when:**
- Processing an inbox stub in a session yields a real `sources/…` record with `suggested` claims + anchors, the stub consumed, the AI pass recorded in `## AI Passes`, and a hand-off into `review-claims`.
- A loosely-written note (approximate dates, informal spellings) processes without a hard refusal; unmappable prose lands in `## Notes`.

---

## Layer I2 - Drafting & inference (Milestone I2 - authored; session check pending)

The skills invoked by name when the human wants prose written or leads found.

---

### MI2.1 - `mine-transcript` skill

**One PR.** `.claude/skills/mine-transcript/SKILL.md`. **Never runs unrequested.**

**Status: authored** (`.claude/skills/mine-transcript/SKILL.md`; invoked-only). Session check pending.

The invoked extraction pass over a transcript: selective claim drafting (`suggested` + `anchor:` - substantive assertions only), name→P-id resolution against the index with candidate proposals for unresolved names (mint stubs on confirmation), narrative chunks to `## Stories`, the rest left in the transcript (it is preserved and searchable; extraction is indexing, not preservation). Record the pass in the source's `## AI Passes` block (model, date, task).

**Orchestrates:** `fha stubs`, `fha id mint`, `fha index --source`, `fha lint`.

**Done when:**
- Mining a transcript in a session drafts suggested claims with anchors, routes stories to `## Stories`, records the pass, and leaves the transcript text intact.
- The skill takes no action unless explicitly invoked.

---

### MI2.2 - `write-biography` skill

**One PR.** `.claude/skills/write-biography/SKILL.md`.

**Status: authored** (`.claude/skills/write-biography/SKILL.md`). Session check pending.

Drafting rules for profiles: facts only from `accepted` claims; cite every factual sentence (summary block: one citation per line; body: all relevant citations); anything uncited must read as story/context; `[[P-…]]`/`[[S-…]]` links only from verified IDs. Consumes the `fha views draft-queue` backlog (uncited accepted claims). Draft prose is written inside `<!-- AI-DRAFT … -->` markers; on human acceptance, `fha confirm draft <P-id>` flips the marker to `<!-- AI-ACCEPTED … -->` (provenance preserved).

**Orchestrates:** `fha views draft-queue`, `fha find` (verify IDs), `fha confirm draft`.

**Done when:**
- Drafting a bio in a session pulls from the draft queue, cites every factual sentence with a verified `[[S-…]]`, wraps new prose in AI-DRAFT markers, and never overwrites human-written text.
- Acceptance flips markers via `fha confirm draft`, not by hand-editing.

---

### MI2.3 - `research-next` skill

**One PR.** `.claude/skills/research-next/SKILL.md`.

**Status: authored** (`.claude/skills/research-next/SKILL.md`). Session check pending.

Inference and steering. **Checks the research log FIRST** - never proposes a search already logged unless the nil has aged past the re-run horizon (default 18 months). Combines open questions, vitals gaps, and open hypotheses with historical context (which record sets exist for the time/place, where they are held, what era events imply) into concrete research leads. May draft hypotheses (`origin: agent`) into research files - leads and hypotheses, never claims. Emits plan-shaped output whose executed searches are logged back to the search log.

**Orchestrates:** `fha report` / index queries (gaps, questions, hypotheses), the search-log surface, `fha lint`.

**Done when:**
- Asking "where should I look for X?" in a session produces log-aware leads (already-searched annotations present), and any drafted hypothesis is `origin: agent`, never a claim.
- No lead duplicates a recent logged nil.

---

## Layer I3 - Frontier-tier skills (Milestone I3 - authored; MI3.1 has an interim enactment + core gap)

Cheap to attempt, expensive to get wrong - escalate to the frontier model tier (TOOLING_INTERFACE.md §1).

---

### MI3.1 - `merge-identities` skill

**One PR.** `.claude/skills/merge-identities/SKILL.md`.

**Status: authored, with an interim enactment path** (`.claude/skills/merge-identities/SKILL.md`). The judgment half - pull both neighborhoods, lay out the evidence, propose, wait for human confirmation - is fully on shipped tools. **Core gap surfaced:** SPEC §9 defines the merge write but **no `fha` verb performs it** (`fha confirm` has no `merge` verb). Per the owner's decision, the skill enacts a human-confirmed merge by a careful SPEC §9 hand-edit **for now**, and `.claude/skills/merge-identities/GAP.md` tracks the wanted `fha confirm merge` verb that should replace it (a BUILD.md core PR). Session check pending.

"Same person" / "two people" judgment. Read the candidate neighborhood (`fha find --related`, co-occurrence signals); propose a merge or a split with the evidence laid out; the human confirms. The mechanical write (setting `merged_into`, relinking) is the deterministic tool's job - the skill never silently merges. A merged person is never directly referenced again (lint E016/W107 enforce this).

**Orchestrates:** `fha find --related`, `fha cooccur`, `fha lint` (E016/W107 verification).

**Done when:**
- A merge/split proposal in a session lays out the neighborhood evidence and waits for human confirmation; post-merge, lint shows no E016/W107 regressions.

---

### MI3.2 - `place-research` skill

**One PR.** `.claude/skills/place-research/SKILL.md`.

**Status: authored** (`.claude/skills/place-research/SKILL.md`; registry writes go through the shipped `fha confirm place`). Session check pending.

"Fill in this place's history." Loose citations are acceptable (place context is narrative scaffolding, not vital fact). Draft dated `history:` entries and place notes, link `[[L-…]]`, and propose registry entries for `fha confirm place` to write. Never edits `places.yaml` coordinates without human confirmation (AGENTS.md).

**Orchestrates:** `fha find --related <L-id>`, `fha places candidates`, `fha confirm place`.

**Done when:**
- Researching a place in a session drafts dated history with `[[L-…]]` links and proposes registry writes via `fha confirm place`; no coordinate is changed without confirmation.

---

## Layer I4 - Skill backlog (Milestone I4 - designed; core verb shipped, SKILL.md pending)

Ideas carried from TOOLING_INTERFACE.md §2.3. `photo-context` has a settled design and its core-tool
gap is now closed (`fha photoindex set-summary` shipped, BUILD.md M3.5); the SKILL.md itself is not
yet written, so the layer stays unshipped.

| Skill | Status | Sketch |
|---|---|---|
| `photo-context` | **designed; core verb shipped - SKILL.md pending** | Update a photo's embedded AI summary (UserComment) with archive knowledge: identified people's relationships, the event/claim context, place history - captions get smarter as the archive grows. Writes marked as AI (SPEC §20); operates through `fha photoindex` and exiftool-via-tool, never bulk-reading the photos tree. |

**Design + status:** `.claude/skills/photo-context/DESIGN.md` settles the trigger (invoked-only, one photo or a
small batch), inputs (`photoindex find`, `photo_people`, `fha relate`, claim/place context), and the
provenance rule (AI-marked, human caption preserved). The core-tool gap it confirmed is closed:
`fha photoindex set-summary` (BUILD.md M3.5) writes the AI-marked `UserComment`, preserves human comment
text verbatim, previews with `--dry-run`, and is working-copy-aware. SPEC §20 already permitted the write,
so no SPEC amendment was needed - only the tool. Per `_STANDARD.md` §6 the SKILL.md was deferred until
that verb existed; writing `photo-context/SKILL.md` against the design is a separate, later skill-mode PR.
This layer flips to shipped only when that SKILL.md lands.

---

## Testing invariants (all phases)

There is no automated test harness for SKILL.md prose - skills are verified by session behavior. For every skill PR, confirm in a real session against `example-archive`:

1. The skill produces exactly the documented archive writes (suggested claims, recorded AI passes, view refreshes, confirm-driven entries) and **no** write the contract forbids - nothing reaches `accepted` without a human `fha claim`, nothing edits below a GENERATED header, no human text is overwritten.
2. Every AI pass is recorded in the source's `## AI Passes` block.
3. The skill degrades gracefully on messy input (loose dates, informal names) - it infers or asks one plain question, never hard-refuses (AGENTS.md "Who you serve").
4. The skill calls deterministic `fha` tools for everything a tool already owns; it adds only judgment.
5. `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9, mirroring TOOLING.md §15) after the skill runs - no new errors or warnings from anything the skill wrote.
