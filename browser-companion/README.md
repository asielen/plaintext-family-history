# browser-companion: the Plaintext capture extension

A Manifest V3 browser extension that stages an open genealogy record page into
your archive's inbox as a **capture bundle**. It is the everyday front-end for
the intake on-ramp described in [`../TOOLING_INGESTION.md`](../TOOLING_INGESTION.md)
§5.

> **Fast, forgiving capture now; structured review later.** Grab everything while
> the page is open; defer every decision that can wait.

The extension is deliberately **dumb and replaceable**. It never logs in, never
scrapes behind your back, and never decides what a record *means*. It stages raw
material, the page's DOM, an optional asset, and a little metadata, and the
durable Python tool (`fha capture --ingest`) does the authoritative extraction
later. A wrong guess in the browser costs nothing, because the raw `page.html` is
always saved and the Python recipe corrects it at filing.

This folder lives **outside** the Python tool suite and outside the archive's
operating layer. It is installed in the *browser*, not vendored into an archive
by `fha install`, so it is **not** a `manifest.json` operating-layer entry. It is
versioned alongside the spec but distributed through the browser.

---

## What it produces (the only contract that matters)

Every capture writes one **staged bundle** to
`Downloads/<folder>/<slug>-<timestamp>/` (folder defaults to `fha-inbox`):

```
<slug>-<timestamp>/
  page.html          ← the raw captured DOM, ALWAYS saved; the clean scrape source
  page-snapshot.html ← optional self-contained page snapshot (role webpage), when
                       "Keep a copy of the whole page" is on
  record.<ext>       ← optional evidence file (role record): an image or PDF, pulled
                       from the page address or dropped in; absent for "No file"
  capture.json       ← your inputs + the browser's generic pre-fill (schema 2)
```

A capture can carry **both** a page snapshot and a separate evidence file (the
"both" case): the "Keep a copy of the whole page" checkbox is its own toggle and
composes with the Yes/No evidence choice. The raw `page.html` is always saved
separately as the recipe's scrape source.

`capture.json` (schema 2; every field except `url` optional; see
[`../TOOLING_INGESTION.md`](../TOOLING_INGESTION.md) §3). The single
`asset_mode`/`asset_file` pair of schema 1 is now an `assets:` LIST, one entry
per staged file with its `role`:

```json
{
  "schema": 2,
  "url": "https://www.ancestry.com/…",
  "title": "1880 United States Federal Census, Thomas Hartley",
  "accessed": "2026-06-24",
  "source_date": "1880",
  "source_type": "census",
  "assets": [
    { "file": "record.jpg", "role": "record", "mode": "manual", "provisional": true },
    { "file": "page-snapshot.html", "role": "webpage", "mode": "singlefile" }
  ],
  "people": ["Thomas Hartley", "Margaret Hartley"],
  "notes": "Bob's great-grandfather's household.",
  "recipe_hint": "ancestry"
}
```

`fha capture --ingest` reads this seam and files it per SPEC §12.1:

- **Zero or one asset** lands as a **lone-sidecar stub** (`{stem}.notes.md` plus
  its same-stem asset, or pointer-only for "No file").
- **Two or more assets** (the "both" case) land as a **bundle folder**
  `inbox/<slug>/` holding one `notes.md` (your notes plus light frontmatter hints,
  including per-file `files:` role hints) and every asset. That is the shape
  `fha process` later dissolves into one source whose `files:` inventory lists
  each asset with its role.

Ingest is **back-compatible**: a legacy **schema 1** bundle (flat
`asset_mode`/`asset_file`) still files unchanged. The raw `page.html` is the
scrape source when present; if a bundle omits it, the `webpage`-role HTML
snapshot is parsed instead.

---

## Install (unpacked, developer mode)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and choose this `browser-companion/` folder.
4. Pin **“Plaintext Family History (Capture)”** to the toolbar.

