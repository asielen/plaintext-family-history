# BUILD_INTERFACE.md - the AI interface (workflow skills) and the workbench: build sequence

**Who this is for:** developers implementing the workflow **skills** that drive the `fha` tool suite, and the `fha serve` workbench. If you just want to use the archive, start with [`GETTING_STARTED.md`](GETTING_STARTED.md).

This file is the order in which to build the **interface layer** from nothing, with the acceptance test for each step: the `.claude/skills/` workflow skills, the harness conventions around them, and the `fha serve` localhost workbench. It is the sibling of [`BUILD.md`](BUILD.md) (core `fha` tools) and [`BUILD_INGESTION.md`](BUILD_INGESTION.md) (capture and the inbox contract). Design rationale lives in [`TOOLING_INTERFACE.md`](TOOLING_INTERFACE.md); this file tells you the sequence and how to verify it.

The `.claude/skills/` directory holds `_STANDARD.md` (the authoring contract every SKILL.md follows) and seventeen SKILL.md files, one folder per skill: `today`, `review-claims`, `process-source`, `mine-transcript`, `write-biography`, `research-next`, `merge-identities`, `place-research`, `photo-context`, `find-photos`, `share-and-export`, `import-recordings`, `transcribe-audio`, `reconcile-site-edits`, `import-notes`, `setup-interview` and `transcribe-source` - each with its milestone below.

---

## What a skill is (conventions, all phases)

- **One folder per skill:** `.claude/skills/{name}/SKILL.md`, using the open SKILL.md standard (portable beyond Claude Code).
- **Instructions + `fha` invocations only.** No harness APIs, no MCP calls, no Python in the SKILL.md itself. A skill orchestrates deterministic tools and adds model judgment; it never reimplements what a tool already does (TOOLING_INTERFACE.md §2). A skill that needs code ships it as a script under its own `scripts/` folder, tested like any other code.
- **The contract is law** (AGENTS.md): AI-drafted claims are `status: suggested`; only the human moves a claim to `accepted` (always via `fha claim`, which stamps `reviewed:` and `reviewed_by:`); every AI extraction pass is recorded in the source's frontmatter `ai_passes:` list (SPEC §14); draft prose lives behind `<!-- AI-DRAFT … -->` markers; nothing edits below a GENERATED header or overwrites human text.
- **Sessions are an interface, not memory** (AGENTS.md): anything worth keeping is written into archive records in SPEC formats before the skill hands back.
- **No skill writes a computed gap as a question.** A gap the report computes is shown; a question is written into `notes/questions.md` only by the human or on the human's yes (SPEC §17).
- **Definition of done is behavioral.** A SKILL.md is verified by running it in a real session against a scratch copy of `example-archive/` and confirming it produces the documented archive writes and hand-offs - and **zero** writes the contract forbids. The "Done when" blocks below are those session checks; where a skill ships a script, the script's tests are named too.
- **Every `fha` command a skill names exists.** Before a SKILL.md lands, each command and flag it invokes is checked against the CLI (`fha <command> --help`), and each worked example that produces a filename is run through the owning tool's naming rule.
- **Vendor-lock rules hold** (TOOLING_INTERFACE.md §1): if a second harness is added, its convention file is a one-line deferral to AGENTS.md and the same SKILL.md files drive it unchanged.
- **Discoverable without a skill loader.** `AGENTS.md`'s "Playbooks" subsection points any harness at `.claude/skills/{name}/SKILL.md` and tells it to read and follow the matching one when a request matches its trigger, so a harness that reads `AGENTS.md` but has no skill loader still finds them.

**Dependency note.** Every skill depends only on `fha` commands from BUILD.md and BUILD_INGESTION.md; there is no skill→skill import. The ordering below is by daily-loop centrality (build the session spine first), not by hard dependency. `process-source` *hands off* to `review-claims`, so build `review-claims` no later than `process-source`. A skill whose core verb does not exist yet is not authored (`_STANDARD.md` §6): the verb is built first, in BUILD.md.

---

## Milestones

