# tools/

The `fha` command suite lives here. Run via `python tools/fha.py <command>` from the repo
root, or `python tools/<tool>.py` for standalone use.

These tools are **generic**: they operate on any spec-conforming archive and contain no family data.
They are the "replaceable glue" of the philosophy - disposable, regenerable from the spec, and safe to publish.
`TOOLING.md` (repo root) is the design document for every tool; consult it before changing any behavior.

Two **out-of-tree companions** live beside this suite, installed into a user's setup rather than vendored by `fha install` and never load-bearing: [`browser-companion/`](../browser-companion/) (web capture) and [`obsidian-templater/`](../obsidian-templater/) (optional Obsidian Templater templates for new person/source notes; see [docs/USING_WITH_OBSIDIAN.md](../docs/USING_WITH_OBSIDIAN.md)).

CLI recovery contract: `fha` prints help on no args; unknown subcommands get a
"did you mean?" hint; ordinary failures explain the cause and the next command
to try; tracebacks are hidden by default and shown only with the global
`--debug` flag for tool-building diagnostics. Shared messages cover missing
PyYAML, malformed `fha.yaml`, missing archive roots, missing exiftool, bad
`source_type` values, and bad archive-date/EDTF values.

`Result` contract (the headless core): every command splits its engine from its
interface. The engine (`run_*`) computes and returns a `_lib.Result` - a small,
JSON-serializable record carrying `ok`, `exit_code`, `data` (the structured
payload), `messages` (human-facing `Message{level, text, next_step, code, path}`
lines), and `changed` (paths created/written/renamed/embedded, empty under
`--dry-run`). The interface (`_cmd_*`) is the only layer that renders a `Result`
to stdout/stderr and returns the exit code. `lint` is the reference
implementation. This is what lets any front door - a terminal, an agent shelling
out, the in-process report orchestrator, a future UI - drive the same engine and
read the same structured result (TOOLING §1).

## Implemented tools (milestone 2)

| Tool | File | Status |
|---|---|---|
| `fha views timeline` | `views.py` | ✓ per-person and --all-curated |
| `fha views sources-index` | `views.py` | ✓ per-person, --all-curated, --couple-folders |
| `fha views draft-queue` | `views.py` | ✓ per-person and --all-curated |
| `fha views brackets` | `views.py` | ✓ W103 bracket refresh, W110 Ahnentafel placement; `--fix` applies, `--dry-run` previews |
| `fha views tree` | `views.py` | ✓ ancestors/descendants/fan modes; `--format json\|dot`; `--generations N`; `--out FILE`; `--format html` deferred (D6) |
| `fha doctor` | `doctor.py` | ✓ all 12 checks; D5 applied (absent index/photoindex = warning, not error); restricted-source counts use the open-marker predicate on both the index and scan paths; a failing staged-captures check degrades to a warning line instead of killing the report |
| `fha find <ID>` | `find.py` | ✓ P/S/C/L/H id types; structured index path when present; tree-scan fallback when absent; `[restricted]` label covers typed values (`dna`, `by-request`, …); `--root` without fha.yaml is refused (exit 3, the shared `_lib.resolve_root_arg` guard) |
| `fha find --text "…"` | `find.py` | ✓ notes_fts + re.search; photo captions searched when photoindex is fresh (else skip-note); `transcripts_fts` created but not yet populated - transcript search deferred (D7) |
| `fha search <words>` | `find.py` | ✓ alias for `fha find --text`; positional phrase joined with spaces (`fha search rose hartley`), same engine/output |
| `fha find --related <ID> [--date EDTF]` | `find.py` | ✓ BUILD.md M4.3 (D4) - neighborhood query for all five ID types, plus a standalone `--related --date EDTF` time slice. Requires a real index (exit 3 if absent/unreadable - no tree-scan fallback, unlike find_by_id) |
| `fha relate <P-A> <P-B>` | `relate.py` | ✓ blood relationship (LCA over genetic edges: cousin/removal/lineal/aunt-uncle) + shortest social path (BFS over all edges); `--json`; structured `Result`. Requires a real index (exit 3 if absent/unreadable). ⚑ `--include-hypotheses` deferred (index derives only accepted edges) |
| `fha id check <ID>` | `fha.py` alias | ✓ re-routed through `find.find_by_id` in fha.py dispatcher; root resolves through the shared `_lib.resolve_root_arg` guard (`--root` without fha.yaml is refused, exit 3, nothing created) |

Views require a fresh `.cache/index.sqlite` (run `fha index` first). The per-person timeline/sources-index/draft-queue forms skip a stub person with a plain note and exit 1 - companion views are curated-person files (SPEC §16); covered by `tests/test_views_stub_guard.py`. `fha find` uses the index when present, warns when it is stale, and falls back to a tree scan only when the index is absent or unreadable; `fha doctor` degrades gracefully without caches. Both `.cache/index.sqlite` and `.cache/photos.sqlite` carry a `meta.schema_version` row plus `PRAGMA user_version`; missing, old, corrupt, or unreadable caches are treated as disposable and rebuilt by `fha index` / `fha photoindex`.
Generated files carry the `<!-- GENERATED … -->` header and must not be hand-edited.

## Implemented tools (milestone 3)

