# browser-companion: the Plaintext capture extension

A Manifest V3 browser extension that stages an open genealogy record page into your archive's inbox as a **capture bundle**. It is the everyday front end for web capture as described in [`../TOOLING_INGESTION.md`](../TOOLING_INGESTION.md) §5. What it must produce is TOOLING_INGESTION's inbox contract (§1) by way of the staged bundle (§3); how it reads each site is covered here (Site readers, below).

The design goal is to grab everything while the page is open and defer every decision that can wait until review.

The extension is deliberately **replaceable**. It never logs in, never fetches anything you did not ask for, and never decides what a record *means*. It stages raw material (the page's DOM, the record's files, what the site says about the record, and a little metadata) and `fha capture --ingest` turns that into an inbox item. The raw `page.html` is always saved and the Python recipe reads it again at filing, so a wrong guess in the browser can be corrected there; what you typed wins over both.

This folder lives **outside** the Python tool suite, since it runs in the *browser* rather than inside the archive, but it **ships with every archive**: `fha install` / `fha update-tools` vendor the loadable files (`manifest.json`, `src/`, `icons/`) into `.fha/browser-companion/`, so from any installed archive you can load it with no clone of this repo:

1. Open `chrome://extensions` (or `edge://extensions`), turn on Developer mode.
2. Click **Load unpacked** and pick your archive's `.fha/browser-companion/` folder. On Windows `.fha` is a hidden folder, so the picker may not list it: type the path into the dialog's file-name box (e.g. `D:\Family Archive\.fha\browser-companion`) and press Enter, or turn on "Show hidden items" in Explorer's View menu first.

The development files here (`tests/`, `test-bundle/`, `package.json`, `ANCESTRY-AUTOFETCH-TEST.md`) stay in the repo; what ships is exactly what the browser loads. **This README stays too**: it is written for whoever works on the extension and links to documents an installed archive does not carry. The document an archive receives at `.fha/browser-companion/README.md` is [`README-ARCHIVE.md`](README-ARCHIVE.md): the same folder, an owner's instructions, and no link that leads outside an archive. Edit that one whenever the loading steps or the ingest command change; `tests/test_scaffold.py` fails if it ever grows a link the installer does not ship.

Developing the extension is a workshop-repo activity like any other tool-building; archives receive the result through `fha update-tools`.

---

## What it produces

Every capture writes one **staged bundle** to `Downloads/<folder>/<slug>-<timestamp>-<token>/` (folder defaults to `fha-inbox`). The trailing `<token>` is six random characters. The slug and the timestamp make the folder read well and sort chronologically, but two captures can share both (two side panels, or a clock adjustment). The browser resolves a folder clash by renaming the FILES inside it rather than the folder, which would leave one merged folder whose `capture.json` names assets belonging to the other capture. The token keeps each folder unique.

```
<slug>-<timestamp>-<token>/
  page.html          ← the raw captured DOM, ALWAYS saved; the clean scrape source
  page-snapshot.html ← optional self-contained page snapshot (role webpage), when
                       "Keep a copy of the whole page" is on
  record.<ext>       ← optional evidence file (role record): an image or PDF, pulled
                       from the page address or dropped in; absent for "No file"
  capture.json       ← your inputs + the browser's generic pre-fill (schema 2)
```

A capture can carry **both** a page snapshot and a separate evidence file (the "both" case): the "Keep a copy of the whole page" checkbox is its own toggle and composes with the Yes/No evidence choice. The raw `page.html` is always saved separately as the recipe's scrape source.

`capture.json` (schema 2; every field except `url` optional; see [`../TOOLING_INGESTION.md`](../TOOLING_INGESTION.md) §3) lists the staged files as an `assets:` list, one entry per file with its `role`:

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

`fha capture --ingest` reads this file and files the bundle per SPEC §12.1:

- **Zero or one asset** lands as a **lone-sidecar stub** (`{stem}.notes.md` plus its same-stem asset, or pointer-only for "No file").
- **Two or more assets** (the "both" case) land as a **bundle folder** `inbox/<slug>/` holding one `notes.md` (your notes plus light frontmatter hints, including per-file `files:` role hints) and every asset. That is the shape `fha process` later dissolves into one source whose `files:` inventory lists each asset with its role.