| Milestone | Layer | Phases | What it builds |
|---|---|---|---|
| I1 | Layer I1 - The session spine | MI1.1-MI1.3 | `today`, `review-claims`, `process-source` |
| I2 | Layer I2 - Drafting & inference | MI2.1-MI2.3 | `mine-transcript`, `write-biography`, `research-next` |
| I3 | Layer I3 - Frontier-tier skills | MI3.1-MI3.2 | `merge-identities`, `place-research` |
| I4 | Layer I4 - Photo context | MI4.1 | `photo-context` |
| I5 | Layer I5 - Conversational front doors | MI5.1-MI5.3 | `find-photos`, `share-and-export`, the `today` connection-reaction step |
| I6 | Layer I6 - Recordings | MI6.1-MI6.3 | `import-recordings`, `transcribe-audio`, the `mine-transcript` two-transcript step |
| I7 | Layer I7 - Escape hatch, legacy notes, first run | MI7.1-MI7.3 | `reconcile-site-edits`, `import-notes`, `setup-interview` |
| I8 | Layer I8 - The image-only gap | MI8.1-MI8.2 | `transcribe-source`, the `process-source` Stage A½ step |
| I9 | Layer I9 - The workbench | MI9.1 | `fha serve` |

---

## Layer I1 - The session spine (Milestone I1)

The three skills a genealogist touches every session: open the workbench, process new material, review what was drafted.

---

### MI1.1 - `today` skill (`/today`)

**One PR.** `.claude/skills/today/SKILL.md`; the folder name is the `/today` wrapper the harness surfaces. The reference skill for `_STANDARD.md`.

Run `fha report`, then narrate it **discoveries-first** (the report is a research narrative before a chore list), and offer to start the top item - most often a `review-claims` session on the oldest `suggested` backlog, or processing the inbox. §6b is eligible for that one slot too: when its largest **unlinked** place-text cluster carries more claims than §1's oldest backlog has suggested ones (or §1 is empty), it wins and the offer hands off to `place-research` at its step 3 (the unlinked-cluster arm - a cluster has no `L-id` for step 1's neighborhood pull). A relative comparison of two counts the briefing already printed, never a hardcoded number, never a §6b GPS line's photo count; the offer says "unlinked", which is what §6b reports, not "unregistered", which this skill runs no registry lookup to establish. The skill reads the report; it does not recompute it. It writes nothing on its own except, on the human's say-so, a `fha confirm discovery` entry for a confirmed win.

**Orchestrates:** `fha report` (read), `fha confirm discovery` (on confirmation), then `review-claims` / `process-source` / `place-research` (the next-action hand-offs).

**Done when:**
- In a session on `example-archive`, `/today` narrates report sections 0-8, leads with discoveries, and offers a concrete next action.
- It makes no archive write unless the human confirms one; a confirmed discovery lands in `notes/discoveries.md` via `fha confirm discovery`.

---

### MI1.2 - `review-claims` skill

**One PR.** `.claude/skills/review-claims/SKILL.md`. The reused accept-gate interaction.

Stage C of the pipeline. Walk one source's `suggested` backlog in one of three styles - guided one-by-one (the default), batched (numbered groups of ~5 grouped by person; one grouped reply = one stated decision per numbered claim; offered proactively past ~6 suggested claims), or self-serve skimming of the source file - the human's choice. Every claim shown, in any style, carries its `anchor:` context AND its evidence link (the anchored asset or record path, then the `[[S-id]]` token); capture the human's accept / dispute / edit decision and any manual additions; write each decision with `fha claim` (which moves status and stamps `reviewed:` - directing the tool *is* the accept; a grouped same-status decision is one batch write, `fha claim C-a C-b ... --status X`, previewed once). A claim the human wants to come back to stays `suggested` with a `reviewed:` date, and - explicit yes only - what would settle it is recorded on the SPEC §17 research ladder (a question, or a working answer with its `verify:`). The close-out nudges promotion once for a decided-on stub at/over `promotion: claims_threshold:` (default 5) or flagged as a direct-line stub, running `fha person promote` (direct-line) or `fha person promote --into connections/` (anyone else, SPEC §12.3) + the `write-biography` hand-off only on the yes. It nudges place registration on the same terms: the largest `fha places candidates` cluster **holding a claim decided on in this session** - same scoping as promotion, so an untouched cluster stays report §6b's business rather than re-firing every run - at/over 10 claims, its town looked up with `fha find --json … --kind place` before the offer is spoken (unlinked is not unregistered), then `fha confirm place` `--into` an existing place or minting a new one, only on the yes. Finish with a reindex (full `fha index` - the `process-source`/`mine-transcript` hand-off usually minted new person stubs, which `fha index --source` does not index; `--source` is fine only for a status-only pass), `fha xref` to surface new corroboration/contradiction, a reindex again if any `fha confirm xref` link was written, a `fha views timeline`/`draft-queue` refresh for each curated person touched (stubs skipped; `views brackets` checked when a relationship claim was accepted), and `fha lint`. A place nudge answered yes writes *after* that done-gate and `fha confirm place` does not reindex, so it reopens the close-out: `fha index` + `fha places lint` + `fha lint` again.

