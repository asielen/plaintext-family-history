# tools/

The `fha` command suite lives here. Run via `python tools/fha.py <command>` from the repo
root, or `python tools/<tool>.py` for standalone use.

These tools are **generic**: they operate on any spec-conforming archive and contain no family data.
They are the "replaceable glue" of the philosophy - disposable, regenerable from the spec, and safe to publish.
`TOOLING.md` (repo root) is the design document for every tool; consult it before changing any behavior.

CLI recovery contract: `fha` prints help on no args; unknown subcommands get a
"did you mean?" hint; ordinary failures explain the cause and the next command
to try; tracebacks are hidden by default and shown only with the global
`--debug` flag for tool-building diagnostics. Shared messages cover missing
PyYAML, malformed `fha.yaml`, missing archive roots, missing exiftool, bad
`source_type` values, and bad archive-date/EDTF values.

## Implemented tools (milestone 2)

| Tool | File | Status |
|---|---|---|
| `fha views timeline` | `views.py` | ✓ per-person and --all-curated |
| `fha views sources-index` | `views.py` | ✓ per-person, --all-curated, --couple-folders |
| `fha views draft-queue` | `views.py` | ✓ per-person and --all-curated |
| `fha views brackets` | `views.py` | ✓ W103 bracket refresh, W110 Ahnentafel placement; `--fix` applies, `--dry-run` previews |
| `fha views tree` | `views.py` | ✓ ancestors/descendants/fan modes; `--format json\|dot`; `--generations N`; `--out FILE`; `--format html` deferred (D6) |
| `fha doctor` | `doctor.py` | ✓ all 11 checks; D5 applied (absent index/photoindex = warning, not error) |
| `fha find <ID>` | `find.py` | ✓ P/S/C/L/H id types; structured index path when present; tree-scan fallback when absent |
| `fha find --text "…"` | `find.py` | ✓ notes_fts + re.search; photo captions searched when photoindex is fresh (else skip-note); `transcripts_fts` created but not yet populated - transcript search deferred (D7) |
| `fha find --related <ID> [--date EDTF]` | `find.py` | ✓ BUILD.md M4.3 (D4) - neighborhood query for all five ID types, plus a standalone `--related --date EDTF` time slice. Requires a real index (exit 3 if absent/unreadable - no tree-scan fallback, unlike find_by_id) |
| `fha id check <ID>` | `fha.py` alias | ✓ re-routed through `find.find_by_id` in fha.py dispatcher |

Views require a fresh `.cache/index.sqlite` (run `fha index` first). `fha find` uses the index when present, warns when it is stale, and falls back to a tree scan only when the index is absent or unreadable; `fha doctor` degrades gracefully without caches. Both `.cache/index.sqlite` and `.cache/photos.sqlite` carry a `meta.schema_version` row plus `PRAGMA user_version`; missing, old, corrupt, or unreadable caches are treated as disposable and rebuilt by `fha index` / `fha photoindex`.
Generated files carry the `<!-- GENERATED … -->` header and must not be hand-edited.

