// content.js - the in-page worker (TOOLING_INGESTION §5.5).
//
// This is injected on demand (via chrome.scripting, gated by `activeTab`) only
// when the human invokes the companion - never ambient page access (§5.4, §7).
// It does the work that must happen *inside the page's own session*:
//
//   • read the DOM for a light, GENERIC pre-fill (title/canonical/dates/people/
//     image) - never per-site parsing; the authoritative Ancestry/FamilySearch/…
//     extraction stays in the Python recipes, which re-run on page.html at ingest
//     (§5.5: "the browser captures; Python extracts").
//   • serialize page.html - the always-saved raw DOM the recipe re-extracts from.
//   • fetch a case-(a) asset in the page's session (`credentials: 'include'`) so a
//     login-gated image the human can already see comes through (§5.6).
//   • build a case-(b) single-file snapshot: a minimal images+CSS inliner so the
//     saved page survives link rot instead of decaying to broken-image
//     placeholders (§5.6, the SingleFile approach).
//
// It is a classic content script (no ES imports - injected scripts run in the
// page's isolated world), self-contained, and idempotent: a second injection
// re-uses the already-registered listener instead of stacking handlers.

(function () {
  // Guard against double-injection: invoking the panel twice on one page must
  // not register two message listeners (which would send two responses and
  // trip "message channel closed" errors).
  if (window.__fhaCaptureInjected) return;
  window.__fhaCaptureInjected = true;

  // Bounds for the single-file inliner so an image-heavy page can't spin
  // forever or bloat the snapshot past what a download can hold. Honest limits,
  // not silent perfection (§5.6 caveat: base64 bloat + CORS-locked sub-resources).
  const SINGLEFILE_MAX_RESOURCES = 120;
  const SINGLEFILE_MAX_BYTES_PER_RESOURCE = 5 * 1024 * 1024; // 5 MB

  // ── helpers ────────────────────────────────────────────────────────────────

  function absUrl(href) {
    if (!href) return null;
    try {
      return new URL(href, document.baseURI).href;
    } catch (e) {
      return null;
    }
  }

  function metaContent(...selectors) {
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      const val = el && (el.getAttribute('content') || el.getAttribute('value'));
      if (val && val.trim()) return val.trim();
    }
    return null;
  }

  function hostnameOf(url) {
    try {
      const h = new URL(url).hostname.toLowerCase();
      return h.startsWith('www.') ? h.slice(4) : h;
    } catch (e) {
      return '';
    }
  }

  // The browser's *guess* at which Python recipe will claim this page. The engine
  // still runs its own detection on page.html and may overrule this, so a wrong
  // guess costs nothing (§5.5) - it only drives the Phase-1 "Looks like…" line.
  // The names match capture_recipes/*.py SOURCE_NAME values for a tidy hand-off.
  function recipeHint(url) {
    const host = hostnameOf(url);
    if (host.includes('ancestry.')) return 'ancestry';
    if (host.includes('familysearch.org')) return 'familysearch';
    if (host.includes('newspapers.com')) return 'newspapers';
    if (host.includes('findagrave.com')) return 'findagrave';
    return null;
  }

  // Light, generic person-name harvest from structured data only. We deliberately
  // do NOT scrape arbitrary "name-ish" DOM text - that is noisy and is the Python
  // recipe's job. JSON-LD Person/name and itemprop=name under a Person scope are
  // high-signal and site-neutral, so they make a good optional pre-fill the human
  // can untick (§5.3 Phase 2). Empty is a fine result; the human types the name.
  //
  // Non-people guard (05-A): exclude entities whose @type is a Place/Organization
  // family type or that carry address/geo properties (these are structural markers
  // of venues, not individuals). Skip BreadcrumbList/ListItem names entirely.
  // This is intentionally generic — the same fix improves every site that mixes
  // people and place/org structured data (not just Find a Grave).

  // Types that must never yield a name in the people harvest, regardless of
  // whether they also carry a `name` property.
  const NON_PERSON_TYPES = new Set([
    'Place', 'LocalBusiness', 'Organization', 'Cemetery',
    'LandmarksOrHistoricalBuildings', 'TouristAttraction',
    'BreadcrumbList', 'ListItem',
    // Broad schema.org Place subtypes encountered in the wild:
    'City', 'Country', 'State', 'AdministrativeArea',
    'CivicStructure', 'PlaceOfWorship', 'Museum', 'Park',
    'Hospital', 'School', 'CollegeOrUniversity',
  ]);

  // Returns true when a JSON-LD node's @type indicates a non-person entity.
  function isNonPersonLdNode(node) {
    const type = node['@type'];
    const types = Array.isArray(type) ? type : (type ? [type] : []);
    if (types.some((t) => NON_PERSON_TYPES.has(t))) return true;
    // Also treat nodes with address/geo sub-objects as places even when @type
    // is absent or set to something generic like "Thing".
    if (node.address || node.geo) return true;
    return false;
  }

  function harvestPeople() {
    const names = [];
    const seen = new Set();
    const add = (n) => {
      if (typeof n !== 'string') return;
      const name = n.trim().replace(/\s+/g, ' ');
      if (name.length < 2 || name.length > 80) return;
      const key = name.toLowerCase();
      if (seen.has(key)) return;
      seen.add(key);
      names.push(name);
    };
    const walkLd = (node) => {
      if (!node || typeof node !== 'object') return;
      if (Array.isArray(node)) {
        node.forEach(walkLd);
        return;
      }
      // Skip the entire subtree if this node is a non-person entity type.
      // BreadcrumbList/ListItem nodes and Places/Orgs are excluded; we do NOT
      // recurse further into them so their nested `name` props don't leak out.
      if (isNonPersonLdNode(node)) return;

      const type = node['@type'];
      const isPerson = type === 'Person' ||
        (Array.isArray(type) && type.includes('Person'));
      if (isPerson) {
        if (typeof node.name === 'string') add(node.name);
        else if (node.givenName || node.familyName) {
          add([node.givenName, node.familyName].filter(Boolean).join(' '));
        }
      }
      // Recurse into nested graphs / related entities.
      for (const key of Object.keys(node)) {
        if (key === '@context') continue;
        walkLd(node[key]);
      }
    };
    document.querySelectorAll('script[type="application/ld+json"]').forEach((s) => {
      try {
        walkLd(JSON.parse(s.textContent));
      } catch (e) {
        /* malformed JSON-LD is common; skip it silently */
      }
    });
    // Microdata harvest: restrict to Person itemscopes only, exclude any
    // itemscope that is itself inside a non-person container (BreadcrumbList,
    // Place, Organization). A flat querySelectorAll already does the right
    // thing when scoped to schema.org/Person — but strip any matched element
    // that lives inside a BreadcrumbList or Place itemscope to be safe.
    document
      .querySelectorAll('[itemtype$="schema.org/Person"] [itemprop="name"]')
      .forEach((el) => {
        // Walk ancestors: reject if any contains a non-person itemtype.
        let ancestor = el.parentElement;
        while (ancestor) {
          const atype = (ancestor.getAttribute('itemtype') || '').split('/').pop();
          if (atype && NON_PERSON_TYPES.has(atype)) return; // skip
          ancestor = ancestor.parentElement;
        }
        add(el.textContent);
      });
    return names.slice(0, 12); // a pre-fill list, not an exhaustive index
  }

  // Best-guess "the image this record centers on" for the Phase-3 (a) pre-fill.
  // og:image is the page's own declared hero; otherwise the largest rendered
  // <img>. Either way it is only a *suggestion* the human can replace or clear.
  function detectImage() {
    const og = metaContent('meta[property="og:image"]', 'meta[name="og:image"]');
    if (og) return absUrl(og);
    let best = null;
    let bestArea = 0;
    for (const img of document.images) {
      const w = img.naturalWidth || img.width || 0;
      const h = img.naturalHeight || img.height || 0;
      const area = w * h;
      // Ignore tiny chrome/sprites; favor something record-sized.
      if (area > bestArea && w >= 200 && h >= 200) {
        bestArea = area;
        best = img.currentSrc || img.src;
      }
    }
    return absUrl(best);
  }

  function detectPdf() {
    const link = document.querySelector('a[href$=".pdf"], a[href*=".pdf?"]');
    return link ? absUrl(link.href) : null;
  }

  // ── Capture-timing guard (08-A) ─────────────────────────────────────────────
  // Canonical reference + tests in src/lib/capture-readiness.js; keep in sync.
  const EMPTY_DETAIL_PHRASES = [
    'no record has been selected',
    'no record selected',
    'no record is selected',
    'select a record to',
    'no results to display',
    'no details to show',
  ];

  function detailLooksUnpopulated(text) {
    const t = String(text || '').toLowerCase();
    return EMPTY_DETAIL_PHRASES.some((p) => t.indexOf(p) !== -1);
  }

  // A capture taken before the record detail is open serializes an empty panel
  // and silently yields nothing to extract (EX20). Warn so the human opens the
  // detail first. Generic heuristic: the known "nothing selected" phrases, or a
  // record-shaped page (image viewer / fact panel) with almost no detail cells.
  function captureWarning() {
    const text = (document.body && (document.body.innerText || document.body.textContent)) || '';
    if (detailLooksUnpopulated(text)) {
      return 'The record detail looks empty (“no record selected”). Open the record so its full data is captured.';
    }
    const cells = document.querySelectorAll('.grid-cell, [data-testid]').length;
    const isRecordPage = /\/(imageviewer|search\/collections|ark:)/i.test(location.href);
    if (isRecordPage && cells > 0 && cells < 4) {
      return 'Only a little record detail is loaded. Open/expand the record before capturing so the full data is saved.';
    }
    return null;
  }

  // ── Ancestry image-viewer detection (asset ACQUISITION, not extraction) ──────
  //
  // This is the one site-specific affordance in the companion, and it is
  // deliberately NOT extraction: it does not read or parse the record, it only
  // identifies the page and, on the human's Capture, fetches the SAME full-res
  // file Ancestry's own Download button would hand back - in the human's own
  // session, one image at a time (the owner's "Option A": automating a single
  // click, not scraping). It exists because `detectImage()` on a tiled deep-zoom
  // viewer returns the 507x600 preview thumbnail (the EX7 trap), so the panel
  // must NOT pre-fill that as the record; the auto path below gets the real scan.
  //
  // Identity test: host ancestry.* AND a parseable
  //   /imageviewer/collections/{dbId}/images/{imageId}   (pId from the query)
  // Anything else returns null and every existing behavior is left untouched.
  function parseAncestryImageViewer(href) {
    let u;
    try {
      u = new URL(href || location.href);
    } catch (e) {
      return null;
    }
    const host = u.hostname.toLowerCase();
    if (!/(^|\.)ancestry\./.test(host)) return null;
    // dbId is digits; imageId is the rest of the path after /images/ and may
    // contain hyphens/dots (e.g. "m-t0627-00331-00237",
    // "43290879-California-219510-0030", "vdvusaca1966_0105_06_n-0089").
    const m = u.pathname.match(
      /\/imageviewer\/collections\/(\d+)\/images\/([^/?#]+)/i
    );
    if (!m) return null;
    const dbId = m[1];
    let imageId;
    try {
      imageId = decodeURIComponent(m[2]);
    } catch (e) {
      imageId = m[2]; // a stray % that isn't valid encoding - use the raw segment
    }
    const pId = u.searchParams.get('pId') || '';
    if (!dbId || !imageId) return null;
    return { dbId, imageId, pId, origin: u.origin };
  }

  // A real assembled census/record scan is hundreds of KB to multiple MB (EX8:
  // 860 KB at 3040x2624). The viewer's preview thumbnail is ~45 KB (EX7). If the
  // download endpoint ever quietly hands back a preview-sized image, treat it as
  // a failure rather than silently filing a thumbnail as the record (the EX7
  // trap, restated as a guard). 80 KB sits well above the thumbnail and well
  // below any genuine scan.
  const ANCESTRY_MIN_FULL_BYTES = 80 * 1024;

  // Fetch the full-res Ancestry record image, in-session, for the CURRENT page.
  // Mirrors fetchAsset's result shape ({ ok, base64, ext, contentType } | { ok:false, error })
  // so the panel's existing buildEvidence() can consume it the same way. Two
  // same-origin GETs with the human's cookies (credentials:'include'):
  //   1. /imageviewer/api/media/token?dbId=&imageId=&pId=  -> { imageDownloadUrl }
  //   2. {origin}{imageDownloadUrl}  (download=True)        -> the assembled JPEG
  // Never falls back to the thumbnail; every failure mode returns a clear error
  // the panel surfaces while keeping the manual paths available.
  async function fetchAncestryFullImage() {
    const info = parseAncestryImageViewer(location.href);
    if (!info) {
      return { ok: false, error: 'this is not an Ancestry image-viewer page' };
    }
    const { dbId, imageId, pId, origin } = info;

    // Step 1 - mint a per-image security token in the human's session.
    let tokenJson;
    try {
      const tokenUrl =
        origin +
        '/imageviewer/api/media/token?dbId=' +
        encodeURIComponent(dbId) +
        '&imageId=' +
        encodeURIComponent(imageId) +
        (pId ? '&pId=' + encodeURIComponent(pId) : '');
      const resp = await fetch(tokenUrl, {
        credentials: 'include',
        headers: { accept: 'application/json' },
      });
      if (!resp.ok) {
        // 401/403 = not logged in (or no access to this collection); say so
        // plainly so the panel can steer to the manual download path.
        const why =
          resp.status === 401 || resp.status === 403
            ? 'Ancestry refused (HTTP ' +
              resp.status +
              ') - sign in on this page, or the collection may not allow downloads'
            : 'the Ancestry image service returned HTTP ' + resp.status;
        return { ok: false, error: why };
      }
      tokenJson = await resp.json();
    } catch (e) {
      return {
        ok: false,
        error: 'could not reach the Ancestry image service (are you online and signed in?)',
      };
    }

    // The download link is server-built and carries the securitytoken +
    // download=True. If it is absent, the collection/account tier has the
    // download flag disabled - fail clearly, do NOT reach for a thumbnail.
    const downloadPath =
      tokenJson && (tokenJson.imageDownloadUrl || tokenJson.imagedownloadurl);
    if (!downloadPath || typeof downloadPath !== 'string') {
      return {
        ok: false,
        error: 'Ancestry did not offer a downloadable image for this record (downloads may be disabled for this collection)',
      };
    }

    // Step 2 - fetch the assembled full-res JPEG. imageDownloadUrl is a
    // site-relative path; resolve it against the page origin.
    try {
      const imgUrl = /^https?:/i.test(downloadPath)
        ? downloadPath
        : origin + (downloadPath.charAt(0) === '/' ? '' : '/') + downloadPath;
      const resp = await fetch(imgUrl, { credentials: 'include' });
      if (!resp.ok) {
        return {
          ok: false,
          error: 'Ancestry refused the image download (HTTP ' + resp.status + ')',
        };
      }
      const blob = await resp.blob();
      // Thumbnail guard: a genuine scan is large; anything tiny is the preview
      // (or an error page), never the record.
      if (blob.size < ANCESTRY_MIN_FULL_BYTES) {
        return {
          ok: false,
          error:
            'the image came back too small (' +
            Math.round(blob.size / 1024) +
            ' KB) to be the full record - use Ancestry’s Download button and drop the file in instead',
        };
      }
      const base64 = await blobToBase64(blob);
      const ext = extFromContentType(
        blob.type || resp.headers.get('content-type'),
        imgUrl
      );
      // The token endpoint always serves JPEG; default to jpg if the type is
      // generic so the staged file is record.jpg, not record.bin.
      return {
        ok: true,
        base64,
        ext: ext === 'bin' ? 'jpg' : ext,
        contentType: blob.type || 'image/jpeg',
      };
    } catch (e) {
      return {
        ok: false,
        error: 'the full-res image would not come through - use Ancestry’s Download button and drop the file in instead',
      };
    }
  }

  // ── IIIF full-image auto-fetch (open archives) ───────────────────────────────
  // IIIF is an open standard, so this is GENERIC asset acquisition (it does not
  // break the "browser stays generic" line). Canonical reference + tests live in
  // src/lib/iiif.js; keep this copy in sync. A content script can't import, so the
  // regex/helpers are duplicated here.
  const IIIF_IMAGE_RE = new RegExp(
    '^(.+?)' +
    '/(full|square|\\d+,\\d+,\\d+,\\d+|pct:[\\d.]+,[\\d.]+,[\\d.]+,[\\d.]+)' +
    '/([^/]+)' +
    '/(!?[\\d.]+)' +
    '/(default|color|colour|gray|grey|bitonal|native)' +
    '\\.(jpe?g|tiff?|png|gif|jp2|webp|pdf)$',
    'i'
  );

  function iiifFullImageCandidates(url) {
    const m = IIIF_IMAGE_RE.exec(String(url || ''));
    if (!m) return [];
    const base = m[1];
    return [base + '/full/full/0/default.jpg', base + '/full/max/0/default.jpg'];
  }

  // The first IIIF Image-API URL present in the DOM (img/source src + srcset,
  // anchor href). The browser's largest rendered <img> is a derivative; this
  // finds a URL we can rewrite to the full image instead.
  function detectIiifImageUrl() {
    const urls = [];
    document.querySelectorAll('img[src], source[src], a[href], link[href]').forEach((el) => {
      const v = el.getAttribute('src') || el.getAttribute('href');
      if (v) urls.push(absUrl(v));
    });
    document.querySelectorAll('img[srcset], source[srcset]').forEach((el) => {
      (el.getAttribute('srcset') || '').split(',').forEach((part) => {
        const u = part.trim().split(/\s+/)[0];
        if (u) urls.push(absUrl(u));
      });
    });
    for (const u of urls) {
      if (IIIF_IMAGE_RE.test(u)) return u;
    }
    return null;
  }

  // An error page or a derivative is small; a full archival scan is not. Lower
  // than the Ancestry bar because a full/full request already asks for the
  // largest the server allows (so it is rarely a thumbnail) - this only catches
  // an error body or an empty response masquerading as the image.
  const IIIF_MIN_FULL_BYTES = 12 * 1024;

  // Fetch the full-res IIIF image for the current page. Public domain, no auth,
  // so a plain fetch suffices (simpler than the Ancestry token dance). Tries
  // full/full (2.x) then full/max (3.x); size-guards the result. Same result
  // shape as fetchAsset so the panel consumes it identically.
  async function fetchIiifFullImage() {
    const seed = detectIiifImageUrl();
    if (!seed) {
      return { ok: false, error: 'no IIIF image was found on this page' };
    }
    const candidates = iiifFullImageCandidates(seed);
    let lastError = 'the IIIF image would not come through';
    for (const imgUrl of candidates) {
      try {
        const resp = await fetch(imgUrl, { credentials: 'omit' });
        if (!resp.ok) {
          lastError = 'the IIIF server returned HTTP ' + resp.status;
          continue;
        }
        const blob = await resp.blob();
        if (blob.size < IIIF_MIN_FULL_BYTES) {
          lastError =
            'the IIIF image came back too small (' +
            Math.round(blob.size / 1024) +
            ' KB) to be the full record';
          continue;
        }
        const base64 = await blobToBase64(blob);
        const ext = extFromContentType(
          blob.type || resp.headers.get('content-type'),
          imgUrl
        );
        return {
          ok: true,
          base64,
          ext: ext === 'bin' ? 'jpg' : ext,
          contentType: blob.type || 'image/jpeg',
        };
      } catch (e) {
        lastError = 'could not reach the IIIF image service';
      }
    }
    return { ok: false, error: lastError };
  }

  function buildPrefill() {
    const url = location.href;
    const canonical = absUrl(
      (document.querySelector('link[rel="canonical"]') || {}).href
    );
    const title =
      metaContent('meta[property="og:title"]', 'meta[name="og:title"]') ||
      (document.title || '').trim() ||
      ((document.querySelector('h1') || {}).textContent || '').trim() ||
      null;
    const sourceDate = metaContent(
      'meta[property="article:published_time"]',
      'meta[name="article:published_time"]',
      'meta[name="date"]'
    );
    // On an Ancestry image-viewer page the auto full-res path replaces the
    // detectImage() guess (which would be the thumbnail - the EX7 trap), so the
    // panel knows not to pre-fill the asset URL with it.
    const ancestry = parseAncestryImageViewer(url);
    return {
      url,
      canonical,
      title,
      sourceDate,
      repository: hostnameOf(url) || null,
      people: harvestPeople(),
      imageUrl: detectImage(),
      pdfUrl: detectPdf(),
      recipeHint: recipeHint(url),
      ancestryImageViewer: !!ancestry,
      // A public archive whose full image can be fetched automatically (IIIF);
      // the panel can offer the one-click fetch instead of a manual download.
      iiif: !!detectIiifImageUrl(),
      // A non-null warning when the detail panel looks empty/unloaded at capture
      // time (08-A) - the panel shows it so the human opens the record first.
      warning: captureWarning(),
    };
  }

  // ── page.html (always saved) ────────────────────────────────────────────────

  function serializePage() {
    // Prepend the doctype so the saved DOM re-parses faithfully. This is the
    // CLEAN scrape source the Python recipe runs on - kept separate from any
    // bulky case-(b) preservation copy (§3: deliberately two files).
    const doctype = document.doctype
      ? '<!DOCTYPE ' +
        document.doctype.name +
        (document.doctype.publicId ? ' PUBLIC "' + document.doctype.publicId + '"' : '') +
        (document.doctype.systemId ? ' "' + document.doctype.systemId + '"' : '') +
        '>\n'
      : '<!DOCTYPE html>\n';
    return doctype + document.documentElement.outerHTML;
  }

  // ── asset fetch, case (a) ────────────────────────────────────────────────────

  function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        // reader.result is a data: URL; strip the prefix to the raw base64.
        const result = String(reader.result);
        const comma = result.indexOf(',');
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = () => reject(reader.error || new Error('read failed'));
      reader.readAsDataURL(blob);
    });
  }

  function extFromContentType(ct, fallbackUrl) {
    const map = {
      'image/jpeg': 'jpg',
      'image/jpg': 'jpg',
      'image/png': 'png',
      'image/gif': 'gif',
      'image/webp': 'webp',
      'image/tiff': 'tif',
      'application/pdf': 'pdf',
    };
    const base = (ct || '').split(';')[0].trim().toLowerCase();
    if (map[base]) return map[base];
    // Fall back to the URL's own extension when the content-type is generic.
    try {
      const path = new URL(fallbackUrl).pathname;
      const m = path.match(/\.([a-z0-9]{1,5})$/i);
      if (m) return m[1].toLowerCase();
    } catch (e) {
      /* ignore */
    }
    return 'bin';
  }

  async function fetchAsset(url) {
    const abs = absUrl(url);
    if (!abs) return { ok: false, error: 'that asset URL could not be read' };
    try {
      // credentials:'include' so a login-gated image the human can already see
      // comes through in their own session (§5.6). Cross-origin/DRM/tiled-viewer
      // images may still refuse - the panel then offers the (c) manual hand-off.
      const resp = await fetch(abs, { credentials: 'include' });
      if (!resp.ok) {
        return { ok: false, error: 'the page refused that download (HTTP ' + resp.status + ')' };
      }
      const blob = await resp.blob();
      const base64 = await blobToBase64(blob);
      const ext = extFromContentType(blob.type || resp.headers.get('content-type'), abs);
      return { ok: true, base64, ext, contentType: blob.type || '' };
    } catch (e) {
      // The usual cause is a CORS-locked or tiled viewer image; say so and let
      // the human fall back to (c) instead of leaving empty-handed.
      return {
        ok: false,
        error: 'this image would not come through (often a protected viewer), try dropping the file in instead',
      };
    }
  }

  // ── single-file snapshot, case (b) ───────────────────────────────────────────

  async function fetchAsDataUri(url) {
    const abs = absUrl(url);
    if (!abs) return null;
    try {
      const resp = await fetch(abs, { credentials: 'include' });
      if (!resp.ok) return null;
      const blob = await resp.blob();
      if (blob.size > SINGLEFILE_MAX_BYTES_PER_RESOURCE) return null;
      const base64 = await blobToBase64(blob);
      const type = blob.type || 'application/octet-stream';
      return 'data:' + type + ';base64,' + base64;
    } catch (e) {
      return null; // CORS / network - leave the original URL in place, honestly
    }
  }

  async function buildSingleFile() {
    // A MINIMAL inliner (TOOLING_INGESTION §9 "write a minimal one"): images and
    // stylesheets are the must-haves so the snapshot survives link rot; fonts,
    // web-components, and lazy media are diminishing returns and left out. We
    // clone the live (post-load) DOM so dynamic content has settled (§5.6).
    const clone = document.documentElement.cloneNode(true);

    // Drop EXECUTABLE scripts (they add nothing to a preservation snapshot and
    // only invite breakage) but KEEP non-executable data scripts - chiefly
    // `<script type="application/ld+json">` JSON-LD - so the snapshot stays
    // scrape-able: the Python recipe reads JSON-LD person names and structured
    // metadata at ingest, and a single-file snapshot that dropped it would lose
    // those hints. A script with no `type`, or `text/javascript`/`module`, is
    // executable and removed; a non-JS data type is preserved verbatim.
    const DATA_SCRIPT = /(^|\/)(ld\+json|json)\b/i;
    clone.querySelectorAll('script').forEach((s) => {
      const type = (s.getAttribute('type') || '').trim().toLowerCase();
      if (!DATA_SCRIPT.test(type)) s.remove();
    });

    let budget = SINGLEFILE_MAX_RESOURCES;

    // Inline <img> sources (and neutralize srcset so the data: src is used).
    for (const img of Array.from(clone.querySelectorAll('img'))) {
      if (budget <= 0) break;
      const src = img.getAttribute('src');
      if (!src || src.startsWith('data:')) continue;
      const dataUri = await fetchAsDataUri(src);
      if (dataUri) {
        img.setAttribute('src', dataUri);
        img.removeAttribute('srcset');
        budget--;
      }
    }

    // Inline stylesheets as <style> blocks so layout survives offline. We inline
    // the CSS text only (not its nested url() resources) to stay minimal and
    // bounded; the result is honest - a readable copy, not a pixel-perfect mirror.
    for (const link of Array.from(
      clone.querySelectorAll('link[rel~="stylesheet"]')
    )) {
      if (budget <= 0) break;
      const href = link.getAttribute('href');
      if (!href) continue;
      try {
        const resp = await fetch(absUrl(href), { credentials: 'include' });
        if (!resp.ok) continue;
        const css = await resp.text();
        const style = document.createElement('style');
        style.textContent = css;
        link.replaceWith(style);
        budget--;
      } catch (e) {
        /* leave the <link> as-is; it just won't resolve offline */
      }
    }

    return '<!DOCTYPE html>\n' + clone.outerHTML;
  }

  // ── message routing ──────────────────────────────────────────────────────────

  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (!msg || !msg.action) return;
    switch (msg.action) {
      case 'ping':
        sendResponse({ ok: true });
        return; // synchronous
      case 'prefill':
        try {
          sendResponse({ ok: true, prefill: buildPrefill() });
        } catch (e) {
          sendResponse({ ok: false, error: String(e) });
        }
        return;
      case 'pagehtml':
        try {
          sendResponse({ ok: true, html: serializePage() });
        } catch (e) {
          sendResponse({ ok: false, error: String(e) });
        }
        return;
      case 'fetchAsset':
        fetchAsset(msg.url).then(sendResponse);
        return true; // async response
      case 'ancestryImage':
        // Full-res Ancestry record fetch for the current page, in-session.
        // Same result shape as fetchAsset so the panel handles both alike.
        fetchAncestryFullImage()
          .then(sendResponse)
          .catch((e) => sendResponse({ ok: false, error: String(e) }));
        return true; // async response
      case 'iiifImage':
        // Full-res IIIF image fetch (open archives) for the current page.
        fetchIiifFullImage()
          .then(sendResponse)
          .catch((e) => sendResponse({ ok: false, error: String(e) }));
        return true; // async response
      case 'singlefile':
        buildSingleFile()
          .then((html) => sendResponse({ ok: true, html }))
          .catch((e) => sendResponse({ ok: false, error: String(e) }));
        return true; // async response
      default:
        return;
    }
  });
})();