No store listing yet; this is a load-from-source extension. The same code loads
in Chrome and Edge; a Firefox port is a packaging detail, not a redesign.

---

## Use it (the four phases)

1. **Invoke.** On a record page, click the toolbar button. A side panel opens; if
   the site is recognized it says so ("Looks like an Ancestry record"), otherwise
   it announces generic capture. Nothing is written yet.
2. **Is this right?** Glance at the pre-filled title, date, where-it's-from, and
   the people it found. Fix a mangled title, untick someone, type a sentence of
   context, or click straight through. Everything here is optional. (People are
   hints, not full claims, until you review them offline.)
3. **Save the record.** Two independent choices:
   - **Keep a copy of the whole page** (a checkbox, on by default): a
     self-contained snapshot with images and styles inlined, so the saved page
     survives the original's rot. Executable scripts are dropped but JSON-LD and
     other metadata are kept, so the snapshot stays scrape-able.
   - **Is there a specific file that's the actual record?**
     - **Yes, save the actual file**, provide the direct url (pre-filled from the
       detected image, edit if wrong) *or* drop a file in. Either way the file is
       pulled when you press Capture, in your own logged-in session; there is no
       separate fetch button. Tick "the file I'm providing is a screen capture"
       when it is a screenshot, so reviewers know to look for a clearer original
       later.
     - **No, the page copy is the record**, for memorials, index entries, and
       write-ups where the page itself is the evidence. (With the page-snapshot toggle
       off too, this is a pointer-only capture: just the citation.)
4. **Capture & save.** The bundle is staged to Downloads. **No source record is
   minted, no claims drafted, no ID assigned**, it is pre-source. Go back to
   researching and capture more; a sitting yields a dozen bundles.

### Filing the bundles into your archive

Nothing sweeps automatically (the archive has no daemons or watchers). When you're
back at your archive, run **one** command:

```sh
fha capture --ingest        # sweeps Downloads/fha-inbox/ into the archive inbox/
```

`fha doctor` reminds you when bundles are waiting:
`staged captures: N bundle(s) … waiting  next: run \`fha capture --ingest\``.

If your archive's `inbox/` lives under your Downloads tree, point the staging
folder there (Settings, or `fha.yaml` `capture_staging:`) and `--ingest` becomes a
no-op, the bundles land in `inbox/` directly.

---

## What it never does (privacy & safety, §7)

- Reads only the **open page, in your own session**. No login, no API calls, no
  pagination, no fetching behind auth on its own initiative. A fetched asset is
  only one you can already see.
- No local machine paths leak, `capture.json` carries the page URL, never a disk
  path.
- Everything enters review as **pre-source**. The S-id, the claims, the person
  resolution, and any living/restricted decision are all the *filing* pass's job,
  gated by your review. The companion never publishes anything outward.
- The throw-away is always yours: ingested bundles are parked in `.ingested/`,
  never hard-deleted.

---

## Architecture

```
manifest.json          MV3 manifest (least privilege)
src/
  background.js        service worker, opens the side panel
  content.js           injected on invoke, DOM read, generic pre-fill,
                       asset fetch (your session), single-file inliner
  panel.html/.css/.js  the side panel, the numbered steps
  lib/
    capture-json.js    builds capture.json (schema 2, the assets[] list) + name
    bundle.js          writes the bundle via chrome.downloads (the §5.1 path)
    native-host.js     optional seamless path (§5.7), scaffolded, off by default
test-bundle/           an example "both" bundle in the exact output shape (round-trip test)
```

The **recipes stay in Python.** The browser does only a light, generic pre-fill
(`<title>`/`og:title`, canonical URL, `article:published_time`, JSON-LD Person
names, the largest image). The per-site census-table / index parsing happens when
`fha capture --ingest` runs the existing Python recipe on the saved `page.html`
(§5.5). One source of truth; the companion stays replaceable.

### The transport reality (§5.1)

