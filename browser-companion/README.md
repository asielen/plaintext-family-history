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

This folder lives **outside** the Python tool suite - it runs in the *browser*,
never inside the archive - but it **ships with every archive**: `fha install` /
`fha update-tools` vendor the loadable files (`manifest.json`, `src/`, `icons/`)
into `.fha/browser-companion/`, so from any installed archive you can load it
with no clone of this repo:

1. Open `chrome://extensions` (or `edge://extensions`), turn on Developer mode.
2. Click **Load unpacked** and pick your archive's `.fha/browser-companion/`
   folder. On Windows `.fha` is a hidden folder, so the picker may not list it:
   type the path into the dialog's file-name box (e.g.
   `D:\Family Archive\.fha\browser-companion`) and press Enter, or turn on
   "Show hidden items" in Explorer's View menu first.

The dev furniture here (`tests/`, `test-bundle/`, `package.json`,
`ANCESTRY-AUTOFETCH-TEST.md`) stays in the repo - what ships is exactly what the
browser loads. **This README stays too**: it is written for whoever works on the
extension and links to documents an installed archive does not carry. The
document an archive receives at `.fha/browser-companion/README.md` is
[`README-ARCHIVE.md`](README-ARCHIVE.md) - the same folder, an owner's
instructions, and no link that leads outside an archive. Edit that one whenever
the loading steps or the ingest command change; `tests/test_scaffold.py` fails if
it ever grows a link the installer does not ship.

Developing the extension remains a workshop-repo activity like any other
tool-building; archives receive the result through `fha update-tools`.

---

## What it produces (the only contract that matters)

Every capture writes one **staged bundle** to
`Downloads/<folder>/<slug>-<timestamp>-<token>/` (folder defaults to
`fha-inbox`). The trailing `<token>` is six random characters: the slug and the
timestamp are there so the folder reads well and sorts chronologically, but two
captures can genuinely share both (two side panels, or a clock adjustment), and
the browser resolves a folder clash by renaming the FILES inside it rather than
the folder - which would leave one merged folder whose `capture.json` names
assets belonging to the other capture. The token makes the folder itself the
unique thing.

```
<slug>-<timestamp>-<token>/
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
       off too, this is a pointer-only capture: citation and link only, staged with
       an empty `assets` list; `fha capture --ingest` files it as a pointer stub
       flagged `asset_elsewhere: true`.)
4. **Capture & save.** The bundle is staged to Downloads. **No source record is
   minted, no claims drafted, no ID assigned**, it is pre-source. Go back to
   researching and capture more; a sitting yields a dozen bundles.

### Filing the bundles into your archive

Nothing sweeps automatically (the archive has no daemons or watchers). When you're
back at your archive, run **one** command:

```sh
./fha capture --ingest      # macOS / Linux - sweeps the default staging folder into inbox/
.\fha capture --ingest      # Windows PowerShell
fha capture --ingest        # Windows Command Prompt
```

The handoff card shows the command with the staging folder the browser actually
used (`./fha capture --ingest "<that folder>"`) as soon as a capture completes -
the download directory is a browser setting, so the panel reads it from the
completed download rather than assuming a home-relative `Downloads`.

`fha doctor` reminds you when bundles are waiting:
`staged captures: N bundle(s) … waiting  next: run \`fha capture --ingest\``.

If your archive's `inbox/` lives under your Downloads tree, point the staging
folder there (Settings, or `fha.yaml` `capture_staging:`) and `--ingest` becomes a
no-op, the bundles land in `inbox/` directly.

---

## What it never does (privacy & safety, §7)

- Reads only the **open page, in your own session**. No login, no pagination, no
  fetching behind auth on its own initiative. A fetched asset is only one you can
  already see. One scoped exception to "no API calls": on an Ancestry image-viewer
  page, Capture calls Ancestry's own image-download endpoint in your session - the
  same single request its Download button makes; never bulk, never uninvited,
  never on any other site (the hand-test plan is
  [`ANCESTRY-AUTOFETCH-TEST.md`](ANCESTRY-AUTOFETCH-TEST.md)). A page whose image
  is served over the open IIIF standard gets the same one-image treatment,
  fetched without credentials.
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
README-ARCHIVE.md      the owner's README, installed as .fha/browser-companion/README.md
src/
  background.js        service worker, opens the side panel
  content.js           injected on invoke, DOM read, generic pre-fill,
                       asset fetch (your session), single-file inliner
  panel.html/.css/.js  the side panel, the numbered steps
  lib/
    capture-json.js    builds capture.json (schema 2, the assets[] list) + name
    bundle.js          writes the bundle via chrome.downloads (the §5.1 path)
    native-host.js     optional seamless path (§5.7), shipped, opt-in (off by default)
    srcset.js          HTML-spec srcset parsing (canonical; content.js keeps a copy)
    iiif.js            IIIF Image-API URL rewriting (canonical; ditto)
    people-harvest.js  JSON-LD person harvest (canonical; ditto)
    capture-readiness.js  the "record detail looks empty" phrases (canonical; ditto)