**Orchestrates:** `fha claim`, `fha confirm xref`, `fha index`, `fha xref`, `fha views timeline`/`draft-queue` (touched persons), `fha lint`.

**Guardrails:** never moves a claim to `accepted` without the human; `accepted` always carries `reviewed:` (E006). The skill presents judgment; the human gates.

**Done when:**
- Reviewing a source's suggested claims in a session results in `fha claim` writes for each decision, a clean reindex, an `fha xref` pass, a timeline/draft-queue refresh for each curated person touched, and `fha lint` exiting on its real findings.
- No claim reaches `accepted` without an explicit human decision in the transcript.

---

### MI1.3 - `process-source` skill

**One PR.** `.claude/skills/process-source/SKILL.md`. Depends on MI1.2 (hands off to it).

The pipeline driver. If the inbox item is a **source stub** (a `*.notes.md` sidecar or a bundle folder - the inbox contract, BUILD_INGESTION MG0.1), its frontmatter + notes seed Stage A (pre-filling SPEC §14 frontmatter) and its parsed-person/vital hints seed Stage B's draft; otherwise Stage A starts from the bare file. Stage A is `fha process`; Stage B is the AI draft (read the file incl. vision, resolve names/places against the index with candidate proposals, draft `suggested` claims with `anchor:`s, pull `## Stories`); then hand off to `review-claims` for Stage C, whose close-out (reindex, xref, view refresh, lint) finishes the pipeline. Place resolution goes by the **settlement name** (`fha find --json "<town>" --kind place` matches `name:`/`alt_names:`, never `hierarchy:`), an unlinked place stays a legitimate permanent state, and a text whose `fha places candidates` cluster already carries 10+ claims earns one explicit-yes-only registration offer - checked against the registry before it is spoken, so an already-registered town is a `--into` merge, not a duplicate mint. That write reaches past the item in hand, so it carries its own `fha index` + `fha places lint` there rather than deferring to the close-out. The stub is **consumed** - promoted into the source record, not left behind. Must handle loosely-written notes gracefully (AGENTS.md): extract what it can, fold the rest into `## Notes`, never stall on imperfect prose.

**Orchestrates:** `fha process`, `fha id mint` (via process), `fha stubs` (unresolved names), `fha find --json … --kind place` / `fha places candidates` / `fha confirm place` + `fha index` + `fha places lint` (the place-registration offer), then `review-claims` (whose close-out owns the rest of the reindex/xref/views/lint).

**Done when:**
- Processing an inbox stub in a session yields a real `sources/…` record with `suggested` claims + anchors, the stub consumed, the pass recorded in `ai_passes:`, and a hand-off into `review-claims`.
- A loosely-written note (approximate dates, informal spellings) processes without a hard refusal; unmappable prose lands in `## Notes`.

---

## Layer I2 - Drafting & inference (Milestone I2)

The skills invoked by name when the human wants prose written or leads found.

---

### MI2.1 - `mine-transcript` skill

**One PR.** `.claude/skills/mine-transcript/SKILL.md`. **Never runs unrequested.**

The invoked extraction pass over a transcript: selective claim drafting (`suggested` + `anchor:` - substantive assertions only), name→P-id resolution against the index with candidate proposals for unresolved names (mint stubs on confirmation), narrative chunks to `## Stories`, the rest left in the transcript (it is preserved and searchable; extraction is indexing, not preservation). Record the pass in the source's `ai_passes:` list (date, model, task).

**Orchestrates:** `fha stubs`, `fha id mint`, `fha index --source`, `fha lint`.

**Done when:**
- Mining a transcript in a session drafts suggested claims with anchors, routes stories to `## Stories`, records the pass, and leaves the transcript text intact.
- The skill takes no action unless explicitly invoked.

---

### MI2.2 - `write-biography` skill

**One PR.** `.claude/skills/write-biography/SKILL.md`.

Drafting rules for profiles: facts only from `accepted` claims; cite every factual sentence (summary block: one citation per line; body: all relevant citations); anything uncited must read as story/context; `[[P-…]]`/`[[S-…]]` links only from verified IDs. Consumes the `fha views draft-queue` backlog (uncited accepted claims). Draft prose is written inside `<!-- AI-DRAFT … -->` markers; on human acceptance, `fha confirm draft <P-id>` flips the marker to `<!-- AI-ACCEPTED … -->` (provenance preserved). Pronouns come from `gender`, else `sex`. Two named styles, `chronicle` (the default) and `narrative`, chosen in the session or by `fha.yaml`'s `biography:` block. A stub target is offered promotion first (`fha person promote`).

