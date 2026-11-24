# TOOLING_INGESTION.md: The Inbox Contract and Web Capture

**Who this is for:** developers building or extending the *intake* side of the `fha` tool suite: anything that puts material into an archive's inbox for `fha process` to turn into a source. To use the archive, start with [`docs/GETTING_STARTED.md`](docs/GETTING_STARTED.md).

**Version 2.41, companion to SPEC.md v2.41 and TOOLING.md v2.41 (versions track the SPEC).**

This document defines one thing first and everything else after it: **what a source waiting in the inbox looks like**, and how `fha process` reads it (§1). That shape is the contract. A person dropping a scan and typing a note, the `fha capture` engine (§2), the browser companion (§5), a script someone writes for their own scanner: each is only a front end, and **any front end that produces the §1 shape is conforming**. SPEC.md §12.1 states what a source stub is; TOOLING.md §13b names the `fha capture` engine; this document specifies the contract and the engine in enough detail to build a new front end in any language.

It is one of three sibling design docs: [`TOOLING.md`](TOOLING.md) (core tools), **this doc** (ingestion) and [`TOOLING_INTERFACE.md`](TOOLING_INTERFACE.md) (workbench + skills). The build order for all three is TOOLING.md §15. How the browser companion reads particular sites is in [`browser-companion/README.md`](browser-companion/README.md); the companion does not need to be rebuildable from this document, only to produce the shape it defines.

TOOLING.md's operating rule applies here too: **tools have no daemons, watchers, or schedulers.** Freshness is refresh-on-use. Capture stages material; nothing sweeps it automatically; a human (or a workbench skill) runs the next step.

Intake is everything before `fha process`: getting material into the inbox as a source stub, the first step of SPEC §4's path from file to fact. A source passes through three states:

| State | Where it lives | What it is | Who makes it |
|---|---|---|---|
| **Research** | the open web, a shoebox, a relative's album | a record the human is looking at | the human |
| **Source stub** | `inbox/` (SPEC §12.1) | an asset (or none) + a freeform notes file with rough notes and any structured hints | hand, **or any front end** |
| **Processed source** | `sources/…` + filed asset | a `{slug}_{s-id}.md` record with `suggested` claims drafted from the stub | `fha process` + the `process-source` skill |

Capture takes everything while the page is open and leaves every decision that can wait to processing.

A stub is *pre-source*: no s-id, no accepted claims, light optional frontmatter over freeform prose. Everything that takes judgment (minting the s-id, resolving people against the index, drafting claims) happens at processing, where the index is at hand.

---

## 1. The inbox contract

### 1.1 The three shapes

A source waiting in `inbox/` (the inbox root resolves through `fha.yaml`, SPEC §12.4) takes exactly one of three shapes:

| Shape | On disk | When |
|---|---|---|
| **Lone sidecar** | `{stem}.{ext}` + `{stem}.notes.md` side by side | one file. The pair is matched by stem: exactly one other file in the folder may share the notes file's stem |
| **Pointer-only stub** | `{stem}.notes.md` alone, frontmatter `asset_elsewhere: true` | no file to copy in: the record is held elsewhere, or the file must never move. It names the evidence with `external_links:`, `asset_path:`, or both |
| **Bundle folder** | `inbox/{name}/` holding a bare `notes.md` and one or more files | more than one file for one source: a page copy and the record, a photo and its back, a recording and its transcript |

A file with no notes file at all is also valid inbox material: processing starts from a blank page. A notes file is never required.

A folder that holds no `notes.md` is not a bundle: `fha process` treats it as a folder of separate items to triage (TOOLING.md §6).

### 1.2 The notes file

**The body** is freeform prose, written by a person or filled in by a front end: the human's own words about what this is and why it matters. It has no schema. At processing it becomes the record's `## Notes` (SPEC §14), where a reviewer reads it beside the evidence.

**The frontmatter** is optional; a person may skip it entirely. When present, these keys are read (every one is optional, and a key not listed is ignored):

