# AGENTS_TOOLING.md - Tool-Building and Code-Review

Supplementary instructions for **tool-building** and **code-review** modes.
Read this file at the start of any session in those modes.
Core contract, modes overview, research workflows, format reference, and tools: **AGENTS.md**.

---

## Tool-building: the implementation loop (per tool)

1. **Read** the tool's TOOLING section and every SPEC section it cites.
2. **Restate the contract** to the human before coding: inputs, outputs, flags, exit
   codes, what it must never do.
   Mismatch caught here is cheap.
3. **Write the documentation shell first.** Before any implementation, write the module
   docstring and all function stubs with their full docstrings. Each docstring should cover:
   what the function does, why the approach was chosen, and any domain constraint a fresh
   reader needs (EDTF quirks, GENERATED header contracts, two-table UNION rationale - the
   things that vanish from memory six months later). If you cannot explain the why before
   implementing, the design is still unsettled; resolve it here rather than in a comment
   retrofitted after the fact. For files with ≥5 non-trivial functions, add a code-map
   comment block near the top listing sections and functions with one-line purposes so the
   file is skimmable without reading every docstring. Use this phase as a final contract
   check - if the stubs reveal spec gaps, surface them now (see Spec-discovery protocol below),
   then flesh out the implementations.
4. **Implement** within the guardrails: Python ≥3.10; dependencies ONLY PyYAML, Jinja2
   (site), Pillow (optional - `fha site` standalone image derivatives; the site degrades
   gracefully without it, omitting images rather than copying originals), exiftool-as-binary
   - adding any other is a proposed decision, not a choice; one
   file per tool under `tools/`, shared code only in `_lib.py`, tools never import tools;
   no network access (geocoder's gazetteer download excepted).
   Split each command into an engine and an interface (TOOLING §1): `run_*` computes and
   returns a `_lib.Result` (it does not print report text or call `sys.exit`), and `_cmd_*`
   is the only layer that renders that `Result` and returns the exit code. `lint` is the
   reference implementation; this split is what lets any front door drive the same engine.
5. **Fixtures, not the archive.** Develop and test ONLY against `tests/fixtures/` copies.
   The real archive is never a test bed; destructive paths are exercised on fixtures exclusively.
6. **Definition of done:** `fha lint` runs clean on the clean pilot fixture; each of the
   tool's error codes fires on its broken fixture; `--dry-run` previews every mutating
   operation; help text exists; TOOLING still describes the tool accurately.
   **Completion gate:** every flag the CLI accepts and every E/W code the tool advertises
   must appear in `tools/README.md` as either ✓ implemented or ⚑ deferred before the tool
   is declared milestone-complete. A flag that exists in the CLI but is absent from that
   table - or present but neither working nor marked deferred - is documentation debt that
   blocks handoff. Do not declare a tool done while any flag or code is in an undocumented
   partial state.
7. **README review.** Before handoff, scan `README.md`, `docs/GETTING_STARTED.md`, and
   `tools/README.md` for any reference to the changed tool's behavior, flags, or build
   status and update anything now inaccurate. A working tool that a README still calls
   "not yet implemented," or whose flags the getting-started guide misdescribes, is a
   documentation bug. (The README rule from AGENTS.md spec-refinement mode binds tool-building
   as much as spec-refinement.)
8. **Spec and Tooling review.** Review `SPEC.md` and `TOOLING.md` for any decision made
   during the build that they do not yet capture - the goal is that those docs alone can
   regenerate the tooling. In **tool-building** mode you do NOT edit SPEC/TOOLING directly:
   record each gap as a *proposed* spec amendment for the human, then switch to
   **spec-refinement** mode (with approval) to make the edit. When mode boundaries conflict,
   **AGENTS.md wins** - it limits tool-building to `tools/`/`tests/` and forbids SPEC/TOOLING
   edits outside spec-refinement.
9. **Handoff:** demo the commands, note any deviation (there should be none unlogged).

### Spec-discovery protocol

When implementation reveals that TOOLING/SPEC is ambiguous, contradictory, or wrong - STOP.
Do not improvise past the spec (the docs must remain able to regenerate the tools).
Present the gap, propose the spec amendment, and proceed only after the human's call.

---

## Coding standards (tool-building mode)

**Before coding:** Map the control flow end-to-end for the area being changed - identify
CLI entrypoints, flags, file I/O, exit-code paths, and side effects before writing a line.
Identify ownership boundaries before touching shared code in `_lib.py`. There must be one
clear owner for each archive mutation or side effect; avoid duplicate pathways for the same
behavior. Preserve existing contracts (CLI flags, exit codes, SPEC-defined file formats)
unless the task explicitly requires changing them; validate all call sites when a shared
interface changes.

**Style:** Write clear, simple, maintainable Python. Prefer simplicity over cleverness;
optimize for readability by a single developer, not enterprise-scale abstraction. Use
straightforward control flow, small focused functions, and descriptive names. Favor boring,
predictable code over compact or clever code. Do not introduce new abstractions, helpers, or
architectural layers unless they clearly reduce complexity. Dead code is acceptable as
intentional scaffolding for a planned feature - tag it with a `# TODO:` comment explaining
what it scaffolds and what must happen before it is activated; remove dead code that has no
planned future use.

**Correctness:** Think through failure modes before finalizing - empty inputs, malformed
YAML, missing files, partial writes, interrupted runs, and `--dry-run` vs. live-execution
divergence. Make cleanup paths explicit; never leave the archive in an inconsistent state
after an error. Do not declare work complete while known medium or high correctness issues
remain.

**User-facing output:** The person reading this tool's output is a non-technical
genealogist (AGENTS.md → "Who you serve"). Hold every line he can see to this bar:
- **Every error states a cause AND the exact next command.** "No source found for S-7q2…
 - run `fha find S-7q2x9c4m1` to see what IDs exist" beats "lookup failed." A message that
  tells him something broke but not what to do is a dead end, and a dead end is a bug.
- **Jargon ships an example or a valid-list.** Any term from the format reference (EDTF,
  `source_type`, anchor, claim status, Crockford ID) gets a plain gloss plus a concrete
  example or the set of allowed values, inline in the message - never a bare "invalid X."
- **No traceback ever reaches the user.** Catch exceptions at the CLI boundary and translate
  them into a plain cause + next step; a Python stack trace on his screen is always a defect.
  (Keep the detail behind a `--debug`/verbose flag for tool-builders, not in the default path.)
- **Mutating operations preview in plain English.** `--dry-run` and pre-write previews
  describe what will change in words a file-browser user understands ("rename
  `letter.pdf` → `letter_S-…​.pdf` and create one source record"), not internal field dumps.

**Documentation:** The aesthetic target is Steinbeck or Hemingway, not Dickens or Kant.
Complex things expressed plainly - not plain things dressed up to seem complex.
A reader picking up the file cold should feel the code is on their side.

*Module docstrings:* every file gets an architecture overview: what this file is for,
how it fits into the larger system, and the shape of data flowing through it.
For files with ≥5 non-trivial functions, include a code-map comment block that lists
sections and functions with one-line purposes so the reader can jump directly to what they
need without reading the whole file.

*Function docstrings:* explain what the function does AND why the approach was chosen.
The what is often clear from the code; the why is what disappears without a docstring.
Prioritise domain context over technical restatement - a reader who knows Python but not
EDTF dates, Crockford IDs, or the GENERATED-header contract needs that context, not a
paraphrase of the implementation.

*Inline comments:* only for non-obvious decisions, tradeoffs, platform workarounds, or
subtle invariants. Never restate the code. Never explain what - only explain why, and only
when the why isn't already covered by the function's own docstring.

**Self-review:** After implementing, review your own diff as a strict code reviewer before
finalizing. Check correctness (failure modes, missing cleanup, contract mismatches, duplicate
side effects) AND documentation (every non-trivial function has a docstring that explains the
why; the module docstring reflects what was actually built; no inline comment merely restates
the code; the code map is accurate). Classify each issue as high, medium, or low severity.
Patch all high and medium issues before declaring done.

Before declaring any task complete, run the following six cross-cutting checks on every
changed file. These are the categories that most reliably survive a diff review but surface
in a downstream code-review pass:

**1 - Symmetry audit.** For each change, ask: is there a symmetric counterpart not yet
touched? Common pairs in this codebase: outgoing links ↔ incoming links (`claim_links`);
write/generate ↔ delete/clean; check in `views.py` ↔ matching check in `lint.py`; profile
files ↔ companion files (`person_profile_paths` ↔ `person_companion_paths`); full rebuild ↔
incremental upsert. A feature implemented for one direction but not its mirror is the single
most common source of missed issues across this PR.

**2 - Error path inventory.** For every new code path, explicitly enumerate three classes of
failure: (a) *absent* - the file doesn't exist, the ID is not in the index, the database is
missing; (b) *malformed* - empty file, corrupt or schema-less database, YAML parse failure;
(c) *misconfigured* - `root_person` is mistyped, a required `fha.yaml` key is absent, a root
mapping points nowhere. Each must either degrade gracefully with a message or return a
non-zero exit code. Silent success on a failure input is always wrong.

**3 - Config surface check.** Any path computed as `archive_root / 'documents'` or
`archive_root / 'photos'` must be verified: does `fha.yaml`'s `roots:` mapping allow that
directory to live outside the archive root? If yes - and for `documents` and `photos` it
always does - resolve the root through `fha_config`/`resolve_path` instead of hardcoding
the internal path. Hardcoded internal paths silently produce wrong results for any archive
with external asset roots.

**4 - Cross-tool propagation.** When a data contract changes - a new field, a widened scope,
a renamed column, a new operation - grep for every tool that implements or consumes that
contract. The same check that lives in `views.py` often has a parallel in `lint.py`. Data
deleted by one step of `upsert_source` must also be re-inserted by a later step if it is
regenerable. A contract change that is applied to `build_index` but not to `upsert_source`
leaves incremental mode silently wrong.

**5 - Simplification safety.** When replacing code for clarity, verify the new form is
equivalent for *all* valid inputs, not just the typical case. Specifically: integer-to-string
conversion does not preserve zero-padding (`int('040')` → `40` → `'40'` ≠ `'040'`); paths
may be absolute or relative; collections may be empty; `None` and an absent key are not
always interchangeable. For any replacement, name the input class where the original and
simplified forms would differ, and confirm that class is impossible or handled.

**6 - Recovery & plain-language audit.** For every new user-visible failure (AGENTS.md →
"Who you serve"), ask three things: (a) *Does it name the fix?* - the message states a cause
and the exact next command, not just that something broke. (b) *Does `fha doctor` know about
it?* - any new failure condition a human could hit unaided should be detectable and explained
by `fha doctor`, with the same plain-language next step. (c) *Could a non-technical user act
on it without opening SPEC.md?* - no leaked traceback, no bare error code, no unglossed jargon;
messy-but-recoverable input is inferred or met with one plain question, never a hard refusal.

**Completion:** Work to completion in one run - do not stop after partial implementation if
more required work is known. Keep interim narration brief so context is reserved for actual
work. If context limits prevent full completion, finish the highest-risk and most central
work first, then clearly list what remains.

---

## Code-review mode

**Invoked** as a pre-push review of the current branch.
**First share code review results before requesting to continue to fix the issues**

Use the full repository context, not just the diff. Read the changed files, nearby files,
tests, docs, CLI/API definitions, schemas, config files, and project instructions. Look for
issues that are likely to survive implementation but get caught during PR review.

Focus less on formatting/style and more on correctness, contract mismatches, edge cases,
and incomplete propagation.

### Issue classes to check

**1 - Contract drift**
- Docs describe behavior the code does not implement.
- Code accepts flags/options/fields not documented anywhere.
- Examples in docs no longer work.
- README/status tables claim something is implemented, deferred, unsupported, or safe when
  the code disagrees.
- Error codes, return codes, events, schema fields, or CLI commands are documented
  inconsistently across `SPEC.md`, `TOOLING.md`, `tools/README.md`, and the implementation.
- *fha-specific:* TOOLING §17 command table and `tools/README.md` flags table disagree with
  what `argparse` actually registers; a tool's exit-code table (0/1/2/3) doesn't match
  what the code returns in every path.

**2 - Symmetry gaps**
Look for one side of a pair being updated while the other side is missed:
- read ↔ write
- generate ↔ clean
- full rebuild (`build_index`) ↔ incremental upsert (`upsert_source`)
- write to FTS table ↔ delete from FTS table before rewrite
- check in `views.py` (W103/W110) ↔ matching check in `lint.py`
- profile files ↔ companion files (`person_profile_paths` ↔ `person_companion_paths`)
- outgoing links (`claim_links.rel`) ↔ incoming links (`claim_links` queried by `target_id`)
- parent record ↔ child records (deletion order)
- happy path ↔ fallback path (index present ↔ tree-scan fallback)
- `--fix` apply ↔ `--dry-run` preview

**3 - Stale or partial state**
- Cache/index can become stale but is still treated as authoritative.
- Incremental `upsert_source` does not do everything a full `build_index` does.
- Deleted or changed content leaves stale rows in `notes_fts`, `citations`, or
  `relationships`.
- Freshness checks (`newest_record_mtime`) ignore config or related files (e.g. `fha.yaml`
  itself, `places/places.yaml`).
- An existing-but-empty, corrupt, or old-schema `.sqlite` file is treated as valid
  (schema probe: `SELECT 1 FROM persons LIMIT 1` should gate all open-db calls).
- A partial upsert failure leaves the index in a misleading mid-delete state.

**4 - Unsafe mutations**
For every operation that writes, moves, deletes, renames, overwrites, or fixes data:
- `--dry-run` must not write, move, or delete anything.
- `--fix` (brackets, W110 folder renames, person file moves) must detect destination
  conflicts before writing.
- Permission/filesystem errors must affect exit code or final status - not silently swallowed.
- Partial success (some renames succeed, one fails) must be reported clearly.
- `fha process` rename of a documents-root file must record `original_filename` before
  renaming and roll back on any failure.
- `fha views clean` must only delete files with the GENERATED header; never touch
  profile, research, or manually authored files.
- Operations must be idempotent or explicitly reject unsafe repeat runs.

**5 - Missing validation**
Check absent, malformed, and misconfigured inputs:
- Missing files, missing IDs, missing config (`fha.yaml` absent or unparseable).
- Empty files, corrupt databases, invalid YAML/schema.
- `root_person` set but not found in the index - W110 should emit on `fha.yaml`, not crash.
- Unknown claim types, source types, or enum values.
- Bad CLI argument combinations (e.g. mutually exclusive flags).
- `None` vs absent keys in frontmatter dicts (`rec['meta'].get('x')` vs `rec['meta']['x']`).
- Empty `persons:` list on a claim, empty `roots:` in `fha.yaml`.
- Absolute paths in `roots:` mapping that don't exist on this machine.
- Duplicate IDs (E001) not caught before insertion.
- Cycles in `relationships` graph (BFS visited-set prevents infinite loops, but confirm).

**6 - False success**
Find places where the command exits 0 but did not do what the user requested:
- Invalid ID or missing record produces plausible-looking but empty output.
- Exception is caught and logged but exit code is still 0.
- A deferred feature (`--related` in milestone 2) emits a generic parser error instead of
  the documented clear deferral message.
- A warning-level issue should actually block the operation (e.g. stale index when `--fix`
  is about to rename folders based on it).
- `fha views clean` reports 0 files deleted when it silently skipped non-GENERATED files
  that looked like generated files.

**7 - Scope mismatches**
- `fha find --text` searches `notes_fts` but not `transcripts_fts`, or vice versa, when
  TOOLING says it covers both.
- The tree-scan fallback (`_find_by_scan`) searches fewer directories than the indexed path.
- A check handles person profile files but not companion files (or vice versa).
- FTS search degrades to regex scan but the regex pass covers different file types.
- Privacy/filtering rules applied to `fha find` are not applied to the scan fallback.
- `fha views sources-index` covers curated persons but not couple folders, or vice versa.

**8 - Path and config assumptions**
- `archive_root / 'documents'` or `archive_root / 'photos'` hardcoded instead of going
  through `resolve_path(alias, fha_config, archive_root)`.
- Relative-path logic that fails when the root is absolute or external.
- `p.relative_to(archive_root)` raises `ValueError` for files in an external root - needs
  try/except with fallback to absolute path or path-as-key.
- Zero-padding lost when folder prefixes are converted to `int` for comparison
  (`int('040')` → `40`; `str(40)` → `'40'` ≠ `'040'`). Use regex to capture digit strings
  as-is, not int conversion.
- Forward-slash normalization for Windows paths stored in `fha.yaml` or the index.

**9 - Duplicate and ordering bugs**
- `notes_fts` accumulates duplicate body rows when `upsert_source` does not `DELETE FROM
  notes_fts WHERE path=?` before calling `_index_source`.
- `citations` or `source_files` orphaned when parent `sources` row deleted first.
- Deletion order: `claim_persons`/`claim_links` → `claims` → `citations`/`notes_fts` →
  `sources`/`source_files`/`source_people`. Reversing leaves orphans because the parent
  subquery finds nothing.
- The same source or person processed twice through different code paths (full rebuild vs
  upsert) producing inconsistent rows.
- `INSERT OR IGNORE` deduplication that relies on a unique constraint that doesn't exist in
  the schema - check DDL.

**10 - Cross-layer propagation**
When a data contract changes, verify every consumer:
- CLI argument parser (`argparse` registration in `register()` and `_standalone_main()`)
- `TOOLING.md` and `tools/README.md` flags/codes tables
- `SPEC.md` Part IV requirements
- The SQLite schema (DDL in TOOLING §2)
- Full rebuild (`build_index`) and incremental upsert (`upsert_source`) paths
- `fha lint` checks (a new field that can drift should have a lint rule)
- `fha doctor` (a new freshness dependency should be checked there)
- `fha find` (a new record type should be locatable)
- `fha views clean` (a new generated file kind should be cleanable)
- Tests/fixtures (broken fixture per new E/W code)

**11 - Privacy and data-safety rules**
These are non-negotiable across all paths - verify each one is enforced consistently:
- `living: unknown` is treated as living (same as `true`) in all export, find, and display
  paths; stubs default to `unknown`.
- `restricted: true` sources never appear in any export or public-facing output.
- DNA sources (`source_type: dna`) always carry `restricted: true`; `--include-restricted`
  does NOT include DNA - only `--include-dna` does.
- Privacy rules applied to the indexed path must also be applied to the tree-scan fallback.
- AI-generated/suggested content must remain at `status: suggested` and never be treated
  as `accepted` without a human `reviewed:` date.
- Local absolute paths (e.g. photo root on a specific machine) must not appear in any
  exported output or committed file.

**12 - Tests that should exist but do not**
For each important finding, identify the missing regression test. Prefer tests that prove:
- The documented command/API example in TOOLING works end-to-end.
- The failure path (absent file, corrupt DB, missing ID) returns the right exit code.
- `--dry-run` produces zero filesystem mutations.
- Incremental `upsert_source` produces the same index state as a full rebuild for the
  same source.
- A stale, corrupt, or schema-empty `.sqlite` is handled (not crash, not silent success).
- Duplicate inputs (two upserts of the same source) do not create duplicate FTS or
  citation rows.
- A folder with a zero-padded Ahnentafel prefix (`040 Thomas…`) is correctly matched.
- Privacy: `living: unknown` persons are excluded from an export without `--include-living`.

**13 - User-recoverability**
The human reading tool output is a non-technical genealogist (AGENTS.md → "Who you serve").
Flag every place the output would leave him stuck:
- A Python traceback or raw exception text reaches the user instead of a plain cause +
  next step.
- A dead-end error: it says something failed but names no fix, no next command, no valid
  alternative.
- Jargon without a gloss: `invalid EDTF`, bare `source_type`, `anchor`, a Crockford ID, or a
  status value shown with no plain explanation, example, or valid-list.
- Fussy/hard failure on messy-but-recoverable input: the tool refuses or errors on loose
  natural-language dates/places or a slightly malformed hand-edit it could have inferred or
  resolved with one plain question.
- A new user-visible failure condition that `fha doctor` doesn't detect and explain.

---

### Output format

Produce the review as a structured report with these sections:

**1. Summary**
- P1/blocker count
- P2/important count
- Lower-priority count
- Documentation/contract drift count
- Missing-test count
- Overall merge risk: **Safe** / **Draft only** / **Do not push**

**2. P1/blockers**
For each issue:
- *Title*
- *File / function / area*
- *Why it matters*
- *Minimal scenario or reproduction*
- *Suggested fix*
- *Suggested regression test*

**3. P2/important issues**
Same format as P1.

**4. Contract drift**
List every mismatch between docs, code, CLI/API, schemas, and tests.

**5. Symmetry audit**
Explicitly state each symmetry pair checked and whether it passed or failed.

**6. Stale-state and incremental-update audit**
State whether full rebuild and incremental/upsert paths stay equivalent.

**7. Mutation-safety audit**
List every changed mutating operation and whether dry-run, conflict handling, errors, and exit status are safe.

**8. Missing tests**
Concrete test names or scenarios - not vague suggestions.

**9. Commands to run**
Exact lint/test/syntax/manual commands that should pass before pushing.

Be specific. Do not give vague advice. Do not praise the branch unless there are no material issues. Do not suggest broad rewrites unless needed for correctness.