**Orchestrates:** `fha views draft-queue`, `fha find` (verify IDs), `fha person promote`, `fha confirm draft`.

**Done when:**
- Drafting a bio in a session pulls from the draft queue, cites every factual sentence with a verified `[[S-…]]`, wraps new prose in AI-DRAFT markers, and never overwrites human-written text.
- Acceptance flips markers via `fha confirm draft`, not by hand-editing.
- A session in each style keeps the identical citation density and the lint baseline; the marker and the pass name the style; with no session ask and no `biography:` block the voice is `chronicle`.

---

### MI2.3 - `research-next` skill

**One PR.** `.claude/skills/research-next/SKILL.md`.

Inference and steering. **Checks the research log FIRST** - never proposes a search already logged unless the nil has aged past the re-run horizon (default 18 months). Combines open questions, vitals gaps, and working answers with historical context (which record sets exist for the time/place, where they are held, what era events imply) into concrete research leads. May write a working answer (`guess:`, `basis:`, `verify:`) on the SPEC §17 research ladder, marked as the agent's - leads and guesses, never claims. Executed searches are logged back to `notes/research-log.md` (SPEC §17c).

**Orchestrates:** `fha report` / index queries (gaps, questions, working answers), the research log, `fha lint`.

**Done when:**
- Asking "where should I look for X?" in a session produces log-aware leads (already-searched annotations present), and any drafted guess is marked as the agent's, never a claim.
- No lead duplicates a recent logged nil.

---

## Layer I3 - Frontier-tier skills (Milestone I3)

Cheap to attempt, expensive to get wrong - escalate to the frontier model tier (TOOLING_INTERFACE.md §1).

---

### MI3.1 - `merge-identities` skill

**One PR.** `.claude/skills/merge-identities/SKILL.md`. Needs BUILD.md M4.4a (`fha confirm merge`).

"Same person" / "two people" judgment. Read the candidate neighborhood (`fha find --related`, co-occurrence signals); propose a merge or a split with the evidence laid out; the human confirms. The mechanical write is `fha confirm merge` (dry-run first) - the skill never silently merges, and the split stays hand-guided (SPEC §9). A merged person is never directly referenced again (lint W107 lists any reference left; it still counts for the survivor meanwhile, SPEC §9).

**Orchestrates:** `fha find --related`, `fha cooccur`, `fha confirm merge`, `fha lint` (W107/W115 verification).

**Done when:**
- A merge/split proposal in a session lays out the neighborhood evidence and waits for human confirmation; post-merge, lint shows no W107 regressions.

---

### MI3.2 - `place-research` skill

**One PR.** `.claude/skills/place-research/SKILL.md`.

"Fill in this place's history." Loose citations are acceptable (place context is narrative scaffolding, not vital fact). Draft dated `history:` entries and place notes, link `[[L-…]]`, and propose registry entries for `fha confirm place` to write. Never edits `places.yaml` coordinates without human confirmation (AGENTS.md).

**Orchestrates:** `fha find --related <L-id>`, `fha places candidates`, `fha confirm place`, `fha places set` / `note`.

**Done when:**
- Researching a place in a session drafts dated history with `[[L-…]]` links and proposes registry writes via `fha confirm place`; no coordinate is changed without confirmation.

---

## Layer I4 - Photo context (Milestone I4)

### MI4.1 - `photo-context` skill

**One PR.** `.claude/skills/photo-context/SKILL.md` (+ `DESIGN.md`, which settles the trigger, inputs and provenance rule). Needs BUILD.md M3.5 (`fha photoindex set-summary`).