An MV3 extension cannot write to an arbitrary path, its only file-writing
affordance is `chrome.downloads.download()`, which writes under the browser's
Downloads directory. So the default path stages to `Downloads/fha-inbox/…` and
you run `fha capture --ingest` to do the one sanctioned *move* into the archive.
The optional native-messaging host (§5.7) removes the Downloads detour, but it is
v2, *designed, not built* on the Python side, so it is off by default and the
extension falls back to the staging path.

---

## Deviations & notes from the spec (for the maintainer)

These are the build-time decisions where the implementation went slightly beyond
[`../TOOLING_INGESTION.md`](../TOOLING_INGESTION.md) §5 as written. They are
recorded here (not silently) as proposed spec clarifications:

- **`sidePanel` permission added.** §5.4's manifest lists
  `activeTab, scripting, downloads, storage` (+ optional `nativeMessaging`). The
  side-panel UX §5.3 describes requires Chrome's `sidePanel` permission and a
  `side_panel` manifest key, so both are present. `sidePanel` is not a
  privacy-sensitive grant (it only allows showing a panel), so this keeps the
  least-privilege intent. **Proposed amendment:** add `sidePanel` to the §5.4
  permission list.
- **`capture.json` is schema 2: an `assets:` list.** The single
  `asset_mode`/`asset_file` pair became `assets: [{file, role, mode,
  provisional?}]` so one capture can carry **both** a page snapshot (role
  `webpage`) and a separate evidence file (role `record`), the "both" case the
  panel's design enables. Ingest reads both shapes (schema 1's flat pair still
  files unchanged) and routes multi-asset captures to a SPEC §12.1 bundle folder.
  **Proposed amendment:** document schema 2 in §3 alongside schema 1 (this README
  and the test-bundle are the worked example).
- **Provisional flag is now structured (and still in `notes`).** A flagged
  screen capture sets `assets[].provisional: true` (schema 2) AND prepends a
  `[provisional image, …]` line to the human's `notes` body, so review sees it
  whether or not a tool honors the structured flag yet. The §5.6 notes-line
  convention is kept as the always-readable belt-and-braces.
- **Single-file snapshot is minimal but scrape-able.** It inlines images and
  stylesheet text (the §9 must-haves), drops **executable** scripts but **keeps
  `<script type="application/ld+json">` and other non-executable metadata** so the
  snapshot stays parseable; it does **not** inline fonts or nested CSS `url()`
  resources, and it is bounded (≤120 resources, ≤5 MB each). `page.html` is still
  saved alongside as the clean scrape source, so scraping never depends on it.
- **Print-to-PDF mode removed.** The old radio offering *Save as PDF* via
  drag-drop is gone: the single-file HTML snapshot supersedes it (§9's case-(b)
  default), and a real PDF still files fine through the "Yes, save the actual
  file" path (paste its url, or drop the PDF). One fewer mode to explain.
- **Bookmarklet is not here, by design.** §4.2 (the MG2.1 decision): a
  `javascript:` bookmark can only trigger a single combined `.html` download,
  never the staged-bundle folder `--ingest` consumes. The **extension is the
  front-end**; the **paste fallback** (`pbpaste | fha capture …`) is the
  zero-install floor for anyone who hasn't installed it.

---

## Testing

There is no browser-driven test harness wired into this repo (the tooling tests
are stdlib Python). The contract that *can* be checked without a browser, that
the extension's output bundle ingests cleanly, is covered by
[`../tests/test_browser_companion.py`](../tests/test_browser_companion.py): it
validates the MV3 manifest, asserts every file the manifest references exists, and
runs the example `test-bundle/` (which mirrors the exact shape `panel.js`/`bundle.js`
write) through `fha capture --ingest` end-to-end. Run it from the repo root:

```sh
python -m unittest tests.test_browser_companion -v
```

Hand-testing the live extension: load it unpacked, open a record page, capture in
each mode, then `fha capture --ingest --dry-run` against your Downloads folder to
confirm each bundle is recognized.