test-bundle/           an example "both" bundle in the exact output shape (round-trip test)
```

content.js is an injected classic script, so it cannot `import`: the four
canonical modules above have hand-kept copies inside it, and
`tests/test-sync.js` re-runs both sides through the same battery so a drift is a
failing build rather than a hope.

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
The optional native-messaging host (§5.7) removes the Downloads detour: the Python
side ships (`fha capture --host` / `--install-host`), and the extension side is
opt-in — OFF by default behind the "file straight into my archive" toggle, falling
back to the staging path when no host answers.

---

## Deviations & notes from the spec (for the maintainer)

These are the build-time decisions where the implementation went slightly beyond
[`../TOOLING_INGESTION.md`](../TOOLING_INGESTION.md) §5 as written. They are
recorded here (not silently) as proposed spec clarifications:

- **`sidePanel` permission added.** The side-panel UX §5.3 describes requires
  Chrome's `sidePanel` permission and a `side_panel` manifest key, so both are
  present. `sidePanel` is not a privacy-sensitive grant (it only allows showing
  a panel), so this keeps the least-privilege intent. *(Amendment landed:
  §5.4's manifest sketch now lists `sidePanel`.)*
- **`capture.json` is schema 2: an `assets:` list.** The single
  `asset_mode`/`asset_file` pair became `assets: [{file, role, mode,
  provisional?}]` so one capture can carry **both** a page snapshot (role
  `webpage`) and a separate evidence file (role `record`), the "both" case the
  panel's design enables. Ingest reads both shapes (schema 1's flat pair still
  files unchanged) and routes multi-asset captures to a SPEC §12.1 bundle folder.
  *(Amendment landed: §3 now documents schema 2 as the shipping shape, with
  schema 1 kept as accepted legacy input; this README and the test-bundle stay
  the worked example.)*
- **Provisional flag is now structured (and still in `notes`).** A flagged
  screen capture sets `assets[].provisional: true` (schema 2) AND prepends a
  `[provisional image, …]` line to the human's `notes` body, so review sees it
  whether or not a tool honors the structured flag yet. The §5.6 notes-line
  convention is kept as the always-readable belt-and-braces.
- **The handoff command names only a location the browser reported.** The
  download directory is a browser setting - moved to OneDrive, to a second
  volume, or carrying a localized name - so a home-relative `Downloads` path
  synthesized from the staging-folder setting was a guess presented as fact: it
  sent `--ingest` to a folder the bundle had never been written to, and the
  sweep reported nothing waiting on a capture that had just succeeded. The
  panel now reads the completed download's own absolute path
  (`chrome.downloads.search` → `DownloadItem.filename`), drops the file and
  bundle segments to get the folder that holds the bundles, and puts THAT in
  the command. With no reported path (before the first capture of a sitting, or
  a path that cannot be quoted into a shell command) the directory-less
  `./fha capture --ingest` stands and a hint line points at the browser's own
  download setting and at `fha.yaml`'s `capture_staging:` - never at an
  invented path.
- **The copied command carries the launcher prefix the machine needs.** `fha`
  is a launcher FILE at the archive root, never a program on PATH (AGENTS.md
  "Execution rules"), so the bare `fha …` the card used to offer was a
  command-not-found for everyone except a Windows Command Prompt user - the one
  shell that searches the current directory. `capture-json.js` `launcher()`
  reads the OS off `navigator.userAgentData.platform` (falling back to
  `navigator.platform`, then the UA string) and renders `./fha` or `.\fha`;
  unknown falls to `./fha`, which PowerShell also accepts. The card holds ONE
  string rather than the docs' three-shell block because that string is what
  the Copy button puts on the clipboard, and Windows gets the PowerShell
  spelling because `.\fha` resolves through PATHEXT in cmd.exe too - so it
  strands nobody, while the bare form strands every PowerShell user. Every
  other command the panel prints (the native-host hints, the fallback warning)
  goes through the same prefix.
- **Single-file snapshot is minimal but scrape-able.** It inlines images and
  stylesheet text (the §9 must-haves), drops **executable** scripts but **keeps
  `<script type="application/ld+json">` and other non-executable metadata** so the
  snapshot stays parseable; it does **not** inline fonts or nested CSS `url()`
  resources, and it is bounded (≤120 resources, ≤5 MB each). `page.html` is still
  saved alongside as the clean scrape source, so scraping never depends on it.
- **Every URL the snapshot keeps is anchored to the live page.** Relative
  references are rewritten to their absolute form rather than by injecting a
  `<base>` (a base of ours would send fragment-only links such as `#facts` to the
  live site and blank out SVG `<use href="#icon">` sprites). A page that declares
  its **own** `<base>` keeps it - that is the author's baseline, and it already
  governs the live page - written in the form the browser itself resolved
  (`document.baseURI`), never by resolving the raw `href` a second time: a
  path-relative `<base href="records/">` is *already* folded into `baseURI`, and
  re-resolving it would save `…/records/records/` and quietly re-point every
  reference one directory too deep. The sweep covers `href`/`src`/`poster`/`data`,
  `srcset` and `imagesrcset`, `form action` and `formaction`, SVG `href` and
  `xlink:href`, inline `style` attributes, `<style>` blocks, and the
  `url()`/`@import` references inside an inlined stylesheet (anchored to the
  **stylesheet's** address, not the document's). `srcset` is parsed by the HTML
  Standard's algorithm, never split on commas - a candidate URL may legally
  contain one. Nothing that resolves to a `file:` URL is ever written into a
  snapshot: capturing a page opened from disk must not bake local folder names
  into a file that lands in the archive.
