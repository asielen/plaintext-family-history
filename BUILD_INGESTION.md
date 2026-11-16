# BUILD_INGESTION.md - intake: the capture engine and the inbox contract, build sequence

**Who this is for:** developers implementing the **intake on-ramp** - `fha capture`, its site recipes, the `--ingest` sweep, `--path`, and the native-messaging host backend. If you just want to use the archive, start with [`GETTING_STARTED.md`](GETTING_STARTED.md).

This file is the order in which to build the ingestion layer from nothing, with the acceptance test for each step. It is the sibling of [`BUILD.md`](BUILD.md) (core `fha` tools) and [`BUILD_INTERFACE.md`](BUILD_INTERFACE.md) (workflow skills and the workbench). Design rationale lives in [`TOOLING_INGESTION.md`](TOOLING_INGESTION.md); the law for what lands in the inbox is SPEC §12.1.

**What is in scope.** Two things must be rebuildable from the docs: the **engine** (`tools/capture.py` and `tools/capture_recipes/`) and the **inbox contract** - the shape of a source waiting in `inbox/`, which `fha process` reads, and the shape of a staged bundle, which `fha capture --ingest` reads. Any producer that writes those shapes is a valid front end. The browser companion in [`browser-companion/`](browser-companion/) is one such producer; it is documented in its own README and is not rebuilt from this file. What this file pins is the format it produces (MG0.2), so a different browser, a script, or a human with a text editor can produce the same thing.

**Tool conventions (all PRs):** identical to BUILD.md - one Python file per tool under `tools/`; tools never import tools (site recipes are plug-in data the engine discovers, not tools); shared code only in `tools/_lib.py`; engine/interface `Result` split; every mutating op ships `--dry-run`; exit codes 0/1/2/3. The four-part **UX bar** and the **`fha doctor` / `docs/TROUBLESHOOTING.md`** rule from BUILD.md bind every phase here too.

---

## Milestones

