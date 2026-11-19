# BACKLOG.md

Deferred fixes in tools that have already shipped, and tool ideas not yet designed. None of the fixes is a regression: each **errs safe today**, which is why it was not fixed when found. Each is recorded with enough context (file references, the hazard and a proposed approach) to be picked up later.

This is a **repo-development tracking doc**, like `RELEASE_CHECKLIST.md`. It is deliberately *not* part of the operating layer (`_ROOT_OPERATING_DOCS` in `tools/scaffold.py`) and is never installed into a user's archive. `TOOLING.md` §14 points here for unbuilt ideas.

---

## 1. GEDCOM export silently drops accepted-but-unsourced vitals

**Where:** `tools/gedcom.py`, the `JOIN sources s ON s.id = c.source_id` in `_load_vitals` and in `_load_marriages`.

**Issue:** both queries inner-join `claims` to `sources`, so an `accepted` vital or marriage claim that carries **no `source_id`** is dropped from the export. An accepted claim is human-asserted truth (SPEC §21 derives the export from relationship and vital claims, not only sourced ones), so the export loses real facts. This is inconsistent with `fha site`, which publishes unsourced accepted facts. It *errs safe*: it under-exports and never leaks.

**Why deferred (privacy hazard in the naive fix):** switching to `LEFT JOIN` would also let through a fact whose **only** source is `restricted` or DNA. With the restricted source filtered out, that fact would look unsourced and be emitted, which is a privacy regression. The inner join is what keeps restricted-only facts out.

**Proposed approach:** distinguish "no source at all" from "only restricted sources." Emit a fact when it is either unsourced **or** has at least one publication-eligible source; suppress it only when every source it has is restricted, DNA or `publication_ok: false`. Add tests: an unsourced accepted vital is emitted; a vital sourced only from restricted material is **not** emitted; a mixed public and restricted vital is emitted with the public `SOUR`. Mirror the same logic for marriages.

**Severity:** low (data completeness, not correctness or privacy, as long as the fix keeps the restricted-only exclusion).

---

## 2. Place `within:` "settlement cannot nest" check over-flags legitimate hierarchies

**Where:** `tools/places.py`, `_lint_within_on_settlement` (see the module docstring: "a `within:` link whose source is itself a settlement … cannot also point [onward]").

**Issue:** the heuristic assumes a place that is already the *target* of some other place's `within:` link (i.e. it has children) cannot itself have a `within:` *parent*. Real multi-level geography breaks this: a city is both the target of `neighborhood → city` and the source of `city → county`. The check flags valid nesting.

**Why deferred (needs a schema change):** doing this correctly requires the place record to declare its level or kind (settlement, administrative region or micro-place), which `places.yaml` (SPEC §15) does not carry. Adding a `class:`/`kind:` field is a `LOCKED` spec amendment (a logged decision), not a tool-only fix.

**Proposed approach (pick one):**

- (a) Add a place `class:` field to SPEC §15 and validate containment by class (a settlement may sit `within:` a region; a region may not sit `within:` a settlement), or
- (b) relax the check to a **warning** until (a) lands, so legitimate hierarchies stop erroring.

**Severity:** low to medium (false errors on valid data; blocks deep place trees).

---

## 3. Write-path reads still use a guard that misses decode errors (#68)

**Where:** the `_lib.read_text_exact` call sites outside `tools/lint.py`, in `confirm.py`, `person.py`, `packet.py`, `process.py`, `source.py`, `claim.py`, `places.py`, `convert_mining.py`, `normalize_links.py`, `reconcile.py` and `serve.py` among others.

