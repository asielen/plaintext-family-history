# BUILD_INGESTION.md - intake pipeline (capture + browser companion): build sequence

**Who this is for:** developers implementing the **intake on-ramp** - `fha capture`, its recipes, the `--ingest` sweep, the browser companion, and the native-messaging host. If you just want to use the archive, start with [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

This file is the build guide for the **ingestion layer** - the capture engine and every front-end that feeds it. It is the sibling of [`BUILD.md`](BUILD.md) (core `fha` tools) and [`BUILD_INTERFACE.md`](BUILD_INTERFACE.md) (workbench skills). Design rationale lives in [`TOOLING_INGESTION.md`](TOOLING_INGESTION.md); this file tells you the sequence and how to verify it.

**Status: shipped.** Every milestone below is ✓ shipped (the native-host front-end activation in MG2.3 is the one deferred step, noted inline).

**Tool conventions (all PRs):** identical to BUILD.md - one Python file per tool under `tools/`; tools never import tools; shared code only in `tools/_lib.py`; engine/interface `Result` split; every mutating op ships `--dry-run`; exit codes 0/1/2/3; `tools/README.md` is the authoritative status record. The four-part **UX bar** and the **`fha doctor` / `docs/TROUBLESHOOTING.md`** rule from BUILD.md bind every phase here too.

---

## Milestones

Phases use the **MG** series (this doc's own numbering), grouped into two layers.

| Milestone | Layer | Phases | Status |
|---|---|---|---|
| G1 | Layer G1 - The capture engine | MG1.1-MG1.3 | ✓ shipped |
| G2 | Layer G2 - Staging, delivery & the seamless path | MG2.1-MG2.3 | ✓ shipped (MG2.3 backend shipped; extension-side activation deferred) |

**Dependencies.** `fha capture` depends on the index and hands off to `fha process` (BUILD.md, Layer 7). The browser companion (MG2.2) depends only on the `fha capture --ingest` backend contract (MG2.1). Build order follows the spine's own logic (TOOLING_INGESTION.md §8): the engine first, then `--ingest` (it makes any staged bundle useful and is a thin wrapper over the existing engine), then the extension (which produces those bundles), then the native host as a seamless-path upgrade.

---

## Layer G1 - The capture engine (Milestone G1 - ✓ shipped)

The deterministic backend and its site recipes - the single source of truth for what a capture becomes.

---

### MG1.1 - `fha capture` - paste fallback + generic recipe (✓ shipped)

**One PR.** New file `tools/capture.py`. Wire `fha capture [--url URL] [--title "…"]
[--type TYPE] [--date DATE] [--asset FILE]` (TOOLING_INGESTION §2).

Read HTML from stdin or `--asset`. Generic recipe: extract title (from `<title>` or `<h1>`),
URL (from `<base href>` or `<link rel="canonical">` or `--url`), accessed-date (today).
Create source stub in `inbox/` as `{slug}.notes.md`:
```yaml
title: "…"
source_type: website
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

### MG1.2 - `fha capture` - site recipes: Ancestry + FamilySearch (✓ shipped)

**One PR.** Create `tools/capture_recipes/` directory with the two genealogy-database
recipes. Each recipe exposes `detect(html, url) -> bool` and `extract(html, url) -> dict`.
`fha capture` tries recipes in priority order: Ancestry → FamilySearch → generic fallback
(Newspapers.com and FindAGrave are added by MG1.3, ahead of the fallback).

Minimum extraction per site (TOOLING_INGESTION §2.2): Ancestry (collection title, date, persons in
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

### MG1.3 - `fha capture` - site recipes: Newspapers.com + FindAGrave (✓ shipped)

**One PR.** Extend `tools/capture_recipes/` with the two remaining recipes, inserted into
the priority order ahead of the generic fallback: Ancestry → FamilySearch → Newspapers.com
→ FindAGrave → generic fallback.

Minimum extraction per site (TOOLING_INGESTION §2.2): Newspapers.com (publication, date, page, article
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

## Layer G2 - Staging, delivery & the seamless path (Milestone G2 - ✓ shipped)

The front-ends that feed the engine: the local bundle sweep, the browser extension that
produces bundles, and the native host that files them without a manual step.

---

### MG2.1 - `fha capture --ingest` - the staged-bundle sweep (✓ shipped)

**One PR.** Extend `tools/capture.py` with a new mode (not a new tool): `fha capture
--ingest [DIR] [--dry-run]` (TOOLING_INGESTION §6). This is the local bridge from a
browser-staged bundle to a real inbox stub - the prerequisite for the browser companion
(§5), whose only output is such a bundle.

Algorithm: resolve `DIR` (explicit arg → `fha.yaml` `capture_staging:` → default
`~/Downloads/fha-inbox`). For each `<slug>-<timestamp>/` bundle (`page.html` + optional
`asset.*` + `capture.json`, §3): read + validate it, then run `run_capture` wholesale -
`page.html` as the HTML, the asset as `--asset`, and the `capture.json` fields as explicit
overrides (`url`/`title`/`type`/`date`/`accessed`/`notes`/`people`). `run_capture` gained
optional `accessed`/`notes`/`people` params (inert when unset, so the paste path stays
byte-identical), so the ingested stub equals the paste-fallback's exactly. On success the
bundle is **parked** in `.ingested/` (moved, never hard-deleted). Idempotent (a name already
in `.ingested/` is skipped); resilient (a malformed bundle - missing `page.html`, bad
`capture.json` - is reported and left in place, never aborting siblings); `--dry-run` writes
nothing. Writes only to `inbox/`, so it stays available in WORKING_COPY mode. Pointer-only
(`asset_mode: none`) bundles flow through with no asset → the stub's existing
`asset_elsewhere: true` path fires (case (c)).

Two companions land in the same PR:
- **`fha doctor` nudge.** Since nothing sweeps automatically, `doctor` warns when bundles sit
  waiting (`staged captures: N … next: run \`fha capture --ingest\``), mirroring inbox aging -
  via a shared `capture.staged_bundles(fha_config)` helper, and only when the staging folder
  exists (silent on machines that never run the companion).
- **`capture.json` schema version.** A `schema:` field (current `capture._CAPTURE_JSON_SCHEMA`
  = 1) lets the companion and backend evolve independently; ingest is forgiving - absent = current,
  newer = read shared fields + warn, never refused.

Add `tests/test_capture_ingest.py` (builds bundles in a temp staging dir from the existing
`capture-samples/*.html`); cover the doctor nudge and the newer-schema warning too.

**Done when:**
```sh
fha capture --ingest tests/fixtures/capture-staging/clean --dry-run  # plan only, no writes
fha capture --ingest <staging-dir>                                   # stubs in inbox/, bundles parked
# ingested stub is byte-identical to the paste-fallback's; re-run is a no-op
```

---

### MG2.2 - Browser companion - the capture extension (✓ shipped, core only)

**One PR.** A Manifest V3 browser extension in [`browser-companion/`](browser-companion/)
(TOOLING_INGESTION §5), the everyday front-end that produces the staged bundles MG2.1's
`--ingest` already consumes. It lives **outside** the Python tool suite and the archive
operating layer - installed in the browser, not vendored by `fha install`, so it is **not**
a `manifest.json` entry. No new Python; the backend contract was finished in MG2.1.

Scope is the **core** extension only:
- The four-phase side panel (§5.3): Invoke → Confirm (generic pre-fill, editable) → Capture
  the evidence (the five asset modes: fetch / single-file / pdf-via-handoff / manual / none)
  → Stage (write the bundle, never mint a record).
- Generic in-browser pre-fill only (§5.5): `<title>`/`og:title`, canonical URL,
  `article:published_time`, JSON-LD Person names, largest image, hostname → `recipe_hint`.
  The authoritative per-site parsing stays in the Python recipes, which re-run on the saved
  `page.html` at ingest. The browser captures; Python extracts.
- The §5.1 transport: assemble `page.html` + optional `asset.<ext>` + `capture.json`
  (schema 1, §3) in memory, write them via `chrome.downloads.download()` into
  `Downloads/fha-inbox/<slug>-<timestamp>/`. The panel reports exactly where the bundle went
  and that `fha capture --ingest` files it - it never pretends Downloads is the archive.
- A minimal single-file inliner (§9): images + stylesheet text inlined, scripts dropped,
  bounded; `page.html` is always saved alongside so scraping never depends on the snapshot.
- A provisional screenshot is surfaced as a `notes` line, since schema 1 has no field for it
  (§5.6). `sidePanel` is added to the §5.4 least-privilege set for the panel UX.

One piece is **explicitly out of this milestone, as a separate layer:** first-class
`asset_provisional` metadata - a `capture.json` schema 2 field + ingest plumbing, deferred
(§9). The seamless native-host path is its own phase (MG2.3), not part of the core extension.

Add `tests/test_browser_companion.py`: validate the MV3 manifest + that every file it names
exists, and round-trip the committed `browser-companion/test-bundle/` (built in the exact
shape the panel writes) through `fha capture --ingest` to prove the output contract against
the live backend. (No browser-driven harness in this repo; the JS runs only in a browser.)

**Done when:**
```sh
python -m unittest tests.test_browser_companion -v   # manifest valid; example bundle ingests clean
# load-unpacked in Chrome → capture a record → fha capture --ingest --dry-run sees the bundle
```

---

### MG2.3 - Native-messaging host - the seamless "straight into inbox/" path (✓ shipped, backend)

**One PR.** Extend `tools/capture.py` with the native-messaging host backend: `fha capture
--host` (the length-prefixed stdin/stdout JSON server Chrome speaks to) and `fha capture
--install-host` (writes the OS-level native-messaging host manifest that registers it)
(TOOLING_INGESTION §5.7). This upgrades MG2.2's download-then-sweep flow into a one-click
"file straight into my archive" path, without changing the bundle contract.

Protocol: length-prefixed (4-byte little-endian) JSON frames on stdin/stdout. The host files
a framed bundle into the configured `inbox/` by delegating to `run_ingest` (so a native-host
capture and a `--ingest` sweep produce byte-identical stubs), plus two read-only helpers the
panel can call live: `suggestNames` (index-backed name candidates) and `checkUrl` (has this
source been captured before?). The host writes only to `inbox/`, so it honors WORKING_COPY
mode like every other capture path.

**Off by default, opt-in.** The extension front-end that drives the host - the `nativeMessaging`
permission request and the `isAvailable` gate behind the "file straight into my archive"
toggle - stays OFF until the human enables it; when no host answers, the extension silently
falls back to the MG2.2 download path. That extension-side activation is the **one deferred
step**; the Python backend and its manifest installer ship complete.

Add `tests/test_capture_host.py`: frame a bundle through the host and assert the resulting
stub matches the `--ingest` output; cover `suggestNames`/`checkUrl` and the `--install-host`
manifest write.

**Done when:**
```sh
fha capture --install-host --dry-run   # previews the native-messaging manifest path; writes nothing
python -m unittest tests.test_capture_host -v   # framed bundle files into inbox/ == --ingest output
```

---

## Testing invariants (all PRs)

Same as BUILD.md: every PR must leave `fha lint --root example-archive` exiting 1 with exactly
the documented W101 - no new errors or warnings. Every mutating path ships `--dry-run`.
`tools/README.md` is the authoritative implementation-status record; update the relevant rows
before closing any PR. The four-part UX bar (no traceback reaches the user; every error names
cause + fix; jargon ships an example; messy-but-recoverable input is inferred or asked, never
hard-failed) is required before closing.
