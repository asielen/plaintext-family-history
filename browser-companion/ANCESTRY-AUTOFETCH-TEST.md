# Hand-test guide: Ancestry full-resolution image auto-fetch

This is the manual test plan for the one-click full-size image fetch on Ancestry image-viewer pages. The design choice behind it: a fetch the user starts, in their own session, one record at a time, equivalent to automating a single click on Ancestry's own Download button. It never scrapes in bulk.

There is no browser harness in this repo, so this path is **hand-tested**. The automated test (`python -m unittest tests.test_browser_companion`) only checks the manifest and the bundle round-trip; it does not exercise a logged-in Ancestry session. Everything below requires you to be **signed in to Ancestry** in the browser you load the extension into.

---

## What it does

On an Ancestry **image-viewer** page (`/imageviewer/collections/{dbId}/images/{imageId}?…&pId=…`), the panel's "Yes, save the actual file" option fetches the full-resolution, server-assembled record image **in your own session** when you press Capture. That is the same image Ancestry's `Tools → Download` button hands back. It does this by calling Ancestry's media-token endpoint and then the download URL it returns, one record at a time, only when you click Capture. It never stitches tiles, and it **never** silently files the small preview thumbnail in place of the scan (the failure the EX7 capture showed; see Assumptions).

---

## Load the extension (unpacked)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and choose the `browser-companion/` folder in this repo (the one with `manifest.json`).
4. Pin **"Plaintext Family History (Capture)"** to the toolbar.
5. Make sure you are **logged in to ancestry.com** in this browser profile.

No new permissions are needed: the in-session same-origin fetch runs under the existing `activeTab` + `scripting` + `host_permissions: <all_urls>` grant. (If Chrome shows new permissions on reload, something regressed; the manifest was not meant to change.)

---

## Test 1: logged in, downloadable collection

**Page to open** (must be the IMAGE VIEWER, not the records/landing page):

```
https://www.ancestry.com/imageviewer/collections/{dbId}/images/{imageId}?…&pId={pId}
```

Good candidates from the capture corpus (open the actual record while signed in):

- 1940 U.S. Census: `dbId=2442`, image `m-t0627-00331-00237`
- California Divorce Index: `dbId=1141`, image `vdvusaca1966_0105_06_n-0089` (a hand download of this one, EX8, gave a 3040×2624, ~860 KB JPEG)

**Steps**

1. With the record open in the viewer, click the toolbar button to open the panel.
2. The banner should read **"Looks like an Ancestry record. Filing will confirm it."**
3. In **Step 2 → "Yes, save the actual file"** you should see the green **"Save the full record image (Ancestry)"** note. The "File address on the page" box should be **hidden**: the file comes by itself here, so there is nothing to type, and the thumbnail is never offered as the address.
4. The asset-status line at the bottom of Step 2 should show **"Record file: full record (Ancestry, auto)"**.
5. Click **Capture & save**.

**Expected result**

- The bundle stages to `Downloads/fha-inbox/<slug>-<timestamp>-<token>/` containing `page.html`, `page-snapshot.html` (if the page-copy toggle is on), `record.jpg`, and `capture.json`.
- **`record.jpg` is the full record**: hundreds of KB to several MB, thousands of pixels on a side (e.g. ~860 KB, 3040×2624 for the divorce-index page). **It must NOT be a ~45 KB / 507×600 thumbnail.** Open it to confirm it is the readable full scan.
- In `capture.json`, the record asset reads `{ "file": "record.jpg", "role": "record", "mode": "ancestry-api" }`.

Then verify ingest still works:

```sh
fha capture --ingest --dry-run     # from your archive, points at Downloads/fha-inbox
```

It should recognize the bundle (a "both" bundle → an inbox bundle folder).

---

## Test 2: fallback when NOT logged in (401/403)

