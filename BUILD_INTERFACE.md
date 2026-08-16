# BUILD_INTERFACE.md - the AI interface (workbench skills): build sequence

**Who this is for:** developers implementing the workflow **skills** that drive the `fha` tool suite. If you just want to use the archive, start with [`GETTING_STARTED.md`](GETTING_STARTED.md).

This file is the build guide for the **interface layer** - the `.claude/skills/` workflow skills and the harness conventions around them. It is the sibling of [`BUILD.md`](BUILD.md) (core `fha` tools) and [`BUILD_INGESTION.md`](BUILD_INGESTION.md) (capture / inbox on-ramp). Design rationale lives in [`TOOLING_INTERFACE.md`](TOOLING_INTERFACE.md); this file tells you the sequence and how to verify it.

**Status: all layers authored (I1-I7; the 2026-07 usability-review wave shipped `photo-context`, `find-photos`, `share-and-export`, and the `today` connection-reaction extension; the 2026-08 recordings wave shipped `import-recordings`, `transcribe-audio`, and the `mine-transcript` two-transcript extension).** The `.claude/skills/` directory now holds `_STANDARD.md` (the authoring contract) and fifteen SKILL.md files: `today`, `review-claims`, `process-source`, `mine-transcript`, `write-biography`, `research-next`, `place-research`, `merge-identities`, `reconcile-site-edits`, `photo-context`, `find-photos`, `share-and-export`, `import-notes`, `import-recordings`, and `transcribe-audio` - one per milestone entry below, `reconcile-site-edits` and `import-notes` under Layer I7. Each SKILL.md was authored against the shipped tools (every `fha` command it invokes was verified to exist) and against `AGENTS.md` / `_STANDARD.md`; the lint invariant holds (`fha lint --root example-archive` still exits 1 on the pre-existing baseline, unchanged by the skill prose). The remaining acceptance gate for each is the **behavioral session check** (run it against `example-archive`, capture the transcript) - marked per-milestone below. Building surfaced **two core-tool gaps**, both closed at the verb level and now at the skill level: MI3.1's merge verb (`fha confirm merge` shipped and the skill's interim hand-edit was retired) and MI4's UserComment write (`fha photoindex set-summary` shipped; `photo-context/SKILL.md` landed with the usability-review wave).

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

Stage C of the pipeline. Walk one source's `suggested` backlog in one of three styles - guided one-by-one (the default), batched (numbered groups of ~5 grouped by person; one grouped reply = one stated decision per numbered claim; offered proactively past ~6 suggested claims), or self-serve skimming of the source file - the human's choice. Every claim shown, in any style, carries its `anchor:` context AND its evidence link (the anchored asset or record path, then the `[[S-id]]` token); capture the human's accept / dispute / edit decision and any manual additions; write each decision with `fha claim` (which moves status and stamps `reviewed:` - directing the tool *is* the accept; a grouped same-status decision is one batch write, `fha claim C-a C-b ... --status X`, previewed once). Parking a claim at `needs-review` offers - explicit yes only - to record what would settle it (a SPEC §17 `## Q:` block or a `verify:` hypothesis); the close-out nudges promotion once for a decided-on direct-line stub at/over `promotion: claims_threshold:` (default 5), running `fha person promote` + the `write-biography` hand-off only on the yes (2026-07-23 revision). Finish with a reindex (full `fha index` - the `process-source`/`mine-transcript` hand-off usually minted new person stubs, which `fha index --source` does not index; `--source` is fine only for a status-only pass), `fha xref` to surface new corroboration/contradiction, a reindex again if any `fha confirm xref` link was written, a `fha views timeline`/`draft-queue` refresh for each curated person touched (stubs skipped; `views brackets` checked when a relationship claim was accepted), and `fha lint`.

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
- (Style addendum, 2026-07-22.) A session drafting in each named style (`chronicle` / `narrative`) on `example-archive` keeps the identical citation density and the lint baseline; the marker and AI-pass name the style; with no session ask and no `fha.yaml` `biography:` block the voice is `chronicle` - unchanged default behavior.

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

## Layer I3 - Frontier-tier skills (Milestone I3 - authored)

Cheap to attempt, expensive to get wrong - escalate to the frontier model tier (TOOLING_INTERFACE.md §1).