| Key | Meaning | What `fha process` does with it |
|---|---|---|
| `title` | what the item is | the record's `title:` (a `--title` flag wins) |
| `source_type` | SPEC §14 vocabulary | the record's `source_type:` and the asset root it files into (`--type` wins); an out-of-vocabulary value is refused before anything moves, naming the allowed words |
| `source_date` | the date of the evidence itself | normalized to EDTF; a loose date ("about 1880", "1870s") is translated, and an unclear one stops processing before the stub is consumed |
| `source_class` | `original` \| `derivative` \| `authored` | the record's `source_class:` (a proof argument is always `authored`) |
| `citation` | a citation, as text | the record's `citation:` |
| `repository` | where the evidence came from or is held | the record's `repository:` |
| `provenance` | where the original came from | the record's `provenance:` |
| `external_links` | `[{url, accessed}]`, or bare URL strings | the record's `external_links:` |
| `people` | a list of **names** as the page or the person wrote them | never resolved here: the names are added to `## Notes` as one line ("Captured people mentioned on source page (unreconciled): …") for the drafting pass to resolve against the index |
| `restricted` | `true` keeps the source out of exports (SPEC §19) | the record's `restricted: true` (always set for `source_type: dna`) |
| `asset_elsewhere` | `true` on a pointer-only stub | lets a notes file with no companion file mint a record with no `files:`; without it a lone notes file is refused as a likely mistake |
| `asset_path` | where a must-never-move file lives, exactly as the person typed it | folded into `provenance:` as "Original not copied into the archive - last known location: …" |
| `asset_path_absolute` | the same path, resolved | written by `fha capture --path` for a person reading the stub; not copied into the record, since a machine-specific path does not belong in a file that travels |
| `files` / `roles` | per-file hints, bundle folders only (§1.3) | each file's entry in the record's `files:` inventory |
| `attach_to` | an existing source's s-id, bundle folders only | the files join that source instead of a new one being minted: filed and named as for a new source but under that s-id, none of them made its main file, each listed in its `files:`, and the notes added to its `## Notes` with the bundle's title and citation. The source keeps its type and everything else it says, so `--type` and `--date` are refused, as is a bundle marked `restricted` joining a source that is not, and a bundle whose site data does not name a FamilySearch film image within two of one that source holds on the same film (so a mistyped s-id cannot fold a different record in). A value that is not a source id, or names no source, stops processing before anything moves; in a lone notes file the key is refused, naming `--more`. Deleting the line files the bundle as a new source |

The fill-in template a person copies by hand is `archive-template/inbox/_TEMPLATE.notes.md`: plain questions, no frontmatter. A notes file must be UTF-8; one saved in another encoding, or with frontmatter that does not parse, is refused with the fix named and nothing changed. A frontmatter value with an unquoted ` #` in it (`title: Deed to Lot #4`) parses but loses everything from the `#` on, so `fha lint` reads the notes files waiting in the inbox and names each one (W157, TOOLING.md §3) while the quotes can still go in.

### 1.3 Role hints in a bundle

A bundle's `notes.md` may say what each file is, in either of two forms:

```yaml
files:                       # a list (the form capture writes)
  - file: grave-1.jpg
    role: grave
    is_primary: true         # the file to open first; at most one
  - file: record-data.json
    role: site-data
roles:                       # or a filename -> role mapping
  interview.mp3: recording
  interview-transcript.md: transcript
```

A `files:` entry may also carry `copy:` (which print or copy the file is, SPEC §14). Each hint becomes that file's `files:` entry in the record; a file with no hint is given a role from its name (`…-back` → `back`) or none. A hint naming a file that is not in the folder stops processing before anything moves, so a typo is fixed in the stub rather than lost with it. `is_primary: true` marks the record's main file (SPEC §14); without one, processing picks the plain scan or the first record file.

### 1.4 How `fha process` reads it

`fha process` (TOOLING.md §6) takes the item (the asset, its notes file, or the bundle folder) and:

