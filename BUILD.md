# BUILD.md - fha tool suite: build sequence

**Who this is for:** developers implementing the `fha` tool suite. If you just want to use the archive, start with [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

This file is the order in which to build the **core** `fha` tools from nothing, with the
acceptance test for each step. Every tool appears in dependency order with the same level of
detail - what to build, the SPEC and TOOLING sections that define it, and a "Done when" a builder
can run. SPEC.md is the law for what the archive holds; `TOOLING.md` holds the design rationale
and the full contract for each tool; this file gives the sequence and how to verify it. Where the
acceptance block names a `tests/` module, that module is the authoritative check; the shell lines
around it are what a builder runs by hand. `<scratch-archive>` means a throwaway copy of
`example-archive/` (never the committed fixture, and never a real archive).

Two sibling layers have their own design + build docs, each with its own milestone numbering:
the capture / inbox on-ramp ([`TOOLING_INGESTION.md`](TOOLING_INGESTION.md) →
[`BUILD_INGESTION.md`](BUILD_INGESTION.md), the **MG** series) and the workflow skills and
`fha serve` workbench ([`TOOLING_INTERFACE.md`](TOOLING_INTERFACE.md) →
[`BUILD_INTERFACE.md`](BUILD_INTERFACE.md), the **MI** series). This file's Layer 7 covers only
the deterministic core intake: `fha process` (M7.1-M7.4) and `fha convert-mining` (M7.5).

**Tool conventions (all PRs):**
- One Python file per tool under `tools/`. Tools never import other tools, except the
  sanctioned front doors and orchestrators listed under the dependency overview.
- Shared code only in `tools/_lib.py`.
- Every subcommand defines its own `--root PATH` flag (argparse does not propagate parent flags).
- Exit codes: 0 clean · 1 warnings · 2 errors · 3 tool failure.
- **Engine/interface split.** A command's `run_*` computes and returns a `_lib.Result` (the structured record of what happened - see M1.1); its `_cmd_*` is the only layer that renders that `Result` to stdout/stderr and returns the exit code. `run_*` does not print report text or call `sys.exit`. This is what makes the suite a headless core any front door drives (TOOLING §1) - so every phase's "Done when" implicitly means "the engine returns a `Result` and the interface renders it."
- Every mutating operation ships `--dry-run`.
- Every flag the CLI accepts and every E/W code a tool emits is documented in `tools/README.md`
  in the same PR.

**UX bar (all PRs) - every PR that adds user-visible output must satisfy all four:**
- **No traceback reaches the user.** Catch exceptions at the CLI boundary; translate to a plain cause + exact next command. A Python stack trace on the human's screen is always a defect. (Keep full detail behind `--debug` if useful for developers.)
- **Every user-facing error names cause + exact fix.** "No record found for s-7q2… - run `fha find s-7q2x9c4m1` to check the ID" beats "lookup failed." A message that reports a failure but gives no next step is a dead end, and a dead end is a bug.
- **Jargon ships an example or a valid-list.** Any term from the format reference (EDTF, `source_type`, anchor, claim status, Crockford ID) gets a plain gloss and a concrete example or the set of allowed values, inline in the message - never a bare "invalid X."
- **Messy-but-recoverable input is inferred or asked, never hard-failed.** Loose natural-language dates, slightly malformed hand-edits, or non-canonical spellings that the tool can resolve should produce a single plain question or a proposed normalisation - not a refusal. History is messy; treat imperfect input as normal.

**`fha doctor` and `docs/TROUBLESHOOTING.md` (all PRs):** Any new user-visible failure condition introduced by a PR must be (a) detectable by `fha doctor` with a plain-language next step, and (b) listed in `docs/TROUBLESHOOTING.md` with cause and remedy. A failure mode the human can hit but `fha doctor` cannot surface is a support burden waiting to happen.

---

## Milestones

Each Layer below is one milestone: a batch of capability that can be built and verified on its
own. Layers are split into PR-sized phases so that no single phase is dramatically larger than
its neighbors; splitting a layer into more phases never changes its milestone number.

**Every phase is numbered `M{milestone}.{phase}`** in its `###` heading (e.g. `M3.2`) -
that number is the stable handle for asking for a review of that specific PR ("review
M3.2", or "review M3" for the whole milestone), and code comments and other docs cite it. The
number is positional within its milestone, not tied to a tool name, so renaming a phase's heading
doesn't change its number. New work goes in a new phase at the end of the milestone it extends,
or in a new milestone, rather than renumbering existing phases.

Build the milestones in order: each one depends only on milestones above it. Within a
milestone, build the phases in order unless a phase says otherwise.

| Milestone | Layer | Phases | What it builds |
|---|---|---|---|
| 1 | Layer 1 - Foundation | M1.1-M1.10 | `_lib.py` with the `Result` contract, `fha.py` dispatcher, `fha id` (M1.1); `fha index` (M1.2-M1.3); `fha lint` / `fha check` engine, errors, warnings W101-W114, fix modes and formatter (M1.4-M1.7); `fha stubs` (M1.8); `fha claim` status moves (M1.9); lint warnings W115-W146 (M1.10) |
| 2 | Layer 2 - Archive views & discovery | M2.1-M2.5 | `fha views` timeline / sources-index / draft-queue (M2.1), brackets (M2.2), tree / clean / refresh (M2.3); `fha doctor` (M2.4); `fha find` + `fha search` (M2.5) |
| 3 | Layer 3 - Photo catalog | M3.1-M3.6 | `fha photoindex` scan and grouping (M3.1), `find` (M3.2), `triage` / `report` (M3.3), `reconcile` / `tag-person` (M3.4), `set-summary` (M3.5), `gallery` (M3.6) |
| 4 | Layer 4 - Cross-reference & connection | M4.1-M4.4a | `fha xref` (M4.1); `fha cooccur` (M4.2); `fha find --related` (M4.3); `fha confirm` xref / cooccur / dismiss / place / discovery / draft (M4.4) and `confirm merge` (M4.4a) |
| 5 | Layer 5 - Research report | M5.1-M5.3 | `fha report` §0-4 + snapshot (M5.1), §5/§5b (M5.2), §6-8 (M5.3) |
| 6 | Layer 6 - Data output | M6.1-M6.6 | `fha packet` (M6.1); `fha places lint` / `candidates` (M6.2), `geocode` / `set` / `note` / `edit-note` (M6.3); `fha gedcom` (M6.4); `fha wikitree` (M6.5); `fha gedcom import` (M6.6) |
| 7 | Layer 7 - Intake pipeline (core side) | M7.1-M7.5 | `fha process`: documents (M7.1), photos + `--more` (M7.2), folders + variations (M7.3), bundles (M7.4); `fha convert-mining` (M7.5). `fha capture` is [`BUILD_INGESTION.md`](BUILD_INGESTION.md) |
| 8 | Layer 8 - Publication | M8.1-M8.5 | `fha site`: foundations + source page (M8.1), person page (M8.2), place + discoveries (M8.3), home page + redaction audit (M8.4), trees (M8.5) |
| 9 | Layer 9 - Scaffolding | M9.1-M9.2 | `fha install` + `manifest.json` (M9.1); `fha update-tools` (M9.2) |
| 10 | Layer 10 - Working-copy mode | M10.1 | `fha working-copy on\|off\|status` and the conservative behaviour in every asset-aware tool |
| 11 | Layer 11 - Repair, refiling and promotion | M11.1-M11.9 | `fha reconcile` (M11.1); inbox filing into `documents/{type}/` (M11.2); `fha claim` batch, field edits, `--clear`, `claim new` (M11.3); `fha person promote` + brackets `--fix-promote` + report §7b (M11.4); `fha source extract` (M11.5); `fha process refile` (M11.6); brackets `--realign` + W120 (M11.7); `fha media dedupe` / `probe` (M11.8); `fha reorganize` (M11.9) |
| 12 | Layer 12 - Person and source write-backs | M12.1-M12.7 | `fha person new` / `set-living` / `set-sex` / `set-profile-photo` (M12.1), `rename` (M12.2), `relate` / `estimate` (M12.3), `edit` / `note` / `edit-note` (M12.4); `fha source note` / `edit-note` (M12.5), `clear-keyword` (M12.6), `set-primary` (M12.7) |
| 13 | Layer 13 - Relationships, links and backup | M13.1-M13.3 | `fha relate` (M13.1); `fha normalize-links` (M13.2); `fha backup` (M13.3) |

The sibling build docs continue the sequence: [`BUILD_INGESTION.md`](BUILD_INGESTION.md) (the
MG series - `fha capture`, its recipes, `--ingest`, the native-messaging host and the inbox
contract) needs Layers 1 and 7; [`BUILD_INTERFACE.md`](BUILD_INTERFACE.md) (the MI series - the
workflow skills and the `fha serve` workbench) needs every command its skills and buttons drive.

---

## Dependency overview

```
_lib (incl. Result) ───────────────────────────────────────── all tools
fha.py ───────────────────── dispatches every tool (lazy imports; never imported by a tool)
index ────────────────────── views, find/search, doctor, stubs, claim, xref, cooccur,
│                             report, packet, process, places, site, gedcom,
│                             wikitree, capture, convert-mining, relate, person promote
│
photoindex ──────────────── report (triage §), packet (photos), find --text (D7),
│                             reconcile (photo pass), gallery
│
xref + cooccur + places ─── find --related (D4), report (§8), confirm (write-back)
│
process ──────────────────── capture (hands off to process), refile, reorganize
│
reconcile ────────────────── reorganize (re-checked after every batch)
│
views tree (JSON) ────────── site (tree rendering adapter)
│
site --linked + every write-back engine ── serve (the workbench, BUILD_INTERFACE)
│
install/update-tools ─────── (needs complete tool manifest)
```

`claim` is the claim-review write-back (edits a source's `## Claims` block; needs only `_lib`);
`confirm` is the write-back floor under the read-only detectors (`xref`/`cooccur`/`places
candidates`) plus the report's discovery prompts and the `write-biography` accept gesture.
`person` and `source` are the record write-backs (Layer 12); like `claim`, they locate records by
scanning the tree and need only `_lib`, except `person promote`, which reads the index to derive
a direct-line destination.

Tools with no inbound lines - places, gedcom, wikitree, packet, convert-mining, relate, media,
normalize-links, backup, working-copy - can be built in any order once the index is stable.
(`gedcom_import`, `normalize_links` and `backup` sit even lower: they need `_lib` alone and no
index at all - a first import is expected to run before one exists.)

**Tools import tools only in three sanctioned places:** `fha.py` (the dispatcher), `serve.py`
(the workbench front door, which drives the engines in-process), and an orchestrator that calls
another tool's engine rather than re-implementing it (`report` → `index`/`lint`/`photoindex`/
`places`/`cooccur`, `reconcile` → `photoindex`, `reorganize` → `lint`/`reconcile`, `doctor` →
`lint`/`capture`). Everything shared otherwise lives in `_lib.py`.

---

## Layer 1 - Foundation (Milestone 1)

Everything else depends on these. Build in the order listed.

---

### M1.1 - `_lib.py` + `fha.py` + `fha id`

**One PR.** Create `tools/_lib.py` (shared library - no CLI), `tools/id.py` (minting tool)
and `tools/fha.py`, the thin dispatcher every later tool plugs into. Wire `fha id mint`,
`fha id check` and `fha id find` into `fha.py`.

**`fha.py` - the dispatcher.** Holds no logic. Each tool module exposes `register(subs)`,
which adds its subcommand(s) to one argparse parser; `fha.py` imports the modules lazily
inside `main()`, calls each `register`, and dispatches to `args.func`. It also owns the CLI
boundary the UX bar needs: a global `--root`/`--debug`, a "did you mean" hint for a
mistyped command (`difflib` over the `COMMANDS` tuple), the one-line working-copy banner
(M10.1), and a top-level guard that turns any uncaught exception into a plain message with
`fha doctor` as the next step (the traceback only under `--debug`). A few command shapes
cannot share a parser with their siblings and are intercepted before argparse sees them:
`fha id check` (an alias for `fha find`), `fha claim new` (M11.3), `fha process refile`
(M11.6) and `fha gedcom import` (M6.6) - each because the sibling parser takes a positional
that the verb word would be misread as. `site.py` is loaded by path under a private module
name, because its stem shadows Python's stdlib `site` module.

**`_lib.py` - the four parsing primitives** (TOOLING §1):

```python
ID_RE     = re.compile(r'\b([PSCLH])-([0-9a-hjkmnp-tv-z]{10})\b', re.I)
# [[ID]] / [[ID|display]] / [[ID#frag]] - one capture group (the ID); the optional second
# bracket each side also matches the tolerated single-bracket [ID] form, so one chokepoint
# resolves both. WIKILINK_RE additionally matches name/stem links ([[Ken Smith]]).
TOKEN_RE  = re.compile(r'\[\[?([PSCLH]-[0-9a-hjkmnp-tv-z]{10})(?:#[^|\]]*)?(?:\|[^\]]*)?\]\]?', re.I)
LEGACY_TOKEN_RE = re.compile(r'(?<!\[)\[([PSCLH]-[0-9a-hjkmnp-tv-z]{10})\](?!\])', re.I)
WIKILINK_RE = re.compile(r'\[\[([^\[\]|#]+?)(?:#[^|\]]*)?(?:\|[^\]]*)?\]\]', re.I)
FRONT_RE  = re.compile(r'\A---\r?\n(.*?)\r?\n---\r?\n', re.S)
CLAIMS_RE = re.compile(r'^## Claims.*?```yaml\r?\n(.*?)```', re.S | re.M)
```

`read_record(path) -> {meta: dict, claims: list, stories: str|None, body: str}`.
YAML scalar normalization: booleans and date objects coerce to strings (`'true'`/`'false'`/ISO).
Claims blocks: `yaml.safe_load`; parse failure is collected as a lint error, never a crash.
All file IO is UTF-8.

`edtf_bounds(s) -> (min_iso, max_iso)` - the EDTF subset the tools need (TOOLING §1 table):
year `1850` → `(1850-01-01, 1850-12-31)`; tilde/question `1850~`/`1850?` → widen ±1y;
decade `185X` → full decade; month `1850-05` → month bounds; interval `A/B` → (min(A), max(B));
open `[..1920]` → `(0001-01-01, 1920-12-31)`; after `[1920..]` → `(1920-01-01, 9999-12-31)`.
Validate with the same regex used for E014.

`resolve_path(p, fha_yaml) -> str` - maps the first segment of a record path through
`fha.yaml`'s `roots:` map; missing alias defaults to `{archive_root}/{alias}`.

**`Result` - the engine/interface seam** (TOOLING §1). `_lib.py` also defines the single value
type every command returns from its `run_*`: a small JSON-serializable record carrying `ok`
(no error-level messages), `exit_code` (an `EXIT_*` constant), `data` (the structured payload a
consumer wants), `messages` (human-facing `Message{level, text, next_step, code, path}` lines -
a lint `Finding` folds into one), and `changed` (paths created/written/renamed/embedded, empty
under `--dry-run`). The `EXIT_*` constants live here too. Establishing `Result` in the foundation
PR is deliberate: it is the contract every later tool's engine returns and every front door reads,
so it must exist before the first `run_*` is written. The reference renderer arrives with `fha
lint` (M1.4).

**`fha id mint [TYPE] [-n N]`** - draw N (default 1) IDs of type `P|S|C|L|H`.
Algorithm: `secrets.choice` over Crockford Base32 alphabet `0-9a-hjkmnpqrstvwxyz` (omit
`i l o u`), 10 chars. Verify absence by scanning the tree with `ID_RE`; retry on collision.
`fha id check <ID>` and `fha id find <ID>` are aliases for `fha find <ID>` (wired through the
`fha.py` dispatcher; `find.py` is the canonical implementation, built in M2.5).

**Done when:**
```sh
python tools/id.py mint P           # prints one p-id
python tools/id.py mint S -n 5     # prints 5 s-ids; none duplicate each other
python -c "from _lib import edtf_bounds; print(edtf_bounds('185X'))"
# ('1850-01-01', '1859-12-31')
fha lnt                             # "unknown fha command 'lnt'. Did you mean `lint`?"; no traceback
python -m unittest tests.test_id tests.test_lib_tokens tests.test_mint_scan tests.test_fha_cli
```

---

### M1.2 - `fha index` - schema + full rebuild

**One PR.** New file `tools/index.py`. Wire `fha index [--root PATH]` into `fha.py`
(TOOLING §2). This phase ships the schema and the from-scratch path only; incremental
`--source` upsert and relationship derivation are M1.3.

**Full rebuild algorithm:**
1. Glob `sources/**/*.md`, `people/**/*.md`, `places/places.yaml`, `notes/**/*.md`.
2. Parse each with `read_record()`; insert in one transaction, building the `aliases` table from every record's `aliases:` plus persons'/places' `name` & variants.
3. Scan all prose bodies for `TOKEN_RE` → `citations` table, storing each citation's resolved canonical ID (a `[[stem]]`/`[[Name]]` reference resolves through the alias map). Strip the `[[ ]]` wrapper from frontmatter `people:`/`places:`/note `persons:`/`sources:` and resolve each to an ID (`source_people`/`source_places`); add on-demand c-id aliases (a source aliases a `c-…` only when a `[[c-…]]` citation exists).
4. Glob asset trees for filenames carrying s-ids → `source_files` reconciliation.
5. Build FTS tables.

**Schema** (TOOLING §2 DDL - create verbatim):

```sql
CREATE TABLE persons(id TEXT PRIMARY KEY, name TEXT NOT NULL, surname TEXT, sex TEXT,
  living TEXT NOT NULL, tier TEXT NOT NULL, status TEXT DEFAULT 'active',
  merged_into TEXT, no_known_marriages INTEGER DEFAULT 0, no_known_children INTEGER DEFAULT 0,
  birth TEXT, death TEXT,   -- optional provisional, unsourced vital estimates (SPEC §9)
  path TEXT NOT NULL);
CREATE TABLE person_variants(person_id TEXT, variant TEXT);
CREATE TABLE person_face_tags(person_id TEXT, tag TEXT);
CREATE TABLE person_files(person_id TEXT, kind TEXT, path TEXT, generated INTEGER DEFAULT 0,
  PRIMARY KEY(person_id, kind));
CREATE TABLE person_external(person_id TEXT, system TEXT, ext_id TEXT);

CREATE TABLE sources(id TEXT PRIMARY KEY, title TEXT NOT NULL, source_type TEXT,
  date_edtf TEXT, date_min TEXT, date_max TEXT, repository TEXT,
  restricted INTEGER DEFAULT 0, source_class TEXT, publication_ok INTEGER,
  status TEXT DEFAULT 'active', superseded_by TEXT, path TEXT NOT NULL);
CREATE TABLE source_files(source_id TEXT, path TEXT, role TEXT, copy TEXT,
  derived INTEGER DEFAULT 0, original_filename TEXT,
  exists_on_disk INTEGER, in_inventory INTEGER, is_primary INTEGER DEFAULT 0);

CREATE TABLE claims(id TEXT PRIMARY KEY, source_id TEXT NOT NULL, type TEXT NOT NULL,
  subtype TEXT, date_edtf TEXT, date_min TEXT, date_max TEXT,
  place_id TEXT, place_text TEXT, value TEXT NOT NULL, status TEXT NOT NULL,
  reviewed TEXT, confidence TEXT, information TEXT, evidence TEXT,
  anchor TEXT, hypothesis TEXT,
  negated INTEGER DEFAULT 0, notes TEXT);
CREATE TABLE claim_persons(claim_id TEXT, person_id TEXT, position INTEGER, role TEXT);
CREATE TABLE claim_links(claim_id TEXT, rel TEXT, target_id TEXT);
CREATE TABLE source_people(source_id TEXT, person_id TEXT);
CREATE TABLE source_places(source_id TEXT, place_id TEXT);   -- from a source's frontmatter places: links

-- relationships is populated by M1.3 (incremental upsert + derivation);
-- create it here so the schema is complete from the first migration.
CREATE TABLE relationships(person_id TEXT, rel TEXT, other_id TEXT,
  claim_id TEXT, date_start TEXT, date_end TEXT);

CREATE TABLE places(id TEXT PRIMARY KEY, name TEXT, hierarchy TEXT,
  within TEXT, lat REAL, lon REAL);
CREATE TABLE place_names(place_id TEXT, alt_name TEXT);
CREATE TABLE place_history(place_id TEXT, period_edtf TEXT, date_min TEXT, date_max TEXT,
  hierarchy TEXT);

CREATE TABLE search_log(date TEXT, person_id TEXT, question TEXT,
  repository TEXT, collection TEXT, terms TEXT, result TEXT, source_id TEXT, path TEXT);
-- working answers: one row per question entry that carries an h- id (SPEC §17a/b). Built from
-- the `## Q:` entries in notes/questions.md and research files' `## Open Questions`; the entry
-- is the truth, this is only its query surface. status is the question's own status.
CREATE TABLE hypotheses(id TEXT PRIMARY KEY, person_id TEXT, question TEXT, guess TEXT,
  basis TEXT, verify TEXT, origin TEXT, status TEXT, path TEXT);
-- every person a working answer is about: the p- ids in its question's refs:, or the research
-- file's own person when refs: names nobody. person_id above is the first of them.
CREATE TABLE hypothesis_persons(hypothesis_id TEXT, person_id TEXT,
  UNIQUE(hypothesis_id, person_id));
CREATE TABLE citations(token TEXT, kind TEXT, path TEXT, line INTEGER);  -- token = resolved canonical ID
-- aliases: the resolution surface (canonical ID + human stems + on-demand c-ids + person/place name & variants)
CREATE TABLE aliases(alias TEXT, canonical_id TEXT, kind TEXT);  -- kind: id | stem | name | variant | claim

CREATE VIRTUAL TABLE notes_fts USING fts5(path, content);
CREATE VIRTUAL TABLE transcripts_fts USING fts5(source_id, path, content);
```

**Done when:**
```sh
fha index --root example-archive         # exits 0; .cache/index.sqlite created
sqlite3 .cache/index.sqlite "SELECT count(*) FROM claims"  # non-zero
sqlite3 .cache/index.sqlite "SELECT count(*) FROM relationships"  # zero - M1.3 populates this
python -m unittest tests.test_index tests.test_index_freshness tests.test_freshness_manifest \
  tests.test_alias_layer tests.test_consumer_sweep tests.test_tools_utf8_stdout
```

---

### M1.3 - `fha index` - incremental upsert + relationship derivation

**One PR.** Extend `tools/index.py`. Wire `fha index --source s-id [--root PATH]`
(TOOLING §2).

**Incremental mode** (`--source s-id`): delete then re-insert one source's rows. Deletion
order matters - delete `claim_persons` and `claim_links` before `claims`; delete `citations`
and `notes_fts` rows for the source path before `sources`. Reversing order leaves orphans.

**Relationship derivation** (after claims load, in both full-rebuild and incremental mode):
for each `accepted` claim - `relationship` whose `roles:` name a child and a parent →
`(child, 'parent', father)` + reciprocal; `birth` with a `roles:` map naming a child and a parent
→ the same pair, through the same writer (roles only, never a positional fallback - TOOLING §2,
SPEC §8.3); `marriage`, or a `relationship` whose `roles:` name spouses → reciprocal `spouse` edges with
`date_start`/`date_end`; social subtypes → `friend`/`associate`/`neighbor` edges. Edges are
pure cache, re-derived from claims on every build - never hand-edited.

**Done when:**
```sh
fha index --root example-archive         # full rebuild now also derives relationships
sqlite3 .cache/index.sqlite "SELECT count(*) FROM relationships"  # non-zero
fha index --source s-4f5f215e60 --root example-archive  # incremental upsert; exits 0
python -m unittest tests.test_index tests.test_merged_survivor
```

---

### M1.4 - `fha lint` - engine + structural/reference errors (E001-E010)

**One PR.** New file `tools/lint.py`. Wire `fha lint [--root PATH] [--json]` into `fha.py`,
plus `fha check` as an alias registered on the same parser (TOOLING §3). This phase ships the
lint engine and the first ten error codes only; the remaining errors (E011-E020, M1.5), the
warning codes (W101-W114 in M1.6, W115-W146 in M1.10), and the fix/formatter flags (M1.7) are
later phases in this same layer.

**Lint is the reference `Result` renderer.** `run_lint` returns a `_lib.Result` (M1.1): each
`Finding` folds into a `Message` (severity → level, E/W code → code, file → path), and `_cmd_lint`
renders both the human `SEVERITY CODE path: message` lines and the `--json` payload from that one
`Result`. Build this split here, cleanly, because every later tool copies it - `run_*` returns the
data, `_cmd_*` formats it.

Lint builds its own in-memory index (does not require `fha index` to have run first).
Runs file-by-file then cross-file passes. Collect all findings; print all; never crash on
one bad file.

**Error codes** (TOOLING §3 table, first ten):

| Code | Detection |
|------|-----------|
| E001 | Duplicate p-id across two primary profiles; duplicate S/C/L/h-id anywhere |
| E002 | Filename fails §13 grammar; ID fails `ID_RE` |
| E003 | Filename ID suffix ≠ frontmatter `id` |
| E004 | Any `[token]`, `persons:`, `place:`, `corroborates/contradicts` target not found |
| E005 | p-id appears anywhere but no person record exists for it |
| E006 | `accepted` claim missing `reviewed` field (disputed/rejected/superseded without it: W139, a warning) |
| E007 | Claim `type` outside §8.2 vocabulary |
| E009 | `contradicts:` link present but no open question in `notes/questions.md` references both c-ids |
| E010 | Missing required frontmatter fields: `id`, `type`, `title` (sources); `id`, `name`, `tier`, `living` (persons) |

**E013 note (forward reference):** the summary-block cross-check is E013, built in the next
phase alongside the other inventory-facing codes - it needs the same record-parsing
machinery this phase establishes, but the codes are numbered out of build order.

**Done when:**
```sh
fha lint --root example-archive              # exits 0 - no codes built yet fire on this fixture
fha check --root example-archive             # same report as fha lint
python -m unittest tests.test_lint          # each of E001-E010 fires on a broken archive the test writes
```

Most code tests build their broken archive in a temporary folder inside the test; a code that
needs a richer tree than a test can write inline gets a committed `tests/fixtures/broken-{CODE}/`.

---

### M1.5 - `fha lint` - inventory, keyword, vocabulary and merge errors (E011-E020)

**One PR.** Extend `tools/lint.py`. Wire `[--with-exif]` into the existing `fha lint`
command (TOOLING §3).

**Error codes** (TOOLING §3 table, remainder):

| Code | Detection |
|------|-----------|
| E011 | `files:` entry missing on disk (resolved via `fha.yaml`); or on-disk file carrying s-id absent from inventory |
| E012 | `SOURCE:` keyword ↔ record inventory disagreement (photos root; requires `--with-exif`) |
| E013 | Summary block `**Born/Died/Married/Parents/Children:**` citations don't match accepted claims |
| E014 | Non-EDTF date value (validate against `edtf_bounds` regex) |
| E015 | `type: relationship` claim missing `roles:` map, or `roles:` that is not a map |
| E017 | DNA source not `restricted: true`, or DNA file outside `documents/dna/` |
| E018 | `AGENTS.md` or skills reference deprecated commands or contradict locked rules |
| E019 | Claim `status` outside the SPEC §8.1 lifecycle set, or `confidence` outside `high`/`medium`/`low` (§8.5) |
| E020 | `status: merged` record whose `merged_into:` names no person, or loops (claims stay on the tombstone) |

E016 is unassigned: a claim naming a merged p-id counts for the survivor (SPEC §9) and is
reported as the W107 warning (M1.6). E008 is unassigned: SPEC 2.1 retired the `significance`
override it checked, and a leftover field is the W143 warning (M1.10).

**E013 parsing detail.** Scan the summary text (H1 to first `## Section`) with `finditer` on
`**Label:**` pattern - do NOT split by line (inline multi-label form exists). Extract
`[[s-id]]` and `[[p-id]]` links per segment; compare to accepted claims of the matching type.

**E011 `missing-fixture` suppression.** Suppress entirely when the archive path is under
`example-archive/` or `tests/fixtures/` - stub asset references are intentional in those
fixtures. (An arbitrary directory merely *named* `tests` in a real archive is not fixture
space; see `is_fixture_path`.)

**Done when:**
```sh
fha lint --root example-archive              # exits 0 - W101 isn't built until M1.6
fha lint --root example-archive --with-exif  # exits without crash (E012 path)
python -m unittest tests.test_lint tests.test_embedded_keywords   # each of E011-E020 fires on its broken archive
```

---

### M1.6 - `fha lint` - warning codes (W101-W114)

**One PR.** Extend `tools/lint.py`. No new flags - warnings ride the existing `fha lint`
invocation (TOOLING §3).

**Warning codes:**

| Code | Detection |
|------|-----------|
| W101 | Vitals gaps per curated person (missing accepted birth/death claims) |
| W102 | `suggested` claim backlog per source |
| W103 | Couple-folder bracket list `[child, …]` doesn't match accepted relationship claims |
| W104 | Summary block line with no supporting accepted claim |
| W105 | Hand-edits under a GENERATED header - reserved: detection needs mtime tracking, so the check is a no-op |
| W106 | Accepted claim missing Mills analysis fields |
| W107 | Direct reference to a merged person - a prose link, or a claim's `persons:` (counts for the survivor meanwhile) |
| W108 | `README.md` older than last `SPEC.md` change |
| W109 | Non-vital accepted claim missing `notes` context; also catch-all for unrecognized `source_type` |
| W110 | Ahnentafel placement issue (requires `root_person` in `fha.yaml`) |
| W111 | Record using `aliases:` omits its own canonical ID (auto-fixed by `fha normalize-links`) |
| W112 | Latent alias clash - one alias string names ≥2 records, nothing links by it yet |
| W113 | Active alias clash - an ambiguous `[[name]]` link in use; manual detangle, never auto-resolved |
| W114 | Claim-shaped YAML under `## Claims` without a ` ```yaml ` fence (read anyway; `--fix-claims-fence`) |

Plus an informational **needs-sourcing backlog** (not a finding, never moves the exit code): a provisional `birth:`/`death:` or `(TODO: import source)` line with no backing accepted claim.

**Done when:**
```sh
fha lint --root example-archive              # exits 1; the documented baseline (canonical list: TOOLING §15)
fha lint --root tests/fixtures/broken-W103  # fires W103
fha lint --root tests/fixtures/broken-W110  # fires W110
python -m unittest tests.test_lint tests.test_provisional_vitals   # each W code fires; the backlog is listed
```

---

### M1.7 - `fha lint` - fix modes + formatter

**One PR.** Extend `tools/lint.py`. Wire `[--dry-run] [--mint-stubs] [--spawn-questions]
[--fix-claims-fence] [--fix-ids] [--fix-reciprocal] [--fix-surnames] [--format-check]
[--format-write]` into `fha.py` (TOOLING §3).

**Fix modes** (gated behind explicit flags; always diff-previewed with `--dry-run`):
- `--mint-stubs` (E005): create stubs in `people/stubs/`
- `--spawn-questions` (E009): append templated question to `notes/questions.md`
- `--fix-claims-fence` (W114): wrap unfenced `## Claims` content in a ` ```yaml ` fence
- `--fix-ids`: mint + rename an id-less hand-authored record to the §13 grammar, keeping the
  slug and the verbatim filename stem as aliases so existing `[[name]]` links keep resolving; a
  template placeholder id (all underscores) counts as missing. The same pass mints missing claim
  `id:`s and stamps `reviewed:` on a hand-written `accepted` claim it mints an id into.
- `--fix-reciprocal` (W116, M1.10): append the missing mirror entry to the other person's
  `relationships:` block
- `--fix-surnames` (W138, M1.10): write the `surname:` line the file name implies, right after `name:`

E011 has no fix flag: re-tying a moved file is `fha reconcile` (M11.1), which E011's message names.

**Formatter** (`--format-check` / `--format-write`): LF endings and one final newline on every
`.md`; on source records and person profiles also frontmatter key order (SPEC §14 / §9, unknown keys
after), `files:` entry key order, lowercase IDs in ID fields, `value` first in each claim, blank-line
runs collapsed outside fences, and whole-section moves into the §14 / §16 order when nothing is in
doubt (else reported). Line-based, so comments survive; a rewritten block must parse back to the
same data. `--format-write` applies what `--format-check` reports. Never rewrites prose. Link
normalization is the separate `fha normalize-links` (M13.2), deliberately not part of the formatter.

**Done when:**
```sh
fha lint --root example-archive --format-check  # reports, writes nothing
fha lint --root <scratch-archive> --format-write # a second --format-check finds nothing
fha lint --root example-archive --mint-stubs --dry-run  # previews stub creation, writes nothing
fha lint --root example-archive --spawn-questions --dry-run
python -m unittest tests.test_lint tests.test_graduation   # --fix-ids takes a by-hand archive to IDs
```

---

### M1.8 - `fha stubs`

**One PR.** New file `tools/stubs.py`. Wire `fha stubs [--root PATH] [--from-names "…"]
[--dry-run] [--force]` into `fha.py` (TOOLING §5). `--force` mints a name even when a person of
that name already exists (otherwise the duplicate is refused, naming the existing record).

**Scan mode.** Collect all p-ids from the index (or in-memory lint pass) that lack a person
record. For each: create `people/stubs/{surname}__{given}_{p-id}.md` with minimal frontmatter
(`id`, `name`, `tier: stub`, `living: unknown`). Name/surname resolved from the claim `value`
text where parseable; else `unknown__unknown_{p-id}.md` (flagged for hand-rename). Never
overwrites; never moves a stub out of `stubs/`.

**`--from-names "Ethel Hartley; Frances Hartley"`** - mint new IDs and create stubs
interactively. One stub per semicolon-delimited name.

**Done when:**
```sh
fha stubs --root example-archive         # creates stubs for any unresolved p-ids
fha lint --root example-archive          # E005 count drops to 0 after stubs minted
fha stubs --from-names "Test Person" --root example-archive  # creates one stub
python -m unittest tests.test_stubs tests.test_person_names
```

---

### M1.9 - `fha claim` - claim-review write-back

**One PR.** New file `tools/claim.py`. Wire `fha claim <c-id> --status accepted|disputed|rejected|
superseded|suggested [--reviewed DATE] [--value "…"] [--date EDTF] [--root PATH] [--dry-run]`
into `fha.py` (TOOLING §3b). The first four `--status` choices are the SPEC §8.1 review outcomes a
human moves a claim into (`suggested → accepted | disputed | rejected | superseded`). A claim the
reviewer has looked at but cannot settle yet stays `suggested`: `--status suggested` keeps it there
and writes the `reviewed:` date (today, or `--reviewed DATE`), which is what marks it "checked,
still open" (called reviewed-suggested below). Belongs in the foundation layer: it is the deterministic half of the
review flow whose codes lint already defines (E006 - an `accepted` claim must carry `reviewed:`),
and it needs only `_lib` (`read_record`, the `## Claims` parsing, `Result`).

**The human gate, from the engine side.** Moving a claim's `status:` is the one write a reviewer
reaches for constantly; this tool makes it a contract-safe CLI action any front door can drive
(SPEC §8.2). Only the human moves a claim to `accepted`, and an accepted claim must carry a
`reviewed:` date - so `--status accepted` always stamps `reviewed:` (the given `--reviewed DATE`,
else today). The tool never accepts on its own; it only executes a decision a human directs.
`disputed`/`rejected`/`superseded` change status rather than delete, preserving the trail
(`disputed` = actively contested, distinct from a ruled-out `rejected`).

**Surgical edit.** Touch only the one named claim's entry inside its source `.md` `## Claims`
block - sibling claims, key order, and hand comments survive (same discipline as `places geocode`
and `process --more`). Locate the claim by scanning `sources/` directly (the `.md` files are the
truth), so it works when `.cache/index.sqlite` is stale or absent; a `value:` that is a YAML
block scalar is refused, not corrupted. `run_claim` returns a `Result` whose `changed[]` names the
edited source file; re-run `fha index` after a write to fold the new status into the query surface.

**Done when:**
```sh
fha claim c-xxxxxxxxxx --status accepted --root example-archive   # status flips; reviewed: stamped today
fha claim c-xxxxxxxxxx --status rejected --dry-run --root example-archive  # previews; writes nothing
fha lint --root example-archive          # an accepted claim with reviewed: stays E006-clean
python -m unittest tests.test_claim
```

Batch moves, the field-edit flags, `--clear` and `fha claim new` extend this verb in M11.3.

---

### M1.10 - `fha lint` - warning codes (W115-W146)

**One PR** (or one per group below; each code is independent). Extend `tools/lint.py`. No new
flags beyond the two fix modes M1.7 lists (`--fix-reciprocal`, `--fix-surnames`). Every code
here is a warning: it moves the exit code to 1, never to 2. The full detection rule and the
message wording for each is TOOLING §3; the table is the build checklist.

Several codes read the same derivation rule as another tool, and must read it through the same
`_lib` function rather than a second copy - a lint twin that reads a narrower set than the tool
it mirrors reports correct records as wrong. The shared rules: `_lib.parentage_parties` (W103,
W115, W126), `_lib.spouse_parties` (W115, W125), `_lib.vital_subjects` (W132),
`_lib.claim_death_reading` (W136, shared with `fha claim`'s accept path), `_lib.merge_pointer`
(W140), and `_lib.build_ahnentafel_map` (W119, W120, W127, W129, shared with `fha views brackets`).

| Code | Detection |
|------|-----------|
| W115 | Sourced `relationships:` entry with no matching accepted kin claim, or the reverse, or a subtype mismatch |
| W116 | Relationship edge recorded on one person without its mirror on the other (`--fix-reciprocal`) |
| W117 | Person filename with no `__` sort separator - guidance, never an error |
| W118 | Claim whose `persons:` resolves to none of the people it names |
| W119 | Direct-line ancestor (derived Ahnentafel position ≥ 2) still a stub - a research lead (requires `root_person`; the promotion verbs are M11.4) |
| W120 | Ahnentafel slot defaulted: one resolved parent with unrecorded `sex:` took the even slot |
| W121 | A `roots:` value changed and orphaned filed assets (compared against `.cache/roots.json`) |
| W122 | Person filename ends in a companion-kind word but the file is a person record |
| W123 | A record folder lint could not open (one warning per folder, emitted last) |
| W124 | Accepted claims on a source whose files carry no searchable text |
| W125 | Accepted, non-negated `marriage`/`divorce` naming two or more people that yields no couple |
| W126 | Accepted, non-negated `birth` naming two or more people that yields no parent edge, or one person listed as both child and parent; a misspelt or plural derivation role (`_lib.role_near_miss`) on a `birth` or `relationship` claim |
| W127 | `root_person` has an accepted genetic child and `root_generation` is not `children` |
| W128 | A file lint tried to read is not UTF-8 (reported, never crashes the run) |
| W129 | `root_generation` is neither `self` nor `children` (skips the Ahnentafel pass) |
| W130 | Curated profile missing one of the four SPEC §16 sections |
| W131 | Documents-root asset carries an embedded keyword naming a person absent from its source's `people:` (`--with-exif` only) |
| W132 | Accepted or reviewed-suggested `death`/`burial`/`baptism` naming two or more people with no `roles:` map |
| W133 | A `roles:` value names a person absent from the claim's `persons:` |
| W134 | `source_type: ephemera` with a non-empty `people:` |
| W135 | The same person named twice in one claim's `persons:` |
| W136 | Person still `living: true`/`unknown` who is the deceased on an accepted death or burial claim |
| W137 | Prose or a claim names an existing asset by folder path, and no source owns that file |
| W138 | A person file's name sorts them under a surname their record no longer reads (`--fix-surnames`) |
| W139 | `disputed`/`rejected`/`superseded` claim with no `reviewed:` date |
| W140 | Half a merge: `merged_into:` without `status: merged`, or the reverse |
| W141 | Claim `information`/`evidence`, source `source_class` or person `sex` outside its vocabulary (`_lib` constants) |
| W143 | A source still carrying a retired field (claim `significance`/`significance_reason`/`asset`, file `language`/`digitized`): one line per record, "safe to delete" |
| W144 | `ai_passes:` not a list, an entry without `date` (EDTF), `by` and `task`, or a retired `## AI Passes` body section (SPEC §14) |
| W145 | More than one `files:` entry marked `is_primary: true`, or an old `role: primary` mark (SPEC §14) |

W119, W120, W127 and W129 each have a twin in `fha views brackets` (M2.2, M11.4, M11.7) built
on the same map. W121 is also surfaced by `fha doctor` and `fha index`, so it fires on the first
command after the `fha.yaml` edit.

**Done when:**
```sh
fha lint --root tests/fixtures/broken-W120   # fires W120
fha lint --root example-archive              # still exits 1 on the documented baseline only
python -m unittest tests.test_lint tests.test_lint_relationships tests.test_lint_w137 \
  tests.test_root_generation tests.test_roots_change tests.test_brackets_spouse_w120
```

---

## Layer 2 - Archive views & discovery (Milestone 2)

Depends on: index.

---

### M2.1 - `fha views` - timeline, sources-index, draft-queue

**One PR.** New file `tools/views.py`. Wire `fha views timeline`, `fha views sources-index`,
and `fha views draft-queue` into `fha.py`. Stub `brackets`, `tree`, `clean`, `refresh` as
"not yet implemented" so the CLI is coherent (TOOLING §7) - M2.2 builds `brackets`, M2.3
builds `tree`/`clean`/`refresh`.

All three sub-commands require a fresh index (exit 3 if absent). All write GENERATED-headed
`.md` files into the tree:
```
<!-- GENERATED by fha views <sub-command> on <ISO-date> - do not edit; regenerate instead -->
```

**`fha views timeline [p-id | --all-curated]`** → `…_timeline_{p-id}.md`.
Query: `claims JOIN claim_persons WHERE person_id = ? AND (status = 'accepted' OR (status = 'suggested' AND reviewed IS NOT NULL))
ORDER BY` each date's known bound, undated last (`_lib.claim_date_order_sql`: `date_min`, but
`date_max` for an open-start `[..1895]`, whose `date_min` is the 0001-01-01 floor). Group by decade
(the year read from `date_edtf`). Line
format: `{date_edtf} - {type}: {value} [@ {place_text_or_name}] [{source_id}]`. After main
chronology, emit `## Unreviewed` section listing the undated `suggested` claims in the same format.
`--all-curated`: iterate every person in `persons WHERE tier = 'curated'`.

**`fha views sources-index [p-id | --all-curated]`**.
Union of source_ids from (a) `claim_persons → claims.source_id` and (b) `source_people`.
Group by `source_type`; each line: `{title} [{s-id}]` then the record path. The list is the
generated `## Sources` region inside the person's own profile (SPEC §16, §21b), rewritten in
place between its markers - it works on a stub too. There is no couple-folder file (SPEC Part
IV: a couple folder's two profiles already list their sources).

**`--format md|html`** on timeline and draft-queue (and on `refresh`): `html` renders a
standalone printable page under `generated/views/` (TOOLING §7 D11: GENERATED marker on line 1,
inlined `design/view.css`, a private-artifact banner). The `## Sources` region has no HTML
form; `sources-index --format html` refuses.

**`fha views draft-queue [p-id | --all-curated]`** → `…_draft-queue_{p-id}.md`.
Load the person's profile body. Extract all `[[s-id]]` links via `TOKEN_RE`. Query all accepted
claims for the person → collect distinct `source_id`s. Set-diff: sources with accepted claims
NOT represented by any `[[s-id]]` link = the uncited backlog. Per uncited source: show title +
`[[s-id]]`, then indent each uncited claim as `{type}: {value} - {date_edtf}`. If diff is empty,
write: `All accepted claims are cited in the profile.`

**Done when:**
```sh
fha views timeline p-de957bcda1 --root example-archive   # file generated; GENERATED header
fha views sources-index --all-curated --root example-archive
fha views draft-queue p-de957bcda1 --root example-archive  # non-empty (profile is sparse)
fha lint --root example-archive   # still exits 1 (documented baseline, TOOLING §15); W105 does not fire on GENERATED files
fha views timeline p-de957bcda1 --format html --root example-archive   # page under generated/views/
python -m unittest tests.test_views tests.test_views_html tests.test_views_index_sync \
  tests.test_views_companion_view_marker tests.test_views_stub_guard
```

---

### M2.2 - `fha views` - brackets (folder maintenance)

**One PR.** Extend `tools/views.py` with `brackets`; stub `tree`, `clean`, `refresh` as
"not yet implemented" so the CLI is coherent (TOOLING §7).

**`fha views brackets [--fix] [--dry-run] [--yes]`** - folder maintenance, three concerns in one
pass. Two later phases add to it: M11.4 adds the fourth check (W119 direct-line stubs, graduated
with `--fix-promote`, depth limited by `--generations`), and M11.7 adds the `+ second spouse`
half of W103, the W120 defaulted-slot note and the combined `--realign` pass. `--yes` skips the
Apply? gate for scripts and harnesses.

1. *Bracket list refresh (W103)*: walk `people/` for couple folders (dirs starting with digits
   directly under `people/` or `people/connections/`). Parse current `[child, …]` suffix.
   Query accepted relationship claims for each person in the folder with `roles.parent` → derive
   child names. If current ≠ derived: report diff; with `--fix`, `os.rename` the folder.

2. *Ahnentafel number verification (W110, requires `root_person` in `fha.yaml`)*: BFS from
   `root_person` over `relationships WHERE rel='parent'`. Assign positions: `sex: M` → 2n,
   `sex: F` → 2n+1; same-sex/unknown → lexicographically-first p-id takes 2n. Compare each
   couple folder's numeric prefix against the derived even Ahnentafel number; report mismatches;
   with `--fix`, rename. Skip if `root_person` absent.

3. *Person file placement (W110)*: for each direct-line person in the Ahnentafel map, verify
   all companion files (profile, research, timeline, sources-index, draft-queue) are in the
   correct couple folder. With `--fix`, `shutil.move` them. Never move persons in
   `connections/` or `stubs/`.

`--fix` always prints a full preview before writing. `--dry-run` prints preview and exits.

**Done when:**
```sh
fha views brackets --root example-archive           # no W103/W110 on clean fixture
fha views brackets --root tests/fixtures/broken-W103  # reports stale bracket
fha views brackets --root tests/fixtures/broken-W110  # reports wrong Ahnentafel placement
python -m unittest tests.test_views tests.test_bracket_names tests.test_bloodline_ahnentafel \
  tests.test_folder_names_windows_safe
```
This step creates `tests/fixtures/broken-W103/` and `tests/fixtures/broken-W110/`.

---

### M2.3 - `fha views` - tree, clean, refresh

**One PR.** Extend `tools/views.py` with the remaining sub-commands (TOOLING §7).

**`fha views tree <p-id> --mode ancestors|descendants|fan [--generations N]
[--format json|dot] [--out FILE]`** - traverses `relationships` edges (TOOLING §7):

- `ancestors`: BFS following `rel='parent'` edges recursively.
- `descendants`: BFS following `rel='child'` edges; for each visited descendant, add one-hop
  `rel='spouse'` edges as leaf nodes (don't recurse into spouse lineage).
- `fan`: BFS all edge types, 2-hop default; `--generations N` overrides depth.
- Cycle guard: visited-set on p-ids.

Output - **neutral tree JSON** (spec-pinned data contract):
```json
{
  "seed": "p-…", "mode": "descendants",
  "nodes": [{"p_id": "p-…", "name": "…", "sex": "M",
              "vitals": {"birth": "1840~", "death": null}}],
  "edges": [{"type": "child", "from": "p-…", "to": "p-…",
              "claim_id": "c-…", "dates": {"start": null, "end": null}}]
}
```
Node `vitals`: first accepted `birth`/`death` claim's `date_edtf` or null.
Edge `dates`: populated only for `spouse` edges (marriage date / divorce or death date from
`relationships.date_start`/`date_end`); null for all other types.
Deduplicate: nodes by p-id; edges by `(from, to, type)`.
`--format dot`: GraphViz DOT with `{name}\n({birth} - {death})` labels.
`--format html`: refused with a pointer to `fha site`, which owns the rendered tree (M8.5).

**`fha views clean [--dry-run]`**: walk `people/` for `.md` files whose first non-blank line
matches `<!-- GENERATED by fha `. Delete them. `--dry-run` lists only.

**`fha views refresh [--format md|html|both]`**: require fresh index. Run in sequence:
`timeline --all-curated`, `draft-queue --all-curated`, `sources-index --all-curated`.

**Done when:**
```sh
fha views tree p-de957bcda1 --mode descendants --format json --root example-archive
# valid JSON; vitals populated; edges carry claim_id
fha views tree p-de957bcda1 --mode ancestors --format dot --root example-archive
fha views clean --root example-archive --dry-run    # lists GENERATED files
fha views refresh --root example-archive            # regenerates all; lint still exits 1 (documented baseline, TOOLING §15)
```

---

### M2.4 - `fha doctor`

**One PR.** New file `tools/doctor.py`. Wire `fha doctor [--root PATH]` into `fha.py`
(TOOLING §3a).

Run the checks in order; collect results; print structured report (each check carries a
`checks[].id` and a `next_step`). Exit 0 = all pass; 1 = warnings only; 2 = errors (any E-level
finding or unreachable root). A printed `next:` is spelled with the archive launcher
(`./fha …`, `.\fha …` on Windows), since `fha` is a file at the archive root, not a program on
PATH.

| Check | Error / Warning |
|-------|----------------|
| Archive root found + `fha.yaml` parses | Fatal exit 2 if either fails |
| Mapped roots in `fha.yaml` reachable | ✓/✗ per root; unreachable = exit 2 |
| `exiftool` on PATH | ✗ = exit 1 (warning; not a hard dep for most commands) |
| PyYAML importable | ✗ = exit 2 |
| Optional dependencies | Jinja2 missing = warning (`fha site` needs it); Pillow, pypdf, pdftotext, PyAV = info; `ffprobe` = warning like exiftool. Each names its install command |
| Index freshness | absent/stale = exit 1 (D5: absent is a warning, not an error) |
| Photoindex freshness | absent/stale = exit 1 (D5 same) |
| Lint summary | import-and-call `run_lint_silent(root)`; E-level findings = exit 2 |
| Inbox aging | items older than 14 days; print count + oldest (only if `inbox/` exists) |
| Counts | restricted sources; living persons; unknown-living persons (from index if fresh) |
| E018 findings | list agent-instruction drift findings if present |
| Backup recency | always print; read `.cache/last_backup.json` (the `fha backup` stamp, M13.3): last-backup date + zip when present, "none recorded - run `fha backup`" when absent; info-level, never changes the exit code; end with the archive-root + mapped-roots list a full backup must cover |
| Git protection | `.gitattributes` protects the mapped asset roots (`git check-attr`); no `.gitignore` pattern silently untracks anything under `sources/` (`git check-ignore -v`, remedy worded for where the pattern lives); silent when git is absent |
| Staged captures | bundles waiting in the capture staging folder, with `fha capture --ingest` as the next step (BUILD_INGESTION MG2.1); silent when the folder does not exist |
| Back scans | an unlisted `-back` sibling beside a listed photo (one candidate: the exact `fha process … --more … back` command; two or more: one command per candidate, never a guess) |
| Main image | a record whose `is_primary` file is a photo's back when a front exists in the same group; next step `fha source set-primary` (M12.7) |
| Safety copies | `originals_backup:` on, set but unusable (warning), or not set up (info, with a ready-to-paste line) - TOOLING §13f |

Index and photoindex freshness check: compare `os.path.getmtime(index.sqlite)` against
`max(mtime)` across all `.md` files under `sources/`, `people/`, `notes/`, and
`places/places.yaml`. If the schema is empty or unreadable → treat as absent, not stale.

**Done when:**
```sh
fha doctor --root example-archive   # exits 1 (index may be stale; that's a warning)
# every check in the table appears in output
# removing fha.yaml → exits 2 immediately
python -m unittest tests.test_doctor tests.test_doctor_originals_backup tests.test_gitignore
```

The git, back-scan, main-image and safety-copy checks read things later milestones write; build
each with the milestone it guards (M7.2 for back scans, M12.7 for the main image, M7.2's
`originals_backup:` helper for safety copies).

---

### M2.5 - `fha find` + `fha search` - ID lookup, text search, JSON results

**One PR.** New file `tools/find.py`. Wire `fha find [ID | --text "…" | --related ID]
[--date EDTF] [--json] [--kind KIND …] [--limit N] [--root PATH]` and `fha search <words…>` into
`fha.py`, both registered by `find.py`. Also wire `fha id check <ID>` / `fha id find <ID>` as
aliases through the `fha.py` dispatcher (TOOLING §4a). `--related` is a stub in this phase;
M4.3 builds it.

Require index when present; degrade gracefully when absent (stale index warns but stays
structured; absent/unreadable index falls back to grep-style tree scan with a warning header).

**By ID type:**

`<p-id>`: person file path, couple folder (dirname), companion files (research, timeline,
sources-index, draft-queue from `person_files`), claims by type+status (summary counts),
citation sites (from `citations` table). Note photo count if photoindex present; else print
`photos: not indexed (run fha photoindex)`.

`<s-id>`: record path; `files:` inventory entries with resolved paths and on-disk status
(✓ / ✗ / missing-fixture); citation sites (`citations` table path + line); claim count by
status (accepted: N, suggested and reviewed: N, suggested: N).

`<c-id>`: source record path; approximate line in file (from `citations` if available; else
grep source file); claim status, type, value; `corroborates`/`contradicts` links.

`<l-id>`: entry from `places` table (name, hierarchy, coords); every claim where
`place_id = ?`; every record body mentioning `[[l-id]]` (from `citations`).

`<h-id>`: the question entry that carries this working answer (SPEC §17b), read through the
`hypotheses` table: its question, `guess:`, `basis:`, `verify:` and the question's status; the
file it lives in (`notes/questions.md` or a research file); any claim whose `hypothesis:`
back-pointer names it (SPEC A.4); every record body mentioning `[[h-id]]`. An `h-id` that now
appears only in a dated `ruled out h-…` context line is reported as ruled out, with that line.

**`--text "…"`**: query `notes_fts` and `transcripts_fts` (if index present) then do a
`re.search` pass over sources, people, notes, and configured documents root. For each hit: path
+ context snippet. `transcripts_fts` holds the text of every `role: transcript` /
`transcription` / `extracted-text` companion, loaded by `fha index`. Photo captions are searched
only when `.cache/photos.sqlite` is verifiably fresh (present, schema includes `photo_fts`, newer
than the photos root); absent/stale/unreadable photoindex prints an explicit status-specific skip
note. Two honesty rules ride on every result, hit or miss (TOOLING §4a D14, D15): a count of the
sources whose files carry no searchable text (the same `_lib.file_entry_carries_text` rule lint
W124 reads), and an `[unchecked AI transcript]` label on any hit read from a transcript no human
has checked against the image.

**`fha search <words…>`**: the plainest search verb, identical to `fha find --text`; the
positional words are joined with spaces, so `fha search rose hartley` works unquoted.

**`--json`**: a ranked list `[{id, type, label, detail}]` on stdout - the reference-resolver
backend the workbench's search box calls (BUILD_INTERFACE MI9.1). `--kind` narrows it (person,
source, place, hypothesis, …; place matches `name:`/`alt_names:`, never `hierarchy:`), `--limit`
caps it (default 20). An unchecked-transcript hit adds `"unchecked": true`; the coverage note
goes to stderr so stdout stays the bare array.

**`--related <ID>`**: in this phase, print a clear deferral message and exit 0; M4.3 replaces
the stub once `fha xref` and `fha cooccur` exist. **`--date <EDTF>`** is reserved alongside it.

Bare non-ID string → treat as text search (same as `--text`).

**Done when:**
```sh
fha find p-de957bcda1 --root example-archive
# prints: file path, couple folder, companions, claim summary, citation sites
fha find s-4f5f215e60 --root example-archive
# prints: record, files with on-disk status, claim counts, citation sites
fha find --text "bookkeeper" --root example-archive  # at least one hit, plus the coverage count
fha search bookkeeper --root example-archive         # same hits as the line above
fha find --json Hartley --kind person --root example-archive   # JSON array on stdout
fha id check p-de957bcda1 --root example-archive    # same output as fha find p-de957bcda1
python -m unittest tests.test_find
```

---

## Layer 3 - Photo catalog (Milestone 3)

Depends on: index.
Unlocks: `fha find --text` photo captions (D7), photo gathering in `fha packet`, triage
section of `fha report`, photo count in `fha find <p-id>`.

---

### M3.1 - `fha photoindex` - scan, schema, variation grouping

**One PR.** New file `tools/photoindex.py`. Wire `fha photoindex [--full]` into `fha.py`
(TOOLING §9). Stub `find`, `triage`, `reconcile`, `tag-person`, `report` as "not built yet"
so the CLI is coherent (views.py precedent) - M3.2-M3.4 build them.

**Schema** (create `.cache/photos.sqlite`):

```sql
CREATE TABLE photos(path TEXT PRIMARY KEY, mtime REAL, size INTEGER,
  title TEXT, caption TEXT, user_comment TEXT,
  exif_date TEXT, date_pattern TEXT, edtf TEXT,
  sublocation TEXT, city TEXT, state TEXT, country TEXT,
  gps_lat REAL, gps_lon REAL,
  source_id TEXT,
  group_id TEXT, is_primary INTEGER DEFAULT 0,
  variant_copy TEXT, variant_role TEXT);
CREATE TABLE photo_groups(group_id TEXT PRIMARY KEY, primary_path TEXT,
  edtf_resolved TEXT, date_conflict INTEGER DEFAULT 0, file_count INTEGER);
CREATE TABLE photo_keywords(path TEXT, keyword TEXT);
CREATE TABLE photo_face_regions(path TEXT, name TEXT, region_type TEXT, area_json TEXT);
CREATE TABLE photo_people(path TEXT, person_ref TEXT, via TEXT);
CREATE VIRTUAL TABLE photo_fts USING fts5(path, title, caption, user_comment, keywords);
```

**Scan.** Run `exiftool -j -r <fields> <photos-root>` - one process, JSON, batch 500 files.
Incremental by `(path, mtime, size)`; `--full` bypasses. For each file:
- `source_id`: keyword matching `SOURCE:\s*([Ss]-[0-9a-hjkmnp-tv-z]{10})`.
- `date_pattern`: the body of a keyword matching `DATE:\s*(.+)` (strip prefix). Per SPEC §20
  rule 1 that body is a precision pattern in letters - `Y!M!D!`, `Y!M~`, `Y~` - and nothing
  else; a digit-bearing body (`DATE: 1880`, `DATE: 1942!-11!-25!`, a raw EDTF string) is
  outside the grammar and yields no date (counted as `nonspec_date_keywords`).
- `edtf`: that pattern resolved against the photo's EXIF `DateTimeOriginal` (SPEC §20 rule 2 -
  the keyword says which components to believe, the EXIF value supplies the digits), stopping
  at the first component that is not `!` or `~`. NULL when the photo carries no DATE: keyword:
  the keyword's presence is what marks a date reviewed, so EXIF alone never resolves.
  One implementation in `_lib` (`resolve_photo_edtf`, `edtf_confidence`) - `fha process`
  folder triage resolves the same way, so both triage rankings agree.
- Face regions: parse `XMP-mwg-rs:RegionInfo` → `photo_face_regions`
  `{path, name, region_type, area_json}` rows.
- Rebuild `photo_people` every scan from cached `photo_keywords` + `photo_face_regions`.
  Confidence order: `pid-keyword` (bare `p-id` keyword, authoritative) → `face-tag`
  (region name matched exactly against `person_face_tags.tag`; ambiguous = skip) →
  `name-match` (persons.name/name_variants; weakest).

**Variation grouping** (TOOLING §9 + `parse_media_filename` from TOOLING §6):
- Pass 1: photos sharing a `source_id` → group `SOURCE:{s-id}`.
- Pass 2: same directory + same `base_id` after stripping suffix grammar in fixed order:
  `-crop` first; then `-negative`/`-back`/`-front`/`-pageN`; then trailing variant letter
  (`-b` or bare digit-letter `034b`) → group `STEM:{dir}:{base_id}`.
  Things kept with the photo (SPEC §13) group into the same stem: a last word of `-cover`,
  `-folder`, `-mount`, `-envelope`, `-sleeve`, `-note` or `-note-N` (any capitals), optionally
  followed by `-back`/`-front` and `-crop`, parses to `part_kind = 'attached'` (with the word
  as its role), so `portrait-envelope-back.jpg` is the envelope's back, not the photo's. An
  attached part sorts after the photo's own back and is never `is_primary`; any other trailing
  word stays part of the base name.
- `is_primary`: file with no variant suffix, front of copy a if more than one (lexicographic tie-break - matches TOOLING §9, not file-path length).
- `photo_groups.edtf_resolved`: best-confidence EDTF across variants (more `!` components
  = higher confidence; prefer `~` over `?`). Any two variants whose bounds don't overlap
  → `date_conflict = 1`.

**Test fixture.** Create `tests/fixtures/photo-fixture/` with 3-4 placeholder images. Stub
`_run_exiftool()` so a test harness can inject pre-cooked JSON. Include at least one
variation pair and one image with a `SOURCE:` keyword.

**Done when:**
```sh
fha photoindex --root tests/fixtures/photo-fixture    # exits 0; photos.sqlite created
fha doctor --root example-archive                     # no regression
```

---

### M3.2 - `fha photoindex find`

**One PR.** Replace the deferral stub in `tools/photoindex.py` with real output (TOOLING §9).

**`fha photoindex find`.** Filters: `--person p-id`, `--keyword TERM` (case-insensitive),
`--edtf EDTF` (bounds overlap), `--text "…"` (FTS), `--under FOLDER` / `--not-under FOLDER`
(photos-root subtree, also on `triage` and `gallery`). Default: one path per group
(`primary_path`). `--files` shows all raw rows.

**Done when:**
```sh
fha photoindex find --person <p-id> --root ...        # returns tagged photo
fha photoindex find --text "cemetery" --root ...      # returns caption hit
fha photoindex find --edtf 192X --root ...             # bounds-overlap filter
python -m unittest tests.test_photoindex
```

---

### M3.3 - `fha photoindex triage` + `report`; unlock D7

**One PR.** Extend `tools/photoindex.py` with the two read-only/reporting sub-commands.
Also update `tools/find.py` to include `photo_fts` in `--text` searches (D7 unlock)
(TOOLING §9, §15b). Grouped together because neither writes to the archive or the photos
themselves - both are "look at what's already indexed" features.

**`fha photoindex triage [--top N]`** (TOOLING §15b). Groups with `source_id IS NULL`. Score:
+3 non-null caption (human transcription heuristic), +2 pid-keyword hit, +1 date at Y!
confidence, +1 group has a `back` variant, -2 AI-only user_comment ("AI:" or "Model:" prefix).
Emit top N (default 10) with signals and suggested `fha process <path>`.

**`fha photoindex report`.** Print groups where `date_conflict = 1` with each photo's `edtf`
and `caption` - a date disagreement between front and back is a research finding.

**D7 unlock in `find.py`.** In `_text_search()`: if `photos.sqlite` fresh, query `photo_fts`
and merge hits as `[photo]` entries. If absent → append note (TOOLING §4a D7). When M2.5 is
built after the photo schema is known, this branch can ship with it.

**Done when:**
```sh
fha photoindex triage --root tests/fixtures/photo-fixture
fha photoindex report --root tests/fixtures/photo-fixture    # prints conflict group
fha find --text "word" --root tests/fixtures/photo-fixture   # returns [photo] hit
fha find --text "word" --root example-archive                # prints "not searched" note
```

---

### M3.4 - `fha photoindex reconcile` + `tag-person`

**One PR.** Extend `tools/photoindex.py` with the two sub-commands that touch on-disk
state or embedded metadata - grouped together and kept separate from `triage`/`report`
because both are mutating (or path-healing) operations that deserve focused review.

**`fha photoindex reconcile [--with-exif] [--dry-run]`.** Missing stored paths: re-match by the
embedded `SOURCE:` keyword (`--with-exif` reads it with exiftool), and a photo that is not a
source by its picture-data fingerprint (`photos.fingerprint`, a hash of a JPEG's or PNG's
picture data and none of its metadata, stored at scan time - TOOLING §9). New files on disk →
flag for next incremental scan. Unmatchable → print for human; mark `MISSING:{path}` in table,
re-deriving the group's primary and date with the same functions the full rebuild uses. The
general `fha reconcile` (M11.1) calls this as its photo pass.

**`fha photoindex tag-person <p-id> [--from-face-tag "TAG" | --paths …] [--dry-run]`.** Preview
list → interactive confirm → write the bare `p-id` keyword → update `photo_people.via` to
`pid-keyword`. Every keyword write in the suite goes through one `_lib` writer that lands the
value in every keyword field a photo manager reads (IPTC `Keywords`, XMP `dc:subject`, and the
Lightroom hierarchy where present - TOOLING §6), and every write into an original first takes the
`originals_backup:` safety copy when one is configured (TOOLING §13f, `_lib.OriginalBackup`).

**Done when:**
```sh
fha photoindex reconcile --root tests/fixtures/photo-fixture
fha photoindex tag-person <p-id> --paths <file> --root ...   # previews; writes on y
python -m unittest tests.test_photoindex tests.test_embedded_keywords tests.test_originals_backup
```

---

### M3.5 - `fha photoindex set-summary`

**One PR.** Extend `tools/photoindex.py` with the embedded-AI-summary write verb (TOOLING §9;
SPEC §20 rule 5 sanctions the write). The `photo-context` skill (BUILD_INTERFACE.md MI4.1)
drives it.

**`fha photoindex set-summary (<path> | --group <group-id>) --text "…" [--append] [--dry-run]`.**
Preview old → new per file → interactive `[y/N]` confirm (injectable; EOF declines) → per-file
read-compose-write: `exiftool -UserComment=<composed> -overwrite_original_in_place`, then patch
`photos.user_comment` + `photo_fts.user_comment` for the paths that wrote (no rescan needed).
The compose rule fails toward preservation: new text is always AI-marked (`AI: <text>`);
an AI-marked comment is replaced (kept with `--append`); an unmarked (human) comment is
preserved verbatim and the AI block appended below - flag or no flag. Never touches
`Caption-Abstract`/`XMP-dc:Description`. Requires a **fresh** photoindex (stale hard-blocks -
a stale cache could address the wrong file for a mutating write); refuses in working-copy mode
(warning-level Result, TOOLING §13d). Exit 0, or 3 when any per-file read/write fails.

**Done when:**
```sh
fha photoindex set-summary photos/x.jpg --text "…" --dry-run --root ...   # previews, writes nothing
fha photoindex set-summary --group "SOURCE:s-…" --text "…" --root ...     # previews; writes on y
python -m unittest tests.test_photoindex
```

---

### M3.6 - `fha photoindex gallery`

**One PR.** Extend `tools/photoindex.py` with a read-only build verb plus one new
Jinja2 template `tools/templates/gallery.html`; no schema or scan change. Factor `find`'s
"filters → matched groups" logic into a shared `_matched_filter_groups` helper so gallery and
find can never disagree about what matches.

**`fha photoindex gallery --person p-… | --keyword … | --edtf 192X | --text "…" [--out FILE]`.**
Same filters as `find` (≥1 required, AND at the group level, same p-id/EDTF validation) →
one throwaway self-contained HTML page under `generated/gallery/` (default
`{slug}_{p-id}.html` for a bare person, with the filter tokens appended when `--person` carries
extra filters, `gallery_{filter tokens}.html` otherwise; `--out` overrides; the exact filename
is filter-dependent, so relay the printed path rather than predicting it). One row per logical
photo (group `primary_path`) with variants as zero-JS `<details>` chips, decade sections
newest-first (decade taken from the EDTF literal, not a widened bounds midpoint) + an "Undated"
tail + a count strip (confirmed matches only in the headline); with `--person`, weak
`face-tag`/`name-match` groups collect into a "Verify these" tail. Inherits the single-file-HTML conventions
verbatim (TOOLING §7 D11): GENERATED marker on line 1 before the doctype (overwrite-by-default,
refuse a marker-less target), inlined `design/view.css`, the private-artifact banner, `file://`
hrefs + lazy `<img>` for renderable `.jpg/.jpeg/.png/.webp/.gif` (a placeholder tile that still
opens the file otherwise); the page links the original files and builds no thumbnails.
Refuses in working-copy mode (broken links; warning-level Result, TOOLING §13d). Cache
posture mirrors `find`: absent/unreadable/old-schema → exit 3; stale warns and still builds
(exit 0); zero matches writes nothing (exit 0); a build prints the path and its `file://` URL.

**Done when:**
```sh
fha photoindex gallery --person <p-id> --root ...        # writes generated/gallery/*.html; prints path + file:// URL
fha photoindex gallery --keyword farm --text "…" --root ...   # non-person landing spot
python -m unittest tests.test_photoindex
```

---

## Layer 4 - Cross-reference & connection (Milestone 4)

Depends on: index (claim_links, relationships).
Unlocks: `fha find --related` (D4), `fha report` section 8.

---

### M4.1 - `fha xref`

**One PR.** New file `tools/xref.py`. Wire into `fha.py`. Does not write to the archive -
output candidates only; the human-confirmed write-back is `fha confirm xref` (M4.4). Requires
fresh index; exit 3 if absent (TOOLING §14a).

**`fha xref`**. Query pairs of accepted or reviewed-suggested claims: same person, same type, different
source, not already linked (`a.id < b.id` deduplication). Post-filter in Python:
- Bounds overlap (via `edtf_bounds()`) → corroboration candidate (vital types,
  `relationship`, `divorce`); other types also need a shared place or agreeing dates and
  must not name different places, and `event`/`note` get none; a negated-vs-positive
  contradiction for those types needs only "not different places" (TOOLING §14a)
- Bounds don't overlap → contradiction candidate (vital types: also flag incompatible values)

Group output by person; label each pair with both claim IDs, sources, dates, values.

**Done when:**
```sh
fha xref --root example-archive      # exits 0; at least one candidate pair
fha lint --root example-archive      # no regression
python -m unittest tests.test_xref
```

---

### M4.2 - `fha cooccur`

**One PR.** New file `tools/cooccur.py`. Wire into `fha.py`. Does not write to the
archive - output candidates only. Requires fresh index; exit 3 if absent (TOOLING §14a2).

**`fha cooccur [--threshold N]`** (default 2). Three outputs:

*Person co-occurrence:* join `source_people` (∪ `claim_persons` participants) on shared
`source_id`; group by person-pair; count distinct sources; exclude pairs with existing
`relationships` row; load and exclude `notes/cooccur_dismissed.json`
(`{"pairs": [["p-id1","p-id2"]], "generated":"…"}` - under `notes/`, not `.cache/`, because
these are a human's decisions, not derived data; read through `_lib.load_cooccur_dismissed`);
rank by count then source-type variety (different `source_type`s weigh more).

*Shared-place co-occurrence (TOOLING §14a2):* accepted or reviewed-suggested claims of different,
unlinked people sharing a place (`place_id` if both have one, else normalized `place_text`)
with overlapping EDTF date bounds; exclude pairs with an existing `relationships` row or a
dismissed tombstone, same as person co-occurrence.

*Org/entity recurrence:* group `claims.value` by `(value, type)` for `occupation`/`military`/
`membership`; emit groups with ≥2 people or ≥2 sources as shared affiliation hubs.

Tombstone file read at startup; missing = empty dismissed set (not an error). This tool only
reads; `fha confirm dismiss` (M4.4) is the writer of the tombstone, and `fha confirm cooccur`
mints the confirmed relationship claim.

**Done when:**
```sh
fha cooccur --root example-archive   # exits 0; handles missing tombstone gracefully
fha lint --root example-archive      # no regression
python -m unittest tests.test_cooccur
```

---

### M4.3 - `fha find --related` - complete implementation

**One PR.** Replace the deferral stub in `tools/find.py` with real output (TOOLING §4a D4).

**By ID type:**

`--related <p-id>` - person's world: relationship edges (rel type + source count); top
co-occurring persons with no existing edge; places ranked by claim frequency; shared claim
values (occupation/military/membership) recurring with others; distinct sources; photos from
`photo_people` (note if photoindex absent).

`--related <l-id>` - place's world: claims naming the place; people ranked by frequency;
sources; photos within ~0.002° of coords; micro-places (`within: l-id` children).

`--related <s-id>` - source's world: its claims by status; persons; places; corroborating/
contradicting sources via `claim_links`; sibling sources sharing a person or repository.

`--related <c-id>` - claim's neighborhood: its source, persons, place; linked claims;
sibling claims (same person + same type).

`--related <h-id>` - a working answer's neighborhood: the people in its question's `refs:`;
claims whose `hypothesis:` back-pointer names the h-id (the claim that settled it, if any).

`--related --date <EDTF>` (standalone): all claims whose bounds overlap the EDTF → persons,
sources, places, photos. Output summary: "Active in {EDTF}: N claims, N people, N sources."

Combination `--related <ID> --date <EDTF>`: apply date filter as additional AND on the
ID-type query (e.g. a place narrowed to a decade).

**Done when:**
```sh
fha find --related p-de957bcda1 --root example-archive   # people, places, sources sections
fha find --related --date 1880 --root example-archive    # time-slice output
fha find --related p-de957bcda1 --date 1880 --root example-archive  # combined
# all five ID types exercised against example-archive
python -m unittest tests.test_find
```

---

### M4.4 - `fha confirm` - the detection write-back layer

**One PR.** New file `tools/confirm.py`. Wire `fha confirm <verb> …` into `fha.py` (TOOLING
§14a3). The detection tools (`fha xref`, `fha cooccur`, and `fha places candidates` from M6.2)
are read-only by contract - they print candidates a human judges. `fha confirm` is the matching
write floor: once the human has picked, the write-back is mechanical, so it lives in one tool any
front door (chat now, a click later) can drive. Keeping the writes here is what lets each detector
advertise a clean read-only surface - a detector that also wrote would be two owners for one
surface.

**Six verbs** (a seventh, `confirm merge`, is M4.4a below; each surgical,
each `--dry-run`, each returns a `Result` whose `changed[]` lists files written; records
located by scanning `sources/`/`people/` directly so a stale or absent index is fine):

| Verb | Write-back |
|---|---|
| `confirm xref <c-a> <c-b> --as corroborates\|contradicts` | Reciprocal `corroborates:`/`contradicts:` link into both source records; a contradiction also spawns the `origin: tool` open question (E009-satisfying, same template as `lint --spawn-questions`). |
| `confirm cooccur <p-a> <p-b> --source S --subtype friend\|associate\|neighbor [--accept] [--reviewed DATE]` | Mint a `relationship` claim (source cited), `suggested` by default - acceptance into a derived edge still goes through `fha claim` (M1.9), since `_derive_relationships` is accepted-only. `--accept` is the escape hatch (stamps `reviewed:` and `reviewed_by:`). |
| `confirm dismiss <p-a> <p-b>` | Write the `notes/cooccur_dismissed.json` tombstone `fha cooccur` reads (durable, under `notes/`, never `.cache/`). |
| `confirm place <c-id> … (--name N [--hierarchy H] \| --into <l-id>)` | Mint/merge an `l-id` in `places/places.yaml` and relink the named claims' `place:` (pairs with `fha places candidates`, M6.2). |
| `confirm discovery "<text>" [--refs …]` | Append a dated, ref-tagged entry to `notes/discoveries.md` (the log `fha report` §0 leads with, M5). |
| `confirm draft <p-id>` | Flip a profile's `<!-- AI-DRAFT … -->` markers to `<!-- AI-ACCEPTED … (accepted DATE) -->`, preserving the original date/model (the `write-biography` accept gesture). |

Built once the detectors it serves exist: `xref`/`cooccur`/`dismiss` pair with M4.1/M4.2,
`place` with `fha places candidates` (M6.2), `discovery`/`draft` with the report (M5) and the
`write-biography` flow - so `fha confirm` is the capstone of the read → judge → write loop.

**Done when:**
```sh
fha confirm xref c-aaaaaaaaaa c-bbbbbbbbbb --as corroborates --root example-archive  # reciprocal links; reindex shows claim_links
fha confirm cooccur p-aaaaaaaaaa p-bbbbbbbbbb --source s-cccccccccc --subtype friend --root example-archive  # suggested relationship claim
fha confirm dismiss p-aaaaaaaaaa p-bbbbbbbbbb --dry-run --root example-archive  # previews tombstone write; writes nothing
fha lint --root example-archive   # a confirmed contradiction stays E009-clean
python -m unittest tests.test_confirm
```

---

### M4.4a - `fha confirm merge` - the identity-merge write

**One PR.** The seventh `confirm` verb, in `tools/confirm.py`:

```
fha confirm merge <p-merged> --into <p-survivor> --reason "<why>" [--dry-run] [--root PATH]
```

SPEC §9 defines the merge write - four tombstone fields, the `MERGED-INTO-p-survivor__` rename
grammar, the folds, resolve-through-pointer. The read side (lint W107/E020/W140, `merged_into` in
the index, packet/site chain resolution, every privacy gate reading `_lib.merge_pointer`) is
built in earlier phases; this verb is the deterministic writer the `merge-identities` skill
drives. In one plan-fully-in-memory-then-apply pass (undo journal; any failure rolls the whole
archive back) it

1. **folds** the merged record's `name` + `name_variants` (restricted `{value:, restricted: true}`
   mappings preserved), `external_ids:` (a same-key different-value conflict keeps the survivor's
   value, warns naming both, exits 1 - never silently resolved), and `relationships:` entries
   (deduped by to+type+subtype; survivor-self edges skipped with an evidence warning) into the
   survivor;
2. **tombstones** the merged record (`status: merged`, `merged_into:`, `merge_reason:`,
   `merged_date:`), strips what folded from its frontmatter (W115 protection) and reduces its
   `aliases:` to the bare p-id;
3. **renames** it to `MERGED-INTO-p-survivor__<original-filename>` - LAST, after every content
   write; the file persists forever;
4. **relinks** every claim's `persons:`/`roles:` across ALL statuses (W107 lists claims of every status;
   bare, `[[p-id]]`, `[[p-id|Name]]`, and resolving-name forms; survivor-already-listed deduped;
   per-file re-parse guard - a file that cannot be rewritten safely is a refusal naming it, with
   zero writes anywhere), other profiles' `relationships:` targets, and source `people:` lists.
   Prose `[[p-merged]]` mentions are deliberately left for lint W107's gradual-cleanup list
   (counted and reported).

Idempotent (`already` on a re-run of the same merge); refusals (exit 3) for self-merge, unknown
ids, a tombstone `--into` target (names the chain's final survivor), a different-survivor
re-merge, and a rename collision. Evidence judgment stays the skill's: the verb may WARN (an
existing relationship edge between the two), never refuses on evidence grounds. The split
stays hand-guided - research judgment per SPEC §9 - so there is no `confirm separate` verb.
Tests: `tests/test_confirm_merge.py`, `tests/test_merged_survivor.py`, `tests/test_merged_privacy.py`.

**Done when:**
```sh
fha confirm merge p-mmmmmmmmmm --into p-ssssssssss --reason "same person" --dry-run --root example-archive  # full-diff preview, zero writes
fha confirm merge p-mmmmmmmmmm --into p-ssssssssss --reason "same person" --root example-archive            # tombstone renamed-and-kept
fha index --root example-archive && fha lint --root example-archive   # no new W115/W107
```

---

## Layer 5 - Research report (Milestone 5)

Depends on: photoindex (triage section), xref (corroboration events), cooccur (section 8).

---

### M5.1 - `fha report` - sections 0-4 + snapshot

**One PR.** New file `tools/report.py`. Wire `fha report [--full] [--section NAME]`
(TOOLING §15a).

**Refresh sequence** (call tool logic directly, not subprocess):
1. `photoindex.run_incremental(root)`
2. `index.build(root)`
3. `lint.run_silent(root)` → `{errors: N, warnings: N}`

**Snapshot** `.cache/last_report.json`:
```json
{"generated":"ISO-date","source_ids":["s-…"],"person_ids":["p-…"],
 "claim_statuses":{"accepted":N,"suggested_reviewed":N,"suggested":N}}
```
`--full` ignores snapshot. After building, write new snapshot.

**Section 0 - Discoveries:** claims moving from `suggested` (reviewed or not) to `accepted` since snapshot; questions
newly `status: answered` or `resolved` (SPEC §17b reads the two as one state); `claim_links`
corroboration rows added since snapshot. The report
prints these; appending a confirmed win to the durable `notes/discoveries.md` log is the human's
gesture, actioned by `fha confirm discovery "<text>" [--refs …]` (M4.4) - the report proposes,
the human's confirm writes.

**Section 1 - Review queue** (W102): sources with `suggested` claims, grouped, oldest first.

**Section 2 - New since last session:** source_ids and person_ids in index not in snapshot.

**Section 3 - Vitals gaps** (W101): reuse lint W101 logic; curated persons first.

**Section 4 - Contradictions** (E009): `claim_links WHERE rel='contradicts'` with no open
question referencing both c-ids.

Output: markdown to stdout + `.cache/report_{date}.md`.

**Done when:**
```sh
fha report --root example-archive            # exits 0; sections 0-4 printed
fha report --root example-archive            # second run: section 0 empty
fha report --section review-queue --root ... # only section 1
fha report --full --root ...                 # ignores snapshot
python -m unittest tests.test_report
```

The report never writes a computed gap into `notes/questions.md`: gaps are shown, and a question
is written only by a human or on a human's yes (SPEC §17).

---

### M5.2 - `fha report` - sections 5 + 5b (research-loop closure)

**One PR.** Extend `tools/report.py` (TOOLING §15a). Both sections close research loops
already in flight - cross-referencing past searches and proposing question closures -
so they share the most original logic of the remaining sections and are paired together.

**Section 5 - Search-log awareness.** For leads in sections 1-4: query `search_log` (which
`fha index` builds from `notes/research-log.md`, SPEC §17c) for matching
`(person_id, collection)`. Annotate with "already searched {date}." Entries older than 18 months
→ "worth re-running." An archive with no research log simply gets no annotations (the log is
optional, SPEC §17c).

**Section 5b - Answerable questions.** Open questions in `notes/questions.md` where the
referenced gap now has an accepted claim → propose closure. Print proposals; do not execute.

**Done when:**
```sh
fha report --root example-archive   # sections 0-5b printed without error
python -m unittest tests.test_report tests.test_lib_questions
```

---

### M5.3 - `fha report` - sections 6-8 (calls into other tools)

**One PR.** Extend `tools/report.py` (TOOLING §15a). All four sections are thin
formatting wrappers around tools built in earlier layers (photoindex, places, cooccur) or
simple counts - the lightest remaining report work, bundled together for that reason.

**Section 6 - Photo triage.** Call `photoindex.triage(root, top=10)` and embed ranked list.

**Section 6b - Place candidates.** Call `places.candidates(root)` if places tool is built;
else stub with a note. Each cluster line carries a ready-to-run `fha confirm place` command
- `--into <existing l-id>` when the cluster's label already matches (exact or near, ignoring
word order/abbreviations) a place already in `places/places.yaml`, `--name=...` (mint) only
when none matches.