- **What would navigate away is disarmed, not anchored.** Absolutizing is right
  for a *resource* the saved page needs; it is exactly wrong for anything that
  acts on the reader's behalf, because it turns a harmlessly broken directive
  into a working one. A `<meta http-equiv="refresh" content="0;url=/login">`
  anchored to the live site would bounce the reader out of the snapshot and onto
  a login page the moment they opened it, and the preserved evidence could never
  be read offline at all. So refreshes (including any hiding inside `<noscript>`),
  captured `Content-Security-Policy` pragmas (which would forbid the very
  `data:` images and `<style>` blocks the snapshot inlined), inline `on…`
  handlers, `<a ping>` beacons, and speculative `<link rel="preload|prefetch|
  prerender|preconnect|dns-prefetch">` fetches are all **disarmed**: the element
  and its value stay, moved onto a `data-fha-disabled-…` attribute no browser
  acts on, plus `data-fha-refresh-target` recording where a refresh pointed. An
  `<iframe>` keeps its live `src` (a framed record viewer is often the evidence)
  but gains a `sandbox` without `allow-top-navigation`, so a frame-busting script
  cannot do one level down what the refresh would have done. Following a link or
  submitting a form is still the reader's own click, so `<a href>` and
  `<form action>` are left anchored.
- **Markup hiding inside an attribute or a text node gets the same treatment.**
  Every sweep above walks *elements*, and two places in a captured page hold
  markup that never became one: an `<iframe srcdoc="…">` (a whole second
  document stored in an attribute value, parsed and run the instant the snapshot
  is opened) and a `<noscript>` (one raw text node while scripting is on, live
  markup again for a reader who opens the file with JavaScript off). Both are
  rewritten as strings: scripts keep their text but are given a type no browser
  runs (`text/fha-disabled-script`, the author's own type parked alongside),
  `on…` handlers, `ping` beacons, refreshes, captured CSPs and speculative
  `<link>`s are disarmed exactly as above, and a nested `<iframe>` gets the same
  sandbox. A `srcdoc` frame is additionally denied `allow-scripts` - its document
  is markup we carry and have already disarmed, so there is nothing legitimate
  left in it to run, and anything the string pass failed to recognise stays
  inert. That sandbox binds every frame nested inside it at any depth (sandbox
  flags are inherited and can only narrow), which is what bounds the rewrite to
  one level of markup. `srcdoc` references are anchored like the outer
  document's - an `about:srcdoc` document inherits the *parent's* base URL, so
  left alone they would resolve into whatever folder the snapshot was opened
  from - while `<noscript>` is left byte for byte, since absolutizing in there
  would arm the tracking pixel those blocks most often carry.
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

There is no browser-driven test harness wired into this repo (a DOM would have to
come with it). Two suites cover everything that *can* be checked without one.

**The pure JS helpers** - srcset parsing, the snapshot's URL anchoring, the
JSON-LD person harvest, IIIF rewriting, `capture.json` building, and the
content.js ↔ `src/lib/` sync guards - run under `node --test` (Node ≥18, no
dependencies):

```sh
npm --prefix browser-companion test
```

**The end-to-end contract** - that the extension's output bundle ingests cleanly -
is covered by [`../tests/test_browser_companion.py`](../tests/test_browser_companion.py):
it validates the MV3 manifest, asserts every file the manifest references exists,
pins the snapshot's URL-rewriting invariants, and runs the example `test-bundle/`
(which mirrors the exact shape `panel.js`/`bundle.js` write) through
`fha capture --ingest` end-to-end. Run it from the repo root:

```sh
python -m unittest tests.test_browser_companion -v
```

Hand-testing the live extension: load it unpacked, open a record page, capture in
each mode, then `fha capture --ingest --dry-run` against your Downloads folder to
confirm each bundle is recognized.