Phases use the **MG** series (this doc's own numbering).

| Milestone | Layer | Phases | What it builds |
|---|---|---|---|
| G0 | Layer G0 - The inbox contract | MG0.1-MG0.2 | The source stub and bundle folder `fha process` reads (MG0.1); the staged bundle `--ingest` reads (MG0.2) |
| G1 | Layer G1 - The capture engine | MG1.1-MG1.4 | `fha capture` paste path + generic recipe (MG1.1); Ancestry + FamilySearch recipes (MG1.2); Newspapers.com + FindAGrave recipes (MG1.3); `--path` (MG1.4) |
| G2 | Layer G2 - Staging and delivery | MG2.1-MG2.2 | `fha capture --ingest` (MG2.1); the native-messaging host backend `--host` / `--install-host` (MG2.2) |

**Dependencies.** Layer G0 is a format, not code: it is written first because both `fha process` (BUILD.md Layer 7) and `fha capture` are built against it. `fha capture` needs `_lib` and, for its research-log entry's `search_log` row, the index; it hands off to `fha process`. MG2.1 depends on MG1.1-MG1.3 (it runs the same engine), and MG2.2 on MG2.1 (it files through the same sweep).

---

## Layer G0 - The inbox contract (Milestone G0)

The shapes a source can take while it waits in `inbox/` for `fha process`. Pre-source: none of
them carries an s-id, and none decides `living` or `restricted` - processing does both.

---

### MG0.1 - The source stub and the bundle folder

**Defined by:** SPEC §12.1 ("Source stubs"), SPEC §14 (the fields a stub's hints pre-fill),
TOOLING_INGESTION §2.1, §2.3, §2.6.

A stub is a plain Markdown notes file, in one of two arrangements:

- **Lone sidecar** for a single asset: `photo.jpg` + `photo.notes.md` beside it, paired by
  basename. A sidecar with no asset at all is a pointer-only stub (below).
- **Bundle folder** the moment there is more than one file: `inbox/<name>/` holding the files
  and one `notes.md`. A recording with its transcript, a document with its translation, a photo
  with its back is always a bundle folder - the folder is the only thing grouping files that
  have no s-id yet. At processing the folder dissolves into one source (BUILD.md M7.4); an
  interview session's folder is kept in `documents/interviews/{interviewee}-{yyyy-mm-dd}/`.

The notes file is **freeform first**: the body is whatever the human wants to say, and it becomes
the record's `## Notes`. A *light, optional* frontmatter holds whatever structured hints exist;
every key is optional, and a hand-written stub with no frontmatter at all is valid:

```yaml
---
title: "1880 United States Federal Census - Thomas Hartley"
source_type: census                  # SPEC §14 vocabulary
source_date: "1880"                  # EDTF or loose wording
source_class: derivative             # original | derivative | authored, only when the page says so
citation: "…, accessed 2026-06-24"
repository: "Ancestry.com"
external_links:
  - url: "https://www.ancestry.com/…"
    accessed: "2026-06-24"
people: ["Thomas Hartley", "Margaret Hartley"]   # NAMES, never p-ids - a hint processing resolves
asset_elsewhere: true                # pointer-only: no companion file, on purpose
asset_path: E:/family-photos/x.jpg   # with --path: where the file that must not move lives
asset_path_absolute: E:/family-photos/x.jpg
---
Bob's great-grandfather's household. The boy listed as 'Calvin' is Cal.
```

Rules a producer must follow:
- **Pairing is by basename.** `{slug}.notes.md` pairs with `{slug}.{ext}`; a name collision is
  resolved before anything is written, never by overwriting.
- **No s-id, no p-id.** `people:` carries names; resolution is the processing pass's job.
- **Pointer-only is explicit.** A stub with no companion file must say `asset_elsewhere: true`;
  without it, `fha process` reads the missing file as an error, not a choice. `asset_path` (as
  typed) and `asset_path_absolute` (resolved, forward slashes) say where a file that cannot be
  moved lives; `fha process` folds `asset_path` into the record's `provenance:` and never
  writes the machine-specific absolute form.
- **No local absolute paths elsewhere.** Everything but `asset_path_absolute` uses alias-form
  paths or plain names.

**Done when:**
```sh
# a hand-made sidecar, a bundle folder and a pointer-only stub in a scratch archive's inbox/
fha process <scratch-archive>/inbox/letter.notes.md --dry-run --root <scratch-archive>   # pre-fills from the hints
fha process <scratch-archive>/inbox/reunion/ --dry-run --root <scratch-archive>          # one record from the bundle
python -m unittest tests.test_process   # incl. test_sidecar_pointer_only_accepts_capture_path_stub
```

---

### MG0.2 - The staged bundle (`capture.json`)

**Defined by:** TOOLING_INGESTION §3 (the contract), §6 (how ingest reads it).

The hand-off from any capture front end to `fha capture --ingest`. A staged bundle is a folder
in the staging directory (`fha.yaml` `capture_staging:`, else `~/Downloads/fha-inbox/`):

```
<slug>-<timestamp>-<token>/
  page.html        ← the raw captured DOM (always present; the recipes extract from it)
  <asset files>    ← zero or more, each listed in capture.json `assets:`
  capture.json     ← the human's inputs + light hints
```

`page.html` is the clean DOM the recipes read; a preservation copy of the page (a single-file
HTML with its images inlined) is a separate asset, because a heavily inlined snapshot is for the
human's eyes and re-parses badly. `capture.json` (schema 2):

```json
{
  "schema": 2,
  "url": "https://www.ancestry.com/...",
  "title": "1880 United States Federal Census - Thomas Hartley",
  "accessed": "2026-06-24",
  "source_date": "1880",
  "source_type": "census",
  "repository": "Ancestry.com",
  "citation": "…",
  "source_class": "derivative",
  "assets": [
    { "file": "record.jpg", "role": "record", "mode": "manual" },
    { "file": "page-snapshot.html", "role": "webpage", "mode": "singlefile", "provisional": false }
  ],
  "people": ["Thomas Hartley", "Margaret Hartley"],
  "notes": "Bob's great-grandfather's household.",
  "recipe_hint": "ancestry"
}
```

- Every field except `url` is optional. `notes` becomes the stub body; `people` is additive to
  the names the recipe finds; `repository`, `citation` and `source_class` (one of the three SPEC
  §14 words, else ignored) win over the recipe's guess; `recipe_hint` is only a hint - the engine
  re-detects on `page.html`.
- `assets` is ordered (record first). A listed file missing on disk makes the bundle malformed:
  it is reported and left in staging, never filed incomplete. An empty list is a pointer-only
  capture and files as an `asset_elsewhere: true` stub. `provisional: true` marks a
  capture-what's-shown stand-in (a screenshot) for an original that could not be fetched.
- The flat schema 1 shape (`asset_mode` + `asset_file`, one asset) is still read, for hand-made
  bundles.
- **`schema` versioning is forgiving:** absent reads as current; newer is read for the fields it
  shares, with a one-line "run `fha update-tools`" warning; never refused. Bump it
  (`capture._CAPTURE_JSON_SCHEMA`) only on an incompatible shape change.

**Done when:**
```sh
fha capture --ingest browser-companion/test-bundle --dry-run --root <scratch-archive>   # names the stub it would write
python -m unittest tests.test_capture_ingest tests.test_browser_companion   # round-trips the committed example bundle
```

---

## Layer G1 - The capture engine (Milestone G1)

The deterministic backend and its site recipes - the single source of truth for what a capture
becomes.

---

### MG1.1 - `fha capture` - paste path + generic recipe

**One PR.** New file `tools/capture.py`. Wire `fha capture [--url URL] [--title "…"]
[--type TYPE] [--date DATE] [--asset FILE] [--dry-run]` (TOOLING_INGESTION §2.1, §2.5).

Read HTML from stdin or `--asset`. Generic recipe: title (from `<title>` or `<h1>`), URL (from
`<base href>`, `<link rel="canonical">` or `--url`), accessed date (today), and ~2000 chars of
visible text. Explicit `--title`/`--type`/`--date` always override the scrape; `--type` is
validated against the `source_type` vocabulary. Write a MG0.1 stub into `inbox/` as
`{slug}.notes.md`, with any `--asset` staged beside it under the same basename. Capture reads
the open page only: it never logs in, paginates, or fetches on its own (TOOLING_INGESTION §2.4,
§7).

**The research-log entry.** A capture is a logged search: append one entry to
`notes/research-log.md` in the SPEC §17c shape (`date`, `question`, `repository`, `collection`,
`terms`, `result`), with `result: staged <inbox path>` naming the stub it just wrote (the
capture is staged, not yet processed), and add the `search_log` row when the index exists, so `fha report`'s
"already searched" annotation sees it at once. The log is durable, under `notes/`, never
`.cache/`.

The modes of `fha capture` (page capture, `--path`, `--ingest`, `--host`, `--install-host`) are
mutually exclusive; mixing flags from two modes is a hard error naming both. Exit: 0 clean ·
2 user error (bad `--type`/`--date`, asset name clash, mixed modes) · 3 filesystem failure.

**Done when:**
```sh
echo "<html><title>Test</title></html>" | fha capture --url "https://example.com" --root <scratch-archive>
# stub in inbox/ with the MG0.1 frontmatter; one research-log entry
fha capture --url "…" --title "Override" --type newspaper --dry-run --root <scratch-archive>
python -m unittest tests.test_capture
```

---

### MG1.2 - Site recipes: Ancestry + FamilySearch

**One PR.** Create `tools/capture_recipes/`, one module per site, each exposing
`detect(html, url) -> bool`, `extract(html, url) -> dict` and a `PRIORITY`. The engine discovers
the modules at runtime, sorts by `PRIORITY`, and runs the first whose `detect()` matches; a
broken or incomplete recipe is skipped with a warning and the page still captures generically
(TOOLING_INGESTION §2.2).

Minimum extraction: Ancestry (collection title, date, persons in the household/index table,
image URL, record vs image-viewer page); FamilySearch (title, date, collection, persons from the
fact table, tree-person page).

Add anonymized HTML samples to `tests/fixtures/capture-samples/`.

**Done when:**
```sh
fha capture --root <scratch-archive> --url "https://www.ancestry.com/…" < tests/fixtures/capture-samples/ancestry.html
fha capture --root <scratch-archive> --url "https://www.familysearch.org/…" < tests/fixtures/capture-samples/familysearch.html
# each recipe: detect() True for its own samples, False for the other's
python -m unittest tests.test_capture tests.test_capture_host_lookups
```

---

### MG1.3 - Site recipes: Newspapers.com + FindAGrave

**One PR.** Two more recipes in `tools/capture_recipes/`. Minimum extraction: Newspapers.com
(publication, date, page, article snippet, the site's own formatted citation); FindAGrave
(memorial name, birth/death, cemetery as `place_text`, persons). The generic recipe stays the
floor under all four.

**Done when:**
```sh
fha capture --root <scratch-archive> --url "https://www.newspapers.com/…" < tests/fixtures/capture-samples/newspapers.html
fha capture --root <scratch-archive> --url "https://www.findagrave.com/…" < tests/fixtures/capture-samples/findagrave.html
# each of the four recipes: detect() True for its own samples, False for the other three
python -m unittest tests.test_capture
```

---

### MG1.4 - `fha capture --path` - register a file that must never move

**One PR.** Extend `tools/capture.py`. Wire `fha capture --path PATH [--note TEXT]
[--title TEXT] [--dry-run]` (TOOLING_INGESTION §2.6).

Registers a file that can never be copied, moved or renamed (a photo in a relative's own
library, a document in someone else's folder) as a pointer-only stub: `asset_elsewhere: true`
plus `asset_path` and `asset_path_absolute` (MG0.1). Reads no HTML and stages no copy; the
target is only checked for existence, and a missing one (an unplugged drive) still writes the
stub with a warning. The slug comes from the target's own stem. `fha process` reads the stub
(BUILD.md M7.1).

**Done when:**
```sh
fha capture --path "E:/family-photos/grandma-wedding.jpg" --note "Aunt Mary's album" --dry-run --root <scratch-archive>
python -m unittest tests.test_capture   # CapturePathTestCase
```

---

## Layer G2 - Staging and delivery (Milestone G2)

The backend modes that turn MG0.2 bundles into MG0.1 stubs.

---

### MG2.1 - `fha capture --ingest` - the staged-bundle sweep

**One PR.** Extend `tools/capture.py` with a mode (not a new tool): `fha capture --ingest [DIR]
[--dry-run]` (TOOLING_INGESTION §6).

Resolve `DIR` (argument → `fha.yaml` `capture_staging:` → `~/Downloads/fha-inbox`). For each
`<slug>-<timestamp>-<token>/` bundle: read and validate it (MG0.2), then run `run_capture`
wholesale - `page.html` as the HTML, the assets as the staged files, and the `capture.json`
fields as explicit overrides - so an ingested stub is byte-identical to the paste path's. On
success the bundle is **parked** in `.ingested/` (moved, never deleted). Idempotent (a name
already in `.ingested/` is skipped); a malformed bundle is reported and left in place, never
aborting its siblings; `--dry-run` writes nothing. Writes only to `inbox/`, so it works in
working-copy mode.

Two companions in the same PR: the `fha doctor` nudge (`staged captures: N … next: run
\`fha capture --ingest\``, through a shared `capture.staged_bundles(fha_config)` helper, silent
when the staging folder does not exist), and the `capture.json` schema check.

**Done when:**
```sh
fha capture --ingest browser-companion/test-bundle --dry-run --root <scratch-archive>   # plan only
fha capture --ingest <staging-dir> --root <scratch-archive>   # stubs in inbox/, bundles parked; re-run is a no-op
python -m unittest tests.test_capture_ingest tests.test_doctor
```

---

### MG2.2 - The native-messaging host backend

**One PR.** Extend `tools/capture.py` with `fha capture --host` (the stdin/stdout server a
browser speaks to) and `fha capture --install-host [--extension-id ID] [--browser NAME]
[--host-manifest-dir DIR] [--dry-run]` (writes the OS-level native-messaging host manifest)
(TOOLING_INGESTION §5.7). It lets a front end file a bundle straight into `inbox/` without the
download-then-sweep step; the bundle contract is unchanged.

Protocol: length-prefixed (4-byte little-endian) JSON frames. A framed bundle is filed by
delegating to `run_ingest`, so a host capture and an `--ingest` sweep produce byte-identical
stubs. Two read-only helpers: `suggestNames` (person name and alias candidates) and `checkUrl`
(has this URL been captured before?). Both read the record files themselves, not the index,
through per-file caches keyed on size and mtime (`.cache/capture_links.json`,
`.cache/capture_names.json`). The host writes records only to `inbox/`, so it honors
working-copy mode. A front end that finds no host answering falls back to MG2.1.

**Done when:**
```sh
fha capture --install-host --dry-run --root <scratch-archive>   # previews the manifest path; writes nothing
python -m unittest tests.test_capture_host tests.test_capture_host_lookups   # framed bundle == --ingest output
```

---

## Testing invariants (all PRs)

Same as BUILD.md: every PR must leave `fha lint --root example-archive` exiting 1 with only the
documented baseline warnings (TOOLING.md §15) - no new errors or warnings. Every mutating path
ships `--dry-run`. Every flag and exit code is documented in `tools/README.md` in the same PR.
The four-part UX bar (no traceback reaches the user; every error names cause + fix; jargon ships
an example; messy-but-recoverable input is inferred or asked, never hard-failed) is required
before closing.