**Place-text oversight escalation.** Any §6b place-text
cluster reaching 20+ claims is also promoted into a banner printed above every section (same
lead position as the refresh's own `archive_notes`), instead of sitting only at §6b's position
10 of 13. One shared `places.run_candidates()` fetch feeds both §6b's own listing and this
banner (never a second, independent call - the escalation reads no more than the same
`place_text_groups` §6b already computed). Prints on `--section`-filtered runs too; when the
active filter omits §6b, the banner names the follow-up command (`fha report --section
place-candidates`) instead of pointing at content the filtered output does not carry. Also
exposed on the structured `Result` (`data['place_escalations']`, `data['sections']
['place-escalations']`) for a headless consumer, not only in the rendered markdown.

**Section 7 - Working answers & draft queues.** Open questions that carry a guess (the
`hypotheses` rows, which are built from question entries, whose question `status` is `open`):
count per person. A ruled-out guess is a dated context line, not a row, so it is not counted. From `person_files` kind='draft-queue': persons whose file is non-trivially non-empty.

**Section 8 - Possible connections.** Call cooccur logic; format top-10 person-pair candidates
with "[confirm] [dismiss]" labels. Acting on a label is `fha confirm cooccur` / `fha confirm
dismiss` (M4.4); the report itself only prints. The §6b place candidates carry the same
confirm-or-dismiss prompt, actioned by `fha confirm place`.

Section 7b (promotion candidates) is added with stub promotion in M11.4.

**Done when:**
```sh
fha report --root example-archive   # all 8 sections printed without error
python -m unittest tests.test_report
```

---

## Layer 6 - Data output (Milestone 6)

Depends on: index (+ photoindex for packet). Tools in this layer are independent of each
other and of layers 4-5; build in any order once layer 3 is done.

---

### M6.1 - `fha packet`

**One PR.** New file `tools/packet.py`. Wire `fha packet <p-id> [-o out/]
[--include-research] [--include-restricted] [--include-dna] [--no-photos]
[--dry-run] [--overwrite]` (TOOLING §8).

Verify person is curated and not `living: true|unknown`. Privacy: `living: unknown` = living.
Sources: distinct `source_id` from `claim_persons`; exclude `restricted: true` unless flag; exclude DNA unless `--include-dna`.
Resolve asset files via `resolve_path()`; note missing. Photos (requires photoindex; skip if
absent/stale or `--no-photos`): union of `pid-keyword` + `face-tag` + `name-match` from
`photo_people`. Generate fresh `timeline.md`. Create directory → zip → print zip path.
Existing packet output refuses unless `--overwrite`; `--dry-run` previews without writing.
Every copied profile, research file and source record passes through the private-fence and
draft cut (`_lib.withhold_private_and_drafts`, TOOLING §3c): `<!-- private -->` text and
unaccepted AI drafts never leave in a packet, and the README counts the notes left out.

Output directory structure:
```
packet_{surname}_{p-id}_{date}/
  README.txt       ← manifest, disclaimer, living-person caution
  profile/         ← person .md (+ research if --include-research)
  timeline.md
  sources/         ← source record .md files
  files/           ← asset copies (original filenames)
  photos/          ← photo copies
```

**Done when:**
```sh
fha packet p-de957bcda1 --root example-archive --no-photos   # zip produced
# zip contains README.txt, profile/, timeline.md, sources/, files/
fha packet p-de957bcda1 --root example-archive --include-research  # adds research file
python -m unittest tests.test_packet tests.test_private_fence_surfaces tests.test_privacy_restricted
```

---

### M6.2 - `fha places lint` + `fha places candidates`

**One PR.** New file `tools/places.py`. Wire both into `fha.py` (TOOLING §10).

**`fha places lint`:** orphan l-ids in `claims.place_id`; duplicate place names (case-folded);
dangling `within:` links; cyclic `within:` chains (visited-set walk); `within:` on a
non-micro-place.

**`fha places candidates [--threshold N] [--include-suggested]`:** normalize distinct
`claim.place_text` (case-fold; expand St/Street, Co/County); cluster near-variants via
token-set match; emit groups ≥3 occurrences (`--threshold`) with claim count and EDTF spread.
GPS clusters: ≥3 photos within ~150m with no known l-id coords nearby. Counts accepted and
reviewed-suggested claims; `--include-suggested` adds `suggested` ones, so a fresh import (everything
`suggested`) still shows its backlog.

**Done when:**
```sh
fha index --root example-archive                     # build the index first
fha places lint --root example-archive               # exits 0 (clean fixture)
fha places candidates --root example-archive         # exits 0
fha index --root tests/fixtures/broken-places
fha places lint --root tests/fixtures/broken-places  # fires on orphan l-id + dangling within:
python -m unittest tests.test_places tests.test_lib_place_registry
```

---

### M6.3 - `fha places geocode` + registry edits (`set`, `note`, `edit-note`)

**One PR.** Extend `tools/places.py`. Wire `fha places geocode [--place l-id] [--all]
[--offline] [--dry-run]`, `fha places set`, `fha places note` and `fha places edit-note`
(TOOLING §10, SPEC §15).

**`geocode`.** Download GeoNames `cities15000.txt` into `.cache/geonames/` on first run (skip if
`--offline` - the gazetteer download is the suite's one network access). Match `name` +
`hierarchy` tokens against GeoNames fields. On unique high-confidence hit: propose `coords` and
alt-names → prompt `[y/N]` before any write. Every write requires interactive confirmation.

**`set <l-id> [--coords "LAT, LON"] [--aka NAME]… [--history "PERIOD | HIERARCHY"]…`.** Replaces
exactly the fields given, each as a full per-field replace; omitted fields untouched. Coordinates
are forgiving on shape and strict on range (catching the swapped pair); each `--aka` is taken
verbatim (so "Washington, D.C." stays one alias); a `--history` period takes the claim-date rule
(valid EDTF kept, loose wording normalized, unreadable refused).

**`note <l-id> --text TEXT`** appends a dated paragraph to the place's `notes:` (written as a
`notes: |` block scalar). **`edit-note <l-id> --old-text TEXT --text TEXT`** rewrites one
existing paragraph named by its whole current text, through the shared
`_lib.replace_paragraph_in_section` / `match_log_entry` matcher the person and source
`edit-note` verbs use (M12.4, M12.5).

All four are surgical text edits of one entry in `places/places.yaml` - comments and every other
entry survive - and all preview with `--dry-run`.

**Done when:**
```sh
fha places geocode --all --root example-archive --offline  # exits 0 without network
# confirmation prompt fires; no write on N
fha places set l-xxxxxxxxxx --coords "41.3, -95.9" --dry-run --root example-archive   # diff, no write
fha places note l-xxxxxxxxxx --text "Renamed in 1890." --dry-run --root example-archive
python -m unittest tests.test_places tests.test_lib_place_registry
```

---

### M6.4 - `fha gedcom`

**One PR.** New file `tools/gedcom.py`. Wire `fha gedcom [<p-id>] [--mode descendants|
ancestors|connected] [--generations N] [--all] [--include-living] [--out FILE]`
(TOOLING §13a).

From `relationships` edges and accepted vital claims: INDI records (name, sex, birth, death);
FAM records (spouse pairs + children). `living`/`unknown` → `/Living/` by default;
`--include-living` overrides. Each vital claim's `source_id` → SOUR note. Emit header comment:
"generated by fha gedcom - do not re-import as truth."

**Done when:**
```sh
fha gedcom p-de957bcda1 --mode descendants --root example-archive  # valid .ged file
# living persons show /Living/; --include-living shows real names
python -m unittest tests.test_gedcom tests.test_vital_pick tests.test_regnal_names
```

---

### M6.5 - `fha wikitree`

**One PR.** New file `tools/wikitree.py`. Wire `fha wikitree <p-id> [--out FILE]`
(TOOLING §13).

`[[s-id]]` in profile body → `<ref name="s-id"/>` at use; definitions collected into hidden
`<div name="references">` block (deduplicated). Factual sentences with cited claim `place`/
`date` → `<span class="spacetime" data-loc="…" data-date="ISO">`. `[[p-id]]` → WikiTree link
from `person_external`; plain name if absent. Ancestry record URLs in `external_links` →
`{{Ancestry Record|db|id}}`, image-viewer URLs → `{{Ancestry Image|db|id}}`. Template hooks:
`tools/wikitree_templates.yaml`. The profile body passes through the private-fence and draft cut
first (as in M6.1). Output to stdout; `--out FILE` writes.

**Done when:**
```sh
fha wikitree p-de957bcda1 --root example-archive   # valid WikiTree markup to stdout
# each s-id appears once in the references div
python -m unittest tests.test_wikitree tests.test_unreadable_person_exports
```

---

### M6.6 - `fha gedcom import`

**One PR.** New file `tools/gedcom_import.py` + a dispatcher intercept in `fha.py`
(`_intercept_gedcom_import`, the `fha id check` mechanism - the exporter's positional
`p-id` parser in `gedcom.py` stays untouched). Wire
`fha gedcom import <file.ged> [--apply] [--plan-out FILE] [--root PATH]` (TOOLING §13a2).

The Ancestry on-ramp: parse the GEDCOM in-module (5.5/5.5.1 line grammar, CONC/CONT
folding, UTF-8 only - ANSEL/UTF-16 refused with a re-export fix; `HEAD SOUR fha`
self-import guard); file the `.ged` as ONE source record (`sources/other/`,
`subtype: gedcom`, `source_class: derivative`, the copy under `documents/gedcom/`);
mint a person stub per INDI (provisional vitals, `name_variants`, the isolated
`living_flag_for_import` heuristic); every assertion → a `suggested` claim with
`anchor: "line N"` into the filed copy (DATE→EDTF table validated with `is_valid_edtf`;
PLAC → `place_text:` only; FAM → marriage/divorce/relationship claims with roles;
PEDI → subtype). Plan-then-apply (dry-run default, convert-mining's undo-registered
rollback, audit CSV `.cache/gedcom_import/{sha12}.csv` written last = re-run sentinel);
duplicates reported, never merged; exactly three `mint_ids` batches for scale.

Names: a person's `surname:` line is written only where the NAME's `/…/` says something the
automatic split of the display name would not (`Jinping /XI/` → `surname: XI`,
`Jon Thorsson //` → `surname: ""`), and given names are accent-folded for the filename slot only
(TOOLING §3c, §13a2). The suite's one GEDCOM-reading module, it needs no index - a first import
is expected to run before one exists.

**Done when:**
```sh
fha gedcom import tests/fixtures/gedcom/small.ged --root <scratch-archive>          # plan, no writes
fha gedcom import tests/fixtures/gedcom/small.ged --root <scratch-archive> --apply  # stubs + record + copy
fha gedcom import tests/fixtures/gedcom/ansel.ged --root <scratch-archive>          # refused, names the re-export fix
# fha index && fha lint afterward: no E-codes; re-running the same file: exit 2, zero writes
python -m unittest tests.test_gedcom_import
```

---

## Layer 7 - Intake pipeline (Milestone 7)

`fha process` depends on: index, lint. `fha convert-mining` depends on: index, lint, stubs.
`fha process` is split into four PRs - variation detection and bundle dissolution are each
significant scope on their own. Two later phases extend it: inbox filing into `documents/{type}/`
(M11.2) and the cross-root `fha process refile` (M11.6).

This layer is the deterministic core of intake: `fha process` (M7.1-M7.4) and the
`fha convert-mining` importer (M7.5). The `fha capture` web on-ramp - engine, recipes,
`--ingest` sweep and the inbox contract - is a separate track in
[`BUILD_INGESTION.md`](BUILD_INGESTION.md) (the MG series; design in
[`TOOLING_INGESTION.md`](TOOLING_INGESTION.md)) and hands off to `fha process`.

---

### M7.1 - `fha process` - document files (Stage A, single-file mode)

**One PR.** New file `tools/process.py`. Wire `fha process <file> [--type TYPE]
[--title "…"] [--slug SLUG] [--date DATE] [--dry-run]` (TOOLING §6, document root only).

Detect document vs photo by asking, in order, where the file already lives, then the stated
`--type`, then the extension: a non-photo `--type` files the asset as a document whatever its
suffix, so a scanned census supplied as a `.jpg` is filed as a record (TOOLING §6). Refuse if
filename already contains `_{s-id}.` pattern. Mint s-id. **Documents root: rename**
`{slug}_{s-id}.{ext}` in place (record `original_filename`), SPEC §13. Scaffold
`sources/{type}/{slug}_{s-id}.md` from the SPEC §14 template; `files:` pre-filled with the one
file marked `is_primary: true`; empty `## Claims`. `--date` takes EDTF or loose wording and
becomes `source_date:`. Print record path. Any failure → rollback.

Source-stub sidecar (`*.notes.md`, the inbox contract BUILD_INGESTION defines): read
frontmatter hints; locate companion asset (same stem); pre-fill record from hints; stub body →
record `## Notes`; delete stub after success. A stub with `asset_elsewhere: true` and no
companion file scaffolds a record with no `files:` entry.

**Done when:**
```sh
fha process <scratch-archive>/inbox/letter.pdf --root <scratch-archive>   # mints, renames, scaffolds
fha process <scratch-archive>/inbox/letter.notes.md --root <scratch-archive>   # pre-fills; deletes stub
fha process <scratch-archive>/inbox/letter.pdf --dry-run --root <scratch-archive>   # preview, zero writes
python -m unittest tests.test_process tests.test_process_document_keyword   # incl. rollback
```

---

### M7.2 - `fha process` - photo files

**One PR.** Extend `tools/process.py` with photo-root support (TOOLING §6).

**Photos are never renamed** (SPEC §12.1, §20). Detect as photo. Refuse if `SOURCE: s-id`
keyword already embedded. Mint s-id. Embed `SOURCE: {s-id}` through the shared keyword writer
(every keyword field the format takes, after the `originals_backup:` safety copy when one is
configured - TOOLING §6, §13f) - abort on failure, do not scaffold. Scaffold
`sources/photos/{slug}_{s-id}.md`; existing photo path in `files:` with `is_primary: true`.
`--people p-id,…` also writes each p-id as a bare keyword and fills the record's `people:`.
A photo filed out of the inbox lands at the top of the photo library unless `--dest SUBPATH`
names a folder of it.

**`--more FILE ROLE[:copy]`** attaches an additional file to an existing record: a documents-root
file is renamed `{stem}-{role}[-{copy}]_{s-id}.{ext}` and must already sit under the documents
root; the entry is appended through `_lib.append_file_entry_to_record`, the single pathway every
inventory append uses.

**Done when:**
```sh
fha process <photos-root>/portrait.jpg --root ...                 # SOURCE: embedded; no rename
fha process <photos-root>/portrait.jpg --more <photos-root>/portrait_back.jpg back --root ...   # adds files: entry
fha process <photos-root>/portrait.jpg --root ...                 # refused: SOURCE: already present
python -m unittest tests.test_process tests.test_embedded_keywords tests.test_originals_backup
```

---

### M7.3 - `fha process` - folder mode + variation detection

**One PR.** Extend `tools/process.py` (TOOLING §6).

**Folder mode.** Run photoindex triage scorer over the folder; print ranked candidates; prompt
to select (number, comma-list, or `all`); process selected items one by one. The signals are
read per file with exiftool (caption, description, `UserComment`, `Keywords`/`Subject`,
`DateTimeOriginal`) instead of from the catalog, but the date signal resolves through the same
`_lib` functions photoindex scores with (`resolve_photo_edtf`, `edtf_confidence`) - the two
rankings of one folder have to agree, and restating the rule locally is how they stopped
agreeing once already.

**Tier 1 variation detection.** Before processing any file, scan its directory for siblings
sharing the same `base_id` (via `parse_media_filename`). If found:
```
Found N files that appear to be variations of the same photo:
  portrait_1880.jpg        [primary]
  portrait_1880_back.jpg   [role: back]
Process as ONE source (shared s-id) or separately?  [one / separate / skip]
```
On `one`: one s-id; SOURCE: keyword on all; one record with all files in `files:` with role
annotations. On `separate`: process each independently. On `skip`: defer.
`--variants one|separate` answers the question up front, for scripts and agents. The front (or
plain scan) is the `is_primary` file, chosen by `_lib.select_variation_primary`.
Display batch type label A-D (TOOLING §6 table) - informational only.

**Done when:**
```sh
# variation pair → "one" → one record with two files:
fha process tests/fixtures/photo-fixture/ --root ...
python -m unittest tests.test_process tests.test_process_variants
```

---

### M7.4 - `fha process` - bundle folder dissolution

**One PR.** Extend `tools/process.py` (SPEC §12.1, TOOLING §6).

**Bundle folder dissolution.** Folder with `notes.md` = source stub bundle.
Mint one s-id. Photos: embed SOURCE: keyword; no rename. Documents: rename and file to tree.
Scaffold one record pre-filled from `notes.md`; `files:` lists all assets. Delete the bundle
folder after success.

**Done when:**
```sh
# bundle folder → single record; folder dissolved; notes.md content in ## Notes
fha process <scratch copy of tests/fixtures/bundle-folder>/ --root ...
python -m unittest tests.test_process tests.test_process_inbox_filing
```

---

### M7.5 - `fha convert-mining`

**One PR.** New file `tools/convert_mining.py`. Wire `fha convert-mining [--apply]`
(TOOLING §11). Default: dry-run. `--apply` required to write.

Algorithm (TOOLING §11): process legacy transcripts via `fha process` Stage A logic; parse
markdown table rows + `Update(T###)` continuations → `suggested` claims (Claim→`value`;
Earliest/Latest→EDTF; Confidence H/M/L→`confidence`; type by keyword heuristics; `status:
suggested`); best-effort anchors (3 rarest words → grep transcript); Stories → `## Stories`;
questions → `notes/questions.md`. Write `.cache/convert_mapping.csv` (legacy_id, new_id, notes).

**Done when:**
```sh
fha convert-mining --root tests/fixtures/legacy-export         # dry-run: prints plan
fha convert-mining --root <scratch copy of legacy-export> --apply   # writes; lint exits clean
python -m unittest tests.test_convert_mining
```

---

## Layer 8 - Publication (Milestone 8)

Depends on: index, photoindex (photo strips), `fha views tree --format json` (tree data
contract). Jinja2 is the one new permitted dependency; add to requirements. Five PRs -
each is independently shippable.

---

### M8.1 - `fha site` - foundations: query layer, Jinja2, source page

**One PR.** New file `tools/site.py`. Wire `fha site [--out PATH] [--standalone | --linked]
[--dry-run]` (TOOLING §12, SPEC §21). This phase ships the infrastructure every later page depends on, proved out
against the simpler of the two page types; the person page (more sections, plus the
person-specific half of redaction) is M8.2.

The site reads all structured data from `.cache/index.sqlite` directly - not from generated
`.md` view files.

`tools/templates/` directory with `base.html` layout. Token swap: each `[[ID]]` link → a relative
href (preferring its `|display` label, else the resolved name); a name/stem `[[ ]]` resolves through
the alias map (a `living` person so named is redacted name-and-all), an unresolved non-ID `[[stem]]`
renders as plain text, and an unresolved well-formed ID link → `<mark>[[X-xxxx]]</mark>`. Minimal stdlib HTML converter for prose
(headings, bold, lists, links) - no markdown library.

`--standalone` (default): web-optimized derivatives (max 1200px, EXIF stripped) into
`site/media/` (Pillow, optional: without it images are omitted, never copied as originals);
redaction baked in (restricted / DNA / `publication_ok: false` sources excluded; `<!-- private -->`
text and unaccepted AI drafts cut through `_lib.withhold_private_and_drafts`). `--linked`: fast
local preview; real archive paths; no copies; no redaction - the view `fha serve` wraps.

**Source page:** citation block; claims table with status badges; thumbnails; file links.

**M8 UX bar (applies to all M8 phases):** error messaging and redaction copy must be
human-readable throughout. Specifically: (a) a malformed archive (corrupt YAML, missing
required fields, bad EDTF) must produce a plain error message with the problem file and a
suggested fix - never a Python traceback; (b) redacted content displays as "Living Person"
or "Restricted - not included in this publication," not as an empty slot or a broken link;
(c) `--standalone` build failures (missing asset, bad template) name the file and a
corrective step, then continue rather than aborting the entire build.

**Done when:**
```sh
fha site --root example-archive --linked
# 1880 census source page: citation, claims table
# malformed source YAML → plain message naming the file; remaining pages still build
python -m unittest tests.test_site tests.test_templates
```

---

### M8.2 - `fha site` - person page

**One PR.** Extend `tools/site.py` (TOOLING §12).

**Person page (curated):** summary block (accepted vitals); biography HTML (using the
prose converter and token-swap from M8.1); timeline (same shape as
`fha views timeline`'s chronology, with statuses split by audience: standalone shows accepted
claims only, with low-confidence ones flagged; linked/workbench also shows suggested claims
with a `reviewed:` date, marked); sources index (same query as `fha views sources-index`); photo strip
(from `photo_people`); Stories; Friends & Family (relationship edges).

**Person-page redaction:** `living`/`unknown` → "Living Person", no person page generated
at all under `--standalone`.

**Done when:**
```sh
fha site --root example-archive --linked
# Thomas Hartley person page: summary, bio, timeline, sources, F&F rendered
# [[s-xxxx]] links in bio -> link to source page
fha site --root example-archive
# a living/unknown person generates no page; nothing else links to one
python -m unittest tests.test_site tests.test_private_fence_surfaces tests.test_merged_privacy
```

---

### M8.3 - `fha site` - place + discoveries pages

**One PR.** Extend `tools/site.py` (TOOLING §12).

**Place page:** name, coords (a map link plus an embedded OpenStreetMap frame, which degrades to
an empty frame offline), dated `history:`, the registry's `notes:` prose, claims naming it,
micro-places, people ranked by association frequency.

**Discoveries page:** render `notes/discoveries.md` as HTML; link p-id and s-id mentions.

**Done when:**
```sh
fha site --root example-archive --linked    # place and discoveries pages present
fha site --root example-archive             # discoveries page redacts living-person names
python -m unittest tests.test_site
```

---

### M8.4 - `fha site` - home page + standalone redaction audit

**One PR.** Extend `tools/site.py` (TOOLING §12).

**Home page:** surname A-Z index; recent-discoveries teaser (last 5 entries); the owner's own
copy from `notes/home.md`; title and hero from `fha.yaml`'s `site:` block. The pedigree
centerpiece is M8.5.

**Standalone redaction audit.** Verify `--standalone` redaction is applied consistently
across every page type built in M8.1-M8.4 (source, person, place, discoveries,
home) - place pages must not link to redacted persons; the home page's surname index must
omit redacted persons; no page links to a person page that doesn't exist under
`--standalone`. This phase exists specifically to catch a redaction rule applied to one
page type and missed on another (AGENTS_TOOLING symmetry audit, applied across pages
instead of across tools).

**Done when:**
```sh
fha site --root example-archive --linked    # home page present
fha site --root example-archive             # living persons redacted across all pages
# site opens from file:// with all links relative
python -m unittest tests.test_site tests.test_privacy_restricted
```

---

### M8.5 - `fha site` - interactive tree rendering

**One PR.** Extend `tools/site.py` (TOOLING §12).

Vendor a client-side tree library (`family-chart`, MIT, D3-based - or comparable) into
`tools/templates/vendor/`. Write an adapter mapping the neutral tree JSON contract
(from `fha views tree --format json`) to the library's input format. The adapter is the only
place that knows about the library; swapping renderers later touches only the adapter.

Two trees, two renderers:

- **Home page - the marriage-aware ancestor pedigree.** A server-rendered static SVG (no
  script), seeded on `site.home_person`, else `root_person`, depth from
  `site.home_pedigree_generations`.
- **Person page - ancestor pedigree and descendant explorer.** Each curated person page carries
  an ancestor pedigree (ancestors mode, 3 generations default) and, inside a closed-by-default
  `<details>`, a "See descendants of {name} →" link to the interactive descendant explorer
  seeded on that person. Both are rendered by the vendored library through the adapter, from
  `site/data/tree_{p-id}_{mode}.json` (the `_{mode}` suffix keeps the descendant and ancestor
  artifacts distinct when one person seeds both).

The tree JSON is written to `site/data/` as the reusable artifact *and* embedded inline in each
page (read from the DOM, not fetched, so it renders from `file://`); redaction is applied
server-side in the JSON so a published tree never names a living person. TOOLING §12 ("Pages:
Home" / "Tree rendering") and `docs/SITE_PLAN.md` describe the customization layer.

**Done when:**
```sh
fha site --root example-archive
# home page: static SVG ancestor pedigree from home_person / root_person
# each curated person page: ancestor pedigree (≥2 generations) + descendant explorer link
# library vendored; no CDN; works from file://
python -m unittest tests.test_site
```

---

## Layer 9 - Scaffolding (Milestone 9)

Build last - the manifest must list every operating-layer file, so this is only stable once
the tool suite is substantially complete.

---

### M9.1 - `fha install` + `manifest.json`

**One PR.** New file `tools/scaffold.py`. Wire `fha install <archive-path>`. Create
`manifest.json` at repo root (TOOLING §13c). This phase ships the manifest and the
write-once skeleton installer; `fha update-tools` (the diff/backup logic - a meaningfully
different risk profile, since it touches an existing populated archive rather than an
empty one) is M9.2.

**`manifest.json`:** one JSON object listing every operating-layer file with `path`, `sha256`,
`spec_version`. Each entry's `path` is the file's destination INSIDE the archive, which is not
the same as its path in this repo: the tool suite and the design assets are vendored, so
`tools/index.py` ships as `.fha/tools/index.py` and `design/custom.css` as
`.fha/design/custom.css`, and `SPEC.md` / `TOOLING.md` as `docs/SPEC.md` / `docs/TOOLING.md`.
Covers `tools/`, `SPEC.md`, `TOOLING.md`, `AGENTS.md`, `CLAUDE.md`, `docs/` (including the
owner's `docs/GETTING_STARTED.md` and `docs/CHEATSHEET.md`), the agent workflow procedures under
`.claude/skills/`, the launchers (`fha`, `fha.cmd`, `serve.cmd`), and the skeleton (`fha.yaml`
template, `START_HERE.md`, `.gitignore`, `.gitattributes`, the empty record dirs, seeded
`places.yaml`).

The browser companion ships with every archive too: it has no build step (its source tree is
the loadable MV3 extension), so its `manifest.json`, `src/`, `icons/` and README are vendored
into `.fha/browser-companion/` as ordinary manifest entries, ready for "Load unpacked". Its
development furniture (`tests/`, `test-bundle/`, `package.json`) is left out through
`scaffold.py`'s `_VENDOR_EXCLUDE_*` lists.

Deliberately EXCLUDED, because they are workshop-clone material and an installed archive is not
where tools get built (TOOLING §13c): `AGENTS_TOOLING.md`, every `BUILD*.md`, and the sibling
tooling designs (`TOOLING_INGESTION.md`, `TOOLING_INTERFACE.md`). Shipping them would tell an
agent inside an archive to follow a build sequence it cannot run. The guiding rule is *everything a genealogist needs to operate*,
not a hand-picked minimum. Also covers the human-facing docs that must ship into every archive:
`docs/GETTING_STARTED.md` and `docs/CHEATSHEET.md` (the owner's entry points), plus `docs/SETUP_FROM_ZIP.md`,
`docs/TROUBLESHOOTING.md`, `docs/FILING_CABINET.md` (create any that don't exist yet as
stubs - the manifest entry is the commitment that they will be present in every installed
archive); in practice the *whole* `docs/` folder ships, since those five are a floor and a
directory rule keeps every doc cross-link intact. Excludes spec-repo furniture: `example-archive/`,
`archive-template/` (its *contents* seed the skeleton, but the folder itself is never copied into
an archive - matching TOOLING §13c), `tests/`, `.github/`, `.claude/settings.json`,
`PRIVACY.md` (the public-repo "no real data" policy - contradictory inside a real archive),
`RELEASE_CHECKLIST.md`, and `manifest.json` itself.

**`fha install <archive-path>`** (run from repo clone): create skeleton; copy all manifest
files; write `.plaintext-version` with manifest version + per-file SHA256 checksums.

**Preflight checks (first-day UX bar):** before writing anything, `fha install` checks
Python ≥ 3.10 and `exiftool` on PATH. Failures produce plain, friendly guidance - not a
Python traceback or bare "not found":
- Python too old → "Python 3.10 or later is required. You have X.Y. Download the latest at python.org."
- `exiftool` missing → "exiftool is not installed. Photo features won't work until it is. Install it from exiftool.org (Mac: `brew install exiftool`; Windows: see exiftool.org/install.html)."

**Zip-based, git-free install path (first-class):** `fha install` must work when the
repo is provided as an extracted zip rather than a git clone. The `--repo PATH` flag
accepts any directory containing `manifest.json`; it must not assume `.git/` exists.
This is the primary install path for non-technical users (`docs/SETUP_FROM_ZIP.md`).

The install's closing "Next steps" names the `setup-interview` skill (BUILD_INTERFACE MI7.3).

**Done when:**
```sh
fha install ./test-archive --repo .   # skeleton; .plaintext-version written
# Python < 3.10 → friendly message, no traceback
# exiftool absent → friendly guidance message, install proceeds (not a hard stop)
# --repo pointing to an unzipped download (no .git/) → same result as a git clone
# START_HERE.md, fha.yaml, AGENTS.md, CLAUDE.md + launchers at the archive ROOT;
# SPEC.md, TOOLING.md, GETTING_STARTED.md, CHEATSHEET.md, SETUP_FROM_ZIP.md etc.
# under docs/, and NO README.md (the project README is repo-facing)
# .fha/browser-companion/manifest.json present; no tests/ under it
python -m unittest tests.test_scaffold tests.test_version_sync
```

---

### M9.2 - `fha update-tools`

**One PR.** Extend `tools/scaffold.py`. Wire `fha update-tools [--dry-run]` (TOOLING §13c).

**`fha update-tools [--dry-run]`** (run from within an archive; `--repo PATH` required):
compare manifest against `.plaintext-version`. For each file - new → copy in; unchanged
(checksum matches) → overwrite silently; customized (checksum differs) → move to
`.plaintext-backup/{date}/` and report; retired from manifest → move to backup and report.
Never deletes. Never silently overwrites customized files. All output is plain English -
"Updating .fha/tools/index.py (unchanged)" or "Your edited .fha/tools/fha.py has been backed up
to .plaintext-backup/2026-06-22/.fha/tools/fha.py - the new version is now in
.fha/tools/fha.py." No technical diffs or checksums shown by default; `--verbose` may add them.

There is no migration path from the pre-`.fha` flat layout, and none is planned: `fha install`
produces the vendored layout directly, so only archives created before it could need one. Not
building it is a deliberate scope decision - a layout transition is a mutating, all-or-nothing
operation on someone's records folder, and the machinery to do it safely (rollback, half-moved
detection, launcher and capture-host repair) is a standing liability for a case that does not
arise in practice.

**Done when:**
```sh
fha update-tools --dry-run --repo .                  # reports plan in plain English; no writes
# edit tools/fha.py; fha update-tools → plain "backed up + updated" message
# no traceback on any error; missing --repo → "Run this command from inside your archive,
#   with --repo pointing to your copy of the plaintext tools."
python -m unittest tests.test_scaffold
```

---

## Layer 10 - Working-copy mode (Milestone 10)

Depends on: every asset-aware tool above (it changes how each treats an absent asset). Design:
SPEC §12.4 (the law) and TOOLING §13d (the per-tool behaviour).

**The problem it solves.** A genealogist keeps the main archive on one computer, with the photo
and document libraries in (often external) asset roots, and syncs a plain-text working copy to a
second computer through git: `sources/`, `people/`, `places/` and `notes/` travel, the binary
assets do not. Run where the assets are absent, the asset-aware tools misbehave - `fha
photoindex` prunes catalog rows for files it cannot find, `fha lint` floods E011, `fha index`
records every file as missing. Most tools already survive absent assets, because `fha index`
builds the query surface from the `.md` files, which are present. So the rule is **"treat assets
as present elsewhere, never as missing or deletable"**, not "block rebuilds".

**The flag is one visible file.** A file named `WORKING_COPY` at the archive root, git-ignored
and machine-local, holding a plain note for whoever finds it. It is a file and not an `fha.yaml`
key because `fha.yaml` syncs: a key there would travel back and flip the main archive into the
mode it exists to protect against. Only its existence matters. `fha install` / `update-tools`
neither create nor remove it.

### M10.1 - `fha working-copy` + the conservative behaviour in every tool

**One PR.** New file `tools/working_copy.py`. Wire `fha working-copy on|off [--yes]|status`
(TOOLING §13d).

- `on` writes the marker and makes sure `.gitignore` lists it; no confirm, since the mode only
  withholds. `off` removes it and **prompts** (`y/N`, default No; `--yes` skips) with a warning,
  sharper when the asset roots look empty or unreachable, because switching off where the
  originals are absent would let a photo re-index prune the catalog. `status` reports the mode.
- `_lib.is_working_copy(archive_root)` - the existence check every tool reads.
- **lint** suppresses E011/E012 and prints one note: `working copy: N asset files assumed
  present in the main archive`.
- **index** records asset presence as unknown (`exists_on_disk = NULL`), never missing (`0`),
  and skips the on-disk reconciliation glob.
- **photoindex scan** refuses and never prunes; read-only photo queries return what the cache holds.
- **Asset-mutating commands** (`fha process <file>`, `photoindex tag-person`/`set-summary`/
  `gallery`, `fha packet`, `fha source extract`) refuse with a plain "the photo and
  document files aren't here - do this on your main archive", warning-level, clean exit.
  Plain-text editing, `index`, `find`, `views`, `report`, and `fha capture` → `inbox/` work.
- **doctor** headlines the mode and stops flagging missing asset roots as errors.
- `fha.py` prints a one-line mode banner before any other command's output.
- The starter archive `.gitignore` lists `WORKING_COPY`, `.cache/`, `.plaintext-*` and the local
  asset roots, so a git-syncing genealogist gets the never-sync-back guarantee for free.

**Done when:**
```sh
fha working-copy on --root tests/fixtures/working-copy      # marker + gitignore entry; plain confirm
fha working-copy status --root tests/fixtures/working-copy  # "this is a working copy …"
fha lint --root tests/fixtures/working-copy                 # no E011 flood; one "working copy" note
fha index --root tests/fixtures/working-copy                # builds; assets exists_on_disk = NULL
fha photoindex --root tests/fixtures/working-copy           # refuses, prunes nothing
fha working-copy off --root tests/fixtures/working-copy     # warns and asks before re-enabling assets
python -m unittest tests.test_working_copy tests.test_gitignore
```

---

## Layer 11 - Repair, refiling and promotion (Milestone 11)

Second-pass verbs that sit on top of Layers 1-10: re-tying moved files, filing corrections,
batch review, stub promotion and folder realignment, PDF text extraction, recording intake, and
a bulk tidy of the documents root. Each depends only on tools already built.

---

### M11.1 - `fha reconcile`

**One PR.** New file `tools/reconcile.py`. Wire `fha reconcile [--dry-run] [--with-exif]`
(TOOLING §9). The general disk ↔ record path healer.

- **Documents.** Every source record `files:` entry under the documents alias whose path no
  longer resolves is re-matched by filename - the `_{s-id}` name is the identity carrier, and a
  filed document is renamed exactly once (SPEC §13). A unique hit rewrites the entry
  line-surgically; a duplicate name is reported ambiguous (never guessed); a vanished file is
  reported missing; an on-disk s-id file no record lists is reported with the `fha process
  --more` attach command. A file moved AND renamed still heals when its s-id survives in the new
  name.
- **Photos.** Delegates to `photoindex.run_reconcile` when `.cache/photos.sqlite` exists (the
  report.py orchestrator-import precedent), so one command reconciles every file type; a corrupt
  or old-schema catalog is an error naming the rebuild, never mistaken for a clean one. Then a
  record-side pass: a photos-alias `files:` entry missing on disk is re-tied only when exactly
  one unlisted file under the photos root has its exact filename and carries the record's own
  `SOURCE:` keyword (from a current catalog, or read with `--with-exif`); every other case is
  reported with the `file:` line to edit by hand.
- Working-copy mode is a clean no-op; an unreachable documents root warns (exit 1) without
  mass-flagging anything as missing. Lint E011's message names `fha reconcile` for a
  documents-root entry, and for a photos-root entry names `fha reconcile` (catalog current),
  `fha reconcile --with-exif` (exiftool installed), or the exact `file:` line to edit.

**Done when:**
```sh
fha reconcile --dry-run --root <scratch-archive>   # previews every re-tie; zero writes
fha reconcile --root <scratch-archive>             # heals a moved document; re-run is a no-op
python -m unittest tests.test_reconcile
```

---

### M11.2 - `fha process` files out of the inbox

**One PR.** Extend `tools/process.py` (TOOLING §6; SPEC §12.1, §12.4).

- **Copy, then park.** When `FILE` (or a `--more FILE`, or a `-back` sibling pulled in with it)
  resolves under the inbox, its bytes are copied into the asset tree and the inbox original is
  moved to `inbox/processed/` under the same relative path - less a leading triage folder named
  in `fha.yaml`'s `inbox_triage_dirs:` (`_lib.inbox_park_relpath`). Nothing is deleted.
- **Documents land in `documents/{type}/`.** A document filed out of the inbox, or a single
  documents-root file sitting at the root's top level, is renamed into `documents/{type}/` - the
  same `_record_subdir` destination bundles use - instead of left flat. A file the human
  pre-filed into any subfolder still renames in place: folders are the human's projection.
- **Photos land at the top of the photo library**, or in the folder `--dest` names; an inbox
  that sits inside an asset root is treated as new material, not as part of the library.

**Done when:**
```sh
python -m unittest tests.test_process tests.test_process_inbox_filing
# incl. test_root_top_document_files_into_type_subfolder
```

---

### M11.3 - `fha claim` - batch moves, field edits, `--clear`, `fha claim new`

**One PR.** Extend `tools/claim.py` (TOOLING §3b, SPEC §8.1-§8.6). Wire the full review verb and
`fha claim new`, intercepted in `fha.py` before argparse because the review verb's positional
c-id would misread the word `new`:

```
fha claim <c-id> [<c-id> …] [--status accepted|disputed|rejected|superseded]
          [--value …] [--date EDTF] [--type TYPE] [--place l-id] [--place-text TEXT]
          [--persons p-id,…] [--confidence …] [--information …] [--evidence …]
          [--anchor TEXT] [--notes TEXT] [--clear FIELD …] [--reviewed DATE]
          [--reviewed-by NAME] [--keep-living] [--dry-run]
fha claim new --source s-id --type TYPE --value "…" [--date …] [--place …] [--place-text …]
          [--persons …] [--subtype WORD] [--status suggested|accepted] [--negated]
          [--confidence …] [--information …] [--evidence …] [--anchor …] [--notes …]
          [--reviewed-by NAME] [--keep-living] [--dry-run]
```

- **Batch.** Several c-ids take one status move each - one human decision per claim, delivered
  in one breath. A batch is status-only (a field flag with more than one id is refused) and
  all-or-nothing on validation (every id checked before any write). `run_claim_batch` loops the
  single-claim `run_claim` - one write path - and on a mid-batch failure stops, naming what was
  applied and the exact command that finishes the rest.
- **Field edits.** `--status` is optional when any field flag is given. Every free-text value
  written is echoed back on a live write. `--place-text` alone resolves against
  `places/places.yaml` and auto-attaches `place:` on an exact match only (SPEC §15).
- **`--clear FIELD`** removes an optional field (`place`, `place_text`, `date`, `information`,
  `evidence`, `anchor`, `notes`) with its whole value, and refuses a required one. After every
  edit the claims are compared parsed before and after; any change beyond the ones asked for
  refuses the write.
- **Who decided.** Every status move writes `reviewed:` and `reviewed_by:` (`human`, or the
  `--reviewed-by` name) - SPEC §8.4.
- **Accepting a death.** Moving a `death` or `burial` claim into `accepted` sets `living: false`
  on the person the claim names as deceased (`_lib.claim_death_reading`, the rule lint W136
  reads), through the same writer as `fha person set-living`, compare-and-set from
  `unknown`/missing only; `--keep-living` opts out. A claim that names nobody `deceased:`, or
  several people with no roles, marks nobody and says why.
- **`fha claim new`** mints a claim onto an existing source's `## Claims` block; defaults to
  `--status accepted` (typing the command is the review), and an AI caller passes
  `--status suggested`. `--confidence` defaults from `source_type`. `--negated` mints a SPEC §8.6
  confirmed absence with `evidence: negative`. Type `relationship` is refused on both verbs (it
  needs a `roles:` map) - the routes are `fha person relate` (M12.3) and `fha confirm cooccur`.

**Done when:**
```sh
fha claim c-a c-b c-c --status accepted --dry-run --root example-archive   # previews all three
fha claim c-a --notes "From the 1880 enumeration." --dry-run --root example-archive
fha claim new --source s-4f5f215e60 --type occupation --value "Bookkeeper" --persons p-de957bcda1 \
  --status suggested --dry-run --root example-archive
python -m unittest tests.test_claim
```

---

### M11.4 - Stub promotion: `fha person promote`, brackets check 4, report §7b

**One PR.** One engine, three front doors (TOOLING §3c, §7; SPEC §4, §12.2, §12.3, §16):

- **`_lib.promote_person_record`** - the engine, transactional with rollback: flip `tier:` to
  curated; backfill any missing SPEC §16 profile section (additive only); move the record out of
  `people/stubs/` into its destination; move an existing `_research` companion with it (never
  create one). The Ahnentafel derivation lives in `_lib.build_ahnentafel_map`, which
  `fha views` also calls.
- **`fha person promote <p-id> [--into FOLDER] [--dry-run]`** - new file `tools/person.py`,
  whose other verbs are Layer 12. A
  direct-line person's destination is their Ahnentafel couple folder `{NNN} {name}`, derived from
  `root_person` and `root_generation` over a fresh index; anyone else needs
  `--into connections/` (SPEC §12.3) and is refused plainly without it; a direct-line person sent
  to `connections/` is refused. An already-curated, already-filed person is an `already` no-op.
  After a live move the index rows are rewritten in place (`_lib.relocate_person_in_index`) so a batch of
  promotions needs no rebuild between them.
- **`fha views brackets` check 4** (W119) + `--fix-promote` (batch-applies the engine under the
  Apply? gate; refused combined with `--fix`) + `--generations N`; lint's report-only W119
  mirror (M1.10).
- **`fha report` §7b** - promotion candidates: stubs at or over `promotion: claims_threshold:`
  (default 5) and direct-line stubs.

Promotion is always explicit - never a side effect of accepting a claim.

**Done when:**
```sh
fha person promote <stub p-id> --dry-run --root example-archive   # names the couple folder; zero writes
fha views brackets --fix-promote --dry-run --root example-archive # previews every W119 promotion
fha report --section promotion-candidates --root example-archive
python -m unittest tests.test_person tests.test_views_brackets_promote tests.test_lib_promote_atomic \
  tests.test_report tests.test_lint
```

---

### M11.5 - `fha source extract`

**One PR.** New file `tools/source.py` (the other `fha source` verbs are M12.5-M12.7). Wire
`fha source extract <s-id> [--pages RANGES] [--copy LABEL] [--dry-run]` (TOOLING §3d, SPEC §14).

Dump a source PDF's embedded text layer into a derived companion filed beside the original,
`{slug}[-{copy}]-extracted-text_{s-id}.md`, `[Page N]`-labeled in the PDF's own pagination so
`anchor: "page N"` claims (SPEC §8.4) line up in any copy. The entry is appended with
`role: extracted-text, derived: true` through `_lib.append_file_entry_to_record`; the original is
never touched (SPEC §12.1). Which PDF: `--copy LABEL`, else the unlabelled `is_primary` PDF, else
the single non-derived PDF - anything ambiguous refuses naming `--copy`.

Pages whose text layer reads as run-together words get a second reading (poppler's `pdftotext`
when on PATH, else pypdf's layout mode), accepted only when it changes nothing but spacing; a page
neither reader can space is still written, listed at the top of the dump and in a warning
(exit 1). A text-less page gets an honest placeholder; an all-image PDF refuses toward
`transcribe-source` with nothing written; a re-run is an `already` no-op. pypdf is an optional
dependency: absent, this one verb refuses with its install command (`fha doctor` reports it
info-level).

**Done when:**
```sh
fha source extract s-xxxxxxxxxx --dry-run --root <archive with a text-layer PDF>   # page coverage, zero writes
fha source extract s-xxxxxxxxxx --root <archive>                                   # companion + files: entry
python -m unittest tests.test_source_extract   # skips cleanly without pypdf
```

---

### M11.6 - `fha process refile`

**One PR.** Extend `tools/process.py`, intercepted in `fha.py` before argparse (process's parser
takes a positional FILE). Wire `fha process refile <s-id> --to photos|documents [--file NAME]
[--dest SUBPATH] [--type TYPE] [--yes] [--dry-run]` (TOOLING §6, the SPEC §12.1/§13 carve-out).

Moves one of a source's files between the documents and photos roots when the original filing
decision was wrong. Record located by s-id tree scan; the single non-derived entry (or
`--file NAME`) moves; a file already under the target root refuses toward `fha reconcile`.
- **Into photos:** `--dest` required (containment-validated); the last rename restores
  `original_filename` (else strips the `_{s-id}`/role/copy suffixes); the `SOURCE:` keyword is
  embedded through process.py's own writer - absent exiftool warns and proceeds.
- **Into documents:** `[y/N]` confirm (or `--yes`; non-interactive without it refuses) because
  the photo catalog will see the photo as missing; rename into the §13 grammar. `--type TYPE`
  rewrites `source_type:` and moves the record to `sources/{type}/` in the same transaction, and
  is required while the record is still typed `photo`.

Record surgery is one atomic write: a value-exact `file:` line rewrite plus a dated `## Notes`
provenance paragraph (`_lib.append_paragraph_to_section`). The whole run is transactional with
reverse-order undo. Exits 0 ok/dry-run · 1 not-found/working-copy · 3 refusals.

**Done when:**
```sh
fha process refile s-xxxxxxxxxx --to photos --dest 1880s --dry-run --root <scratch-archive>
fha process refile s-xxxxxxxxxx --to documents --type census --yes --root <scratch-archive>
python -m unittest tests.test_process_refile
```

---

### M11.7 - `fha views brackets --realign`, the spouse half of W103, W120

**One PR.** Extend `tools/views.py` and `tools/lint.py` (TOOLING §7, SPEC §12.2).

- **W103 spouse half** (both backends, shared `_lib.spouse_extended_base`): propose the missing
  `+ second spouse` half of a couple-folder name - add-only (exactly two derived partners, no
  `+` yet, base exactly matches one partner's name; a hand-crafted name is never rewritten). A
  batch promotion that creates a couple folder with both partners aboard names it for the couple.
- **`--realign`** - the whole-tree recovery verb after anything that shifts the derived tree (a
  `root_person` change, a corrected `sex:`): checks 1-4 in one previewed pass under one Apply?
  gate, renames first, promotions second with destinations recomputed against the post-rename
  tree (`views._realign_promotion_dests`); refused with `--fix`/`--fix-promote`.
- **W120** (views + lint twins, collected inside the shared Ahnentafel BFS): a couple with exactly
  one resolved parent whose `sex:` is unrecorded took the even slot by default; the message names
  the fix (`fha person set-sex`, then `fha index`, then `fha views brackets --realign`).

**Done when:**
```sh
fha views brackets --root tests/fixtures/broken-W120            # reports W120
fha views brackets --realign --dry-run --root example-archive   # one combined preview
python -m unittest tests.test_brackets_spouse_w120 tests.test_views_brackets_promote
```

---

### M11.8 - `fha media dedupe` + `fha media probe`

**One PR.** New file `tools/media.py`, registered as the `media` group (TOOLING §6a). The two
deterministic steps the `import-recordings` skill (BUILD_INTERFACE MI6.1) runs before filing a
recording.

**`fha media dedupe INCOMING [INCOMING…] [--json PATH] [--quiet]`** - content-hash dedupe of
incoming recordings against every archived one: size first, then SHA-256 on same-size
candidates, walking the roots from `_lib.get_roots`. Coverage is the contract: an unhashable
same-size candidate, a directory symlink, a symlink loop, a configured-but-missing root, or a
batch containing its own duplicate each degrade to "could not be completed", never "no match".
**Its exit ladder is its own** and documented in the module docstring: 0 checked everything, no
match · 1 usage/config error · 2 duplicate found · 3 coverage could not be completed (fail
closed). `--json PATH` writes the findings, refused before any hashing if the path would land on
a recording, `fha.yaml`, or inside a media root.

**`fha media probe FILE [--json]`** - a recording's true duration and container
`creation_time`, through `ffprobe` (primary) or PyAV (fallback), and the derived local start
time: `com.apple.quicktime.creationdate` settles the offset when present; otherwise the filename
clock is solved for the offset (`filename_time + duration − creation_time`, rounded to the
nearest quarter hour, rejected past a couple of minutes' miss); otherwise `offset_source: none`,
said plainly - never a silent fallback to the machine's timezone or the file's mtime. Flags the
midnight-straddle case. Ordinary 0/1/2/3 ladder (offset settled / no offset / no timestamp /
tool failure). `fha doctor` reports `ffprobe` (warning) and PyAV (info).

**Done when:**
```sh
fha media dedupe <incoming.m4a> --root example-archive   # exit 0 (no match) or 2 (duplicate, path named)
fha media probe <recording.m4a> --json --root example-archive
python -m unittest tests.test_media   # mocked ffprobe JSON; no binary needed
```

---

### M11.9 - `fha reorganize`

**One PR.** New file `tools/reorganize.py` (TOOLING §9a). Wire `fha reorganize [--dry-run]
[--apply] [--yes] [--batch-size N] [--group-threshold N] [--limit N]`. The bulk catch-up for
documents that sit loose at the documents root, from before M11.2 or from a hand migration.

- **Survey** (the default, writes nothing): read every `sources/` record's documents-alias
  `files:` entries. A file is eligible only by path shape - flat at the documents root, or one
  level down in the folder its own `source_type` maps to. Any other folder is presumptively
  human-organized and never proposed. An alias claimed by two records refuses both; a file whose
  own filename carries a different s-id than the record claiming it refuses; a destination that
  collides with an unrelated file refuses that one move.
- **Rules:** destination `documents/{type}/`; a source with more than `--group-threshold`
  (default 3) eligible files gets its own `documents/{type}/{slug}_{s-id}/` subfolder.
- **Apply** (`--apply`, plus a confirm or `--yes`): each record's moves and its `files:` rewrite
  are one atomic unit with rollback. Runs in batches (`--batch-size`, default 25 files, never
  splitting one record); after each batch `fha reconcile --dry-run` is re-run and a rise in its
  warning/error count halts the run. Completed batches are never rolled back.
- Photos are out of scope: a photo is identified by its embedded keyword, not its path.

**Done when:**
```sh
fha reorganize --root <scratch-archive>                   # plan only
fha reorganize --apply --yes --root <scratch-archive>     # moves in checked batches
fha reconcile --dry-run --root <scratch-archive>          # nothing new to heal
python -m unittest tests.test_reorganize
```

---

## Layer 12 - Person and source write-backs (Milestone 12)

Depends on: `_lib` only (records are located by scanning `people/` and `sources/`, never the
index, so a stale index never blocks or misdirects a write). Every verb here is a surgical text
edit: only the named field or section changes, the rewritten text is re-parsed and vetted before
the atomic write (`_lib.write_text_exact_atomic`, CRLF preserved), a merged tombstone is never
edited (the refusal names the survivor), a value already in place is an `already` no-op, and
`--dry-run` shows the diff. These are the parity commands behind the `fha serve` workbench's
buttons (BUILD_INTERFACE MI9.1). Exit codes: 0 ok/already/dry-run · 1 not found (with the
`fha find` next step) · 2 argparse · 3 refusals.

---

### M12.1 - `fha person new`, `set-living`, `set-sex`, `set-profile-photo`

**One PR.** Extend `tools/person.py` (created for `promote` in M11.4) - TOOLING §3c; SPEC §9,
§13, §19. Wire:

```
fha person new "Name" [--sex M|F|intersex|unknown] [--gender TEXT] [--birth DATE] [--death DATE]
               [--birth-place TEXT] [--death-place TEXT] [--surname TEXT] [--force] [--dry-run]
fha person new --from-file PEOPLE.tsv [--force] [--dry-run]
fha person set-living <p-id> true|false|unknown [--dry-run]
fha person set-sex <p-id> M|F|intersex|unknown [--dry-run]
fha person set-profile-photo <p-id> PHOTO [--dry-run]
```

- **`new`** mints one p-id and writes its stub under `people/stubs/`, sharing
  `_lib.render_stub_content`/`stub_filename`/`mint_ids` with `fha stubs` so a stub reads the
  same whichever tool wrote it. Every input validates before an ID is minted. `--birth`/`--death`
  are provisional estimates (strict EDTF or loose wording, normalized). The filename follows
  SPEC §13: a generational or regnal suffix is never taken for the surname
  (`_lib.strip_generational_suffix`, `is_name_suffix`); letters are folded to base letters
  (`_lib.fold_accents`); `--surname` overrides the split and is recorded as a `surname:` line
  (`--surname ""` = no surname). `--from-file` mints a batch, one person per line with an optional
  surname column. A name that already has a record is refused unless `--force`.
- **`set-living`** - the privacy switch every export hangs on (`unknown` counts as living). The
  writer is `_lib.set_person_living`, shared with `fha claim`'s death-accept path (M11.3). The
  output states the privacy consequence of the new value in plain words.
- **`set-sex`** - the one frontmatter fact the Ahnentafel derivation reads; a live change ends
  with the reminder to run `fha index`, then `fha views brackets` (`--realign` to apply).
- **`set-profile-photo`** - sets `profile_photo:` (a filename, path or s-id, resolved leniently at
  site build time), quoted through `_lib.yaml_inline`.

**Done when:**
```sh
fha person new "Ada Hartley" --sex F --birth "about 1852" --dry-run --root example-archive   # preview with a real p-id
fha person set-living p-de957bcda1 false --dry-run --root example-archive   # one-line diff
fha person set-sex p-de957bcda1 M --root example-archive                    # "already"
python -m unittest tests.test_person tests.test_person_names tests.test_inclusive_naming \
  tests.test_regnal_names tests.test_mint_scan
```

---

### M12.2 - `fha person rename`

**One PR.** Extend `tools/person.py` (TOOLING §3c). Wire `fha person rename <p-id> "New Name"
[--keep-variant] [--surname TEXT] [--dry-run]`.

Corrects a name everywhere it is mechanically derivable, in one command: (1) `name:` replaced,
the old name appended to `name_variants:` under `--keep-variant`; (2) the profile file renamed to
the filename the new name derives (`_lib.stub_filename`); (3) the research companion renamed
alongside; (4) any GENERATED companion at the old filename deleted (identified by its header, not
its name) and left for `fha views` to regenerate; (5) when the old name is exactly one
unambiguous partner segment of the couple folder's name, the folder is renamed too - zero or two
matches are left alone. Steps 1-3 roll back on failure; step 5 may fail on its own
(`ok-folder-unrenamed`, exit 1) because a correct record in a stale-named folder is cosmetic. A
shell whose working directory sits inside the folder is named before the rename is tried.

**Done when:**
```sh
fha person rename p-de957bcda1 "Thomas E. Hartley" --keep-variant --dry-run --root example-archive   # diff of every rename
python -m unittest tests.test_person tests.test_person_names
```

---

### M12.3 - `fha person relate` + `fha person estimate`

**One PR.** Extend `tools/person.py` (TOOLING §3c; SPEC §9, §11). Wire:

```
fha person relate <p-id> (--parent|--child|--sibling|--spouse) <p-id2> [--subtype WORD]
                  [--reciprocal] [--dry-run]
fha person estimate <p-id> [--birth DATE|-] [--death DATE|-] [--birth-place PLACE|-]
                    [--death-place PLACE|-] [--dry-run]
```

- **`relate`** appends one entry to a person's `relationships:` list, always
  `status: hypothesis` - a sourced tie is a claim, never this command's job. `--subtype` names
  the bond (SPEC §8.2 kin vocabulary); `--reciprocal` writes the mirror on the second person, each
  side checked for idempotency on its own so a later run fills in only the missing half.
- **`estimate`** writes the provisional `birth:`/`death:` fields (and their places) - a normal
  starting state, superseded once an accepted vital claim exists; `-` clears a field; both dates
  validate before either is written.

**Done when:**
```sh
fha person relate p-aaaaaaaaaa --parent p-bbbbbbbbbb --reciprocal --dry-run --root example-archive
fha person estimate p-de957bcda1 --birth "about 1840" --dry-run --root example-archive
fha lint --root example-archive   # the hypothesis edge is listed in the needs-sourcing backlog
python -m unittest tests.test_person tests.test_provisional_vitals
```

---

### M12.4 - `fha person edit`, `note`, `edit-note`

**One PR.** Extend `tools/person.py` (TOOLING §3c; SPEC §16, §16c). Wire:

```
fha person edit <p-id> --section biography|stories|research (--text TEXT | --file PATH) [--append] [--dry-run]
fha person note <p-id> --section stories|research --text TEXT [--dry-run]
fha person edit-note <p-id> --section stories|research --old-text TEXT --text TEXT [--dry-run]
```

Bounded prose write-backs into a curated profile's `## Biography`, `## Stories` or
`## Research Notes`; frontmatter and every other section are left byte-for-byte. `edit` replaces
(or `--append`s); `note` only ever appends a paragraph (`_lib.append_paragraph_to_section`);
`edit-note` rewrites one existing paragraph named by its whole current text
(`_lib.replace_paragraph_in_section` / `match_log_entry`, with reason codes `entry-partial`,
`entry-spans`, `entry-not-found`, `entry-ambiguous`, `no-section`). The `<!-- private -->` fence
(SPEC §16c) is respected: `edit` warns when a replacement drops a balanced fence, `note` refuses
when the section's fence is unbalanced.

**Done when:**
```sh
fha person note p-de957bcda1 --section stories --text "Kept bees." --dry-run --root example-archive
python -m unittest tests.test_person tests.test_private_fence_surfaces tests.test_atomic_record_writes
```

---

### M12.5 - `fha source note` + `fha source edit-note`

**One PR.** Extend `tools/source.py` (TOOLING §3d; SPEC §14). Wire
`fha source note <s-id> --text TEXT [--dry-run]` and
`fha source edit-note <s-id> --old-text TEXT --text TEXT [--dry-run]`.

`note` appends a paragraph to the source's `## Notes` (creating the heading at the end of a
hand-made record that lacks one); `edit-note` rewrites one paragraph by its whole current text,
through the same matcher as M12.4. A regression guard re-checks the `## Claims` block before and
after and refuses if the edit broke a previously sound block; a Claims-less source is never
penalized for staying Claims-less.

**Done when:**
```sh
fha source note s-4f5f215e60 --text "Original held by Aunt Ruth." --dry-run --root example-archive
python -m unittest tests.test_source tests.test_source_aliases
```

---

### M12.6 - `fha source clear-keyword`

**One PR.** Extend `tools/source.py` (TOOLING §3d; SPEC §20 rule 7). Wire
`fha source clear-keyword <s-id> --keyword TEXT [--replace-with TEXT] [--file NAME] [--dry-run]`.

The only sanctioned way to remove or correct a wrongly embedded keyword on a documents-root
asset (the stray keyword lint W131 finds). Locates the source's documents-root file (`--file`
when it lists more than one), reads that file's own keyword values (IPTC `Keywords`, XMP
`dc:subject`, Lightroom `lr:hierarchicalSubject`, where a `People|Name` entry matches by its last
part), matches `--keyword` case-insensitively, and removes the exact on-file spelling from every
field; a keyword not on the file refuses naming what is there. `--replace-with` adds the
correction to every keyword field the format takes, skipping a field that already names it. The
preview, the writer and the post-write check all read one per-field plan. Takes the
`originals_backup:` safety copy first; remove and add happen in one exiftool call.
Documents-root only.

**Done when:**
```sh
fha source clear-keyword s-xxxxxxxxxx --keyword "Margaret Hartley" --dry-run --root <archive>
python -m unittest tests.test_source_clear_keyword tests.test_embedded_keywords
```

---

### M12.7 - `fha source set-primary`

**One PR.** Extend `tools/source.py` (TOOLING §3d; SPEC §14 "The main file"). Wire
`fha source set-primary <s-id> --file NAME [--dry-run]`.

Moves the `is_primary: true` mark to the `--file` entry (matched by full alias or bare filename,
exactly then ignoring capitals) and takes it off the previous main file, giving that one the role
its own filename spells (`_lib.variant_role`). No photo is renamed or touched. Refuses, writing
nothing, when `--file` matches no entry or several, names a non-photos-root entry, the list is in
a one-line form, or the result would not hold exactly one `is_primary: true`. The rewritten text
must parse to exactly the planned list before the write, and is re-read and restored if it does
not read back as planned. `fha doctor`'s main-image check (M2.4) names this command.

**Done when:**
```sh
fha source set-primary s-xxxxxxxxxx --file portrait.jpg --dry-run --root <archive>   # words + diff
python -m unittest tests.test_source_set_primary tests.test_doctor
```

---

## Layer 13 - Relationships, links and backup (Milestone 13)

Three independent tools. `fha relate` depends on the index; `fha normalize-links` and
`fha backup` need only `_lib`.

---

### M13.1 - `fha relate`

**One PR.** New file `tools/relate.py`. Wire `fha relate <p-a> <p-b> [--json]` (TOOLING §7;
SPEC §12.2, Part IV generated views).

A pure read over `.cache/index.sqlite` - an absent index is a hard error naming `fha index`,
never a silent wrong answer; no writes. One pass loads two graphs from `relationships` (joined to
`claims` for each edge's nature): a genetic parent map (`biological` [default],
`surrogate-genetic`, `donor-sperm`, `donor-egg`) and an undirected adjacency over every edge plus
sibling edges derived from shared parents. Two answers, reported separately:
- **Blood** - the lowest common ancestor(s) over the genetic map, named by the generation/removal
  formula ("second cousin once removed", "great-grandmother"); adoptive, step, foster and
  guardian parents never count.
- **Path** - the shortest chain over all edges by BFS, rendered hop by hop ("your brother's
  friend's sister's father").

`run_relate` returns `{a, b, blood: {relationship, common_ancestors, gens_a, gens_b}, any: {path,
rendered}}`; `--json` prints it for a UI. Half, step and in-law wording comes from a small label
table in the renderer.

**Done when:**
```sh
fha index --root example-archive
fha relate p-aaaaaaaaaa p-bbbbbbbbbb --root example-archive          # blood sentence + path sentence
fha relate p-aaaaaaaaaa p-bbbbbbbbbb --json --root example-archive   # the structured result
python -m unittest tests.test_relate
```

---

### M13.2 - `fha normalize-links`

**One PR.** New file `tools/normalize_links.py`. Wire `fha normalize-links [--dry-run | --write]
[--quiet]` (TOOLING §3, SPEC §3 "resolve always; rewrite only on purpose", SPEC §18).

The one explicit, previewed rewrite pass over citation prose - deliberately separate from the
formatter, which never rewrites prose. Default is a dry run; `--write` applies; both together is
refused (exit 2). Three rewrites toward the stable, ID-carrying form:
- a single-bracket `[s-…]` prose cite → `[[s-…]]`;
- a resolved human stem or name in prose → `[[s-…|stem]]` / `[[p-…|Name]]` (the human's text
  kept as display);
- a source's `people:` / `places:` and a claim's `persons:`, in any written form → one
  `- "[[p-…|Name]]"` line per entry (SPEC §8.7, §14): a display or `# note` a person wrote is
  kept; the record's name is added only where there is no display.

A stem is never dropped (it stays in the record's `aliases:`, which lets the old link keep
resolving), and a record's missing self-alias is added (lint W111). An ambiguous name (W113) is
reported and left as written. In the `## Claims` yaml block only `persons:` is touched; `roles:`
and `place:` stay bare IDs.

**Done when:**
```sh
fha normalize-links --root example-archive          # preview of every rewrite; zero writes
fha normalize-links --write --root <scratch-archive> # applies the same diff; a re-run finds nothing
python -m unittest tests.test_normalize_links tests.test_alias_layer
```

---

### M13.3 - `fha backup`

**One PR.** New file `tools/backup.py`. Wire `fha backup [--to PATH] [--include-assets]
[--allow-incomplete] [--dry-run]` (TOOLING §13e). There is no restore verb: a backup is the
files, zipped, and unzipping it restores the archive.

- **Destination** - always outside the archive and every asset root: a sibling
  `{root-folder-name}-backups/` by default, `fha.yaml` `backup: {path:}`, or `--to`; anything
  resolving inside is refused (exit 3). Zip name `{root-folder-name}-backup_{YYYY-MM-DD}.zip`; a
  same-day re-run appends `_2`, `_3` - never overwrites.
- **Records only by default, walk-and-subtract** - everything under the root except `.cache/`,
  `generated/`, `out/`, `.git/`, the `WORKING_COPY` marker and the resolved asset roots. An
  `inbox/` inside the root is always included. Every records-only run says, with estimated sizes,
  that photos and documents are not in it.
- **`--include-assets`** zips each external root under its alias name and an in-archive root at
  its real relative path, so an unzip restores the layout `fha.yaml` describes; a name collision
  inside the zip refuses.
- **Completeness** - every walk carries `_lib.unreadable_dir_recorder`; a folder that cannot be
  listed, a symlinked subfolder, or (with `--include-assets`) a mapped root that is not a
  directory refuses the run before anything is written. `--allow-incomplete` names the artifact
  `…-INCOMPLETE.zip` and adds a `BACKUP_INCOMPLETE.txt` member listing what is missing.
- **Integrity** - every member's CRC is verified after writing; on any failure the partial zip is
  deleted (exit 3). There is no exit 1: a suspect backup is never a warning.
- **The stamp** - `.cache/last_backup.json` (date, zip, counts, `assets_included`, `complete`),
  read by `fha doctor` (M2.4) and excluded from the zip, so a restored archive honestly reports
  "no backup recorded".
- **Working copy** - a records-only backup runs; `--include-assets` is refused (warning-level).

**Done when:**
```sh
fha backup --dry-run --root example-archive       # plan: destination, counts, exclusions; nothing created
fha backup --root <scratch-archive>               # zip beside the archive; "backup verified: N files"
fha doctor --root <scratch-archive>               # reports the backup date
python -m unittest tests.test_backup
```

---

## Testing invariants (all PRs)

Every PR must leave `fha lint --root example-archive` exiting 1 with only the documented
baseline warnings (the canonical list lives in TOOLING.md §15: W101, W102, and the W119
direct-line-stub leads). No new errors or warnings may appear.
Broken fixtures in `tests/fixtures/broken-{CODE}/` must continue to fire their targeted code.
Any new lint code requires a test that builds a broken archive for it and proves it fires (in
`tests/test_lint*.py`, or a committed `tests/fixtures/broken-{CODE}/` when the tree is too rich
to write inline).

Run the suite one module at a time (`python -m unittest tests.test_<name>`), as the "Done when"
blocks do. A flag or code that exists in the CLI but is absent from `tools/README.md` is
documentation debt that blocks handoff.

**UX bar check (all PRs - required before closing):** For every new user-visible message or
error path introduced by the PR, confirm:
1. No Python traceback can reach the user under any input - including absent files, malformed
   YAML, bad IDs, and interrupted runs.
2. Every failure names a cause and a next command or corrective step.
3. Any jargon term (EDTF, `source_type`, anchor, Crockford ID, claim status) is glossed with
   an example or a valid-list in the message itself.
4. Messy-but-recoverable input (loose dates, near-miss IDs, slightly malformed hand-edits) is
   inferred or met with one plain question, never a hard refusal.
5. New failure conditions are covered by `fha doctor` and have an entry in
   `docs/TROUBLESHOOTING.md`.

A PR that passes lint and tests but fails the UX bar is not done.