1. **Pairs it.** An asset finds its `{stem}.notes.md`; a notes file finds its one same-stem companion (several is a refusal: process the intended file directly); a notes file with none needs `asset_elsewhere: true` and at least one of `external_links:` / `asset_path:`.
2. **Decides the type and root.** `--type` outranks the notes file's `source_type:`, which outranks an `fha.yaml` `intake_defaults:` rule and then the file's extension. The type decides the asset root: a scanned record saved as a `.jpg` with `source_type: census` files into the documents root, not the photo library.
3. **Mints the s-id and files the evidence** (or, for a bundle whose notes say `attach_to:`, takes that source's s-id). Documents-root files are renamed `{slug}[-{copy}][-{role}]_{s-id}.{ext}` and carry the `SOURCE:` keyword where the format holds one; photos-root files keep their names and get the keyword (SPEC §12.1, §13). A bundle dissolves: one s-id for every file, the folder gone.
4. **Scaffolds the record** from the frontmatter (§1.2) and the role hints (§1.3), with the notes body as `## Notes`.
5. **Consumes the stub.** The notes file is gone once the record holds its content; the original files are copied into their roots and the inbox originals parked in `inbox/processed/` (SPEC §12.1). Nothing is deleted.

Every step before the record write can fail cleanly, and a failure leaves the inbox exactly as it was. Drafting claims from the stub is the `process-source` skill's job, after Stage A (TOOLING_INTERFACE.md §2.1): the notes and the name hints seed `suggested` claims, never accepted facts.

### 1.5 Conformance

A front end conforms when what it writes into `inbox/` is one of the §1.1 shapes, its notes file parses as §1.2 describes, and it never writes outside the inbox. It need not use `fha capture`; it need not be a browser. A front end that cannot write into the inbox directly (a browser extension, §5.1) may stage a §3 bundle for `fha capture --ingest` to turn into a §1 shape instead.

---

## 2. The engine: `fha capture`

[`tools/capture.py`](tools/capture.py) is the reference front end, and the one the others feed: given a web page, it decides what the page becomes in the inbox.

### 2.1 What it does

```
fha capture [--url URL] [--title "…"] [--type TYPE] [--date DATE] [--asset FILE] [--dry-run]
```

HTML arrives on **stdin** (`… | fha capture`) or from an `--asset` that is itself a saved page. The engine:

1. **Chooses a recipe.** Site recipes (§2.2) are tried in priority order; the first whose `detect()` matches wins; an unknown site falls to the **generic recipe** (page title, canonical URL, accessed date, and about 2000 characters of visible text as the citation basis). Any page is capturable.
2. **Extracts citation fields:** title, `source_type`, `citation`, `repository`, `source_date`, `source_class`, `external_links`, and the **person names the page lists**. `source_date` is the date of the source itself (SPEC §14), so a derivative record (an index entry, a Find a Grave memorial) gets none: it was made after the event it reports, on a day the page does not give, and the event date stays in the stub's text for a claim. When the page is one person's own page (a Find a Grave memorial, a FamilySearch, Ancestry or WikiTree tree profile), the site's id for that person rides on the stub as `external_ids:` (SPEC A.7); `fha process` carries it into the source's notes, to be added to that person's record when the person is linked. Explicit `--title`/`--type`/`--date` always override the scrape (the human's word wins); `--type` is checked against the `source_type` vocabulary so a typo surfaces now, not as an unprocessable stub later.
3. **Writes the stub:** a lone sidecar (§1.1): `{slug}.notes.md` whose frontmatter carries the recovered fields (§1.2) and whose body is the human's notes or the page's visible text, beside the asset as `{slug}.{ext}`. A capture with no asset and a URL is a pointer-only stub (`asset_elsewhere: true`). Collisions are detected before any overwrite.
4. **Logs the search.** Capture is itself a search, so each capture appends one entry to `notes/research-log.md`, the archive's one search log, in SPEC §17c's shape: `date` (the capture date), `question` (the page title), `repository` (the site), `collection` (the record's collection when the recipe knows it), `terms` when there are any, and `result: staged <inbox path>` (captured, not yet processed, so no s-id exists yet). `fha index` reads the log, so `fha report`'s "already searched" annotation sees the capture. The log is a durable record under `notes/`, never `.cache/`.

### 2.2 Recipes are data, not code

Recipes live in [`tools/capture_recipes/`](tools/capture_recipes/), one module per site, each exposing `detect(html, url)` and `extract(html, url)`, discovered at runtime and sorted by `PRIORITY`. They are **plug-in data** for capture: a new site needs a new module, not an engine change. A broken or interface-incomplete recipe is skipped with a warning and the page is captured by the generic recipe instead.

Current coverage, in priority order: **Ancestry** (record pages, the image viewer, census household tables, shared tree photos), **Newspapers.com** (clipping + citation), **FamilySearch** (record, tree person, and a bare film image with its film, image number and citation, and, when its Full-Text view is open, FamilySearch's machine reading of the image as an unchecked `transcript`), **Find a Grave** (memorial + cemetery place), **Internet Archive** (a book page: title, author, citation with the page link), **West Virginia Archives** (a Vital Research Records image: its film and image number; also the image behind a FamilySearch record's "View on wvculture.org" badge, saved with that record). Any other site gets the generic recipe.

### 2.3 The three asset cases

Every capture resolves its evidence one of three ways, referred to below as **(a)/(b)/(c)**:

- **(a) A file**, an image or document the page holds, becomes the source's asset.
- **(b) The page is the record:** an obituary, a wiki biography, a memorial page. The asset is a **self-contained preservation copy** of the page: a single-file HTML with its images and stylesheets inlined, so it stays readable offline and after the original page is gone. A bare `outerHTML` dump is not enough: it still points at images and styles by URL, which disappear with the page. The raw DOM is saved separately as the scrape source (`page.html`, §3), because the inlined copy is for the human's eyes and the recipe reads the clean DOM.
- **(c) Pointer-only:** the page only says where the record is held. No asset: citation + `external_links` only, written `asset_elsewhere: true` (§1.1), so the missing file is deliberate.

A screenshot of an image the page will not hand over is a case-(a) asset marked **provisional**, so review knows a cleaner original may exist.

### 2.4 Boundaries

The engine reads only the HTML it is given; it fetches nothing itself. A front end fetches only on the human's own action, in the human's own browser session, and only what that session can already open: it never logs in, never asks for or stores a password, and never sends a site's credentials anywhere but that site. What each site reader fetches, and how it paces itself and stops when a site asks it to slow down, is documented per site in [`browser-companion/README.md`](browser-companion/README.md). Everything that arrives enters as pre-source material; no claim is accepted without a human `reviewed:` date.

### 2.5 Flags & exit codes

| Flag | Meaning |
|---|---|
| `--url URL` | the record page URL (added to `external_links`) |
| `--title "…"` | override the captured title |
| `--type TYPE` | override the inferred `source_type` (controlled vocabulary) |
| `--date DATE` | override the source's own date (EDTF or loose natural language) |
| `--asset FILE` | an image/document to stage, or the saved page HTML to read |
| `--path PATH` | register a file that must stay exactly where it is (§2.6) |
| `--note TEXT` | a note to record with `--path` (goes in the stub body) |
| `--ingest [DIR]` | sweep staged bundles from `DIR` (default: `fha.yaml` `capture_staging:`, else `~/Downloads/fha-inbox`) into the inbox (§6) |
| `--host` / `--install-host [--browser B]` | the native-messaging host and its one-time registration (§5.7) |
| `--dry-run` | preview every write without touching disk |

The modes are mutually exclusive: `--install-host`, `--host`, `--ingest`, `--path`, and the default page capture (`--url`/`--title`/`--type`/`--date`/`--asset`) each run alone. Mixing flags across two modes (e.g. `--ingest --url …`) is a hard error naming both sides, not a silent drop of the losing mode's flags. (`--browser` belongs to `--install-host`; `--dry-run` is cross-mode; `--title` is shared between page capture and `--path`, since both use it to label the stub.)

Exit: `0` clean · `1` warnings (a `--path` target not found right now) · `2` user error (bad `--type`/`--date`, asset destination clash, mixing mode flags) · `3` filesystem failure while staging. Tracebacks never reach the user; every error names a cause and the next command.

### 2.6 `--path`: register a file that must never move

`fha capture --path PATH [--note TEXT] [--title TEXT]` registers a file that can never be copied, moved, or renamed but still belongs on the processing queue, most often a photo in a family member's own library, or a document in someone else's archive folder. It reads no HTML and stages no copy; the target is only checked for existence, never opened.

It writes a pointer-only stub (§1.1) carrying the location:

```yaml
---
title: "Grandma's wedding photo"
asset_elsewhere: true
asset_path: E:/family-photos/grandma-wedding.jpg
asset_path_absolute: E:/family-photos/grandma-wedding.jpg
---
```

`asset_path` is the path exactly as the human typed it (a mapped drive letter, a relative note-to-self); `asset_path_absolute` is the resolved form. Both are forward-slashed. The stub is named after the target file's own stem. A missing target (an unplugged drive, a typo) is a warning, not a refusal: the stub is still written. `fha process` reads `asset_path` into the record's `provenance:` (§1.2).

---

## 3. The staged bundle (written by the browser)

A browser cannot write into `inbox/` (§5.1), so the companion writes a **staged bundle** under the browser's Downloads folder instead, and `fha capture --ingest` (§6) turns each bundle into a §1 inbox shape. A staged bundle is a folder:

```
<slug>-<timestamp>-<token>/
  page.html        ← the raw captured DOM, the scrape source the recipe runs on
  <asset files>    ← the evidence: the record image, the page snapshot, site data … (none for case (c))
  capture.json     ← the human's inputs + the browser's pre-fill + the list of assets
```

`page.html` and a case-(b) page snapshot are **two files even when both are HTML**: the snapshot inlines its resources so it survives link rot but is bulky to re-parse, while `page.html` stays the clean DOM. A bundle without `page.html` is still read: the `webpage`-role snapshot (else the first HTML asset) is parsed instead, and a bundle with no HTML at all is reported malformed. The trailing `<token>` makes the folder name unique even when two captures share a slug and a timestamp.

`capture.json`:

```json
{
  "schema": 2,
  "url": "https://www.ancestry.com/...",
  "title": "1880 United States Federal Census - Thomas Hartley",
  "accessed": "2026-06-24",
  "source_date": "1880",
  "source_type": "census",
  "source_class": "derivative",
  "repository": "Ancestry.com",
  "citation": "…",
  "assets": [
    { "file": "record.jpg", "role": "record", "mode": "manual", "provisional": true },
    { "file": "page-snapshot.html", "role": "webpage", "mode": "singlefile" }
  ],
  "people": ["Thomas Hartley", "Margaret Hartley", "Ethel Hartley"],
  "notes": "Bob's great-grandfather's household. The boy listed as 'Calvin' is Cal.",
  "recipe_hint": "ancestry"
}
```

| Field | Meaning |
|---|---|
| `schema` | the shape version (current **2**, `capture._CAPTURE_JSON_SCHEMA`). Absent is read as current; a newer one is read for the fields it shares, with a one-line "run `fha update-tools`" warning, never refused. Bumped only on an incompatible shape change |
| `url` | the record page. The only field a bundle should always carry |
| `title`, `source_type`, `source_date`, `repository` | the human's edits, or the browser's pre-fill; they override what the recipe reads from `page.html` |
| `citation` | a citation the page itself supplied (a site's own "cite this"); replaces the one the recipe assembles |
| `source_class` | `original` \| `derivative` \| `authored`, when the page says which; any other word is dropped |
| `accessed` | the capture date |
| `assets` | the ordered list of evidence files, `{file, role, mode, provisional?, is_primary?}`. A listed file missing from the folder makes the bundle malformed: it is reported and left in staging, never filed incomplete. `role` becomes the file's role hint (§1.3); `is_primary: true` marks the main file. `mode` (how the file was obtained) and `provisional` are informational: the provisional warning reaches review as a line the companion adds to `notes` |
| `people` | names the human ticked or typed, plus names the site reader found; additive |
| `notes` | the human's own words; they become the stub body |
| `recipe_hint` | the browser's guess at the site; the engine still runs detection on `page.html` and may overrule it |
| `attach_to` | the s-id of a source the human said these files belong to (the companion asks when a FamilySearch film image sits within two images of one the archive holds, §5.5). Written into the bundle folder's notes as `attach_to:` (§1.2); a value that is not a source id is dropped with a warning, and a capture with only one file is filed as a new source with a warning, since only a bundle folder's notes carry the key |

The flat schema-1 shape (`asset_mode: "fetch|singlefile|pdf|manual|none"` + `asset_file`) is also accepted: one asset with the default `record` role, or none.

---

## 4. Front ends

Each front end produces either the §1 shape directly or a §3 bundle for `--ingest`.

### 4.1 By hand

Drop a file in `inbox/` and, if there is anything to say, a `{stem}.notes.md` beside it (or make a folder with a `notes.md`). This is the shape everything else imitates; it needs no tool at all.

### 4.2 Paste

`pbpaste | fha capture --url …`, or `fha capture --asset saved-page.html --url …`. Needs no extension and no browser permission; it writes the inbox stub directly. Every other web front end can fall back to it.

### 4.3 Browser extension (§5)

A Manifest V3 extension with a side panel: pre-fill, a notes box, the asset choices, and a staged bundle written to a capture folder. The everyday web front end.

### 4.4 Native-messaging host (§5.7)

An optional local host the extension hands bundles to, so they land straight in the archive `inbox/` (even an external root) with no Downloads detour and no separate ingest step.

### 4.5 An AI harness with browser access

An agent that can read the open page (Claude in Chrome, or any harness with a browser tool) produces the same `capture.json` + `page.html` + assets, or the inbox stub directly, and hands off to `fha capture`/`fha process`. It is a front end, not a separate engine.

A bookmarklet is not a front end: a `javascript:` bookmark can trigger only a single combined `.html` download, never a bundle folder, so a saved page goes through §4.2 instead.

---

## 5. The browser companion

[`browser-companion/`](browser-companion/) is a Manifest V3 extension, Chromium-first (the same code loads in Edge). It runs in the browser, never inside the archive, but **ships with every archive**: `fha install` and `fha update-tools` copy its loadable files (`manifest.json`, `src/`, `icons/`, and the owner's README) into `.fha/browser-companion/`, so any archive owner can load it unpacked with no clone of the project. Its development files (`tests/`, `test-bundle/`, `package.json`) stay in the project repo. Its README documents the panel, every site reader, and the extension's tests; this section states only what the rest of the toolchain relies on.

### 5.1 Transport

An MV3 extension cannot write to an arbitrary path: its only file-writing call, `chrome.downloads.download()`, writes under the browser's Downloads folder. So the companion writes each bundle (§3) to `Downloads/fha-inbox/<slug>-<timestamp>-<token>/`, and the human runs **`fha capture --ingest`** (§6) to file it, the one sanctioned *move* at intake (SPEC §12.1). If the archive's inbox lies under the Downloads folder, or `capture_staging:` points there, the bundle needs only that one command. With the native host (§5.7) installed and switched on, bundles go straight into `inbox/`. The panel never claims it wrote to the archive when it wrote to Downloads: its last line says where the bundle went and gives the exact command, with the folder the browser actually used.

### 5.2 What it writes

Exactly the §3 bundle. `page.html` is saved in every mode, so a wrong pre-fill is recoverable at ingest.

### 5.3 The panel

One gesture opens a side panel over the record page; nothing is written yet. The panel shows the pre-filled title, date, repository and people as editable fields, a notes box, and the evidence choice: a page-copy checkbox (case (b), on by default) and "is there a specific file that is the record?" (case (a): a URL to fetch or a file dropped in; a screen capture is ticked as provisional). With neither, the capture is pointer-only (case (c)). *Capture* writes the bundle and reports where it went; no s-id is assigned and no claim drafted. The panel stays bound to the tab it opened on, follows that tab's navigation, and warns, without blocking, when the page changed after the form was filled.

### 5.4 Permissions

The companion's own [`manifest.json`](browser-companion/manifest.json) is the reference. It asks for `activeTab` and `scripting` (read the page when the human invokes the panel), `downloads` (write the bundle), `storage` (preferences), `sidePanel` (the panel itself), the optional `nativeMessaging` (only when the human turns on §5.7), and `<all_urls>` host access (re-reading a page after the tab navigates, reading the tab's address for the staleness warning, and the site readers' requests to the record's own site).

### 5.5 Pre-fill and site readers

On any page the panel reads generic signals (`<title>`/`og:title`, `<link rel=canonical>`, `article:published_time`, JSON-LD Person names, the largest image) and guesses `recipe_hint` from the hostname. On the sites it knows (Ancestry, FamilySearch, Find a Grave, Newspapers.com, Internet Archive book pages, and IIIF image servers) a **site reader** in the companion reads the record as the site presents it and saves what it finds as bundle assets: the record image at full size, the site's own index of the record as a `site-data` JSON file, photos, and the site's own citation. Each reader is described in [`browser-companion/README.md`](browser-companion/README.md).

The Python recipe still runs on the saved `page.html` at ingest, and the human's typed fields still win over both. Where the browser and the recipe derive the same hint (`source_type`, `source_date`, `source_class` from a record's collection and event date), they apply **one shared rule written twice**, `recordHints` in `browser-companion/src/lib/ancestry-record.js` and `record_hints` in `tools/capture_recipes/_common.py`, held to the same answers by one case table (`tests/fixtures/capture-samples/ancestry-hints.json`) that both test suites run.

### 5.6 Asset acquisition

| Case | How | Caveat |
|---|---|---|
| (a) file by URL | `fetch` in the page's own session | a cross-origin or protected image may refuse; the panel says so and offers a dropped file |
| (a) dropped file | the human's own download, *Save as PDF*, or screenshot dropped on the panel | a screenshot is marked provisional |
| (a) site reader | the record image, photos or page tiles fetched by the site's reader (§5.5) | per site; see the companion's README |
| (b) single-file snapshot | the page cloned after load, images and stylesheets inlined as `data:` URIs, executable scripts dropped, JSON-LD kept | bounded in size; fonts and nested CSS resources are not inlined |
| (c) none | citation + `external_links` only | ingest writes `asset_elsewhere: true` |

The companion labels every asset for what it is: a screenshot is labelled provisional, a pointer-only capture is labelled asset-elsewhere, and a snapshot is a copy of the page, not the record.

### 5.7 Native-messaging host

`fha capture --host` is a Python native-messaging host the extension launches on demand (no resident process): JSON frames over stdin/stdout, each behind a 4-byte length prefix in native byte order, as Chrome specifies. It files a bundle straight into the configured `inbox/` through the same code as `--ingest`, and answers two read-only questions: `suggestNames` (person names and aliases) and `checkUrl` (is this URL already captured?). Both read the record files, not the index, through caches keyed on each file's size and mtime (`.cache/capture_names.json`, `.cache/capture_links.json`). The host writes only into `inbox/`, so it honours working-copy mode. `fha capture --install-host` writes the per-browser registration once. The extension uses it only when the human turns on "file straight into my archive"; if the host answers but fails to file a bundle, the capture falls back to Downloads and the panel says why, naming `fha capture --ingest` (recover this one) and `fha capture --install-host` (fix it).

### 5.8 Seen It: the research-side add-on

[`seen-it/`](seen-it/) is a separate, optional MV3 add-on for the research half of a sitting: it remembers which Newspapers.com pages, Ancestry/FamilySearch records and Find a Grave memorials the human has already opened, ruled out, clipped or attached to a tree, and marks them on the sites. It writes nothing to the archive and stages nothing; its research-log export produces entries for the human to paste into `notes/research-log.md`. It works standalone by default; its opt-in Advanced mode adds a stdlib-Python native-messaging helper (`seen-it/host/seenit_host.py`) that **only reads** an archive (it resolves `documents` and `inbox` through `fha.yaml` `roots:`, reads `sources/`, and sweeps the staging folder) to say which items are already filed (with their s-id) or captured but not yet filed. It also knows each filed FamilySearch film image by where it sits on its film (`fsfilm:{DGS}:{image number}`), which the capture companion asks about to offer a neighbouring image to the source that holds it. Its own data folder is a per-user setting, refused inside an archive or a git checkout. It is not copied into `.fha/`; `fha install`/`update-tools` keep the owner's loaded copy current in the folder `fha.yaml`'s `browser_extensions:` names (TOOLING §13c). See [`seen-it/README.md`](seen-it/README.md).

---

## 6. `fha capture --ingest`: the sweep

Turns staged bundles (§3) into inbox shapes (§1).

```
fha capture --ingest [DIR] [--dry-run]
```

`DIR` is resolved in order: the argument, then `fha.yaml`'s `capture_staging:` (read as a plain path and `~`-expanded; it is not an archive root), then `~/Downloads/fha-inbox/`. For each bundle folder it finds:

1. **Read** `capture.json`, the scrape source, and the listed assets (§3).
2. **Run the engine** (§2.1) with the scrape source as the HTML and the `capture.json` fields as explicit overrides, exactly as if a human had typed them. The recipe re-detects on the page (overruling a wrong `recipe_hint`), the human's `notes` become the stub body, and `people` carry through as name hints.
3. **File** the result into the archive `inbox/`: zero or one asset as a lone sidecar (or a pointer-only stub), two or more as a bundle folder whose `notes.md` carries the `files:` role hints and the `is_primary` mark (§1.3).
4. **Log** the capture in `notes/research-log.md` (§2.1).
5. **Park** the staged bundle in `fha-inbox/.ingested/`, never hard-deleted.

`--dry-run` reports which bundles, which recipe each matched and which stub each would become, and writes nothing. A bundle already in `.ingested/` is skipped, so re-running after an interruption is safe. A malformed bundle (no scrape source, unreadable `capture.json`, a listed asset missing, a file still locked by the browser) is reported with its fix and left in place; one bundle's failure never stops the others. A capture of a photo shared to someone's Ancestry tree that holds no picture is never filed; it is set aside once in `fha-inbox/.not-filed/` and said so.

Because nothing sweeps automatically, **`fha doctor` reminds the human**: when the staging folder holds un-ingested bundles it reports `staged captures: N bundle(s) … waiting  next: run \`fha capture --ingest\``. It speaks only when the staging folder exists.

**Working copy (SPEC §12.4):** `--ingest` writes only to `inbox/` and reads no originals, so it works on a `WORKING_COPY` machine; captures made while travelling sweep into the inbox to carry back.

---

## 7. Privacy, safety, and what intake must never do

These bind every front end and the engine alike:

- **Fetching happens in the human's session, on the human's action.** Nothing logs in, stores a credential, or fetches in the background; a front end fetches only what the human's session can already open, when the human asks (§2.4).
- **No local absolute paths leak.** Stubs store alias-form paths; `capture.json` carries the page URL, not a disk path. The one exception is `asset_path_absolute` on a `--path` stub, which never reaches the record (§1.2).
- **Everything enters as pre-source.** No s-id, no accepted claim, no person resolution at intake; all of it is the processing pass's job, gated by human review.
- **Living/restricted decisions wait for processing.** A stub carries a `restricted:` hint at most; the record is where the decision is made.
- **Only the human deletes.** Ingested bundles move to `.ingested/`, processed inbox originals to `inbox/processed/`; nothing is hard-deleted.

---

## 8. Build status

| Piece | Status |
|---|---|
| Inbox contract read by `fha process` (§1) | **built** ([`tools/process.py`](tools/process.py) `_read_sidecar`, `_bundle_file_hints`, `process_pointer_only`, `process_bundle`) |
| Engine `fha capture` (paste, recipes, generic, stub, search log) | **built** ([`tools/capture.py`](tools/capture.py)) |
| `fha capture --path` (§2.6) | **built** (`run_capture_path`; `fha process` reads `asset_path`) |
| Recipes: Ancestry, FamilySearch, Newspapers.com, Find a Grave, Internet Archive + generic | **built** ([`tools/capture_recipes/`](tools/capture_recipes/)) |
| `fha capture --ingest` (§6) and the `fha doctor` nudge | **built** (`run_ingest`; [`tools/doctor.py`](tools/doctor.py)) |
| Browser companion (§5) | **built** ([`browser-companion/`](browser-companion/)); copied into every archive at `.fha/browser-companion/` |
| Native-messaging host (§5.7) | **built** (`fha capture --host` / `--install-host`; off by default in the extension) |
| Seen It (§5.8) | **built** ([`seen-it/`](seen-it/)) |

Build order: the engine, then `--ingest` (it makes any staged bundle useful), then the extension, then the native host. TOOLING.md §15 (layer 9) lists the tests that show it is done.

---

## 9. Open questions

- **The provisional flag in the stub.** `assets[].provisional` reaches review only as the `[provisional image - …]` line the companion adds to `notes`. A stub frontmatter flag review could filter on would need ingest to read the field.
- **How much a snapshot inlines.** Images and stylesheets are inlined; fonts, web components and lazy-loaded media are not. Inlining more makes the snapshot larger, so the inliner stays minimal until a capture needs more.
