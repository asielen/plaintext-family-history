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

  // ── srcset parsing ──────────────────────────────────────────────────────────
  // Canonical reference + tests in src/lib/srcset.js; keep in sync (the sync
  // guard in tests/test-sync.js runs both copies through the same battery).
  //
  // A `srcset` attribute LOOKS like a comma-separated list, but a comma is a
  // legal character inside a candidate URL - `data:image/svg+xml,…` carries one
  // by construction and parameterized CDN URLs (`…/resize,w_600/photo.jpg`)
  // carry them routinely. Splitting on commas therefore cuts one URL into
  // several fragments and rewrites each as a path of its own, quietly breaking
  // every image the snapshot did not manage to inline. The HTML Standard's
  // "parse a srcset attribute" algorithm is what avoids that: a URL run ends at
  // WHITESPACE, and a comma separates candidates only when it trails that run
  // or terminates the descriptors after it.
  // FHA-SYNC-BEGIN srcset
  const SRCSET_WHITESPACE = '\t\n\f\r ';

  function isSrcsetWhitespace(ch) {
    return SRCSET_WHITESPACE.indexOf(ch) !== -1;
  }

  function parseSrcset(value) {
    const input = String(value == null ? '' : value);
    const length = input.length;
    const candidates = [];
    let pos = 0;

    while (pos < length) {
      while (pos < length && (isSrcsetWhitespace(input[pos]) || input[pos] === ',')) {
        pos += 1;
      }
      if (pos >= length) break;

      const urlStart = pos;
      while (pos < length && !isSrcsetWhitespace(input[pos])) pos += 1;
      let url = input.slice(urlStart, pos);

      const descriptors = [];
      let hadTrailingComma = false;
      while (url.length && url[url.length - 1] === ',') {
        url = url.slice(0, -1);
        hadTrailingComma = true;
      }

      if (!hadTrailingComma) {
        let state = 'descriptor';
        let current = '';
        while (true) {
          const ch = pos < length ? input[pos] : null;
          if (state === 'descriptor') {
            if (ch === null) {
              if (current) descriptors.push(current);
              break;
            }
            if (isSrcsetWhitespace(ch)) {
              if (current) descriptors.push(current);
              current = '';
              state = 'after-descriptor';
            } else if (ch === ',') {
              pos += 1;
              if (current) descriptors.push(current);
              break;
            } else if (ch === '(') {
              current += ch;
              state = 'parens';
            } else {
              current += ch;
            }
          } else if (state === 'parens') {
            if (ch === null) {
              if (current) descriptors.push(current);
              break;
            }
            if (ch === ')') {
              current += ch;
              state = 'descriptor';
            } else {
              current += ch;
            }
          } else {
            // after-descriptor: whitespace runs are collapsed; anything else
            // starts the next descriptor and must be re-read, not consumed.
            if (ch === null) break;
            if (!isSrcsetWhitespace(ch)) {
              state = 'descriptor';
              continue;
            }
          }
          pos += 1;
        }
      }

      if (url) candidates.push({ url: url, descriptor: descriptors.join(' ') });
    }

    return candidates;
  }

  function serializeSrcset(candidates) {
    return (candidates || [])
      .filter(function (c) { return c && c.url; })
      .map(function (c) { return c.descriptor ? c.url + ' ' + c.descriptor : c.url; })
      .join(', ');
  }

  function rewriteSrcset(value, rewrite) {
    const candidates = parseSrcset(value).map(function (c) {
      const replaced = rewrite(c.url);
      return replaced ? { url: replaced, descriptor: c.descriptor } : c;
    });
    return serializeSrcset(candidates);
  }

  function srcsetUrls(value) {
    return parseSrcset(value).map(function (c) { return c.url; });
  }
  // FHA-SYNC-END srcset

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
    // An explicit Person is a person even when it carries an address/geo
    // (schema.org Person inherits `address` from Thing, and obituary/genealogy
    // pages routinely attach a residence) — never let the place heuristic drop it.
    if (types.includes('Person')) return false;
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
    // srcset/imagesrcset are parsed, never split on commas: a candidate URL may
    // legally contain one, and a fragment of a URL never matches the IIIF shape.
    document
      .querySelectorAll('img[srcset], source[srcset], link[imagesrcset]')
      .forEach((el) => {
        const raw = el.getAttribute('srcset') || el.getAttribute('imagesrcset');
        srcsetUrls(raw).forEach((u) => {
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
  //
  // THE RULE for every rewrite below, stated once so nobody has to re-derive it.
  // A preserved snapshot exists to be read offline, years later, by someone
  // asking "what did this page say?". Two different things live in its markup and
  // they get opposite treatment:
  //
  //   ABSOLUTIZE what the page needs in order to SHOW what it showed - an image,
  //   a stylesheet, a poster frame, the href a reader may later choose to click.
  //   Left relative, those resolve into whatever folder the file was opened from,
  //   and the evidence renders as a wall of broken boxes.
  //
  //   NEUTRALIZE anything that acts on the reader's behalf - a navigation away
  //   from the file, a fetch nobody asked for, a rule that would hide what we
  //   just inlined. Absolutizing one of those does not preserve it, it ARMS it:
  //   a `<meta http-equiv=refresh content="0;url=/login">` that was harmlessly
  //   broken becomes a working one-way trip to the live login page, and the
  //   evidence can then never be inspected offline at all.
  //
  // The test is "does it fire without the reader asking?", not "is it a link?".
  // A click IS the reader asking, so an `<a href>`, a `<form action>`, even a
  // `javascript:` href are left as they are - following one is a decision the
  // reader made. A refresh, a preload, an onload handler and a frame that can
  // retarget the top window do not wait to be asked, so they are disarmed.
  //
  // NEUTRALIZE MEANS DISARM, NOT DELETE. The page said what it said, refresh
  // directives and all, and a snapshot that quietly drops half of that is
  // evidence with edits. Everything disarmed here keeps its element and its
  // original value, moved onto a `data-fha-disabled-*` attribute that no browser
  // acts on and that any reader - or any grep - can still find.

  // URL forms the snapshot must NEVER rewrite. `#facts` has to keep scrolling
  // within the saved page (and an SVG `<use href="#icon">` has to keep finding
  // its sprite); the rest already carry their own payload or protocol and have
  // nothing to resolve.
  // FHA-SYNC-BEGIN snapshot-urls
  const SNAPSHOT_SKIP_URL = /^(#|data:|blob:|javascript:|mailto:|tel:|about:)/i;

  /**
   * Absolute form of `value` against `base`, or null to leave it alone.
   *
   * Returns null for the skip forms above, for anything unparseable, and - the
   * privacy rule - for anything that resolves to a `file:` URL. A page opened
   * from disk would otherwise have this machine's folder names written into the
   * saved snapshot, which then travels into the archive; an unresolved relative
   * reference is a broken image, a leaked path is someone's home directory.
   */
  function absolutizeUrl(value, base) {
    const v = String(value == null ? '' : value).trim();
    if (!v || SNAPSHOT_SKIP_URL.test(v)) return null;
    let resolved;
    try {
      resolved = new URL(v, base);
    } catch (_e) {
      return null;
    }
    if (resolved.protocol === 'file:') return null;
    return resolved.href;
  }

  /**
   * Absolutize the `url()` and `@import` references inside a CSS text.
   *
   * `base` is the URL the CSS itself came from, not the document's: a fetched
   * stylesheet resolves its own relative paths against its own address, so
   * inlining `/theme/site.css` into a `<style>` block without this rewrite
   * silently re-points every `url(../img/x.png)` at the document instead - and
   * at the local folder once the snapshot is opened from file://.
   *
   * Conservative on purpose: a reference it cannot parse is left exactly as it
   * was. Garbling a stylesheet is worse than leaving one rule unresolved.
   */
  function absolutizeCss(css, base) {
    return String(css == null ? '' : css)
      .replace(/url\(\s*(["']?)([^"')]*)\1\s*\)/gi, (whole, quote, target) => {
        const abs = absolutizeUrl(target, base);
        return abs ? 'url(' + quote + abs + quote + ')' : whole;
      })
      .replace(/@import\s+(["'])([^"']*)\1/gi, (whole, quote, target) => {
        const abs = absolutizeUrl(target, base);
        return abs ? '@import ' + quote + abs + quote : whole;
      });
  }
  // FHA-SYNC-END snapshot-urls

  // The per-element decisions, one function each, so the node suite can drive
  // them without a DOM (tests/test-snapshot-urls.js hands them a stub element).
  // Each takes an element from the CLONE - never the live page - and follows the
  // absolutize/neutralize rule stated at the top of this section.
  // FHA-SYNC-BEGIN snapshot-rewrites

  /** Prefix every disarmed value is parked under (see the rule above). */
  const DISABLED_ATTR = 'data-fha-disabled-';

  /**
   * Move one attribute onto its `data-fha-disabled-` twin.
   *
   * The single mechanic behind "disarm, not delete": the browser stops acting on
   * the value because the attribute it reads is gone, and the reader keeps it
   * because the value is still sitting right there in the markup.
   */
  function disarmAttribute(el, name) {
    if (!el.hasAttribute(name)) return;
    el.setAttribute(DISABLED_ATTR + name, el.getAttribute(name));
    el.removeAttribute(name);
  }

  /**
   * Point the cloned page's own <base> at the base the browser actually used.
   *
   * `pageBase` is document.baseURI, which has ALREADY folded this element's href
   * into the page URL: a page at /collections/detail carrying `<base
   * href="records/">` reports a baseURI of /collections/records/. Resolving the
   * raw attribute against that again - which is what an earlier round did -
   * applies the same relative path twice and writes /collections/records/records/,
   * so every reference the inliner left relative silently re-points one directory
   * too deep. The resolved value IS the answer; the raw attribute is never read.
   *
   * absolutizeUrl is still called (resolving an absolute URL against itself is
   * the identity) for its two refusals: an unparseable base, and the privacy rule
   * that no `file:` path may enter a snapshot. Either way the author's own href
   * is left exactly as written - on a page captured from disk nothing else gets
   * absolutized either, so the snapshot stays internally consistent instead of
   * half-anchored to someone's home directory.
   */
  function anchorBaseElement(baseEl, pageBase) {
    if (!baseEl) return;
    const href = absolutizeUrl(pageBase, pageBase);
    if (href) baseEl.setAttribute('href', href);
  }

  // The <meta http-equiv> pragmas a browser acts on that a snapshot must not let
  // it act on. Everything else is left untouched - `content-type` above all,
  // since it carries the charset the parser prescans for in the first 1024 bytes.
  const DISARM_META_PRAGMA = new Set([
    // Fires on open, with no reader input, and takes the browser AWAY from the
    // saved file - typically to a login or expired-session page, after which the
    // preserved evidence cannot be read at all.
    'refresh',
    // A captured policy was written for the LIVE page and knows nothing about the
    // snapshot: `default-src 'self'` blocks precisely what preservation depends
    // on - the data: images we inlined and the <style> blocks we swapped the
    // stylesheets for - so honouring it renders the saved page blank. The
    // report-only twin blocks nothing but phones the live collector on open.
    'content-security-policy',
    'content-security-policy-report-only',
  ]);

  /**
   * Disarm one <meta http-equiv> if its pragma is one of the harmful few.
   *
   * `content` is kept verbatim: the directive is part of what the page said, and
   * a reader should be able to see the page carried it. For a refresh the
   * resolved destination is recorded next to it as well, so "where would this
   * have sent me?" stays answerable offline without resolving a relative path by
   * hand - recording a target is not the same as following one.
   *
   * A refresh with no `url=` (a plain self-reload) is disarmed too: it has no
   * target to record, and left live it would sit there reloading the snapshot.
   */
  function disarmMetaPragma(meta, pageBase) {
    const equiv = (meta.getAttribute('http-equiv') || '').trim().toLowerCase();
    if (!DISARM_META_PRAGMA.has(equiv)) return;
    disarmAttribute(meta, 'http-equiv');
    if (equiv !== 'refresh') return;
    const m = (meta.getAttribute('content') || '')
      .match(/^\s*[\d.]*\s*;\s*url\s*=\s*(["']?)(.*?)\1\s*$/i);
    const target = m ? absolutizeUrl(m[2], pageBase) : null;
    if (target) meta.setAttribute('data-fha-refresh-target', target);
  }

  /**
   * Strip the inline event handlers from one element.
   *
   * With every executable <script> already removed, an `on*` attribute is the
   * only JavaScript a snapshot can still run - and the line most likely to be in
   * it is `location = …`. A body `onload` that redirects to the live site, an
   * `onerror` that swaps in a network URL: both fire on open and neither renders
   * anything. This is also what closes the remaining routes the removed scripts
   * used to own - a service-worker registration, a `history.pushState`, an
   * auto-submitting form - since none of them can start without script.
   */
  function disarmInlineHandlers(el) {
    for (const name of el.getAttributeNames()) {
      // Three letters minimum after "on": the shortest real handler is `oncut`,
      // and the bar keeps an ordinary attribute that happens to start with those
      // two letters (`one`, `once`) out of the sweep.
      if (/^on[a-z]{3,}$/i.test(name)) disarmAttribute(el, name);
    }
  }

  // <link rel> values that fetch on their own initiative and contribute nothing
  // to what the saved page shows. The resource each one warms up is requested by
  // a real element elsewhere in the page, so dropping the hint costs a snapshot
  // nothing; keeping it means opening an archived file quietly pings the live
  // server (`prerender` fetches an entire page) years after the capture.
  const DISARM_LINK_REL = /^(preload|modulepreload|prefetch|prerender|preconnect|dns-prefetch)$/i;

  /**
   * Disarm a <link> that exists only to reach out to the network.
   *
   * Only when EVERY rel token is one of those: a `rel="stylesheet preload"` still
   * has a rendering job to do, and `rel="canonical"`, `rel="icon"` and the rest
   * are left alone entirely - canonical records which live URL the page claimed
   * to be, which is evidence, and no browser navigates to it.
   */
  function disarmSpeculativeLink(link) {
    const rels = (link.getAttribute('rel') || '').trim().split(/\s+/).filter(Boolean);
    if (!rels.length || !rels.every((rel) => DISARM_LINK_REL.test(rel))) return;
    disarmAttribute(link, 'rel');
  }

  // What a framed page may still do inside a snapshot. Deliberately permissive
  // except for the two powers that matter here: no allow-top-navigation token,
  // in any of its spellings, and no allow-scripts for a frame whose whole
  // document travels with us inside its own `srcdoc` attribute.
  const FRAME_SANDBOX_DEFAULT = 'allow-scripts allow-same-origin allow-forms allow-popups';
  const TOP_NAVIGATION_TOKEN = /^allow-top-navigation/i;
  const SCRIPTS_TOKEN = /^allow-scripts$/i;

  /**
   * The sandbox one preserved frame should carry.
   *
   * `declared` is the author's own sandbox attribute (null when they wrote
   * none). It is only ever pruned, never widened, so a page that had already
   * locked its frames down stays locked down.
   *
   * allow-scripts SURVIVES for a frame pointing at a live page (`src`): the
   * framed thing is often the record viewer itself and most viewers are a blank
   * box without script, so a live copy the reader can still open is the honest
   * outcome for something we cannot inline. It does NOT survive for a frame
   * carrying `srcdoc`. That document is markup we captured and can disarm, the
   * pass below does exactly that, and withholding the permission as well means
   * anything the pass failed to recognise stays inert instead of running the
   * moment the saved file is opened. Nothing legitimate is lost: a snapshot's
   * inline frame has no script left to run.
   *
   * Both tokens also bind every frame nested inside this one, at any depth: a
   * nested browsing context inherits its parent's sandbox flags and can only be
   * more restrictive, never less. That inheritance is what bounds the srcdoc
   * rewrite below to a single level of markup.
   */
  function frameSandboxValue(declared, carriesSrcdoc) {
    const tokens = (declared == null ? FRAME_SANDBOX_DEFAULT : String(declared))
      .trim().split(/\s+/).filter(Boolean);
    return tokens
      .filter((t) => !TOP_NAVIGATION_TOKEN.test(t))
      .filter((t) => !(carriesSrcdoc && SCRIPTS_TOKEN.test(t)))
      .join(' ');
  }

  /**
   * Stop a framed page from steering the whole snapshot, or running at all when
   * we are the ones carrying its markup.
   *
   * An `<iframe src>` is absolutized like any other resource on purpose (see
   * frameSandboxValue). What it must not keep is the power to replace the
   * top-level document - a frame-busting script inside the framed page
   * (`top.location = …`) does exactly what the meta refresh did, one level down,
   * and the reader loses the evidence the same way.
   */
  function limitFrameNavigation(frame) {
    frame.setAttribute('sandbox', frameSandboxValue(
      frame.getAttribute('sandbox'), frame.hasAttribute('srcdoc')));
  }

  // ── markup that lives inside an attribute or a raw text node ────────────────
  //
  // Every pass above walks ELEMENTS. Two places in a captured page hold markup
  // that never became an element in the tab we cloned, so no element pass ever
  // sees it - and both become live markup again in the saved file:
  //
  //   • `<iframe srcdoc="…">` - a whole document stored in an attribute value.
  //     It is parsed and rendered the instant the snapshot is opened, and it
  //     brings its own <script>s, its own on* handlers and its own meta refresh
  //     with it.
  //   • `<noscript>` - with scripting on (which it is in the tab being cloned)
  //     its contents sit in the DOM as one raw text node. That text is live
  //     markup for exactly the reader most likely to open an archived file with
  //     JavaScript off.
  //
  // Both are handled as STRING rewrites over the start tags, for the reason the
  // <noscript> pass already gave: markup a pattern does not recognise passes
  // through unharmed rather than garbled, and a tag nothing applies to comes
  // back byte for byte. The tradeoff, stated plainly: a start tag written inside
  // a comment or inside disabled script text is rewritten too, because a string
  // scan cannot tell those apart. That costs a cosmetic edit in a place nothing
  // renders; re-parsing and re-serializing the markup instead would rewrite
  // every quote and entity in the whole fragment.

  /** Script types the snapshot KEEPS (see the executable-script sweep below). */
  const DATA_SCRIPT_TYPE = /(^|\/)(ld\+json|json)\b/i;

  /** What an executable script's type becomes: unknown to the browser, plain to a reader. */
  const INERT_SCRIPT_TYPE = 'text/fha-disabled-script';

  // Every (tag, attribute) pair the snapshot anchors to the live page, shared by
  // the element sweep in buildSingleFile and the srcdoc rewrite here so the two
  // cannot drift. The judgement calls worth naming, so they are not re-litigated:
  //   • `iframe`/`embed`/`object` DO fetch live content the moment the snapshot
  //     opens. That is the honest outcome for something we cannot inline - a
  //     framed record viewer the reader can still open beats a blank box - and
  //     the powers that would cost the reader the evidence were removed by
  //     limitFrameNavigation above.
  //   • an autoplaying `video`/`audio` also reaches the network unasked, but it
  //     is a resource the page carried and it takes nobody off the page, so the
  //     src stays and `autoplay` is left as the author wrote it.
  //   • a saved form that still posts somewhere posts to the live site, as it did
  //     before; a relative action would post to the local folder. Nothing can
  //     submit it on its own: the scripts are gone and the on* handlers with
  //     them, so a submission is always the reader's own click.
  // No pair appears twice in this list, so nothing is resolved twice.
  const SNAPSHOT_URL_ATTRS = [
    ['a', 'href'], ['area', 'href'], ['link', 'href'],
    ['img', 'src'], ['source', 'src'], ['video', 'src'], ['audio', 'src'],
    ['video', 'poster'], ['iframe', 'src'], ['embed', 'src'],
    ['object', 'data'], ['track', 'src'], ['input', 'src'],
    ['form', 'action'], ['button', 'formaction'], ['input', 'formaction'],
  ];

  /** The URL attributes of one tag name, for the string passes. */
  function markupUrlAttrs(tagName) {
    // SVG `<use>`/`<image>` take a plain href in SVG 2 and the legacy
    // xlink:href everywhere else; both are usually a bare `#icon` that
    // absolutizeUrl declines, so the in-document sprite keeps resolving.
    if (tagName === 'use' || tagName === 'image') return ['href', 'xlink:href'];
    const attrs = [];
    for (const [tag, attr] of SNAPSHOT_URL_ATTRS) {
      if (tag === tagName) attrs.push(attr);
    }
    return attrs;
  }

  // A start tag, with quoted attribute values allowed to contain `>`
  // (`title="a > b"` is legal). Closing tags, comments and doctypes do not
  // match - the name has to start with a letter.
  const START_TAG = /<([a-zA-Z][^\s/>]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>/g;

  // Attribute name, then an optional value: double-quoted, single-quoted, or
  // bare. Values are captured in SOURCE form, entities and all.
  const START_TAG_ATTR = /([^\s=/>]+)(?:\s*=\s*("[^"]*"|'[^']*'|[^\s>]*))?/g;

  // A <style> element and its text, for the one rewrite that lives between tags
  // rather than inside one.
  const STYLE_BLOCK = /(<style\b[^>]*>)([\s\S]*?)(<\/style\s*>)/gi;

  /** The five references that matter for reading a URL back out of markup. */
  function decodeMarkupEntities(value) {
    return String(value == null ? '' : value)
      .replace(/&lt;/gi, '<').replace(/&gt;/gi, '>')
      .replace(/&quot;/gi, '"').replace(/&#0*39;|&apos;/gi, "'")
      .replace(/&amp;/gi, '&');
  }

  /**
   * The inverse, for a value this pass computed rather than copied.
   *
   * Both quote characters are escaped, not just the one in use: a rewritten
   * value is written back inside whichever quotes the author happened to
   * choose, and a URL may legally carry an apostrophe.
   */
  function encodeMarkupAttr(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  /**
   * Split one start tag's attribute text into `{name, quote, value}` records.
   *
   * Values stay in source form - unescaping them would mean re-escaping every
   * one on the way out, and the whole point of this pass is that untouched
   * markup comes back untouched. Only a value this pass replaces is decoded (to
   * resolve it) and re-encoded (to write it).
   */
  function parseStartTagAttrs(attrText) {
    const attrs = [];
    START_TAG_ATTR.lastIndex = 0;
    let m;
    while ((m = START_TAG_ATTR.exec(attrText)) !== null) {
      const raw = m[2];
      let quote = '';
      let value = null;
      if (raw !== undefined) {
        const first = raw.charAt(0);
        if (raw.length >= 2 && (first === '"' || first === "'") &&
            raw.charAt(raw.length - 1) === first) {
          quote = first;
          value = raw.slice(1, -1);
        } else {
          value = raw;
        }
      }
      attrs.push({ name: m[1], quote: quote, value: value });
    }
    return attrs;
  }

  /** Put a changed tag back together. Only ever called for tags a rule touched. */
  function serializeStartTag(name, attrs, selfClosing) {
    const rendered = attrs.map((a) => {
      if (a.value === null) return a.name;
      const quote = a.quote || '"';
      return a.name + '=' + quote + a.value + quote;
    });
    return '<' + name + (rendered.length ? ' ' + rendered.join(' ') : '') +
      (selfClosing ? ' /' : '') + '>';
  }

  /**
   * Run `rewriteTag(tagName, attrs)` over every start tag in a markup string.
   *
   * The callback mutates the attribute list in place and returns true when it
   * changed something; only then is the tag rebuilt. A tag no rule touched is
   * returned as the exact substring that was matched.
   */
  function rewriteStartTags(markup, rewriteTag) {
    return String(markup == null ? '' : markup).replace(
      START_TAG,
      (whole, name, attrText) => {
        const attrs = parseStartTagAttrs(attrText);
        const selfClosing = /\/\s*$/.test(attrText);
        return rewriteTag(name.toLowerCase(), attrs)
          ? serializeStartTag(name, attrs, selfClosing)
          : whole;
      });
  }

  /**
   * The neutralize half of the string passes: everything that would act on the
   * reader's behalf, disarmed in the same `data-fha-disabled-` idiom the element
   * passes use, so the value stays visible to a reader and to grep.
   *
   * Handled here, one per element pass above: inline `on*` handlers and `ping`
   * beacons; an executable `<script>`, which keeps its text but is given a type
   * no browser will run (the element pass can delete the node outright, a string
   * pass cannot do that without dropping evidence); a `<meta http-equiv>` that
   * refreshes or carries a captured CSP; a speculative `<link rel>`; and a
   * nested `<iframe>`, which gets the same sandbox its outer twin does.
   */
  function disarmMarkupTag(tagName, attrs) {
    let changed = false;
    for (const attr of attrs) {
      const lower = attr.name.toLowerCase();
      if (/^on[a-z]{3,}$/.test(lower) || lower === 'ping') {
        attr.name = DISABLED_ATTR + attr.name;
        changed = true;
      }
    }
    const find = (name) => attrs.find((a) => a.name.toLowerCase() === name);

    if (tagName === 'script') {
      const type = find('type');
      const declared = type && type.value ? type.value.trim().toLowerCase() : '';
      // The second test keeps a second pass over already-disarmed markup from
      // stacking a second type onto the same tag.
      if (!DATA_SCRIPT_TYPE.test(declared) && declared !== INERT_SCRIPT_TYPE) {
        // A script with no type, or a JavaScript type, executes. Park whatever
        // the author wrote and hand the browser a type it does not know.
        if (type) type.name = DISABLED_ATTR + type.name;
        else attrs.push({ name: DISABLED_ATTR + 'type', quote: '"', value: '' });
        attrs.push({ name: 'type', quote: '"', value: INERT_SCRIPT_TYPE });
        changed = true;
      }
    }

    if (tagName === 'meta') {
      const equiv = find('http-equiv');
      const pragma = equiv && equiv.value ? equiv.value.trim().toLowerCase() : '';
      if (DISARM_META_PRAGMA.has(pragma)) {
        equiv.name = DISABLED_ATTR + equiv.name;
        changed = true;
      }
    }

    if (tagName === 'link') {
      const rel = find('rel');
      const tokens = (rel && rel.value ? rel.value : '').trim().split(/\s+/).filter(Boolean);
      if (tokens.length && tokens.every((t) => DISARM_LINK_REL.test(t))) {
        rel.name = DISABLED_ATTR + rel.name;
        changed = true;
      }
    }

    if (tagName === 'iframe') {
      const sandbox = find('sandbox');
      const value = frameSandboxValue(
        sandbox ? sandbox.value : null, Boolean(find('srcdoc')));
      if (sandbox) {
        if (sandbox.value !== value) { sandbox.value = value; changed = true; }
      } else {
        attrs.push({ name: 'sandbox', quote: '"', value: value });
        changed = true;
      }
    }
    return changed;
  }

  /**
   * The absolutize half: anchor a tag's URL attributes to the live page.
   *
   * `base` is the base URL the framed document actually resolves against - the
   * parent document's, which is what an `about:srcdoc` document inherits, unless
   * the markup declares a <base> of its own. Left relative, every one of these
   * resolves into whatever folder the snapshot was opened from and the frame
   * renders as broken boxes.
   */
  function anchorMarkupTagUrls(tagName, attrs, base) {
    let changed = false;
    const urlAttrs = markupUrlAttrs(tagName);
    for (const attr of attrs) {
      if (attr.value === null) continue;
      const lower = attr.name.toLowerCase();
      const value = decodeMarkupEntities(attr.value);
      let next = null;
      if (tagName === 'base' && lower === 'href') {
        // The effective base is already resolved (see srcdocBase); resolving the
        // author's relative href against it again would apply the same path
        // twice, exactly as the outer document's <base> once did.
        next = absolutizeUrl(base, base);
      } else if (urlAttrs.indexOf(lower) !== -1) {
        next = absolutizeUrl(value, base);
      } else if (lower === 'srcset' || lower === 'imagesrcset') {
        next = rewriteSrcset(value, (u) => absolutizeUrl(u, base));
      } else if (lower === 'style' && value.indexOf('url(') !== -1) {
        next = absolutizeCss(value, base);
      }
      // An already-absolute reference resolves to itself, and a tag nothing
      // actually moved is not a tag worth re-serializing.
      const encoded = next === null ? null : encodeMarkupAttr(next);
      if (encoded !== null && encoded !== attr.value) {
        attr.value = encoded;
        attr.quote = attr.quote || '"';
        changed = true;
      }
    }
    return changed;
  }

  /**
   * The base URL a srcdoc document resolves against.
   *
   * An `about:srcdoc` document inherits its parent's base URL, so `pageBase` is
   * the answer unless the stored markup declares a <base> of its own - in which
   * case that, resolved against the page, is what the frame used. A base this
   * pass cannot resolve (or one that lands on `file:`) falls back to the page.
   */
  function srcdocBase(markup, pageBase) {
    const m = markup.match(
      /<base\b[^>]*?\shref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/i);
    if (!m) return pageBase;
    const raw = m[1] !== undefined ? m[1] : (m[2] !== undefined ? m[2] : m[3]);
    return absolutizeUrl(decodeMarkupEntities(raw), pageBase) || pageBase;
  }

  /**
   * Disarm and anchor the document a page stored in an `<iframe srcdoc="…">`.
   *
   * This is the one place a snapshot carries a second whole document, and until
   * this pass existed it was the one document nothing sanitized: the script
   * sweep, the handler sweep and the URL sweep all walk elements, and srcdoc is
   * an attribute VALUE. Opening the saved file parsed it fresh and ran whatever
   * the page had put in there.
   *
   * Bounded to one level on purpose. A srcdoc may itself contain an
   * `<iframe srcdoc="…">`, whose markup arrives here doubly escaped; this pass
   * gives that nested frame a sandbox but does not unescape and recurse into it.
   * It does not need to: sandbox flags are inherited by every nested browsing
   * context, so no descendant of a frame we denied allow-scripts can run script
   * either, however deep the nesting goes. What a deeper level can still do is
   * fetch - an inner box loading a live URL - which is the same honest outcome
   * the snapshot already accepts for `<iframe src>`.
   */
  function disarmSrcdocMarkup(markup, pageBase) {
    const text = String(markup == null ? '' : markup);
    if (!text) return text;
    const base = srcdocBase(text, pageBase);
    const anchoredCss = text.replace(
      STYLE_BLOCK, (whole, open, css, close) => open + absolutizeCss(css, base) + close);
    return rewriteStartTags(anchoredCss, (tagName, attrs) => {
      const disarmed = disarmMarkupTag(tagName, attrs);
      const anchored = anchorMarkupTagUrls(tagName, attrs, base);
      return disarmed || anchored;
    });
  }

  /**
   * Disarm the markup hiding inside a <noscript>.
   *
   * The classic payload is `<meta http-equiv="refresh" content="0;url=/nojs">`,
   * which bounces a JavaScript-off reader straight off the evidence; a captured
   * CSP blanks the page for them the same way, a speculative <link> fetches on
   * open, and a bare `<iframe>` in there can retarget the whole window because
   * nothing sandboxed it. All of those fire without being asked, so all of them
   * are disarmed.
   *
   * URLs in here are deliberately NOT anchored. A <noscript> is live only for a
   * reader with scripting off, its contents are held byte for byte, and
   * absolutizing a relative reference in there would arm the one thing these
   * blocks most often carry: a tracking pixel aimed at the live server.
   */
  function disarmNoscriptText(text) {
    return rewriteStartTags(text, disarmMarkupTag);
  }
  // FHA-SYNC-END snapshot-rewrites

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
    //
    // This is also the widest neutralize in the file, and most of what the rule
    // at the top of this section worries about dies here: a `location =` redirect,
    // a service-worker registration, an analytics beacon, a form that submits
    // itself. What survives it is inline `on*` handlers, disarmed below - and
    // the markup a page keeps inside an `<iframe srcdoc>` attribute, which no
    // element pass can reach and which disarmSrcdocMarkup handles below.
    clone.querySelectorAll('script').forEach((s) => {
      const type = (s.getAttribute('type') || '').trim().toLowerCase();
      if (!DATA_SCRIPT_TYPE.test(type)) s.remove();
    });

    // Anchor the snapshot's remaining RELATIVE references (links, images the
    // bounded inliner below skips, stylesheets) back to the live page: opened
    // from file:// every relative URL would otherwise resolve into the local
    // folder and break. Done by rewriting each attribute to its absolute form
    // rather than by INJECTING a <base>: a base of our own would re-target
    // fragment-only links too, so a table-of-contents `#facts` would navigate to
    // the LIVE site (network, login wall) instead of scrolling the snapshot, SVG
    // `<use href="#icon">` sprites would render blank, and a 1-2 KB base URL
    // pushes the page's <meta charset> past the 1024-byte prescan window.
    //
    // A page that declares its OWN <base> keeps it - that is the author's
    // baseline, and fragment links already resolve against it on the live page,
    // so preserving it is the faithful thing - but its href is written in
    // resolved form. A relative base such as `<base href="records/">` resolves
    // against the live site in the browser and against the local filesystem once
    // the snapshot is opened from file://, which would break every reference the
    // bounded inliner below did not swallow. Skipping the whole rewrite because a
    // base is present (as an earlier version did) leaves exactly those pages
    // broken.
    //
    // document.baseURI is already the RESOLVED base - the live page's own answer
    // to "what do relative URLs mean here?", <base> included - so it is both the
    // value to write into the cloned base (anchorBaseElement, which is why the
    // raw href is never re-resolved) and the thing to resolve every other
    // attribute against.
    const pageBase = document.baseURI;
    const absolutize = (value) => absolutizeUrl(value, pageBase);

    anchorBaseElement(clone.querySelector('base[href]'), pageBase);

    // Disarm first, so the anchoring sweep below cannot hand a working URL to
    // something that would use it on its own initiative. An `on*` handler is the
    // last JavaScript left once the executable scripts are gone; `ping` is a
    // fire-and-forget beacon to the live server that renders nothing; a
    // speculative <link> fetches on open for a page that no longer benefits; a
    // frame must not be able to retarget the top window. Reasoning for each sits
    // on its function, under the rule at the top of this section.
    disarmInlineHandlers(clone);
    for (const el of Array.from(clone.querySelectorAll('*'))) {
      disarmInlineHandlers(el);
    }
    for (const el of Array.from(clone.querySelectorAll('[ping]'))) {
      disarmAttribute(el, 'ping');
    }
    for (const link of Array.from(clone.querySelectorAll('link[rel]'))) {
      disarmSpeculativeLink(link);
    }
    for (const frame of Array.from(clone.querySelectorAll('iframe'))) {
      limitFrameNavigation(frame);
    }
    // The two places markup hides from every pass above: an attribute value and
    // a raw text node. Both are live markup again in the saved file, so both get
    // the same treatment as a string (see the section on them above).
    for (const frame of Array.from(clone.querySelectorAll('iframe[srcdoc]'))) {
      frame.setAttribute(
        'srcdoc', disarmSrcdocMarkup(frame.getAttribute('srcdoc'), pageBase));
    }
    for (const noscript of Array.from(clone.querySelectorAll('noscript'))) {
      noscript.textContent = disarmNoscriptText(noscript.textContent);
    }

    // Absolutize territory, every one of them: a reference the saved page needs
    // in order to show what it showed, or one the reader may choose to follow.
    // The list and the judgement calls behind it sit on SNAPSHOT_URL_ATTRS
    // above, which the srcdoc pass reads too so the two cannot drift.
    for (const [tag, attr] of SNAPSHOT_URL_ATTRS) {
      for (const el of Array.from(clone.querySelectorAll(tag + '[' + attr + ']'))) {
        const abs = absolutize(el.getAttribute(attr));
        if (abs) el.setAttribute(attr, abs);
      }
    }

    // SVG references. `<use>` and `<image>` take a plain `href` in SVG 2 and the
    // legacy `xlink:href` everywhere else, and both are commonly a bare `#icon`
    // pointing at a sprite in the same document - which absolutizeUrl leaves
    // alone, so the sprite keeps resolving inside the snapshot. Only an external
    // sprite file (`sprite.svg#icon`) is rewritten. The attribute is read with
    // getAttribute rather than matched by selector because `xlink:href` needs
    // namespace-aware escaping that querySelectorAll does not do portably.
    for (const el of Array.from(clone.querySelectorAll('use, image'))) {
      for (const attr of ['href', 'xlink:href']) {
        if (!el.hasAttribute(attr)) continue;
        const abs = absolutize(el.getAttribute(attr));
        if (abs) el.setAttribute(attr, abs);
      }
    }

    // srcset (and <link rel=preload imagesrcset>) is a list of "url descriptor"
    // candidates - parsed, never split on commas (see the srcset section above).
    for (const el of Array.from(clone.querySelectorAll('[srcset], [imagesrcset]'))) {
      for (const attr of ['srcset', 'imagesrcset']) {
        if (!el.hasAttribute(attr)) continue;
        el.setAttribute(attr, rewriteSrcset(el.getAttribute(attr), absolutize));
      }
    }

    // A meta refresh is a navigation AWAY from the evidence, not a resource the
    // page needs, so it is disarmed rather than absolutized - the one case where
    // rewriting a URL to its working form makes the snapshot useless instead of
    // usable. Its target is recorded beside it; see disarmMetaPragma.
    for (const meta of Array.from(clone.querySelectorAll('meta[http-equiv]'))) {
      disarmMetaPragma(meta, pageBase);
    }

    // CSS carries URLs too, and it is the half a snapshot most often forgets:
    // a `style="background:url(hero.jpg)"` or a `<style>` block full of relative
    // url()s resolves against the document, so both need the same anchoring the
    // attributes above got. All of it is absolutize territory - CSS paints, it
    // does not navigate, and a background image is a resource the page needs.
    //
    // ORDER MATTERS, and it is the other half of the base bug. These two passes
    // run BEFORE the stylesheet inliner below, because that inliner anchors the
    // sheet's text against the SHEET's own address; a document-based pass over
    // the <style> blocks it produced would be resolving values against a base
    // that is not theirs. (absolutizeCss is idempotent - a second pass over an
    // already-absolute url() is a no-op - so the ordering protects correctness
    // rather than papering over a doubling, but keep the passes in this order.)
    for (const el of Array.from(clone.querySelectorAll('[style]'))) {
      const style = el.getAttribute('style') || '';
      if (style.indexOf('url(') === -1) continue;
      el.setAttribute('style', absolutizeCss(style, pageBase));
    }
    for (const styleEl of Array.from(clone.querySelectorAll('style'))) {
      styleEl.textContent = absolutizeCss(styleEl.textContent, pageBase);
    }

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
        // Inside a <picture>, a surviving <source srcset> outranks the <img>
        // we just inlined, so the snapshot would go back to the network for an
        // image it already holds - and break when that URL rots. The whole
        // point of inlining is that it does not. (Only <picture> sources: a
        // <video>/<audio> <source> is a different element with a src, not a
        // fallback for this image.)
        const picture = img.parentElement;
        if (picture && picture.tagName && picture.tagName.toLowerCase() === 'picture') {
          Array.from(picture.querySelectorAll('source')).forEach((s) => s.remove());
        }
        budget--;
      }
    }

    // Inline stylesheets as <style> blocks so layout survives offline. We inline
    // the CSS text only (not its nested url() resources) to stay minimal and
    // bounded; the result is honest - a readable copy, not a pixel-perfect mirror.
    // The url()s inside it ARE re-anchored to the stylesheet's own address,
    // because moving the text into the document silently re-bases them otherwise.
    //
    // `href` here has already been absolutized by the sweep above, and absUrl of
    // an absolute URL is that same URL - no second resolution, no doubled path.
    // The base handed to absolutizeCss is the address the sheet was FETCHED from
    // (resp.url, which follows redirects), never the document's.
    for (const link of Array.from(
      clone.querySelectorAll('link[rel~="stylesheet"]')
    )) {
      if (budget <= 0) break;
      const href = link.getAttribute('href');
      if (!href) continue;
      const sheetUrl = absUrl(href);
      if (!sheetUrl) continue;
      try {
        const resp = await fetch(sheetUrl, { credentials: 'include' });
        if (!resp.ok) continue;
        const css = await resp.text();
        const style = document.createElement('style');
        style.textContent = absolutizeCss(css, resp.url || sheetUrl);
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
