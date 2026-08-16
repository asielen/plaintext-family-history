# Hand-test guide: Ancestry full-resolution image auto-fetch

This is the manual test plan for the **seamless, one-image-at-a-time, full-res
auto-fetch** on Ancestry image-viewer pages (the owner's "Option A": a
user-initiated, in-session fetch equivalent to automating a single click on
Ancestry's own Download button - not mass scraping).

There is no browser harness in this repo, so this path is **hand-tested**. The
automated test (`python -m unittest tests.test_browser_companion`) only checks the
manifest and the bundle round-trip; it does not exercise a logged-in Ancestry
session. Everything below requires you to be **signed in to Ancestry** in the
browser you load the extension into.

---

## What it does (in one breath)

On an Ancestry **image-viewer** page (`/imageviewer/collections/{dbId}/images/{imageId}?…&pId=…`),
the panel's "Yes, save the actual file" option fetches the full-resolution,
server-assembled record image **in your own session** when you press Capture - the
same image Ancestry's `Tools → Download` button hands back. It does this by
calling Ancestry's media-token endpoint and then the download URL it returns, one
record at a time, only when you click Capture. It never stitches tiles, and it
**never** silently files the small preview thumbnail (the old EX7 trap).

---

## Load the extension (unpacked)

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and choose the `browser-companion/` folder in this repo
   (the one with `manifest.json`).
4. Pin **"Plaintext Family History (Capture)"** to the toolbar.
5. Make sure you are **logged in to ancestry.com** in this browser profile.

No new permissions are needed - the in-session same-origin fetch runs under the
existing `activeTab` + `scripting` + `host_permissions: <all_urls>` grant. (If
Chrome shows new permissions on reload, something regressed; the manifest was not
meant to change.)

---

## Test 1 - the happy path (logged in, downloadable collection)

**Page to open** (must be the IMAGE VIEWER, not the records/landing page):

```
https://www.ancestry.com/imageviewer/collections/{dbId}/images/{imageId}?…&pId={pId}
```

Good candidates from the capture corpus (open the actual record while signed in):

- 1940 U.S. Census - `dbId=2442`, image `m-t0627-00331-00237`
- California Divorce Index - `dbId=1141`, image `vdvusaca1966_0105_06_n-0089`
  (EX8 proved this one yields a 3040×2624 / ~860 KB JPEG by hand)

**Steps**

1. With the record open in the viewer, click the toolbar button to open the panel.
2. The banner should read **"Looks like an Ancestry record."**
3. In **Step 2 → "Yes, save the actual file"** you should see the green
   **"Save the full record image (Ancestry)"** note. The "File address on the page"
   box should be **empty** (it is intentionally NOT pre-filled with the thumbnail),
   and its placeholder reads *"leave blank to auto-save the full record…"*.
4. The asset-status line at the bottom of Step 2 should show
   **"Record file: full record (Ancestry, auto)"**.
5. Click **Capture & save**.

**Expected result**

- The bundle stages to `Downloads/fha-inbox/<slug>-<timestamp>-<token>/` containing
  `page.html`, `page-snapshot.html` (if the page-copy toggle is on), `record.jpg`,
  and `capture.json`.
- **`record.jpg` is the full record** - hundreds of KB to multiple MB, multiple
  thousand pixels on a side (e.g. ~860 KB, 3040×2624 for the divorce-index page).
  **It must NOT be a ~45 KB / 507×600 thumbnail.** Open it to confirm it is the
  readable full scan.
- In `capture.json`, the record asset reads
  `{ "file": "record.jpg", "role": "record", "mode": "ancestry-api" }`.

Then verify ingest still works:

```sh
fha capture --ingest --dry-run     # from your archive, points at Downloads/fha-inbox
```

It should recognize the bundle (a "both" bundle → an inbox bundle folder).

---

## Test 2 - fallback: NOT logged in (401/403)

1. Sign **out** of Ancestry (or open the same viewer URL in a guest/incognito
   profile that has the extension loaded but no Ancestry session).
2. Open the image-viewer page (you may only see a teaser, that's fine).
3. Open the panel, leave "Yes, save the actual file" selected, click **Capture**.

**Expected result**

- The capture **does not hard-fail.** If "Keep a copy of the whole page" is on,
  the bundle still stages (page copy + `page.html` + `capture.json`), and the
  status line turns amber:
  *"…The full record image was not saved: Ancestry refused (HTTP 401/403) - sign in
  on this page, or the collection may not allow downloads. You can use Ancestry's
  Download button and drop the file in…"*
- No `record.jpg` is written, and **no thumbnail is filed** in its place.
- The green "Saved" handoff card still appears (the page copy is safe in Downloads).

> Edge case: if you had **un-ticked** the page copy too (so there was nothing else
> to stage), the capture surfaces the same error as a red message and stages
> nothing - correct, because there would be no bundle worth keeping. Re-tick the
> page copy, or drop the file in, and Capture again.

---

## Test 3 - fallback: collection with downloads disabled

Some collections / account tiers disable `download=True`; the token endpoint
returns no `imageDownloadUrl` (or an error). Pick such a record if you have one.

**Expected result**

- Same graceful behavior as Test 2: the page copy still stages; the status line
  reports *"Ancestry did not offer a downloadable image for this record (downloads
  may be disabled for this collection). You can use Ancestry's Download button and
  drop the file in…"*. No thumbnail filed.
- **Manual fallback works:** click Ancestry's own `Tools → Download`, then in the
  panel drop the downloaded file into the drop box (or paste an image address) and
  Capture - that path is unchanged and should produce `record.jpg` (mode `manual`
  for a dropped file).

---

## Test 4 - the thumbnail-size guard

If you can reach an image-viewer page where the download URL returns something
small (rare; mainly a safety backstop), the fetch is rejected with
*"the image came back too small (NN KB) to be the full record…"* rather than
filing the small image as the record. This is the EX7-trap guard; it should never
quietly accept a preview-sized image as `role: record`.

---

## Test 5 - non-Ancestry pages are untouched (regression)

Open any non-Ancestry record page (e.g. a Newspapers.com clipping or a Find A Grave
memorial) and confirm:

- No green Ancestry note appears.
- The "File address on the page" box pre-fills with the detected image/PDF as
  before (placeholder *"image or PDF address"*).
- "Yes, save the actual file" still requires a URL or a dropped file (it does not
  claim an auto record), and Capture pulls the URL via the normal `fetch` path.
- A records/landing Ancestry page (NOT `/imageviewer/`) also behaves the
  pre-existing way - the auto path only triggers on a parseable
  `/imageviewer/.../images/...` URL.

---

## Assumptions that need live-Ancestry verification

These were derived from the captured HARs (EX5/EX7/EX8) and the existing code, but
cannot be confirmed without a logged-in session (the implementer could not test
that):

1. **Token endpoint path & response shape.** Assumes a same-origin GET to
   `/imageviewer/api/media/token?dbId=&imageId=&pId=` returns JSON with an
   `imageDownloadUrl` field (a site-relative `/api/media/retrieval/v2/image/...?securitytoken=…&download=True…`
   path). If Ancestry changed the field name or made it absolute, the code reads
   `imageDownloadUrl` (case-tolerant of `imagedownloadurl`) and resolves relative
   vs. absolute - but a different field name would surface as the "did not offer a
   downloadable image" fallback, not a crash.
2. **`pId` is sometimes optional.** The token request includes `pId` only when the
   page URL has it. Verify the endpoint accepts the request when `pId` is present
   (all corpus examples had it); the code omits the param if absent.
3. **`imageId` parsing.** Assumes `imageId` is the single path segment after
   `/images/` up to `?`/`#`, may contain hyphens, underscores, and dots, and is
   `decodeURIComponent`'d once. Confirmed against EX5/EX7/EX8 shapes; verify no
   collection uses a slash inside the image id (which would split the segment).
4. **`credentials:'include'` carries the session.** Assumes the logged-in cookies
   ride both same-origin GETs (token + download). This is standard for same-origin
   requests from a content script on `www.ancestry.com`; verify the assembled JPEG
   actually comes back full-res and not a login redirect page (the size guard
   catches a redirect/HTML body as "too small").
5. **Full-res threshold.** The thumbnail guard rejects anything under **80 KB**.
   EX7's thumbnail was ~45 KB and EX8's full scan ~860 KB, so 80 KB cleanly
   separates them - but verify a genuinely small-but-real record page never trips
   it (raise the floor only if a real scan is ever seen under ~80 KB, which would
   be unusual).

If any of these differ live, the fallback chain means the worst case is a clear
panel message and the manual download path - never a silently-filed thumbnail and
never a failed capture when a page copy was requested.