---

### MI3.1 - `merge-identities` skill

**One PR.** `.claude/skills/merge-identities/SKILL.md`.

**Status: authored** (`.claude/skills/merge-identities/SKILL.md`). The judgment half - pull both neighborhoods, lay out the evidence, propose, wait for human confirmation - is fully on shipped tools, and so is the write: the core gap this milestone surfaced (SPEC §9's merge write had no `fha` verb) **closed when `fha confirm merge` shipped** (BUILD.md M4.4a; audit Wave 3 / plan 16). The skill now drives the verb (dry-run first) and the interim hand-edit path was retired; `.claude/skills/merge-identities/GAP.md` remains as the historical record of the spec-discovery. Session check pending.

"Same person" / "two people" judgment. Read the candidate neighborhood (`fha find --related`, co-occurrence signals); propose a merge or a split with the evidence laid out; the human confirms. The mechanical write is `fha confirm merge`'s job - the skill never silently merges, and the split stays hand-guided (SPEC §9). A merged person is never directly referenced again (lint E016/W107 enforce this).

**Orchestrates:** `fha find --related`, `fha cooccur`, `fha confirm merge`, `fha lint` (E016/W107/W115 verification).

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

## Layer I4 - Skill backlog (Milestone I4 - shipped)

Ideas carried from the former TOOLING_INTERFACE.md §2.3 backlog. `photo-context`, its one entry, is
fully shipped: the core verb (`fha photoindex set-summary`, BUILD.md M3.5) landed first, and the
SKILL.md landed with the 2026-07 usability-review wave. The backlog is empty.

| Skill | Status | Sketch |
|---|---|---|
| `photo-context` | **shipped** (`.claude/skills/photo-context/SKILL.md`) | Update a photo's embedded AI summary (UserComment) with archive knowledge: identified people's relationships, the event/claim context, place history - captions get smarter as the archive grows. Writes marked as AI (SPEC §20); operates through `fha photoindex` and exiftool-via-tool, never bulk-reading the photos tree. |

**Design + status:** `.claude/skills/photo-context/DESIGN.md` settles the trigger (invoked-only, one photo or a
small batch), inputs (`photoindex find`, `photo_people`, `fha relate`, claim/place context), and the
provenance rule (AI-marked, human caption preserved). The core-tool gap it confirmed is closed:
`fha photoindex set-summary` (BUILD.md M3.5) writes the AI-marked `UserComment`, preserves human comment
text verbatim, previews with `--dry-run`, and is working-copy-aware. SPEC §20 already permitted the write,
so no SPEC amendment was needed - only the tool. Per `_STANDARD.md` §6 the SKILL.md was deferred until
that verb existed; it was then authored against the design (DESIGN.md keeps the history and records the
flip). Session check pending, like the other layers.

---

## Layer I5 - Usability-review session skills (Milestone I5 - authored)

The 2026-07 usability review (owner point 3: skill coverage) added the two conversational front doors
the persona report found missing, plus the `today` extension that completes the report's connection
loop. Design: TOOLING_INTERFACE.md §2.3.

### MI5.1 - `find-photos` skill

**Status: authored** (`.claude/skills/find-photos/SKILL.md`). Session check pending (behavioral transcript with the phase-2 landing).

The photo subsystem's front door: resolve "show me grandma's photos" to a P-id via `fha find`, answer from `fha photoindex find` at variation-group granularity in plain language, offer a clickable `fha photoindex gallery` page, and hand identification to `tag-person`'s own confirm prompt. Read-only.

**Orchestrates:** `fha find` (+ `--related`), `fha photoindex` (freshen when stale), `fha photoindex find`/`triage`/`gallery`, `fha photoindex tag-person` (hand-off).

**Done when:** person / date / topic / triage asks each produce a plain-language, group-level answer with zero archive writes; the gallery offer reports the exact file location and `file://` link.

### MI5.2 - `share-and-export` skill

**Status: authored** (`.claude/skills/share-and-export/SKILL.md`). Session check pending (behavioral transcript with the phase-2 landing).

The guided path for the privacy-sensitive act: route the request to `packet` / `gedcom` / `site --standalone` / `wikitree` / `backup`, speak each tool's privacy defaults in plain words before running, preview first, then report exactly what went out and what stayed home. Never adds an override flag unasked; importing a tree INTO the archive is a gedcom-import / migration conversation, not this skill.

**Orchestrates:** `fha packet`, `fha gedcom`, `fha site --standalone`, `fha wikitree`, `fha backup`.

**Done when:** each sharing phrase routes to the right tool with the privacy script spoken first; a living-person packet request is declined with the tool's reason translated, never worked around; every write is preceded by a preview and the report names the artifact path and the exclusions.

### MI5.3 - `today` connection-reaction extension

**Status: authored** (folded into `.claude/skills/today/SKILL.md` as flow step 6). Completes the loop `tools/report.py` §8 left to the skill layer: "yes, they were neighbors" → `fha confirm cooccur` (dry-run echoed first, minted `suggested` unless the human's flat, unhedged answer is the review); "no, stop suggesting that pair" → `fha confirm dismiss` (tombstone, reversible). No write without an explicit ruling.

## Layer I6 - Recordings skills (Milestone I6 - authored in a live archive, ported 2026-08-15)

Authored against real phone-app exports in a live archive on 2026-08-11..13 and ported here scrubbed of family specifics. Design: TOOLING_INTERFACE.md §2.4.

### MI6.1 - `import-recordings` skill

**Status: authored** (`.claude/skills/import-recordings/SKILL.md` + `scripts/attribute_speakers.py` + `scripts/find_duplicate_media.py` + `GAP.md`), with `tests/test_import_recordings.py` covering both scripts (the 0.90 confidence gate, the 80% timestamp gate with its tail-coverage and blind-span rules, the two-sided mispair gate with its 20-matched-word floor, the output-collision refusals, the refuse-before-publishing paths, the refusal to replace an existing `--out`/`--report` without `--replace` (kept separate from `--force`), and the size-then-SHA-256 dedupe including its fail-closed `indeterminate` verdict and exit 3). Session check pending on `example-archive` (needs a synthetic recording + app-transcript pair in the fixture; a whisper-free dry path is enough to exercise dedupe, dating, grouping and the `fha process` sequence).

The recordings on-ramp: dedupe by content, date from the container, group by sitting into one session source, always a fresh whisper pass beside the app transcript, speaker labels only under gates and speaker names only on the human's yes.

**Orchestrates:** `fha find` (and `fha search` for a same-sitting lead when the bytes differ), `fha process` (`--type interview --slug`, then `--more FILE ROLE` once per companion), `fha index`, `fha lint`; the skill's own `scripts/` for whisper, label transfer, and the size-then-SHA-256 duplicate check; `ffprobe` for the container probe. The last two are the interim enactments recorded in `GAP.md` (wanted: `fha media dedupe` #43, `fha media probe` #44 - core-tool backlog).

**Done when:** see the skill's own "Done when" - one sitting lands as one folder under one S-id with every companion attached by its own `--more` call; a byte-identical repeat is skipped and reported with the path it duplicates; a pair failing the 50% gate degrades to two plain transcripts; speaker → person is a table and no name is written until answered.

### MI6.2 - `transcribe-audio` skill

**Status: authored** (`.claude/skills/transcribe-audio/SKILL.md` + `scripts/transcribe_audio.py`), with the script's non-model logic covered by [`tests/test_transcribe_audio.py`](tests/test_transcribe_audio.py). The transcription itself requires `faster-whisper` on the machine that holds the audio and is not exercised in CI; the tests inject fake segment iterators instead, which is where the failure modes actually live.

Local re-transcription attached beside the original (both kept, always), the `--name` prefix rule that keeps a source's files together in a listing, and the offered claim-by-claim audit of facts mined from the garbled original.

The `--name` value is the source's **shared stem with no role suffix**: `attach_more` appends the role itself, so `--name …-whisper` files as `…-whisper-whisper-transcript_S-….md`. The reviewed `.md` is copied under the documents root before `--more` (which refuses a file filed anywhere else), and a filed name that looks wrong is reported, never renamed - `fha process` renames a documents-root file once and no verb renames it again, so the human reorganizes and `fha reconcile` re-ties.

Two script invariants the skill's batch advice rests on, both regression-tested: **all-or-nothing publication** (`.part` siblings renamed into place only after the segment iterator is exhausted; an interruption, a decode failure or a zero-speech run leaves no file behind, so a "skip what already exists" queue can never mistake a stump for a finished pass, and an existing output is a clean no-op unless `--force`) and a **portable `.md` header** (the recording is named by filename, never by the absolute path typed on the command line - AGENTS_TOOLING.md §11, SPEC §12.4).

**Orchestrates:** `scripts/transcribe_audio.py`, `fha process --more … whisper-transcript`, `fha claim <C-id> --value` for audited corrections, `review-claims` for new material.

```
python -m unittest tests.test_transcribe_audio -v   # atomic publish, portable header, documented commands
```

### MI6.3 - `mine-transcript` two-transcript extension

**Status: authored** (folded into `.claude/skills/mine-transcript/SKILL.md` step 1). Mine from the whisper/app comparison, anchor to the transcript actually quoted, and treat a coverage divergence as a signal the app truncated or mis-attached a file.

---

## Layer I7 - Skills authored outside the layered waves (Milestone I7 - authored)

Two skills landed with the work they serve rather than with a numbered skill wave, so they had no
milestone entry here while the header above already counted them. They are recorded here so every
one of the fifteen shipped SKILL.md files has a status line in this doc, which is the authoritative
build-status record for the interface layer. Design: TOOLING_INTERFACE.md §2.5.

### MI7.1 - `reconcile-site-edits` skill

**Status: authored** (`.claude/skills/reconcile-site-edits/SKILL.md`). Shipped with the static site's
Phase E escape hatch (docs/SITE_PLAN.md layer (e)): `fha site` is deterministic and never reads its own
output, so a hand-edited HTML page is overwritten on the next build. The skill reads the edited page,
diffs it against a pristine baseline build to recover the human's intent, folds that intent into the
real source (`.fha/design/custom.css`, `notes/home.md`, the person's record, or `fha.yaml`'s `site:`
block), and rebuilds. Every source write is human-confirmed first; `fha site` learns nothing.
Session check pending, like the other layers.

**Orchestrates:** `fha site` (baseline + rebuild), `fha person edit`, plain file edits to the named
sources - no new verb.

### MI7.2 - `import-notes` skill

**Status: authored** (`.claude/skills/import-notes/SKILL.md`). The legacy-notes on-ramp: chunk a pile of
freeform research notes, propose a home per chunk under the routing rule (evidence someone asserted →
inbox → source; a thing to find out → open question; a testable belief → hypothesis; a search already
run → research log; everything else → `notes/research/`), and write each chunk only on the human's
confirmation. It drafts no claims - evidence earns those later through `process-source` and
`review-claims` - and never deletes or rewrites the original notes. Session check pending.

**Orchestrates:** `fha process` (via the inbox hand-off to `process-source`), `fha find` for name
resolution, plain writes into `notes/questions.md` and `notes/research/` - no new verb.

---

## Testing invariants (all phases)

There is no automated test harness for SKILL.md prose - skills are verified by session behavior. The exception is a skill that ships a `scripts/` file: that code is tested like any other code, and the test may also pin the parts of its SKILL.md that are mechanically checkable - that every flag the prose names exists in the script's parser, and that a worked example really produces the filename it shows when run through the owning tool's naming rule (`tests/test_transcribe_audio.py` is the pattern). For every skill PR, confirm in a real session against `example-archive`:

1. The skill produces exactly the documented archive writes (suggested claims, recorded AI passes, view refreshes, confirm-driven entries) and **no** write the contract forbids - nothing reaches `accepted` without a human `fha claim`, nothing edits below a GENERATED header, no human text is overwritten.
2. Every AI pass is recorded in the source's `## AI Passes` block.
3. The skill degrades gracefully on messy input (loose dates, informal names) - it infers or asks one plain question, never hard-refuses (AGENTS.md "Who you serve").
4. The skill calls deterministic `fha` tools for everything a tool already owns; it adds only judgment.
5. `fha lint --root example-archive` still exits 1 with only the documented baseline warnings (`_STANDARD.md` §9, mirroring TOOLING.md §15) after the skill runs - no new errors or warnings from anything the skill wrote.
