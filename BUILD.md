# BUILD.md - fha tool suite: build sequence

**Who this is for:** developers implementing the `fha` tool suite. If you just want to use the archive, start with [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

This file is the complete build guide for the **core** `fha` CLI, written as if nothing exists yet.
Every tool appears in dependency order with the same level of detail - algorithm, constraints,
and done criteria. Implementation status is tracked separately in `tools/README.md`.
Design rationale lives in `TOOLING.md`; this file tells you the sequence and how to verify it.

Two sibling layers have their own design + build docs and are **not** covered here, each with
its own milestone numbering: the capture / inbox on-ramp
([`TOOLING_INGESTION.md`](TOOLING_INGESTION.md) → [`BUILD_INGESTION.md`](BUILD_INGESTION.md),
the **MG** series) and the workbench skills / AI interface
([`TOOLING_INTERFACE.md`](TOOLING_INTERFACE.md) → [`BUILD_INTERFACE.md`](BUILD_INTERFACE.md),
the **MI** series). This file's Layer 7 covers only the deterministic core intake:
`fha process` (M7.1-M7.4) and `fha convert-mining` (M7.5).

**Tool conventions (all PRs):**
- One Python file per tool under `tools/`. Tools never import other tools.
- Shared code only in `tools/_lib.py`.
- Every subcommand defines its own `--root PATH` flag (argparse does not propagate parent flags).
- Exit codes: 0 clean · 1 warnings · 2 errors · 3 tool failure.
- **Engine/interface split.** A command's `run_*` computes and returns a `_lib.Result` (the structured record of what happened - see M1.1); its `_cmd_*` is the only layer that renders that `Result` to stdout/stderr and returns the exit code. `run_*` does not print report text or call `sys.exit`. This is what makes the suite a headless core any front door drives (TOOLING §1) - so every phase's "Done when" implicitly means "the engine returns a `Result` and the interface renders it."
- Every mutating operation ships `--dry-run`.
- `tools/README.md` is the authoritative implementation-status record. Update it on every PR.

**UX bar (all PRs) - every PR that adds user-visible output must satisfy all four:**
- **No traceback reaches the user.** Catch exceptions at the CLI boundary; translate to a plain cause + exact next command. A Python stack trace on the human's screen is always a defect. (Keep full detail behind `--debug` if useful for developers.)
- **Every user-facing error names cause + exact fix.** "No record found for S-7q2… - run `fha find S-7q2x9c4m1` to check the ID" beats "lookup failed." A message that reports a failure but gives no next step is a dead end, and a dead end is a bug.
- **Jargon ships an example or a valid-list.** Any term from the format reference (EDTF, `source_type`, anchor, claim status, Crockford ID) gets a plain gloss and a concrete example or the set of allowed values, inline in the message - never a bare "invalid X."
- **Messy-but-recoverable input is inferred or asked, never hard-failed.** Loose natural-language dates, slightly malformed hand-edits, or non-canonical spellings that the tool can resolve should produce a single plain question or a proposed normalisation - not a refusal. History is messy; treat imperfect input as normal.

**`fha doctor` and `docs/TROUBLESHOOTING.md` (all PRs):** Any new user-visible failure condition introduced by a PR must be (a) detectable by `fha doctor` with a plain-language next step, and (b) listed in `docs/TROUBLESHOOTING.md` with cause and remedy. A failure mode the human can hit but `fha doctor` cannot surface is a support burden waiting to happen.

---

## Milestones

Each Layer below maps 1:1 to a milestone - a shipped batch of capability. Layers are split
into more PR-sized phases than earlier drafts of this doc so that no single phase is
dramatically larger than its neighbors; splitting a layer into more phases never changes
its milestone number.

**Every phase is numbered `M{milestone}.{phase}`** in its `###` heading (e.g. `M3.2`) -
that number is the stable handle for asking for a review of that specific PR ("review
M3.2", or "review M3" for the whole milestone). The number is positional within its
milestone, not tied to a tool name, so renaming a phase's heading later doesn't change its
number; only inserting/removing a phase does - if that happens, renumber everything after
the insertion point in the same edit.

| Milestone | Layer | Phases | Status |
|---|---|---|---|
| 1 | Layer 1 - Foundation | M1.1-M1.9 | ✓ shipped - includes the `Result` contract (M1.1), `fha lint` as its reference renderer (M1.4), and `fha claim` the claim-review write-back (M1.9) |
| 2 | Layer 2 - Archive views & discovery | M2.1-M2.5 | ✓ shipped |
| 3 | Layer 3 - Photo catalog | M3.1-M3.5 | ✓ shipped - M3.1 (`photoindex` scan/schema/grouping), M3.2 (`photoindex find`), M3.3 (`photoindex triage`/`report`), M3.4 (`photoindex reconcile`/`tag-person`), M3.5 (`photoindex set-summary`) |
| 4 | Layer 4 - Cross-reference & connection | M4.1-M4.4a | ✓ shipped - M4.1 (`fha xref`), M4.2 (`fha cooccur`), M4.3 (`fha find --related`), M4.4 (`fha confirm` - the read-only detectors' write-back layer), M4.4a (`fha confirm merge` - the SPEC §9 identity-merge write) |
| 5 | Layer 5 - Research report | M5.1-M5.3 | ✓ shipped - M5.1 (`fha report` §0-4 + snapshot), M5.2 (§5/§5b search-log + answerable questions), M5.3 (§6-8 photo triage/place candidates/hypotheses/cooccur) |
| 6 | Layer 6 - Data output | M6.1-M6.6 | ✓ shipped - M6.1 (`fha packet`), M6.2 (`fha places lint`/`candidates`), M6.3 (`fha places geocode`), M6.4 (`fha gedcom`), M6.5 (`fha wikitree`), M6.6 (`fha gedcom import` - the Ancestry on-ramp, added in the 2026-07 usability follow-up) |
| 7 | Layer 7 - Intake pipeline (core side) | M7.1-M7.5 | ✓ shipped - M7.1-M7.4 (`fha process`: documents, photos + `--more`, folder triage + variation detection, bundle dissolution); M7.5 (`fha convert-mining`). The `fha capture` on-ramp is a separate track in [`BUILD_INGESTION.md`](BUILD_INGESTION.md) (MG series). |
| 8 | Layer 8 - Publication | M8.1-M8.5 | ✓ shipped - M8.1 (`fha site` foundations: query layer, Jinja2, source page), M8.2 (person page), M8.3 (place + discoveries pages), M8.4 (home page + standalone redaction audit), M8.5 (interactive trees via a vendored, dependency-free renderer + adapter seam) |
| 9 | Layer 9 - Scaffolding | M9.1-M9.2 | ✓ shipped - M9.1 (`fha install` + `manifest.json`: bootstrap an archive's operating layer + skeleton, stamp `.plaintext-version`, zip/git-free), M9.2 (`fha update-tools`: refresh the operating layer, back up customized/retired files, never delete, never touch skeleton seeds) |
| 10 | Layer 10 - Working-copy mode | M10.1 | ✓ shipped - `fha working-copy on|off|status`, marker plumbing, asset-check suppression, asset-command refusals |

---

## Dependency overview

```
_lib (incl. Result) ───────────────────────────────────────── all tools
index ────────────────────── views, find, doctor, stubs, claim, xref, cooccur,
│                             report, packet, process, places, site, gedcom,
│                             wikitree, capture, convert-mining
│
photoindex ──────────────── report (triage §), packet (photos), find --text (D7)
│
xref + cooccur + places ─── find --related (D4), report (§8), confirm (write-back)
│
process ──────────────────── capture (hands off to process)
│
views tree (JSON) ────────── site (tree rendering adapter)
│
install/update-tools ─────── (needs complete tool manifest)
```

`claim` is the claim-review write-back (edits a source's `## Claims` block; needs only `_lib`);
`confirm` is the write-back floor under the read-only detectors (`xref`/`cooccur`/`places
candidates`) plus the report's discovery prompts and the `write-biography` accept gesture.

Tools with no inbound lines - places, gedcom, wikitree, packet, convert-mining - depend only
on the index and can be built in any order once the index is stable. (`gedcom_import` sits
even lower: it scaffolds from `_lib` alone and needs no index at all - a first import is
expected to run before one exists.)

---

## Layer 1 - Foundation (Milestone 1 - ✓ shipped)

Everything else depends on these. Build in the order listed.

---

### M1.1 - `_lib.py` + `fha id`

**One PR.** Create `tools/_lib.py` (shared library - no CLI) and `tools/id.py`
(minting tool). Wire `fha id mint` and `fha id check` into `fha.py`.

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
open `[..1920]` → `(0001-01-01, 1920-12-31)`. Validate with the same regex used for E014.

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
`fha id check <ID>` is an alias for `fha find <ID>` (wire through `fha.py` dispatcher;
`find.py` is the canonical implementation, built in layer 2).

**Done when:**
```sh
python tools/id.py mint P           # prints one P-id
python tools/id.py mint S -n 5     # prints 5 S-ids; none duplicate each other
python -c "from _lib import edtf_bounds; print(edtf_bounds('185X'))"
# ('1850-01-01', '1859-12-31')
```

---

### M1.2 - `fha index` - schema + full rebuild

**One PR.** New file `tools/index.py`. Wire `fha index [--root PATH]` into `fha.py`
(TOOLING §2). This phase ships the schema and the from-scratch path only; incremental
`--source` upsert and relationship derivation are M1.3.

**Full rebuild algorithm:**
1. Glob `sources/**/*.md`, `people/**/*.md`, `places/places.yaml`, `notes/**/*.md`.
2. Parse each with `read_record()`; insert in one transaction, building the `aliases` table from every record's `aliases:` plus persons'/places' `name` & variants.
3. Scan all prose bodies for `TOKEN_RE` → `citations` table, storing each citation's resolved canonical ID (a `[[stem]]`/`[[Name]]` reference resolves through the alias map). Strip the `[[ ]]` wrapper from frontmatter `people:`/`places:`/note `persons:`/`sources:` and resolve each to an ID (`source_people`/`source_places`); add on-demand C-id aliases (a source aliases a `C-…` only when a `[[C-…]]` citation exists).
4. Glob asset trees for filenames carrying S-ids → `source_files` reconciliation.
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
  exists_on_disk INTEGER, in_inventory INTEGER);

CREATE TABLE claims(id TEXT PRIMARY KEY, source_id TEXT NOT NULL, type TEXT NOT NULL,
  subtype TEXT, date_edtf TEXT, date_min TEXT, date_max TEXT,
  place_id TEXT, place_text TEXT, value TEXT NOT NULL, status TEXT NOT NULL,
  reviewed TEXT, confidence TEXT, information TEXT, evidence TEXT,
  asset TEXT, anchor TEXT, hypothesis TEXT,
  significance_override TEXT, significance_reason TEXT,
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
CREATE TABLE hypotheses(id TEXT PRIMARY KEY, person_id TEXT, hypothesis TEXT,
  basis TEXT, verify TEXT, origin TEXT, status TEXT, verified_claim TEXT, path TEXT);
CREATE TABLE citations(token TEXT, kind TEXT, path TEXT, line INTEGER);  -- token = resolved canonical ID
-- aliases: the resolution surface (canonical ID + human stems + on-demand C-ids + person/place name & variants)
CREATE TABLE aliases(alias TEXT, canonical_id TEXT, kind TEXT);  -- kind: id | stem | name | variant | claim

CREATE VIRTUAL TABLE notes_fts USING fts5(path, content);
CREATE VIRTUAL TABLE transcripts_fts USING fts5(source_id, path, content);
```

**Done when:**
```sh
fha index --root example-archive         # exits 0; .cache/index.sqlite created
sqlite3 .cache/index.sqlite "SELECT count(*) FROM claims"  # non-zero
sqlite3 .cache/index.sqlite "SELECT count(*) FROM relationships"  # zero - M1.3 populates this
```

---

### M1.3 - `fha index` - incremental upsert + relationship derivation

**One PR.** Extend `tools/index.py`. Wire `fha index --source S-id [--root PATH]`
(TOOLING §2).

**Incremental mode** (`--source S-id`): delete then re-insert one source's rows. Deletion
order matters - delete `claim_persons` and `claim_links` before `claims`; delete `citations`
and `notes_fts` rows for the source path before `sources`. Reversing order leaves orphans.

**Relationship derivation** (after claims load, in both full-rebuild and incremental mode):
for each `accepted` claim - `relationship subtype: child-of` → `(child, 'parent', father)` +
reciprocal; `marriage` or `relationship subtype: spouse-of` → reciprocal `spouse` edges with
`date_start`/`date_end`; social subtypes → `friend`/`associate`/`neighbor` edges. Edges are
pure cache, re-derived from claims on every build - never hand-edited.

**Done when:**
```sh
fha index --root example-archive         # full rebuild now also derives relationships
sqlite3 .cache/index.sqlite "SELECT count(*) FROM relationships"  # non-zero
fha index --source S-4f5f215e60 --root example-archive  # incremental upsert; exits 0
```

---

### M1.4 - `fha lint` - engine + structural/reference errors (E001-E010)

**One PR.** New file `tools/lint.py`. Wire `fha lint [--root PATH] [--json]` into `fha.py`
(TOOLING §3). This phase ships the lint engine and the first ten error codes only;
inventory/keyword/agent-drift errors (E011-E018), all warning codes (W101-W114), and the
fix/formatter flags are later phases in this same layer.

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
| E001 | Duplicate P-id across two primary profiles; duplicate S/C/L/H-id anywhere |
| E002 | Filename fails §13 grammar; ID fails `ID_RE` |
| E003 | Filename ID suffix ≠ frontmatter `id` |
| E004 | Any `[token]`, `persons:`, `place:`, `corroborates/contradicts` target not found |
| E005 | P-id appears anywhere but no person record exists for it |
| E006 | `accepted` claim missing `reviewed` field |
| E007 | Claim `type` outside §8.2 vocabulary |
| E008 | `significance_override` present without `significance_reason` |
| E009 | `contradicts:` link present but no open question in `notes/questions.md` references both C-ids |
| E010 | Missing required frontmatter fields: `id`, `type`, `title` (sources); `id`, `name`, `tier`, `living` (persons) |

**E013 note (forward reference):** the summary-block cross-check is E013, built in the next
phase alongside the other inventory-facing codes - it needs the same record-parsing
machinery this phase establishes, but the codes are numbered out of build order.

**Done when:**
```sh
fha lint --root example-archive              # exits 0 - no codes built yet fire on this fixture
fha lint --root tests/fixtures/broken-E001  # fires E001
# repeat for each of E002-E010 against its broken fixture
```

---

### M1.5 - `fha lint` - inventory, keyword, and agent-drift errors (E011-E018)

**One PR.** Extend `tools/lint.py`. Wire `[--with-exif]` into the existing `fha lint`
command (TOOLING §3).

**Error codes** (TOOLING §3 table, remainder):

| Code | Detection |
|------|-----------|
| E011 | `files:` entry missing on disk (resolved via `fha.yaml`); or on-disk file carrying S-id absent from inventory |
| E012 | `SOURCE:` keyword ↔ record inventory disagreement (photos root; requires `--with-exif`) |
| E013 | Summary block `**Born/Died/Married/Parents/Children:**` citations don't match accepted claims |
| E014 | Non-EDTF date value (validate against `edtf_bounds` regex) |
| E015 | `type: relationship` claim missing `roles:` map |
| E016 | New claim references a P-id whose record has `merged_into` set |
| E017 | DNA source not `restricted: true`, or DNA file outside `documents/dna/` |
| E018 | `AGENTS.md` or skills reference deprecated commands or contradict locked rules |

**E013 parsing detail.** Scan the summary text (H1 to first `## Section`) with `finditer` on
`**Label:**` pattern - do NOT split by line (inline multi-label form exists). Extract
`[[S-id]]` and `[[P-id]]` links per segment; compare to accepted claims of the matching type.

**E011 `missing-fixture` suppression.** Suppress entirely when the archive path is under
`example-archive/` or `tests/fixtures/` - stub asset references are intentional in those
fixtures. (An arbitrary directory merely *named* `tests` in a real archive is not fixture
space; see `is_fixture_path`.)

**Done when:**
```sh
fha lint --root example-archive              # exits 0 - W101 isn't built until M1.6
fha lint --root tests/fixtures/broken-E011  # fires E011
# repeat for each of E012-E018 against its broken fixture
fha lint --root example-archive --with-exif  # exits without crash (E012 path)
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
| W105 | Hand-edits under a GENERATED header (deferred - detection requires mtime tracking; check is a no-op) |
| W106 | Accepted claim missing Mills analysis fields |
| W107 | Direct reference to a merged person |
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
fha lint --root example-archive              # exits 1; baseline W101 + W102 (TOOLING §15)
fha lint --root tests/fixtures/broken-W103  # fires W103
# repeat for each W code that has a broken fixture
```

---

### M1.7 - `fha lint` - fix modes + formatter

**One PR.** Extend `tools/lint.py`. Wire `[--dry-run] [--mint-stubs] [--spawn-questions]
[--fix-inventory] [--fix-claims-fence] [--fix-ids] [--format-check] [--format-write]` into `fha.py` (TOOLING §3).

**Fix modes** (gated behind explicit flags; always diff-previewed with `--dry-run`):
- `--mint-stubs` (E005): create stubs in `people/stubs/`
- `--spawn-questions` (E009): append templated question to `notes/questions.md`
- `--fix-inventory` (E011): placeholder/deferred - prints a warning and suggests `fha process`; full ID-glob rebuild is not yet implemented
- `--fix-claims-fence` (W114): wrap unfenced `## Claims` content in a ` ```yaml ` fence
- `--fix-ids`: mint + rename an id-less hand-authored record to the §13 grammar, keeping the filename slug as an alias

**Formatter** (`--format-check` / `--format-write`): final-newline and CRLF line-ending hygiene (initial subset). Frontmatter key order, lowercase ID normalization, blank-line hygiene, and YAML list indentation are deferred.
`--format-write` applies what `--format-check` reports. Never rewrites prose. Link normalization is the separate `fha normalize-links` (single-bracket/stem/name-link → canonical `[[ ]]`, dry-run default, keeps stems in `aliases:`), deliberately not part of the formatter.

**Done when:**
```sh
fha lint --root example-archive --format-check  # exits without crash
fha lint --root example-archive --mint-stubs --dry-run  # previews stub creation, writes nothing
fha lint --root example-archive --spawn-questions --dry-run
```

---

### M1.8 - `fha stubs`

**One PR.** New file `tools/stubs.py`. Wire `fha stubs [--root PATH] [--from-names "…"]`
into `fha.py` (TOOLING §5).

**Scan mode.** Collect all P-ids from the index (or in-memory lint pass) that lack a person
record. For each: create `people/stubs/{surname}__{given}_{P-id}.md` with minimal frontmatter
(`id`, `name`, `tier: stub`, `living: unknown`). Name/surname resolved from the claim `value`
text where parseable; else `unknown__unknown_{P-id}.md` (flagged for hand-rename). Never
overwrites; never moves a stub out of `stubs/`.

**`--from-names "Ethel Hartley; Frances Hartley"`** - mint new IDs and create stubs
interactively. One stub per semicolon-delimited name.

**Done when:**
```sh
fha stubs --root example-archive         # creates stubs for any unresolved P-ids
fha lint --root example-archive          # E005 count drops to 0 after stubs minted
fha stubs --from-names "Test Person" --root example-archive  # creates one stub
```

---

### M1.9 - `fha claim` - claim-review write-back

**One PR.** New file `tools/claim.py`. Wire `fha claim <C-id> --status accepted|disputed|rejected|
needs-review|superseded [--reviewed DATE] [--value "…"] [--date EDTF] [--root PATH] [--dry-run]`
into `fha.py` (TOOLING §3b). The five `--status` choices are the SPEC §8.1 review outcomes a
human moves a claim into (out of `suggested`). Belongs in the foundation layer: it is the deterministic half of the
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
fha claim C-xxxxxxxxxx --status accepted --root example-archive   # status flips; reviewed: stamped today
fha claim C-xxxxxxxxxx --status rejected --dry-run --root example-archive  # previews; writes nothing
fha lint --root example-archive          # an accepted claim with reviewed: stays E006-clean
```

---

## Layer 2 - Archive views & discovery (Milestone 2 - ✓ shipped)

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

**`fha views timeline [P-id | --all-curated]`** → `…_timeline_{P-id}.md`.
Query: `claims JOIN claim_persons WHERE person_id = ? AND status IN ('accepted','needs-review')
ORDER BY date_min ASC NULLS LAST`. Group by decade (floor `date_min` year to decade). Line
format: `{date_edtf} - {type}: {value} [@ {place_text_or_name}] [{source_id}]`. After main
chronology, emit `## Unreviewed` section listing `suggested` claims in the same format.
`--all-curated`: iterate every person in `persons WHERE tier = 'curated'`.

**`fha views sources-index [P-id | --all-curated | --couple-folders]`**.
Per-person: union of source_ids from (a) `claim_persons → claims.source_id` and (b)
`source_people`. Group by `source_type`; each line: `{title} [{S-id}]` then indented record
path. Write `…_sources-index_{P-id}.md`.
Couple-folder variant: for each couple folder in `people/`, enumerate all profile P-ids in
that folder; union their source_ids; write `sources-index.md` at the folder root (no P-id -
the folder is its context).

**`fha views draft-queue [P-id | --all-curated]`** → `…_draft-queue_{P-id}.md`.
Load the person's profile body. Extract all `[[S-id]]` links via `TOKEN_RE`. Query all accepted
claims for the person → collect distinct `source_id`s. Set-diff: sources with accepted claims
NOT represented by any `[[S-id]]` link = the uncited backlog. Per uncited source: show title +
`[[S-id]]`, then indent each uncited claim as `{type}: {value} - {date_edtf}`. If diff is empty,
write: `All accepted claims are cited in the profile.`

**Done when:**
```sh
fha views timeline P-de957bcda1 --root example-archive   # file generated; GENERATED header
fha views sources-index --all-curated --couple-folders --root example-archive
fha views draft-queue P-de957bcda1 --root example-archive  # non-empty (profile is sparse)
fha lint --root example-archive   # still exits 1 (baseline W101 + W102); W105 does not fire on GENERATED files
```

---

### M2.2 - `fha views` - brackets (folder maintenance)

**One PR.** Extend `tools/views.py` with `brackets`; stub `tree`, `clean`, `refresh` as
"not yet implemented" so the CLI is coherent (TOOLING §7).

**`fha views brackets [--fix] [--dry-run]`** - folder maintenance, three concerns in one pass:

1. *Bracket list refresh (W103)*: walk `people/` for couple folders (dirs starting with digits
   directly under `people/` or `people/connections/`). Parse current `[child, …]` suffix.
   Query accepted relationship claims for each person in the folder with `roles.parent` → derive
   child names. If current ≠ derived: report diff; with `--fix`, `os.rename` the folder.

2. *Ahnentafel number verification (W110, requires `root_person` in `fha.yaml`)*: BFS from
   `root_person` over `relationships WHERE rel='parent'`. Assign positions: `sex: M` → 2n,
   `sex: F` → 2n+1; same-sex/unknown → lexicographically-first P-id takes 2n. Compare each
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
```
Create `tests/fixtures/broken-W103/` and `tests/fixtures/broken-W110/` as part of this PR.

---

### M2.3 - `fha views` - tree, clean, refresh

**One PR.** Extend `tools/views.py` with the remaining sub-commands (TOOLING §7).

**`fha views tree <P-id> --mode ancestors|descendants|fan [--generations N]
[--format json|dot] [--out FILE]`** - traverses `relationships` edges (TOOLING §7):

- `ancestors`: BFS following `rel='parent'` edges recursively.
- `descendants`: BFS following `rel='child'` edges; for each visited descendant, add one-hop
  `rel='spouse'` edges as leaf nodes (don't recurse into spouse lineage).
- `fan`: BFS all edge types, 2-hop default; `--generations N` overrides depth.
- Cycle guard: visited-set on P-ids.

Output - **neutral tree JSON** (spec-pinned data contract):
```json
{
  "seed": "P-…", "mode": "descendants",
  "nodes": [{"p_id": "P-…", "name": "…", "sex": "M",
              "vitals": {"birth": "1840~", "death": null}}],
  "edges": [{"type": "child", "from": "P-…", "to": "P-…",
              "claim_id": "C-…", "dates": {"start": null, "end": null}}]
}
```
Node `vitals`: first accepted `birth`/`death` claim's `date_edtf` or null.
Edge `dates`: populated only for `spouse` edges (marriage date / divorce or death date from
`relationships.date_start`/`date_end`); null for all other types.
Deduplicate: nodes by P-id; edges by `(from, to, type)`.
`--format dot`: GraphViz DOT with `{name}\n({birth} - {death})` labels.
`--format html`: print deferral message directing to `fha site`; do not implement here.
*(Status 2026-07: still a refusal for the tree - the HTML tree ships with the site's
full-tree feature. The three content views and `refresh` DID gain `--format md|html`
in the 2026-07 usability wave; conventions in TOOLING §7 D11.)*

**`fha views clean [--dry-run]`**: walk `people/` for `.md` files whose first non-blank line
matches `<!-- GENERATED by fha `. Delete them. `--dry-run` lists only.

**`fha views refresh`**: require fresh index. Run in sequence: `timeline --all-curated`,
`draft-queue --all-curated`, `sources-index --all-curated --couple-folders`.

**Done when:**
```sh
fha views tree P-de957bcda1 --mode descendants --format json --root example-archive
# valid JSON; vitals populated; edges carry claim_id
fha views tree P-de957bcda1 --mode ancestors --format dot --root example-archive
fha views clean --root example-archive --dry-run    # lists GENERATED files
fha views refresh --root example-archive            # regenerates all; lint still exits 1 (baseline W101 + W102)
```

---

### M2.4 - `fha doctor`

**One PR.** New file `tools/doctor.py`. Wire `fha doctor [--root PATH]` into `fha.py`
(TOOLING §3a).

Run 11 checks in order; collect results; print structured report. Exit 0 = all pass;
1 = warnings only; 2 = errors (any E-level finding or unreachable root).

| Check | Error / Warning |
|-------|----------------|
| Archive root found + `fha.yaml` parses | Fatal exit 2 if either fails |
| Mapped roots in `fha.yaml` reachable | ✓/✗ per root; unreachable = exit 2 |
| `exiftool` on PATH | ✗ = exit 1 (warning; not a hard dep for most commands) |
| PyYAML importable | ✗ = exit 2 |
| Index freshness | absent/stale = exit 1 (D5: absent is a warning, not an error) |
| Photoindex freshness | absent/stale = exit 1 (D5 same) |
| Lint summary | import-and-call `run_lint_silent(root)`; E-level findings = exit 2 |
| Inbox aging | items older than 14 days; print count + oldest (only if `inbox/` exists) |
| Counts | restricted sources; living persons; unknown-living persons (from index if fresh) |
| E018 findings | list agent-instruction drift findings if present |
| Backup recency | always print; read `.cache/last_backup.json` (the `fha backup` stamp, TOOLING §13e): last-backup date + zip when present, "none recorded - run `fha backup`" when absent; info-level, never changes the exit code; end with the archive-root + mapped-roots list a full backup must cover |

Index and photoindex freshness check: compare `os.path.getmtime(index.sqlite)` against
`max(mtime)` across all `.md` files under `sources/`, `people/`, `notes/`, and
`places/places.yaml`. If the schema is empty or unreadable → treat as absent, not stale.

**Done when:**
```sh
fha doctor --root example-archive   # exits 1 (index may be stale; that's a warning)
# all 11 checks appear in output
# removing fha.yaml → exits 2 immediately
```

---

### M2.5 - `fha find` - ID types, `--text`, `--related` stub

**One PR.** New file `tools/find.py`. Wire `fha find [ID | --text "…" | --related ID]
[--date EDTF] [--root PATH]` into `fha.py`. Also wire `fha id check <ID>` as an alias through
the `fha.py` dispatcher (TOOLING §4a).

Require index when present; degrade gracefully when absent (stale index warns but stays
structured; absent/unreadable index falls back to grep-style tree scan with a warning header).

**By ID type:**

`<P-id>`: person file path, couple folder (dirname), companion files (research, timeline,
sources-index, draft-queue from `person_files`), claims by type+status (summary counts),
citation sites (from `citations` table). Note photo count if photoindex present; else print
`photos: not indexed (run fha photoindex)`.

`<S-id>`: record path; `files:` inventory entries with resolved paths and on-disk status
(✓ / ✗ / missing-fixture); citation sites (`citations` table path + line); claim count by
status (accepted: N, needs-review: N, suggested: N).

`<C-id>`: source record path; approximate line in file (from `citations` if available; else
grep source file); claim status, type, value; `corroborates`/`contradicts` links.

`<L-id>`: entry from `places` table (name, hierarchy, coords); every claim where
`place_id = ?`; every record body mentioning `[[L-id]]` (from `citations`).

`<H-id>`: hypothesis entry from `hypotheses` table (status, basis, verified_claim); research
file path; every record body mentioning `[[H-id]]` link.

**`--text "…"`**: query `notes_fts` FTS table (if index present) then do a `re.search` pass
over sources, people, notes, and configured documents root. For each hit: path + context
snippet. `transcripts_fts` is provisioned but not yet populated - transcript search is
deferred. Photo captions are searched only when `.cache/photos.sqlite` is verifiably fresh
(present, schema includes `photo_fts`, newer than the photos root); absent/stale/unreadable
photoindex prints an explicit status-specific skip note.

**`--related <ID>`**: print a clear deferral message and exit 0. This feature requires
`fha xref` and `fha cooccur` (layer 4); implement fully in that layer's PR.
Message: `--related is not yet available. It will be implemented after fha xref and fha cooccur
are built. (Design decision D4, TOOLING §4a)`

**`--date <EDTF>`**: reserved flag; print deferral message alongside `--related`.

Bare non-ID string → treat as text search (same as `--text`).

**Done when:**
```sh
fha find P-de957bcda1 --root example-archive
# prints: file path, couple folder, companions, claim summary, citation sites
fha find S-4f5f215e60 --root example-archive
# prints: record, files with on-disk status, claim counts, citation sites
fha find --text "bookkeeper" --root example-archive  # at least one hit
fha find --related P-de957bcda1 --root example-archive  # deferral message, exits 0
fha id check P-de957bcda1 --root example-archive    # same output as fha find P-de957bcda1
```

---

## Layer 3 - Photo catalog (Milestone 3 - ✓ shipped)

Depends on: index.
Unlocks: `fha find --text` photo captions (D7), photo gathering in `fha packet`, triage
section of `fha report`, photo count in `fha find <P-id>`.

---

### M3.1 - `fha photoindex` - scan, schema, variation grouping (✓ shipped)

**One PR.** New file `tools/photoindex.py`. Wire `fha photoindex [--full]` into `fha.py`
(TOOLING §9). Stub `find`, `triage`, `reconcile`, `tag-person`, `report` as "deferred to a
follow-up photoindex PR" so the CLI is coherent (views.py precedent) - M3.2-M3.4 build them.

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
- `edtf` + `date_pattern`: keywords matching `DATE:EDTF` (strip prefix); confidence per SPEC §20.
- Face regions: parse `XMP-mwg-rs:RegionInfo` → `photo_face_regions`
  `{path, name, region_type, area_json}` rows.
- Rebuild `photo_people` every scan from cached `photo_keywords` + `photo_face_regions`.
  Confidence order: `pid-keyword` (bare `P-id` keyword, authoritative) → `face-tag`
  (region name matched exactly against `person_face_tags.tag`; ambiguous = skip) →
  `name-match` (persons.name/name_variants; weakest).

**Variation grouping** (TOOLING §9 + `parse_media_filename` from TOOLING §6):
- Pass 1: photos sharing a `source_id` → group `SOURCE:{S-id}`.
- Pass 2: same directory + same `base_id` after stripping suffix grammar in fixed order:
  `-crop` first; then `-negative`/`-back`/`-front`/`-pageN`; then trailing variant letter
  (`-b` or bare digit-letter `034b`) → group `STEM:{dir}:{base_id}`.
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

### M3.2 - `fha photoindex find` (✓ shipped)

**One PR.** Replace the deferral stub in `tools/photoindex.py` with real output (TOOLING §9).

**`fha photoindex find`.** Filters: `--person P-id`, `--keyword TERM` (case-insensitive),
`--edtf EDTF` (bounds overlap), `--text "…"` (FTS). Default: one path per group
(`primary_path`). `--files` shows all raw rows.

**Done when:**
```sh
fha photoindex find --person <P-id> --root ...        # returns tagged photo
fha photoindex find --text "cemetery" --root ...      # returns caption hit
fha photoindex find --edtf 192X --root ...             # bounds-overlap filter
```

---

### M3.3 - `fha photoindex triage` + `report`; unlock D7 (✓ shipped)

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
and merge hits as `[photo]` entries. If absent → append note. Update TOOLING §4a D7 entry.
(Already implemented as of milestone 2's `find.py` build - TOOLING §4a's D7 entry is marked
"implemented milestone 2." No further `find.py` change was needed for this phase.)

**Done when:**
```sh
fha photoindex triage --root tests/fixtures/photo-fixture
fha photoindex report --root tests/fixtures/photo-fixture    # prints conflict group
fha find --text "word" --root tests/fixtures/photo-fixture   # returns [photo] hit
fha find --text "word" --root example-archive                # prints "not searched" note
```

---

### M3.4 - `fha photoindex reconcile` + `tag-person` (✓ shipped)

**One PR.** Extend `tools/photoindex.py` with the two sub-commands that touch on-disk
state or embedded metadata - grouped together and kept separate from `triage`/`report`
because both are mutating (or path-healing) operations that deserve focused review.

**`fha photoindex reconcile`.** Missing stored paths: try re-match by `source_id` glob (needs
`--with-exif`). New files on disk → flag for next incremental scan. Unmatchable → print for
human; mark `MISSING:{path}` in table.

**`fha photoindex tag-person <P-id> [--from-face-tag "TAG" | --paths …]`.** Preview list →
interactive confirm → `exiftool -keywords+="P-id" -overwrite_original_in_place` → update
`photo_people.via` to `pid-keyword`.

**Done when:**
```sh
fha photoindex reconcile --root tests/fixtures/photo-fixture
fha photoindex tag-person <P-id> --paths <file> --root ...   # previews; writes on y
```

---

### M3.5 - `fha photoindex set-summary` (✓ shipped)

**One PR.** Extend `tools/photoindex.py` with the embedded-AI-summary write verb (TOOLING §9;
SPEC §20 rule 5 already sanctions the write). This is the core verb the `photo-context` skill
was blocked on (BUILD_INTERFACE.md Layer I4); the SKILL.md itself is separate, later skill-mode work.

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
fha photoindex set-summary --group "SOURCE:S-…" --text "…" --root ...     # previews; writes on y
```

---

## Layer 4 - Cross-reference & connection (Milestone 4 - ✓ shipped)

Depends on: index (claim_links, relationships).
Unlocks: `fha find --related` (D4), `fha report` section 8.

---

### M4.1 - `fha xref` (✓ shipped)

**One PR.** New file `tools/xref.py`. Wire into `fha.py`. Does not write to the archive -
output candidates only; the human-confirmed write-back is `fha confirm xref` (M4.4). Requires
fresh index; exit 3 if absent (TOOLING §14a).

**`fha xref`**. Query pairs of accepted/needs-review claims: same person, same type, different
source, not already linked (`a.id < b.id` deduplication). Post-filter in Python:
- Bounds overlap (via `edtf_bounds()`) → corroboration candidate
- Bounds don't overlap → contradiction candidate (vital types: also flag incompatible values)

Group output by person; label each pair with both claim IDs, sources, dates, values.

**Done when:**
```sh
fha xref --root example-archive      # exits 0; at least one candidate pair
fha lint --root example-archive      # no regression
```

---

### M4.2 - `fha cooccur` (✓ shipped)

**One PR.** New file `tools/cooccur.py`. Wire into `fha.py`. Does not write to the
archive - output candidates only. Requires fresh index; exit 3 if absent (TOOLING §14a2).

**`fha cooccur [--threshold N]`** (default 2). Three outputs:

*Person co-occurrence:* join `source_people` (∪ `claim_persons` participants) on shared
`source_id`; group by person-pair; count distinct sources; exclude pairs with existing
`relationships` row; load and exclude `.cache/cooccur_dismissed.json`
(`{"pairs": [["P-id1","P-id2"]], "generated":"…"}`); rank by count then source-type variety
(different `source_type`s weigh more).

*Shared-place co-occurrence (TOOLING §690b):* accepted/needs-review claims of different,
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
```

---

### M4.3 - `fha find --related` - complete implementation (✓ shipped)

**One PR.** Replace the deferral stub in `tools/find.py` with real output. Update TOOLING §4a
D4 note: "implemented" (TOOLING §4a).

**By ID type:**

`--related <P-id>` - person's world: relationship edges (rel type + source count); top
co-occurring persons with no existing edge; places ranked by claim frequency; shared claim
values (occupation/military/membership) recurring with others; distinct sources; photos from
`photo_people` (note if photoindex absent).

`--related <L-id>` - place's world: claims naming the place; people ranked by frequency;
sources; photos within ~0.002° of coords; micro-places (`within: L-id` children).

`--related <S-id>` - source's world: its claims by status; persons; places; corroborating/
contradicting sources via `claim_links`; sibling sources sharing a person or repository.

`--related <C-id>` - claim's neighborhood: its source, persons, place; linked claims;
sibling claims (same person + same type).

`--related <H-id>` - hypothesis's neighborhood: person it concerns; claims referencing its
H-id; verifying claim if `verified_claim` is set.

`--related --date <EDTF>` (standalone): all claims whose bounds overlap the EDTF → persons,
sources, places, photos. Output summary: "Active in {EDTF}: N claims, N people, N sources."

Combination `--related <ID> --date <EDTF>`: apply date filter as additional AND on the
ID-type query (e.g. a place narrowed to a decade).

**Done when:**
```sh
fha find --related P-de957bcda1 --root example-archive   # people, places, sources sections
fha find --related --date 1880 --root example-archive    # time-slice output
fha find --related P-de957bcda1 --date 1880 --root example-archive  # combined
# all five ID types exercised against example-archive
```

---

### M4.4 - `fha confirm` - the detection write-back layer (✓ shipped)

**One PR.** New file `tools/confirm.py`. Wire `fha confirm <verb> …` into `fha.py` (TOOLING
§14a3). The detection tools (`fha xref`, `fha cooccur`, and `fha places candidates` from M6.2)
are read-only by contract - they print candidates a human judges. `fha confirm` is the matching
write floor: once the human has picked, the write-back is mechanical, so it lives in one tool any
front door (chat now, a click later) can drive. Keeping the writes here is what lets each detector
advertise a clean read-only surface - a detector that also wrote would be two owners for one
surface.

**The six original verbs** (a seventh, `confirm merge`, ships in M4.4a below; each surgical,
each `--dry-run`, each returns a `Result` whose `changed[]` lists files written; records
located by scanning `sources/`/`people/` directly so a stale or absent index is fine):

| Verb | Write-back |
|---|---|
| `confirm xref <C-a> <C-b> --as corroborates\|contradicts` | Reciprocal `corroborates:`/`contradicts:` link into both source records; a contradiction also spawns the `origin: tool` open question (E009-satisfying, same template as `lint --spawn-questions`). |
| `confirm cooccur <P-a> <P-b> --source S --subtype friend\|associate\|neighbor [--accept]` | Mint a `relationship` claim (source cited), `suggested` by default - acceptance into a derived edge still goes through `fha claim` (M1.9), since `_derive_relationships` is accepted-only. `--accept` is the escape hatch (stamps `reviewed:`). |
| `confirm dismiss <P-a> <P-b>` | Write the `.cache/cooccur_dismissed.json` tombstone `fha cooccur` reads. |
| `confirm place <C-id> … (--name N [--hierarchy H] \| --into <L-id>)` | Mint/merge an `L-id` in `places/places.yaml` and relink the named claims' `place:` (pairs with `fha places candidates`, M6.2). |
| `confirm discovery "<text>" [--refs …]` | Append a dated, ref-tagged entry to `notes/discoveries.md` (the log `fha report` §0 leads with, M5). |
| `confirm draft <P-id>` | Flip a profile's `<!-- AI-DRAFT … -->` markers to `<!-- AI-ACCEPTED … (accepted DATE) -->`, preserving the original date/model (the `write-biography` accept gesture). |

Built once the detectors it serves exist: `xref`/`cooccur`/`dismiss` pair with M4.1/M4.2,
`place` with `fha places candidates` (M6.2), `discovery`/`draft` with the report (M5) and the
`write-biography` flow - so `fha confirm` is the capstone of the read → judge → write loop.

**Done when:**
```sh
fha confirm xref C-aaaaaaaaaa C-bbbbbbbbbb --as corroborates --root example-archive  # reciprocal links; reindex shows claim_links
fha confirm cooccur P-aaaaaaaaaa P-bbbbbbbbbb --source S-cccccccccc --subtype friend --root example-archive  # suggested relationship claim
fha confirm dismiss P-aaaaaaaaaa P-bbbbbbbbbb --dry-run --root example-archive  # previews tombstone write; writes nothing
fha lint --root example-archive   # a confirmed contradiction stays E009-clean
```

---

### M4.4a - `fha confirm merge` - the identity-merge write (✓ shipped)

**One PR** (audit Wave 3 / usability plan 16). The seventh `confirm` verb, in `tools/confirm.py`:

```
fha confirm merge <P-merged> --into <P-survivor> --reason "<why>" [--dry-run] [--root PATH]
```

SPEC §9 fully defined the merge write - four tombstone fields, the `MERGED-INTO-P-survivor__`
rename grammar, the folds, resolve-through-pointer - and the read side already existed (lint
E016/W107 and the tombstone filename grammar, `merged_into` in the index, packet/site chain
resolution), but nothing *performed* it; the merge-identities skill hand-edited per SPEC §9 as a
documented owner-approved interim. This verb is the deterministic owner: in one
plan-fully-in-memory-then-apply pass (undo journal; any failure rolls the whole archive back) it

1. **folds** the merged record's `name` + `name_variants` (restricted `{value:, restricted: true}`
   mappings preserved), `external_ids:` (a same-key different-value conflict keeps the survivor's
   value, warns naming both, exits 1 - never silently resolved), and `relationships:` entries
   (deduped by to+type+subtype; survivor-self edges skipped with an evidence warning) into the
   survivor;
2. **tombstones** the merged record (`status: merged`, `merged_into:`, `merge_reason:`,
   `merged_date:`), strips what folded from its frontmatter (W115 protection) and reduces its
   `aliases:` to the bare P-id;
3. **renames** it to `MERGED-INTO-P-survivor__<original-filename>` - LAST, after every content
   write; the file persists forever;
4. **relinks** every claim's `persons:`/`roles:` across ALL statuses (E016 has no status filter;
   bare, `[[P-id]]`, `[[P-id|Name]]`, and resolving-name forms; survivor-already-listed deduped;
   per-file re-parse guard - a file that cannot be rewritten safely is a refusal naming it, with
   zero writes anywhere), other profiles' `relationships:` targets, and source `people:` lists.
   Prose `[[P-merged]]` mentions are deliberately left for lint W107's gradual-cleanup list
   (counted and reported).

Idempotent (`already` on a re-run of the same merge); refusals (exit 3) for self-merge, unknown
ids, a tombstone `--into` target (names the chain's final survivor), a different-survivor
re-merge, and a rename collision. Evidence judgment stays the skill's: the verb may WARN (an
existing relationship edge between the two), never refuses on evidence grounds. The split
(`confirm separate`) stays hand-guided - research judgment per SPEC §9, deliberately not built.
Tests: `tests/test_confirm_merge.py`.

**Done when:**
```sh
fha confirm merge P-mmmmmmmmmm --into P-ssssssssss --reason "same person" --dry-run --root example-archive  # full-diff preview, zero writes
fha confirm merge P-mmmmmmmmmm --into P-ssssssssss --reason "same person" --root example-archive            # tombstone renamed-and-kept
fha index --root example-archive && fha lint --root example-archive   # no E016, no new W115/W107
```

---

## Layer 5 - Research report (Milestone 5 - ✓ shipped)

Depends on: photoindex (triage section), xref (corroboration events), cooccur (section 8).

---

### M5.1 - `fha report` - sections 0-4 + snapshot (✓ shipped)

**One PR.** New file `tools/report.py`. Wire `fha report [--full] [--section NAME]`
(TOOLING §15a).

**Refresh sequence** (call tool logic directly, not subprocess):
1. `photoindex.run_incremental(root)`
2. `index.build(root)`
3. `lint.run_silent(root)` → `{errors: N, warnings: N}`

**Snapshot** `.cache/last_report.json`:
```json
{"generated":"ISO-date","source_ids":["S-…"],"person_ids":["P-…"],
 "claim_statuses":{"accepted":N,"needs-review":N,"suggested":N}}
```
`--full` ignores snapshot. After building, write new snapshot.

**Section 0 - Discoveries:** claims moving `needs-review → accepted` since snapshot; questions
newly `status: answered`; `claim_links` corroboration rows added since snapshot. The report
prints these; appending a confirmed win to the durable `notes/discoveries.md` log is the human's
gesture, actioned by `fha confirm discovery "<text>" [--refs …]` (M4.4) - the report proposes,
the human's confirm writes.

**Section 1 - Review queue** (W102): sources with `suggested` claims, grouped, oldest first.

**Section 2 - New since last session:** source_ids and person_ids in index not in snapshot.

**Section 3 - Vitals gaps** (W101): reuse lint W101 logic; curated persons first.

**Section 4 - Contradictions** (E009): `claim_links WHERE rel='contradicts'` with no open
question referencing both C-ids.

Output: markdown to stdout + `.cache/report_{date}.md`.

**Done when:**
```sh
fha report --root example-archive            # exits 0; sections 0-4 printed
fha report --root example-archive            # second run: section 0 empty
fha report --section review-queue --root ... # only section 1
fha report --full --root ...                 # ignores snapshot
```

---

### M5.2 - `fha report` - sections 5 + 5b (research-loop closure) (✓ shipped)

**One PR.** Extend `tools/report.py` (TOOLING §15a). Both sections close research loops
already in flight - cross-referencing past searches and proposing question closures -
so they share the most original logic of the remaining sections and are paired together.

**Section 5 - Search-log awareness.** For leads in sections 1-4: query `search_log` for
matching `(person_id, collection)`. Annotate with "already searched {date}." Entries older
than 18 months → "worth re-running."

**Section 5b - Answerable questions.** Open questions in `notes/questions.md` where the
referenced gap now has an accepted claim → propose closure. Print proposals; do not execute.

**Done when:**
```sh
fha report --root example-archive   # sections 0-5b printed without error
```

---

### M5.3 - `fha report` - sections 6-8 (calls into other tools) (✓ shipped)

**One PR.** Extend `tools/report.py` (TOOLING §15a). All four sections are thin
formatting wrappers around tools built in earlier layers (photoindex, places, cooccur) or
simple counts - the lightest remaining report work, bundled together for that reason.

**Section 6 - Photo triage.** Call `photoindex.triage(root, top=10)` and embed ranked list.

**Section 6b - Place candidates.** Call `places.candidates(root)` if places tool is built;
else stub with a note.

**Section 7 - Hypotheses & draft queues.** From `hypotheses WHERE status='open'`: count per
person. From `person_files` kind='draft-queue': persons whose file is non-trivially non-empty.

**Section 8 - Possible connections.** Call cooccur logic; format top-10 person-pair candidates
with "[confirm] [dismiss]" labels. Acting on a label is `fha confirm cooccur` / `fha confirm
dismiss` (M4.4); the report itself only prints. The §6b place candidates carry the same
confirm-or-dismiss prompt, actioned by `fha confirm place`.

**Done when:**
```sh
fha report --root example-archive   # all 8 sections printed without error
```

---

## Layer 6 - Data output (Milestone 6 - ✓ shipped)

Depends on: index (+ photoindex for packet). Tools in this layer are independent of each
other and of layers 4-5; build in any order once layer 3 is done.

---

### M6.1 - `fha packet` (✓ shipped)

**One PR.** New file `tools/packet.py`. Wire `fha packet <P-id> [-o out/]
[--include-research] [--include-restricted] [--include-dna] [--no-photos]
[--dry-run] [--overwrite]` (TOOLING §8).

Verify person is curated and not `living: true|unknown`. Privacy: `living: unknown` = living.
Sources: distinct `source_id` from `claim_persons`; exclude `restricted: true` unless flag; exclude DNA unless `--include-dna`.
Resolve asset files via `resolve_path()`; note missing. Photos (requires photoindex; skip if
absent/stale or `--no-photos`): union of `pid-keyword` + `face-tag` + `name-match` from
`photo_people`. Generate fresh `timeline.md`. Create directory → zip → print zip path.
Existing packet output refuses unless `--overwrite`; `--dry-run` previews without writing.

Output directory structure:
```
packet_{surname}_{P-id}_{date}/
  README.txt       ← manifest, disclaimer, living-person caution
  profile/         ← person .md (+ research if --include-research)
  timeline.md
  sources/         ← source record .md files
  files/           ← asset copies (original filenames)
  photos/          ← photo copies
```

**Done when:**
```sh
fha packet P-de957bcda1 --root example-archive --no-photos   # zip produced
# zip contains README.txt, profile/, timeline.md, sources/, files/
fha packet P-de957bcda1 --root example-archive --include-research  # adds research file
```

---

### M6.2 - `fha places lint` + `fha places candidates` (✓ shipped)

**One PR.** New file `tools/places.py`. Wire both into `fha.py` (TOOLING §10).

**`fha places lint`:** orphan L-ids in `claims.place_id`; duplicate place names (case-folded);
dangling `within:` links; cyclic `within:` chains (visited-set walk); `within:` on a
non-micro-place.

**`fha places candidates`:** normalize distinct `claim.place_text` (case-fold; expand St/Street,
Co/County); cluster near-variants via token-set match; emit groups ≥3 occurrences with claim
count and EDTF spread. GPS clusters: ≥3 photos within ~150m with no known L-id coords nearby.

**Done when:**
```sh
fha index --root example-archive                     # build the index first
fha places lint --root example-archive               # exits 0 (clean fixture)
fha places candidates --root example-archive         # exits 0
fha index --root tests/fixtures/broken-places
fha places lint --root tests/fixtures/broken-places  # fires on orphan L-id + dangling within:
```

---

### M6.3 - `fha places geocode` (✓ shipped)

**One PR.** Extend `tools/places.py`. Wire `fha places geocode [--place L-id] [--all]
[--offline]` (TOOLING §10).

Download GeoNames `cities15000.txt` into `.cache/geonames/` on first run (skip if `--offline`).
Match `name` + `hierarchy` tokens against GeoNames fields. On unique high-confidence hit:
propose `coords` and alt-names → prompt `[y/N]` before any write. Use `ruamel.yaml` to
preserve comments (permitted here, per TOOLING §10).

Every write requires interactive confirmation.

**Done when:**
```sh
fha places geocode --all --root example-archive --offline  # exits 0 without network
# confirmation prompt fires; no write on N
```

---

### M6.4 - `fha gedcom` (✓ shipped)

**One PR.** New file `tools/gedcom.py`. Wire `fha gedcom [<P-id>] [--mode descendants|
ancestors|connected] [--generations N] [--all] [--include-living] [--out FILE]`
(TOOLING §13a).

From `relationships` edges and accepted vital claims: INDI records (name, sex, birth, death);
FAM records (spouse pairs + children). `living`/`unknown` → `/Living/` by default;
`--include-living` overrides. Each vital claim's `source_id` → SOUR note. Emit header comment:
"generated by fha gedcom - do not re-import as truth."

**Done when:**
```sh
fha gedcom P-de957bcda1 --mode descendants --root example-archive  # valid .ged file
# living persons show /Living/; --include-living shows real names
```

---

### M6.5 - `fha wikitree` (✓ shipped)

**One PR.** New file `tools/wikitree.py`. Wire `fha wikitree <P-id> [--out FILE]`
(TOOLING §13).

`[[S-id]]` in profile body → `<ref name="S-id"/>` at use; definitions collected into hidden
`<div name="references">` block (deduplicated). Factual sentences with cited claim `place`/
`date` → `<span class="spacetime" data-loc="…" data-date="ISO">`. `[[P-id]]` → WikiTree link
from `person_external`; plain name if absent. Ancestry image URLs in `external_links` →
`{{Ancestry Image|db|id}}`. Template hooks: `tools/wikitree_templates.yaml`. Output to stdout;
`--out FILE` writes.

**Done when:**
```sh
fha wikitree P-de957bcda1 --root example-archive   # valid WikiTree markup to stdout
# each S-id appears once in the references div
```

---

### M6.6 - `fha gedcom import` (✓ shipped - 2026-07 usability follow-up, plan 06)

**One PR.** New file `tools/gedcom_import.py` + a dispatcher intercept in `fha.py`
(`_intercept_gedcom_import`, the `fha id check` mechanism - the exporter's positional
`P-id` parser in `gedcom.py` stays untouched). Wire
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

**Done when:**
```sh
python tools/fha.py gedcom import family-tree.ged --root <archive>            # plan, no writes
python tools/fha.py gedcom import family-tree.ged --root <archive> --apply   # stubs + record + copy
# fha index && fha lint afterward: no E-codes; re-running the same file: exit 2, zero writes
```

---

## Layer 7 - Intake pipeline (Milestone 7 - core side)

`fha process` depends on: index, lint. `fha convert-mining` depends on: index, lint, stubs.
`fha process` is split into four PRs - variation detection and bundle dissolution are each
significant scope on their own.

This layer is the deterministic core of intake: `fha process` (M7.1-M7.4) and the
`fha convert-mining` importer (M7.5). The `fha capture` web on-ramp - engine, recipes,
`--ingest` sweep, browser companion, native host - is a separate track in
[`BUILD_INGESTION.md`](BUILD_INGESTION.md) (the MG series; design in
[`TOOLING_INGESTION.md`](TOOLING_INGESTION.md)) and hands off to `fha process`.

---

### M7.1 - `fha process` - document files (Stage A, single-file mode) (✓ shipped)

**One PR.** New file `tools/process.py`. Wire `fha process <file> [--type TYPE]
[--title "…"] [--slug SLUG]` (TOOLING §6, document root only).

Detect as document (not a photo extension; not under photos root). Refuse if filename already
contains `_{S-id}.` pattern. Mint S-id. **Documents root: rename** `{slug}_{S-id}.{ext}` in
place (record `original_filename`). Scaffold `sources/{type}/{slug}_{S-id}.md` from §14
template; `files:` pre-filled; empty `## Claims`. Print record path. Any failure → rollback.

Source-stub sidecar (`*.notes.md`): read frontmatter hints; locate companion asset (same stem);
pre-fill record from hints; stub body → record `## Notes`; delete stub after success.

**Done when:**
```sh
fha process tests/fixtures/sample.pdf --root example-archive  # mints, renames, scaffolds
fha process tests/fixtures/sample.notes.md --root ...         # pre-fills; deletes stub
# rollback: interrupted processing leaves no partial state
```

---

### M7.2 - `fha process` - photo files (✓ shipped)

**One PR.** Extend `tools/process.py` with photo-root support (TOOLING §6).

**Photos are never renamed.** Detect as photo. Refuse if `SOURCE: S-id` keyword already
embedded. Mint S-id. Run `exiftool -keywords+="SOURCE: {S-id}" -overwrite_original_in_place`
 - abort on failure, do not scaffold. Scaffold `sources/photos/{slug}_{S-id}.md`; existing
photo path in `files:`, `role: primary`, `is_primary: true`.

`--more FILE role[:copy]` attaches additional files to an existing record.

**Done when:**
```sh
fha process tests/fixtures/photo.jpg --root ...           # SOURCE: embedded; no rename
fha process photo.jpg --more photo_back.jpg role:back ... # adds files: entry
# refusing already-processed photo (SOURCE: present)
```

---

### M7.3 - `fha process` - folder mode + variation detection (✓ shipped)

**One PR.** Extend `tools/process.py` (TOOLING §6).

**Folder mode.** Run photoindex triage scorer over the folder; print ranked candidates; prompt
to select (number, comma-list, or `all`); process selected items one by one.

**Tier 1 variation detection.** Before processing any file, scan its directory for siblings
sharing the same `base_id` (via `parse_media_filename`). If found:
```
Found N files that appear to be variations of the same photo:
  portrait_1880.jpg        [primary]
  portrait_1880_back.jpg   [role: back]
Process as ONE source (shared S-id) or separately?  [one / separate / skip]
```
On `one`: one S-id; SOURCE: keyword on all; one record with all files in `files:` with role
annotations. On `separate`: process each independently. On `skip`: defer.
Display batch type label A-D (TOOLING §6 table) - informational only.

**Done when:**
```sh
# variation pair → "one" → one record with two files:
fha process tests/fixtures/photo-fixture/ --root ...
```

---

### M7.4 - `fha process` - bundle folder dissolution (✓ shipped)

**One PR.** Extend `tools/process.py` (SPEC §12.1, TOOLING §6).

**Bundle folder dissolution.** Folder with `notes.md` = source stub bundle.
Mint one S-id. Photos: embed SOURCE: keyword; no rename. Documents: rename and file to tree.
Scaffold one record pre-filled from `notes.md`; `files:` lists all assets. Delete the bundle
folder after success.

**Done when:**
```sh
# bundle folder → single record; folder dissolved; notes.md content in ## Notes
fha process tests/fixtures/bundle-folder/ --root ...
```

---

### M7.5 - `fha convert-mining` (✓ shipped)

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
fha convert-mining --root tests/fixtures/legacy-export --apply # writes; lint exits clean
```

---

## Layer 8 - Publication (Milestone 8)

Depends on: index, photoindex (photo strips), `fha views tree --format json` (tree data
contract). Jinja2 is the one new permitted dependency; add to requirements. Five PRs -
each is independently shippable.

---

### M8.1 - `fha site` - foundations: query layer, Jinja2, source page

**One PR.** New file `tools/site.py`. Wire `fha site [--out PATH] [--linked]`
(TOOLING §12). This phase ships the infrastructure every later page depends on, proved out
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
`site/media/`; redaction baked in (restricted / DNA / `publication_ok: false` sources
excluded). `--linked`: fast local preview; real archive paths; no copies; no redaction.

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
```

---

### M8.2 - `fha site` - person page

**One PR.** Extend `tools/site.py` (TOOLING §12).

**Person page (curated):** summary block (accepted vitals); biography HTML (using the
prose converter and token-swap from M8.1); timeline (same index query as
`fha views timeline`); sources index (same query as `fha views sources-index`); photo strip
(from `photo_people`); Stories; Friends & Family (relationship edges).

**Person-page redaction:** `living`/`unknown` → "Living Person", no person page generated
at all under `--standalone`.

**Done when:**
```sh
fha site --root example-archive --linked
# Thomas Hartley person page: summary, bio, timeline, sources, F&F rendered
# [[S-xxxx]] links in bio -> link to source page
fha site --root example-archive
# a living/unknown person generates no page; nothing else links to one
```

---

### M8.3 - `fha site` - place + discoveries pages

**One PR.** Extend `tools/site.py` (TOOLING §12).

**Place page:** name, coords (map URL - no embedded map), dated `history:`, claims naming it,
micro-places, people ranked by association frequency.

**Discoveries page:** render `notes/discoveries.md` as HTML; link P-id and S-id mentions.

**Done when:**
```sh
fha site --root example-archive --linked    # place and discoveries pages present
fha site --root example-archive             # discoveries page redacts living-person names
```

---

### M8.4 - `fha site` - home page + standalone redaction audit

**One PR.** Extend `tools/site.py` (TOOLING §12).

**Home page:** surname A-Z index; recent-discoveries teaser (last 5 entries).

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
```

---

### M8.5 - `fha site` - interactive tree rendering

**One PR.** Extend `tools/site.py` (TOOLING §12).

Vendor a client-side tree library (`family-chart`, MIT, D3-based - or comparable) into
`tools/templates/vendor/`. Write an adapter mapping the neutral tree JSON contract
(from `fha views tree --format json`) to the library's input format. The adapter is the only
place that knows about the library; swapping renderers later touches only the adapter.

At build time: generate `site/data/tree_{P-id}_{mode}.json` (the `_{mode}` suffix
keeps the descendant and ancestor artifacts distinct when the same person seeds
both) for the root person's descendant tree and ancestor pedigrees per curated
person (ancestors mode, 3 generations default). The home descendant tree seeds
from the **apex of `root_person`'s line** (its most distant recorded ancestor),
not `root_person` literally: the Ahnentafel `root_person` is the proband and has
no descendants, so seeding there would yield a single node - seeding from the apex
makes the explorer fan forward across the whole family (TOOLING §12's "descendant
explorer from a root ancestor"). The tree JSON is written to `site/data/` as the
reusable artifact *and* embedded inline in each page (read from the DOM, not
fetched, so it renders from `file://`); redaction is applied server-side in the
JSON so a published tree never names a living person.

**Done when:**
```sh
fha site --root example-archive
# home page: interactive descendant tree from root person
# each curated person page: ancestor pedigree (≥2 generations)
# library vendored; no CDN; works from file://
```

---

## Layer 9 - Scaffolding (Milestone 9)

Build last - the manifest must list every operating-layer file, so this is only stable once
the tool suite is substantially complete.

---

### M9.1 - `fha install` + `manifest.json` (✓ shipped)

**One PR.** New file `tools/scaffold.py`. Wire `fha install <archive-path>`. Create
`manifest.json` at repo root (TOOLING §13c). This phase ships the manifest and the
write-once skeleton installer; `fha update-tools` (the diff/backup logic - a meaningfully
different risk profile, since it touches an existing populated archive rather than an
empty one) is M9.2.

**`manifest.json`:** one JSON object listing every operating-layer file with `path`, `sha256`,
`spec_version`. Covers `tools/`, `SPEC.md`, `TOOLING.md`, `AGENTS.md`, `AGENTS_TOOLING.md`,
`CLAUDE.md`, `BUILD.md`, the public `README.md` (project orientation), the agent workflow
procedures under `.claude/skills/`, and the skeleton (`fha.yaml` template, the empty record
dirs, seeded `places.yaml`). The guiding rule is *everything a genealogist needs to operate*,
not a hand-picked minimum. Also covers the human-facing docs that must ship into every archive:
`docs/GETTING_STARTED.md`, `docs/SETUP_FROM_ZIP.md`, `docs/CHEATSHEET.md`,
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
This is the primary install path for non-technical users (see PR 09 / `docs/SETUP_FROM_ZIP.md`).

**Done when:**
```sh
python tools/fha.py install ./test-archive --repo .   # skeleton; .plaintext-version written
# Python < 3.10 → friendly message, no traceback
# exiftool absent → friendly guidance message, install proceeds (not a hard stop)
# --repo pointing to an unzipped download (no .git/) → same result as a git clone
# docs/GETTING_STARTED.md, docs/SETUP_FROM_ZIP.md etc. present in installed archive
```

---

### M9.2 - `fha update-tools` (✓ shipped)

**One PR.** Extend `tools/scaffold.py`. Wire `fha update-tools [--dry-run]` (TOOLING §13c).

**`fha update-tools [--dry-run]`** (run from within an archive; `--repo PATH` required):
compare manifest against `.plaintext-version`. For each file - new → copy in; unchanged
(checksum matches) → overwrite silently; customized (checksum differs) → move to
`.plaintext-backup/{date}/` and report; retired from manifest → move to backup and report.
Never deletes. Never silently overwrites customized files. All output is plain English -
"Updating tools/index.py (unchanged)" or "Your edited tools/fha.py has been backed up to
.plaintext-backup/2026-06-22/fha.py - the new version is now in tools/fha.py." No technical
diffs or checksums shown by default; `--verbose` may add them.

**Done when:**
```sh
fha update-tools --dry-run --repo .                  # reports plan in plain English; no writes
# edit tools/fha.py; fha update-tools → plain "backed up + updated" message
# no traceback on any error; missing --repo → "Run this command from inside your archive,
#   with --repo pointing to your copy of the plaintext tools."
```

---

## Layer 10 - Working-copy mode (Milestone 10 - shipped)

**Status: shipped.** The working-copy mode is formalized in
**SPEC §12.4** (the law) and **TOOLING §13d** (the per-tool design): it is flagged by a visible,
git-ignored **`WORKING_COPY`** marker file at the archive root - *not* an `fha.yaml` key, because
the mode is machine-local and an `fha.yaml` key would sync back and flip the main archive too.
This section records the shipped contract.

**The problem.** A genealogist keeps the *main* archive on one computer, with the photo and
document libraries living in (often external) asset roots. They want to sync a **plain-text
working copy** to a second computer via git - which carries the `sources/*.md`, `people/*.md`,
`places.yaml`, and `notes/` but **not** the binary assets (assets are gitignored or live
outside the repo). On the second computer they want to do real work that needs no originals:
write narratives from existing accepted claims, build inference against existing records, drop
new material into `inbox/` to carry back. The blocker is that the asset-aware tools, run where
the files are absent, misbehave: `fha photoindex`'s incremental scan **prunes cache rows for
files it can't find** (it would empty the photo cache), `fha lint` floods E011 ("file missing
on disk") for every asset, and `fha index` records every `source_files.exists_on_disk = 0`.

**The key insight.** Most tools already survive missing assets - only the *asset* paths break.
`fha index` rebuilds the **query surface from the plain-text `.md` files**, which *are* present
in a working copy and are exactly what narrative-writing and inference read. So the rule is not
"block rebuilds" - it is **"treat assets as assumed-present-elsewhere, never as missing or
deletable."** `fha index` stays available; only the asset side is neutralized.

**The design - one visible marker file, conservative everywhere.** The flag is the presence of a
file named `WORKING_COPY` at the archive root (git-ignored, machine-local), holding a plain note
for whoever finds it:
```
WORKING_COPY  (file at the archive root)
─────────────────────────────────────────
This is a working copy synced from the main archive. The photo and document
files don't live here - the tools won't treat them as missing or lost. Delete
this file to turn the archive back into a full, asset-aware archive.
```
A marker file - not an `fha.yaml` key - because the mode is a fact about *this machine's copy*:
`fha.yaml` syncs, so a key there would be committed and pulled back to the main archive, flipping
*it* into working-copy mode (the exact failure the mode prevents). The marker is git-ignored, so
it can never travel. It is *visible* (a human-managed control belongs in the file browser, like
the rest of the archive); only its existence matters. When present, every tool becomes *more
conservative, never less*:
- **lint** suppresses E011/E012 (asset-on-disk checks) and prints one line:
  `working copy: N asset files assumed present in the main archive` (a note, not a warning).
- **index** records asset presence as **unknown** (`exists_on_disk = NULL`), not missing (`0`),
  and skips the on-disk asset-reconciliation glob. The claim/person/source/relationship surface
  is built normally from the `.md` files.
- **photoindex scan** refuses and **never prunes** (`this is a working copy - run photo
  indexing on your main archive`); read-only photo queries return whatever the cache holds.
- **asset-mutating commands** (`fha process <file>`, `fha photoindex tag-person`,
  `fha photoindex set-summary`, `fha packet`)
  refuse with a plain `the photo/document files aren't here - do this on your main archive`,
  exit clean. Plain-text editing, `fha index`, `find`, `views`, `report`, and
  `fha capture` → `inbox/` all work normally.
- **doctor** headlines the mode and stops flagging missing asset roots as errors.

**Why it keeps behavior obvious** (the governing constraint): the flag is one visible file the
human owns; every command announces the mode in one line of output; the mode only ever
*withholds* destructive/asset actions (never fabricates data, never silently rebuilds over
absence); the file-browser + text-editor experience is unchanged; and round-trips are safe
because `.cache/` and `WORKING_COPY` are gitignored (a working copy's caches and mode flag never
reach the main archive) and the return path is the existing `inbox/` plan. Because a working copy
never edits `fha.yaml`, that file may keep syncing without conflict.

**Ratified SPEC amendment (SPEC §12.4):** working-copy mode is flagged by a visible, git-ignored
`WORKING_COPY` marker file at the archive root (existence = mode; content is a human note). Tools
must treat absent assets as present-but-unavailable (suppress on-disk asset errors, never prune
asset caches, refuse asset-mutating operations) and must announce the mode. The human opts in/out
with `fha working-copy on|off` (the friendly front door) or by hand; `fha install`/`update-tools`
neither create nor remove the marker. The per-tool behaviour is specified in TOOLING §13d.

**Phasing:**
- **M10.1** - `tools/working_copy.py` provides `fha working-copy on|off|status` (creates /
  removes the marker + keeps it git-ignored / reports the mode - so the human never has to know
  the filename). `on` needs no confirm (it only withholds); `off` is the risky direction and
  **prompts** (`y/N`, default No, `--yes` to bypass) with a warning - sharpened when the asset
  roots look empty/unreachable - because switching off where the originals are absent would let a
  photo re-index prune the catalogue. Plus `is_working_copy(archive_root)` plumbing in `_lib`
  (existence check on the
  marker, read by every other tool) + lint E011/E012 suppression + a `tests/fixtures/working-copy/`
  fixture (records present, asset roots empty, a `WORKING_COPY` marker) that lints clean. index
  `exists_on_disk = NULL` + skip reconciliation. photoindex scan refusal/no-prune.
  Asset-mutating-command refusals. doctor mode banner. One-line mode banner in the shared CLI
  entry. Seed a starter archive `.gitignore` (currently `archive-template/` has none) listing
  `WORKING_COPY`, `.cache/`, `.plaintext-*`, and the local asset roots, so a git-syncing
  genealogist gets the never-sync-back guarantee for free.

**Done when (target):**
```sh
fha working-copy on --root my-copy   # creates WORKING_COPY marker + gitignore entry; plain confirm
fha working-copy status --root my-copy  # "this is a working copy …"
# then, with assets absent:
fha lint --root my-copy          # no E011 flood; one "working copy" note; exits per real findings
fha index --root my-copy         # builds the claim/person surface; assets exists_on_disk = NULL
fha photoindex --root my-copy    # refuses, prunes nothing
fha process some.jpg --root my-copy  # plain "do this on your main archive" refusal
fha report --root my-copy        # narrative-writing surface works
fha working-copy off --root my-copy  # WARNS + confirms (are the originals really here?) before
                                     # re-enabling asset features; --yes to skip the prompt
```

---

## Testing invariants (all PRs)

Every PR must leave `fha lint --root example-archive` exiting 1 with only the documented
baseline warnings (TOOLING.md §15 - currently W101, Thomas Hartley's intentionally absent death
record, and W102, one suggested claim staged as review-demo material). No new errors or warnings
may appear.
Broken fixtures in `tests/fixtures/broken-{CODE}/` must continue to fire their targeted code.
Any new lint code being implemented in that PR requires a new broken fixture for it.

`tools/README.md` is the authoritative implementation-status record. Before closing any PR:
update the relevant rows there. A flag or code that exists in the CLI but is absent from that
table - in any state - is documentation debt that blocks handoff.

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