Invoked only, for one photo or a small batch: gather what the archive knows (identified people and how they are related, the event and claim context, the place's history), draft a better summary, and - only after the human approves the exact text - write it as an AI-marked caption (`AI: …` in `UserComment`, SPEC §20) via `fha photoindex set-summary`. A human-written comment is preserved verbatim and the AI text added below it; never bulk, never by reading the photos tree directly.

**Orchestrates:** `fha photoindex find`, `fha relate`, `fha find --related`, `fha photoindex set-summary`.

**Done when:**
- In a session, a caption request produces a drafted summary, writes nothing until the human approves the exact text, and the written comment is AI-marked with any human text above it intact.

---

## Layer I5 - Conversational front doors (Milestone I5)

The conversational entry points to the photo catalog and to sharing, plus the step that completes the report's connection loop. Design: TOOLING_INTERFACE.md §2.3.

### MI5.1 - `find-photos` skill

**One PR.** `.claude/skills/find-photos/SKILL.md`.

The photo subsystem's front door: resolve "show me grandma's photos" to a P-id via `fha find`, answer from `fha photoindex find` at variation-group granularity in plain language, offer a clickable `fha photoindex gallery` page, and hand identification to `tag-person`'s own confirm prompt. Read-only.

**Orchestrates:** `fha find` (+ `--related`), `fha photoindex` (freshen when stale), `fha photoindex find`/`triage`/`gallery`, `fha photoindex tag-person` (hand-off).

**Done when:** person / date / topic / triage asks each produce a plain-language, group-level answer with zero archive writes; the gallery offer reports the exact file location and `file://` link.

### MI5.2 - `share-and-export` skill

**One PR.** `.claude/skills/share-and-export/SKILL.md`.

The guided path for the privacy-sensitive act: route the request to `packet` / `gedcom` / `site --standalone` / `wikitree` / `backup`, speak each tool's privacy defaults in plain words before running, preview first, then report exactly what went out and what stayed home. Never adds an override flag unasked; importing a tree INTO the archive is a gedcom-import conversation, not this skill.

**Orchestrates:** `fha packet`, `fha gedcom`, `fha site --standalone`, `fha wikitree`, `fha backup`.

**Done when:** each sharing phrase routes to the right tool with the privacy script spoken first; a living-person packet request is declined with the tool's reason translated, never worked around; every write is preceded by a preview and the report names the artifact path and the exclusions.

### MI5.3 - `today` connection-reaction step

**One PR.** A flow step in `.claude/skills/today/SKILL.md`. Completes the loop `fha report` §8 leaves to the skill layer: "yes, they were neighbors" → `fha confirm cooccur` (dry-run echoed first, minted `suggested` unless the human's flat, unhedged answer is the review); "no, stop suggesting that pair" → `fha confirm dismiss` (tombstone, reversible).

**Done when:** a narrated connection answered yes or no produces exactly one `fha confirm cooccur` or `fha confirm dismiss` write, previewed first; no write happens without an explicit ruling.

---

## Layer I6 - Recordings (Milestone I6)

Design: TOOLING_INTERFACE.md §2.4. Needs BUILD.md M11.8 (`fha media`).

### MI6.1 - `import-recordings` skill

**One PR.** `.claude/skills/import-recordings/SKILL.md` + `scripts/attribute_speakers.py` (+ `GAP.md`, the record of the core verbs it needed).

The recordings on-ramp: dedupe by content (`fha media dedupe`), date from the container (`fha media probe`), group by sitting into one session source under `documents/interviews/{interviewee}-{yyyy-mm-dd}/`, always a fresh whisper pass beside the app transcript, speaker labels only under gates and speaker names only on the human's yes. The script's gates: the 0.65 confidence gate with one-vote-per-word pooling (two methods covering the same span must not each claim it), the 80% timestamp gate with its tail-coverage and blind-span rules, the two-sided mispair gate with its 20-matched-word floor; it refuses before publishing on any failed gate, never replaces an existing `--out`/`--report` without `--replace`, pre-flights every destination before any alignment or write, and prints recovery commands that run as printed.

**Orchestrates:** `fha find` (and `fha search` for a same-sitting lead when the bytes differ), `fha media dedupe`, `fha media probe`, `fha process` (`--type interview --slug`, then `--more FILE ROLE` once per companion), `fha index`, `fha lint`; the skill's own `scripts/` for whisper and label transfer only.

**Done when:** one sitting lands as one folder under one S-id with every companion attached by its own `--more` call; a byte-identical repeat is skipped and reported with the path it duplicates; a pair failing the 50% gate degrades to two plain transcripts; speaker → person is a table and no name is written until answered.

```
python -m unittest tests.test_import_recordings tests.test_media
```

### MI6.2 - `transcribe-audio` skill

**One PR.** `.claude/skills/transcribe-audio/SKILL.md` + `scripts/transcribe_audio.py`. The transcription needs `faster-whisper` on the machine that holds the audio and is not run in CI; the tests inject fake segment iterators instead, which is where the failure modes live.

Local re-transcription attached beside the original (both kept, always), the `--name` prefix rule that keeps a source's files together in a listing, and the offered claim-by-claim audit of facts mined from the garbled original.

The `--name` value is the source's **shared stem with no role suffix**: `attach_more` appends the role itself, so `--name …-whisper` files as `…-whisper-whisper-transcript_S-….md`. The reviewed `.md` is copied under the documents root before `--more` (which refuses a file filed anywhere else), and a filed name that looks wrong is reported, never renamed - `fha process` renames a documents-root file once and no verb renames it again, so the human reorganizes and `fha reconcile` re-ties.

Two script invariants: **all-or-nothing publication** (`.part` siblings renamed into place only after the segment iterator is exhausted; an interruption, a decode failure or a zero-speech run leaves no file behind, and an existing output is a clean no-op unless `--force`) and a **portable `.md` header** (the recording is named by filename, never by an absolute path - AGENTS_TOOLING.md, SPEC §12.4).

**Orchestrates:** `scripts/transcribe_audio.py`, `fha process --more … whisper-transcript`, `fha claim <C-id> --value` for audited corrections, `review-claims` for new material.

**Done when:**
```
python -m unittest tests.test_transcribe_audio -v   # atomic publish, portable header, documented commands
```

### MI6.3 - `mine-transcript` two-transcript step

**One PR.** Step 1 of `.claude/skills/mine-transcript/SKILL.md`. Mine from the whisper/app comparison, anchor to the transcript actually quoted, and treat a coverage divergence as a signal the app truncated or mis-attached a file.

**Done when:** mining a session with two transcripts anchors each claim to the transcript it quotes and reports any coverage divergence before drafting.

---

## Layer I7 - Escape hatch, legacy notes, first run (Milestone I7)

Design: TOOLING_INTERFACE.md §2.5 (MI7.1-MI7.2) and §2.7 (MI7.3).

### MI7.1 - `reconcile-site-edits` skill

**One PR.** `.claude/skills/reconcile-site-edits/SKILL.md`. `fha site` is deterministic and never reads its own output, so a hand-edited HTML page is overwritten on the next build (docs/SITE_PLAN.md layer (e)). The skill reads the edited page, diffs it against a pristine baseline build to recover the human's intent, folds that intent into the real source (`.fha/design/custom.css`, `notes/home.md`, the person's record, or `fha.yaml`'s `site:` block), and rebuilds. Every source write is human-confirmed first; `fha site` learns nothing.

**Orchestrates:** `fha site` (baseline + rebuild), `fha person edit`, plain file edits to the named sources.

**Done when:** a hand-edited page in a session is traced back to one source change per intent, each confirmed before writing, and a rebuild reproduces the edit.

### MI7.2 - `import-notes` skill

**One PR.** `.claude/skills/import-notes/SKILL.md`. The legacy-notes on-ramp: chunk a pile of freeform research notes, propose a home per chunk under the routing rule (evidence someone asserted → inbox → source; a thing to find out → a question; a testable belief → a working answer; a search already run → the research log; everything else → `notes/research/`), and write each chunk only on the human's confirmation. It drafts no claims - evidence earns those later through `process-source` and `review-claims` - and never deletes or rewrites the original notes.

**Orchestrates:** `fha process` (via the inbox hand-off to `process-source`), `fha find` for name resolution, plain writes into `notes/questions.md`, `notes/research-log.md` and `notes/research/`.

**Done when:** a notes file in a session is split into chunks, each routed only on a yes, with the original untouched.

### MI7.3 - `setup-interview` skill

**One PR.** `.claude/skills/setup-interview/SKILL.md`. The first-run on-ramp: `fha install` stamps a brand-new archive and asks the new owner nothing, so the one family it is guaranteed to have authoritative information on - his own - would otherwise start unrecorded. The skill asks him, once, right after install: who he is (mints his own person record, `sex:` set); how he wants the tree numbered (`root_generation: self | children`, written into `fha.yaml`); and his immediate family - parents, spouse(s), children. It writes one `source_class: authored` interview source and drafts `relationship` claims with explicit `roles:` maps for parentage (never `birth` claims) plus a `marriage` claim per spouse, every claim `status: suggested`, then hands off to `review-claims` - the interview proposes, it never self-accepts. `fha install`'s "Next steps" points here.

**Step 0 - the place-backlog offer.** Before the family questions, one silent, one-time check: an archive that already carries imported material (`fha gedcom import` run first) can reach this interview with a real backlog of unlinked `place_text` and zero registered places. `fha index` then `fha places candidates --include-suggested` surfaces the clusters (`--include-suggested`, because an import mints everything `suggested`); past a 20-claim bar, the skill offers once to register the biggest cluster(s) via `fha confirm place`, then reindexes and runs `fha places lint`. "Not now" or silence drops it for the session; a registry that already has an entry, or every cluster below the bar, means the check says nothing.

**Orchestrates:** `fha index`, `fha places candidates --include-suggested`, `fha confirm place`, `fha places lint`, `fha person new`, `fha person set-sex`, `fha person set-living`, `fha process`, hand-drafted claims (the same Stage-B judgment `process-source` performs), `review-claims`.

**Done when:** on a freshly installed archive, the session mints the owner's record, writes `root_person`/`root_generation`, files one authored interview source whose claims are all `suggested` with explicit `roles:`, and hands off to `review-claims`.

---

## Layer I8 - The image-only gap (Milestone I8)

An image-only source contributes no text to the archive, so `fha find --text` searches only what an earlier pass wrote into a claim value while looking exactly like a search of the evidence. The deterministic half of the answer is in BUILD.md (lint W124, the coverage note `fha find --text` prints, `role: transcript` companions loaded into `transcripts_fts`). The half that cannot be deterministic - somebody reads the pictures - is this layer. Design: TOOLING_INTERFACE.md §2.6.

### MI8.1 - `transcribe-source` skill

**One PR.** `.claude/skills/transcribe-source/SKILL.md` (+ `GAP.md`).

Reads an image-only source and writes out what it says as a `[Page N]`-labeled `role: transcript` companion in the original's pagination. `fha source extract` is tried first on any PDF so the model never re-reads text the file already holds. Two entry points: from `process-source` **between Stage A and Stage B** (MI8.2), and standalone for backfill in confirmed batches of five sources or fewer, whose worklist is re-read from `fha lint` (W124) each time rather than carried in session memory.

The skill **drafts and edits no claims**. Unclaimed facts hand off to `mine-transcript`; a reading that contradicts an existing claim - **including an `accepted` one** - becomes a question in `notes/questions.md`, never a silent correction. That contradiction report is the most valuable thing it produces.

**The unreviewed-transcript marker contract** (what `fha find --text` reads to label a hit `[unchecked AI transcript]`) reuses `<!-- AI-DRAFT … -->` / `<!-- AI-ACCEPTED … -->` rather than inventing a third convention - four states, *unreviewed* outranking *verified*, *damaged* failing closed to *unreviewed*, one marker as the last non-blank line of a heading-free body. Full statement: TOOLING_INTERFACE.md §2.6 and the skill's own "The marker" section. No verb flips the marker yet (`fha confirm draft` edits person profiles only), so every AI transcript stays *unreviewed*; the wanted verb is recorded in `transcribe-source/GAP.md`, and a skill never hand-edits a marker.

**Orchestrates:** `fha find`, `fha source extract`, `fha process --more … transcript`, `fha index`, `fha source note`, `fha lint` (W124 as its worklist and done-gate).

**Done when:**
- On a scratch copy: W124 fires on an image-only source with an accepted claim; `fha process --more <file> transcript` files `{stem}.md` as `{stem}-transcript_{S-id}.md`; `fha index` puts it in `transcripts_fts`; `fha find --text` returns a phrase that was unfindable before and its coverage count drops by one; W124 clears.

```
python -m unittest tests.test_transcribe_source -v   # documented commands, naming rule, marker contract
```

### MI8.2 - `process-source` Stage A½ step

**One PR.** Stage A½ of `.claude/skills/process-source/SKILL.md`, between Stage A and Stage B. When `fha process` has just filed an item whose files are all images, the transcript is written **before** any claim is drafted, so Stage B drafts from text somebody read out line by line rather than from a single pass over pictures. Stage B's "text layer first" advice for large PDFs names the same `transcribe-source` path for the all-image case.

**Done when:** processing an all-image item in a session produces the transcript companion before any `suggested` claim is drafted.

---

## Layer I9 - The workbench (Milestone I9)

### MI9.1 - `fha serve` - the localhost workbench

**One PR** (or one per bullet group below). New file `tools/serve.py`. Wire `fha serve [--port N]
[--no-browser]` (default port 8765; port 0 picks a free one), plus the `serve.cmd` launcher
(TOOLING_INTERFACE.md §1b; TOOLING.md §12 for the site it wraps). Needs: `fha site --linked`
(BUILD.md M8), `fha find --json` (M2.5), and every write-back engine its buttons drive.

**What it is.** A human-started, foreground, 127.0.0.1-only web front door onto the archive: it
serves the linked (unredacted) `fha site` build over HTTP and adds an editing layer. It is a
**front door, not an engine**: `serve.py` is one of the two sanctioned places a tool imports
other tools (with `fha.py`), driving their `run_*` engines in-process; `site.py` is loaded by
path under a private name, as in `fha.py`. Not a daemon: no auth, no network beyond the loopback
socket, no file watcher, and an AI agent never starts it.

**The parity rule.** Every button is exactly one documented `fha` command, run in-process
through its `run_*` engine, and every apply echoes the literal CLI command it ran. The verb table
(`VERBS`, each entry a schema + engine call + echo + the reindex it needs):

| Verb | Command it runs |
|---|---|
| `claim.review`, `claim.new` | `fha claim …`, `fha claim new …` |
| `confirm.xref`, `confirm.cooccur`, `confirm.dismiss` | `fha confirm xref / cooccur / dismiss` |
| `person.new`, `person.set-living`, `person.set-sex`, `person.set-profile-photo`, `person.relate`, `person.estimate`, `person.edit`, `person.note`, `person.edit_note` | the matching `fha person` verb |
| `person.add_family` | `fha person new` (when the relative has no record yet) then `fha person relate --reciprocal` - both echoed |
| `source.note`, `source.edit_note` | `fha source note / edit-note` |
| `place.set`, `place.note`, `place.edit_note` | `fha places set / note / edit-note` |
| `process.file` | `fha process` |
| `capture.path` | `fha capture --path` |
| `site.publish` | `fha site --standalone` |
| `index.rebuild` | `fha index` |
| `home.edit` | a bounded write of `notes/home.md` - the same edit as opening the file; dry-run shows the diff |

Two conveniences inside the rule: relate and add-family buttons pass `--reciprocal` (the echo
names it; the CLI default is unchanged), and `/api/pickfile` opens the OS's own file dialog so
the register-by-path form gets a full path - the pick itself reads and writes nothing.

**Routes.** `GET` serves the snapshot pages, `/review` (the claim-review queue), `/inbox` (the
inbox, pairing each file with its sidecar), and `/root/…` (an archive file, confined).
`POST /api/run` runs a verb; `/api/find` is the search box (`fha find --json`); `/api/reindex`;
`/api/open` opens a confined file in the OS; `/api/upload` writes a dropped file into `inbox/`
with an optional `<stem>.notes.md` sidecar in the BUILD_INGESTION MG0.1 shape (the "what" text
as its body, the "who" names under `people:`); `/api/pickfile`.

**The human gate is a click.** Every mutating request is a dry-run preview first, applied only by
a second explicit confirmation; the server itself defaults `dry_run` to true unless the POST says
`{"dry_run": false}`. An accept through the workbench stamps `reviewed:` exactly as the CLI does.
The workbench never reads evidence to draft anything - Stage B stays with the skills.

**Security is the whole trust boundary** (`run_serve_preflight` and the handler): bind
127.0.0.1 only; a Host-header allowlist on every request; a per-process CSRF token compared with
`hmac.compare_digest` on every POST; canonical-path confinement (resolve + `is_relative_to`) on
snapshot files, `/root/` aliases, `/api/open`, `/api/upload` and the `process.file` argument;
`no-store` + `nosniff` headers; one global mutation lock held across mutate → reindex →
snapshot-invalidate.

**Refresh on use.** The read side is a snapshot of `fha site --linked` under `.cache/serve/`,
rebuilt only when a page is requested and the record trees changed since the last build (an
mtime probe plus a path manifest, so a deleted record is caught too) - never on a timer. That
snapshot is the only thing serve writes outside what its engines write; deleting `serve.py` and
`.cache/serve/` changes nothing about the archive, and stopping the process is always safe.

**Done when:**
```sh
fha serve --root <scratch-archive> --no-browser    # prints the 127.0.0.1 URL; Ctrl-C exits cleanly
# a person page's "set living" button previews, then applies, and echoes `fha person set-living …`
# a POST without the CSRF token, or with a foreign Host header, is refused
python -m unittest tests.test_serve
```

---

## Testing invariants (all phases)

Skill prose has no automated harness - skills are verified by session behavior. The exception is a skill that ships a `scripts/` file: that code is tested like any other code, and the test may also pin the parts of its SKILL.md that are mechanically checkable - that every flag the prose names exists in the script's parser, and that a worked example really produces the filename it shows when run through the owning tool's naming rule (`tests/test_transcribe_audio.py` is the pattern). `fha serve` is tested like any other tool (`tests/test_serve.py`). For every skill PR, confirm in a real session against a scratch copy of `example-archive`:

1. The skill produces exactly the documented archive writes (suggested claims, recorded AI passes, view refreshes, confirm-driven entries) and **no** write the contract forbids - nothing reaches `accepted` without a human `fha claim`, nothing edits below a GENERATED header, no human text is overwritten.
2. Every AI extraction pass is recorded in the source's `ai_passes:` list (SPEC §14).
3. The skill degrades gracefully on messy input (loose dates, informal names) - it infers or asks one plain question, never hard-refuses (AGENTS.md "Who you serve").
4. The skill calls deterministic `fha` tools for everything a tool already owns; it adds only judgment.
5. `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9, mirroring TOOLING.md §15) after the skill runs - no new errors or warnings from anything the skill wrote.