Ingest also reads the **schema 1** shape (a flat `asset_mode`/`asset_file` pair). The raw `page.html` is the scrape source when present; if a bundle omits it, the `webpage`-role HTML snapshot is parsed instead.

---

## Install (unpacked, developer mode)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and choose this `browser-companion/` folder.
4. Pin **“Plaintext Family History (Capture)”** to the toolbar.

There is no store listing; this is a load-from-source extension. The same code loads in Chrome and Edge, and a Firefox port would need packaging changes only.

---

## Use it (the four phases)

1. **Invoke.** On a record page, click the toolbar button. A side panel opens; if the site is recognized it says so ("Looks like an Ancestry record. Filing will confirm it."), otherwise it announces generic capture. Nothing is written yet.
2. **Is this right?** Glance at the pre-filled title, date, where-it's-from, and the people it found. Fix a mangled title, untick someone, type a sentence of context, or click straight through. Everything here is optional. (People stay hints until you review them offline; they are not claims.)
3. **Save the record.** Two independent choices:
   - **Keep a copy of the whole page** (a checkbox, on by default): a self-contained snapshot with images and styles inlined, so the saved page survives after the original changes or disappears. Executable scripts are dropped but JSON-LD and other metadata are kept, so the snapshot stays scrape-able.
   - **Is there a specific file that's the actual record?**
     - **Yes, save the actual file.** Provide the direct url (pre-filled from the detected image, edit if wrong) *or* drop a file in. Either way the file is pulled when you press Capture, in your own logged-in session; there is no separate fetch button. Tick "the file I'm providing is a screen capture" when it is a screenshot, so reviewers know to look for a clearer original later. Where the file is fetched automatically (Ancestry, FamilySearch, Find a Grave, Newspapers.com, IIIF) the url box is hidden. It comes back after an image viewer's or IIIF page's auto-fetch fails, and on Newspapers.com when none of the clippings are yours. A status box says what will be fetched; after Capture it keeps a short log, one line per fetch (tick with size, cross with the site's reason, and a note when a picture is byte-identical to the last saved capture's).
     - **No, the page copy is the record**, for memorials, index entries, and write-ups where the page itself is the evidence. (With the page-snapshot toggle off too, this is a pointer-only capture: citation and link only, staged with an empty `assets` list; `fha capture --ingest` files it as a pointer stub flagged `asset_elsewhere: true`.)

   The panel stays bound to the tab it opened on (it follows that tab's navigation, never a tab switch). **↻ Re-read page** rebinds it to the active tab and refills everything from scratch; **↻ Re-read file** refreshes only the file half, on the same tab and record, keeping what was typed.
4. **Capture & save.** The bundle is staged to Downloads. **No source record is minted, no claims drafted, no ID assigned**; the bundle is pre-source. Go back to researching and capture more; one sitting can produce a dozen bundles.

### Filing the bundles into your archive

Nothing sweeps automatically (the archive has no daemons or watchers). When you're back at your archive, run **one** command:

```sh
./fha capture --ingest      # macOS / Linux: sweeps the default staging folder into inbox/
.\fha capture --ingest      # Windows PowerShell
fha capture --ingest        # Windows Command Prompt
```

As soon as a capture completes, the handoff card shows the command with the staging folder the browser actually used (`./fha capture --ingest "<that folder>"`). The download directory is a browser setting, so the panel reads it from the completed download rather than assuming a home-relative `Downloads`.

`fha doctor` reminds you when bundles are waiting: `staged captures: N bundle(s) … waiting  next: run \`fha capture --ingest\``.

If your archive's `inbox/` lives under your Downloads tree, point the staging folder there (Settings, or `fha.yaml` `capture_staging:`). The bundles then land in `inbox/` directly and `--ingest` has nothing to move.

---

## Privacy and safety

- **Requests happen only in your session, on your click.** The add-on never logs in, never asks for or stores a password, and fetches nothing in the background. Every request it makes rides your own signed-in browser session, on the site you are looking at, when you press Capture (or open the panel, for the few sites whose record list it reads to fill the form). Each site's rules are below.
- No local machine paths leak: `capture.json` carries the page URL, never a disk path.
- Everything enters as **pre-source**. The s-id, the claims, the person resolution, and any living/restricted decision are all the *filing* pass's job, gated by your review. The add-on never publishes anything outward.
- Ingested bundles are parked in `.ingested/`, never hard-deleted; deleting them is your call.

---

## Site readers

On the sites below, a site reader in the add-on reads the record the way the site presents it and saves what it finds as bundle files. The Python recipe still reads the saved `page.html` at ingest. Each reader's code lives in `src/lib/` (the canonical module; `content.js` keeps a copy, see Architecture).

### Generic pages and IIIF

Any page gets the generic pre-fill: `<title>`/`og:title`, the canonical URL, `article:published_time`, JSON-LD Person names, and the largest image. A page whose image is served over the open IIIF standard has that one full-size image fetched without credentials (`lib/iiif.js`).

### Ancestry

- **Image viewer.** Capture calls Ancestry's own image-download endpoint in your session (the same single request its Download button makes) and saves the image as `record.jpg` (role `record`). The hand-test plan is [`ANCESTRY-AUTOFETCH-TEST.md`](ANCESTRY-AUTOFETCH-TEST.md).
- **Record pages** (`/search/collections/{dbId}/records/{recordId}`). The reader (`lib/ancestry-record.js`) reads the record as the page shows it (its labelled index fields, the household table, the "Source Citation" sections) into `record-data.json` (role `site-data`: the site's index, never a transcript). It fetches the linked scan (`/imageviewer/…`, or an image-only collection's `/interactive/…` link) through the same download call, as `record.jpg`. A record with no scan, or one Ancestry will not hand over, still stages its index with a note saying so. A bundle that would hold only `record-data.json` keeps the page copy even when it was unticked, so filing keeps the index's role.
- **Record hints.** Where you left the form blank, `capture.json` carries the record's own `source_type` and `source_date` and, for an index, `source_class: derivative`. Type comes from the collection and date from the record's own event-date field, so a death index dates by the death, never by the index's first year. The rule is `recordHints`, shared with the Python recipe's `record_hints` through one case table (`tests/fixtures/capture-samples/ancestry-hints.json`) that both test suites run.
- **Shared census pages.** Household members share one census page. A page saved earlier in the sitting, or one the Seen It add-on reports the archive holds, is not downloaded again; the bundle records `same_image_as` (the record that holds the page, by `rec:` key and address, or the archive source's s-id). `record.imageSaved` says whether the bundle itself holds the scan (`imageId` is only the image the record links to); Seen It counts a page as saved only on it. If the Seen It add-on is installed, the panel asks it one question before that download ("does the archive already hold this census page?") and nothing else. Seen It's own injected badges are never read or saved.
- **Photos shared to a tree** (the media viewer, `/mediaui-viewer/…/media/{mediaId}`). Capture reads the photo's Details as the page shows them and fetches the one image the viewer shows, from the page's own site in your session (`ancMediaImageUrl` builds an address on the page's origin only, and `content.js` checks it again before the fetch), keeping the bytes exactly as sent. The bundle files as a `photo` source whose `external_links` keep the viewer's address without its query, which is the address Seen It knows the photo by (`med:{mediaId}`). The rules:
  - It reads only the photo's own Details box: Ancestry's own panel (`#mediaViewerDetailsPanel`, read by its structure alone, `ancMediaPanelDetails`), else the element whose own heading or aria-label is "Details" (`ancMediaDetailsBoxes`), whole, each field by its own label. From Ancestry's panel it never reads the AI-estimated date or suggested description Ancestry shows in place of an empty field, the "Saved by" members or the comments. Nothing outside the box is read; with no box found, only the photo, its address and the tab's own title are kept.
  - No copy of the page is kept (the page lists other members): the archive keeps the photo and `photo-data.json`, whose fields are only what the Details reader extracts, and the Python recipe reads no people or text from that page. `fha capture --ingest` leaves out a page copy an older add-on made of such a page (it stays parked in the staging folder) and says so in the notes.
  - A photo source is never filed without its picture. When the photo will not come and no file was dropped in, nothing is staged; a shared-photo bundle with no picture is set aside once, in the staging folder's `.not-filed/`.
  - Capture reads the page again first and stages nothing while the page shows Details or linked people the form has not shown you (they are added to the form for you to check), so a photo is captured once. Fields the page filled in and you never changed follow that second read, and a change is shown before anything is staged. While Ancestry's panel still describes the item shown before (its title differs from the alt text of the viewer's own picture, `ancMediaPanelIsFor`), nothing is read and Capture waits; a second press on the same item, or a press with your own file dropped in, stages the picture and its address with no details from the panel (`details: {}`, the title from the picture's alt text unless you typed one).
  - The panel's category sets the form's Kind (`ancMediaKind`): an obituary or newspaper article is a newspaper, a birth, death or marriage certificate a vital record, a property record a land record, another document what its title says (else other), and anything else (portraits, military service, occupation, education, yearbooks, unknown words) a photo. The Kind at Capture decides the filing: a photo's picture is role `front` (photo library), any other kind's is `record` (documents root).
  - **A Newspapers.com clipping saved to the tree** is Ancestry's smaller copy. When the sharer's card links the original ("originally shared this article … from Newspapers.com", `ancMediaNewspapersClip`, read from that card only and only when it names one clipping), a note above step 1 says so, says whether the archive already holds the clipping (Seen It, `clip:{id}`), and offers **Open it on Newspapers.com**. That opens the original in the same tab, where the panel follows it and captures it as any clipping, with the people ticked on Ancestry added to its form. Capturing Ancestry's copy stays possible.

- **Find a Grave Index entries** (collection 60525) index one memorial, which is the better evidence (its photos, the family it links, the volunteer's notes). The entry names it in its own "URL" field (`ancRecordMemorial`, read from `#recordFieldsContainer` only, and only when it names one memorial). The same note above step 1 says so, says whether the archive holds the memorial with its photos (`fag:{id}`) or without them (`fagidx:{id}`), and offers **Open it on Find a Grave**, carrying the ticked people to the memorial's form.

### FamilySearch

On a record (`ark:/61903/1:1:{id}`) or the image viewer opened from one (`personArk=`), the reader (`lib/familysearch-record.js`) reads the record through FamilySearch's own data service (GEDCOM X) with your signed-in session, into `record-data.json` (role `site-data`: the data and its citation, FamilySearch's index of the record, never a transcript), and fetches the full image behind `ark:/61903/3:1:{image}` (its `dist.jpg`) as `record.jpg` (role `record`). A bare image viewer saves that full image the ordinary way.

- **The login rule.** The session (the `fssessionid` login, sent as a Bearer token) goes **only** to https addresses on familysearch.org or its subdomains, never to an address FamilySearch's reply names without that check. It is read fresh from the page for each request, and never stored, logged, or written into a capture (`fsAuthHeaders`, tested in `tests/test-familysearch.js` and `tests/test-content-load.js`).
- Before Capture the panel asks for the image's description and first kilobyte only, to say whether it can be saved from here; a restricted (center-only) image turns the status amber ("the record's data only") and the bundle notes say which image and why.
- Title, type, date and class come from FamilySearch's own data, by the same `recordHints` rule as Ancestry. `record.imageSaved`, `same_image_as`, the always-kept page copy for an image-less bundle, and the Seen It stripping follow the Ancestry reader.

- **Find a Grave Index entries** (collection 2221801): the page does not show the memorial, so it is read from the entry's data (its `ExtRecordId` field, `fsIndexedMemorial`) when the panel checks the record, and the panel offers **Open it on Find a Grave** as on Ancestry. The record keeps `memorialId` only for such an entry.

### Find a Grave

On a memorial (`/memorial/{id}/{slug}`, not its gallery or flower pages) no login is used at all. The reader (`lib/findagrave-record.js`) reads the memorial as the page shows it into `record-data.json` (role `site-data`, with the list of its photos): its name, birth and death dates and places, cemetery (and its id), plot, inscription, gravesite details, bio, the family members it links (relation, memorial id, name, dates), who made it, and Find a Grave's own citation ("accessed" filled with the capture date). It reads the photo list from the memorial's own gallery pages (`…/photo?page=N`, at most 20, `credentials: 'omit'`).

- **Photos.** The panel (`lib/findagrave.js`) downloads each photo at full size as `{kind}-{n}.{ext}` with that role (`grave-1`, `person-1`, `family-1`, `document-1`, `flowers-1`, `cemetery-1`, `other-1`, else `photo-1`). It fetches only from `https://images.findagrave.com/photos/…` (no other host or port, no credentials in the address, redirects never followed with `redirect: 'error'`, an image content type), without cookies or a referrer; at most 40 per capture, one at a time with a 0.8-2 s pause (`fagPhotoUrlOk`, tested in `tests/test-findagrave.js`, `tests/test-panel-findagrave.js` and `tests/test-content-load.js`). The gravestone (else the person) is marked `is_primary` in `capture.json`, which ingest carries into `notes.md` for `fha process`.
- **When the site says slow down.** A 429 (from a photo or a later gallery page) stops the downloads at once, and the bundle says which photos are still to come. A photo list read only in part (a later page failed, fewer listed than Find a Grave's count, or no count and a page it does not recognise) turns the result amber ("Find a Grave shows N photos; M are saved") and records `photosPartial: true`, with `photosTotal` null when the count is not known. Only a count of 0 means "no photos". The next Capture is a capture of its own, to be filed with the memorial's source by hand. A refused or missing photo is named with its cause; a gallery that will not read still saves the memorial; a removed or merged memorial (no `#bio-name`) stages nothing and says to search for the person.
- **Hints** (the shared `recordHints` rule, collection "Find a Grave"): `source_type: website`, `source_class: derivative`, and no `source_date`. A memorial is a volunteer's compilation rather than a vital record, and SPEC §8.5 would otherwise default its claims to high confidence. It was made long after the death, on a day the page does not give, so neither the death nor the birth dates it (SPEC §14: the date of the source itself).
- Each photo in `record-data.json` carries `file` (or null with `same_image_as`, `error` or `skipped`); a photo already saved in the sitting or reported by the Seen It add-on (`fagphoto:{photoId}`, from a saved file only) is not downloaded again. Typed title/date/type/repository win.

### Internet Archive

On a page of a book in the Internet Archive's reader (`archive.org/details/{id}`, with `/page/n20` for the scan at position 20 counting from 0, or `/page/11` for a printed page) no login is used. The panel loads `lib/archiveorg-record.js` (the same file is the canonical reader that `content.js` copies for the address) and asks archive.org's own hosts, without cookies or a referrer, for:

- **The book.** `/metadata/{id}`: title, author (the catalogue's "[Mason, Jesse D] [from old catalog]" is written "Jesse D. Mason"), publisher, date, volume and page count. An item that is not a text, or is gone, stops the capture with the reason.
- **The printed page.** `{id}_page_numbers.json` says which printed page each scan carries. BookReader rewrites `n20` to `11` as the page loads, so a bare number in the address is placed through the same list. A book without the list still saves, and the citation says "scan n20 (printed page not known)".
- **The page image.** `iiif.archive.org/iiif/{id}${leaf}/full/full/0/default.jpg`, saved as `page-1.jpg` (role `page-1`; `page-2` and on for the next pages). Leaf numbers count from 1, so the scan at `/page/n20` is leaf 21. A lending-library book refuses its images: the panel says to borrow it, and saves the details and a page copy.
- **The page's text.** `{id}_hocr_pageindex.json.gz` gives the byte range of each leaf in `{id}_hocr.html`, so one page's text is one small range request and never the whole book (an answer that is not a 206 is not read). It is saved as `transcript.md` (role `transcript`) under one `<!-- AI-DRAFT Internet Archive OCR, unchecked, captured … -->` marker. A book without the index saves the image alone and says so.
- **More than one page.** The "Pages to save" box offers this page, or this page and the next one, two or three (at most four), one image after another with a short pause.
- **Citation and hints.** `{author}, {title} ({publisher}, {date}), p. {printed}; digital images, Internet Archive ({link} : accessed {date}).` The link keeps the form of the address she was looking at. `source_type: book`, `source_class: derivative`, and `source_date` the publication year when the date is one single year.
- `record-data.json` (role `site-data`) holds the book's details and, for each page, its scan number, printed page, address, file and whether its text was saved.

The whole book's text or PDF is not saved yet; that waits for a new file role.

### Newspapers.com

Opening the panel on a clipping or a page reads the page's own embedded data (each clipping's rectangle, the page size, a day-long page pass) and the page's list of clippings, using the same request its viewer makes, at most a few pages of 25 (`lib/newspapers-page-data.js`). It offers the opened clipping whoever made it, plus your own clippings on the page, never one that marks no area to cut out.

On Capture, the panel (`lib/newspapers.js`) rebuilds that newspaper page at 1:1 from the site's own image tiles, one tile at a time with short random gaps (0.2-0.7 s), backing off only on the site's 429/503 "slow down" answers and stopping at once with the real cause on any other refusal, and cuts out each clipping you ticked (one more request each, for its text). The site's own *Download PDF* is a 2048 px JPEG, a quarter of a broadsheet scan, which is why the page is rebuilt.

It stages **one bundle per clipping** (separately published items never share an s-id, SPEC §7): `clipping.jpg` (role `clipping`), `page.jpg` (`page`), `annotated.jpg` (`annotated`, the page with the item boxed, a capture-time derivative and never the newspaper), and `clipping-data.json` (`site-data`: details, the site's four citation styles, and its machine OCR, kept as `.json` so it is never searched or read as a transcript). The site's Genealogy-style citation goes in `capture.json` `citation`. The tab's page copy goes only with the bundle for the record on screen (on a clipping page, that clipping), and the closing message names where each bundle went and which, if any, failed.

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
    bundle.js          writes the bundle via chrome.downloads (TOOLING_INGESTION §5.1)
    native-host.js     optional direct-to-archive path (§5.7), opt-in (off by default)
    srcset.js          HTML-spec srcset parsing (canonical; content.js keeps a copy)
    iiif.js            IIIF Image-API URL rewriting (canonical; ditto)
    people-harvest.js  JSON-LD person harvest (canonical; ditto)
    capture-readiness.js  the "record detail looks empty" phrases (canonical; ditto)
    newspapers-page-data.js  reads a Newspapers.com page's embedded data (canonical; ditto)
    ancestry-record.js  reads an Ancestry record page: fields, household, citation, scan, hints;
                       and a shared photo's viewer page: its id, image and Details (canonical; ditto)
    familysearch-record.js  a FamilySearch record from its data service: login rule, record, image, hints (canonical; ditto)
    findagrave-record.js  a Find a Grave memorial: its facts, family, citation, photo list, photo-host rule, hints (canonical; ditto)
    newspapers.js      rebuilds a Newspapers.com page from tiles; clipping, annotated view, citations
    findagrave.js      downloads a memorial's photos from Find a Grave's image host (panel side)
    archiveorg-record.js  a page of an Internet Archive book: address, details, printed page, image and text requests, citation (canonical for the address block; the panel loads the whole file)
test-bundle/           example bundles in the exact output shape (round-trip tests): a "both"
                       census capture, and a shared Ancestry photo the panel tests stage byte for byte
```

content.js is an injected classic script, so it cannot `import`: the eight canonical modules above have hand-kept copies inside it, and `tests/test-sync.js` re-runs both sides through the same battery, so a drift fails the build.

**Who extracts what.** The site readers above read the sites in the browser, where your session is, and save what they read as bundle files. The Python recipe (`tools/capture_recipes/`) reads the saved `page.html` again at ingest for the citation fields and the people, and where both derive the same hint they share one rule and one case table. What you typed in the form wins over both.

### How files reach the archive (TOOLING_INGESTION §5.1)

An MV3 extension cannot write to an arbitrary path. Its only way to write a file is `chrome.downloads.download()`, which writes under the browser's Downloads directory. So the default path stages to `Downloads/fha-inbox/…` and you run `fha capture --ingest` to do the one sanctioned *move* into the archive. The optional native-messaging host (§5.7) removes the Downloads detour: the Python side is `fha capture --host` / `--install-host`, and the extension side is opt-in (OFF by default, behind the "file straight into my archive" toggle), falling back to the staging path when no host answers.

---

## Design notes (for the maintainer)

- **`capture.json` is schema 2: an `assets:` list,** `[{file, role, mode, provisional?, is_primary?}]`, so one capture can carry **both** a page snapshot (role `webpage`) and a separate evidence file (role `record`). Ingest also reads the schema-1 flat `asset_mode`/`asset_file` pair, and routes multi-asset captures to a SPEC §12.1 bundle folder. TOOLING_INGESTION §3 is the contract; this README and the test-bundle are the worked example.
- **The provisional flag is recorded twice.** A flagged screen capture sets `assets[].provisional: true` AND prepends a `[provisional image, …]` line to your `notes`, so review sees it whether or not a tool reads the structured flag.
- **The handoff command names only a location the browser reported.** The download directory is a browser setting (moved to OneDrive, to a second volume, or carrying a localized name), so a home-relative `Downloads` path built from the staging-folder setting would be a guess presented as fact, sending `--ingest` to a folder the bundle was never written to. The panel reads the completed download's own absolute path (`chrome.downloads.search` → `DownloadItem.filename`), drops the file and bundle segments to get the folder that holds the bundles, and puts THAT in the command. With no reported path (before the first capture of a sitting, or a path that cannot be quoted into a shell command) the directory-less `./fha capture --ingest` stands, and a hint line points at the browser's own download setting and at `fha.yaml`'s `capture_staging:`, never at an invented path.
- **The copied command carries the launcher prefix the machine needs.** `fha` is a launcher FILE at the archive root, never a program on PATH (AGENTS.md "Execution rules"), so a bare `fha …` is command-not-found for everyone except a Windows Command Prompt user. `capture-json.js` `launcher()` reads the OS off `navigator.userAgentData.platform` (falling back to `navigator.platform`, then the UA string) and renders `./fha` or `.\fha`; unknown falls to `./fha`, which PowerShell also accepts. The card holds ONE string rather than the docs' three-shell block because that string is what the Copy button puts on the clipboard, and Windows gets the PowerShell spelling because `.\fha` resolves through PATHEXT in cmd.exe too. Every other command the panel prints (the native-host hints, the fallback warning) goes through the same prefix.
- **Single-file snapshot is minimal but scrape-able.** It inlines images and stylesheet text, drops **executable** scripts but **keeps `<script type="application/ld+json">` and other non-executable metadata** so the snapshot stays parseable; it does **not** inline fonts or nested CSS `url()` resources, and it is bounded (≤120 resources, ≤5 MB each). `page.html` is still saved alongside as the clean scrape source, so scraping never depends on it.
- **Every URL the snapshot keeps is anchored to the live page.** Relative references are rewritten to their absolute form rather than by injecting a `<base>` (a base of ours would send fragment-only links such as `#facts` to the live site and blank out SVG `<use href="#icon">` sprites). A page that declares its **own** `<base>` keeps it, since that is the author's baseline and it already governs the live page. It is written in the form the browser itself resolved (`document.baseURI`), never by resolving the raw `href` a second time: a path-relative `<base href="records/">` is *already* folded into `baseURI`, and re-resolving it would save `…/records/records/` and quietly re-point every reference one directory too deep. The sweep covers `href`/`src`/`poster`/`data`, `srcset` and `imagesrcset`, `form action` and `formaction`, SVG `href` and `xlink:href`, inline `style` attributes, `<style>` blocks, and the `url()`/`@import` references inside an inlined stylesheet (anchored to the **stylesheet's** address rather than the document's). `srcset` is parsed by the HTML Standard's algorithm, never split on commas, because a candidate URL may legally contain one. Nothing that resolves to a `file:` URL is ever written into a snapshot: capturing a page opened from disk must not bake local folder names into a file that lands in the archive.
- **Anything that would navigate away is disarmed instead of anchored.** Absolutizing is right for a *resource* the saved page needs. It is wrong for anything that acts on the reader's behalf, because it turns a harmlessly broken directive into a working one. A `<meta http-equiv="refresh" content="0;url=/login">` anchored to the live site would bounce the reader out of the snapshot and onto a login page the moment they opened it, and the preserved evidence could never be read offline at all. So refreshes (including any hiding inside `<noscript>`), captured `Content-Security-Policy` pragmas (which would forbid the very `data:` images and `<style>` blocks the snapshot inlined), inline `on…` handlers, `<a ping>` beacons, and speculative `<link rel="preload|prefetch|prerender|preconnect|dns-prefetch">` fetches are all **disarmed**: the element and its value stay, moved onto a `data-fha-disabled-…` attribute no browser acts on, plus `data-fha-refresh-target` recording where a refresh pointed. An `<iframe>` keeps its live `src` (a framed record viewer is often the evidence) but gains a `sandbox` without `allow-top-navigation`, so a frame-busting script cannot do one level down what the refresh would have done. Following a link or submitting a form is still the reader's own click, so `<a href>` and `<form action>` are left anchored.
- **Markup hiding inside an attribute or a text node gets the same treatment.** Every sweep above walks *elements*, and two places in a captured page hold markup that never became one: an `<iframe srcdoc="…">` (a whole second document stored in an attribute value, parsed and run the instant the snapshot is opened) and a `<noscript>` (one raw text node while scripting is on, live markup again for a reader who opens the file with JavaScript off). Both are rewritten as strings: scripts keep their text but are given a type no browser runs (`text/fha-disabled-script`, the author's own type parked alongside), `on…` handlers, `ping` beacons, refreshes, captured CSPs and speculative `<link>`s are disarmed exactly as above, and a nested `<iframe>` gets the same sandbox. A `srcdoc` frame is also denied `allow-scripts`: its document is markup the snapshot carries and has already disarmed, so nothing legitimate is left in it to run, and anything the string pass failed to recognise stays inert. That sandbox binds every frame nested inside it at any depth (sandbox flags are inherited and can only narrow), which is what bounds the rewrite to one level of markup. `srcdoc` references are anchored like the outer document's, because an `about:srcdoc` document inherits the *parent's* base URL and left alone they would resolve into whatever folder the snapshot was opened from. `<noscript>` is left byte for byte, since absolutizing in there would arm the tracking pixel those blocks most often carry.
- **No print-to-PDF mode.** The single-file HTML snapshot covers case (b), and a real PDF files through the "Yes, save the actual file" path (paste its url, or drop the PDF).
- **No bookmarklet.** A `javascript:` bookmark can only trigger a single combined `.html` download, never the staged-bundle folder `--ingest` consumes; the paste path (`pbpaste | fha capture …`) is the zero-install floor.

---

## Testing

There is no browser-driven test harness in this repo (it would need a real DOM). Two suites cover everything that *can* be checked without one.

**The pure JS helpers** run under `node --test` (Node ≥18, no dependencies): srcset parsing, the snapshot's URL anchoring, the JSON-LD person harvest, IIIF rewriting, `capture.json` building, the Newspapers.com page-data parsers and tile/report logic, the Ancestry record readers (run over the same scrubbed fixture pages the Python recipe tests read, through the small HTML reader in `tests/mini-dom.js`), and the content.js ↔ `src/lib/` sync guards. `tests/test-panel-newspapers.js`, `tests/test-panel-ancestry.js`, `tests/test-panel-familysearch.js`, `tests/test-panel-findagrave.js`, `tests/test-panel-archiveorg.js` and `tests/test-panel-ux.js` (the Re-read buttons, tab binding and the fetch log) also drive `panel.js` itself against a stand-in for the panel page and a scripted content script (`tests/panel-harness.js`), so those capture flows (and the panel opening at all) are checked without a browser. `tests/test-content-load.js` loads the whole of `content.js` in a sandbox page and drives its message listener. The pixels and the live sites still need a hand test in a loaded extension.

```sh
npm --prefix browser-companion test
```

**The end-to-end contract**, that the extension's output bundle ingests cleanly, is covered by [`../tests/test_browser_companion.py`](../tests/test_browser_companion.py). It validates the MV3 manifest, asserts every file the manifest references exists, pins the snapshot's URL-rewriting invariants, and runs the example `test-bundle/` (which mirrors the exact shape `panel.js`/`bundle.js` write) through `fha capture --ingest` end to end. Run it from the repo root:

```sh
python -m unittest tests.test_browser_companion -v
```

Hand-testing the live extension: load it unpacked, open a record page, capture in each mode, then `fha capture --ingest --dry-run` against your Downloads folder to confirm each bundle is recognized.
