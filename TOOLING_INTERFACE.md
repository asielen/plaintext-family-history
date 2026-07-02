# TOOLING_INTERFACE.md - The AI Interface: Research Workbench & Skills

**Who this is for:** developers building or extending the *AI-interface* side of the archive - the workbench harness configuration and the workflow skills that turn `fha` tools into a genealogy-aware research assistant. If you just want to use the archive, start with [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

**Version 1.2 - companion to SPEC.md v1.2 and TOOLING.md v1.2 (versions track the SPEC).**

This document is a focused expansion of one layer that the main [`TOOLING.md`](TOOLING.md) only sketches: the **harness + skills** layer that sits *on top of* the deterministic `fha` tool suite. TOOLING.md specifies the tools (schemas, algorithms, command shapes); this document specifies the AI interface that drives them - the workbench configuration that makes any conforming harness genealogy-aware, and the workflow skills that orchestrate `fha` calls plus model judgment into the everyday research loop.

The governing split, stated once and obeyed everywhere below: **deterministic work belongs in `fha` tools; AI judgment belongs in workbench skills; human review is the only gate to `accepted`.** A skill never reimplements what a tool does - it calls the tool and adds the judgment a tool cannot have. AI passes are always *invoked* and recorded - nothing mines, extracts, or classifies silently.

The three sibling design docs:

| Doc | Concern | Build doc |
|---|---|---|
| [`TOOLING.md`](TOOLING.md) | core `fha` tools (index, lint, views, find, process, site, …) | [`BUILD.md`](BUILD.md) |
| [`TOOLING_INGESTION.md`](TOOLING_INGESTION.md) | capture / inbox / web on-ramp | [`BUILD_INGESTION.md`](BUILD_INGESTION.md) |
| **`TOOLING_INTERFACE.md`** (this doc) | workbench harness + workflow skills | [`BUILD_INTERFACE.md`](BUILD_INTERFACE.md) |

---

## 1. The research workbench (harness configuration)

**Pattern (SPEC §6):** an agentic CLI harness opened on the archive root, beside a plain text editor - human and AI edit the same files.
Claude Code is the operating choice, not a required one.
The configuration below is what makes any conforming harness genealogy-aware, and what keeps the choice reversible.

**Vendor-lock prevention rules.**
1. **`AGENTS.md` is canonical.** All agent operating instructions live there, in plain markdown, harness-agnostic. `CLAUDE.md` is a one-line deferral (`Read and follow AGENTS.md.`) plus, at most, Claude-Code-specific notes. Any other harness's convention file (e.g. for Codex or Gemini CLI) gets the same one-line deferral.
2. **Skills are portable.** Workflow skills live in `.claude/skills/{name}/SKILL.md` using the open SKILL.md standard (adopted beyond Claude Code); they contain instructions and `fha` invocations only - no harness APIs.
3. **No harness-only state is load-bearing.** Session memory, harness caches, and MCP configurations are disposable; anything worth keeping is written into archive records. Switching harnesses must cost one afternoon, not a migration.
4. The harness's "knowledge" of the archive is the **index and the `fha` tools**, never bulk file ingestion - ten thousand photos cost zero context because photo questions are `fha photoindex` calls.

**External roots in the workbench.** When `fha.yaml` maps a root outside the archive, the harness needs access granted: for Claude Code, launch with `--add-dir <photos-root>` (the settings-file `additionalDirectories` route has had reliability reports; prefer the flag, e.g. in a small launch script committed next to fha.yaml).
The agent still must not bulk-read asset trees - access exists for exiftool/process/packet operations, not ingestion.

**Model selection (workbench economics).** Deterministic `fha` tools cost no model credits - the deterministic/judgment split is also the cost model.
For model work, tier by judgment density, not habit: the **workhorse tier** (currently Claude Sonnet) is the default for tool-building, processing, review, and drafting; the **frontier tier** (currently Claude Opus / Fable) is escalated per task for proof arguments, merge/separate judgment, brick-wall research, spec-refinement, and stuck debugging - the tell is *cheap to attempt, expensive to get wrong*; the **fast tier** (currently Claude Haiku) serves batch API pipelines only after a sample-quality bake-off (handwriting transcription degrades quietly on small models).
Switch per session (`/model`); the tiers are roles, not vendors - any harness's equivalents slot in.

**Workbench session hygiene** (enforced by AGENTS.md): run `fha lint` after any batch of edits; never bypass `fha process` for renames; new claims always `status: suggested` when AI-drafted; never edit below a GENERATED header.

---

## 2. The skills (the working surface)

Skills are the layer a genealogist actually touches - most `fha` commands are what skills shell into (TOOLING.md §17). Each skill is a `.claude/skills/{name}/SKILL.md`: portable instructions plus `fha` invocations, no harness APIs. A skill's job is the *judgment* around a deterministic tool - which claim to draft, which name resolves to which person, where to look next - never the bookkeeping the tool already owns.

Every skill obeys the contract (AGENTS.md): AI-drafted claims are `status: suggested`; only the human moves a claim to `accepted`; every AI pass is recorded; nothing edits below a GENERATED header or overwrites human-written text.

### 2.1 Initial skills (build alongside linter v1)

- `review-claims` - Stage C: walk a source's `suggested` backlog (guided one-by-one, or open the source file for self-serve skimming - human's choice); capture accept/dispute/edit and manual claim additions; set `reviewed`; finish with incremental reindex, `fha xref`, a timeline/draft-queue refresh for the touched curated persons (a `views brackets` check too when a relationship claim was accepted), and lint. The human gate from the engine side: the skill assesses and presents; the human's decision is written with `fha claim`.
- `process-source` - the pipeline driver. If the inbox item is a **source stub** (a `*.notes.md` sidecar or a bundle folder, SPEC §12.1), its frontmatter + notes seed Stage A (pre-filling §14 frontmatter) and its parsed-person/vital hints seed Stage B's draft; otherwise Stage A starts from the bare file. Stage A `fha process`; Stage B AI draft (file reading incl. vision, entity resolution with candidate proposals against the index, `suggested` claims + stories); hand-off to `review-claims` for Stage C, whose close-out (reindex, xref, view refresh, lint) finishes the pipeline. The stub is consumed - promoted into the source record, not left behind.
- `mine-transcript` - the invoked extraction pass: selective claim drafting (`suggested` + `anchor:`), name→P-id resolution against the index with candidate proposals for unresolved names (mint stubs on confirmation), stories to `## Stories`, the pass recorded in the source's `## AI Passes` block (model, date). Never runs unrequested.
- `today` - run `fha report`, narrate it discoveries-first, offer to start the top item (e.g. a `review-claims` session). Surfaced as the `/today` slash wrapper.
- `research-next` - inference and steering (checks the research log FIRST - never proposes a search already logged unless the nil has aged past the re-run horizon; emits plan-shaped output whose executed searches are logged back): combine open questions, vitals gaps, and open hypotheses with historical context (which record sets exist for the time/place, where they are held, what era events imply) into concrete research leads; may draft hypotheses (origin: agent) into research files - leads and hypotheses, never claims.
- `write-biography` - drafting rules for profiles: citation density (SPEC §16), uncited-prose-is-context, summary-block format, `[[P-…]]`/`[[S-…]]` links only from verified IDs. Consumes the `fha views draft-queue` backlog (TOOLING.md §14b); draft prose carries `<!-- AI-DRAFT … -->` markers until the human accepts it via `fha confirm draft`.