## Implemented tools (milestone 3, in progress)

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
human-confirm gate" discipline: they only print suggestions. Confirming a pair (writing
`corroborates:`/`contradicts:` links, minting a `relationship` claim, or writing a
dismissal) is left to a future skill layer - out of scope for M4.1/M4.2. `fha find
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
| Refresh sequence | ✓ | `photoindex.run_scan(full=False)` → `index.build_index` → `lint._run_lint_core`, every run regardless of `--full`/`--section` |
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
| §8 Possible connections | ✓ | Embeds `cooccur.run_cooccur(threshold=2)` - person pairs, shared-place pairs, and org/entity recurrence hubs, top 10 each, with `[confirm] [dismiss]` labels (confirming/dismissing is still a future skill-layer action, same as `fha cooccur` itself) |
| `--full` | ✓ | Treats the snapshot baseline as empty for diffing; still writes a fresh snapshot afterward |
| `--section NAME` | ✓ | Narrows stdout to one section; the persisted snapshot and `.cache/report_{date}.md` always hold the complete report |
| `notes/discoveries.md` append | ⚑ deferred | TOOLING §15a describes appending confirmed discoveries there with human confirmation - a future skill-layer action, mirroring `fha cooccur`'s read-only tombstone discipline. `fha report` only ever prints |

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
| Dismissed-pairs tombstone | ✓ (read-only) | `.cache/cooccur_dismissed.json`; missing file = empty set, not an error; this tool never writes it |
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
| `fha wikitree <P-id> [--out FILE]` | `wikitree.py` | ✓ M6.5 - see "fha wikitree - implementation status" below |

## Implemented tools (milestone 7)

| Tool | File | Status |
|---|---|---|
| `fha process FILE|FOLDER [--type TYPE] [--title …] [--date DATE] [--slug SLUG] [--people P-IDS] [--more FILE ROLE[:copy]] [--dry-run]` | `process.py` | ✓ M7.1-M7.4 - single-file documents and photos + `--more`; `--people` records known P-ids on photos at intake; folder triage + tier-1 variation detection (M7.3); `notes.md` bundle dissolution (M7.4); see "fha process - implementation status" below |
| `fha capture [--url URL] [--title …] [--type TYPE] [--date DATE] [--asset FILE] [--dry-run]` | `capture.py`, `capture_recipes/` | ✓ M7.5-M7.7 - paste-fallback web capture into an inbox source stub; generic recipe + Ancestry/FamilySearch/Newspapers.com/FindAGrave site recipes; see "fha capture - implementation status" below |
| `fha convert-mining [--apply]` | `convert_mining.py` | ✓ M7.8 - one-time legacy transcript-mining migration into conformant sources/claims/stubs/questions; dry-run by default; see "fha convert-mining - implementation status" below |

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

## fha capture - implementation status

The web-record intake on-ramp (TOOLING §13b). Capture reads the HTML the human
already has - piped on stdin, or an HTML `--asset` - and stages a **source stub**
in `inbox/` (SPEC §12.1); it never logs in, fetches behind auth, or mints an
S-id. Stdlib only (HTML parsed with `html.parser` - no third-party library).

| Flag / feature | Status | Notes |
|---|---|---|
| Generic recipe (M7.5) | ✓ | The universal fallback for an unknown site: title (`<title>`/`og:title`/`<h1>`), URL (`--url`/`<link rel=canonical>`/`<base href>`/`og:url`), accessed-date (today), `repository` (page host), and visible text (~2000 chars, script/style stripped, truncated on a word boundary) as the citation basis. `source_type: website` (the controlled-vocabulary value for BUILD.md's shorthand `web`, so the stub processes cleanly) |
| Inbox stub | ✓ | `{slug}.notes.md` with light optional frontmatter (title, source_type, citation, repository, source_date, external_links, person-name hints) over a freeform body. Re-parses cleanly via `read_record`, so `fha process` consumes it. Stub and asset slug collisions both uniquify (`slug-2`, `slug-3`); the stub and its `--asset` copy share a stem so they pair by basename without overwriting an existing inbox file |
| `--asset FILE` | ✓ | Copied alongside the stub with the matching stem; an HTML `--asset` doubles as the page source when nothing is piped. Existing same-stem asset files force a new stem before writing |
| Flag overrides | ✓ | `--title`/`--type`/`--date` always win over recipe/generic inference; `--type` is validated against the controlled vocabulary and `--date` accepts clear plain-language dates before storing the archive form (typos refuse here, not as an unprocessable stub later). A recipe-produced unclear source date is warned and dropped |
| `--dry-run` | ✓ | Previews the recipe match, stub path, optional asset copy, and research-log write without creating `inbox/` or `.cache/` |
| Research-log entry | ✓ | Capture is itself a logged search: the row always appends to `.cache/capture_log.jsonl` (the durable copy `fha index` re-ingests into `search_log` on every full rebuild, since that table is dropped and recreated from scratch), and *also* goes straight into `.cache/index.sqlite`'s `search_log` table when it exists (so `fha report`'s "already searched" sees it immediately; `person_id`/`source_id` null - a stub has neither yet). A logging failure warns but never fails the capture |
| No-asset capture (pointer-only) | ✓ | When the page only points elsewhere and no asset is saved, the stub is written with `asset_elsewhere: true` alongside its `external_links` (TOOLING §13b case (c)) - the explicit flag `fha process` requires before it will mint a source record with no companion file |
| stdin encoding | ✓ | stdin is read as raw bytes and decoded UTF-8 (not the locale codec), so a piped page's en-dashes/accents survive into the stub |
| Site recipes (M7.6/M7.7) | ✓ | `tools/capture_recipes/` plug-in modules (`detect`/`extract`, discovered at runtime, tried in ascending `PRIORITY`, generic fallback last): **Ancestry** (collection title, date, household/index persons, image URL), **FamilySearch** (title, date, collection, fact-table persons), **Newspapers.com** (publication, date, page, snippet, citation; `source_type: newspaper`), **FindAGrave** (memorial name, birth/death, cemetery as a place hint, family members). Each detects its own page by host (with an `og:site_name` fallback) and rejects the others'. A broken or failing recipe is skipped with a warning - the page still captures generically |

Automated tests: `tests/test_capture.py` (stdlib `unittest`, no network) drives
`run_capture` over the anonymized `tests/fixtures/capture-samples/*.html`,
covering generic frontmatter + jsonl fallback + the `search_log` write path,
flag overrides + unknown-type/unclear-date refusal, `--dry-run` no-op,
write-failure exit status, `--asset` stem pairing, slug/asset collision, the
UTF-8 stdin path, each recipe's extraction, mutually-exclusive recipe detection,
and the truncation/domain helpers. Run with `python -m unittest
tests.test_capture -v` from the repo root.

## fha convert-mining - implementation status

One-time migration of a legacy transcript-mining export (the `mining/` folder of
`sources.txt`, `facts.txt`, `stories.txt`, `questions.txt`, `aliases.txt`, and
`transcripts/`) into conformant records (TOOLING §11). **Dry-run by default**;
`--apply` writes. Self-contained re-use of `_lib` primitives (tools never import
tools - it does not import `process.py`).

| Step / feature | Status | Notes |
|---|---|---|
| Sources first | ✓ | Each legacy `S###` → transcript copied to `documents/interviews/{slug}_{S-id}.txt` (renamed with the minted S-id, `original_filename` kept), a `sources/interview/{slug}_{S-id}.md` record scaffolded (`source_type: interview`, `people:` resolved via the alias map), and the extraction pass (model + run date) recorded in `## AI Passes` with human-readable import context in `## Notes` |
| Facts → suggested claims | ✓ | `facts.txt` markdown rows → `suggested` claims: Claim→`value` (a blank Claim cell is warned and skipped, not imported as an empty-value claim that would lint E010); Earliest/Latest→a single EDTF value or `min/max` interval - same-value/blank collapses to one value, an unknown-final-digit cell maps to the EDTF decade form `X` (TOOLING §11), and a decade/decade or decade/year mismatch becomes the matching interval rather than being silently narrowed or dropped; Confidence H/M/L→`confidence`; type by keyword heuristic (birth/marriage/served/worked/lived… → vocabulary) defaulting to `event` + the Section as `subtype`. `relationship`/`name` are never inferred (relationship needs `roles`). `Update(T###):` continuation lines merge into the preceding claim's `notes` |
| Anchors | ✓ | Best-effort: the 3 rarest content words of a claim value are searched in the transcript; the first uniquely-matching line (all 3, then 2, then the rarest) → `anchor: line N`, else omitted |
| Stories → `## Stories` | ✓ | `stories.txt` blocks attach to their source record, person resolved to a `[P-id]` token; a story whose header omits `(S###)` or names an unknown source is warned and its narrative dropped rather than silently lost |
| Questions → `notes/questions.md` | ✓ | `## Q:` blocks appended (`origin: tool`, `status: open`) with the person/source refs mapped to their new P-id/S-id; a `source:` naming an unrecognized legacy id is warned and that ref omitted (the question still imports) |
| People + stubs | ✓ | Every named person resolves to a P-id (alias map, else freshly minted); a P-id with no existing record is minted as a `people/stubs/` stub (`tier: stub`, `living: unknown`), so every claim/story/question reference resolves (lint E005-clean) while privacy defaults stay conservative |
| Audit trail | ✓ | `.cache/convert_mapping.csv` (`legacy_id, new_id, notes`) for every source/claim/person mapping |
| Apply safety | ✓ | Dry-run is the default. `--apply` refuses if `.cache/convert_mapping.csv` already exists (one-shot repeat-run sentinel) or if any planned destination exists; live writes register their undo *before* the write/copy call (not after), so a write that fails partway (e.g. disk full) still gets cleaned up on rollback instead of leaving an orphaned file; rollback also restores any appended `notes/questions.md` text on a later failure |
| Result | ✓ | The converted archive lints with no E-level findings (only the expected W102 suggested-claim backlog) - the M7.8 "Done when" |

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
| Privacy filtering | ✓ | `restricted: true` sources excluded by default (`--include-restricted` overrides); `source_type: dna` sources excluded even with `--include-restricted` (only `--include-dna` includes them); excluded sources are listed by ID + reason in the README, never silently dropped |
| Other-living-person caution | ✓ | Any other person named by an included source's claims or `source_people`, with `living` in `('true', 'unknown')`, is listed in a README caution |
| `profile/` | ✓ | Profile `.md` always; `+research.md` with `--include-research` - if no research file exists for the person, a warning is reported (in messages and exit code) instead of silently omitting it |
| `timeline.md` | ✓ | Freshly generated for the export, filtered to the packet's *included* sources only (an excluded restricted/DNA source's claims never leak into the timeline) - intentionally simpler than `fha views timeline` (no decade headers, no GENERATED header; this is a one-shot export artifact, not a tracked view file) |
| `sources/` + `files/` | ✓ | One copy of each included source record; `source_files` assets resolved via `resolve_path` and copied with their on-disk filenames. Missing/unresolvable assets are listed in stderr and README.txt, and the CLI exits with warnings |
| `photos/` | ✓ | Union of `photo_people` (`pid-keyword`/`face-tag`/`name-match`, already computed by `fha photoindex`) expanded to each match's full `photo_groups` variation group, plus image-suffixed asset files from included sources. Missing/unreadable/stale photo index refuses (exit 3) unless `--no-photos`; an individual photo file missing on disk (stale cache entry) is listed in stderr and README.txt, same as a missing source asset, never silently dropped |
| `--no-photos` | ✓ | Skips photo gathering entirely; no photoindex required |
| Output | ✓ | `packet_{surname}_{P-id}_{date}/` under `-o`/`--out` (default `out/` under the archive root), then zipped alongside it; directory and zip are both left on disk. Existing same-name output refuses unless `--overwrite`; `--dry-run` previews without writing |
| Filesystem-error handling | ✓ | A single file's copy failing (locked file, permission error) is caught, reported in messages/exit code, and skipped - it never aborts the build. A structural failure (can't create the packet dir, zip write fails, disk full) is caught at the top level, the half-built directory is removed on a best-effort basis, and the command returns `write-failed` (exit 3) instead of an unhandled traceback |

Automated tests: `tests/test_packet.py` builds a synthetic `.cache/index.sqlite` (and, where needed, a synthetic `.cache/photos.sqlite`) directly from `index.py`'s/`photoindex.py`'s DDL, covering the curated/living gates, strict stale-index refusal, restricted/DNA source filtering (both directions), the other-living-person caution (`living: true` and `living: unknown`), timeline source filtering, missing asset/photo reporting, `--include-research` with no research file, output conflict/overwrite/dry-run (including `--dry-run --overwrite` together) behavior, external `--out` display, the missing/absent/stale photoindex paths, photo-group expansion (a person tagged on one variant pulls in its siblings), a per-file copy failure (mocked `shutil.copy2`), and a structural build failure (mocked `_zip_directory`).

## fha places - implementation status

| Feature | Status | Notes |
|---|---|---|
| `fha places lint` | ✓ | Orphan `claims.place_id` references (`PL001`); duplicate place names case-folded across `name` + `alt_names` (`PL002`); dangling `within:` links (`PL003`); cyclic `within:` chains, including a self-loop, reported once per cycle (`PL004`); a place that is itself a `within:` target (a settlement) also carrying its own outward `within:` link - settlement-to-jurisdiction containment belongs in `history:`, never `within:` (`PL005`, SPEC §15); a non-string `within:` value, e.g. an unquoted YAML scalar (`PL006`) |
| `fha places candidates` | ✓ | Distinct *unlinked* (`place_id` empty) claim `place_text` values normalized (case-fold, punctuation, whitespace, St→Street/Co→County expansion) and clustered by a sorted token-set key, so word-order, punctuation, and abbreviation variants land in one group; groups with ≥ `--threshold` (default 3) claims are surfaced with claim count and EDTF date spread |
| GPS clusters | ✓ | Geotagged photos (`.cache/photos.sqlite`) clustered by ≤150m haversine distance, excluding photos within 150m of a known place's `coords`; absent/unreadable photo index is skipped, not an error (mirrors `fha packet --no-photos` treatment) |
| `fha report` §6b integration | ✓ | `report.py`'s `_section_place_candidates` imports `places` and calls `run_candidates()` - now live instead of the BUILD.md M6.2 deferral stub |
| `fha places geocode` | ✓ (M6.3) | Backfills `coords` (and proposes `alt_names`) for registry places missing coordinates (`--all`) or one place (`--place L-id`). Gazetteer is the offline **GeoNames** `cities15000` dump downloaded once into `.cache/geonames/` (`--offline` never fetches: a cached dump is required, else `no-gazetteer`/exit 1). A place's `name` + `hierarchy` tokens match against the dump; country names and US state names narrow candidates (admin1/country codes), and **only a single high-confidence hit is proposed** - `ambiguous` (multi-candidate) and no-match places are skipped, never guessed. **Every write is gated by an interactive `[y/N]`** (`confirm` callable is injectable for tests). Writes are surgical text edits to the matched `places.yaml` block (coords inserted/replaced; `alt_names` added only when absent, never clobbering a human list), preserving comments without needing `ruamel.yaml` |

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
| Privacy (restricted/DNA) | ✓ | Restricted and DNA sources are not eligible fact sources for public GEDCOM export; their vital/marriage event details and `SOUR` records are withheld while already-derived relationship edges may still preserve tree shape |
| Output | ✓ | GEDCOM 5.5.1 with a "do not re-import as truth" HEAD note; CRLF line endings; stdout or `--out FILE`. Never re-imported - GEDCOM is a one-way export bridge. Stable xrefs: persons `I{n}` by id, families `F{n}`, sources `S{n}` |

Automated tests: `tests/test_gedcom.py` (synthetic `.cache/index.sqlite`, relationships inserted directly) covers descendant/ancestor traversal and the generations cap, living-redaction default vs. `--include-living`, restricted/DNA fact exclusion, marriage role handling with witnesses, vitals/marriage/source emission, `--all`, the EDTF→GEDCOM and name-formatting helpers, and the not-found/bad-id/no-index paths.

## fha wikitree - implementation status

| Feature | Status | Notes |
|---|---|---|
| Subject gating | ✓ | Curated profiles only; `living: true`/`unknown` subjects refused (external-facing output, AGENTS.md privacy rule); invalid/non-P id and missing person handled distinctly from a missing index |
| Privacy (restricted/DNA) | ✓ | Profile prose that cites restricted or DNA sources is refused before output is written; the exporter fails closed with the blocking S-ids rather than dropping refs and leaving unsupported public facts behind |
| Named-ref reuse | ✓ | Each `[S-id]` in the body → self-closing `<ref name="S-id"/>` at the use site; full `<ref name="S-id">{citation}</ref>` definitions (citation read from the source record's frontmatter, else its title) gathered once, deduplicated, in first-use order, into the hidden `<div name="references" style="display: none">` block; `== Sources ==` ends with `<references/>` |
| Person links + name folding | ✓ | `[P-id]` → `[[wikitree_id\|name]]` when `external_ids.wikitree` is recorded (`person_external`), else the plain name. A preceding "Name " in the prose (full name, first given word, or a `name_variant`) is folded into the link so "Margaret A. Cole [P-id]" renders the name once, not twice - and the same detection means an in-dialect "married [P-id]" still emits the name |
| Spacetime spans | ✓ | A sentence carrying exactly one `[S-id]` whose (subject, source) pair resolves to a single dated+placed claim **and** whose claim year appears in the sentence text is wrapped in `<span class="spacetime" data-loc=… data-date=ISO>`. The single-claim + year-in-sentence guards keep a source cited across several sentences from stamping the wrong (e.g. marriage) date onto an unrelated (e.g. birth) sentence. Sentence splitting skips initials ("Margaret A. Cole") and common abbreviations |
| Ancestry images | ✓ | A source's `external_links` Ancestry image URLs (`dbid=…&h=…` or `/view/{id}:{db}`) → `{{Ancestry Image\|db\|id}}`, appended to that source's reference definition |
| Template hooks | ✓ | Optional `tools/wikitree_templates.yaml` maps a claim `type` → a WikiTree infobox template (+ field map over `date`/`place`/`value`); each matching accepted claim renders the template near the top. Ships empty (no templates) so the default export emits none; a missing/malformed file disables the feature without breaking the export |
| Output | ✓ | Heading conversion (`##`→`==`, `###`→`===`, H1 dropped), `*(none yet)*` placeholders removed; stdout or `--out FILE`; never uploads |

Automated tests: `tests/test_wikitree.py` builds a small on-disk archive (profile + source records) with a synthetic index, covering ref dedup/placement and the `<references/>` anchor, `[P-id]` link folding (no doubled name), the spacetime span landing only on the year-matching sentence, the Ancestry-image template, placeholder removal, the living/not-curated/not-found/bad-id gates, restricted/DNA citation refusal, and the URL/heading/sentence-split unit helpers.

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
| Backup reminder | ✓ | always printed |

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
| Shared library | `_lib.py` | ✓ foundations |
| `fha` CLI dispatcher | `fha.py` | ✓ routes all subcommands |
| `fha id mint/check` | `id.py` | ✓ Crockford Base32, existence check |
| `fha index` | `index.py` | ✓ full SQLite rebuild + incremental upsert |
| `fha lint` | `lint.py` | ✓ see lint status table below |
| `fha stubs` | `stubs.py` | ✓ scan + mint stubs |

`fha lint --root example-archive` exits 1 with one expected W101 - the fictional Thomas Hartley
has no located death record, which is intentional for a minimal fixture.
No E-level errors. The `example-archive/` is a demonstration fixture permitted to carry documented
known warnings; the `tests/fixtures/` clean fixture (not yet built) must exit 0.

## fha lint - implementation status

This table is the authoritative build-status record for lint codes and flags.
`TOOLING.md §3` describes the full design intent; this table tracks what is actually built.
A code listed in TOOLING must appear here as either ✓ or ⚑ before the tool is milestone-complete.

| Code / flag | Status | Notes |
|-------------|--------|-------|
| E001-E010, E013, E015-E017 | ✓ implemented | - |
| E004 (place) | ✓ implemented | Forgiving (PR 05): a well-formed `L-id` in `place:` that doesn't resolve is still E004 (broken link). Free text in `place:` is **not** rejected - it emits W109 pointing the human to `place_text:`. |
| E011 | ✓ implemented | inventory→disk direction; document disk→inventory scan by filename S-id. Photo disk→inventory direction requires `--with-exif`. |
| E012 | ✓ implemented | Only runs when `--with-exif` is passed (requires exiftool on PATH); silently skipped otherwise. |
| E014 | ✓ implemented | Forgiving (PR 05): a loose-but-clear date (`circa 1870`, `1870s`, `before 1920`) is normalized via `_lib.normalize_date` and emits a **W109 suggestion** of the canonical form, not E014. Only a genuinely unreadable date is E014, with a plain example-bearing message (no bare `EDTF` jargon). Applies to both claim `date:` and `source_date:`. |
| E018 | ✓ implemented (partial) | Deprecated-command check active. Photo-rename instruction check is a no-op pass - text pattern too ambiguous to assert direction reliably. |
| W101, W102, W104, W106, W107, W108 | ✓ implemented | - |
| W109 | ✓ implemented | The catch-all warning: missing `notes` context, unknown `source_type`, `--format-check` file-format issues, **and (PR 05) loose-but-clear dates and free-text `place:` values** - each with an actionable suggestion of the stored form. |
| W103 | ✓ implemented | Stale couple-folder bracket lists; fires in `fha lint` and `fha views brackets`. |
| W105 | ⚑ deferred | Requires mtime comparison against a known-good generated state. |
| W110 | ✓ implemented | Direct-line person file in wrong Ahnentafel couple folder; fires in `fha lint` (requires `root_person`) and `fha views brackets`. |
| `--with-exif` | ✓ implemented | Exiftool batch keyword read; drives E012 and photo-side E011. |
| `--json` | ✓ implemented | - |
| `--format-check` | ✓ implemented (partial) | Final-newline and CRLF hygiene active. Frontmatter key order, lowercase ID normalization, YAML indentation: ⚑ deferred. |
| `--format-write` | ✓ implemented (partial) | Writes the fixes reported by `--format-check`. Frontmatter normalization: ⚑ deferred. |
| `--dry-run` | ✓ implemented | Each active fix mode prints "Would …" lines without writing. |
| `--mint-stubs` | ✓ implemented | - |
| `--spawn-questions` | ✓ implemented | - |
| `--fix-inventory` | ⚠ CLI placeholder | Prints a not-yet-implemented warning; `fha process` is the current alternative. |