| Tool | File | Status |
|---|---|---|
| `fha photoindex [--full]` | `photoindex.py` | ✓ M3.1 - schema, exiftool scan (incremental by mtime/size; `--full` rescans all), variation grouping, person resolution |
| `fha photoindex find` | `photoindex.py` | ✓ M3.2 - `--person`/`--keyword`/`--edtf`/`--text` filters (AND'd at the group level when combined); one path per group by default, `--files` for raw rows |
| `fha photoindex triage` | `photoindex.py` | ✓ M3.3 - ranks unprocessed (no `source_id`) groups by evidence signals; `--top N` (default 10) |
| `fha photoindex report` | `photoindex.py` | ✓ M3.3 - lists `photo_groups` with `date_conflict=1` and each variant's date/caption |
| `fha photoindex reconcile [--with-exif]` | `photoindex.py` | ✓ M3.4 - re-matches a moved file by its embedded `SOURCE:` keyword (`--with-exif` only); unmatchable rows are flagged `MISSING:` in the cache; new on-disk files are counted, not scraped; `photo_fts` is re-keyed alongside every other path-keyed table |
| `fha photoindex tag-person <P-id> [--from-face-tag TAG \| --paths PATH...] [--dry-run]` | `photoindex.py` | ✓ M3.4 - preview -> interactive `[y/N]` confirm (or `--dry-run`) -> one `exiftool -keywords+=` write per candidate -> `photo_people`/`photo_keywords`/`photo_fts` cache update for whichever candidates' writes succeeded |

## Implemented tools (milestone 4)

| Tool | File | Status |
|---|---|---|
| `fha xref` | `xref.py` | ✓ M4.1 - corroboration/contradiction candidate pairs: same person + same claim `type` + different source + not already linked (`claim_links`); classified by `edtf_bounds` overlap, plus a vital-type (`birth`/`death`/`marriage`) place mismatch check when bounds overlap (`place_text`, falling back to conservative place phrases in `value`). Read-only; never writes `claim_links`. Absent/unreadable index → exit 3; stale → warns, still queries. |
| `fha cooccur [--threshold N]` | `cooccur.py` | ✓ M4.2 - three candidate detectors: (1) person co-occurrence - person-pairs sharing ≥`--threshold` (default 2) sources via `source_people` ∪ `claim_persons` participants, excluding pairs with an existing `relationships` edge or a dismissed-tombstone entry (`.cache/cooccur_dismissed.json`, read-only), ranked by source count then source-type variety; (2) shared-place co-occurrence - accepted/needs-review claims of different, unlinked people sharing a place (`place_id` if both have one, else normalized `place_text`) with overlapping EDTF bounds, same exclusion rules as person co-occurrence; (3) org/entity recurrence - `occupation`, `military`, and membership-style `event`/`note` claims grouped by `(category, normalized value)`, emitted when ≥2 people or ≥2 sources share the same category/value hub. Read-only; never mints claims or writes the tombstone. Same absent/unreadable/stale handling as `xref`. |
| `fha find --related <ID> [--date EDTF]` | `find.py` | ✓ M4.3 - see "fha find - implementation status" below |

`fha xref` and `fha cooccur` follow the TOOLING §14a/§14a2 "deterministic candidates,
human-confirm gate" discipline: they only print suggestions. The human-confirm write-back
they leave open now lives in **`fha confirm`** (see "fha confirm - implementation status"
below): confirming an xref pair writes the `corroborates:`/`contradicts:` links, confirming
a co-occurrence pair mints a `relationship` claim, and dismissing a pair writes the
`.cache/cooccur_dismissed.json` tombstone `fha cooccur` reads. The detection tools
themselves stay read-only - the write owner is `fha confirm`, not the detector. `fha find
--related` (M4.3) is purely a read query over the data those two tools (plus `relationships`
and `claim_links`) already populate - it writes nothing.

## Implemented tools (milestone 5)

| Tool | File | Status |
|---|---|---|
| `fha report [--full] [--section NAME]` | `report.py` | ✓ M5.1-M5.3 - see "fha report - implementation status" below |

`fha report` is the one tool that calls other tools' logic directly in-process
(`index.build_index`, `lint._run_lint_core`, `photoindex.run_scan`/`run_triage`,
`cooccur.run_cooccur` - BUILD.md M5.1: "call tool logic directly, not subprocess").
Every other tool in the suite follows "tools never import other tools"; `report.py`
is the documented orchestrator exception, not a precedent for any other tool to
start importing siblings.

## fha report - implementation status

| Section | Status | Notes |
|---|---|---|
| Refresh sequence | ✓ | `index.build_index` → `photoindex.run_scan(full=False)` → `lint._run_lint_core`, every run regardless of `--full`/`--section` (index is rebuilt first so the photo scan and lint both see this session's records); the build's Result messages (malformed places.yaml coords) render as an "Archive notes from this refresh" block at the top of the report and ride `result.messages` - printed, never exit-changing (report's exit code stays the lint verdict). An explicit `--root` naming a folder without fha.yaml is refused before any refresh (exit 3, nothing created - the shared `_lib.resolve_root_arg` guard) |
| §0 Discoveries | ✓ | Claims flipping `needs-review`→`accepted`; new `claim_links` `corroborates` rows; questions newly `status: answered`; profiles newly vital-complete (left the W101 set); newly confirmed `relationships` edges. Diffed against `.cache/last_report.json`, which stores per-claim status, `claim_links`, `relationships`, the W101 person-id set, and per-question status (a superset of the minimal example in BUILD.md/TOOLING §15a - the extra fields are what a real transition diff needs) |
| §1 Review queue (W102) | ✓ | Suggested claims grouped by source, sources ordered by oldest claim `date_min` |
| §2 New since last session | ✓ | Source-id / claim-id + changed-claim / person-id diff vs. snapshot |
| §3 Vitals gaps (W101) | ✓ | Reuses lint's findings from the same refresh pass |
| §4 Contradictions (E009) | ✓ | Reuses lint's findings from the same refresh pass |
| §5 Search-log awareness | ✓ | Annotates leads (W101/suggested-claim/E009 persons) from `search_log`; nil searches older than 18 months flagged "worth re-running (stale nil search)". Populated by `## Research Log` entries (person research files, `notes/research-log.md`) and by `fha capture`. Capture rows always carry `person_id IS NULL` (a stub isn't reconciled to a person yet) so they can never match a lead above; a separate "Recently captured (not yet linked to a person)" call-out lists the last 30 days' worth so they stay visible until `fha process` resolves the stub, rather than only existing silently in the table |
| §5b Answerable questions | ✓ | Open `notes/questions.md` questions whose referenced `[C-id]` is now `accepted`, or whose referenced `[P-id]` now has all its required-vitals accepted claims; proposals only - printed, never executed |
| §6 Photo triage | ✓ | Embeds `photoindex.run_triage(top=10)`; absent/unreadable photo index reported, not treated as an error |
| §6b Place candidates | ✓ | `fha places` (BUILD.md M6.2) is built; this section calls `places.run_candidates()` directly - unlinked place-text clusters and GPS clusters, or a "none found" note |
| §7 Hypotheses & draft queues | ✓ | `hypotheses` table is provisioned but not yet populated by any tool - reports "no open hypotheses" until something writes to it; draft-queue backlog reads `person_files` companion files for non-placeholder content |
| §8 Possible connections | ✓ | Embeds `cooccur.run_cooccur(threshold=2)` - person pairs, shared-place pairs, and org/entity recurrence hubs, top 10 each, with `[confirm] [dismiss]` labels. Acting on a label is now `fha confirm cooccur` / `fha confirm dismiss`; `fha report` itself still only prints |
| `--full` | ✓ | Treats the snapshot baseline as empty for diffing; still writes a fresh snapshot afterward |
| `--section NAME` | ✓ | Narrows stdout to one section; the persisted snapshot and `.cache/report_{date}.md` always hold the complete report |
| `notes/discoveries.md` append | ✓ (via `fha confirm discovery`) | TOOLING §15a describes appending confirmed discoveries there with human confirmation. `fha report` itself still only prints; the human-confirmed append is `fha confirm discovery "<text>" [--refs …]` |

Automated tests: `tests/test_report.py` builds a tiny on-disk archive fixture (not a
synthetic index - `fha report` rebuilds from files) and exercises the vitals-gap →
accepted-claim discovery transition, the new-source diff, an unchanged second run's
empty discoveries section, `--section` filtering, the unknown-section error path, and
the place-candidates/photo-triage deferral/absent-cache messages.

## fha xref / fha cooccur - implementation status

| Feature | Status | Notes |
|---|---|---|
| Corroboration/contradiction classification | ✓ | Bounds-overlap via `edtf_bounds`; vital types additionally compared on normalized `place_text`, falling back to conservative place phrases in `value`, when bounds overlap |
| Already-linked exclusion | ✓ | Any existing `claim_links` row between the two claims (either rel) suppresses the candidate |
| Person co-occurrence ranking | ✓ | `(source_count desc, source_type variety desc)` |
| Existing-relationship exclusion | ✓ | Any `relationships` row between the pair (either direction) suppresses the candidate |
| Dismissed-pairs tombstone | ✓ (read-only) | `.cache/cooccur_dismissed.json`; missing file = empty set, not an error; `fha cooccur` never writes it - `fha confirm dismiss` is the writer |
| Shared-place co-occurrence | ✓ | Different, unlinked people's claims sharing a place (`place_id` else normalized `place_text`) with overlapping EDTF bounds; same exclusion rules as person co-occurrence |
| Org/entity recurrence | ✓ | Groups `occupation`, `military`, and membership-style `event`/`note` claims by `(category, normalized value)` |
| `--threshold N` | ✓ | Minimum distinct shared sources for a person co-occurrence candidate (default 2); rejects `< 1` |

Automated tests: `tests/test_xref.py`, `tests/test_cooccur.py` (stdlib `unittest`) build a
synthetic `.cache/index.sqlite` directly from `index.py`'s `_DDL` schema and exercise
corroboration/contradiction classification, same-source and already-linked exclusion,
threshold filtering, existing-relationship exclusion, the dismissed-tombstone read path,
and org-recurrence grouping - without needing a full archive fixture or `exiftool`.
`tests/test_find.py` follows the same synthetic-index pattern for `fha find --related`:
all five ID-type neighborhoods, the standalone and combined `--date` forms, the
`--related` dispatch sentinel (typed-with-no-value vs. not-typed-at-all), and the
absent-index/invalid-ID/invalid-EDTF failure paths.

## fha claim - implementation status

The single deterministic claim-review write-back (TOOLING §3b, SPEC §8.2, AGENTS.md): move one
claim's `status:` and stamp `reviewed:`. The human gate from the engine side - `review-claims`
and the report's prompts drive it, but the accept decision is always the human's. The edit is a
surgical text edit of the one named claim's entry inside its source `.md` `## Claims` block
(sibling claims, key order, comments preserved); the claim is located by scanning `sources/`
directly, so it works when `.cache/index.sqlite` is stale or absent. `run_claim` returns a
`_lib.Result` with `changed[]`.

| Command | Flags | Status | Notes |
|---|---|---|---|
| `fha claim <C-id> --status accepted\|disputed\|rejected\|needs-review\|superseded` | `--reviewed DATE`, `--value "…"`, `--date EDTF`, `--root`, `--dry-run` | ✓ | The five `--status` choices are the SPEC §8.1 review outcomes. `--status accepted` always stamps `reviewed:` (the given `--reviewed`, else today) - only the human moves a claim to `accepted` (lint E006), and directing this tool *is* the human's decision; the tool never accepts on its own. `disputed`/`rejected`/`superseded` change status rather than delete (trail preserved; `disputed` = actively contested vs. a ruled-out `rejected`). `--value`/`--date` correct the claim in the same surgical edit; a YAML block-scalar `value:` is refused, not corrupted. The claim is located by its OWN `id:` key line with a parse-back cross-check, so an `id: C-…` quoted inside another claim's block scalar (`notes: \|`) never draws the edit onto the quoting claim. A pre-existing duplicate C-id refuses with the E001 repair path (`fha id mint C`), not the corruption warning. Missing C-id → exit 1; re-run `fha index` after a write |

Automated tests: `tests/test_claim.py` (stdlib `unittest`) covers the surgical-edit helpers and
each status transition (the only target claim touched, comments preserved), plus the
`accepted`-stamps-`reviewed` rule (explicit date and today-default), the block-scalar refusal,
malformed/unknown-C-id refusals, a `--dry-run`-writes-nothing case, and an end-to-end
`fha index` + `fha lint` integration check that the status change is reflected on a real archive.

## fha confirm - implementation status

The deterministic write-back layer for the detection tools and the report's confirm/dismiss
prompts (TOOLING §14a/§14a2/§15a, AGENTS.md). Every verb is a *human-directed* write: the
detection tools propose, `fha confirm` writes back the human's pick. Source/registry edits
are surgical text edits (sibling claims, key order, comments preserved), records are located
by scanning `sources/`/`people/` directly (works when `.cache/index.sqlite` is stale or
absent), every verb ships `--dry-run`, and each returns a `_lib.Result` with `changed[]`.

| Subcommand | Flags | Status | Notes |
|---|---|---|---|
| `fha confirm xref <C-a> <C-b>` | `--as corroborates\|contradicts`, `--root`, `--dry-run` | ✓ | Writes the link **reciprocally** into both claims' source records; a `contradicts` confirm also spawns an `origin: tool` open question in `notes/questions.md` referencing both C-ids (so lint E009 stays satisfied, same template as `fha lint --spawn-questions`). Each claim is located by its OWN `id:` key line with a parse-back cross-check, so an `id: C-…` quoted inside another claim's block scalar never receives the link (both directions land on the right claims); a pre-existing duplicate C-id refuses with the E001 repair path (`fha id mint C`), not the corruption warning. Already-linked → no-op `already`; missing claim → exit 1; self-link / bad C-id → exit 3 |
| `fha confirm cooccur <P-a> <P-b>` | `--source S-id` (req), `--subtype friend\|associate\|neighbor` (req), `--accept`, `--reviewed DATE`, `--root`, `--dry-run` | ✓ | Mints a `relationship` claim (with `roles:` for E015, cited to `--source`) into that source's `## Claims` block. **Default `suggested`** - the human's confirm proposes; acceptance into a load-bearing `relationships` edge still goes through step-05 review (`fha claim … --status accepted`). `--accept` mints `accepted` + stamps `reviewed:` (today unless given), the only path that yields a derived edge on re-index. **Idempotent:** if the source already holds a live relationship claim for the same pair + subtype (any status except `rejected`/`superseded`), it is never duplicated - without `--accept` (or when it is already `accepted`) the run reports no-op `already` naming the existing C-id + status and writes nothing; **with `--accept` on a still-`suggested`/`needs-review` claim the run promotes that existing claim to `accepted` (stamping `reviewed:`)** so the flag is honored rather than dropped - a rejected/superseded claim never blocks a fresh confirm. The gate reads `persons:` in every taught link form (bare `P-x`, quoted `"[[P-x]]"`/`"[[P-x\|Name]]"`, and the unquoted `[[P-x]]` nested-list YAML shape); a plain-name entry (`"[[Sam Rivera]]"`) cannot block without an alias map and fails toward a visible duplicate, never a skipped mint |
| `fha confirm dismiss <P-a> <P-b>` | `--root`, `--dry-run` | ✓ | Appends the unordered pair to `.cache/cooccur_dismissed.json` (lowercased ids, deduped) - the tombstone `fha cooccur` reads. No re-index needed. Already-dismissed → no-op `already`; a corrupt tombstone is treated as disposable cache and rebuilt |
| `fha confirm place <C-id> [<C-id> …]` | `--name NAME`, `--hierarchy TEXT`, `--into L-id`, `--root`, `--dry-run` | ✓ | Registers a place-text cluster: mints a new `L-id` place block in `places/places.yaml` (or merges into an existing one via `--into`) **and** relinks the named claims' `place:` to it, so the cluster stops surfacing as an unlinked `fha places candidates` group. Claims are located by their OWN `id:` key line with a parse-back cross-check (a quoted `id: C-…` inside a block scalar never gets the relink); a pre-existing duplicate C-id refuses with the E001 repair path (`fha id mint C`). A missing C-id aborts before any write |
| `fha confirm discovery "<text>"` | `--refs IDS` (comma-separated S-/P-/C-/L-/H-), `--root`, `--dry-run` | ✓ | Appends `- YYYY-MM-DD: <text> [refs]` to `notes/discoveries.md` (the research-wins log `fha report` §0 leads with) |
| `fha confirm draft <P-id>` | `--root`, `--dry-run` | ✓ | Flips a curated profile's `<!-- AI-DRAFT … -->` markers to `<!-- AI-ACCEPTED … (accepted DATE) -->`, preserving the original date/model (provenance kept, §20). No marker found → exit 1/`none` |

Automated tests: `tests/test_confirm.py` (stdlib `unittest`) covers the pure-text edit helpers
(link append/idempotence, scalar set, claim append, AI-DRAFT flip) and each verb against a copy
of `example-archive/`: confirm-xref → `claim_links` present after re-index; contradiction → no
E009; accepted cooccur → derived `friend` edge (suggested → none); dismiss → pair excluded from
the next `fha cooccur`; place mint/relink + `--into`; discovery append; draft flip - each with a
`--dry-run`-writes-nothing and an invalid/not-found case.

## fha photoindex - implementation status

| Feature | Status | Notes |
|---|---|---|
| Schema (`.cache/photos.sqlite`) | ✓ | `photos`, `photo_groups`, `photo_keywords`, `photo_face_regions`, `photo_people`, `photo_fts`; face regions cache XMP names/types/area JSON so weak person resolution can be rebuilt without re-scraping unchanged images |
| Scan - incremental | ✓ | Skips re-scraping a file via exiftool when `(path, mtime, size)` is unchanged; removes cache rows for files no longer on disk. Existing compatible caches without `photo_face_regions` get one backfill scrape; incompatible/corrupt disposable caches are recreated |
| Scan - `--full` | ✓ | Bypasses the incremental check, rescans every file |
| Variation grouping | ✓ | Pass 1: shared `SOURCE:` keyword. Pass 2: same directory + same filename `base_id` (`_lib.parse_media_filename`). `is_primary`, `variant_copy`, `variant_role` populated; grouping is recomputed in full on every scan |
| Date resolution (`edtf_resolved`, `date_conflict`) | ✓ | Best-confidence variant wins ties broken by the group's primary file, then by path; non-overlapping bounds across variants set `date_conflict=1` |
| Person resolution | ✓ | Rebuilt every scan from cached keywords, source records, and face regions. Four tiers (authoritative first): `pid-keyword` (bare P-id keyword in the image file, regex-only, no index needed) and `source-people` (person listed in the source record's `people:` field - authoritative even with no face regions, enabling the no-photo-manager path) → `face-tag` (face region name matched against `person_face_tags`, skipped if ambiguous) → `name-match`. `face-tag`/`name-match` require a fresh `.cache/index.sqlite`; absent/stale/unreadable index degrades to `pid-keyword`/`source-people` only |
| `fha photoindex find` | ✓ (BUILD.md M3.2) | `--person P-id` (must be a P-id - wrong-type or malformed ids are rejected), `--keyword TERM` (case-insensitive substring), `--edtf EDTF` (must be valid EDTF; bounds-overlap against each photo's own `edtf`), `--text "…"` (`photo_fts`); filters AND together **at the group level** (a filter matching any variant matches the whole logical photo). Default dedupes matches to one row per group (`primary_path`); `--files` returns every raw row of each matched group (including sibling variants that didn't themselves match). Absent/unreadable/incompatible-schema `.cache/photos.sqlite` → clear error, exit 3; stale → warns but still queries |
| `fha photoindex triage [--top N]` | ✓ (BUILD.md M3.3) | Candidates = `photo_groups` rows where no member photo has `source_id` set. Score (TOOLING §15b): +3 any member has a caption, +2 any member's `photo_people` row is `via='pid-keyword'`, +1 any member's `edtf` has no `~`/`?` marker (`Y!` confidence or better), +1 any member's `variant_role` starts with `back`, −2 no caption anywhere in the group **and** some member's `user_comment` starts with `AI:`/`Model:`. Sorted by `(-score, primary_path)`; `--top` (default 10) caps the list. Same absent/unreadable/stale handling as `find` |
| `fha photoindex report` | ✓ (BUILD.md M3.3) | Lists every `photo_groups` row with `date_conflict=1`, plus each member photo's `edtf` and `caption` - a front/back date disagreement is a research finding, not something to average away. Same absent/unreadable/stale handling as `find` |
| `fha photoindex reconcile` | ✓ (BUILD.md M3.4) | Compares cached paths to what's on disk. A stored path missing on disk is, with `--with-exif`, re-matched against untracked files by their embedded `SOURCE:` keyword (silent path update, including dependent `photo_keywords`/`photo_face_regions`/`photo_people`/`photo_fts` rows - `_RECONCILE_TABLES`); without `--with-exif`, or when no source_id/no unique match exists, the row's path (and its `photo_fts` row) is prefixed `MISSING:` and reported. Already-`MISSING:`-prefixed rows are left alone on later runs (a human is expected to act, or the next ordinary scan's cache-removal pass clears a resolved one). New on-disk files with no claimed missing row are counted (`new_count`) but never scraped - that stays `fha photoindex`'s job. Exit: `EXIT_WARNINGS` when any row is left `MISSING:`, else `EXIT_CLEAN` |
| `fha photoindex tag-person` | ✓ (BUILD.md M3.4) | `<P-id>` plus exactly one of `--from-face-tag TAG` (every photo whose cached `photo_face_regions.name` equals TAG) or `--paths PATH...` (resolved against cataloged alias-form paths). Already-tagged candidates (`via='pid-keyword'` for that P-id) are reported separately and excluded from the write list. Preview is always printed; `--dry-run` stops there, otherwise an interactive `Tag these photos? [y/N]` confirm gates the write. On `y`: one `exiftool -keywords+=P-id -overwrite_original_in_place` call **per candidate** (not batched - a locked/unwritable file's failure must not hide a sibling's successful write), then `photo_keywords`/`photo_people` (`via='pid-keyword'`)/`photo_fts.keywords` are updated immediately for every path that wrote successfully - no rescan required to see the new match. Any per-path failures are returned alongside the successes and reported as a non-zero exit without touching the cache for the failed paths |

Test fixture: `tests/fixtures/photo-fixture/` - 4 placeholder JPEGs with real embedded metadata (written via exiftool, not a code-level stub): a front/back variation pair with disagreeing `DATE:` keywords (exercises `date_conflict`), a photo carrying a `SOURCE:` keyword (exercises source-id grouping), and one ungrouped photo.

Automated tests: `tests/test_photoindex.py` (stdlib `unittest`, no new dependency) monkeypatches `photoindex._run_exiftool` (and, for tag-person, `_run_exiftool_write`/`builtins.input`) to inject canned JSON rows or simulated confirm answers over a copy of the fixture, covering grouping/date-conflict/pid-keyword resolution, face-region caching, stale-index-disables-weak-resolution behavior, fresh-index weak-resolution refresh from cached regions, `--full` vs. incremental scan equivalence, reconcile's rematch/missing/untracked outcomes (including `photo_fts` re-keying), and tag-person's plan/confirm/dry-run/write paths (including `photo_fts` refresh and per-candidate partial-failure handling). Run with `python -m unittest tests.test_photoindex -v` from the repo root. This is the first `.py` test file in the repo; no test runner is wired into CI yet.

## Implemented tools (milestone 6)

| Tool | File | Status |
|---|---|---|
| `fha packet <P-id> [-o out/] [--include-research] [--include-restricted] [--include-dna] [--no-photos] [--dry-run] [--overwrite]` | `packet.py` | ✓ M6.1 - see "fha packet - implementation status" below |
| `fha places lint` / `fha places candidates [--threshold N]` / `fha places geocode [--place L-id] [--all] [--offline]` | `places.py` | ✓ M6.2-M6.3 - see "fha places - implementation status" below |
| `fha gedcom [<P-id>] [--mode descendants\|ancestors\|connected] [--generations N] [--all] [--include-living] [--out FILE]` | `gedcom.py` | ✓ M6.4 - see "fha gedcom - implementation status" below |
| `fha gedcom import <file.ged> [--apply] [--plan-out FILE]` | `gedcom_import.py` (dispatcher-intercepted in `fha.py`) | ✓ M6.6 - see "fha gedcom import - implementation status" below |
| `fha wikitree <P-id> [--out FILE]` | `wikitree.py` | ✓ M6.5 - see "fha wikitree - implementation status" below |

## Implemented tools (milestone 7)

| Tool | File | Status |
|---|---|---|
| `fha process FILE|FOLDER [--type TYPE] [--title …] [--date DATE] [--slug SLUG] [--people P-IDS] [--more FILE ROLE[:copy]] [--dry-run]` | `process.py` | ✓ M7.1-M7.4 - single-file documents and photos + `--more`; `--people` records known P-ids on photos at intake; folder triage + tier-1 variation detection (M7.3); `notes.md` bundle dissolution (M7.4); see "fha process - implementation status" below |
| `fha capture [--url URL] [--title …] [--type TYPE] [--date DATE] [--asset FILE] [--ingest [DIR]] [--host] [--install-host [--extension-id ID] [--host-manifest-dir DIR] [--browser chrome\|edge]] [--dry-run]` | `capture.py`, `capture_recipes/` | ✓ MG1.1-MG1.3 - paste-fallback web capture into an inbox source stub; generic recipe + Ancestry/FamilySearch/Newspapers.com/FindAGrave site recipes; MG2.1 `--ingest` sweeps browser-staged bundles into the inbox; MG2.3 `--host`/`--install-host` are the native-messaging host (§5.7); see "fha capture - implementation status" below |
| `fha convert-mining [--apply]` | `convert_mining.py` | ✓ M7.5 - one-time legacy transcript-mining migration into conformant sources/claims/stubs/questions; dry-run by default; **hidden from the top-level `fha --help` listing** (no `help=` on its `add_parser`) as a one-owner migration, but fully runnable; see "fha convert-mining - implementation status" below |

## Implemented tools (milestone 8)

| Tool | File | Status |
|---|---|---|
| `fha site [--out PATH] [--standalone \| --linked] [--dry-run]` | `site.py`, `templates/` (incl. `templates/vendor/`) | ✓ M8.1-M8.5 - static-HTML explorer: source page (M8.1), curated person page (M8.2), place + discoveries pages (M8.3), home page (surname A-Z + discoveries teaser) + standalone redaction audit (M8.4), interactive descendant/ancestor trees via a vendored renderer (M8.5), with social/adoptive edges drawn distinctly from the genetic line (SPEC §12.2). See "fha site - implementation status" below |

`fha site` reads structured data only from `.cache/index.sqlite` (so the site is
as fresh as the last `fha index`), reads biography/Stories prose from the curated
person `.md` and the citation text from the source `.md` frontmatter (neither is
in the index), and reads the photo strip from `.cache/photos.sqlite` when fresh.
It writes only to the output directory (default `generated/site/`), never to the
archive. **Dependencies:** Jinja2 (required); Pillow (optional - standalone image
derivatives use it when present; without it the standalone site omits images
rather than copying originals, which would leak EXIF). See `tools/requirements.txt`.

## Implemented tools (milestone 10)

| Tool | File | Status |
|---|---|---|
| `fha working-copy on\|off\|status [--root PATH] [--yes]` | `working_copy.py` | ✓ M10.1 - working-copy mode management; see "fha working-copy - implementation status" below |

Working-copy mode lets a genealogist git-sync their archive to a second machine
(laptop, NAS, travel device) without carrying the binary asset files (photos and
documents). The WORKING_COPY marker file at the archive root is git-ignored so it
is machine-local and never syncs back. When active:

- **Lint** suppresses E011 and E012 (asset-on-disk checks); emits one
  `[working copy] N asset file(s) assumed present on the main machine` note
  instead. All other lint rules run normally.
- **Index** stores `exists_on_disk = NULL` (not 0) for inventory files - callers
  can distinguish "unknown" from "missing".
- **Photoindex scan** and **reconcile** are refused so a working copy cannot
  prune or rewrite the photo cache; read-only photoindex commands (find, triage,
  report) work against any pre-existing `.cache/photos.sqlite`.
- **process**, **packet**, and **site** refuse with "do this on your main archive".
- **photoindex tag-person** refuses (asset-mutating).
- **doctor** headlines the mode, shows absent asset roots as informational (not
  errors), and shows photoindex as paused.
- **fha** (all commands) prints a one-line mode banner before running.

`fha working-copy on` writes the WORKING_COPY marker and ensures it is listed in
`.gitignore`. `fha working-copy off` prompts for confirmation (default No) before
removing the marker; `--yes` skips the prompt.

The `archive-template/.gitignore` already lists `WORKING_COPY`, so `fha install`
gives every new archive the guarantee for free.

Test fixture: `tests/fixtures/working-copy/` - records present, asset roots
pointing to absent directories, WORKING_COPY marker present. Lints clean.

## Implemented tools (milestone 9)

| Tool | File | Status |
|---|---|---|
| `fha install ARCHIVE-PATH [--repo PATH] [--dry-run]` | `scaffold.py`, `manifest.json` | ✓ M9.1 - first-time bootstrap: copy the operating layer + skeleton into a new archive and stamp `.plaintext-version`. See "fha install / fha update-tools - implementation status" below |
| `fha update-tools [--repo PATH] [--dry-run] [--verbose] [--root PATH]` | `scaffold.py` | ✓ M9.2 - refresh the operating layer from an updated public clone; back up customized/retired files, never delete, never touch skeleton seeds. (The no-op per-command `--spec-root` was removed; only the global `fha --spec-root` and `fha lint --spec-root` remain.) See below |

`manifest.json` (repo root) is the committed packing list every install/update reads.
It is regenerated from the repo - not hand-edited - with the maintenance command
`python tools/scaffold.py write-manifest --repo .` after any change to a tool, doc, or
skeleton file; `tests/test_scaffold.py`'s manifest-sync test fails the build if the
committed copy drifts from the repo. The `fha` command surface is exactly `install` +
`update-tools`; `write-manifest` is a tool-builder-only path, not part of it.

## fha process - implementation status

This is Stage A (the deterministic mint + mark + scaffold) of the intake
pipeline; the AI draft pass and review pass are the `process-source` /
`review-claims` skills, not this tool (TOOLING §6).

| Flag / feature | Status | Notes |
|---|---|---|
| Document intake (M7.1) | ✓ | Detect a non-photo file (extension + not under the resolved photos root); refuse a filename already carrying `_{S-id}`; mint an S-id via `_lib.mint_ids`; **rename in place** to `{slug}_{S-id}.{ext}` recording `original_filename`; scaffold `sources/{type}/{slug}_{S-id}.md` from the §14 template with an empty `## Claims` block. Transactional - the rename and record-write each register an undo and any failure rolls back; destination conflicts and unknown `--type` values refuse before writing |
| Photo intake (M7.2) | ✓ | Detect a photo (extension or under the photos root); refuse a file already carrying a `SOURCE:` keyword; mint an S-id; **never rename** - embed `SOURCE: {S-id}` via `exiftool -keywords+= -overwrite_original_in_place` (abort, scaffold nothing, on failure); scaffold `sources/photos/{slug}_{S-id}.md` with `role: primary`, `is_primary: true`. `source_type` is always `photo` |
| Inbox relocation | ✓ | An asset (and its sidecar, if any) staged under the resolved `inbox/` root - e.g. `fha capture --asset` - is moved flat (no rename) into `documents/` or `photos/` (by extension/photo-root heuristic, same as `classify_asset`) *before* document/photo intake runs, rather than refusing it as "not under the configured root." That's the point of an inbox: every `fha process` entrypoint files out of it instead of making the user move things by hand first. `--dry-run` previews the move without touching the filesystem; a destination-name collision refuses before anything moves |
| Source-stub sidecar (`*.notes.md`) | ✓ | A lone `{stem}.notes.md` beside a single asset (SPEC §12.1) is read as the starting point whether the user passes the asset or the sidecar itself: its optional `title`/`source_type` frontmatter hints refine the record (photos remain `source_type: photo`), its prose body becomes the record's `## Notes`, and the stub is deleted after the record is safely written. A `people:` hint (names the captured page showed, no P-ids yet) is folded into that same `## Notes` text rather than dropped, since a §14 record has no other slot for an unreconciled name. A sidecar with no same-stem asset is refused *unless* it explicitly flags `asset_elsewhere: true` (TOOLING §13b case (c), "pointer-only") - then it mints a no-asset record from `citation`/`external_links` hints instead (`files:` omitted, `--type`/`--title`/`--slug` overrides honored same as the asset path, `restricted: true` forced for `source_type: dna`), refusing if no `external_links` are present, and refusing `--more` outright (there's no asset for a second file to attach to). Bundle folders (multiple files + one bare `notes.md`) are handled by M7.4 below |
| `--more FILE ROLE[:copy]` | ✓ | Attach an additional file to the existing source named by the positional asset's S-id (its embedded `SOURCE:` keyword for a photo, its `_{S-id}` filename for a document). A photo `--more` file is keyword-marked and left in place; a document `--more` file is renamed `{slug}-[{copy}-]{role}_{S-id}.{ext}` with `original_filename` recorded. The new file's inventory entry is appended to the record via surgical text edit (frontmatter comments/order preserved, mirroring `fha places geocode`) |
| `--type TYPE` | ✓ | Source type + subdirectory for a document (default `other`); ignored for photos (always `photo`). A `*.notes.md` `source_type` hint overrides the default when it is in the controlled vocabulary |
| `--date DATE` | ✓ | Source date override for the scaffolded record. Clear plain-language dates are accepted and stored in archive form, e.g. `about 1880` -> `1880~`, `June 1880` -> `1880-06`, `1880s` -> `188X`. Explicit `--date` wins over a sidecar/bundle `source_date` hint |
| `--title` / `--slug` | ✓ | `--slug` wins; else `--title`; else the filename stem - slugified to lowercase-hyphenated. `--title` also seeds the record `title`/`citation` |
| `--people P-IDS` | ✓ | Comma-separated list of known P-ids of people in this photo (e.g. `P-de957bcda1,P-ab3c8f0e12`). Photos only. Each P-id must already have a person record; unknown-but-well-formed IDs are refused before any keyword write. Each accepted P-id is (a) embedded as a bare keyword in the photo file in the same exiftool call as `SOURCE:` (transactional - rolled back together if the scaffold fails) and (b) added to the source record's `people:` list so `fha index` + `fha find --related P-xxx` surface the photo without any face-region placement. `--people` is rejected with `--more` (use `fha photoindex tag-person` for already-processed photos), with documents, and with folder triage mode |
| `--dry-run` | ✓ | Previews mint/rename/keyword/scaffold/stub-delete and performs no filesystem effect (no exiftool call) |
| Folder triage (M7.3) | ✓ | Passing a directory (without a `notes.md`) lists its unprocessed top-level photos, grouped into variation sets by the shared `_lib.grouping_stem` and ranked by the same evidence signals as `fha photoindex triage` (caption +3, pid-keyword +2, confident date +1, back-variant +1, AI-only −2; metadata read best-effort, degrading to filename-only when exiftool is absent). The human selects groups (numbers, comma/space list, or `all`; blank skips); each selected group runs through the variation flow below |
| Tier-1 variation detection (M7.3) | ✓ | Before processing a single photo, its directory is scanned for siblings sharing a filename `base_id` (front/back, copy letters, crops, negatives, booklet pages - the TOOLING §6 grammar, shared with `fha photoindex` via `_lib`). A real set is surfaced with its batch-type label (A-D) and the `one / separate / skip` prompt: `one` mints a shared S-id over the whole set (one record, each file role-annotated, `is_primary` on the plain scan, SOURCE: embedded on every file, transactional rollback); `separate` processes each as its own source; `skip` (also blank/unrecognized - never mutates on an unclear answer) defers. Tier-2 `--with-vision` perceptual grouping remains backlog |
| Bundle folder dissolution (M7.4) | ✓ | A folder holding a bare `notes.md` (SPEC §12.1) becomes one source: one S-id covers every asset, documents are renamed `{slug}[-{role}]_{S-id}.{ext}` and filed under the documents root, photos are moved under the photos root **without renaming** and carry the SOURCE: keyword, one record is scaffolded from the notes (frontmatter hints → §14 fields, prose → `## Notes`) listing all assets in `files:`, and the emptied folder is deleted. Photo-only bundles default to `source_type: photo`; mixed or document-only bundles use the hint/default type. Transactional - every move/rename/embed registers an undo and any failure unwinds them. Destination convention (documents → `documents/{subdir}/`, photos → photos-root top level; `{subdir}` is the plural/`proofs` mapping `_record_subdir` applies) is an implementation choice - SPEC §12 treats asset subfolders as free projection; the §12.1 dissolution rules (shared S-id, `[-role]` document grammar, photo SOURCE: keyword, notes → `## Notes`, folder removed) are honored exactly |
| `--with-vision` tier-2 grouping | ⚑ deferred | Backlog (TOOLING §6) |
| Exit codes | ✓ | 0 success (incl. a skipped variation set or an empty selection - no mutation requested); 2 for a refusal / missing file / unknown `--type` / `_{S-id}` already present / `--more` on a folder / bundle with no assets; 3 for a tool failure (exiftool missing or write error, rolled-back record/keyword write) |

M7.4 bundle note: `notes.md` role hints are honored from either `roles:
{filename: role}` or `files:` entries carrying `file`, `role`, optional `copy`,
and optional `is_primary`; malformed or stale hints refuse before any move.
Bundles also refuse already-processed photos before moving assets, include
`*.notes.md`-named assets as ordinary files (only bare `notes.md` is the stub),
and restore `notes.md` if a late dissolution failure rolls back the folder.

Automated tests: `tests/test_process.py` (stdlib `unittest`) builds a throwaway
archive and monkeypatches the exiftool seams
(`_run_exiftool_read_keywords` / `_run_exiftool_embed_source` /
`_run_exiftool_remove_source` / `_run_exiftool_read_meta`) with an in-memory
`FakePhotoStore`, and the interactive `_prompt` seam with a scripted answer
queue, covering document mint/rename/scaffold, the empty-`## Claims`
parse, `--dry-run` no-op, already-processed refusal, sidecar-into-`## Notes`
(with `source_type` hint routing), rollback on a record-write failure, photo
keyword-embed-no-rename, the already-keyworded refusal, the embed-failure abort,
`--more` for both a photo back and a document page, asset classification, the
slug helpers, M7.3 variation detection (`one`/`separate`/`skip`, partly-processed
refusal, group rollback), M7.3 folder triage (grouping + selection + the empty
folder no-op), and M7.4 bundle dissolution (single-source dissolution, dry-run,
role hints, already-processed-photo refusal, sidecar-named asset inclusion,
late-failure rollback, no-asset refusal). Run with `python -m unittest
tests.test_process -v` from the
repo root.

## fha site - implementation status

The static-HTML family explorer (TOOLING §12). Reads structured data from
`.cache/index.sqlite`, prose from the person `.md`, citation text from the
source `.md` frontmatter, and the photo strip from `.cache/photos.sqlite`.
Writes only to the output directory.

| Flag / feature | Status | Notes |
|---|---|---|
| Source page (M8.1) | ✓ | Citation block (read from the source `.md` frontmatter, title fallback), source metadata, claims table with status badges (all statuses shown; people linked to their pages), and a files list (thumbnails + links). A malformed source record warns plainly and still renders with its title in place of the citation; one bad page never aborts the build |
| Person page (M8.2) | ✓ | Summary block from accepted vital claims; biography + Stories HTML (stdlib markdown→HTML + token swap, read from the person `.md`); timeline (accepted + needs-review, decade-grouped - same query as `fha views timeline`, suggested excluded); sources index grouped by `source_type` (same two-table UNION as `fha views sources-index`); photo strip (`photo_people`, one entry per variation group); Friends & Family from the `relationships` edges |
| Token swap | ✓ | `TOKEN_RE` in prose → relative hrefs: `[P-id]` → person page (or "Living Person" when redacted, or plain name for a stub/page-less person); `[S-id]` → source page (or "Restricted - not included in this publication" when redacted); `[L-id]` → place name (place pages are M8.3, no link yet); any unresolved token → `<mark>[X-xxxx]</mark>` |
| Markdown-link URL allowlist | ✓ | A prose link `[text](url)` emits an href only for `http`/`https`/`mailto` (case-insensitive) or a scheme-less relative URL; any other scheme-bearing URL (first `:` before any `/`, `?`, `#` - javascript:, data:, vbscript:, file:, …) renders its label as plain text, closing the stored-XSS hole in published prose |
| AI-DRAFT exclusion | ✓ | Biography/Stories prose still inside `<!-- AI-DRAFT … -->` markers is excluded from both build modes until `fha confirm draft` flips it to AI-ACCEPTED (marker grammar shared via `_lib.strip_unaccepted_drafts`, mirroring confirm.py; the excluded span runs from the previous AI marker or `#`/`##` heading to the marker - fail-closed, so unmarked prose directly above a draft is withheld with it until acceptance). AI-ACCEPTED prose publishes with its marker removed (provenance comments never render as visible text); a section emptied by the exclusion renders like a person with no such section - no stray heading. A DAMAGED marker (missing `-->`, orphan `<!-- /AI-DRAFT -->`, or stray marker text) withholds that person's Biography and Stories entirely and the build finishes with one warning naming the file and the fix - a broken marker can never publish the draft |
| `--standalone` (default) | ✓ | Self-contained, redacted snapshot. Living/unknown persons - and `restricted` persons (any value, read from the person file) - get **no page** and render as "Living Person"; restricted, DNA, and `rights.publication_ok: false` sources get **no page** and render as "Restricted…" (a free-text `restricted: by-request` source type is read from the file beyond the index's 0/1); a single `restricted` claim is withheld from the summary, timeline, source page, and place page even when its source publishes; a restricted name variant (a deadname) resolves internally - `[[prior name]]` still links - but renders the person's unrestricted display name (SPEC §18); source pages publish only **accepted + needs-review** claims (`suggested` AI drafts and `rejected`/`superseded` claims are withheld - matching the timeline); image assets become resized (≤1200px), EXIF-stripped derivatives copied into `site/media/` under collision-free names (stem + a hash of the alias path, so two same-stem photos never overwrite each other). A page is linked only if it was generated (no dangling redacted links) |
| `--linked` | ✓ | Local developer preview: real archive paths (no copies), no redaction. Mutually exclusive with `--standalone` |
| `--out PATH` | ✓ | Output directory (default `generated/site/`, or `generated/site-linked/` with `--linked`, under the archive root); absolute or archive-relative |
| `--dry-run` | ✓ | Reports how many pages would be built and lists the files/subtrees a real rebuild would first remove from the output dir (also returned as the dry-run payload's `reset_preview` key); writes nothing |
| Idempotent rebuild | ✓ | Each run clears only the subtrees it owns (`persons/`, `sources/`, `places/`, `media/`, `data/`, `vendor/`, and the `index.html`/`discoveries.html` files) before regenerating, so a record that becomes redacted loses its stale page; the `.fha-site` ownership marker is stamped the moment the output dir is cleared/created (an interrupted build can no longer lock its own folder) and refreshed when the build completes |
| Output-path safety | ✓ | Refuses (exit 3) to build into the archive root or another archive's folder (its `sources/` clear-on-rebuild would otherwise delete real records) - and refuses any non-empty output dir that lacks the `.fha-site` ownership marker, so `--out ~/Documents` can never delete a pre-existing sources/media/data folder; the message names the fix (point `--out` at a new/empty folder, or delete it yourself). A marker-less dir that is clearly a prior site build (`index.html` + `vendor/fha-tree.js`) is accepted and gains the marker on rebuild; empty or brand-new dirs always proceed. The default `generated/site/` is always safe |
| Place page (M8.3) | ✓ | Name, coords as an OpenStreetMap **URL** (no embedded map), dated `history:`, claims naming the place, contained micro-places (`within:` children, linked), and a people-frequency list. `[L-id]` tokens in prose now link here. People links redact as everywhere; the people-frequency list omits redacted persons entirely so a standalone place page never names a living person |
| Discoveries page (M8.3) | ✓ | Renders `notes/discoveries.md` through the same prose→HTML + token swap, so `[P-id]`/`[S-id]` mentions link (and living persons redact to "Living Person") for free. Missing/empty file → a plain "nothing logged yet" page |
| Home page (M8.4) | ✓ | Surname A-Z index (built from `person_pages`, so redacted persons are already excluded), a recent-discoveries teaser (last 5 `##`/`###` sections or top-level bullets of `discoveries.md`, redacted), plus place and source navigation so every generated page is reachable |
| Standalone redaction audit (M8.4) | ✓ | Enforced structurally: all cross-links resolve against the authoritative `person_pages`/`source_pages`/`place_pages` sets decided once in `prepare()`, so a page is linked only if it was generated. `tests/test_site.py` crawls every emitted standalone page (and every tree JSON node `url`) and asserts no `persons/`/`sources/` link points at a missing page |
| Interactive tree rendering (M8.5) | ✓ | A vendored, dependency-free renderer (`templates/vendor/fha-tree.js`) + a single adapter seam (`tree-adapter.js`) map the neutral tree JSON contract (TOOLING §7/§14b) to an SVG collapsible tree; no D3, no CDN, works from file://. At build time the home page gets a **descendant** tree seeded from the apex of `root_person`'s line (so the explorer fans forward across the whole family - reconciling BUILD's "root person" with TOOLING's "root ancestor"); each curated person page gets a 3-generation **ancestor** pedigree. JSON is written to `site/data/tree_{P-id}_{mode}.json` (the reusable artifact) **and** embedded inline (read from the DOM, not fetched - file:// has no network). Redaction is applied server-side in the JSON (living/unknown → "Living Person", no vitals, no link), so a published tree file never carries a living person's name or a link to a page that wasn't generated. The home descendant explorer passes a bounded `initialDepth` to the renderer (deeper generations start collapsed) so a large family doesn't paint thousands of nodes at once; the data stays complete and the reader expands forward |
| Exit codes | ✓ | 0 clean; 1 if any page warned (missing asset, malformed record, image that couldn't be processed); 3 (`EXIT_FAILURE`, the convention `fha packet` uses for can't-run refusals) for the Jinja2-missing, index-absent, unsafe-output, malformed-`fha.yaml` (bad-config), and output-reset-failure paths, each with a plain hint (install Jinja2 / run `fha index` / pick another folder / fix `fha.yaml`) |

`fha site`'s file is `tools/site.py`, but the module stem `site` collides with
Python's stdlib `site`; `fha.py` (and `tests/test_site.py`) load it by path under
the private name `fha_site` to avoid the cached-stdlib-module collision.

**Index dependency note:** M8.1 corrected `index.py` to store
`rights.publication_ok` three-state (`1`=true, `0`=explicit false, `NULL`=absent)
instead of folding explicit false to `NULL`. The shared redaction predicate
`COALESCE(publication_ok, 1) = 0` (used by `fha site`, `fha gedcom`, `fha wikitree`)
only fires on a stored `0`, so this is what makes a `publication_ok: false` source
actually withheld from public output. The DDL is unchanged (the column already
existed), but `INDEX_SCHEMA_VERSION` was bumped to **2** so an index built before
this fix (which stored `false` as `NULL`) is treated as old-schema and rebuilt by
`fha index` rather than silently under-redacting. The `.cache` is disposable and
gitignored, so the cost is one rebuild.

Automated tests: `tests/test_site.py` (stdlib `unittest`) builds a synthetic
`.cache/index.sqlite` (and, where needed, `.cache/photos.sqlite`) the same way
`tests/test_packet.py` does, and writes the prose/citation `.md` files alongside.
It covers the source page (citation/claims/status/people-links, missing-asset
note), source redaction (restricted/DNA/`publication_ok: false` → no page +
"Restricted" reference; present in `--linked`), the person page (all sections,
biography token swap including the unresolved-`<mark>` case, timeline
needs-review-in/suggested-out + decade grouping, grouped F&F and sources),
person redaction (living/unknown → no page; `unknown` treated as living; stub
never paged; present in `--linked`), the malformed-source-warns-and-continues
path, `--dry-run` writing nothing, idempotent rebuild dropping a now-redacted
page, the no-index status, the unsafe-output refusal (archive root as `--out`),
the standalone image derivative (resized + EXIF-stripped) vs. linked file link
vs. non-image "kept in the archive", and the prose→HTML converter
(headings/bold/lists/links + HTML escaping). M8.3/M8.4 add: the place page
(coords URL, alt-names, dated history, claims, micro-place links, people list)
and `[L-id]` token linking; the discoveries page (P/S linking + living-person
redaction) and the home discoveries teaser + missing-file path; the home surname
A-Z index and its omission of living persons under standalone; and a
standalone redaction audit that crawls every emitted page (and every tree JSON
node url) and asserts no `persons/`/`sources/` link points at a page that
wasn't generated. M8.5 adds: the vendored bundle is copied and free of any
remote/CDN reference; the home descendant tree seeds from the apex of
`root_person`'s line (whole-line node set); the per-person 3-generation
ancestor pedigree; tree JSON redaction (living apex → "Living Person", no url)
and url-points-only-at-generated-pages; and the no-tree-without-`root_person`
case. Post-review hardening tests: two same-stem photos get distinct media
derivatives (no overwrite); `--standalone` source pages exclude
`suggested`/`rejected` claims while `--linked` keeps them; the home tree passes a
bounded `initialDepth` and the pedigree passes null; and an old-schema (v1)
index is refused, not trusted. Run with `python -m unittest tests.test_site -v`
from the repo root. The generalized `restricted` redaction (a restricted person
gets no page, a free-text `by-request` source gets no page, a restricted claim is
withheld from a published source page, and a deadname name variant resolves
internally but renders the unrestricted display name) is covered by
`tests/test_privacy_restricted.py`.

## fha capture - implementation status

The web-record intake on-ramp (TOOLING §13b). Capture reads the HTML the human
already has - piped on stdin, or an HTML `--asset` - and stages a **source stub**
in `inbox/` (SPEC §12.1); it never logs in, fetches behind auth, or mints an
S-id. Stdlib only (HTML parsed with `html.parser` - no third-party library).

| Flag / feature | Status | Notes |
|---|---|---|
| Generic recipe (MG1.1) | ✓ | The universal fallback for an unknown site: title (`<title>`/`og:title`/`<h1>`), URL (`--url`/`<link rel=canonical>`/`<base href>`/`og:url`), accessed-date (today), `repository` (page host), and visible text (~2000 chars, script/style stripped, truncated on a word boundary) as the citation basis. `source_type: website` (the controlled-vocabulary value for BUILD_INGESTION.md's shorthand `web`, so the stub processes cleanly) |
| Inbox stub | ✓ | `{slug}.notes.md` with light optional frontmatter (title, source_type, citation, repository, source_date, external_links, person-name hints) over a freeform body. Re-parses cleanly via `read_record`, so `fha process` consumes it. Stub and asset slug collisions both uniquify (`slug-2`, `slug-3`); the stub and its `--asset` copy share a stem so they pair by basename without overwriting an existing inbox file |
| `--asset FILE` | ✓ | Copied alongside the stub with the matching stem; an HTML `--asset` doubles as the page source when nothing is piped. Existing same-stem asset files force a new stem before writing |
| Flag overrides | ✓ | `--title`/`--type`/`--date` always win over recipe/generic inference; `--type` is validated against the controlled vocabulary and `--date` accepts clear plain-language dates before storing the archive form (typos refuse here, not as an unprocessable stub later). A recipe-produced unclear source date is warned and dropped |
| `--dry-run` | ✓ | Previews the recipe match, stub path, optional asset copy, and research-log write without creating `inbox/` or `.cache/` |
| Research-log entry | ✓ | Capture is itself a logged search: the row always appends to `.cache/capture_log.jsonl` (the durable copy `fha index` re-ingests into `search_log` on every full rebuild, since that table is dropped and recreated from scratch), and *also* goes straight into `.cache/index.sqlite`'s `search_log` table when it exists (so `fha report`'s "already searched" sees it immediately; `person_id`/`source_id` null - a stub has neither yet). A logging failure warns but never fails the capture |
| No-asset capture (pointer-only) | ✓ | When the page only points elsewhere and no asset is saved, the stub is written with `asset_elsewhere: true` alongside its `external_links` (TOOLING §13b case (c)) - the explicit flag `fha process` requires before it will mint a source record with no companion file |
| `--ingest [DIR]` (MG2.1) | ✓ | Sweeps browser-staged bundles (`page.html` + optional `asset.*` + `capture.json`) from `DIR` (default: `fha.yaml` `capture_staging:` key, else `~/Downloads/fha-inbox`) into `inbox/`. Each bundle runs through `run_capture` wholesale (the `capture.json` `accessed`/`notes`/`people`/`repository` fields override the scrape; `people` is additive - the human's curated names lead, recipe-found relatives are kept), so the stub is byte-identical to the paste path's; swept bundles are parked in `.ingested/`, never deleted. Idempotent (a parked name is skipped), resilient (a malformed bundle - including a `page.html`/snapshot the browser still holds locked, the common Windows case - is reported with a close-the-program next step and left in place, never aborting siblings), and WORKING_COPY-safe (writes only to `inbox/`). The local bridge for the browser companion (TOOLING_INGESTION §6) |
| Non-archive `--root` refusal | ✓ | An explicit `--root` naming a folder without fha.yaml is refused before anything is staged (exit 3, nothing created - the shared `_lib.resolve_root_arg` guard); covers paste, `--ingest`, `--host`, and `--install-host` modes alike |
| stdin encoding | ✓ | stdin is read as raw bytes and decoded UTF-8 (not the locale codec), so a piped page's en-dashes/accents survive into the stub |
| Native-messaging host `--host` (§5.7) | ✓ backend | `fha capture --host` serves the browser companion over length-prefixed stdin/stdout JSON (launched on demand, no daemon): files a framed bundle straight into the configured `inbox/` via `run_ingest` (byte-identical to `--ingest`), plus two read-only queries - `suggestNames` (person name/alias autocomplete) and `checkUrl` (already-captured check by normalized host+path, durable query ids like `clipping_id` preserved). `--install-host [--extension-id ID] [--host-manifest-dir DIR] [--browser chrome\|edge]` writes the per-OS native-messaging manifest + launcher (absolute paths, as Chrome/Edge require; `--browser` selects the Chrome vs Edge location/registry key; `--dry-run` previews; on Windows it prints the `REG ADD` registry command). The **extension front-end** that consumes this host (IIIF/empty-detail panel wiring + the opt-in `nativeMessaging` permission request) is wired in `native-host.js`/`panel.js`, OFF by default behind a "file straight into my archive" toggle (the MV3 UI flows want a real-browser verification pass) |
| Site recipes (MG1.2/MG1.3) | ✓ | `tools/capture_recipes/` plug-in modules (`detect`/`extract`, discovered at runtime, tried in ascending `PRIORITY`, generic fallback last): **Ancestry** (collection title, date, household/index persons, image URL), **FamilySearch** (title, date, collection, fact-table persons), **Newspapers.com** (publication, date, page, snippet, citation; `source_type: newspaper`), **FindAGrave** (memorial name, birth/death, cemetery as a place hint, family members). Each detects its own page by host (with an `og:site_name` fallback) and rejects the others'. A broken or failing recipe is skipped with a warning - the page still captures generically |

Automated tests: `tests/test_capture.py` (stdlib `unittest`, no network) drives
`run_capture` over the anonymized `tests/fixtures/capture-samples/*.html`,
covering generic frontmatter + jsonl fallback + the `search_log` write path,
flag overrides + unknown-type/unclear-date refusal, `--dry-run` no-op,
write-failure exit status, `--asset` stem pairing, slug/asset collision, the
UTF-8 stdin path, each recipe's extraction, mutually-exclusive recipe detection,
and the truncation/domain helpers. Run with `python -m unittest
tests.test_capture -v` from the repo root. The `--ingest` sweep is covered
separately by `tests/test_capture_ingest.py` (clean sweep + parking, the
byte-identical-to-paste-path guarantee, dry-run no-op, idempotency, malformed-bundle
resilience, pointer-only `asset_elsewhere`, and config/default staging resolution).

## fha convert-mining - implementation status

One-time migration of a legacy transcript-mining export (the `mining/` folder of
`sources.txt`, `facts.txt`, `stories.txt`, `questions.txt`, `aliases.txt`, and
`transcripts/`) into conformant records (TOOLING §11). **Dry-run by default**;
`--apply` writes. Self-contained re-use of `_lib` primitives (tools never import
tools - it does not import `process.py`).

| Step / feature | Status | Notes |
|---|---|---|
| Sources first | ✓ | Each legacy `S###` → transcript copied to `documents/interviews/{slug}_{S-id}.txt` (renamed with the minted S-id, `original_filename` kept), a `sources/interview/{slug}_{S-id}.md` record scaffolded (`source_type: interview`, `people:` resolved via the alias map), and the extraction pass (model + run date) recorded in `## AI Passes` with human-readable import context in `## Notes` |
| Facts → suggested claims | ✓ | `facts.txt` markdown rows → `suggested` claims: Claim→`value` (a blank Claim cell is warned and skipped, not imported as an empty-value claim that would lint E010); Earliest/Latest→a single EDTF value or `min/max` interval - same-value/blank collapses to one value, an unknown-final-digit cell maps to the EDTF decade form `X` (TOOLING §11), and a decade/decade or decade/year mismatch becomes the matching interval rather than being silently narrowed or dropped; Confidence H/M/L→`confidence`; a blank/unrecognized Confidence cell defaults to `confidence: medium` (the `fha confirm` precedent) with one summarized warning naming the count, keeping the no-E010 contract; type by keyword heuristic (birth/marriage/served/worked/lived… → vocabulary) defaulting to `event` + the Section as `subtype`. `relationship`/`name` are never inferred (relationship needs `roles`). `Update(T###):` continuation lines merge into the preceding claim's `notes` |
| Encoding tolerance | ✓ | Export files and transcripts are read UTF-8-strict first, then with replacement characters on a decode failure (old ChatGPT-era exports routinely carry cp1252 smart quotes, e.g. byte 0x92) plus one warning per affected file - the plan, dry-run, and apply all survive, and the transcript filed into `documents/` is still copied byte-for-byte |
| Anchors | ✓ | Best-effort: the 3 rarest content words of a claim value are searched in the transcript; the first uniquely-matching line (all 3, then 2, then the rarest) → `anchor: line N`, else omitted |
| Stories → `## Stories` | ✓ | `stories.txt` blocks attach to their source record, person resolved to a `[P-id]` token; a story whose header omits `(S###)` or names an unknown source is warned and its narrative dropped rather than silently lost |
| Questions → `notes/questions.md` | ✓ | `## Q:` blocks appended (`origin: tool`, `status: open`) with the person/source refs mapped to their new P-id/S-id; a `source:` naming an unrecognized legacy id is warned and that ref omitted (the question still imports) |
| People + stubs | ✓ | Every named person resolves to a P-id (alias map, else freshly minted); a P-id with no existing record is minted as a `people/stubs/` stub (`tier: stub`, `living: unknown`), so every claim/story/question reference resolves (lint E005-clean) while privacy defaults stay conservative |
| Audit trail | ✓ | `.cache/convert_mapping.csv` (`legacy_id, new_id, notes`) for every source/claim/person mapping |
| Apply safety | ✓ | Dry-run is the default. `--apply` refuses if `.cache/convert_mapping.csv` already exists (one-shot repeat-run sentinel) or if any planned destination exists; live writes register their undo *before* the write/copy call (not after), so a write that fails partway (e.g. disk full) still gets cleaned up on rollback instead of leaving an orphaned file; rollback also restores any appended `notes/questions.md` text on a later failure |
| Result | ✓ | The converted archive lints with no E-level findings (only the expected W102 suggested-claim backlog) - the M7.5 "Done when" |

Automated tests: `tests/test_convert_mining.py` copies
`tests/fixtures/legacy-export/` to a throwaway tree and exercises the dry-run
(writes nothing), `--apply` (sources/claims/privacy-safe stubs, story + question
import, mapping CSV), repeat-apply refusal, rollback after a write failure, the
AI pass audit block, EDTF/type-heuristic units, the missing-`mining/` error, and
 - the contract - that the converted archive lints with zero errors via
`lint.run_lint_silent`. Run with `python -m unittest tests.test_convert_mining
-v` from the repo root.

## fha packet - implementation status

| Feature | Status | Notes |
|---|---|---|
| Curated/living gate | ✓ | Non-curated/stub persons and unknown P-ids refuse with exit 1 (`not-curated`/`not-found`), distinct from a missing index (exit 3). Packet subjects with `living: true` or `living: unknown` refuse before output is created, matching SPEC's external-output rule until a future explicit packet opt-in exists |
| Source gathering | ✓ | `claim_persons ∪ source_people` union (same two-table pattern as `views.py`'s sources-index, duplicated per-tool per TOOLING §15 - tools never import tools) |
| Privacy filtering | ✓ | The `restricted` marker (SPEC §19) is honored at the source, claim, and subject level, read from the record files (so a free-text type like `restricted: by-request` is recognized, not just the index's 0/1): plain/free-text restrictions open with `--include-restricted`, `restricted: dna` needs `--include-dna` (only that; `--include-restricted` never opens DNA), `restricted: by-request` never opens under any flag. A restricted subject is refused (`restricted-subject`) - absolutely for `by-request`, otherwise unless `--include-restricted`/`--include-dna`. A single restricted claim inside an otherwise-included source is dropped from the generated timeline AND spliced out of the packet's copied source record (surgical line-span removal that keeps the copy a valid record; the withhold is decided per parsed claim entry, so it never requires the claim to carry an `id:`). A source whose claims cannot be read at all (malformed claims YAML, a non-mapping claim entry) is not copied and its indexed claims are kept off the timeline - fail closed - with a WARNING naming the fix (`fha lint`). The copied profile likewise omits withheld `name_variants` entries (deadnames) and their `aliases:` mirrors, matched through wikilink wrappers and the nested-list form an unquoted `[[name]]` parses to. The packet README's "Left out for privacy" section counts what was withheld in plain words. Excluded sources are listed by ID + reason in the README, never silently dropped |
| AI-draft prose | ✓ | Unaccepted `<!-- AI-DRAFT ... -->` prose is withheld from the profile copy via the shared `_lib.strip_unaccepted_drafts` (no packet flag opens it; acceptance is `fha confirm draft`), and the README counts it plainly ("N draft paragraphs awaiting your review were left out"). AI-ACCEPTED prose ships with its provenance marker removed. A damaged marker (missing `-->`) fails the build (`write-failed`, exit 3) with a repair hint - the profile can neither ship verbatim nor be trusted piecemeal. Research files (`--include-research`) stay byte copies by the documented scope decision; one carrying an AI-DRAFT marker adds a one-line README caution ("the research copy may contain unreviewed draft text") |
| Other-living-person caution | ✓ | Any other person named by an included source's claims or `source_people`, with `living` in `('true', 'unknown')`, is listed in a README caution |
| `profile/` | ✓ | Profile `.md` always; `+research.md` with `--include-research` - if no research file exists for the person, a warning is reported (in messages and exit code) instead of silently omitting it |
| `timeline.md` | ✓ | Freshly generated for the export, filtered to the packet's *included* sources only (an excluded restricted/DNA source's claims never leak into the timeline) - intentionally simpler than `fha views timeline` (no decade headers, no GENERATED header; this is a one-shot export artifact, not a tracked view file) |
| `sources/` + `files/` | ✓ | One copy of each included source record; `source_files` assets resolved via `resolve_path` and copied with their on-disk filenames. Missing/unresolvable assets are listed in stderr and README.txt, and the CLI exits with warnings |
| `photos/` | ✓ | Union of `photo_people` (`pid-keyword`/`face-tag`/`name-match`, already computed by `fha photoindex`) expanded to each match's full `photo_groups` variation group, plus image-suffixed asset files from included sources. Missing/unreadable/stale photo index refuses (exit 3) unless `--no-photos`; an individual photo file missing on disk (stale cache entry) is listed in stderr and README.txt, same as a missing source asset, never silently dropped |
| `--no-photos` | ✓ | Skips photo gathering entirely; no photoindex required |
| Output | ✓ | `packet_{surname}_{P-id}_{date}/` under `-o`/`--out` (default `out/` under the archive root), then zipped alongside it; directory and zip are both left on disk. Existing same-name output refuses unless `--overwrite`; `--dry-run` previews without writing |
| Filesystem-error handling | ✓ | A single file's copy failing (locked file, permission error) is caught, reported in messages/exit code, and skipped - it never aborts the build. A structural failure (can't create the packet dir, zip write fails, disk full) is caught at the top level, the half-built directory is removed on a best-effort basis, and the command returns `write-failed` (exit 3) instead of an unhandled traceback |

Automated tests: `tests/test_packet.py` builds a synthetic `.cache/index.sqlite` (and, where needed, a synthetic `.cache/photos.sqlite`) directly from `index.py`'s/`photoindex.py`'s DDL, covering the curated/living gates, strict stale-index refusal, restricted/DNA source filtering (both directions), the other-living-person caution (`living: true` and `living: unknown`), timeline source filtering, missing asset/photo reporting, `--include-research` with no research file, output conflict/overwrite/dry-run (including `--dry-run --overwrite` together) behavior, external `--out` display, the missing/absent/stale photoindex paths, photo-group expansion (a person tagged on one variant pulls in its siblings), a per-file copy failure (mocked `shutil.copy2`), and a structural build failure (mocked `_zip_directory`). The generalized `restricted` contract (by-request never opens, dna needs `--include-dna`, restricted subject refused, restricted claim dropped from the timeline) is covered by `tests/test_privacy_restricted.py`. AI-draft withholding in the profile copy (draft absent, accepted prose shipped with markers removed, damaged marker leading to `write-failed`, the research-copy caution) is covered by `tests/test_packet.py`; the id-less and unparseable restricted-claim fail-closed paths, the timeline consistency pin, and the wrapped/nested alias-mirror forms by `tests/test_privacy_restricted.py`.

## fha places - implementation status

| Feature | Status | Notes |
|---|---|---|
| `fha places lint` | ✓ | Orphan `claims.place_id` references (`PL001`); duplicate place names case-folded across `name` + `alt_names` (`PL002`); dangling `within:` links (`PL003`); cyclic `within:` chains, including a self-loop, reported once per cycle (`PL004`); a place that is itself a `within:` target (a settlement) also carrying its own outward `within:` link - settlement-to-jurisdiction containment belongs in `history:`, never `within:` (`PL005`, SPEC §15); a non-string `within:` value, e.g. an unquoted YAML scalar (`PL006`). `Result.exit_code` carries the severity verdict itself (any E → 2, warnings only → 1, clean → 0, unusable index → 3) and the CLI returns it unchanged - one source of truth for headless callers and the terminal |
| `fha places candidates` | ✓ | Distinct *unlinked* (`place_id` empty) claim `place_text` values normalized (case-fold, punctuation, whitespace, St→Street/Co→County expansion) and clustered by a sorted token-set key, so word-order, punctuation, and abbreviation variants land in one group; groups with ≥ `--threshold` (default 3) claims are surfaced with claim count and EDTF date spread |
| GPS clusters | ✓ | Geotagged photos (`.cache/photos.sqlite`) clustered by ≤150m haversine distance, excluding photos within 150m of a known place's `coords`; absent/unreadable photo index is skipped, not an error (mirrors `fha packet --no-photos` treatment) |
| `fha report` §6b integration | ✓ | `report.py`'s `_section_place_candidates` imports `places` and calls `run_candidates()` - now live instead of the BUILD.md M6.2 deferral stub |
| `fha places geocode` | ✓ (M6.3) | Backfills `coords` (and proposes `alt_names`) for registry places missing coordinates (`--all`) or one place (`--place L-id`). Gazetteer is the offline **GeoNames** `cities15000` dump downloaded once into `.cache/geonames/` (`--offline` never fetches: a cached dump is required, else `no-gazetteer`/exit 1). A place's `name` + `hierarchy` tokens match against the dump; country names and US state names narrow candidates (admin1/country codes), and **only a single high-confidence hit is proposed** - `ambiguous` (multi-candidate) and no-match places are skipped, never guessed. **Every write is gated by an interactive `[y/N]`** (`confirm` callable is injectable for tests). Writes are surgical text edits to the matched `places.yaml` block (coords inserted/replaced; `alt_names` added only when absent, never clobbering a human list), preserving comments without needing `ruamel.yaml`. The surgical edit ends a place's block at the next list item at the same-or-shallower indent as its `- id:` line, so a uniformly indented registry (valid YAML) is edited correctly - the target entry alone changes and a later entry's `coords:` is never touched |

Automated tests: `tests/test_places.py` builds a synthetic `.cache/index.sqlite` directly from `index.py`'s DDL (same pattern as `tests/test_cooccur.py`), covering each lint code individually, word-order/punctuation/abbreviation clustering, the unlinked-only filter, date-spread computation, the missing-index failure path, haversine distance sanity checks, and (geocode) unique/ambiguous/none matching with country+state narrowing, the surgical YAML edit (insert/replace coords, preserve comments and an existing `alt_names`, touch only the target block), and the offline-no-gazetteer / decline-writes-nothing / accept-writes-coords run paths via an injected `confirm`.

## fha gedcom - implementation status

| Feature | Status | Notes |
|---|---|---|
| Scope selection | ✓ | `<P-id> --mode descendants\|ancestors\|connected` traverses the `relationships` edges (descendants follow `child` + one spouse hop to complete couples; ancestors follow `parent` + spouse hop; connected = the whole component over parent/child/spouse). `--generations N` caps descendants/ancestors depth (ignored by connected/`--all`). `--all` exports every non-merged person |
| INDI records | ✓ | NAME `Given /Surname/` (surname from the index slug, else last token), SEX, BIRT/DEAT from the first accepted dated birth/death claim (DATE/PLAC/SOUR), FAMS/FAMC links, `REFN` carrying the P-id |
| FAM records | ✓ | Couples keyed by parent-set (from `child`-of edges) merged with spouse pairs; HUSB/WIFE by sex (deterministic fallback for unknown/same-sex), CHIL links, MARR from the accepted public-safe marriage claim keyed by the spouse pair (role=`spouse` when present, first two persons as the legacy fallback so witnesses do not break the couple match) |
| Dates | ✓ | EDTF → GEDCOM 5.5.1 (`1850`, `ABT 1850`, `MAY 1850`, `20 MAY 1850`, `BET … AND …` for intervals, `ABT` for decades, `BEF` for open `[..Y]` bounds) |
| Sources | ✓ | Each emitted vital/marriage fact's `source_id` → `2 SOUR @Sn@`; top-level `SOUR` records carry `TITL` + `REFN` (the S-id), emitted only for sources actually cited by a non-redacted fact |
| Privacy (living redaction) | ✓ | `living: true`/`unknown` → `NAME /Living/`, birth/death and their SOUR withheld, marriage details of any family they belong to withheld, REFN omitted; structural FAMS/FAMC/HUSB/WIFE/CHIL links kept so the tree shape survives. `--include-living` lifts it. A redaction count is reported on stderr |
| Privacy (restricted/DNA) | ✓ | Restricted and DNA sources are not eligible fact sources for public GEDCOM export; their vital/marriage event details and `SOUR` records are withheld while already-derived relationship edges may still preserve tree shape. A free-text restricted type (`restricted: by-request`) the index stored as 0 is read from the source file and excluded too. A `restricted` **person** (any value, read from the person file) has their NAME withheld as `/Restricted/` with no override (`--include-living` lifts only the living redaction), structural FAMS/FAMC/HUSB/WIFE/CHIL links kept so the tree shape survives |
| Output | ✓ | GEDCOM 5.5.1 with a "do not re-import as truth" HEAD note; CRLF line endings; stdout or `--out FILE`. Never re-imported - GEDCOM is a one-way export bridge. Stable xrefs: persons `I{n}` by id, families `F{n}`, sources `S{n}` |

Automated tests: `tests/test_gedcom.py` (synthetic `.cache/index.sqlite`, relationships inserted directly) covers descendant/ancestor traversal and the generations cap, living-redaction default vs. `--include-living`, restricted/DNA fact exclusion, marriage role handling with witnesses, vitals/marriage/source emission, `--all`, the EDTF→GEDCOM and name-formatting helpers, and the not-found/bad-id/no-index paths. `tests/test_privacy_restricted.py` additionally covers a `restricted` person redacted as `/Restricted/` with no `--include-living` override and a free-text `by-request` source dropped as a fact source.

## fha gedcom import - implementation status

The Ancestry on-ramp (TOOLING §13a2): file a *foreign* GEDCOM as one source record + person
stubs + suggested claims, plan-then-apply. Routed by a dispatcher intercept in `fha.py`
(the `fha id check` mechanism) so the exporter's positional `P-id` surface is untouched;
`tools/gedcom_import.py` also runs standalone. Scaffolds directly from `_lib` primitives
(tools never import tools).

| Flag / feature | Status | Notes |
|---|---|---|
| Dry-run plan (default) | ✓ | Parses + plans + prints, writes nothing (byte-for-byte). Headline counts first (persons/families/claims/cited databases/duplicates + the living-flag split), then detail capped at 20 with a `--plan-out` pointer |
| `--apply` | ✓ | Copies the `.ged` to `documents/gedcom/{slug}_{S-id}.ged` (original untouched; documents root resolved through `fha.yaml` roots), writes one stub per INDI into `people/stubs/` (progress line per 100), one source record `sources/other/{slug}_{S-id}.md`, and the audit CSV **last**. Closing output states the review posture (imported ≠ reviewed; edges materialize as claims are accepted) |
| Parser (in-module, no new dependency) | ✓ | GEDCOM 5.5/5.5.1 line grammar `LEVEL [@XREF@] TAG [VALUE]`; CONC (no space)/CONT (newline) folded; malformed lines counted + warned, never fatal. Unread tags tallied honestly in the plan (`N lines carried tags this importer does not read`) - the filed copy preserves 100% of the file |
| Encoding guards (UTF-8-only v1) | ✓ | UTF-8 BOM stripped; UTF-16 BOM, undecodable bytes, and a `HEAD CHAR ANSEL` declaration each refuse (exit 2) naming the re-export-as-UTF-8 fix. ⚑ ANSEL translation table deferred (explicitly not v1) |
| Self-import guard | ✓ | `HEAD SOUR fha` (our exporter's stamp) refuses: the archive is the source of record; GEDCOM is a one-way bridge out |
| INDI → person stub | ✓ | `{surname}__{given}_{P-id}.md` grammar (no NAME → `unknown__unknown_…`; surname-less leads with `__`); primary NAME → `name:`, extra NAMEs → `name_variants:`; `sex:` kept only for M/F; provisional `birth:`/`death:` EDTF from BIRT/DEAT; `tier: stub`, `aliases: [P-id]`. No `relationships:` blocks (the suggested claims are the durable home). GEDCOM xrefs live in the audit CSV only, not `external_ids:` |
| `living:` heuristic | ✓ | The one privacy-relevant default, isolated in `living_flag_for_import`: DEAT present (even dateless `DEAT Y`) or latest-plausible birth year >110 years back → `living: false`; else `unknown`. Counts printed in every plan. Owner-flagged at review (plan 06 open question 1) |
| Events → suggested claims | ✓ | BIRT/DEAT/CHR/BAPM/BURI/OCCU/RESI/CENS/EDUC/IMMI/EMIG/NATU/`_MILT` → the §8.2 vocabulary; `EVEN`/any dated unknown tag → `event` + `subtype`; INDI NOTE → `note` claim (first line = value). All `status: suggested`, `confidence: low` (`medium` with a GEDCOM SOUR citation), `anchor: "line N"` into the filed copy, values lead with the assertion and name the xref, dates as-written kept in the value |
| FAM → couple + parent-child claims | ✓ | MARR → `marriage` (roles `{spouse: […]}`), DIV → `divorce`; one `relationship` claim per child (`roles: {child, parent}`, E015-satisfying); `FAMC…PEDI adopted/foster` → `subtype: adoptive`/`foster` (biological default stays unwritten). Dangling HUSB/WIFE/CHIL pointers warned + skipped |
| DATE → EDTF table | ✓ | `12 JAN 1850`→`1850-01-12` · `JAN 1850`→`1850-01` · `1850`→`1850` · ABT/EST/CAL→`~` at precision · `BEF X`→`[..X]` · `BET A AND B`/`FROM A TO B`→`A/B` · `AFT X`→ runtime probe of the `[X..]` after-form (today's `_lib` suite rejects it, so AFT omits `date:` and keeps the wording in the value) · INT/phrase/unparseable → omit `date:`, wording kept. Every emitted date validated with `is_valid_edtf`; failures downgrade to omit-date with one summarized warning |
| GEDCOM SOUR records | ✓ | Titles ride as research leads: claim `notes:` gains `GEDCOM cites: …`, the record's `## Notes` lists every cited database with its citation count and the honest "find the original records" framing. No S-records minted for them (we do not hold that evidence) |
| PLAC handling | ✓ | `place_text:` only, never an L-id; the existing `fha places candidates` flow harvests them later |
| Dedupe report | ✓ | Tree-scan of `people/**/*.md` (templates/companions skipped; the index may be stale on a first-import machine): normalized name-token match + birth year ±2 (or exact-name when a year is absent) → listed as possible matches, **still imported as new stubs, never auto-merged** |
| Re-run guard + audit CSV | ✓ | `.cache/gedcom_import/{sha12}.csv` (sha12 = first 12 hex of the file's SHA-256): `#` header rows carry file name/full hash/date/S-id; body rows map every xref → minted id. Same-hash re-import refused naming the date + S-id; a different export (different hash) imports cleanly |
| Rollback | ✓ | Every write registers its undo before executing; any failure unwinds in reverse and the message says everything was rolled back and no cleanup is needed. The audit CSV is the final write, so a failed run leaves no sentinel |
| `--plan-out FILE` | ✓ | Writes the FULL (uncapped) plan text; refused inside the archive root except top-level `out/` (packet's guard). An explicit flag so dry-run stays side-effect-free by default |
| Scale | ✓ | Exactly three `mint_ids` batches (S, P, C) - one tree scan each; apply prints one progress line per 100 stubs; one big `## Claims` block by design (SPEC §14 - reviewed in filtered passes, never read linearly) |
| Exit codes | ✓ | 0 clean plan/apply · 1 completed with warnings (downgraded dates, skipped malformed lines, dangling pointers) · 2 refusals before/without writes (missing/not-GEDCOM file, encoding, self-import, already-imported sentinel, destination collision, bad `--plan-out`) · 3 root unresolvable, or a write failure during apply (everything rolled back, message says so) |
| `run_import(...) -> Result` | ✓ | `data = {'applied','persons','families','claims','cited_sources','duplicates','warnings','source_id','audit_csv'}`; `changed` lists every written file on apply, empty on dry-run |

Automated tests: `tests/test_gedcom_import.py` over crafted fixtures in
`tests/fixtures/gedcom/` (`small.ged` with ABT/BEF/AFT/BET/FROM-TO/phrase dates, CONC/CONT,
an adoptive PEDI, a dateless `DEAT Y`, a dangling CHIL pointer, and two SOUR records;
`ansel.ged`; `utf16.ged`; `self-export.ged`): the full DATE table incl. the AFT probe, the
living heuristic (dateless-DEAT false / 1850 false / 1990 unknown / conservative upper-bound
reading), dry-run byte-for-byte no-op + counts, `--plan-out` full text + inside-archive
refusal + `out/` exemption, stub grammar/fields/variants, source-record shape (no `people:`,
`subtype: gedcom`, role `original`), claim completeness (required fields, roles, PEDI subtype,
confidence lift, unparseable-date wording preserved), anchors verified against the filed
copy's lines, post-apply `fha index` + `fha lint` (0 E-codes) + accepted-relationship edge
materialization, re-run guard (refusal naming date + S-id; modified copy imports + dedupe
flags), dedupe report-but-still-import, injected-failure rollback (byte-identical tree, no
sentinel, clean re-run), encoding/self-import/missing/not-GEDCOM/collision refusals,
dispatcher routing (both `--root` positions; exporter surface untouched), and a
1,000-person scale smoke (exactly 3 mint scans, progress lines, capped plan).

## fha wikitree - implementation status

| Feature | Status | Notes |
|---|---|---|
| Subject gating | ✓ | Curated profiles only; `living: true`/`unknown` subjects refused (external-facing output, AGENTS.md privacy rule); invalid/non-P id and missing person handled distinctly from a missing index |
| Privacy (restricted/DNA) | ✓ | Fails closed (public path, no opt-in): a `restricted` **subject** (any value, read from the person file) is refused (`restricted-subject`); profile prose that cites a `restricted` or DNA source is refused (`restricted-sources`, cited sources read from their files so a free-text `by-request` type is caught beyond the index's 0/1); and a `[[P-id]]` link to a `restricted` person is refused (`restricted-people`) - the blocking S-ids/P-ids are named for cleanup rather than dropped. A restricted name variant (a deadname) is excluded from the published name forms so it never folds into output; one written as an ID-token **display** (`[[P-id\|Deadname]]`) is caught the same way and refused (`restricted-names`) |
| Privacy (living, beyond the subject) | ✓ | An ID-token `[[P-id]]` link to a living/`unknown` person renders `[living person]` (preceding prose name folded away). A **name-wikilink** (`[[Ken Smith]]`) that resolves to a living/`unknown` person cannot be redacted in place, so the export is refused (`living-people`), naming each person and the fix: remove the reference or pin it to its `[[P-id]]` token so the redaction applies. `unknown` is treated as living throughout |
| Named-ref reuse | ✓ | Each `[S-id]` in the body → self-closing `<ref name="S-id"/>` at the use site; full `<ref name="S-id">{citation}</ref>` definitions (citation read from the source record's frontmatter, else its title) gathered once, deduplicated, in first-use order, into the hidden `<div name="references" style="display: none">` block; `== Sources ==` ends with `<references/>` |
| Person links + name folding | ✓ | `[P-id]` → `[[wikitree_id\|name]]` when `external_ids.wikitree` is recorded (`person_external`), else the plain name. A preceding "Name " in the prose (full name, first given word, or a `name_variant`) is folded into the link so "Margaret A. Cole [P-id]" renders the name once, not twice - and the same detection means an in-dialect "married [P-id]" still emits the name |
| Spacetime spans | ✓ | A sentence carrying exactly one `[S-id]` whose (subject, source) pair resolves to a single dated+placed claim **and** whose claim year appears in the sentence text is wrapped in `<span class="spacetime" data-loc=… data-date=ISO>`. The single-claim + year-in-sentence guards keep a source cited across several sentences from stamping the wrong (e.g. marriage) date onto an unrelated (e.g. birth) sentence. Sentence splitting skips initials ("Margaret A. Cole") and common abbreviations |
| Ancestry images | ✓ | A source's `external_links` Ancestry image URLs (`dbid=…&h=…` or `/view/{id}:{db}`) → `{{Ancestry Image\|db\|id}}`, appended to that source's reference definition |
| Template hooks | ✓ | Optional `tools/wikitree_templates.yaml` maps a claim `type` → a WikiTree infobox template (+ field map over `date`/`place`/`value`); each matching accepted claim renders the template near the top. Ships empty (no templates) so the default export emits none; a missing/malformed file disables the feature without breaking the export |
| Output | ✓ | Heading conversion (`##`→`==`, `###`→`===`, H1 dropped), `*(none yet)*` placeholders removed; stdout or `--out FILE`; never uploads |
| AI-DRAFT exclusion | ✓ | Body prose still inside `<!-- AI-DRAFT … -->` markers is dropped from the export (span + marker; grammar mirrors confirm.py, boundary = previous AI marker or `#`/`##` heading, fail-closed) BEFORE the privacy scans - a draft is not-yet-content, so a citation or link living only inside one neither emits a ref nor triggers a refusal. AI-ACCEPTED prose exports with its marker removed; a section emptied by the exclusion drops its heading (no stray `== … ==`). A DAMAGED marker (missing `-->`, orphan `<!-- /AI-DRAFT -->`) refuses the export - status `broken-draft-marker`, exit 3, message names the file and the fix - because draft can no longer be told from accepted prose |

Automated tests: `tests/test_wikitree.py` builds a small on-disk archive (profile + source records) with a synthetic index, covering ref dedup/placement and the `<references/>` anchor, `[P-id]` link folding (no doubled name), the spacetime span landing only on the year-matching sentence, the Ancestry-image template, placeholder removal, the living/not-curated/not-found/bad-id gates, restricted/DNA citation refusal, and the URL/heading/sentence-split unit helpers. `tests/test_privacy_restricted.py` covers the restricted-subject, restricted-person-link, and free-text-restricted-source refusals.

## fha doctor - implementation status

| Check | Status | Notes |
|---|---|---|
| Archive root + fha.yaml | ✓ | Fatal exit 2 if either absent/unparseable |
| Mapped roots reachable | ✓ | ✓/✗ per root; unreachable → exit 2 |
| exiftool on PATH | ✓ | ✗ → exit 1 (warning; not a hard dep for most commands) |
| Python deps (PyYAML) | ✓ | ✗ → exit 2 |
| Index freshness | ✓ | absent/stale → exit 1 (D5) |
| Photoindex freshness | ✓ | schema probed before "fresh"; absent/stale/unreadable → exit 1 (D5) |
| Lint summary | ✓ | import-and-call `run_lint_silent`; E-level findings → exit 2 |
| Inbox aging (14 days) | ✓ | printed only when inbox/ dir exists |
| Counts | ✓ | from index when fresh, else quick scan |
| E018 findings detail | ✓ | lists findings when present |
| Tools version (M9) | ✓ | reads `.plaintext-version` (present/absent/unreadable) + counts pending `.plaintext-backup/` files; unreadable stamp → exit 1, else informational. Self-contained read (no import of scaffold.py) |
| Backup reminder | ✓ | always printed |

## fha install / fha update-tools - implementation status

The scaffolding pair (TOOLING §13c). `manifest.json` is the committed packing list;
both commands read it and copy operating-layer + skeleton files between a public-repo
clone (or unzipped download) and a private archive. Generic glue - touches no family data.

| Flag / feature | Status | Notes |
|---|---|---|
| `manifest.json` | ✓ M9.1 | One JSON object listing every operating-layer + skeleton file with `path`, `category` (`operating`/`skeleton`), `sha256`, `spec_version` (parsed from SPEC.md's version line), and a `src` field on skeleton entries whose archive path drops the `archive-template/` prefix. Built by a repo walk: the root rulebooks (`README.md`/`SPEC.md`/`TOOLING.md`/`AGENTS.md`/`AGENTS_TOOLING.md`/`CLAUDE.md`/`BUILD.md`), all of `tools/` (minus `__pycache__`/`*.pyc`), all of `docs/`, the agent workflow skills under `.claude/skills/`, and the `archive-template/` contents (minus its own `README.md`). Excludes spec-repo furniture (`example-archive/`, `archive-template/` as a folder, `tests/`, `.github/`, `.claude/settings.json`, `PRIVACY.md` - the public-repo "no real data" policy, contradictory inside a real archive - `RELEASE_CHECKLIST.md`, `manifest.json` itself) |
| docs scope | ✓ | The whole `docs/` folder ships, not just BUILD.md M9.1's named five - they are the floor; a directory rule keeps every doc cross-link intact in an installed archive and auto-covers future docs. The operating layer also ships `README.md` (project orientation) and `.claude/skills/` (the genealogy workflow procedures) - everything a genealogist needs to operate, minus public-repo furniture |
| `fha install` copy + stamp | ✓ M9.1 | Creates `ARCHIVE-PATH` if absent; copies every manifest file (skeleton remapped to archive root); writes `.plaintext-version` (manifest version, spec version, install timestamp, per-file SHA256). Validates every source exists **before** writing, so a broken clone fails clean with no half-installed archive |
| Preflight | ✓ M9.1 | Python ≥ 3.10 → friendly download pointer + hard stop if older; `exiftool` missing → advisory only, install still finishes (photo features wait) |
| Re-install guard | ✓ | Refuses an archive that already has `.plaintext-version` or `tools/fha.py`, pointing at `fha update-tools` - install is one-time |
| Zip-based / git-free | ✓ M9.1 | `--repo` only needs a directory containing `manifest.json`; `.git/` is never referenced. `--repo` defaults to the tools being run from (correct for a clone or an unzipped download) |
| `fha install --dry-run` | ✓ | Previews the file/skeleton counts and the stamp path; writes nothing (BUILD.md "every mutating op ships `--dry-run`") |
| `fha update-tools` reconcile | ✓ M9.2 | Compares the manifest against `.plaintext-version` and classifies each **operating** file: new → copy; unchanged-from-stock (disk == recorded) → overwrite silently; customized (disk differs from recorded) → move to `.plaintext-backup/{date}/` then install stock; already-current (disk == new stock) → no-op. Retired (recorded but gone from the manifest, still on disk) → move to backup. Never deletes |
| Skeleton-is-install-once | ✓ M9.2 | `update-tools` reconciles only `category: operating` files. Skeleton seeds (`fha.yaml`, `places/places.yaml`, `inbox/_TEMPLATE.notes.md`, the `.gitkeep`s) are **never touched** - they fill with the human's config/data, so refreshing them would clobber it. The stamp carries their checksums over unchanged. This realizes §13c's governing principle ("never silently overwrites your work") for the two files most certain to be customized; surfaced as a TOOLING §13c clarification |
| Backup safety | ✓ | `.plaintext-backup/{date}/{path}` preserves the archive subtree; a same-day collision gets a `-2`/`-3` suffix so an earlier backup is never overwritten. Per-file outcome messages and the `Done:` summary counts are emitted **after** each operation succeeds (and count only actual successes), so a per-file copy/move `OSError` never produces a false "backed up / updated" line; the failure is reported on stderr and downgrades the run to exit 1 |
| Stamp rewrite | ✓ | After an update each operating file's recorded baseline is: its new on-disk hash if installed this run; its on-disk hash (== stock) if it was already current; or - if it **failed** this run - the **prior** recorded baseline, never the failed file's current bytes (recording a failed customized file's edit would make the next run treat it as pristine stock and silently overwrite it). Skeleton entries carry over verbatim; retired files that moved drop out, while a retired file whose move failed stays recorded so the next run retries it. `--dry-run` writes no stamp |
| No-stamp archive | ✓ | An archive whose tools were hand-copied (no `.plaintext-version`) is handled: every differing existing file is treated as customized (backed up, never overwritten); identical hand-copies match new stock and are left alone |
| `fha update-tools --dry-run` | ✓ M9.2 | Prints the full plain-English would-do plan and a summary line; writes nothing |
| `--verbose` | ✓ | Also lists files that are already up to date |
| `--repo` required + archive check | ✓ | Missing `--repo` → the BUILD.md-specified "run from inside your archive, with --repo pointing to your copy of the plaintext tools" message; not-an-archive (auto-detect finds no `fha.yaml`, **or** an explicit `--root` points at a folder without `fha.yaml`) → a distinct plain refusal before any file is written; all exit 3 |
| `write-manifest` (maintenance) | ✓ | `python tools/scaffold.py write-manifest --repo .` regenerates `manifest.json`; not on the `fha` surface. Kept honest by `tests/test_scaffold.py`'s manifest-sync test |
| Exit codes | ✓ | 0 clean (incl. install with the exiftool advisory, and an update that backed files up); 1 if some files couldn't be written/moved (reported); 3 for can't-run (Python too old, missing/invalid manifest, re-install refusal, missing `--repo`, not-an-archive, write failure) |

Automated tests: `tests/test_scaffold.py` (stdlib `unittest`) builds a tiny **git-free** fake
repo (3 operating + 3 skeleton files) and throwaway archives, covering install (copy + remap +
stamp, dry-run no-op, re-install refusal, missing-source/missing-manifest refusal, Python-too-old
hard stop, exiftool-advisory-only), every update outcome (no-op, stock-overwrite, customized
backup, retired quarantine, added file, dry-run no-op, no-stamp hand-copied archive), the critical
**skeleton-never-touched** safety property, the partial-failure paths (a mocked `shutil.move`
failure: no false-success output, honest summary counts, and - the data-loss regression - a failed
customized update keeps the edit safe so the retry backs it up instead of silently overwriting it;
a failed retired move stays tracked and is retried), the friendly `_cmd_*` error exits (missing
`--repo`, not-an-archive, **explicit `--root` that isn't an archive**, bad repo), and a
**manifest-sync** guard that recomputes the manifest from the real repo and asserts the committed
`manifest.json` still matches (so a forgotten regeneration fails CI).
Run with `python -m unittest tests.test_scaffold -v` from the repo root.

## fha find - implementation status

| Flag / feature | Status | Notes |
|---|---|---|
| `<P-id>` lookup | ✓ | file, couple folder, companions, claims, citations, photo note |
| `<S-id>` lookup | ✓ | record, files (resolved + on-disk status), claims, citation sites |
| `<C-id>` lookup | ✓ | source record + approx line, status, value, corroborates/contradicts |
| `<L-id>` lookup | ✓ | place entry, claims referencing it, prose mentions |
| `<H-id>` lookup | ✓ | hypothesis entry, section heading, status, prose mentions |
| `--text "…"` | ✓ | notes_fts + re.search; photo captions searched when photoindex is fresh, else explicit skip-note; transcript FTS ⚑ deferred (D7) |
| `--related <P-id>` | ✓ | relationship edges (rel + distinct source count); co-occurring persons with no existing edge (per-tool duplicate of cooccur.py's person co-occurrence, scoped to one person); places by claim frequency; shared occupation/military/membership affiliations with other people; distinct source count; photos via `photo_people` (note if photoindex absent) |
| `--related <L-id>` | ✓ | claims naming the place; people ranked by claim frequency; distinct source count; micro-places (`within: L-id` children); photos within ~0.002° of the place's coords |
| `--related <S-id>` | ✓ | claim counts by status; persons; places; corroborating/contradicting sources via `claim_links` (both directions, inverse-rel labeled); sibling sources sharing a person or `repository` |
| `--related <C-id>` | ✓ | source, persons, place; linked claims (outgoing + incoming via `claim_links`); sibling claims (same person + same type). No `--date` (a single claim's own `date_edtf` already pins it) |
| `--related <H-id>` | ✓ | person concerned, status, verifying claim from the `hypotheses` table when a row exists; since the index builder never populates that table (see `_find_hypothesis`), the normal case derives the same neighborhood from `claims.hypothesis` + `claim_persons` instead - not a failure |
| `--related --date <EDTF>` (standalone, no ID) | ✓ | every accepted/needs-review claim whose bounds overlap the EDTF, plus the people/sources/places behind them; summary line `Active in {EDTF}: N claims, N people, N sources` |
| `--related <ID> --date <EDTF>` (combined) | ✓ | P-id, L-id, and S-id neighborhoods accept `--date` as an additional AND filter (e.g. a person's relationships/places, or a source's claims by status, narrowed to a decade); C/H neighborhoods ignore it (a single claim's own `date_edtf` already pins it, and a hypothesis isn't meaningfully time-sliced) |
| `--related` index requirement | ✓ | absent/unreadable `.cache/index.sqlite` → exit 3 (no tree-scan fallback - unlike find_by_id, the relational joins have no scan equivalent); stale → warns, still queries |
| Index fallback (ID lookup, `--text`) | ✓ | stale index warns but remains structured; absent/unreadable index tree-scans with "WARNING: index not fresh" header |

## Implemented tools (milestone 1)

| Tool | File | Status |
|---|---|---|
| Shared library | `_lib.py` | ✓ foundations; the `--root` archive guard lives here (`resolve_root_arg` - an explicit `--root` without fha.yaml is one shared refusal, exit 3, nothing created); unfenced `## Claims` are read strict-first - a ``` quoted inside a claim's `value:` is evidence and survives the read; fence lines are only dropped when the as-typed parse fails (half-typed fence) |
| `fha` CLI dispatcher | `fha.py` | ✓ routes all subcommands |
| `fha id mint/check` | `id.py` | ✓ Crockford Base32, existence check |
| `fha index` | `index.py` | ✓ full SQLite rebuild + incremental upsert; any truthy `restricted:` (incl. typed `dna`/`by-request`) indexes as restricted = 1 (schema v5); `--root` pointing at a folder without fha.yaml is refused (exit 3, nothing created - the shared `_lib.resolve_root_arg` guard); full rebuild and `--source` upsert resolve claim/frontmatter names through the same persons+places alias map, so upserts stay row-for-row identical to the full rebuild even when another record's alias clashes with a person name |
| `fha normalize-links [--dry-run] [--write] [--quiet]` | `normalize_links.py` | ✓ the one explicit, previewed rewrite pass settling citations to the canonical `[[ID]]`/`[[ID\|display]]` form: legacy single-bracket `[S-…]` upgraded, resolved human stems/name-links pinned to their ID (display text kept, the stem preserved in `aliases:`), `people:`/`places:` frontmatter name-links settled; an ambiguous name (alias clash, W113) is reported and left as written (exit 1), never guessed. Dry-run is the default AND `--dry-run` parses as an explicit no-op (the always-preview habit); `--dry-run --write` together is refused with a plain pick-one message (exit 2); `--write` applies, showing the same diff (`--quiet` suppresses it). The claims yaml block and bare-ID frontmatter lists are never touched. GENERATED companion files are skipped by their first NON-BLANK line (`_lib.is_generated_file`, BOM tolerated - matching lint/views ownership semantics); the old byte-0 check rewrote generated files that began with a blank line. Engine covered by `tests/test_alias_layer.py`; the CLI flag contract by `tests/test_normalize_links.py` |
| `fha lint` (alias `fha check`) | `lint.py` | ✓ see lint status table below; `check` is an argparse alias routing to the same command |
| `fha stubs` | `stubs.py` | ✓ scan + mint stubs |

`fha lint --root example-archive` exits 1 with the documented baseline warnings (TOOLING.md §15):
W101 (the fictional Thomas Hartley has no located death record) and W102 (one suggested claim
staged on the family-portrait source as review-demo material) - both intentional for the fixture.
No E-level errors. The `example-archive/` is a demonstration fixture permitted to carry documented
known warnings; the `tests/fixtures/` clean fixture (not yet built) must exit 0.

## fha lint - implementation status

This table is the authoritative build-status record for lint codes and flags.
`TOOLING.md §3` describes the full design intent; this table tracks what is actually built.
A code listed in TOOLING must appear here as either ✓ or ⚑ before the tool is milestone-complete.

| Code / flag | Status | Notes |
|-------------|--------|-------|
| E001-E010, E013, E015-E017 | ✓ implemented | - |
| E017 (restricted recognition) | ✓ implemented | The `restricted` marker is open (SPEC §19): a value of `true` or any free-text type (`dna`, `by-request`, `deadname`, …) on a source, claim, person, or name is valid and never an error. E017 fires only when a DNA source has *no* restricting value at all; a free-text `restricted: dna` satisfies it. No new error code. A `{value:, restricted: true}` name variant resolves through the alias surface (its `value` feeds the clash check), so a `[[prior name]]` deadname link does not raise E004. |
| E004 (place) | ✓ implemented | Forgiving (PR 05): a well-formed `L-id` in `place:` that doesn't resolve is still E004 (broken link). Free text in `place:` is **not** rejected - it emits W109 pointing the human to `place_text:`. |
| E002 (hyphenated names) | ✓ implemented | SPEC §13 name slots admit interior hyphens: `smith-jones__anne_P-…`, `hartley__mary-jane_P-…`, and their `_research`/`_timeline`/`_sources-index`/`_draft-queue` companions lint clean. A hyphen never leads a slot, and companion-kind classification is untouched (it keys on the suffix's own leading underscore in `_lib.parse_filename`, which already accepted hyphenated names). A missing `__` separator stays the W117 nudge, never E002. |
| E002/E010 (template placeholder ids) | ✓ implemented | A template placeholder id (`P-__________`/`S-__________`/`C-__________` - TYPE dash + all underscores, ≥4; underscores are not Crockford chars, so no real id can match) is treated as MISSING, never malformed: person/source records classify as auto-mintable (the listing says the placeholder will be replaced) and `--fix-ids` rewrites the placeholder in place; a placeholder claim id is E010 naming `--fix-ids` as the fix, never E002. A placeholder id beside a filename that already carries a real id is a single E003 with a "paste that code into the id: line" message. |
| E004/E005 (near-miss codes) | ✓ implemented | A claim reference that LOOKS like a mistyped record code is a finding, never silently inert: a `P-`/`S-`/`C-`/`L-`/`H-` prefix followed by a code-shaped body (near-code-length, purely alphanumeric, carrying a digit) that fails the 10-char Crockford grammar (wrong length, or letters outside 0-9 a-z minus i l o u), or a bare *exactly-10-character* code-like token carrying a digit (a code pasted without its prefix), fires E005 (claim `persons:`) / E004 (`corroborates:`/`contradicts:`) with a plain gloss and the fix. A name that resolves is fine; an unresolvable plain NAME stays an inert note-link - including a name+year token like `Anna1850` (shorter than a 10-char code) and a note-link that merely starts with a type letter + hyphen (`L-something`, `C-note`), which carry no code-shaped body; template placeholders (`C-__________`) stay in the E010/`--fix-ids` story. Invalid ids are flagged, never stub-minted. |
| E004 (hypotheses) | ✓ implemented | The known-H-id universe includes hypotheses defined as `id:` entries in `## Hypotheses` sections of person research files (SPEC §16) - the same place `fha index` discovers them - so a `[[H-…]]` cite of a research-file hypothesis is not an orphan. A citation alone never defines an H-id; a genuinely dangling `[[H-…]]` stays E004. |
| Needs-sourcing worklist | ✓ implemented | Informational only (never moves the exit code): recorded provisional vitals not yet backed by an accepted claim, `(TODO: import source)` prose, and unsourced/hypothesis `relationships:` entries. A present-but-empty `birth:`/`death:` key records nothing and is never listed; death entries are skipped for `living: true`/`unknown` persons (SPEC §8.2 - death is inapplicable while living; unknown counts as living). |
| E011 | ✓ implemented | inventory→disk direction; document disk→inventory scan by filename S-id. Photo disk→inventory direction requires `--with-exif`. |
| E012 | ✓ implemented | Only runs when `--with-exif` is passed (requires exiftool on PATH); silently skipped otherwise. |
| E014 | ✓ implemented | Forgiving (PR 05): a loose-but-clear date (`circa 1870`, `1870s`, `before 1920`) is normalized via `_lib.normalize_date` and emits a **W109 suggestion** of the canonical form, not E014. Only a genuinely unreadable date is E014, with a plain example-bearing message (no bare `EDTF` jargon). Applies to both claim `date:` and `source_date:`. |
| E018 | ✓ implemented (partial) | Deprecated-command check active. Photo-rename instruction check is a no-op pass - text pattern too ambiguous to assert direction reliably. |
| W101, W102, W104, W106, W107, W108 | ✓ implemented | - |
| W109 | ✓ implemented | The catch-all warning: missing `notes` context, unknown `source_type`, `--format-check` file-format issues, **and (PR 05) loose-but-clear dates and free-text `place:` values** - each with an actionable suggestion of the stored form. |
| W103 | ✓ implemented | Stale couple-folder bracket lists; fires in `fha lint` and `fha views brackets`. Marks a child who joined other than by birth (`Ruth (adopted)`, `(step)`, …) from the backing claim's `subtype` (SPEC §12.2). ⚑ The cross-link entry for a person who occupies multiple Ahnentafel positions (`Thomas Hartley (also #128 - see 040)`) is **deferred** - the lowest-numbered folder is already the deterministic home (BFS-first), so this is a display-only follow-up. |
| W105 | ⚑ deferred | Requires mtime comparison against a known-good generated state. |
| W110 | ✓ implemented | Direct-line person file in wrong Ahnentafel couple folder; fires in `fha lint` (requires `root_person`) and `fha views brackets`. Numbering walks only **genetic** parent edges (SPEC §12.2): adoptive/step/foster/guardian/surrogate-gestational/social parents are shown in brackets but never numbered; an unset/unknown nature defaults to genetic (legacy archives number unchanged). |
| W115 | ✓ implemented | Relationship reconciliation drift: a sourced person-doc `relationships:` entry whose backing claim is missing, doesn't record the edge, or disagrees on `subtype` (nature); also an opted-in block that omits an accepted kin claim naming the person. Warning, never a gate. |
| W116 | ✓ implemented | Missing reciprocal edge: a sourced relationship recorded on one person but not mirrored on the other; `--fix-reciprocal` appends the mirror. |
| W117 | ✓ implemented | Name-grammar guidance: a person filename with no `__` sort separator (SPEC §13). A surname-less filename that leads with `__` (`__caesar_P-…`) is valid and silent; only a missing-separator name is nudged, never E002. Detecting genuine surname-first *ordering* (`tanaka__hanako`) is left to the human - the check only flags the absent separator. |
| W118 | ✓ implemented | Detached claim persons: a claim whose `persons:` resolves to no person record at all (every entry is an unresolvable plain name - not an ID, not an alias) attaches to no one and is missing from every timeline/vitals/merge check. Warned, never blocked (an unresolved plain name stays an inert note-link by contract; a whole list that detaches is almost always a typo/rename). Not raised when at least one named person resolves, nor when a near-miss code among the entries already fires E005 (no double-report). |
| `--with-exif` | ✓ implemented | Exiftool batch keyword read; drives E012 and photo-side E011. |
| `--json` | ✓ implemented | - |
| `--format-check` | ✓ implemented (partial) | Final-newline and CRLF hygiene active. Frontmatter key order, lowercase ID normalization, YAML indentation: ⚑ deferred. |
| `--format-write` | ✓ implemented (partial) | Writes the fixes reported by `--format-check`. Frontmatter normalization: ⚑ deferred. |
| `--dry-run` | ✓ implemented | Each active fix mode prints "Would …" lines without writing. |
| `--fix-ids` | ✓ implemented | Mints IDs for hand-authored, id-less records (SPEC §10 mint-on-contact): renames to the §13 grammar and keeps BOTH the slug and the verbatim filename stem as aliases so existing `[[name]]` links keep resolving - MERGED into an existing `aliases:` block (deduped, formatting preserved; templates ship one), and "(old name kept as an alias)" prints only when entries were actually written. Never touches a GENERATED-headed file or a `README.md`. Second half: mints missing claim `id:`s by guarded text surgery - every scan anchored to the item's own key lines (a `status: accepted` or `id:` lookalike inside a `value: \|` scalar is never edited), a blank `id:` completed in place (never a second key), anchor-led `- &c1` items and one-line `- {...}` flow items refused naming `fha id mint C`, reads/writes exact-newline (an LF archive stays LF), and the full rewrite re-parsed (`claims_edit_problem` + a minted-ids-landed count) before writing - any doubt is a per-file refusal, so the success message can never lie. Unfenced (W114) claim blocks are sequenced to `--fix-claims-fence` first (one combined run completes both). Stamps `reviewed:` (today) on hand-written `accepted` claims it mints into (TOOLING §3b); accepted claims that already carry ids stay E006's. Template placeholder ids count as missing and are rewritten in place. All previewed by `--dry-run` |
| `--fix-claims-fence` | ✓ implemented | W114: wraps unfenced `## Claims` content in a yaml fence, dedented exactly as the unfenced reader parses it, and re-parses the result - the wrap is refused (nothing written) unless the fenced block reads back to the identical claims. A ``` line inside the section (a half-typed fence, or ``` quoted in a claim value) is never deleted: the file is refused with the line number and the by-hand fix. Refusals surface under `--dry-run` too |
| `--mint-stubs` | ✓ implemented | - |
| `--spawn-questions` | ✓ implemented | - |
| `--fix-reciprocal` | ✓ implemented | Appends the missing mirror entry (W116) to the other person's `relationships:` block; additive only, previewed with `--dry-run`, skips a person who has no record yet. |
| `--fix-inventory` | ⚑ deferred (not exposed) | Removed from the CLI while unimplemented (a flag that only printed a warning taught users flags might be decorative). `fha process` per document is the current path; re-add with the real E011 fixer. |