### 2.2 Further skills (frontier-tier candidates)

- `merge-identities` - "same person" / "two people" judgment. Frontier-tier: cheap to attempt, expensive to get wrong. Reads the candidate neighborhood (`fha find --related`, co-occurrence), proposes a merge or a split for human confirmation; the mechanical write is the deterministic tool's job, never the skill's silent action. A `merged_into` person is never directly referenced again (lint E016/W107).
- `place-research` - "fill in this place's history." Loose citations are acceptable here (place context is narrative scaffolding, not vital fact); drafts dated `history:` entries and place notes, links `[[L-…]]`, and proposes registry entries for `fha confirm place` to write.

### 2.3 Skill backlog

`photo-context` (below) now has a settled design - it is **blocked** on a core-tool gap, not undesigned; see [`BUILD_INTERFACE.md`](BUILD_INTERFACE.md) Layer I4 for the authoritative status.

| Idea | Sketch |
|---|---|
| `photo-context` skill | Update a photo's embedded AI summary (UserComment) with archive knowledge: identified people's relationships, the event/claim context, place history - the pipeline's captions get smarter as the archive grows. Writes are marked as AI per SPEC §20. |

---

## 3. Build status & milestones

The workflow skills are authored - `.claude/skills/` holds `_STANDARD.md` (the authoring contract) plus the SKILL.md files, with `photo-context` designed but blocked on a core-tool gap. Authoritative build status lives in [`BUILD_INTERFACE.md`](BUILD_INTERFACE.md); this document is the design it implements against, exactly as TOOLING.md is to BUILD.md and TOOLING_INGESTION.md is to BUILD_INGESTION.md.

The workbench harness configuration (§1) is not "built" in the tool-suite sense - it is documentation plus a few committed conventions (`AGENTS.md`, `CLAUDE.md`, the `--add-dir` launch script). Its "build" is keeping those conventions accurate as the harness landscape changes.