**Issue:** `read_text_exact` decodes UTF-8 strictly, and these sites sit inside a `try: ... except OSError:`, which does not catch `UnicodeDecodeError` (a **ValueError**). So a record or note saved in another codepage (cp1252, a Windows editor's default) raises past the refusal path of a *write* verb. The plainest case: `fha confirm discovery` (`run_add_discovery` in `tools/confirm.py`) appends to `notes/discoveries.md` by reading the whole log first, so one cp1252 byte anywhere in that log makes every later discovery entry fail with the raw codec text through `fha.py`'s catch-all ("something went wrong: 'utf-8' codec can't decode byte 0xf3 …", exit 3) and a "run `fha doctor`" line that does not name the file. `fha lint` W128 does name it, but nothing points the human there from the failure.

**Why it errs safe:** these are read-modify-write paths, and the read is what fails, so nothing is written, nothing is truncated and no private material leaks. The cost is a dead-end refusal, not lost data. That is why #68 is being closed one caller at a time (see `_lib.read_record`'s docstring) rather than by making that function hand every existing guard an empty record. `lint.py`, whose write paths skip an undecodable file rather than rewriting bytes they could not read, is the model.

**Proposed approach:** per tool, not repo-wide. A write verb already has a natural channel: refuse *this one file* with W128's plain cause and the re-save fix, leave the file untouched, and keep the tool's existing exit-code convention for a refusal. Never re-encode: the file is the human's, and it is not damaged, only saved in another encoding.

**Severity:** low (fails closed and loudly, no data loss), but it leaves a non-technical human with no next step, which is what `AGENTS_TOOLING.md` class 13 exists to catch.

---

## 4. Minor and informational

- **`fha confirm cooccur` hard-codes `confidence: medium`** for the minted relationship claim (`run_confirm_cooccur` in `tools/confirm.py`) instead of defaulting from the confirming source's `source_type` per SPEC §8.5. It is defensible for social ties (a relationship asserted from a record is reasonably "medium"), so this is a consistency nicety, not a bug. If addressed, derive the default from `source_type` (vital-record → high, census or newspaper → medium, interview → low) with `medium` as the fallback.
- **W101 vitals-gap does not accept proxies.** The completeness check reports a missing `birth`/`death` even when a `baptism`/`burial` claim exists for the person. Genealogically a baptism or burial is strong evidence of the vital; the check could treat them as partial satisfaction (or emit a softer note).

---

## 5. Deferred fixes

Each errs safe: it refuses, keeps something private, or leaves something out.

- **A damaged AI-draft marker is found only at publish time.** If a biography's `<!-- AI-DRAFT … -->` marker is broken (the closing `-->` missing, say), `fha site` and `fha packet` quietly leave that prose out and `fha wikitree` refuses, but nothing warned the owner earlier. Neither `fha lint` nor `fha doctor` checks the markers (`_lib.py` `strip_unaccepted_drafts` holds the grammar). *Approach:* a lint warning naming the file and the marker, using the same grammar. *Severity:* low.
- **`fha capture --asset` looks only in the current folder.** An owner who types a path as it sits inside the archive gets "file not found" (`capture.py`, the `--asset` resolution). `fha process` already retries relative to the archive root. *Approach:* the same retry. *Severity:* low.
- **The "is this restricted?" rule is copied into several tools** (`_is_restricted_value` in index, doctor, gedcom, site and wikitree; `_is_restricted` in lint; `_restricted_type` in packet and site; `_restricted_type_of` in process). The copies agree. If one drifts, a private record could leak from one export path. *Approach:* one function in `_lib`, plus a test that pins its true/false table. *Severity:* low, but privacy-class.
- **A withheld earlier name can still show in a packet's prose.** Packet redaction strips a restricted `name_variants` entry and its alias copies, but leaves body prose alone on purpose, so a link written `[[p-x|Old Name]]` still shows the private name. Low risk, since a packet is a private family export. *Approach:* rewrite the display text of links to that person, or name the leftovers in the packet README.

---

## 6. Unbuilt tool ideas

- **WikiTree importer.** Legacy profiles become draft sources, claim date and place hints, `external_ids` and profile scaffolds, all `suggested`.
- **Citation assistant.** Match uncited profile sentences to accepted claims and suggest `[[s-…]]` insertions as a diff.
- **Auto-anchor refinement** for `fha convert-mining` output.
- **Model-assisted photo triage scoring** for `fha photoindex triage`.
- **`fha process` vision pass.** Group photo variations whose filenames are unrelated (scan001/scan002 as front/back) by looking at the images.
- **`fha stubs` names from claims.** Derive a stub's name from claim values instead of `unknown__unknown`.
- **Accept an AI transcript.** A verb that flips a checked transcript's AI-DRAFT marker so it loses the `[unchecked AI transcript]` label.
- **`fha doctor --fixity`.** Optional checksum verification for bit rot, without making checksums part of the research model.

---

## 7. Feature ideas (not yet on the roadmap)

These are not fixes. Each stays here until it has a design section in `TOOLING.md`.

- **"Possible duplicate people" in `fha report`.** People coming from Ancestry expect duplicate flags. `fha confirm merge` exists, but nothing surfaces candidate pairs (similar name, compatible birth years, shared places), even though the merge-identities skill speaks of "the report flagged a possible duplicate".
- **Photo-tagging progress in the archive totals.** `fha metrics` reports people, sources, claims and searchable text, but not how many photos are still untagged ("18 photos untagged"), which would read the photo index.
- **`fha views canvas`.** A regenerable Obsidian `.canvas` (or Mermaid) family tree drawn from relationship claims, so an Obsidian user can see the tree without leaving the vault. `fha views tree` emits JSON or DOT only.
- **A generated family summary on the home page.** An at-a-glance panel built from the index each run, never hand-edited: how many people the archive holds, its surnames, the span of years it covers, the earliest ancestor on the main line, and a short list of recent discoveries.
- **More custom prose pages on the site.** `notes/home.md` is the one custom prose page. A `notes/narrative/` folder of "chapters" (a migration story, a war letter, a family-recipe page), with the site building a small table of contents across them, would use the same source-first model. It needs answers first: whether a chapter naming a living person is redacted, how chapters are ordered, and whether they appear in the standalone snapshot.
