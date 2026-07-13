# TOOLING_INGESTION.md - Research, Capture & the Inbox On-Ramp

**Who this is for:** developers building or extending the *intake* side of the `fha` tool suite - the path that turns research a genealogist is doing on the open web into staged material the archive can process. If you just want to use the archive, start with [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

**Version 1.2 - companion to SPEC.md v1.2 and TOOLING.md v1.2 (versions track the SPEC).**

This document is a focused expansion of one tool family that the main [`TOOLING.md`](TOOLING.md) only sketches: the **capture / inbox / ingestion** on-ramp. SPEC.md Part III (§12.1) states *what* a source stub is and how assets and records separate; TOOLING.md §13b names the `fha capture` engine; this document specifies *how* the whole intake spine works end to end - the engine, every delivery form that feeds it, and the browser companion in enough detail to build it in any language.

It is one of three sibling design docs, each with its own build doc: [`TOOLING.md`](TOOLING.md) (core tools) → [`BUILD.md`](BUILD.md); **this doc** (ingestion) → [`BUILD_INGESTION.md`](BUILD_INGESTION.md); [`TOOLING_INTERFACE.md`](TOOLING_INTERFACE.md) (workbench + skills) → [`BUILD_INTERFACE.md`](BUILD_INTERFACE.md).

The governing rule carried from TOOLING.md applies here without exception: **tools have no daemons, watchers, or schedulers.** Freshness is refresh-on-use. Capture stages material; nothing sweeps it automatically; a human (or a workbench skill) runs the next step. The browser companion is a *dumb, replaceable front-end* - it stages raw material, and the durable Python tooling decides what that material means.

---

## 1. Where intake sits in the loop

The archive's operating spine is **capture → file → process** (SPEC §4). Intake is the first two steps:

| State | Where it lives | What it is | Who makes it |
|---|---|---|---|
| **Research** | the open web (Ancestry, FamilySearch, Newspapers.com, FindAGrave, a county site) | a record the human is looking at, behind their own login | the human, mid-search |
| **Source stub** | `inbox/` (SPEC §12.1) | an asset (or none) + a freeform `*.notes.md` sidecar with rough notes and any structured hints a recipe recovered | hand, **or capture** |
| **Processed source** | `sources/…` + filed asset | a real `{slug}_{S-id}.md` record with `suggested` claims drafted from the stub | `fha process` + the `process-source` skill |

Intake's job is to get a research find safely into the inbox **as a stub**, fast, without forcing a full review session mid-browse. Everything judgmental - minting the S-id, resolving people against the index, drafting claims - is deferred to processing, where the index is at hand. The principle, stated once and obeyed everywhere below:

> **Fast, forgiving capture now; structured review later.** Grab everything while the page is open; defer every decision that can wait.

This is why the inbox stub is *pre-source*: no S-id, no accepted claims, light optional frontmatter over freeform prose (SPEC §12.1). The same stub format is produced whether a human drops a scan and types a note, or the browser companion captures a census page - the only difference is who typed the notes.

---

## 2. The engine: `fha capture` (already built)

The deterministic backend is [`tools/capture.py`](tools/capture.py). It is the single source of truth for *what a capture becomes*; every delivery form in §4 is a thinner or fatter front-end onto this one engine. It is built and shipping today as the **paste-fallback** path (§4.1).

### 2.1 What it does

`fha capture` reads page HTML the human already has in front of them and writes a **source stub** into `inbox/` - never a finished record.

```
fha capture [--url URL] [--title "…"] [--type TYPE] [--date DATE] [--asset FILE] [--dry-run]
```

HTML arrives on **stdin** (`… | fha capture`) or from an `--asset` that is itself a saved page. The engine:

1. **Chooses a recipe.** Site recipes (§2.2) are tried in priority order; the first whose `detect()` matches wins; an unknown site falls to the **generic recipe** (page title, canonical URL, accessed-date, and ~2000 chars of visible text as the citation basis). Any page is capturable.
2. **Extracts citation fields** - title, `source_type`, `citation`, `repository`, `source_date`, `external_links`, and the **person names the page lists**. Explicit `--title`/`--type`/`--date` always override the scrape (the human's word wins); `--type` is validated against the controlled `source_type` vocabulary so a typo surfaces now, not as an unprocessable stub later.
3. **Renders the stub** - a `{slug}.notes.md` whose *light, optional* frontmatter carries the recovered citation fields and whose body is the visible text or the human's notes. `people:` here is a list of **names** (a hint the processing pass reconciles against the index), never resolved `P-id`s - a stub has none yet.
4. **Stages the asset** beside the stub, sharing the stub's basename so they pair (SPEC §12.1 lone-sidecar rule). Collisions are detected before any overwrite.
5. **Writes a research-log entry** - capture is itself a logged search (SPEC §16), so `fha report`'s "already searched here" annotation sees it immediately: a `search_log` row when the index exists, also appended to `.cache/capture_log.jsonl` as an ephemeral write-behind supplement (the `search_log` row is the durable record; the cache file is disposable).

### 2.2 Recipes are data, not code

Recipes live in [`tools/capture_recipes/`](tools/capture_recipes/) - one module per site, each exposing `detect(html, url)` and `extract(html, url)`, discovered at runtime and sorted by `PRIORITY`. They are **plug-in data** for capture (extensible without touching the engine), which is why importing them does not breach the tools-never-import-tools rule. A broken or interface-incomplete recipe is skipped with a warning, never aborting the capture - the page still captures generically.

Current coverage, in priority order: **Ancestry** (record + image viewer, census household tables), **Newspapers.com** (clipping + citation), **FamilySearch** (record + tree person), **FindAGrave** (memorial + cemetery place). The generic recipe is the floor under all of them.

### 2.3 The three asset cases

Capture resolves the asset in exactly three ways (the cases the rest of this doc refers to as **(a)/(b)/(c)**):

- **(a) Downloadable** - a real image/document the human can save (or the companion can fetch) becomes the source's asset.
- **(b) Page-is-the-record** - an obituary, a wiki bio, a memorial page: store a **self-contained preservation asset** in `documents/web/…` (an acceptable second-tier format per SPEC §2). A bare `outerHTML` dump is *not enough* - it still references images, CSS, and fonts by URL, so once the page rots the "evidence" is a skeleton of broken links. The asset must therefore inline its resources: a **single-file HTML** (images/CSS/fonts rewritten to embedded data-URIs) is the default - viewable offline forever and *still text-scrapeable* - with **print-to-PDF** the alternative when a faithful rendered page matters more than re-parsing it. Either way the raw DOM is *also* saved separately as the scrape source (`page.html`, §3), because a PDF and a heavily-inlined snapshot are for the human's eyes; the recipe extracts from the clean DOM.
- **(c) Pointer-only** - the page merely says "record held at the county courthouse": no asset, citation + `external_links` only, written with `asset_elsewhere: true` so `fha process` knows the missing companion is deliberate (the explicit flag it requires before minting a no-asset record), and flagged for a later retrieval pass.

A fourth, practical sub-case of (a) lives in the companion: **on-screen but not downloadable** (the common Ancestry viewer image you cannot right-click). The companion offers *capture-what's-shown* - a screenshot/snapshot saved as a `provisional-image`, honest that a cleaner original may exist behind the paywall, but never letting the human leave empty-handed.

### 2.4 Boundaries (non-negotiable)

Capture reads the **open DOM/HTML only**. It does not log in, paginate, query APIs, or fetch behind auth on its own; it sees only what the browser is already showing the human in their own session. Two scoped exceptions ride the human's own Capture click on the page in front of them: on an Ancestry image-viewer page the companion calls Ancestry's own image-download endpoint in the human's session - the same single request Ancestry's Download button makes; never bulk, never uninvited, never on any other site - and a page whose image is served over the open IIIF standard has that one full-size image fetched without credentials. Bulk or automated retrieval against a site's terms is out of scope **by design** - this is a tool for filing the record you are already looking at, not a scraper. Everything it produces enters at `suggested` / needs-review like any intake; no claim is accepted without a human `reviewed:` date.

### 2.5 Flags & exit codes

| Flag | Meaning |
|---|---|
| `--url URL` | the record page URL (added to `external_links`) |
| `--title "…"` | override the captured title |
| `--type TYPE` | override the inferred `source_type` (controlled vocabulary) |
| `--date DATE` | override the source's own date (EDTF or loose natural language) |
| `--asset FILE` | an image/document to stage, or the saved page HTML to read |
| `--path PATH` | register a file that must stay exactly where it is - see §2.6 |
| `--note TEXT` | a note to record with `--path` (goes in the stub body) |
| `--ingest [DIR]` | sweep staged companion bundles from `DIR` (default: `fha.yaml` `capture_staging:`, else `~/Downloads/fha-inbox`) into the inbox (§6) |
| `--dry-run` | preview every write without touching disk |

The modes are mutually exclusive: `--install-host`, `--host`, `--ingest`, `--path`, and the default page capture (`--url`/`--title`/`--type`/`--date`/`--asset`) each run alone. Mixing flags across two modes (e.g. `--ingest --url …`) is a hard error naming both sides, not a silent drop of the losing mode's flags. (`--browser` belongs to `--install-host`; `--dry-run` is cross-mode; `--title` is shared between page capture and `--path`, since both use it to label the stub.)

Exit: `0` clean · `2` user error (bad `--type`/`--date`, asset destination clash, mixing mode flags) · `3` filesystem failure while staging. Tracebacks never reach the user; every error names a cause and the next command.

### 2.6 `--path` - register a file that must never move (plan 17)

A fourth mode, unrelated to web pages: `fha capture --path PATH [--note TEXT] [--title TEXT]` registers a file - most often a photo still sitting in a family member's own library, or a document in someone else's archive folder - that can never be copied, moved, or renamed, but still belongs on the processing queue. It reads no HTML and stages no asset copy; the target is only `.exists()`-checked, never opened.

The stub it writes reuses the case-(c) pointer-only shape (§2.3): `asset_elsewhere: true`, so `fha process` knows the missing companion asset is deliberate, not an oversight. Two new frontmatter keys carry the location itself, since case (c) previously only ever said a companion was *missing*, never *where it actually lives*: `asset_path` (the path exactly as the human typed it - a mapped drive letter, a relative note-to-self, whatever shorthand was meaningful to them) and `asset_path_absolute` (the resolved, unambiguous form, usable regardless of the working directory a later pass runs from). Both are forward-slashed for a stable cross-platform read.

```yaml
---
title: "Grandma's wedding photo"
asset_elsewhere: true
asset_path: E:/family-photos/grandma-wedding.jpg
asset_path_absolute: E:/family-photos/grandma-wedding.jpg
---
```

`slug` comes from the target file's own stem (there is no page title to borrow), so the stub is named after what it points at. A missing target (an unplugged drive, a typo'd path) is not a hard refusal - the human may be capturing before reconnecting the drive - so the stub is still written and a warning printed either way.

**Recorded follow-up, not a promise.** As of this build, `fha process` reads and honors `asset_elsewhere: true` (the existing case-(c) flag), but does **not yet** read `asset_path`/`asset_path_absolute` - it mints a no-asset record the same way it already does for any other pointer-only stub, without pre-filling those two locations into the record. Consuming them (e.g. offering the resolved path as a "confirm you can see this file" step at processing time) is a natural next step for `fha process`, not yet built.

---

## 3. The staged-bundle contract (the seam)

Every delivery form converges on one artifact: a **staged bundle** the engine can turn into an inbox stub. Defining this contract once is what keeps the front-ends dumb and interchangeable.

A staged bundle is a folder containing:

```
<slug>-<timestamp>/
  page.html        ← the raw captured DOM (always present; the scrape source recipes run on)
  asset.<ext>      ← the asset: case (a) the file/image; case (b) the self-contained
                     preservation copy (single-file .html or .pdf); absent for case (c)
  capture.json     ← the human's inputs + the browser's generic pre-fill
```

`page.html` (the always-saved scrape source) and the case-(b) `asset` (the human-facing preservation copy) are deliberately **two files even when both are HTML**: the asset inlines its images/CSS so it survives link rot but is bulky and lossy to re-parse, while `page.html` stays the clean DOM the recipe extracts from. For case (a) the asset is the downloaded/fetched file itself; for case (c) there is no asset. `capture.json` is the human-and-browser layer; `page.html` is the machine-extractable layer. The split is deliberate: **the browser captures raw material and light hints; the Python recipe does the authoritative extraction at ingest** (§5.5). `capture.json`:

```json
{
  "schema": 2,
  "url": "https://www.ancestry.com/...",
  "title": "1880 United States Federal Census - Thomas Hartley",
  "accessed": "2026-06-24",
  "source_date": "1880",
  "source_type": "census",
  "repository": "Ancestry.com",
  "assets": [
    { "file": "record.jpg", "role": "record", "mode": "manual" },
    { "file": "page-snapshot.html", "role": "webpage", "mode": "singlefile" }
  ],
  "people": ["Thomas Hartley", "Margaret Hartley", "Ethel Hartley"],
  "notes": "Bob's great-grandfather's household. The boy listed as 'Calvin' is Cal.",
  "recipe_hint": "ancestry"
}
```

Every field except `url` is optional. `notes` is the human's free-text body; `people` is the human's curated checklist, carrying **at minimum the name of a person the record is about**; it is **additive** (the human's names lead, and recipe-found household/family names the panel never showed are kept rather than dropped). `repository` is the human's "where it's from" edit and wins over the recipe/host guess. `recipe_hint` is the browser's *guess*; the engine still runs detection on `page.html` and may overrule it.

**Assets (schema 2).** The shipping companion and backend use `assets: [ {file, role, mode, provisional?} ]` - an ordered list (record-then-webpage), so the page-copy-plus-record "both" case files as a §12.1 bundle folder; the optional per-asset `provisional: true` marks a capture-what's-shown stand-in (a screenshot) for an original that couldn't be fetched (set by the companion; ingest does not yet read it - the notes line is the operative carrier, §5.6). A file listed here is part of the completed-capture contract: if it is missing on disk at ingest, the bundle is reported malformed and left in staging, not filed incomplete. The earlier flat **schema 1** shape (`asset_mode: "fetch|singlefile|pdf|manual|none"` + `asset_file: "asset.jpg"`, single asset) is still **accepted** by ingest as legacy/hand-authored input.

`schema` is the `capture.json` shape version (current: **2**, the `capture._CAPTURE_JSON_SCHEMA` constant) and exists so the companion and the backend can evolve independently - ingest is **forgiving** about it: an *absent* `schema` is read as the current version (legacy/hand-authored bundles), and a *newer* one is read for the fields it shares with a one-line "run `fha update-tools`" warning, **never refused**. Bump it only on an incompatible shape change.

---

## 4. Delivery forms (front-ends onto one engine)

The front-ends, in increasing polish and increasing install cost. **One backend serves them all:** every form converges on the §3 staged-bundle contract (or, for the paste floor, on the engine directly).

### 4.1 Paste fallback - *the v1 path, shipping today*

The human copies the page (or saves the HTML) and pipes it in: `pbpaste | fha capture --url …`, or `fha capture --asset saved-page.html --url …`. Needs nothing new - no extension, no browser permission. This is the always-available floor and the path every other form degrades to. It produces the inbox stub directly.

### 4.2 Bookmarklet - *not pursued*

> **Decision (MG2.1):** the bookmarklet is **not a supported delivery form.** A `javascript:` bookmark can only trigger a *single combined `.html` download*, never the `<slug>-<timestamp>/` staged-bundle folder that `fha capture --ingest` (§6) consumes - so it would need its own loose-single-file ingest path, a second seam to maintain for a form strictly weaker than the extension (no multi-asset, no authenticated fetch, no clean `page.html`/asset split). The **browser extension (§4.3) is the front-end**; the **paste fallback (§4.1) is the zero-install floor** for anyone who hasn't installed it. A saved single page is still always capturable by hand: `fha capture --asset saved-page.html --url …`.

### 4.3 Browser extension - *the nice panel* (§5)

A proper MV3 extension with a side panel: generic pre-fill, a notes box, the three asset modes, and a staged bundle written to a capture folder. The preferred everyday form. Fully specified in §5.

### 4.4 Native-messaging host - *seamless, v2*

An optional small local host the extension can hand bundles to, so they land **straight in the archive `inbox/`** (even an external root) with no Downloads detour and no manual ingest step. Install-gated; the extension works without it (falling back to §5.1's download path). Specified in §5.7.

### 4.5 Claude-in-Chrome - *the AI front-end*

Where available, Claude-in-Chrome reads the open record page and writes the staged bundle (or the inbox stub directly) through the normal tool path - no custom extension to maintain. It is a delivery form, not a separate engine: it produces the same `capture.json` + `page.html` + asset and hands off to `fha capture`/`fha process`.

---

## 5. The browser extension - full design

The everyday companion. Manifest V3, Chromium-first (the same code loads in Edge; a Firefox port is a packaging detail, not a redesign). It lives **outside** the Python tool suite and outside the archive's operating layer - it is installed in the *browser*, not vendored into an archive by `fha install`, so it is **not** a `manifest.json` operating-layer entry. Its home in the repo is `browser-companion/` (source + build), shipped as an unpacked/load-from-store extension, versioned alongside the spec but distributed through the browser, not the archive.

### 5.1 The transport problem and its solution

The hard constraint that §13b never resolved: **an MV3 extension cannot write to an arbitrary filesystem path.** It has no access to the archive's `inbox/`. Its only file-writing affordance is `chrome.downloads.download()`, which writes **under the browser's Downloads directory** (subfolders are allowed; escaping the Downloads root is not). It also cannot *move* a file the user already downloaded.

So the extension cannot, by itself, drop a bundle into `inbox/`. Three honest resolutions, in order of how much they ask of the user:

1. **Staging folder + explicit ingest (default).** The extension writes the bundle to `Downloads/fha-inbox/<slug>-<timestamp>/`. Locally, the human runs **`fha capture --ingest`** (§6), which sweeps that folder into the archive's real `inbox/` - the one sanctioned *move* at intake (SPEC §12.1). No watcher, no daemon: an explicit command, on the human's schedule, exactly like every other refresh-on-use step.
2. **Download directly into the inbox.** If the archive's `inbox/` lives under the user's Downloads tree (or the browser's download directory is pointed at it), the extension's bundle lands in `inbox/` immediately and `--ingest` is a no-op. A convenience for the common single-machine setup; not assumable in general because `inbox/` is a configurable root (SPEC §12.4) that often lives elsewhere.
3. **Native-messaging host (§5.7).** A registered local host writes the bundle straight into `inbox/`, anywhere it lives, with no Downloads detour. The seamless upgrade, install-gated.

The default (1) needs zero native install and works in plain Chrome. The extension never pretends it wrote to the archive when it only wrote to Downloads - its final panel line says exactly where the bundle went and, when staging, that `fha capture --ingest` will file it.

### 5.2 The staged bundle (what the extension writes)

Exactly the §3 contract: `page.html`, an optional `asset.<ext>`, and `capture.json`. The extension assembles all three in memory (content script + panel), then writes them with `chrome.downloads.download()` into `Downloads/fha-inbox/<slug>-<timestamp>/`. `page.html` is **always** saved - it is the raw material the Python recipe re-extracts from, so even a generic in-browser pre-fill that got the title wrong is recoverable at ingest.

### 5.3 The panel workflow (the four phases)

The UX the owner described, mapped to the capture→file spine. The panel never blocks on a decision answerable in two seconds.

**Phase 1 - Invoke (one gesture).** On any record page the human clicks the toolbar button (or a page-action overlay). A side panel opens over the page; nothing is written yet. If a recipe hint matches, the panel says so ("Looks like an Ancestry census"); otherwise it announces generic capture.

**Phase 2 - Confirm, don't fill (a glance).** The panel shows what the browser pre-filled - title, date, repository/collection, the person names it found, the image it spotted - as editable fields, plus a **notes box pre-seeded with a tiny template**:

```
Who is this for? (the person/family this record is about)
What is it? / anything to remember
```

The human's job is a glance and a nudge: fix a mangled title, untick an irrelevant person, type a sentence of context. Everything is optional; a hurried human clicks straight through. The panel's *only* insistence is on the asset (Phase 3) - the one thing that cannot be redone once the page is closed.

**Phase 3 - Capture the evidence (asset mode a/b/c).** The panel offers the three modes explicitly, because this is where pages fight back:

- **(a) Auto-capture from a link** - the human pastes (or the panel pre-fills from a detected `<img>`/PDF link) the asset URL; the extension `fetch()`es it **in the page's own session** (`credentials: 'include'`) and saves the bytes as `asset.<ext>`. Honest caveat: a cross-origin or DRM-protected viewer image may refuse the fetch - the panel says so and offers (c).
- **(b) Page-as-asset** - one click stores a **self-contained preservation copy** as the asset (case (b)): a **single-file HTML** with images/CSS/fonts inlined as data-URIs (`asset_mode: "singlefile"`), so the saved page survives the original's rot instead of decaying into broken-image placeholders. When a faithful *rendered* page matters more than re-parsing it, the panel also offers **save-as-PDF** (`asset_mode: "pdf"`); see §5.6 for the MV3 reality of each. The raw `page.html` is saved alongside regardless, so scraping never depends on the bulky preservation copy.
- **(c) Manual hand-off** - the human downloads the file the normal way (or screenshots the viewer), then **drag-drops it into the panel**; the extension folds it into the bundle as `asset.<ext>` (`asset_mode: "manual"`, flagged `provisional-image` when it is a screenshot). This is the always-works escape hatch.

A fourth choice, **none** (case (c) pointer-only), writes citation + `external_links` with no asset.

**Phase 4 - Stage, don't process (hand-off).** Clicking *Capture* writes the staged bundle (§5.2) and the panel reports where it went. **No source record is minted, no claims drafted, no S-id assigned** - the bundle is pre-source. The human goes back to researching and captures five more the same way; a research sitting yields a dozen bundles, and one later `fha capture --ingest` + `process-source` session works them all. Batch capture is the natural mode.

Batch mode keeps the form honest across navigations: a navigation that lands *during* a capture is not lost - the panel replays the pre-fill refresh the moment the capture finishes; a viewer that changes only the address (no page load, e.g. next-image arrows) refreshes the form the same way; and if the page still moved between pre-fill and *Capture*, the panel warns ("This page changed since the form was filled") but never blocks - the human may have edited the fields deliberately.

### 5.4 MV3 manifest & permissions (least privilege)

```jsonc
{
  "manifest_version": 3,
  "name": "Plaintext Family History - Capture",
  "version": "0.1.0",
  "action": { "default_title": "Capture this record" },
  "background": { "service_worker": "background.js" },
  "side_panel": { "default_path": "panel.html" },
  "permissions": ["activeTab", "scripting", "downloads", "storage", "sidePanel"],
  "optional_permissions": ["nativeMessaging"],
  "host_permissions": ["<all_urls>"]
}
```

- `activeTab` + `scripting` - read the current page's DOM only when the human invokes the companion (no ambient page access).
- `downloads` - write the staged bundle into `Downloads/fha-inbox/…`.
- `storage` - remember the human's preferences (default asset mode, capture-folder name, whether the native host is installed).
- `sidePanel` (+ the `side_panel` key) - show the Phase-1→4 panel (§5.3) over the page via Chrome's side-panel API. Not a privacy-sensitive grant - it only lets the extension open its own panel - so it holds the least-privilege intent while enabling the described UX.
- `nativeMessaging` is **optional** - requested only if the human opts into the §5.7 seamless path.
- `host_permissions: <all_urls>` is needed only for the case-(a) cross-site asset `fetch`; it can be narrowed to the recipe domains for a tighter build. The DOM read itself rides on `activeTab` and needs no host grant.

### 5.5 In-browser pre-fill vs. authoritative Python extraction

**The recipes stay in Python. The browser does only a light, generic pre-fill.** The panel reads obvious signals - `<title>`/`og:title`, `<link rel=canonical>`, `article:published_time`, visible person-name selectors - to fill the glance-and-nudge fields, and guesses a `recipe_hint` from the hostname. That is *all*. The durable, per-site citation parsing - the census household table, the marriage-index fields - happens when `fha capture --ingest` runs the existing Python recipe on the saved `page.html`.

This is a deliberate architecture choice, not laziness:

- **One source of truth.** Re-implementing Ancestry/FamilySearch parsing in JavaScript would mean two recipe sets drifting apart. The browser captures; Python extracts.
- **The companion stays dumb and replaceable** (the TOOLING §13b promise). The extension, the native host, and Claude-in-Chrome all produce the same `page.html`; the engine treats them identically.
- **Hints are cheap to be wrong about.** Because `page.html` is always saved, a bad browser guess costs nothing - the Python recipe corrects it at ingest, and the human's explicit edits in `capture.json` still win over both.

### 5.6 Asset acquisition, honestly

| Mode | How | Caveat / MV3 reality |
|---|---|---|
| (a) fetch | `fetch(url, {credentials:'include'})` in the content-script context | cross-origin / tiled viewer / DRM images may refuse → fall to (c) |
| (b) singlefile | walk the DOM; `fetch` each referenced image/CSS/font and rewrite its URL to a `data:` URI; serialize to one `.html` | **doable in pure MV3** (the SingleFile approach); cost is fetch volume + base64 bloat on image-heavy pages, and same-origin/CORS limits on a few sub-resources. Capture *after* load so dynamic content has settled. The default for case (b). |
| (b) pdf | print-to-PDF of the rendered page | **not reliably one-click in MV3**: extensions have no print API; programmatic PDF needs the `chrome.debugger` `Page.printToPDF` protocol (heavyweight, shows a warning banner) or the §5.7 native host. The dependable path is the human's own *Save as PDF* handed in via (c). Offer it, but don't promise a silent one-click. |
| (a/b) page images | also pull the page's primary `<img>` as its own asset file | a cheap add-on to (a)/(b): when the record centers on a photo, save that image even if the page itself is the asset, so the evidence isn't trapped inside a snapshot. Schema 2's `assets:` list carries multiple files (record + page snapshot), filed as a SPEC §12.1 bundle (§3) |
| (c) manual | drag-drop the human's own download (a saved image, a *Save-as-PDF*, a screenshot) into the panel | screenshots are flagged `provisional-image` - a better scan may exist |
| none | citation + `external_links` only | `asset_elsewhere: true`; lands in the research-to-do |

The companion never silently produces a worse asset than it claims: a screenshot is labelled provisional, a pointer-only capture is labelled asset-elsewhere, a single-file snapshot is honest that it is a *copy of the page* not the original record, and review sees every flag. And the raw `page.html` is saved in every mode, so a preservation asset that is hard to re-parse (a PDF, a bulky inlined snapshot) never costs the recipe its clean scrape source.

**Carrying the provisional flag (the shipping convention).** `capture.json` schema 2 (§3) carries the flag in the contract, per asset - `assets: [{file, role, mode, provisional?}]` - and the companion sets it. The backend does **not yet read it**: `fha capture --ingest` consumes only each entry's `file` + `role` (`capture.py` `_resolve_bundle_assets`), so the warning reaches review through the companion's *other* carrier - the `[provisional image - a cleaner original may exist …]` line it prepends to the human's `notes` body, which ingest files as the stub body. A legacy **schema 1** bundle, which has no dedicated field, carries the flag the same way. Teaching ingest and the stub render to honor the structured field (a stub frontmatter flag review can filter on) is the remaining plumbing step (§9).

### 5.7 Native-messaging host (the seamless upgrade)

An optional Python host (`fha capture --host`, registered as a native-messaging manifest the extension can find) that receives a bundle over stdin/stdout framing and writes it **straight into the archive's `inbox/`** - resolving `inbox` through `fha.yaml` (SPEC §12.4), so it works for an external inbox root the Downloads path can't reach. With the host installed, Phase 4 files directly; without it, the extension falls back to §5.1's staging folder. If the opted-in host answers the availability ping but then fails to file a bundle, the capture falls back to Downloads *and says so* - the panel names the reason and points at `fha capture --ingest` (recover this one) and `fha capture --install-host` (fix the recurrence); the seamless path never degrades silently. The host runs only when the browser invokes it (no resident daemon), preserving the no-watcher rule. Installation is a one-time `fha capture --install-host` that writes the per-browser native-messaging manifest pointing at the local `fha`.

---

## 6. `fha capture --ingest` - the sweep (new backend mode)

The local bridge from a staged-bundle folder to real inbox stubs. A new mode of the existing engine, not a new tool.

```
fha capture --ingest [DIR] [--dry-run]
```

`DIR` defaults to the known capture folder (`~/Downloads/fha-inbox/`, overridable by an optional `fha.yaml` `capture_staging:` key). For each `<slug>-<timestamp>/` bundle it finds:

1. **Read** `capture.json`, `page.html`, and the optional asset.
2. **Run the engine** - feed `page.html` as the HTML, the asset as `--asset`, and the `capture.json` fields as the explicit overrides (`title`/`type`/`date`/`url`/`accessed`/`notes`/`people`/`repository`), exactly as if a human had typed them. The recipe re-detects on `page.html` (overruling a wrong `recipe_hint`), the human's `notes` become the stub body, and `people` names carry through as hints. This **reuses `run_capture` wholesale** - the ingest path produces a byte-identical stub to what the paste-fallback would have, which is the whole point of the staged-bundle seam.
3. **File** the resulting `{slug}.notes.md` + asset into the archive `inbox/` (the one sanctioned move).
4. **Clear** the staged bundle (move it to a `fha-inbox/.ingested/` holding folder, never hard-delete - the same never-lose-the-human's-work bias as everywhere else).

`--dry-run` reports the plan - which bundles, which recipe each matched, which stub each would become - and writes nothing. Ingest is idempotent: a bundle already swept (present in `.ingested/`) is skipped, so re-running after an interruption is safe. A malformed bundle (no `page.html`, unreadable `capture.json`) is reported and left in place, never silently dropped.

Because nothing sweeps automatically (the no-watcher rule), the human has to *remember* to run `--ingest` - so **`fha doctor` surfaces the nudge**: when the staging folder holds un-ingested bundles it reports `staged captures: N bundle(s) … waiting  next: run \`fha capture --ingest\``, the same name-the-next-step treatment as inbox aging. It only speaks up when the staging folder exists (most machines never run the companion), so it is silent noise-free elsewhere.

**Working-copy interaction (SPEC §12.4):** `--ingest` writes only to `inbox/` and reads no originals, so it stays available on a `WORKING_COPY` machine - captures made while travelling sweep into the inbox to carry back, exactly like any other inbox material.

---

## 7. Privacy, safety, and what intake must never do

These bind every delivery form and the engine alike:

- **Reads only the open page, in the human's own session.** No login, no pagination, no fetching behind auth on the tool's own initiative (§2.4). The case-(a) asset fetch retrieves only an asset the human can already see, with §2.4's two scoped exceptions - the Ancestry image-download endpoint called in the human's own session (the same single request as Ancestry's Download button; never bulk, never on any other site) and a IIIF full-size image fetched without credentials - both firing only on the human's Capture click, one image at a time.
- **No local absolute paths leak.** Stubs store alias-form paths; `capture.json` carries the page URL, not a machine path.
- **Everything enters `suggested`.** Capture drafts nothing as accepted; the S-id, the claims, and the person resolution are all the processing pass's job, gated by human review.
- **Living/restricted handling is deferred to processing.** A stub is pre-source and carries no `living`/`restricted` decision; those are set when the source record is minted. The companion never publishes anything outward - it only stages into the local inbox.
- **The human always owns the throw-away.** Ingested bundles move to `.ingested/`, never hard-deleted; provisional and asset-elsewhere flags surface in review rather than being silently resolved.

---

## 8. Build status & milestones

| Piece | Status |
|---|---|
| Engine `fha capture` (paste fallback, recipes, generic, stub render, research-log) | **built** ([`tools/capture.py`](tools/capture.py)) |
| `fha capture --path` - register a must-never-move asset (§2.6) | **built** (plan 17, [`tools/capture.py`](tools/capture.py) `run_capture_path`; [`tests/test_capture.py`](tests/test_capture.py) `CapturePathTestCase`) - one pointer stub, `asset_path`/`asset_path_absolute` recorded; `fha process` (`process_pointer_only`) now accepts `asset_path` as a second pointer-only shape alongside `external_links`, folding the human's own `asset_path` shorthand into `provenance:` (never the machine-specific `asset_path_absolute`) - PR #30 review follow-up, [`tests/test_process.py`](tests/test_process.py) `test_sidecar_pointer_only_accepts_capture_path_stub` |
| Recipes: Ancestry, FamilySearch, Newspapers.com, FindAGrave + generic | **built** ([`tools/capture_recipes/`](tools/capture_recipes/)) |
| `fha capture --ingest` sweep (§6) | **built** ([`tools/capture.py`](tools/capture.py) `run_ingest`; BUILD_INGESTION.md MG2.1) |
| `fha doctor` staged-captures nudge (§6) | **built** ([`tools/doctor.py`](tools/doctor.py); BUILD_INGESTION.md MG2.1) - warns when bundles sit in the staging folder waiting for `--ingest` |
| Browser extension (§5) | **built** ([`browser-companion/`](browser-companion/), MV3) - the four-phase side panel, generic pre-fill, all five asset modes, and the staged-bundle download path; lives outside the archive operating layer (not a `manifest.json` entry). The seamless native-host path (§5.7) now ships end to end: its backend (below) plus the extension front-end that consumes it (IIIF/warning panel wiring and the opt-in `nativeMessaging` permission request in `native-host.js`), which stays OFF by default behind the "file straight into my archive" toggle |
| Native-messaging host backend (§5.7) | **built** ([`tools/capture.py`](tools/capture.py) `fha capture --host` / `--install-host`; [`tests/test_capture_host.py`](tests/test_capture_host.py)) - length-prefixed stdin/stdout JSON: files a framed bundle into the configured `inbox/` (via `run_ingest`), plus read-only `suggestNames` / `checkUrl`. The extension front-end that drives it (the opt-in `nativeMessaging` permission request + `isAvailable` gate) also ships, OFF by default behind the "file straight into my archive" toggle |
| Bookmarklet (§4.2) | **not pursued** - the extension is the front-end; the paste fallback is the floor (see §4.2) |

The build order that follows the spine's own logic: the engine first, then `--ingest` (it makes *any* staged bundle useful and is a thin wrapper over the existing engine), then the extension (which produces those bundles), then the native host as a seamless-path upgrade. The concrete, phased milestone breakdown lives in [`BUILD_INGESTION.md`](BUILD_INGESTION.md) (the MG series: engine MG1.1-MG1.3, `--ingest` MG2.1, browser companion MG2.2, native host MG2.3); this document is the design it implements against.

---

## 9. Open questions (deferred, not decided)

- **Capture-folder discovery.** *Resolved when `--ingest` was built (MG2.1):* the sweep resolves `DIR` in the order **explicit positional arg → `fha.yaml` `capture_staging:` key → default `~/Downloads/fha-inbox/`**. `capture_staging` is read straight off the config and `~`-expanded - it is *not* an archive root (it lives under the browser's Downloads tree), so it is never routed through `resolve_path`. The extension's own `storage` of a chosen folder is a browser-side convenience layered on top, not a backend concern.
- **Tiled-viewer assets.** The Ancestry image viewer serves tiles, not a single file; case-(a) fetch can't reassemble them. v1 answer is the (c) screenshot `provisional-image`; a tile-stitcher is explicitly out of scope (and near the §2.4 boundary).
- **Single-file vs. PDF as the case-(b) default.** Single-file HTML stays scrapeable and is one-click in MV3, but bloats on image-heavy pages and can miss CORS-locked sub-resources; PDF renders faithfully but isn't reliably one-click (needs `chrome.debugger` or the native host) and can't be re-parsed. v1 leans single-file, with PDF offered via the human's *Save as PDF* + drag-drop (c). Whether to bundle a vendored SingleFile-style inliner or write a minimal one is a build-time call.
- **How much to inline.** Images and CSS are the must-haves for case (b); fonts, web-components, and lazy-loaded media are diminishing returns against snapshot size. Settle the inlining scope when the extension is built.
- **Recipe parity in the panel.** Whether to let a recipe ship an *optional* JS pre-fill snippet for a richer Phase-2 glance, without becoming a second source of truth. Kept out of v1 to hold the §5.5 line.
- **First-class provisional/asset flags.** *Half-resolved when `capture.json` schema 2 landed:* the per-asset `provisional` flag now rides in the contract (`assets: [{file, role, mode, provisional?}]`, §3) and the companion sets it - but `fha capture --ingest` currently reads only each entry's `file`/`role` and drops the flag, so the warning reaches review via the `[provisional image - …]` notes line (§5.6). The remaining step is the ingest/stub plumbing: read the field at sweep time and surface it as a stub frontmatter flag review can filter on.