1. Sign **out** of Ancestry (or open the same viewer URL in a guest/incognito profile that has the extension loaded but no Ancestry session).
2. Open the image-viewer page (you may only see a teaser; that's fine).
3. Open the panel, leave "Yes, save the actual file" selected, click **Capture**.

**Expected result**

- The capture **does not hard-fail.** If "Keep a copy of the whole page" is on, the bundle still stages (page copy + `page.html` + `capture.json`), and the status line turns amber. Its message says the full record image was not saved because Ancestry refused (HTTP 401/403), says to sign in on the page or that the collection may not allow downloads, and points at Ancestry's Download button.
- No `record.jpg` is written, and **no thumbnail is filed** in its place.
- The green "Saved" handoff card still appears (the page copy is safe in Downloads).
- Because the automatic download failed, the "File address on the page" box comes back so an address can be pasted.

> Edge case: if you had **un-ticked** the page copy too (so there was nothing else to stage), the capture shows the same error as a red message and stages nothing. That is correct, because the bundle would be empty. Re-tick the page copy, or drop the file in, and Capture again.

---

## Test 3: fallback for a collection with downloads disabled

Some collections or account tiers disable `download=True`; the token endpoint returns no `imageDownloadUrl` (or an error). Pick such a record if you have one.

**Expected result**

- The page copy still stages, as in Test 2, and the status line reports *"Ancestry did not offer a downloadable image for this record (downloads may be disabled for this collection)"*. No thumbnail is filed.
- **The manual fallback works:** click Ancestry's own `Tools → Download`, then drop the downloaded file into the panel's drop box (or paste an image address into the box that came back) and Capture. That path produces `record.jpg` (mode `manual` for a dropped file).

---

## Test 4: the thumbnail-size guard

If you can reach an image-viewer page where the download URL returns something small (rare; this is mainly a safety backstop), the fetch is rejected with *"the image came back too small (NN KB) to be the full record"* rather than filing the small image as the record. The guard should never quietly accept a preview-sized image as `role: record`.

---

## Test 5: other pages are untouched (regression)

Open a record page on a site with no site reader (a library catalogue entry or a county records page with an image) and confirm:

- No green Ancestry note appears.
- The "File address on the page" box shows and pre-fills with the detected image/PDF (placeholder *"image or PDF address"*).
- "Yes, save the actual file" still requires a URL or a dropped file (it does not claim an auto record), and Capture pulls the URL via the normal `fetch` path.

On an Ancestry record page (NOT `/imageviewer/`), the image-viewer path above does not run; the record-page reader handles it instead (see the Ancestry section of README.md). The image-viewer path only triggers on a parseable `/imageviewer/.../images/...` URL.

---

## Assumptions that need live-Ancestry verification

These were derived from captured HAR files (EX5, EX7 and EX8 in the capture corpus) and the code. They cannot be confirmed without a logged-in session, which was not available when the code was written:

1. **Token endpoint path & response shape.** Assumes a same-origin GET to `/imageviewer/api/media/token?dbId=&imageId=&pId=` returns JSON with an `imageDownloadUrl` field (a site-relative `/api/media/retrieval/v2/image/...?securitytoken=…&download=True…` path). The code reads `imageDownloadUrl` (case-tolerant of `imagedownloadurl`) and resolves relative or absolute addresses. A different field name would show up as the "did not offer a downloadable image" fallback rather than a crash.
2. **`pId` is sometimes optional.** The token request includes `pId` only when the page URL has it. Verify the endpoint accepts the request when `pId` is present (all corpus examples had it); the code omits the param if absent.
3. **`imageId` parsing.** Assumes `imageId` is the single path segment after `/images/` up to `?`/`#`, may contain hyphens, underscores, and dots, and is `decodeURIComponent`'d once. This matches the EX5/EX7/EX8 shapes; verify no collection uses a slash inside the image id (which would split the segment).
4. **`credentials:'include'` carries the session.** Assumes the logged-in cookies ride both same-origin GETs (token + download). This is standard for same-origin requests from a content script on `www.ancestry.com`; verify the assembled JPEG actually comes back full-res and not a login redirect page (the size guard catches a redirect/HTML body as "too small").
5. **Full-res threshold.** The thumbnail guard rejects anything under **80 KB**. EX7's thumbnail was ~45 KB and EX8's full scan ~860 KB, so 80 KB separates them cleanly. Verify a small but real scan never trips it, and raise the floor only if a real scan is ever seen under ~80 KB (which would be unusual).

If any of these differ live, the worst case is a clear panel message and the manual download path. A thumbnail is never silently filed, and a capture with a page copy requested never fails outright.
