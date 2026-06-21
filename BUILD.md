# BUILD.md — fha tool suite: build sequence

This file is the complete build guide for the `fha` CLI, written as if nothing exists yet.
Every tool appears in dependency order with the same level of detail — algorithm, constraints,
and done criteria. Implementation status is tracked separately in `tools/README.md`.
Design rationale lives in `TOOLING.md`; this file tells you the sequence and how to verify it.

**Tool conventions (all PRs):**
- One Python file per tool under `tools/`. Tools never import other tools.
- Shared code only in `tools/_lib.py`.
- Every subcommand defines its own `--root PATH` flag (argparse does not propagate parent flags).
- Exit codes: 0 clean · 1 warnings · 2 errors · 3 tool failure.
- Every mutating operation ships `--dry-run`.
- `tools/README.md` is the authoritative implementation-status record. Update it on every PR.

---

## Milestones

Each Layer below maps 1:1 to a milestone — a shipped batch of capability. Layers are split
into more PR-sized phases than earlier drafts of this doc so that no single phase is
dramatically larger than its neighbors; splitting a layer into more phases never changes
its milestone number.

**Every phase is numbered `M{milestone}.{phase}`** in its `###` heading (e.g. `M3.2`) —
that number is the stable handle for asking for a review of that specific PR ("review
M3.2", or "review M3" for the whole milestone). The number is positional within its
milestone, not tied to a tool name, so renaming a phase's heading later doesn't change its
number; only inserting/removing a phase does — if that happens, renumber everything after
the insertion point in the same edit.

| Milestone | Layer | Phases | Status |
|---|---|---|---|
| 1 | Layer 1 — Foundation | M1.1 – M1.8 | ✓ shipped |
| 2 | Layer 2 — Archive views & discovery | M2.1 – M2.5 | ✓ shipped |
| 3 | Layer 3 — Photo catalog | M3.1 – M3.4 | ✓ shipped — M3.1 (`photoindex` scan/schema/grouping), M3.2 (`photoindex find`), M3.3 (`photoindex triage`/`report`), M3.4 (`photoindex reconcile`/`tag-person`) |
| 4 | Layer 4 — Cross-reference & connection | M4.1 – M4.3 | ✓ shipped — M4.1 (`fha xref`), M4.2 (`fha cooccur`), M4.3 (`fha find --related`) |
| 5 | Layer 5 — Research report | M5.1 – M5.3 | ✓ shipped — M5.1 (`fha report` §0–4 + snapshot), M5.2 (§5/§5b search-log + answerable questions), M5.3 (§6–8 photo triage/place candidates/hypotheses/cooccur) |
| 6 | Layer 6 — Data output | M6.1 – M6.5 | future |
| 7 | Layer 7 — Intake pipeline | M7.1 – M7.8 | future |
| 8 | Layer 8 — Publication | M8.1 – M8.5 | future |
| 9 | Layer 9 — Scaffolding | M9.1 – M9.2 | future |

---

## Dependency overview

```
_lib ──────────────────────────────────────────────────────── all tools
index ────────────────────── views, find, doctor, stubs, xref, cooccur,
│                             report, packet, process, places, site, gedcom,
│                             wikitree, capture, convert-mining
│
photoindex ──────────────── report (triage §), packet (photos), find --text (D7)
│
xref + cooccur ──────────── find --related (D4), report (§8)
│
process ──────────────────── capture (hands off to process)
│
views tree (JSON) ────────── site (tree rendering adapter)
│
install/update-tools ─────── (needs complete tool manifest)
```

Tools with no inbound lines — places, gedcom, wikitree, packet, convert-mining — depend only
on the index and can be built in any order once the index is stable.

---

## Layer 1 — Foundation (Milestone 1 — ✓ shipped)

Everything else depends on these. Build in the order listed.

---

### M1.1 — `_lib.py` + `fha id`

**One PR.** Create `tools/_lib.py` (shared library — no CLI) and `tools/id.py`
(minting tool). Wire `fha id mint` and `fha id check` into `fha.py`.

**`_lib.py` — the four parsing primitives** (TOOLING §1):

```python
ID_RE     = re.compile(r'\b([PSCLH])-([0-9a-hjkmnp-tv-z]{10})\b', re.I)
TOKEN_RE  = re.compile(r'\[([PSCLH]-[0-9a-hjkmnp-tv-z]{10})\]', re.I)
FRONT_RE  = re.compile(r'\A---\r?\n(.*?)\r?\n---\r?\n', re.S)
CLAIMS_RE = re.compile(r'^## Claims.*?```yaml\r?\n(.*?)```', re.S | re.M)
```

`read_record(path) -> {meta: dict, claims: list, stories: str|None, body: str}`.
YAML scalar normalization: booleans and date objects coerce to strings (`'true'`/`'false'`/ISO).
Claims blocks: `yaml.safe_load`; parse failure is collected as a lint error, never a crash.
All file IO is UTF-8.

`edtf_bounds(s) -> (min_iso, max_iso)` — the EDTF subset the tools need (TOOLING §1 table):
year `1850` → `(1850-01-01, 1850-12-31)`; tilde/question `1850~`/`1850?` → widen ±1y;
decade `185X` → full decade; month `1850-05` → month bounds; interval `A/B` → (min(A), max(B));
open `[..1920]` → `(0001-01-01, 1920-12-31)`. Validate with the same regex used for E014.

`resolve_path(p, fha_yaml) -> str` — maps the first segment of a record path through
`fha.yaml`'s `roots:` map; missing alias defaults to `{archive_root}/{alias}`.

**`fha id mint [TYPE] [-n N]`** — draw N (default 1) IDs of type `P|S|C|L|H`.
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

### M1.2 — `fha index` — schema + full rebuild

**One PR.** New file `tools/index.py`. Wire `fha index [--root PATH]` into `fha.py`
(TOOLING §2). This phase ships the schema and the from-scratch path only; incremental
`--source` upsert and relationship derivation are M1.3.

**Full rebuild algorithm:**
1. Glob `sources/**/*.md`, `people/**/*.md`, `places/places.yaml`, `notes/**/*.md`.
2. Parse each with `read_record()`; insert in one transaction.
3. Scan all prose bodies for `TOKEN_RE` → `citations` table.
4. Glob asset trees for filenames carrying S-ids → `source_files` reconciliation.
5. Build FTS tables.

**Schema** (TOOLING §2 DDL — create verbatim):

```sql
CREATE TABLE persons(id TEXT PRIMARY KEY, name TEXT NOT NULL, surname TEXT, sex TEXT,
  living TEXT NOT NULL, tier TEXT NOT NULL, status TEXT DEFAULT 'active',
  merged_into TEXT, no_known_marriages INTEGER DEFAULT 0, no_known_children INTEGER DEFAULT 0,
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
CREATE TABLE citations(token TEXT, kind TEXT, path TEXT, line INTEGER);

CREATE VIRTUAL TABLE notes_fts USING fts5(path, content);
CREATE VIRTUAL TABLE transcripts_fts USING fts5(source_id, path, content);
```

**Done when:**
```sh
fha index --root example-archive         # exits 0; .cache/index.sqlite created
sqlite3 .cache/index.sqlite "SELECT count(*) FROM claims"  # non-zero
sqlite3 .cache/index.sqlite "SELECT count(*) FROM relationships"  # zero — M1.3 populates this
```

---

### M1.3 — `fha index` — incremental upsert + relationship derivation

**One PR.** Extend `tools/index.py`. Wire `fha index --source S-id [--root PATH]`
(TOOLING §2).

**Incremental mode** (`--source S-id`): delete then re-insert one source's rows. Deletion
order matters — delete `claim_persons` and `claim_links` before `claims`; delete `citations`
and `notes_fts` rows for the source path before `sources`. Reversing order leaves orphans.

**Relationship derivation** (after claims load, in both full-rebuild and incremental mode):
for each `accepted` claim — `relationship subtype: child-of` → `(child, 'parent', father)` +
reciprocal; `marriage` or `relationship subtype: spouse-of` → reciprocal `spouse` edges with
`date_start`/`date_end`; social subtypes → `friend`/`associate`/`neighbor` edges. Edges are
pure cache, re-derived from claims on every build — never hand-edited.

**Done when:**
```sh
fha index --root example-archive         # full rebuild now also derives relationships
sqlite3 .cache/index.sqlite "SELECT count(*) FROM relationships"  # non-zero
fha index --source S-4f5f215e60 --root example-archive  # incremental upsert; exits 0
```

---

### M1.4 — `fha lint` — engine + structural/reference errors (E001–E010)

**One PR.** New file `tools/lint.py`. Wire `fha lint [--root PATH] [--json]` into `fha.py`
(TOOLING §3). This phase ships the lint engine and the first ten error codes only;
inventory/keyword/agent-drift errors (E011–E018), all warning codes (W101–W110), and the
fix/formatter flags are later phases in this same layer.

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
phase alongside the other inventory-facing codes — it needs the same record-parsing
machinery this phase establishes, but the codes are numbered out of build order.

**Done when:**
```sh
fha lint --root example-archive              # exits 0 — no codes built yet fire on this fixture
fha lint --root tests/fixtures/broken-E001  # fires E001
# repeat for each of E002–E010 against its broken fixture
```

---

### M1.5 — `fha lint` — inventory, keyword, and agent-drift errors (E011–E018)

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
`**Label:**` pattern — do NOT split by line (inline multi-label form exists). Extract
`[S-id]` and `[P-id]` tokens per segment; compare to accepted claims of the matching type.

**E011 `missing-fixture` suppression.** Suppress entirely when the archive path is under
`example-archive/` or `tests/fixtures/` — stub asset references are intentional in those
fixtures. (An arbitrary directory merely *named* `tests` in a real archive is not fixture
space; see `is_fixture_path`.)

**Done when:**
```sh
fha lint --root example-archive              # exits 0 — W101 isn't built until M1.6
fha lint --root tests/fixtures/broken-E011  # fires E011
# repeat for each of E012–E018 against its broken fixture
fha lint --root example-archive --with-exif  # exits without crash (E012 path)
```

---

### M1.6 — `fha lint` — warning codes (W101–W110)

**One PR.** Extend `tools/lint.py`. No new flags — warnings ride the existing `fha lint`
invocation (TOOLING §3).

**Warning codes:**

| Code | Detection |
|------|-----------|
| W101 | Vitals gaps per curated person (missing accepted birth/death claims) |
| W102 | `suggested` claim backlog per source |
| W103 | Couple-folder bracket list `[child, …]` doesn't match accepted relationship claims |
| W104 | Summary block line with no supporting accepted claim |
| W105 | Hand-edits under a GENERATED header (deferred — detection requires mtime tracking; check is a no-op) |
| W106 | Accepted claim missing Mills analysis fields |
| W107 | Direct reference to a merged person |
| W108 | `README.md` older than last `SPEC.md` change |
| W109 | Non-vital accepted claim missing `notes` context; also catch-all for unrecognized `source_type` |
| W110 | Ahnentafel placement issue (requires `root_person` in `fha.yaml`) |

**Done when:**
```sh
fha lint --root example-archive              # exits 1; exactly one W101
fha lint --root tests/fixtures/broken-W103  # fires W103
# repeat for each W code that has a broken fixture
```

---

### M1.7 — `fha lint` — fix modes + formatter

**One PR.** Extend `tools/lint.py`. Wire `[--dry-run] [--mint-stubs] [--spawn-questions]
[--fix-inventory] [--format-check] [--format-write]` into `fha.py` (TOOLING §3).

**Fix modes** (gated behind explicit flags; always diff-previewed with `--dry-run`):
- `--mint-stubs` (E005): create stubs in `people/stubs/`
- `--spawn-questions` (E009): append templated question to `notes/questions.md`
- `--fix-inventory` (E011): placeholder/deferred — prints a warning and suggests `fha process`; full ID-glob rebuild is not yet implemented

**Formatter** (`--format-check` / `--format-write`): final-newline and CRLF line-ending hygiene (initial subset). Frontmatter key order, lowercase ID normalization, blank-line hygiene, and YAML list indentation are deferred.
`--format-write` applies what `--format-check` reports. Never rewrites prose.

**Done when:**
```sh
fha lint --root example-archive --format-check  # exits without crash
fha lint --root example-archive --mint-stubs --dry-run  # previews stub creation, writes nothing
fha lint --root example-archive --spawn-questions --dry-run
```

---

### M1.8 — `fha stubs`

**One PR.** New file `tools/stubs.py`. Wire `fha stubs [--root PATH] [--from-names "…"]`
into `fha.py` (TOOLING §5).

**Scan mode.** Collect all P-ids from the index (or in-memory lint pass) that lack a person
record. For each: create `people/stubs/{surname}__{given}_{P-id}.md` with minimal frontmatter
(`id`, `name`, `tier: stub`, `living: unknown`). Name/surname resolved from the claim `value`
text where parseable; else `unknown__unknown_{P-id}.md` (flagged for hand-rename). Never
overwrites; never moves a stub out of `stubs/`.

**`--from-names "Ethel Hartley; Frances Hartley"`** — mint new IDs and create stubs
interactively. One stub per semicolon-delimited name.

**Done when:**
```sh
fha stubs --root example-archive         # creates stubs for any unresolved P-ids
fha lint --root example-archive          # E005 count drops to 0 after stubs minted
fha stubs --from-names "Test Person" --root example-archive  # creates one stub
```

---

## Layer 2 — Archive views & discovery (Milestone 2 — ✓ shipped)

Depends on: index.

---

### M2.1 — `fha views` — timeline, sources-index, draft-queue

**One PR.** New file `tools/views.py`. Wire `fha views timeline`, `fha views sources-index`,
and `fha views draft-queue` into `fha.py`. Stub `brackets`, `tree`, `clean`, `refresh` as
"not yet implemented" so the CLI is coherent (TOOLING §7) — M2.2 builds `brackets`, M2.3
builds `tree`/`clean`/`refresh`.

All three sub-commands require a fresh index (exit 3 if absent). All write GENERATED-headed
`.md` files into the tree:
```
<!-- GENERATED by fha views <sub-command> on <ISO-date> — do not edit; regenerate instead -->
```

**`fha views timeline [P-id | --all-curated]`** → `…_timeline_{P-id}.md`.
Query: `claims JOIN claim_persons WHERE person_id = ? AND status IN ('accepted','needs-review')
ORDER BY date_min ASC NULLS LAST`. Group by decade (floor `date_min` year to decade). Line
format: `{date_edtf} — {type}: {value} [@ {place_text_or_name}] [{source_id}]`. After main
chronology, emit `## Unreviewed` section listing `suggested` claims in the same format.
`--all-curated`: iterate every person in `persons WHERE tier = 'curated'`.

**`fha views sources-index [P-id | --all-curated | --couple-folders]`**.
Per-person: union of source_ids from (a) `claim_persons → claims.source_id` and (b)
`source_people`. Group by `source_type`; each line: `{title} [{S-id}]` then indented record
path. Write `…_sources-index_{P-id}.md`.
Couple-folder variant: for each couple folder in `people/`, enumerate all profile P-ids in
that folder; union their source_ids; write `sources-index.md` at the folder root (no P-id —
the folder is its context).

**`fha views draft-queue [P-id | --all-curated]`** → `…_draft-queue_{P-id}.md`.
Load the person's profile body. Extract all `[S-id]` tokens via `TOKEN_RE`. Query all accepted
claims for the person → collect distinct `source_id`s. Set-diff: sources with accepted claims
NOT represented by any `[S-id]` token = the uncited backlog. Per uncited source: show title +
`[S-id]`, then indent each uncited claim as `{type}: {value} — {date_edtf}`. If diff is empty,
write: `All accepted claims are cited in the profile.`

**Done when:**
```sh
fha views timeline P-de957bcda1 --root example-archive   # file generated; GENERATED header
fha views sources-index --all-curated --couple-folders --root example-archive
fha views draft-queue P-de957bcda1 --root example-archive  # non-empty (profile is sparse)
fha lint --root example-archive   # still exits 1 W101; W105 does not fire on GENERATED files
```

---

### M2.2 — `fha views` — brackets (folder maintenance)

**One PR.** Extend `tools/views.py` with `brackets`; stub `tree`, `clean`, `refresh` as
"not yet implemented" so the CLI is coherent (TOOLING §7).

**`fha views brackets [--fix] [--dry-run]`** — folder maintenance, three concerns in one pass:

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

### M2.3 — `fha views` — tree, clean, refresh

**One PR.** Extend `tools/views.py` with the remaining sub-commands (TOOLING §7).

**`fha views tree <P-id> --mode ancestors|descendants|fan [--generations N]
[--format json|dot] [--out FILE]`** — traverses `relationships` edges (TOOLING §7):

- `ancestors`: BFS following `rel='parent'` edges recursively.
- `descendants`: BFS following `rel='child'` edges; for each visited descendant, add one-hop
  `rel='spouse'` edges as leaf nodes (don't recurse into spouse lineage).
- `fan`: BFS all edge types, 2-hop default; `--generations N` overrides depth.
- Cycle guard: visited-set on P-ids.

Output — **neutral tree JSON** (spec-pinned data contract):
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
`--format dot`: GraphViz DOT with `{name}\n({birth}–{death})` labels.
`--format html`: print deferral message directing to `fha site`; do not implement here.

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
fha views refresh --root example-archive            # regenerates all; lint still exits 1 W101
```

---

### M2.4 — `fha doctor`

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
| Backup reminder | always print: "Backup policy must cover archive root and all mapped roots" |

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

### M2.5 — `fha find` — ID types, `--text`, `--related` stub

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
`place_id = ?`; every record body mentioning `[L-id]` (from `citations`).

`<H-id>`: hypothesis entry from `hypotheses` table (status, basis, verified_claim); research
file path; every record body mentioning `[H-id]` token.

**`--text "…"`**: query `notes_fts` FTS table (if index present) then do a `re.search` pass
over sources, people, notes, and configured documents root. For each hit: path + context
snippet. `transcripts_fts` is provisioned but not yet populated — transcript search is
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

## Layer 3 — Photo catalog (Milestone 3 — ◐ in progress)

Depends on: index.
Unlocks: `fha find --text` photo captions (D7), photo gathering in `fha packet`, triage
section of `fha report`, photo count in `fha find <P-id>`.

---

### M3.1 — `fha photoindex` — scan, schema, variation grouping (✓ shipped)

**One PR.** New file `tools/photoindex.py`. Wire `fha photoindex [--full]` into `fha.py`
(TOOLING §9). Stub `find`, `triage`, `reconcile`, `tag-person`, `report` as "deferred to a
follow-up photoindex PR" so the CLI is coherent (views.py precedent) — M3.2–M3.4 build them.

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

**Scan.** Run `exiftool -j -r <fields> <photos-root>` — one process, JSON, batch 500 files.
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
- `is_primary`: file with no variant suffix, front of copy a if more than one (lexicographic tie-break — matches TOOLING §9, not file-path length).
- `photo_groups.edtf_resolved`: best-confidence EDTF across variants (more `!` components
  = higher confidence; prefer `~` over `?`). Any two variants whose bounds don't overlap
  → `date_conflict = 1`.

**Test fixture.** Create `tests/fixtures/photo-fixture/` with 3–4 placeholder images. Stub
`_run_exiftool()` so a test harness can inject pre-cooked JSON. Include at least one
variation pair and one image with a `SOURCE:` keyword.

**Done when:**
```sh
fha photoindex --root tests/fixtures/photo-fixture    # exits 0; photos.sqlite created
fha doctor --root example-archive                     # no regression
```

---

### M3.2 — `fha photoindex find` (✓ shipped)

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

### M3.3 — `fha photoindex triage` + `report`; unlock D7 (✓ shipped)

**One PR.** Extend `tools/photoindex.py` with the two read-only/reporting sub-commands.
Also update `tools/find.py` to include `photo_fts` in `--text` searches (D7 unlock)
(TOOLING §9, §15b). Grouped together because neither writes to the archive or the photos
themselves — both are "look at what's already indexed" features.

**`fha photoindex triage [--top N]`** (TOOLING §15b). Groups with `source_id IS NULL`. Score:
+3 non-null caption (human transcription heuristic), +2 pid-keyword hit, +1 date at Y!
confidence, +1 group has a `back` variant, -2 AI-only user_comment ("AI:" or "Model:" prefix).
Emit top N (default 10) with signals and suggested `fha process <path>`.

**`fha photoindex report`.** Print groups where `date_conflict = 1` with each photo's `edtf`
and `caption` — a date disagreement between front and back is a research finding.

**D7 unlock in `find.py`.** In `_text_search()`: if `photos.sqlite` fresh, query `photo_fts`
and merge hits as `[photo]` entries. If absent → append note. Update TOOLING §4a D7 entry.
(Already implemented as of milestone 2's `find.py` build — TOOLING §4a's D7 entry is marked
"implemented milestone 2." No further `find.py` change was needed for this phase.)

**Done when:**
```sh
fha photoindex triage --root tests/fixtures/photo-fixture
fha photoindex report --root tests/fixtures/photo-fixture    # prints conflict group
fha find --text "word" --root tests/fixtures/photo-fixture   # returns [photo] hit
fha find --text "word" --root example-archive                # prints "not searched" note
```

---

### M3.4 — `fha photoindex reconcile` + `tag-person` (✓ shipped)

**One PR.** Extend `tools/photoindex.py` with the two sub-commands that touch on-disk
state or embedded metadata — grouped together and kept separate from `triage`/`report`
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

## Layer 4 — Cross-reference & connection (Milestone 4 — ✓ shipped)

Depends on: index (claim_links, relationships).
Unlocks: `fha find --related` (D4), `fha report` section 8.

---

### M4.1 — `fha xref` (✓ shipped)

**One PR.** New file `tools/xref.py`. Wire into `fha.py`. Does not write to the archive —
output candidates only. Requires fresh index; exit 3 if absent (TOOLING §14a).

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

### M4.2 — `fha cooccur` (✓ shipped)

**One PR.** New file `tools/cooccur.py`. Wire into `fha.py`. Does not write to the
archive — output candidates only. Requires fresh index; exit 3 if absent (TOOLING §14a2).

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

Tombstone file read at startup; missing = empty dismissed set (not an error). Skill layer
writes dismissals; this tool only reads.

**Done when:**
```sh
fha cooccur --root example-archive   # exits 0; handles missing tombstone gracefully
fha lint --root example-archive      # no regression
```

---

### M4.3 — `fha find --related` — complete implementation (✓ shipped)

**One PR.** Replace the deferral stub in `tools/find.py` with real output. Update TOOLING §4a
D4 note: "implemented" (TOOLING §4a).

**By ID type:**

`--related <P-id>` — person's world: relationship edges (rel type + source count); top
co-occurring persons with no existing edge; places ranked by claim frequency; shared claim
values (occupation/military/membership) recurring with others; distinct sources; photos from
`photo_people` (note if photoindex absent).

`--related <L-id>` — place's world: claims naming the place; people ranked by frequency;
sources; photos within ~0.002° of coords; micro-places (`within: L-id` children).

`--related <S-id>` — source's world: its claims by status; persons; places; corroborating/
contradicting sources via `claim_links`; sibling sources sharing a person or repository.

`--related <C-id>` — claim's neighborhood: its source, persons, place; linked claims;
sibling claims (same person + same type).

`--related <H-id>` — hypothesis's neighborhood: person it concerns; claims referencing its
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

## Layer 5 — Research report (Milestone 5 — ✓ shipped)

Depends on: photoindex (triage section), xref (corroboration events), cooccur (section 8).

---

### M5.1 — `fha report` — sections 0–4 + snapshot (✓ shipped)

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

**Section 0 — Discoveries:** claims moving `needs-review → accepted` since snapshot; questions
newly `status: answered`; `claim_links` corroboration rows added since snapshot.

**Section 1 — Review queue** (W102): sources with `suggested` claims, grouped, oldest first.

**Section 2 — New since last session:** source_ids and person_ids in index not in snapshot.

**Section 3 — Vitals gaps** (W101): reuse lint W101 logic; curated persons first.

**Section 4 — Contradictions** (E009): `claim_links WHERE rel='contradicts'` with no open
question referencing both C-ids.

Output: markdown to stdout + `.cache/report_{date}.md`.

**Done when:**
```sh
fha report --root example-archive            # exits 0; sections 0–4 printed
fha report --root example-archive            # second run: section 0 empty
fha report --section review-queue --root ... # only section 1
fha report --full --root ...                 # ignores snapshot
```

---

### M5.2 — `fha report` — sections 5 + 5b (research-loop closure) (✓ shipped)

**One PR.** Extend `tools/report.py` (TOOLING §15a). Both sections close research loops
already in flight — cross-referencing past searches and proposing question closures —
so they share the most original logic of the remaining sections and are paired together.

**Section 5 — Search-log awareness.** For leads in sections 1–4: query `search_log` for
matching `(person_id, collection)`. Annotate with "already searched {date}." Entries older
than 18 months → "worth re-running."

**Section 5b — Answerable questions.** Open questions in `notes/questions.md` where the
referenced gap now has an accepted claim → propose closure. Print proposals; do not execute.

**Done when:**
```sh
fha report --root example-archive   # sections 0-5b printed without error
```

---

### M5.3 — `fha report` — sections 6–8 (calls into other tools) (✓ shipped)

**One PR.** Extend `tools/report.py` (TOOLING §15a). All four sections are thin
formatting wrappers around tools built in earlier layers (photoindex, places, cooccur) or
simple counts — the lightest remaining report work, bundled together for that reason.

**Section 6 — Photo triage.** Call `photoindex.triage(root, top=10)` and embed ranked list.

**Section 6b — Place candidates.** Call `places.candidates(root)` if places tool is built;
else stub with a note.

**Section 7 — Hypotheses & draft queues.** From `hypotheses WHERE status='open'`: count per
person. From `person_files` kind='draft-queue': persons whose file is non-trivially non-empty.

**Section 8 — Possible connections.** Call cooccur logic; format top-10 person-pair candidates
with "[confirm] [dismiss]" labels.

**Done when:**
```sh
fha report --root example-archive   # all 8 sections printed without error
```

---

## Layer 6 — Data output (Milestone 6)

Depends on: index (+ photoindex for packet). Tools in this layer are independent of each
other and of layers 4–5; build in any order once layer 3 is done.

---

### M6.1 — `fha packet`

**One PR.** New file `tools/packet.py`. Wire `fha packet <P-id> [-o out/]
[--include-research] [--include-restricted] [--include-dna] [--no-photos]` (TOOLING §8).

Verify person is curated. Privacy: `living: unknown` = living. Sources: distinct `source_id`
from `claim_persons`; exclude `restricted: true` unless flag; exclude DNA unless `--include-dna`.
Resolve asset files via `resolve_path()`; note missing. Photos (requires photoindex; skip if
absent or `--no-photos`): union of `pid-keyword` + `face-tag` + `name-match` from
`photo_people`. Generate fresh `timeline.md`. Create directory → zip → print zip path.

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

### M6.2 — `fha places lint` + `fha places candidates`

**One PR.** New file `tools/places.py`. Wire both into `fha.py` (TOOLING §10).

**`fha places lint`:** orphan L-ids in `claims.place_id`; duplicate place names (case-folded);
dangling `within:` links; cyclic `within:` chains (visited-set walk); `within:` on a
non-micro-place.

**`fha places candidates`:** normalize distinct `claim.place_text` (case-fold; expand St/Street,
Co/County); cluster near-variants via token-set match; emit groups ≥3 occurrences with claim
count and EDTF spread. GPS clusters: ≥3 photos within ~150m with no known L-id coords nearby.

**Done when:**
```sh
fha places lint --root example-archive               # exits 0 (clean fixture)
fha places candidates --root example-archive         # exits 0
fha places lint --root tests/fixtures/broken-places  # fires on orphan L-id + dangling within:
```

---

### M6.3 — `fha places geocode`

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

### M6.4 — `fha gedcom`

**One PR.** New file `tools/gedcom.py`. Wire `fha gedcom [<P-id>] [--mode descendants|
ancestors|connected] [--generations N] [--all] [--include-living] [--out FILE]`
(TOOLING §13a).

From `relationships` edges and accepted vital claims: INDI records (name, sex, birth, death);
FAM records (spouse pairs + children). `living`/`unknown` → `/Living/` by default;
`--include-living` overrides. Each vital claim's `source_id` → SOUR note. Emit header comment:
"generated by fha gedcom — do not re-import as truth."

**Done when:**
```sh
fha gedcom P-de957bcda1 --mode descendants --root example-archive  # valid .ged file
# living persons show /Living/; --include-living shows real names
```

---

### M6.5 — `fha wikitree`

**One PR.** New file `tools/wikitree.py`. Wire `fha wikitree <P-id> [--out FILE]`
(TOOLING §13).

`[S-id]` in profile body → `<ref name="S-id"/>` at use; definitions collected into hidden
`<div name="references">` block (deduplicated). Factual sentences with cited claim `place`/
`date` → `<span class="spacetime" data-loc="…" data-date="ISO">`. `[P-id]` → WikiTree link
from `person_external`; plain name if absent. Ancestry image URLs in `external_links` →
`{{Ancestry Image|db|id}}`. Template hooks: `tools/wikitree_templates.yaml`. Output to stdout;
`--out FILE` writes.

**Done when:**
```sh
fha wikitree P-de957bcda1 --root example-archive   # valid WikiTree markup to stdout
# each S-id appears once in the references div
```

---

## Layer 7 — Intake pipeline (Milestone 7)

`fha process` depends on: index, lint. `fha capture` hands off to process.
`fha convert-mining` depends on: index, lint, stubs. `fha process` is split into four PRs
and `fha capture`'s site recipes into two — variation detection, bundle dissolution, and
the four site recipes are each significant scope on their own.

---

### M7.1 — `fha process` — document files (Stage A, single-file mode)

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

### M7.2 — `fha process` — photo files

**One PR.** Extend `tools/process.py` with photo-root support (TOOLING §6).

**Photos are never renamed.** Detect as photo. Refuse if `SOURCE: S-id` keyword already
embedded. Mint S-id. Run `exiftool -keywords+="SOURCE: {S-id}" -overwrite_original_in_place`
— abort on failure, do not scaffold. Scaffold `sources/photos/{slug}_{S-id}.md`; existing
photo path in `files:`, `role: primary`, `is_primary: true`.

`--more FILE role[:copy]` attaches additional files to an existing record.

**Done when:**
```sh
fha process tests/fixtures/photo.jpg --root ...           # SOURCE: embedded; no rename
fha process photo.jpg --more photo_back.jpg role:back ... # adds files: entry
# refusing already-processed photo (SOURCE: present)
```

---

### M7.3 — `fha process` — folder mode + variation detection

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
Display batch type label A–D (TOOLING §6 table) — informational only.

**Done when:**
```sh
# variation pair → "one" → one record with two files:
fha process tests/fixtures/photo-fixture/ --root ...
```

---

### M7.4 — `fha process` — bundle folder dissolution

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

### M7.5 — `fha capture` — paste fallback + generic recipe

**One PR.** New file `tools/capture.py`. Wire `fha capture [--url URL] [--title "…"]
[--type TYPE] [--date DATE] [--asset FILE]` (TOOLING §13b).

Read HTML from stdin or `--asset`. Generic recipe: extract title (from `<title>` or `<h1>`),
URL (from `<base href>` or `<link rel="canonical">` or `--url`), accessed-date (today).
Create source stub in `inbox/` as `{slug}.notes.md`:
```yaml
title: "…"
source_type: web
citation: "…, accessed {date}"
repository: "{domain}"
external_links:
  - url: "…"
    accessed: "{date}"
```
Body: visible text (strip tags, ~2000 chars). `--asset FILE`: copy alongside stub. Write
research-log entry to `search_log` (via index if present; `.cache/capture_log.jsonl` fallback).

**Done when:**
```sh
echo "<html><title>Test</title></html>" | fha capture --url "https://example.com"
# stub in inbox/ with correct frontmatter
fha capture --url "…" --title "Override" --type newspaper
```

---

### M7.6 — `fha capture` — site recipes: Ancestry + FamilySearch

**One PR.** Create `tools/capture_recipes/` directory with the two genealogy-database
recipes. Each recipe exposes `detect(html, url) -> bool` and `extract(html, url) -> dict`.
`fha capture` tries recipes in priority order: Ancestry → FamilySearch → generic fallback
(Newspapers.com and FindAGrave are added by M7.7, ahead of the fallback).

Minimum extraction per site (TOOLING §13b): Ancestry (collection title, date, persons in
household/index table, image URL); FamilySearch (title, date, collection, persons from fact
table).

Add `tests/fixtures/capture-samples/` with one anonymized HTML snippet per site.

**Done when:**
```sh
fha capture < tests/fixtures/capture-samples/ancestry.html --url "https://ancestry.com/…"
# routes to Ancestry recipe; correct source_type
fha capture < tests/fixtures/capture-samples/familysearch.html --url "https://familysearch.org/…"
# each recipe: detect() True for own sample, False for the other's
```

---

### M7.7 — `fha capture` — site recipes: Newspapers.com + FindAGrave

**One PR.** Extend `tools/capture_recipes/` with the two remaining recipes, inserted into
the priority order ahead of the generic fallback: Ancestry → FamilySearch → Newspapers.com
→ FindAGrave → generic fallback.

Minimum extraction per site (TOOLING §13b): Newspapers.com (publication, date, page, article
snippet, formatted citation); FindAGrave (memorial name, birth/death, cemetery as
`place_text`, persons).

Add one anonymized HTML snippet per site to `tests/fixtures/capture-samples/`.

**Done when:**
```sh
fha capture < tests/fixtures/capture-samples/newspapers.html --url "https://newspapers.com/…"
fha capture < tests/fixtures/capture-samples/findagrave.html --url "https://findagrave.com/…"
# each of the four recipes: detect() True for its own sample, False for the other three
```

---

### M7.8 — `fha convert-mining`

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

## Layer 8 — Publication (Milestone 8)

Depends on: index, photoindex (photo strips), `fha views tree --format json` (tree data
contract). Jinja2 is the one new permitted dependency; add to requirements. Five PRs —
each is independently shippable.

---

### M8.1 — `fha site` — foundations: query layer, Jinja2, source page

**One PR.** New file `tools/site.py`. Wire `fha site [--out PATH] [--linked]`
(TOOLING §12). This phase ships the infrastructure every later page depends on, proved out
against the simpler of the two page types; the person page (more sections, plus the
person-specific half of redaction) is M8.2.

The site reads all structured data from `.cache/index.sqlite` directly — not from generated
`.md` view files.

`tools/templates/` directory with `base.html` layout. Token swap: `TOKEN_RE` → relative hrefs;
unresolved tokens → `<mark>[X-xxxx]</mark>`. Minimal stdlib HTML converter for prose
(headings, bold, lists, links) — no markdown library.

`--standalone` (default): web-optimized derivatives (max 1200px, EXIF stripped) into
`site/media/`; redaction baked in (restricted / DNA / `publication_ok: false` sources
excluded). `--linked`: fast local preview; real archive paths; no copies; no redaction.

**Source page:** citation block; claims table with status badges; thumbnails; file links.

**Done when:**
```sh
fha site --root example-archive --linked
# 1880 census source page: citation, claims table
```

---

### M8.2 — `fha site` — person page

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
# [S-xxxx] tokens in bio -> link to source page
fha site --root example-archive
# a living/unknown person generates no page; nothing else links to one
```

---

### M8.3 — `fha site` — place + discoveries pages

**One PR.** Extend `tools/site.py` (TOOLING §12).

**Place page:** name, coords (map URL — no embedded map), dated `history:`, claims naming it,
micro-places, people ranked by association frequency.

**Discoveries page:** render `notes/discoveries.md` as HTML; link P-id and S-id mentions.

**Done when:**
```sh
fha site --root example-archive --linked    # place and discoveries pages present
fha site --root example-archive             # discoveries page redacts living-person names
```

---

### M8.4 — `fha site` — home page + standalone redaction audit

**One PR.** Extend `tools/site.py` (TOOLING §12).

**Home page:** surname A–Z index; recent-discoveries teaser (last 5 entries).

**Standalone redaction audit.** Verify `--standalone` redaction is applied consistently
across every page type built in M8.1–M8.4 (source, person, place, discoveries,
home) — place pages must not link to redacted persons; the home page's surname index must
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

### M8.5 — `fha site` — interactive tree rendering

**One PR.** Extend `tools/site.py` (TOOLING §12).

Vendor a client-side tree library (`family-chart`, MIT, D3-based — or comparable) into
`tools/templates/vendor/`. Write an adapter mapping the neutral tree JSON contract
(from `fha views tree --format json`) to the library's input format. The adapter is the only
place that knows about the library; swapping renderers later touches only the adapter.

At build time: generate `site/data/tree_{P-id}.json` for the root person (descendants mode)
and ancestor pedigrees per curated person (ancestors mode, 3 generations default).

**Done when:**
```sh
fha site --root example-archive
# home page: interactive descendant tree from root person
# each curated person page: ancestor pedigree (≥2 generations)
# library vendored; no CDN; works from file://
```

---

## Layer 9 — Scaffolding (Milestone 9)

Build last — the manifest must list every operating-layer file, so this is only stable once
the tool suite is substantially complete.

---

### M9.1 — `fha install` + `manifest.json`

**One PR.** New file `tools/scaffold.py`. Wire `fha install <archive-path>`. Create
`manifest.json` at repo root (TOOLING §13c). This phase ships the manifest and the
write-once skeleton installer; `fha update-tools` (the diff/backup logic — a meaningfully
different risk profile, since it touches an existing populated archive rather than an
empty one) is M9.2.

**`manifest.json`:** one JSON object listing every operating-layer file with `path`, `sha256`,
`spec_version`. Covers `tools/`, `SPEC.md`, `TOOLING.md`, `AGENTS.md`, `AGENTS_TOOLING.md`,
`CLAUDE.md`, `BUILD.md`, and the skeleton (`fha.yaml` template, the empty record dirs, seeded
`places.yaml`). Excludes spec-repo furniture: `example-archive/`, `archive-template/` (its
*contents* seed the skeleton, but the folder itself is never copied into an archive — matching
TOOLING §13c), `tests/`, `.github/`, public `README.md`, `PRIVACY.md`, `RELEASE_CHECKLIST.md`.

**`fha install <archive-path>`** (run from repo clone): create skeleton; copy all manifest
files; write `.plainfile-version` with manifest version + per-file SHA256 checksums.

**Done when:**
```sh
python tools/fha.py install ./test-archive --repo .  # skeleton; .plainfile-version written
```

---

### M9.2 — `fha update-tools`

**One PR.** Extend `tools/scaffold.py`. Wire `fha update-tools [--dry-run]` (TOOLING §13c).

**`fha update-tools [--dry-run]`** (run from within an archive; `--repo PATH` required):
compare manifest against `.plainfile-version`. For each file — new → copy in; unchanged
(checksum matches) → overwrite silently; customized (checksum differs) → move to
`.plainfile-backup/{date}/` and report; retired from manifest → move to backup and report.
Never deletes. Never silently overwrites customized files.

**Done when:**
```sh
fha update-tools --dry-run --repo .                  # reports plan; no writes
# edit tools/fha.py; fha update-tools → edited version moved to .plainfile-backup/
```

---

## Testing invariants (all PRs)

Every PR must leave `fha lint --root example-archive` exiting 1 with exactly the documented
W101 (Thomas Hartley death record absent — intentional). No new errors or warnings may appear.
Broken fixtures in `tests/fixtures/broken-{CODE}/` must continue to fire their targeted code.
Any new lint code being implemented in that PR requires a new broken fixture for it.

`tools/README.md` is the authoritative implementation-status record. Before closing any PR:
update the relevant rows there. A flag or code that exists in the CLI but is absent from that
table — in any state — is documentation debt that blocks handoff.
