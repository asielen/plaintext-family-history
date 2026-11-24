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
  // Versioned: a page that still holds a content script from an EARLIER copy
  // of the extension (reloading the extension does not unload it) gets this
  // newer one registered too; the orphaned old one can no longer answer.
  // Taken from the manifest, so every release re-registers on an open tab
  // without anyone having to remember to bump a constant.
  const FHA_CONTENT_VERSION = (() => {
    try {
      return chrome.runtime.getManifest().version;
    } catch (e) {
      return 'unknown-' + Date.now();
    }
  })();
  if (window.__fhaCaptureInjected === FHA_CONTENT_VERSION) return;
  window.__fhaCaptureInjected = FHA_CONTENT_VERSION;

  // Bounds for the single-file inliner so an image-heavy page can't spin
  // forever or bloat the snapshot past what a download can hold. Honest limits,
  // not silent perfection (§5.6 caveat: base64 bloat + CORS-locked sub-resources).
  const SINGLEFILE_MAX_RESOURCES = 120;
  const SINGLEFILE_MAX_BYTES_PER_RESOURCE = 5 * 1024 * 1024; // 5 MB

  // ── helpers ────────────────────────────────────────────────────────────────

  // Remove the Seen It add-on's injected badges (seen-it/ in the repo) from a
  // copy of the page before it is saved (TI-067): they record what you had
  // looked at, not what the site shows. Its own elements only - the lists
  // live with the Ancestry readers (ANC_SEEN_IT_CLASSES, ancIsSeenItNode).
  // Built on use, not at load: ANC_SEEN_IT_CLASSES is declared further down.
  function seenItSelector() {
    return ['[id^="nsi-"]'].concat(ANC_SEEN_IT_CLASSES.map((c) => '.' + c)).join(',');
  }
  function stripSeenIt(root) {
    for (const el of root.querySelectorAll(seenItSelector())) el.remove();
  }

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
    if (iaBookPage(url)) return 'archiveorg';
    if (host.includes('wvculture.org')) return 'wvculture';
    if (wtParsePage(url)) return 'wikitree';
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
        // ancText, not textContent: Seen It puts its badges inside a linked
        // name ("Seen 1× · last Nov 8"), and they are not the person's name.
        add(ancText(el));
      });
    return names.slice(0, 12); // a pre-fill list, not an exhaustive index
  }

  // Best-guess "the image this record centers on" for the Phase-3 (a) pre-fill.
  // og:image is the page's own declared hero; otherwise the largest rendered
  // <img>. Either way it is only a *suggestion* the human can replace or clear.
  // `staleHead`: the page's head still describes an earlier record (see
  // headIsStale), so its og:image is that record's picture, never this one's.
  function detectImage(staleHead) {
    const og = staleHead ? null : metaContent('meta[property="og:image"]', 'meta[name="og:image"]');
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
  // `ids` ({ dbId, imageId }) fetches a named image instead - the record page
  // case, where the scan is linked from the page rather than open in it.
  async function fetchAncestryFullImage(ids) {
    const info = ids && ids.dbId && ids.imageId
      ? { dbId: String(ids.dbId), imageId: String(ids.imageId), pId: '', origin: location.origin }
      : parseAncestryImageViewer(location.href);
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
        // `slowDown`/`transient`: a "slow down" or a failing server passes,
        // and the panel's batch downloads come back for the scan later.
        return { ok: false, error: why, slowDown: resp.status === 429 || resp.status === 503,
          transient: resp.status === 429 || resp.status >= 500 };
      }
      tokenJson = await resp.json();
    } catch (e) {
      return {
        ok: false,
        transient: true,
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
          slowDown: resp.status === 429 || resp.status === 503,
          transient: resp.status === 429 || resp.status >= 500,
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
        transient: true,
        error: 'the full-res image would not come through - use Ancestry’s Download button and drop the file in instead',
      };
    }
  }

  // ── Ancestry record pages (the record's own index, citation and scan) ─────────
  // On a record page (/search/collections/{dbId}/records/{recordId}) Capture
  // saves what the site shows of the record - its labelled index fields, the
  // household, the source citation - as record-data.json, and fetches the
  // linked scan through the same token call the image viewer uses
  // (fetchAncestryFullImage with ids). It is the site's index, never a
  // transcript of the image. The readers below are a COPY of
  // lib/ancestry-record.js (the canonical, tested module); tests/test-sync.js
  // fails if they drift.
  // FHA-SYNC-BEGIN ancestry
  // A record has one identity wherever it appears: {dbId}:{recordId}, at
  // /search/collections/{dbId}/records/{recordId} (older links:
  // /discoveryui-content/view/{recordId}:{dbId}). null for anything else.
  function ancParseRecordPage(href) {
    let u;
    try {
      u = new URL(href);
    } catch (e) {
      return null;
    }
    if (!/(^|\.)ancestry\.[a-z.]+$/i.test(u.hostname)) return null;
    let m = /\/search\/collections\/(\d+)\/records\/(\d+)/.exec(u.pathname);
    if (m) return { dbId: m[1], recordId: m[2] };
    m = /\/discoveryui-content\/view\/(\d+):(\d+)/.exec(u.pathname);
    if (m) return { dbId: m[2], recordId: m[1] };
    return null;
  }

  // The scan a record page links to, for THIS collection only (a household
  // member's link points at another record, never at this record's image):
  // /imageviewer/collections/{dbId}/images/{imageId}, or in image-only
  // collections /interactive/{dbId}/{imageId}/{recordId}. null otherwise.
  function ancImageFromHref(href, dbId) {
    const s = String(href || '');
    const m = /\/imageviewer\/collections\/(\d+)\/images\/([^/?#]+)/.exec(s) ||
      /\/interactive\/(\d+)\/([^/?#]+)\/\d+/.exec(s);
    if (!m || m[1] !== String(dbId)) return null;
    try {
      return decodeURIComponent(m[2]);
    } catch (e) {
      return m[2]; // a stray % that is not valid encoding: keep the raw segment
    }
  }

  // Seen It's own elements (seen-it/content*.js): its pills, marks and tree
  // chips, and its badges and banners, whose ids all start "nsi-". Seen It also
  // toggles a few "nsi-" modifier classes (nsi-ruled-out, nsi-was-seen,
  // nsi-has-clip) on the SITE's own rows; those rows are the site's index and
  // are kept, which is why this is a list and not every "nsi-" class.
  // tests/test-ancestry.js reads seen-it/ and fails if it creates a class
  // this list does not know.
  const ANC_SEEN_IT_CLASSES = ['nsi-pills', 'nsi-pill', 'nsi-marks', 'nsi-mark', 'nsi-node-chip'];

  function ancIsSeenItNode(el) {
    if (!el || el.nodeType !== 1) return false;
    if (/^nsi-/.test(String(el.id || ''))) return true;
    const cls = String(el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className || '');
    return cls.split(/\s+/).some((c) => ANC_SEEN_IT_CLASSES.indexOf(c) >= 0);
  }

  const ANC_SKIP_TAGS = ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'BUTTON', 'svg', 'SVG'];
  const ANC_BLOCK_TAGS = ['ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'DD', 'DIV', 'DL', 'DT',
    'FIGCAPTION', 'FIGURE', 'FOOTER', 'FORM', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'HEADER',
    'HR', 'LI', 'MAIN', 'NAV', 'OL', 'P', 'PRE', 'SECTION', 'TABLE', 'TBODY', 'TD', 'TFOOT',
    'TH', 'THEAD', 'TR', 'UL'];

  // An element's text as a reader sees it: line breaks between blocks, spaces
  // tidied, and nothing from Seen It, scripts, buttons ("Show more", "Save") or
  // anything `opts.hidden(el)` says is not shown. `opts.block(el, tag)` says
  // whether an element starts a new line (on the live page, its CSS display;
  // without one, the usual block tags). `opts.skip` is one more element to
  // leave out (a section's own heading). Not innerText: that would read Seen
  // It's badges as if they were the record.
  function ancText(node, opts) {
    const o = opts || {};
    const parts = [];
    (function walk(n) {
      if (!n) return;
      if (n.nodeType === 3) {
        parts.push(String(n.nodeValue || '').replace(/\s+/g, ' '));
        return;
      }
      if (n.nodeType !== 1 && n.nodeType !== 9 && n.nodeType !== 11) return;
      if (n.nodeType === 1) {
        const tag = String(n.tagName || '').toUpperCase();
        if (n === o.skip || ANC_SKIP_TAGS.indexOf(tag) >= 0 || ancIsSeenItNode(n)) return;
        if (o.hidden && o.hidden(n)) return;
        if (tag === 'BR') {
          parts.push('\n');
          return;
        }
        const block = o.block ? o.block(n, tag) : ANC_BLOCK_TAGS.indexOf(tag) >= 0;
        if (block) parts.push('\n');
        for (const c of n.childNodes || []) walk(c);
        if (block) parts.push('\n');
        return;
      }
      for (const c of n.childNodes || []) walk(c);
    })(node);
    return parts.join('')
      .split('\n')
      .map((l) => l.replace(/[ \t ]+/g, ' ').trim())
      .filter(Boolean)
      .join('\n');
  }

  function ancElements(root, pred, out) {
    const list = out || [];
    for (const c of (root && root.childNodes) || []) {
      if (c.nodeType !== 1) continue;
      if (pred(c)) list.push(c);
      ancElements(c, pred, list);
    }
    return list;
  }

  function ancTag(el) {
    return String(el.tagName || '').toUpperCase();
  }

  function ancById(root, id) {
    return ancElements(root, (e) => e.id === id)[0] || null;
  }

  // A table's own rows and cells - never those of a table nested inside it.
  function ancTableRows(table, text) {
    const rows = [];
    (function walk(n) {
      for (const c of n.childNodes || []) {
        if (c.nodeType !== 1) continue;
        const tag = ancTag(c);
        if (tag === 'TABLE') continue;
        if (tag === 'TR') {
          const cells = (c.childNodes ? [...c.childNodes] : [])
            .filter((td) => td.nodeType === 1 && (ancTag(td) === 'TD' || ancTag(td) === 'TH'))
            .map((td) => text(td).replace(/\n+/g, ' '));
          if (cells.some(Boolean)) rows.push(cells);
        } else {
          walk(c);
        }
      }
    })(table);
    return rows;
  }

  // The tree-hint prompt Ancestry can put in an <h1>; it is not the record.
  const ANC_HINT_RE = /match the person in your tree|^does\b.+\?$/i;

  // The site's name at the end of a tab title, on every Ancestry site and in
  // both forms it uses: "… - Ancestry.com", "… - Ancestry.co.uk", "… | Ancestry".
  const ANC_TITLE_SUFFIX_RE = /\s*[-|\u2013]\s*Ancestry(\.com)?(\.[a-z.]+)?\s*$/i;

  // The whole record from a page (`root`: the document, or a stand-in tree):
  // { ok, record: {dbId, recordId, imageId, url, name, collection}, fields:
  // [{label, value}], household: [[cells]...] per table, citation: {heading:
  // text} }. `opts.title` is the document title; `opts.origin` the site origin;
  // `opts.hidden` / `opts.block` are ancText's (the live page passes its CSS).
  function ancRecordFrom(root, href, opts) {
    const o = opts || {};
    const id = ancParseRecordPage(href);
    if (!id) return { ok: false, error: 'this is not an Ancestry record page' };
    const text = (el, skip) => (el ? ancText(el, { hidden: o.hidden, block: o.block, skip }) : '');

    const fields = [];
    const box = ancById(root, 'recordFieldsContainer') || ancById(root, 'recordData');
    const dl = box ? ancElements(box, (e) => ancTag(e) === 'DL')[0] : null;
    if (dl) {
      let cur = null;
      for (const c of dl.childNodes || []) {
        if (c.nodeType !== 1 || ancIsSeenItNode(c)) continue;
        if (ancTag(c) === 'DT') {
          cur = { label: text(c), value: '' };
          fields.push(cur);
        } else if (ancTag(c) === 'DD' && cur) {
          cur.value = text(c);
        }
      }
    }

    // The household: every table under #recordData that holds rows of its own
    // (the site wraps the household table in a layout table; that wrapper's
    // rows are only the inner table again, so tables holding tables are passed).
    const household = [];
    const data = ancById(root, 'recordData');
    if (data) {
      for (const table of ancElements(data, (e) => ancTag(e) === 'TABLE')) {
        if (ancElements(table, (e) => ancTag(e) === 'TABLE').length) continue;
        const rows = ancTableRows(table, text);
        if (rows.length) household.push(rows);
      }
    }

    const citation = {};
    const sc = ancById(root, 'sourceCitation');
    if (sc) {
      for (const sec of ancElements(sc, (e) => ancTag(e) === 'SECTION')) {
        const h = ancElements(sec, (e) => ancTag(e) === 'H3')[0];
        if (!h || ancElements(sec, (e) => ancTag(e) === 'SECTION').length) continue;
        const heading = text(h);
        const body = text(sec, h);
        if (heading && body) citation[heading] = body;
      }
    }

    let imageId = null;
    for (const a of ancElements(root, (e) => ancTag(e) === 'A')) {
      if (ancIsSeenItNode(a)) continue;
      imageId = ancImageFromHref(a.getAttribute ? a.getAttribute('href') : '', id.dbId);
      if (imageId) break;
    }

    const h1 = ancElements(root, (e) => ancTag(e) === 'H1').map((e) => text(e)).find((t) => t && !ANC_HINT_RE.test(t));
    const collection = String(o.title || '').replace(ANC_TITLE_SUFFIX_RE, '').trim();
    const origin = o.origin || 'https://www.ancestry.com';
    return {
      ok: true,
      record: {
        dbId: id.dbId,
        recordId: id.recordId,
        imageId,
        url: origin + '/search/collections/' + id.dbId + '/records/' + id.recordId,
        name: (h1 && h1.split('\n')[0]) || null,
        collection: collection || null,
      },
      fields,
      household,
      citation,
    };
  }

  const ANC_MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9, oct: 10, nov: 11, dec: 12 };

  function ancMonth(word) {
    const w = String(word || '').toLowerCase().replace(/\.$/, '');
    const key = w === 'sept' ? 'sept' : w.slice(0, 3);
    const n = ANC_MONTHS[key];
    if (!n) return null;
    // A full name must really be that month ("Marchant" is not March).
    const full = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
      'september', 'october', 'november', 'december'][n - 1];
    return w.length <= 4 || full === w ? n : null;
  }

  const pad2 = (n) => (n < 10 ? '0' : '') + n;

  // An Ancestry index date in the archive's form (EDTF), or null when it is not
  // one of the plain shapes: "7 Jan 1950" -> "1950-01-07", "Jan 1950" ->
  // "1950-01", "1950" -> "1950", and "abt 1845" (about) -> "1845~". Anything
  // else - "bef 1900", a range, a place by mistake - is null: no date is better
  // than a wrong one, and the reviewer adds what the record supports.
  function ancDateToEdtf(value) {
    let v = String(value || '').trim().replace(/\s+/g, ' ');
    let approx = '';
    const a = /^(?:abt\.?|about|circa|ca\.?|c\.)\s+(.+)$/i.exec(v);
    if (a) {
      v = a[1];
      approx = '~';
    }
    let m = /^(\d{1,2}) ([A-Za-z]{3,9}\.?),? (\d{4})$/.exec(v);
    if (m) {
      const mo = ancMonth(m[2]);
      const d = Number(m[1]);
      return mo && d >= 1 && d <= 31 ? m[3] + '-' + pad2(mo) + '-' + pad2(d) + approx : null;
    }
    m = /^([A-Za-z]{3,9}\.?) (\d{1,2}),? (\d{4})$/.exec(v);
    if (m) {
      const mo = ancMonth(m[1]);
      const d = Number(m[2]);
      return mo && d >= 1 && d <= 31 ? m[3] + '-' + pad2(mo) + '-' + pad2(d) + approx : null;
    }
    m = /^([A-Za-z]{3,9}\.?) (\d{4})$/.exec(v);
    if (m) {
      const mo = ancMonth(m[1]);
      return mo ? m[2] + '-' + pad2(mo) + approx : null;
    }
    m = /^(\d{4})$/.exec(v);
    return m ? m[1] + approx : null;
  }

  // The event a collection is about, as the words its date fields start with,
  // in the order to try them. A burial, cemetery or funeral-home record often
  // gives only the death date; an obituary's event is the death.
  const ANC_EVENTS = [
    [/\bdeaths?\b|\bdied\b|\bmortality\b/i, ['Death']],
    [/\bburials?\b|\bgraves?\b|\bcemeter(y|ies)\b|\bfunerals?\b/i, ['Burial', 'Death']],
    [/\bmarriages?\b|\bmarried\b/i, ['Marriage']],
    [/\bbirths?\b|\bborn\b/i, ['Birth']],
    [/\bbaptisms?\b/i, ['Baptism']],
    [/\bchristenings?\b/i, ['Christening']],
    [/\bdivorces?\b/i, ['Divorce']],
  ];
  const ANC_OBITUARY_RE = /\bobituar(y|ies)\b/i;

  // A collection that runs over years - "1940-1997", "1930-Current",
  // "1800s-present", "1847-Current" - names when it was gathered, not when the
  // record's event happened, so its years never date the record.
  const ANC_RANGE_RE = /\b\d{4}s?\s*[-\u2013/]\s*(current|present|\d{2,4}s?)\b/i;

  // A Find a Grave memorial, or an index of them ("U.S., Find a Grave Index"):
  // a volunteer's page built from the stone, records and family knowledge. It
  // dates like a burial record but is a website, not a vital record - filed as
  // one, its claims would default to high confidence (SPEC §8.5) on a
  // stranger's transcription.
  const ANC_FINDAGRAVE_RE = /\bfind\s*a\s*grave\b/i;

  // What a record says about itself, for filing (TI-066), on any site: from
  // the collection name and `dateOf(event)` - the record's own date for "Death",
  // "Marriage", "Census"… as EDTF, or null - never from the rest of the page:
  //   sourceType  census for a census; website for Find a Grave; vital-record
  //               for a birth, marriage, death, burial, baptism or divorce
  //               collection; newspaper for an obituary or newspaper index;
  //               else null (let filing decide);
  //   sourceDate  the record's own event date ("Death Date" in a death index;
  //               "Burial Date", else "Death Date", for a burial, cemetery or
  //               funeral record; "Death Date" for an obituary), else a census's
  //               year, else the collection's one year when it names exactly one;
  //               never a year of a range, closed or open ("Death Index,
  //               1940-1997" or "Obituary Collection, 1930-Current" says when
  //               the collection runs, not when she died); always null for a
  //               derivative record, which was made later than its event;
  //   sourceClass 'derivative' for an index (a compiled list, not the record
  //               itself) or Find a Grave, else null (filing's default, original).
  function recordHints(collection, dateOf) {
    const c = String(collection || '');
    const ask = typeof dateOf === 'function' ? dateOf : () => null;
    const census = /\bcensus\b/i.test(c);
    // Every event the collection names, in the order ANC_EVENTS gives them:
    // a "Birth and Death Records" entry is a death record when it has a death
    // date, else a birth record.
    const events = [];
    if (!census) {
      for (const [re, words] of ANC_EVENTS) {
        if (re.test(c)) for (const w of words) if (events.indexOf(w) < 0) events.push(w);
      }
    }
    const dateWords = census ? ['Census', 'Residence']
      : events.length ? events : ANC_OBITUARY_RE.test(c) ? ['Death'] : [];

    const findagrave = ANC_FINDAGRAVE_RE.test(c);
    let sourceType = null;
    if (census) sourceType = 'census';
    else if (findagrave) sourceType = 'website';
    else if (events.length) sourceType = 'vital-record';
    else if (/\bnewspapers?\b|\bobituar(y|ies)\b/i.test(c)) sourceType = 'newspaper';

    let sourceDate = null;
    for (const word of dateWords) {
      sourceDate = ask(word) || null;
      if (sourceDate) break;
    }
    const years = c.match(/\b(1[5-9]\d{2}|20\d{2})\b/g) || [];
    const isRange = ANC_RANGE_RE.test(c);
    if (!sourceDate && census) {
      const lead = /^\s*(\d{4})\b/.exec(c);
      if (lead) sourceDate = lead[1];
      else if (years.length === 1) sourceDate = years[0];
    }
    if (!sourceDate && !census && years.length === 1 && !isRange) sourceDate = years[0];

    const sourceClass = findagrave || /\bindex(es)?\b/i.test(c) ? 'derivative' : null;
    // A derivative record (an index entry, a memorial) is never dated by the
    // event it reports: it was made later, on a day the page does not give.
    if (sourceClass === 'derivative') sourceDate = null;
    return { sourceType, sourceDate, sourceClass };
  }

  // ancHints for an Ancestry record: its labelled fields answer "{Event} Date"
  // (or "{Event} Year") in Ancestry's own date words.
  function ancHints(record, fields) {
    const list = Array.isArray(fields) ? fields : [];
    return recordHints((record && record.collection) || '', (word) => {
      const re = new RegExp('^' + word + ' (date|year)$', 'i');
      const f = list.find((x) => re.test(String(x.label || '').trim()));
      return f ? ancDateToEdtf(f.value) : null;
    });
  }

  // The bundle's title from the form's UNTOUCHED prefill (a title she typed
  // never comes here - the panel uses it as typed, PR #214 review): that title,
  // unless the form still holds the tab's own title ("1920 United States Federal Census - Ancestry.com", which
  // names only the collection; on any Ancestry site, ANC_TITLE_SUFFIX_RE) or
  // nothing; then "{name} - {collection}".
  function ancTitle(record, formTitle) {
    const r = record || {};
    const auto = [r.name, r.collection].filter(Boolean).join(' - ') ||
      'Ancestry record ' + r.dbId + ':' + r.recordId;
    const typed = String(formTitle || '').trim();
    return !typed || ANC_TITLE_SUFFIX_RE.test(typed) ? auto : typed;
  }

  // ── shared photos (the media viewer) ──
  // A photo or story another member shared to their tree opens at
  // /mediaui-viewer/tree/{treeId}/person/{personId}/media/{mediaId}, with
  // query parameters (destTreeId, hid, …) that only say how she got there.
  // The media id - a GUID - is the photo's identity: the same in every tree
  // that saved it. Seen It keys it med:{mediaId}, lowercased
  // (seen-it/host/seenit_host.py), from the address this returns as `url`:
  // the viewer's own tree/person/media path, without the query.
  const ANC_GUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
  const ANC_MEDIA_PAGE_RE = new RegExp('^/mediaui-viewer/((?:[^/?#]+/)*?)media/(' + ANC_GUID + ')/?$', 'i');

  // The shared photo a viewer address names: { mediaId, treeId, personId, url },
  // or null for anything else.
  function ancParseMediaPage(href) {
    let u;
    try {
      u = new URL(href);
    } catch (e) {
      return null;
    }
    if (!/(^|\.)ancestry\.[a-z.]+$/i.test(u.hostname)) return null;
    const m = ANC_MEDIA_PAGE_RE.exec(u.pathname);
    if (!m) return null;
    const mediaId = m[2].toLowerCase();
    const tree = /(?:^|\/)tree\/(\d+)\//.exec(m[1]);
    const person = /(?:^|\/)person\/(\d+)\//.exec(m[1]);
    return {
      mediaId,
      treeId: tree ? tree[1] : null,
      personId: person ? person[1] : null,
      url: u.origin + '/mediaui-viewer/' + m[1] + 'media/' + mediaId,
    };
  }

  // The photo's image as the viewer shows it: Ancestry's media service at
  // /api/media/retrieval/v2/image/namespaces/{ns}/media/{mediaId}, asked for
  // its "hq" size (1200x1920 on the page this was built from). The page's
  // thumbnails of the same photo (…/media/{mediaId}.jpg?MaxSide=…) name the
  // namespace too, so either will do; another photo's image never does. Some
  // photos - a newspaper clipping shared from Newspapers.com, say - keep their
  // picture under an image id of its own: the viewer's <img> then carries the
  // photo's media id as its id attribute, and its src names that image id,
  // which is the one to fetch (asked for by the media id, Ancestry answers
  // "not found", and the capture failed with "the photo has not appeared on the
  // page yet" - issue #261). Only the picture the viewer marks with this
  // photo's id is taken that way, never another photo's. The address is
  // always on the page's own site (`origin`), so the signed-in session the
  // fetch carries goes nowhere else. null while no image of THIS photo is on
  // the page. Whether Ancestry keeps a larger original behind the page's own
  // Download menu is not known; this is the size the page shows. The photo is
  // still filed under its media id (ancPhotoFileStem), the id Seen It knows.
  const ANC_MEDIA_IMG_RE = new RegExp('/api/media/retrieval/v2/image/namespaces/(\\d+)/media/(' + ANC_GUID +
    ')(\\.jpg)?(?=[?#]|$)', 'i');

  function ancMediaImageUrl(root, mediaId, origin) {
    const want = String(mediaId || '').toLowerCase();
    if (!want) return null;
    // Each [namespace, image id]. The viewer's own picture of the media id
    // names the namespace best; then the picture marked with the photo's id;
    // then a thumbnail of the media id.
    let full = null;
    let marked = null;
    let thumb = null;
    for (const img of ancElements(root, (e) => ancTag(e) === 'IMG')) {
      if (ancIsSeenItNode(img) || !img.getAttribute) continue;
      const ours = String(img.getAttribute('id') || '').trim().toLowerCase() === want;
      for (const attr of ['src', 'data-src']) {
        const m = ANC_MEDIA_IMG_RE.exec(String(img.getAttribute(attr) || ''));
        if (!m) continue;
        const image = m[2].toLowerCase();
        if (image === want && m[3]) thumb = thumb || [m[1], image];
        else if (image === want) full = full || [m[1], image];
        else if (ours) marked = marked || [m[1], image];
      }
    }
    const pick = full || marked || thumb;
    if (!pick) return null;
    return String(origin || 'https://www.ancestry.com') + '/api/media/retrieval/v2/image/namespaces/' + pick[0] +
      '/media/' + pick[1] + '?client=trees-mediaservice&imageQuality=hq';
  }

  // The labels of the photo's Details panel, as the page shows them.
  const ANC_MEDIA_LABELS = [
    ['title', /^title$/i],
    ['date', /^(date|date taken)$/i],
    ['place', /^(place|location)$/i],
    ['description', /^description$/i],
    ['originallyAddedBy', /^(originally )?(added|uploaded|shared) by$/i],
    ['linkedTo', /^(linked|attached) to$/i],
  ];
  // The lists of OTHER members a photo page shows - who saved it, and their
  // comments. Never kept: their names are not the family's. Recognised only
  // where they are shaped like a heading or a label (ancMediaLabel), never
  // from words in a sentence: "Saved by Aunt Ethel from the 1938 farmhouse
  // fire" in a description is the family's story, and is kept.
  //   "Saved by", "Saved by 2 members", "Saved by (2)", "Also saved by",
  //   "Saved to 3 trees", "Members who saved this", "Comments (4)"
  const ANC_MEDIA_SAVED_HEADING_RE = /^((also\s+)?saved\s+(by|to)|(\d+\s+)?(other\s+)?(members?|people|users)\s+(who|that)\s+(also\s+)?saved(\s+this(\s+photo)?)?)(\s*\(?\d+\)?)?(\s+(other\s+)?(members?|people|users|others|trees?))?$/i;
  const ANC_MEDIA_COMMENTS_RE = /^comments?(\s*\(?\d+\)?)?$/i;
  // The one-line form, the names in links: "Saved by <a>name</a> and 3 others".
  const ANC_MEDIA_SAVED_INLINE_RE = /^(also\s+)?saved\s+by(\s*,|\s+and\s+\d+\s+others?|\s+and)*$/i;
  // ...and in plain text, a member's username (one word with a digit, "_" or
  // ".") or one name and "and N others": "Saved by cousin_member_one". Never a
  // sentence: "Saved by Aunt Ethel from the 1938 farmhouse fire." is a story.
  const ANC_MEDIA_SAVED_NAMED_RE = /^(also\s+)?saved\s+by\s+([a-z0-9._-]*[0-9_.][a-z0-9._-]*(\s*,\s*[a-z0-9._-]+)*(\s+and\s+\d+\s+others?)?|[^\s,.]+\s+and\s+\d+\s+others?)$/i;

  // The one-line form of a members list - its names in links, or a username.
  function ancMediaSavedInline(el, own) {
    return (ANC_MEDIA_SAVED_INLINE_RE.test(own) && ancElements(el, (e) => ancTag(e) === 'A').length > 0) ||
      ANC_MEDIA_SAVED_NAMED_RE.test(own);
  }
  const ANC_MEDIA_HEADING_TAGS = ['H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'DT', 'TH', 'LEGEND', 'LABEL', 'SUMMARY'];
  // "Originally added by hartley_genealogy on 3 Mar 2015", on one line.
  const ANC_MEDIA_INLINE_RE = /^((?:originally )?(?:added|uploaded|shared) by)\s*:?\s+(.+)$/i;
  const ANC_MEDIA_INLINE_START_RE = /^((?:originally )?(?:added|uploaded|shared) by)\b/i;
  // A line starting "Originally added by" (or "Originally shared/uploaded by")
  // is always the upload line, whatever follows - a link, a display name,
  // "(Ruth Hartley)": it is never a description (PR #258 round 7). A plain
  // "Added by" / "Shared by" / "Uploaded by" is one only when it is shaped like
  // one: the phrase, then the uploader in a link or one username-like word,
  // optionally "on <date>"; or a name of one to four words followed by "on
  // <date>" - a real date (round 8). Nothing else. "Shared by Aunt Ethel
  // Hartley at the 1950 reunion; she is the girl on the left." and "Shared by
  // Aunt Ethel on her 90th birthday" are the family's description, and stay
  // one (rounds 6, 8).
  const ANC_MEDIA_ORIGINALLY_RE = /^(originally (?:added|uploaded|shared) by)\b\s*:?\s*/i;
  const ANC_MEDIA_DATE = '(?:\\d{1,2}\\s+[a-z]{3,9}\\.?,?\\s+\\d{4}|[a-z]{3,9}\\.?\\s+\\d{1,2},?\\s+\\d{4}|' +
    '[a-z]{3,9}\\.?\\s+\\d{4}|\\d{1,2}[/.-]\\d{1,2}[/.-]\\d{2,4}|\\d{4}-\\d{2}-\\d{2}|\\d{4})';
  const ANC_MEDIA_PHRASE = '((?:originally )?(?:added|uploaded|shared) by)\\s*:?';
  const ANC_MEDIA_ATTRIB_TAIL_RE = new RegExp('^' + ANC_MEDIA_PHRASE + '\\s*[^.!?;]{0,40}$', 'i');
  const ANC_MEDIA_ATTRIB_LINKED_RE = new RegExp('^' + ANC_MEDIA_PHRASE + '\\s*(on\\s+' + ANC_MEDIA_DATE + ')?$', 'i');
  const ANC_MEDIA_ATTRIB_WORD_RE = new RegExp('^' + ANC_MEDIA_PHRASE + '\\s+[a-z0-9_.@-]*[a-z0-9_@-](\\s+on\\s+' +
    ANC_MEDIA_DATE + ')?$', 'i');
  const ANC_MEDIA_ATTRIB_NAME_RE = new RegExp('^' + ANC_MEDIA_PHRASE + "\\s+[a-z][a-z'\u2019.-]*(?:\\s+[a-z][a-z'\u2019.-]*){0,3}" +
    '\\s+on\\s+' + ANC_MEDIA_DATE + '$', 'i');
  // The uploader a section headed "Originally added by" names: its first line,
  // when that is a link text or a name (one to four words), optionally "on
  // <date>" - never a list of other members or a sentence after it (round 8).
  const ANC_MEDIA_UPLOADER_RE = new RegExp("^[^\\s,;:!?()]+(?:\\s+[^\\s,;:!?()]+){0,3}?(?:\\s+on\\s+" + ANC_MEDIA_DATE + ')?$', 'i');

  // An attribution line - { phrase, who } - or null. `text(n)` reads a node.
  function ancMediaAttribution(el, text) {
    const own = ancOwnText(el);
    if (!ANC_MEDIA_INLINE_START_RE.test(own)) return null;
    const line = text(el).split('\n')[0];
    const originally = ANC_MEDIA_ORIGINALLY_RE.exec(own) && ANC_MEDIA_ORIGINALLY_RE.exec(line);
    if (originally) return { phrase: originally[1], who: line.slice(originally[0].length).trim() };
    const links = ancElements(el, (e) => ancTag(e) === 'A');
    const linked = links.length > 0 && (ANC_MEDIA_ATTRIB_LINKED_RE.test(own) ||
      // A linked username with any short tail that is no sentence - "Added by
      // <a>hartley_family_tree</a> 3 years ago": a username is never the
      // family's words (round 9). A display name in the link ("<a>Aunt Ethel</a>
      // on her 90th birthday") still needs a real date.
      (/^[a-z0-9_.@-]+$/i.test(text(links[0]).trim()) &&
        ANC_MEDIA_ATTRIB_TAIL_RE.test(own)));
    if (!linked && !ANC_MEDIA_ATTRIB_WORD_RE.test(line) && !ANC_MEDIA_ATTRIB_NAME_RE.test(line)) return null;
    const m = ANC_MEDIA_INLINE_RE.exec(line);
    return m ? { phrase: m[1], who: m[2].trim() } : null;
  }

  // The text an element holds directly (not in its child elements). A live
  // NodeList has no filter, so the children are copied first.
  function ancOwnText(el) {
    return [...(el.childNodes || [])].filter((c) => c.nodeType === 3)
      .map((c) => String(c.nodeValue || '')).join(' ').replace(/\s+/g, ' ').trim();
  }

  // Which Details label an element is - its own words, a colon allowed - or
  // null: a key of ANC_MEDIA_LABELS, or 'savedBy' / 'comments' for the lists
  // of other members. A members list is a heading element starting "Saved
  // by", a label in one of the forms above, or the one-line form whose names
  // are links - never a paragraph of words.
  function ancMediaLabel(el) {
    const own = ancOwnText(el).replace(/\s*:$/, '');
    if (!own) return null;
    const heading = ANC_MEDIA_HEADING_TAGS.indexOf(ancTag(el)) >= 0;
    if (ANC_MEDIA_SAVED_HEADING_RE.test(own) || (heading && /^(also\s+)?saved\s+(by|to)\b/i.test(own))) return 'savedBy';
    if (ancMediaSavedInline(el, own)) return 'savedBy';
    if (ANC_MEDIA_COMMENTS_RE.test(own)) return 'comments';
    for (const [key, re] of ANC_MEDIA_LABELS) if (re.test(own)) return key;
    return null;
  }

  // A label's value: what the label holds besides its own words, else what
  // follows it - its next siblings, up to the next label - or, for a label
  // alone in its box, what follows that box. Never read from outside `scope`
  // (the photo's Details box, or the page when it has none). `text(n)` reads
  // a node.
  function ancMediaValue(el, text, scope) {
    const tidy = (parts) => parts.join('\n').split('\n').map((l) => l.replace(/\s+/g, ' ').trim())
      .filter(Boolean).join('\n');
    // Where one field ends: the next label, or an upload attribution
    // ("Originally added by <a>name</a> on …"), which is never a value of
    // another field (PR #258 round 5, Codex PRRT_kwDOS6vm6c61yqx6) - only one
    // shaped like attribution, never a sentence (round 6).
    const boundary = (n) => !!ancMediaLabel(n) || !!ancMediaAttribution(n, text);
    const holdsLabel = (n) => n.nodeType === 1 && (boundary(n) || ancElements(n, boundary).length > 0);
    const inside = tidy([...(el.childNodes || [])].filter((c) => c.nodeType === 1).map((c) => text(c)));
    if (inside) return inside;
    let node = el;
    for (let up = 0; up < 3 && node && node !== scope && node.parentNode; up++) {
      const sibs = [...(node.parentNode.childNodes || [])]; // a live NodeList has no indexOf
      const parts = [];
      for (let i = sibs.indexOf(node) + 1; i < sibs.length; i++) {
        const sib = sibs[i];
        if (holdsLabel(sib)) break;
        parts.push(sib.nodeType === 3 ? String(sib.nodeValue || '') : sib.nodeType === 1 ? text(sib) : '');
      }
      const v = tidy(parts);
      if (v) return v;
      node = node.parentNode; // alone in its box: read on after the box
    }
    return '';
  }

  // The photo's own Details box: an element whose own label says "Details" -
  // its aria-label, or the first heading inside it. When the page has one,
  // the photo's details are read from it alone (ancMediaDetails). A class or
  // id with "detail" in it is not enough: a linked person's "person-details"
  // card is not the photo's (PR #258 review).
  function ancMediaDetailsBoxes(root) {
    const DETAILS = /^(photo\s+|media\s+)?details$/i;
    const named = (e) => {
      const aria = e.getAttribute ? String(e.getAttribute('aria-label') || '').trim() : '';
      if (DETAILS.test(aria)) return true;
      // Its own heading: a child of it (or of its <header>), not one inside a
      // box within it - so the page around the Details box is not the box.
      const kids = [...(e.childNodes || [])].filter((c) => c.nodeType === 1);
      const heads = kids.concat(...kids.filter((c) => ancTag(c) === 'HEADER').map((c) => [...(c.childNodes || [])]));
      const h = heads.find((x) => x.nodeType === 1 && /^H[1-6]$/.test(ancTag(x)));
      return !!h && DETAILS.test(ancOwnText(h));
    };
    return ancElements(root, (e) => ['ASIDE', 'SECTION', 'DIV', 'ARTICLE', 'DIALOG', 'FORM'].indexOf(ancTag(e)) >= 0 &&
      named(e) && ancElements(e, ancMediaLabel).length > 0);
  }

  // Ancestry's own Details panel in its photo viewer (issue #261, from a real
  // page and the viewer's own script): the element whose id is exactly
  // "mediaViewerDetailsPanel". Its "Details" heading sits in a button in a
  // header shown only on phones, so the box rules above never found it, and a
  // shared photo's title, uploader and linked people were all left out; its
  // id finds it. Each field in it is a .mediaDataField: an icon whose
  // aria-label names the field - translated on Ancestry's other-language
  // sites, so the icon's class names it too - then the field's
  // .mediaDataFieldValue. The category - Ancestry's label for what the item
  // is, as its sharer chose it ("Portrait / Family Photo", "Document /
  // Certificate") - says whether it is filed as a photo or a document
  // (ancMediaKind). The transcription is not read.
  const ANC_MEDIA_PANEL_ID = 'mediaViewerDetailsPanel';
  const ANC_MEDIA_PANEL_FIELDS = [
    ['date', /^date$/i, 'iconCalendar'],
    ['place', /^location$/i, 'iconMap'],
    ['category', /^category$/i, 'iconTileLarge'],
    ['description', /^description$/i, 'iconNote'],
  ];
  const ANC_MEDIA_PANEL_KEYS = ['title', 'date', 'place', 'category', 'description', 'originallyAddedBy', 'linkedTo'];
  // Never read inside the panel: what Ancestry's AI suggests in place of an
  // empty field (an estimated date such as "1920s", a suggested description -
  // Ancestry's guess, never the words of whoever shared the photo), the
  // members who saved it (their names hidden for screen readers), the
  // comments, and the panel's "Save to someone else in your tree".
  const ANC_MEDIA_PANEL_NEVER = ['autodateField', 'photoInsightWrapper', 'iconAi', 'insightBtn', 'savedBy',
    'userCardPile', 'mediaComments', 'hideVisually', 'addMediaToPerson'];

  // An element's classes, as a list (an SVG element's className is an object).
  function ancClasses(el) {
    const cls = el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className;
    return String(cls || '').split(/\s+/).filter(Boolean);
  }

  // The photo's Details from Ancestry's own panel, by the panel's structure
  // alone - never by the label reading of the box rules: the panel's comments
  // are headed "Comments on this photo", and a commenter's "Shared by …" is
  // not who shared the photo. A value is only what a .mediaDataFieldValue
  // holds, less Ancestry's own prompts and icons: on her own photo, an empty
  // field holds Ancestry's "add a date" prompt (a .link), and that is no date;
  // a value with Ancestry's AI mark in it is no value of hers. The description
  // keeps its own line breaks (Ancestry shows them as written); the other
  // fields are one line. Who first shared it is the owner card's name, with
  // the date beside it ("hartley_family_tree on 3 Mar 2015"); the people it
  // is linked to are the names on the panel's "Linked to" buttons.
  // `hidden(el)`: is this element not shown on the page (it is left out).
  function ancMediaPanelDetails(panel, hidden) {
    const els = [];
    (function walk(n) {
      for (const c of n.childNodes || []) {
        if (c.nodeType !== 1 || ancIsSeenItNode(c) || (hidden && hidden(c))) continue;
        if (ancClasses(c).some((k) => ANC_MEDIA_PANEL_NEVER.indexOf(k) >= 0)) continue;
        els.push(c);
        walk(c);
      }
    })(panel);
    const has = (e, k) => !!e && e.nodeType === 1 && ancClasses(e).indexOf(k) >= 0;
    const up = (e, k) => {
      for (let n = e.parentNode; n && n !== panel; n = n.parentNode) if (has(n, k)) return n;
      return null;
    };
    // A value's words, line by line: all of its text - words in <em> or a
    // link inside it too, a <br> or a block a new line, its own line breaks
    // kept (the description's) - but never Ancestry's "Add a …" prompt (a
    // .link) or an icon inside it.
    const lines = (e) => {
      const parts = [];
      (function walk(n) {
        for (const c of n.childNodes || []) {
          if (c.nodeType === 3) {
            parts.push(String(c.nodeValue || ''));
            continue;
          }
          if (c.nodeType !== 1 || (hidden && hidden(c)) || ANC_SKIP_TAGS.indexOf(ancTag(c)) >= 0 || ancIsSeenItNode(c) ||
            has(c, 'link') || has(c, 'icon')) continue;
          if (ancTag(c) === 'BR') {
            parts.push('\n');
            continue;
          }
          const block = ANC_BLOCK_TAGS.indexOf(ancTag(c)) >= 0;
          if (block) parts.push('\n');
          walk(c);
          if (block) parts.push('\n');
        }
      })(e);
      return parts.join('').split('\n').map((l) => l.replace(/\s+/g, ' ').trim()).filter(Boolean);
    };
    // A value Ancestry marks as its AI's estimate is not hers.
    const markedAi = (e) => ancElements(e, (x) => ancClasses(x).some((k) => ANC_MEDIA_PANEL_NEVER.indexOf(k) >= 0)).length > 0;
    const words = (e) => ancText(e, { hidden }).replace(/\s+/g, ' ').trim();
    const found = {};
    for (const v of els.filter((e) => has(e, 'mediaDataFieldValue'))) {
      const box = v.parentNode;
      if (!has(box, 'mediaDataField') || markedAi(v)) continue;
      let key = has(box, 'mediaTitle') ? 'title' : null;
      if (!key) {
        const icon = [...(box.childNodes || [])].find((c) => c.nodeType === 1 && ancTag(c) === 'SPAN' && has(c, 'icon') &&
          c.getAttribute && String(c.getAttribute('aria-label') || '').trim());
        const label = icon ? String(icon.getAttribute('aria-label')).trim() : '';
        const def = icon && ANC_MEDIA_PANEL_FIELDS.find(([, re, cls]) => re.test(label) || has(icon, cls));
        key = def ? def[0] : null;
      }
      const value = lines(v).join(key === 'description' ? '\n' : ' ');
      if (key && value && !(key in found)) found[key] = value;
    }
    const owner = els.find((e) => has(e, 'mediaOwnerFullName') && up(e, 'mediaOwnerCard'));
    const name = owner ? words(owner) : '';
    if (name) {
      const card = up(owner, 'mediaOwnerCard');
      const when = els.find((e) => has(e, 'mediaOwnerDate') && up(e, 'mediaOwnerCard') === card);
      const date = when ? words(when) : '';
      found.originallyAddedBy = date ? name + ' on ' + date : name;
    }
    const linked = ancLinkedNames(els.filter((e) => has(e, 'linkedToName') && up(e, 'linkedToSection')).map(words).join('\n'));
    if (linked.length) found.linkedTo = linked;
    const details = {};
    for (const key of ANC_MEDIA_PANEL_KEYS) if (found[key]) details[key] = found[key];
    return details;
  }

  // The photo's Details as the page shows them: { title, date, place,
  // description, originallyAddedBy, linkedTo: [names] } - from Ancestry's own
  // Details panel when the page has one (ancMediaPanelDetails, issue #261);
  // otherwise read from the
  // photo's own Details box only (ancMediaDetailsBoxes: the element whose own
  // heading or aria-label says "Details"), whole, each field by its own label.
  // A label's value is ancMediaValue's, never read from outside the box, and
  // a label claims its key only with a value; the first such value wins -
  // except that "Originally added by" wins over a plain "Added by" / "Shared
  // by" read first (PR #258 round 5).
  //
  // Nothing outside the box is read - no description, no "Linked to", no
  // upload line beside it - and with no Details box on the page (closed, or a
  // layout the add-on does not recognise) nothing at all: when the page's
  // layout is unclear, the add-on leaves text out rather than file someone
  // else's words as hers, and anything left out can still be read on Ancestry
  // from the photo's saved address (PR #258 round 9, a reversible decision -
  // five rounds of guessing at the page around the box each let a relative's
  // facts or other members' names through in a new shape). Inside the box,
  // nothing is read from a members section ("Comments (2)", "Saved by …"),
  // and a field headed by the upload label gives only its first name
  // (ANC_MEDIA_UPLOADER_RE).
  // `opts.hidden` / `opts.block` are ancText's (the live page passes its CSS).
  function ancMediaDetails(root, opts) {
    const o = opts || {};
    const text = (n) => ancText(n, { hidden: o.hidden, block: o.block });
    const shown = (e) => {
      for (let n = e; n && n.nodeType === 1; n = n.parentNode) if (o.hidden && o.hidden(n)) return false;
      return true;
    };
    // Ancestry's own panel, when the page has one: read by its structure
    // alone (ancMediaPanelDetails), and nothing else - closed, it gives nothing.
    const panels = ancElements(root, (e) => e.id === ANC_MEDIA_PANEL_ID);
    if (panels.length) {
      const panel = panels.find(shown);
      return panel ? ancMediaPanelDetails(panel, o.hidden) : {};
    }
    const found = {};
    const rank = {};
    let scope = root; // a value is never read from outside it
    const claim = (key, value, weight) => {
      if (!value) return;
      if (key in found && !(weight > (rank[key] || 0))) return;
      found[key] = value;
      rank[key] = weight || 0;
    };
    const addedByWeight = (words) => (/^originally\b/i.test(words) ? 2 : 1);
    const walk = (n) => {
      for (const c of (n && n.childNodes) || []) {
        if (c.nodeType !== 1 || ANC_SKIP_TAGS.indexOf(ancTag(c)) >= 0 || ancIsSeenItNode(c)) continue;
        if ((o.hidden && o.hidden(c)) || ancMediaMembersSection(c)) continue;
        const key = ancMediaLabel(c);
        if (key) {
          let value = ancMediaValue(c, text, scope);
          if (key === 'originallyAddedBy') {
            const first = value.split('\n')[0].trim();
            value = ANC_MEDIA_UPLOADER_RE.test(first) ? first : '';
          }
          claim(key, value, key === 'originallyAddedBy' ? addedByWeight(ancOwnText(c)) : 0);
          continue;
        }
        // "Originally added by hartley_genealogy", in the one element whose own
        // words start with it (the name may sit in a link inside).
        const inline = ancMediaAttribution(c, text);
        if (inline) {
          claim('originallyAddedBy', inline.who, addedByWeight(inline.phrase));
          continue;
        }
        walk(c);
      }
    };
    for (const box of ancMediaDetailsBoxes(root).filter(shown)) {
      scope = box;
      walk(box);
    }
    const details = {};
    for (const [key] of ANC_MEDIA_LABELS) {
      // "Saved by" and comments are never kept: other members' names.
      if (!found[key]) continue;
      if (key === 'linkedTo') {
        const names = ancLinkedNames(found[key]);
        if (names.length) details.linkedTo = names;
      } else {
        details[key] = key === 'description' ? found[key] : found[key].replace(/\n+/g, ' ');
      }
    }
    return details;
  }

  // A section of other members' words: its first label or heading is
  // "Comments (N)" or a "Saved by …" list. Nothing inside it is read - a
  // commenter's "Shared by …" is not who added the photo.
  function ancMediaMembersSection(el) {
    const kids = [...(el.childNodes || [])].filter((c) => c.nodeType === 1);
    const head = kids.find((c) => /^H[1-6]$/.test(ancTag(c)));
    const key = (kids[0] && ancMediaLabel(kids[0])) || (head && ancMediaLabel(head)) || null;
    return key === 'comments' || key === 'savedBy';
  }

  // The people a shared photo is linked to, as names: one per line, without
  // the years Ancestry puts beside them ("Thomas Hartley 1840–1910", "abt
  // 1840–1910"); a living person Ancestry shows as "Private" or "Living …" is
  // no name.
  function ancLinkedNames(value) {
    const names = [];
    for (const line of String(value || '').split(/\n|;/)) {
      const n = line
        .replace(/\(?\s*((abt|about|bef|aft|circa|ca|c)\.?\s+)?(\d{4}|\?{1,4})?\s*[-–—]\s*((abt|about|bef|aft|circa|ca|c)\.?\s+)?(\d{4}|\?{1,4})?\s*\)?\s*$/i, '')
        .replace(/\s+/g, ' ').trim();
      if (n.length >= 2 && n.length <= 80 && !/^(private|living)\b/i.test(n) && names.indexOf(n) < 0) names.push(n);
    }
    return names;
  }

  // Does Ancestry's Details panel describe the item in the address? When she
  // clicks the viewer's arrow to the next item, the address and the picture
  // change before the panel does, and for a moment it still shows the last
  // item's title, date and people (#261 audit B1). The viewer's own picture
  // of the item - the <img> marked with its media id, or whose address names
  // it (never a thumbnail) - carries the item's title as its alt text, on
  // both real pages this was built from. So when that alt text and the
  // panel's title are both there and differ, the panel is still on another
  // item and nothing of it is filed: the capture waits (ANC_MEDIA_PANEL_BEHIND).
  // With no alt text, or no title in Ancestry's own panel, nothing is checked.
  const ANC_MEDIA_PANEL_BEHIND = 'the details on the page have not caught up with the picture yet (they still ' +
    'describe the item shown before), so nothing was saved - wait a moment, then press Capture again';

  function ancMediaPanelIsFor(root, mediaId, details) {
    const title = String((details && details.title) || '');
    if (!title || !ancElements(root, (e) => e.id === ANC_MEDIA_PANEL_ID).length) return true;
    const tidy = (s) => String(s || '').toLowerCase();
    return ancMediaOwnAlts(root, mediaId).every((alt) => tidy(alt) === tidy(title.replace(/\s+/g, ' ').trim()));
  }

  // The alt text of the viewer's own picture of this item (the <img> marked
  // with its media id, or whose address names it - never a thumbnail), spaces
  // tidied, the empty ones left out. It names this item's picture, so it is
  // the title kept when the Details panel is left out (ancMediaFrom).
  function ancMediaOwnAlts(root, mediaId) {
    const want = String(mediaId || '').toLowerCase();
    const alts = [];
    for (const img of ancElements(root, (e) => ancTag(e) === 'IMG')) {
      if (ancIsSeenItNode(img) || !img.getAttribute) continue;
      const marked = String(img.getAttribute('id') || '').trim().toLowerCase() === want;
      const m = ANC_MEDIA_IMG_RE.exec(String(img.getAttribute('src') || ''));
      const named = !!m && !m[3] && m[2].toLowerCase() === want;
      const alt = String(img.getAttribute('alt') || '').replace(/\s+/g, ' ').trim();
      if ((marked || named) && alt) alts.push(alt);
    }
    return alts;
  }

  // The whole shared photo from its viewer page (`root`: the document, or a
  // stand-in tree): { ok, media: { mediaId, treeId, personId, url, title,
  // imageUrl, fileStem }, details }. `opts.title` is the document title (the photo's
  // title, as whoever shared it wrote it); `opts.origin` the site origin.
  function ancMediaFrom(root, href, opts) {
    const o = opts || {};
    const id = ancParseMediaPage(href);
    if (!id) return { ok: false, error: 'this is not a photo shared on Ancestry' };
    const details = ancMediaDetails(root, o);
    const docTitle = String(o.title || '').replace(ANC_TITLE_SUFFIX_RE, '').trim();
    let origin = 'https://www.ancestry.com';
    try {
      origin = new URL(href).origin;
    } catch (e) {
      /* parsed above */
    }
    if (!ancMediaPanelIsFor(root, id.mediaId, details)) {
      // The panel is on another item: none of it. What names this item -
      // its address, its picture and that picture's alt text - is still given
      // (`behind`), for when she chooses to save it without its details.
      return {
        ok: false, transient: true, behind: true, error: ANC_MEDIA_PANEL_BEHIND,
        media: Object.assign({}, id, {
          title: ancMediaOwnAlts(root, id.mediaId)[0] || docTitle || null,
          imageUrl: ancMediaImageUrl(root, id.mediaId, o.origin || origin),
          fileStem: ancPhotoFileStem(id.mediaId),
        }),
      };
    }
    return {
      ok: true,
      media: Object.assign({}, id, {
        title: details.title || docTitle || null,
        imageUrl: ancMediaImageUrl(root, id.mediaId, o.origin || origin),
        fileStem: ancPhotoFileStem(id.mediaId),
      }),
      details,
      newspapersClip: ancMediaNewspapersClip(root),
    };
  }

  // A Newspapers.com clipping saved into a tree's gallery is an Ancestry copy:
  // a picture about 1200 pixels tall, under a media id of its own. The original
  // on Newspapers.com is better evidence (the whole page at full size, the
  // clipping cut from it, the site's citation), and the archive may hold it
  // already. The sharer's card says so: "originally shared <a>this article</a>
  // on <date> from Newspapers.com", in span.mediaOriginallyShared. Its link to
  // the clipping (/article/{slug}/{id}/, or the older /clip/{id}/{slug}/) gives
  // { clippingId, url }; `url` is that address on www.newspapers.com, path only.
  // Read from that card alone, never from a comment that links some other
  // clipping, and only when the card names exactly one.
  // An entry in Ancestry's "U.S., Find a Grave Index" (collection 60525) is an
  // index of one memorial. The memorial itself is the better evidence (its
  // photos, the family it links, the volunteer's notes), and the archive may
  // hold it already. The entry names it in its own "URL" field, a link inside
  // the record's fields (#recordFieldsContainer): { memorialId, url }, read
  // from there alone and only when the fields name exactly one memorial.
  const ANC_FAG_INDEX_DB = '60525';
  const ANC_FAG_MEMORIAL_RE = /^https?:\/\/(?:www\.)?findagrave\.com(\/memorial\/(\d+)(?:\/[^/?#]*)?\/?)(?:[?#]|$)/i;

  function ancRecordMemorial(root, ids) {
    if (!ids || ids.dbId !== ANC_FAG_INDEX_DB) return null;
    const box = ancById(root, 'recordFieldsContainer');
    if (!box) return null;
    const found = [];
    for (const a of ancElements(box, (e) => ancTag(e) === 'A')) {
      const m = ANC_FAG_MEMORIAL_RE.exec(String(a.getAttribute('href') || '').trim());
      const id = m ? String(Number(m[2])) : '';
      if (id && id !== '0' && !found.some((f) => f.memorialId === id)) {
        found.push({ memorialId: id, url: 'https://www.findagrave.com' + m[1] });
      }
    }
    return found.length === 1 ? found[0] : null;
  }

  const ANC_NP_CLIP_RE = /^https?:\/\/(?:www\.)?newspapers\.com(\/(?:article\/[^/?#]+\/(\d+)|(?:article|clip)\/(\d+))(?:\/[^/?#]*)?\/?)(?:[?#]|$)/i;

  function ancMediaNewspapersClip(root) {
    const found = [];
    for (const card of ancElements(root, (e) => ancClasses(e).indexOf('mediaOriginallyShared') >= 0)) {
      for (const a of ancElements(card, (e) => ancTag(e) === 'A')) {
        const m = ANC_NP_CLIP_RE.exec(String(a.getAttribute('href') || '').trim());
        const id = m ? Number(m[2] || m[3]) : 0;
        if (id > 0 && !found.some((f) => f.clippingId === id)) found.push({ clippingId: id, url: 'https://www.newspapers.com' + m[1] });
      }
    }
    return found.length === 1 ? found[0] : null;
  }

  // What kind of source a shared item is, from the category its sharer chose
  // on Ancestry (issue #261, the owner: an item Ancestry calls a document
  // "should be saved as a document"). The page shows only the category's
  // words, in the site's language, so they are matched loosely, in English:
  //   an obituary or newspaper article      newspaper
  //   a birth, death or marriage certificate vital-record
  //   a property record                      land-record
  //   another document - "Document / Certificate", a legal document, personal
  //   papers, a diploma, immigration - is what its title says (recordHints:
  //   "Obituary for Thomas Hartley" is a newspaper), else other;
  //   anything else - "Portrait / Family Photo", a site, a headstone, an
  //   heirloom, a recipe, an award, "Other", no category, or words in a
  //   language this does not know - a photo, as before: when unsure, nothing
  //   changes. So are military service, occupation, education and yearbook
  //   (the owner: they often hold portraits; for discharge papers or a
  //   diploma filed there she changes the Kind). She can always change the
  //   Kind before she captures it.
  function ancMediaKind(category, title) {
    const c = String(category || '').toLowerCase();
    if (/\bobituar|\bnewspaper/.test(c)) return 'newspaper';
    if (/\b(birth|death|marriage)\b/.test(c) && /\bcertificate/.test(c)) return 'vital-record';
    if (/\bproperty\b/.test(c)) return 'land-record';
    if (/\b(documents?|certificates?|legal|papers?|diploma|immigration)\b/.test(c)) {
      return recordHints(String(title || '')).sourceType || 'other';
    }
    return 'photo';
  }

  // What a shared item says about itself, for filing: its kind (ancMediaKind:
  // a photo unless its Ancestry category says it is a document), dated by the
  // date its Details give when that is one the archive can read ("1925",
  // "abt 1925", "12 Jun 1925"), else no date - never a guess.
  function ancMediaHints(details) {
    const d = details || {};
    return { sourceType: ancMediaKind(d.category, d.title), sourceDate: ancDateToEdtf(d.date) || null, sourceClass: null };
  }

  // The name the photo is filed under, without its extension. It lands in the
  // photo library and is never renamed there (SPEC §12.1), so it is unique to
  // the photo - its media id, letters, digits and dashes only, never the
  // free-text title - and ends in a word, never in what the archive reads as a
  // print letter ("…6f") or a back ("…-back"). null for anything not a media id.
  function ancPhotoFileStem(mediaId) {
    const id = String(mediaId || '').toLowerCase();
    return new RegExp('^' + ANC_GUID + '$').test(id) ? 'ancestry-' + id + '-photo' : null;
  }
  // FHA-SYNC-END ancestry

  // What the live page shows: ancText asks the page's CSS whether an element
  // is shown and whether it starts a new line, the way innerText would.
  const ANC_CSS = {
    hidden(el) {
      const st = getComputedStyle(el);
      return st.display === 'none' || st.visibility === 'hidden';
    },
    block(el) {
      const d = getComputedStyle(el).display;
      return !/^(inline|inline-block|inline-flex|inline-grid|contents)$/.test(d);
    },
  };

  // The record, plus what it says about itself (ancHints: type, date, class)
  // and the bundle's title - the human's `formTitle` unless it is still the
  // tab's own title (ancTitle).
  function ancestryRecordInfo(formTitle) {
    const info = ancRecordFrom(document, location.href, {
      title: document.title,
      origin: location.origin,
      hidden: ANC_CSS.hidden,
      block: ANC_CSS.block,
    });
    if (!info.ok) return info;
    info.hints = ancHints(info.record, info.fields);
    info.title = ancTitle(info.record, formTitle);
    return info;
  }

  // ── A photo someone shared on Ancestry (the media viewer) ────────────────────
  // On /mediaui-viewer/…/media/{mediaId} Capture saves the photo the page
  // shows, with its Details (title, date, place, description, who added it,
  // who it is linked to - never the "Saved by" list of other members), so it
  // files as a photo. The readers are in the ancestry block above.

  function ancestryPhotoInfo() {
    const info = ancMediaFrom(document, location.href, {
      title: document.title,
      origin: location.origin,
      hidden: ANC_CSS.hidden,
      block: ANC_CSS.block,
    });
    if (!info.ok) return info;
    info.hints = ancMediaHints(info.details);
    return info;
  }

  // The photo's image, in her session, from Ancestry's own site only: the
  // address ancMediaImageUrl builds is on this page's origin, and is checked
  // again here, so the session goes to no other host. The bytes are kept
  // exactly as Ancestry sends them. `mediaId` is the photo the panel read;
  // the page must still be showing it.
  async function fetchAncestryPhoto(mediaId) {
    const here = ancParseMediaPage(location.href);
    if (!here || here.mediaId !== String(mediaId || '').toLowerCase()) {
      return { ok: false, error: 'the page no longer shows this photo, so it was not fetched - go back to the ' +
        'photo and press Capture again' };
    }
    const url = ancMediaImageUrl(document, here.mediaId, location.origin);
    if (!url) {
      return { ok: false, transient: true, error: 'the photo has not appeared on the page yet - wait until it ' +
        'shows, then press Capture again' };
    }
    let target;
    try {
      target = new URL(url);
    } catch (e) {
      target = null;
    }
    if (!target || target.origin !== location.origin || target.protocol !== 'https:') {
      return { ok: false, error: 'the photo’s address is not on Ancestry’s own site, so it was not fetched' };
    }
    try {
      const resp = await fetch(target.href, { credentials: 'include', headers: { accept: 'image/*' } });
      if (!resp.ok) {
        const why = resp.status === 401 || resp.status === 403
          ? 'Ancestry refused the photo (HTTP ' + resp.status + ') - sign in on this page and press Capture again'
          : 'Ancestry would not hand over the photo (HTTP ' + resp.status + ')';
        return { ok: false, error: why, status: resp.status, slowDown: resp.status === 429 || resp.status === 503,
          transient: resp.status === 401 || resp.status === 429 || resp.status >= 500 };
      }
      const blob = await resp.blob();
      const type = String(blob.type || (resp.headers && resp.headers.get('content-type')) || '').split(';')[0].trim();
      if (!/^image\//i.test(type)) {
        return { ok: false, error: 'Ancestry sent a page instead of the photo - sign in on this page and press ' +
          'Capture again' };
      }
      const ext = extFromContentType(type, target.href);
      return { ok: true, base64: await blobToBase64(blob), ext: ext === 'bin' ? 'jpg' : ext, contentType: type,
        imageUrl: target.href };
    } catch (e) {
      return brokeOff(e);
    }
  }

  // ── FamilySearch records (the record's own data, through its data service) ───
  // On a record (ark:/61903/1:1:{id}) - or the image viewer opened from one
  // (personArk=) - Capture saves FamilySearch's own GEDCOM X data for the
  // record with its citation, and the full image behind ark:/61903/3:1:{id};
  // on an image viewer, the full image (dist.jpg), not the deep-zoom tile the
  // page draws. Through FamilySearch's data service, in the researcher's own
  // signed-in session. The readers are a COPY of lib/familysearch-record.js
  // (the canonical, tested module); tests/test-sync.js fails if they drift.
  // FHA-SYNC-BEGIN familysearch
  const FS_HOST_RE = /(^|\.)familysearch\.org$/i;

  // A FamilySearch record: ark:/61903/1:1:{id}, or the image viewer opened from
  // one (ark:/61903/3:1:{image}?…personArk=…1:1:{id}) - captured as the record
  // it was opened from, with this image. null for anything else.
  function fsParseRecordPage(href) {
    let u;
    try {
      u = new URL(href);
    } catch (e) {
      return null;
    }
    if (!FS_HOST_RE.test(u.hostname)) return null;
    const m = /\/ark:\/61903\/1:1:([A-Z0-9-]+)/i.exec(u.pathname);
    if (m) return { fsId: m[1].toUpperCase(), viewerImageId: null };
    const img = /\/ark:\/61903\/3:1:([A-Z0-9-]+)/i.exec(u.pathname);
    if (img) {
      const p = /1:1:([A-Z0-9-]+)/i.exec(u.searchParams.get('personArk') || '');
      if (p) return { fsId: p[1].toUpperCase(), viewerImageId: img[1].toUpperCase() };
    }
    return null;
  }

  // A bare film image: ark:/61903/3:1:{id} with NO record behind it (no
  // personArk), as reached by paging an unindexed film from the catalog. A viewer
  // opened from a record is still that record (fsParseRecordPage). null for
  // anything else. { imageId }.
  function fsParseImagePage(href) {
    let u;
    try {
      u = new URL(href);
    } catch (e) {
      return null;
    }
    if (!FS_HOST_RE.test(u.hostname) || fsParseRecordPage(href)) return null;
    const m = /\/ark:\/61903\/3:1:([A-Z0-9-]+)/i.exec(u.pathname);
    if (!m) return null;
    let q = u.search;
    try {
      q = decodeURIComponent(q);
    } catch (e) {
      /* a stray % that is not valid encoding: read the query as it is */
    }
    // A personArk in the query, however it is written, makes it a record's image.
    if (/personArk=[^&]*1:1:[A-Z0-9-]+/i.test(q)) return null;
    return { imageId: m[1].toUpperCase() };
  }

  // The image (ark:/61903/3:1:{id}) an address names, in its path or query.
  function fsImageIdFromHref(href) {
    let u;
    try {
      u = new URL(href);
    } catch (e) {
      return null;
    }
    if (!FS_HOST_RE.test(u.hostname)) return null;
    let query = u.search;
    try {
      query = decodeURIComponent(query);
    } catch (e) {
      /* a stray % that is not valid encoding: read the query as it is */
    }
    const m = /ark:\/61903\/3:1:([A-Z0-9-]+)/i.exec(u.pathname + query);
    return m ? m[1].toUpperCase() : null;
  }

  // The login header for `href` (resolved against `base`, the page's address):
  // { Authorization: 'Bearer …' } only for https on familysearch.org or a
  // subdomain of it, and only when someone is signed in (`token`); else {}.
  function fsAuthHeaders(href, base, token) {
    let u;
    try {
      u = new URL(href, base);
    } catch (e) {
      return {};
    }
    if (u.protocol !== 'https:' || !FS_HOST_RE.test(u.hostname) || u.username || u.password) return {};
    return token ? { Authorization: 'Bearer ' + token } : {};
  }

  // What an HTTP answer from FamilySearch means, in words that end in what to
  // do next. `what` is 'record' or 'image'; `signedIn` whether a login was sent.
  // Built from the status alone: no address, header or token ever goes into it.
  function fsStatusMessage(status, what, signedIn) {
    const thing = what === 'image' ? 'this image' : 'this record';
    if (status === 401 || (status === 403 && !signedIn)) {
      return 'FamilySearch asked you to sign in (HTTP ' + status + '). Sign in to FamilySearch in this tab, ' +
        'reload the page, then press Capture again';
    }
    if (status === 403) {
      return 'FamilySearch will not show ' + thing + ' here (HTTP 403): it is restricted to a FamilySearch ' +
        'center or affiliate library. It can be saved from a computer there';
    }
    if (status === 404) return 'FamilySearch has no ' + (what === 'image' ? 'image' : 'record') + ' at this address (HTTP 404) - check the page is the one you meant';
    if (status === 429) return 'FamilySearch asked us to slow down (HTTP 429). Wait a few minutes, then press Capture again';
    if (status >= 500) return 'FamilySearch is having trouble (HTTP ' + status + '). Try again in a little while';
    return 'FamilySearch answered HTTP ' + status + ' for ' + thing + '. Reload the page and press Capture again';
  }

  // GEDCOM X's formal date in the archive's form (EDTF): "+1957-01-07" ->
  // "1957-01-07", "+1957-01" -> "1957-01", "+1957", and "A+1887" (about) ->
  // "1887~". A range or open date ("+1880/+1881", "/+1900") is null: no date
  // beats a wrong one.
  function fsFormalToEdtf(formal) {
    const m = /^(A)?\+(\d{4})(-\d{2})?(-\d{2})?$/.exec(String(formal || '').trim());
    return m ? m[2] + (m[3] || '') + (m[4] || '') + (m[1] ? '~' : '') : null;
  }

  const fsTypeName = (t) => String(t || '').replace(/^.*[/#]/, '');
  const fsFirstTitle = (x) => (x && x.titles && x.titles[0] && x.titles[0].value) || null;

  // The person this record entry IS: the one whose persistent identifier names
  // ark 1:1:{fsId}. A marriage record has two principals - the bride's entry and
  // the groom's - so "the first principal" would title the bride's record with
  // the groom's name (PR #214 review). Failing an identifier, the principal,
  // else the first person.
  function fsOpenedPerson(g, fsId) {
    const persons = (g && Array.isArray(g.persons)) ? g.persons : [];
    const want = '1:1:' + String(fsId || '').toUpperCase();
    const names = (p) => {
      const ids = (p && p.identifiers) || {};
      return [].concat(...Object.values(ids).map((v) => (Array.isArray(v) ? v : [v])))
        .map((x) => String(x || '').toUpperCase());
    };
    return (fsId && persons.find((p) => names(p).some((x) => x.indexOf(want) >= 0))) ||
      persons.find((p) => p && p.principal) || persons[0] || null;
  }

  function fsPersonName(p) {
    const n = p && p.names && p.names[0];
    const f = n && n.nameForms && n.nameForms[0];
    return (f && String(f.fullText || '').trim()) || null;
  }

  // An entry in FamilySearch's "Find a Grave Index" (collection 2221801)
  // indexes one memorial. The page does not show which; the entry's data does,
  // in its own ExtRecordId field. The memorial's id, when the entry is in that
  // collection and names exactly one; else null.
  const FS_FAG_INDEX_COLLECTION = '2221801';
  function fsIndexedMemorial(g, collectionId) {
    if (collectionId !== FS_FAG_INDEX_COLLECTION) return null;
    const ids = [];
    for (const f of (g && Array.isArray(g.fields)) ? g.fields : []) {
      if (!/\/ExtRecordId$/.test(String((f && f.type) || ''))) continue;
      for (const v of (f.values || [])) {
        const t = String((v && v.text) || '').trim();
        if (/^\d{1,15}$/.test(t) && Number(t) > 0 && ids.indexOf(String(Number(t))) < 0) ids.push(String(Number(t)));
      }
    }
    return ids.length === 1 ? ids[0] : null;
  }

  // Some index entries have no image on FamilySearch: the image is at a partner
  // archive, and the record page shows a "View on {partner}" badge instead. The
  // badge's address is in the record's own data. FamilySearch's page code (read
  // 2026-11-24) takes the record's image description (a DigitalArtifact source
  // description: the one the opened person's first source names, else the
  // first) and its `about` (else its persistent identifier); when that address
  // is not on familysearch.org the image is a partner's, and "Go to {partner}"
  // opens exactly that address. Same rule here: the address, or null.
  const FS_ARTIFACT = 'http://gedcomx.org/DigitalArtifact';
  const FS_PERSISTENT = 'http://gedcomx.org/Persistent';
  function fsPartnerImageUrl(g, fsId) {
    const sds = (g && Array.isArray(g.sourceDescriptions)) ? g.sourceDescriptions : [];
    const artifacts = sds.filter((s) => s && s.resourceType === FS_ARTIFACT);
    if (!artifacts.length) return null;
    const person = fsOpenedPerson(g, fsId);
    const src = person && Array.isArray(person.sources) ? person.sources[0] : null;
    const ref = String((src && src.descriptionId) || '').replace(/^#/, '');
    const sd = (ref && artifacts.find((s) => s.id === ref)) || artifacts[0];
    const ids = (sd.identifiers && sd.identifiers[FS_PERSISTENT]) || [];
    const href = String(sd.about || (Array.isArray(ids) ? ids[0] : ids) || '').trim();
    let u;
    try {
      u = new URL(href);
    } catch (e) {
      return null;
    }
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
    if (FS_HOST_RE.test(u.hostname)) return null;
    return u.href;
  }

  // The partner archives whose image pages Capture follows from a record's
  // badge, one reader each. A reader takes the badge's address and returns the
  // page to fetch ({ site, archive, pageUrl, cite, … }) or null. Only archives
  // seen on a live record are listed; another state archive is a second entry.
  const FS_PARTNER_PAGES = [wvPartnerPage];
  function fsPartnerImage(url) {
    if (!url) return null;
    for (const read of FS_PARTNER_PAGES) {
      const page = read(url);
      if (page) return page;
    }
    return null;
  }

  // The record from FamilySearch's data for it (`g`, GEDCOM X) and its identity
  // (`ids`, from fsParseRecordPage). Everything comes from the data, not the
  // page: a single-page site's title tags can still describe an earlier record.
  function fsRecordFrom(g, ids) {
    const sds = (g && Array.isArray(g.sourceDescriptions)) ? g.sourceDescriptions : [];
    const about = (s) => String((s && s.about) || '');
    const persona = sds.find((s) => /ark:\/61903\/1:1:/i.test(about(s)) &&
      about(s).toUpperCase().indexOf('1:1:' + ids.fsId) >= 0) || {};
    const image = sds.find((s) => /DigitalArtifact/.test(String(s.resourceType || '')) && /3:1:/i.test(about(s)));
    const coll = sds.find((s) => /Collection/.test(String(s.resourceType || '')) && /\/collections\/\d+$/.test(about(s)));
    const rec = sds.find((s) => /\/Record$/.test(String(s.resourceType || '')));
    const imageId = (image && ((/3:1:([A-Z0-9-]+)/i.exec(about(image)) || [])[1] || '').toUpperCase()) ||
      ids.viewerImageId || null;
    const cite = persona.citations && persona.citations[0] && persona.citations[0].value;
    const partnerImageUrl = imageId ? null : fsPartnerImageUrl(g, ids.fsId);
    const collectionId = coll ? (/collections\/(\d+)$/.exec(about(coll)) || [])[1] || null : null;
    const out = {
      record: {
        fsId: ids.fsId,
        url: 'https://www.familysearch.org/ark:/61903/1:1:' + ids.fsId,
        imageId,
        recordArk: rec ? (/1:2:[A-Z0-9-]+/i.exec(about(rec)) || [])[0] || null : null,
        collectionId,
        collection: fsFirstTitle(coll),
        name: fsPersonName(fsOpenedPerson(g, ids.fsId)),
      },
      citation: cite ? String(cite).replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim() || null : null,
      title: fsFirstTitle(persona),
    };
    // The image at a partner archive (no image on FamilySearch): the badge's
    // address, and the page Capture follows when it can read that archive.
    if (partnerImageUrl) {
      out.record.partnerImageUrl = partnerImageUrl;
      const partner = fsPartnerImage(partnerImageUrl);
      if (partner) out.record.partnerImage = partner;
    }
    // A Find a Grave Index entry: the memorial it indexes (only then).
    const memorialId = fsIndexedMemorial(g, collectionId);
    if (memorialId) out.record.memorialId = memorialId;
    return out;
  }

  // recordHints over the record's own events: the opened person's facts (and
  // the couple's, for a marriage), by GEDCOM X type ("http://gedcomx.org/Death").
  function fsHints(record, g) {
    const main = fsOpenedPerson(g, record && record.fsId);
    const facts = [].concat((main && main.facts) || []);
    for (const r of (g && g.relationships) || []) {
      const ids = [r.person1, r.person2].map((x) => String((x && (x.resource || x.resourceId)) || '').replace(/^#/, ''));
      if (!main || !main.id || ids.indexOf(main.id) >= 0) facts.push(...(r.facts || []));
    }
    return recordHints((record && record.collection) || '', (word) => {
      for (const f of facts) {
        if (fsTypeName(f.type).toLowerCase() !== word.toLowerCase()) continue;
        const d = f.date || {};
        const v = fsFormalToEdtf(d.formal) || ancDateToEdtf(d.original);
        if (v) return v;
      }
      return null;
    });
  }

  // The bundle's title from the form's UNTOUCHED prefill: that title, unless it
  // is the page's own title (`pageTitle`, the tab's - on FamilySearch often an
  // earlier record's) or looks like one, or is empty; then "{name} -
  // {collection}" from FamilySearch's data. A title she typed never comes here:
  // the panel uses it as typed (PR #214 review).
  function fsTitle(record, formTitle, pageTitle) {
    const r = record || {};
    const auto = [r.name, r.collection].filter(Boolean).join(' - ') || 'FamilySearch record ' + r.fsId;
    const typed = String(formTitle || '').trim();
    const page = String(pageTitle || '').trim();
    return !typed || typed === page || /\bFamilySearch\s*$|•/.test(typed) ? auto : typed;
  }

  // Where a film image sits, from the two answers FamilySearch's own film viewer
  // asks for (fsFilmData): `film.imageData` names the image's film by its DGS
  // number, and `film.filmData` holds the film's catalog entry (title, film
  // numbers) and its images in order. The image's GEDCOM X description says
  // none of this (checked on a live film image, 2026-11-22). `page.title` is the
  // page's own title ("Deeds, 1785-1851; ark:/61903/3:1:…"), used for the
  // catalog title only when FamilySearch's answer did not come.
  // { imageId, dgs, imageNumber, imageCount, filmNumber, filmNote, catalogTitle,
  // catalogUrl, arkUrl }. A value not found is null, never guessed.
  function fsImageInfoFrom(imageId, film, page) {
    const id = String(imageId || '').toUpperCase();
    const f = film || {};
    const fd = f.filmData && typeof f.filmData === 'object' ? f.filmData : {};
    const text = (v) => (v == null || typeof v === 'object' ? '' : String(v).replace(/\s+/g, ' ').trim());
    const unpad = (v) => text(v).replace(/^0+(?=\d)/, '');
    const rawDgs = text((f.imageData && f.imageData.dgsNum) || fd.dgsNum);
    // The viewer pads the DGS to nine digits ("008129459"); the catalog does not.
    const dgs = /^\d+$/.test(rawDgs) ? unpad(rawDgs) : null;
    const cats = Array.isArray(fd.catalogs) ? fd.catalogs.filter((c) => c && c.data && typeof c.data === 'object') : [];
    const notesOf = (c) => (Array.isArray(c.data.film_note) ? c.data.film_note : []);
    const isOurs = (n) => !!dgs && !!n && unpad(n.digital_film_no) === dgs;
    // A film can sit under more than one catalog entry: the one that lists this film.
    const cat = cats.find((c) => notesOf(c).some(isOurs)) || cats[0] || null;
    const note = cat ? notesOf(cat).find(isOurs) || null : null;
    const fromPage = text(page && page.title).replace(/\s*;\s*ark:\/61903\/.*$/i, '')
      .replace(/\s*[|–—]\s*(?:Film\s*#.*|FamilySearch)\s*$/i, '').trim();
    const catalogId = cat && /^\/(?:[a-z]{2}\/)?search\/catalog\/(?:info\/)?([A-Za-z0-9:._-]+)$/.exec(text(cat.path));
    const images = Array.isArray(fd.images) ? fd.images : [];
    const at = images.findIndex((u) => {
      const m = /\/3:1:([A-Z0-9-]+)/i.exec(text(u));
      return !!m && m[1].toUpperCase() === id;
    });
    return {
      imageId: id,
      dgs,
      imageNumber: at >= 0 ? at + 1 : null,
      imageCount: at >= 0 ? images.length : null,
      filmNumber: (note && /^\d+$/.test(text(note.filmno)) && text(note.filmno)) || null,
      filmNote: (note && (text(note.text) || text(note.public_note))) || null,
      catalogTitle: (cat && (text(cat.data.title) || text(cat.data.display_title))) || fromPage || null,
      catalogUrl: catalogId ? 'https://www.familysearch.org/search/catalog/' + catalogId[1] : null,
      arkUrl: 'https://www.familysearch.org/ark:/61903/3:1:' + id,
    };
  }

  // The citation for a bare film image; a part that is not known is left out.
  // `accessed` is the date text (YYYY-MM-DD).
  function fsImageCitation(info, accessed) {
    const i = info || {};
    const where = 'FamilySearch (' + [i.arkUrl, accessed ? 'accessed ' + accessed : ''].filter(Boolean).join(' : ') + ')';
    const lead = i.catalogTitle ? '"' + i.catalogTitle + ',"' : '';
    const n = i.imageNumber ? 'image ' + i.imageNumber + (i.imageCount ? ' of ' + i.imageCount : '') : '';
    const from = [i.filmNumber ? 'film ' + i.filmNumber : '', i.dgs ? 'DGS ' + i.dgs : ''].filter(Boolean).join(', ');
    return [[lead, 'images,', where].filter(Boolean).join(' '), n, from ? 'citing ' + from : ''].filter(Boolean).join('; ') + '.';
  }

  // The panel's title for a bare film image: "{catalog title}, image {n}".
  function fsImageTitle(info) {
    const i = info || {};
    const parts = [i.catalogTitle, i.imageNumber ? 'image ' + i.imageNumber : ''].filter(Boolean);
    return parts.length ? parts.join(', ') : 'FamilySearch image ' + (i.imageId || '');
  }

  // FamilySearch's own machine reading of the image, when the page the human has
  // open shows it (the viewer's Full-Text panel, `?view=fullText`). Checked on a
  // live page 2026-11-23: the page makes no data call the panel can repeat from
  // a page script (its transcript service is on another host that refuses the
  // page's origin), so the text is read from the panel itself, by its own
  // test id. FamilySearch draws it one word to a span with no line breaks, so
  // the text comes back as one run of words. Nothing is clicked or fetched: no
  // panel on the page, or a panel for another image, is `{ ok: true, text: null }`,
  // never a failed capture. `href` is the page's address; `imageId` the image the
  // capture is of, so the text of a different image is never filed with this one.
  function fsFullTextFrom(doc, href, imageId) {
    const none = { ok: true, text: null };
    const id = String(imageId || '').toUpperCase();
    const m = /\/ark:\/61903\/3:1:([A-Z0-9-]+)/i.exec(String(href || ''));
    if (!id || !m || m[1].toUpperCase() !== id) return none;
    let el = null;
    try {
      el = doc && doc.querySelector('[data-testid="full-text-transcript"]');
    } catch (e) {
      return none;
    }
    const text = el ? String(el.textContent || '').replace(/\s+/g, ' ').trim() : '';
    return { ok: true, text: text || null };
  }

  // The full-image link in an image description; null when there is none
  // (a restricted image often has none).
  function fsImageLink(j) {
    const links = (j && j.links) || {};
    const l = links['image-stream-image-dist'] || links.image;
    return l && typeof l.href === 'string' && l.href ? l.href : null;
  }

  // An Error in fsStatusMessage's words, saying whether its cause passes
  // (`transient`: no connection, a "slow down", a failing server) and whether
  // FamilySearch said slow down or busy (`slowDown`: 429/503). `status` null
  // means no answer at all.
  function fsFailure(message, status) {
    const e = new Error(message);
    e.transient = status == null || status === 429 || status >= 500;
    e.slowDown = status === 429 || status === 503;
    return e;
  }

  // One GET of FamilySearch's data service. Throws an Error with
  // fsStatusMessage's words (never the token or the address). `what` is what
  // she is told was refused: 'record' (the default) or 'image' (an image's
  // description), so a refused image is never reported as a refused record.
  // `body`, when given, makes it a POST of that JSON (the film viewer's own
  // questions, fsFilmData), under the same login rule.
  async function fsJson(path, accept, d, what, body) {
    const headers = Object.assign({ Accept: accept || 'application/x-gedcomx-v1+json' },
      body ? { 'Content-Type': 'application/json' } : {}, fsAuthHeaders(path, d.base, d.token));
    const init = body ? { method: 'POST', credentials: 'include', headers, body: JSON.stringify(body) }
      : { credentials: 'include', headers };
    let r;
    try {
      r = await d.fetch(new URL(path, d.base).href, init);
    } catch (e) {
      throw fsFailure('could not reach FamilySearch - check your internet connection, then press Capture again', null);
    }
    if (!r.ok) throw fsFailure(fsStatusMessage(r.status, what || 'record', !!d.token), r.status);
    if (r.status === 204) return {};
    let text;
    try {
      text = await r.text();
    } catch (e) {
      // The answer began and then broke off: a cause that passes.
      throw fsFailure('the connection to FamilySearch broke off while its answer was coming down - press Capture again', null);
    }
    try {
      return text ? JSON.parse(text) : {};
    } catch (e) {
      throw new Error('FamilySearch sent something other than ' +
        (what === 'image' ? 'the image\u2019s description' : 'its record data') +
        ' - reload the page and press Capture again');
    }
  }

  // The record on the page at `href`: { ok, record, citation, title, hints,
  // gedcomx } or { ok:false, error }.
  async function fsRecordInfo(href, d) {
    const ids = fsParseRecordPage(href);
    if (!ids) return { ok: false, error: 'this is not a FamilySearch record page' };
    try {
      const g = await fsJson('/ark:/61903/1:1:' + ids.fsId, null, d);
      const info = fsRecordFrom(g, ids);
      return Object.assign({ ok: true }, info, { hints: fsHints(info.record, g), gedcomx: g });
    } catch (e) {
      return { ok: false, error: e.message, slowDown: !!e.slowDown, transient: !!e.transient };
    }
  }

  // An image's description (GEDCOM X), once. The check before Capture reads it
  // and hands it to fsImageCheck and fsImage rather than asking again.
  async function fsImageDescription(imageId, d) {
    return fsJson('/ark:/61903/3:1:' + imageId, null, d, 'image');
  }

  // Which film a bare image is on, and where on it: the same two questions
  // FamilySearch's own film viewer asks when it shows the image (image-data
  // names the film's DGS; film-data, for that DGS, its catalog entry and its
  // images in order). Checked on a live film image, 2026-11-22. Never throws:
  // { imageData, filmData, error, slowDown }, a part not answered is null and
  // `error` says why, so the image and its link are still saved without them.
  async function fsFilmData(imageId, d) {
    const out = { imageData: null, filmData: null, error: null, slowDown: false };
    const ask = (type, args) => fsJson('/search/filmdatainfo/' + type, 'application/json', d, 'image',
      { type, args: Object.assign(args, { state: {}, locale: 'en' }) });
    try {
      out.imageData = await ask('image-data', { imageURL: 'https://familysearch.org/ark:/61903/3:1:' + imageId });
      const dgs = String((out.imageData && out.imageData.dgsNum) || '');
      if (!/^\d+$/.test(dgs)) {
        out.error = 'FamilySearch did not say which film this image is on';
        return out;
      }
      out.filmData = await ask('film-data', { dgsNum: dgs });
    } catch (e) {
      out.error = e.message;
      out.slowDown = !!e.slowDown;
    }
    return out;
  }

  // Before Capture: can this page's image be saved from here? Reads the image's
  // description and asks for its first kilobyte, never the whole picture.
  // `imageId` is the record's or the viewer's. { ok, imageId, available, reason? }.
  async function fsImageCheck(imageId, d, desc) {
    if (!imageId) return { ok: true, imageId: null, available: false, reason: 'there is no image for it on FamilySearch' };
    let link;
    try {
      link = fsImageLink(desc || await fsImageDescription(imageId, d));
    } catch (e) {
      // `slowDown`: FamilySearch said slow down or busy even before Capture;
      // the owner's batch downloads take their break (PR #224 Codex 4504294846).
      return { ok: true, imageId, available: false, reason: e.message, slowDown: !!e.slowDown };
    }
    if (!link) {
      return { ok: true, imageId, available: false,
        reason: 'it is restricted: FamilySearch shows it only at a FamilySearch center or affiliate library' };
    }
    let r;
    try {
      r = await d.fetch(new URL(link, d.base).href.split('?')[0], {
        headers: Object.assign({ Accept: 'image/jpeg', Range: 'bytes=0-1023' }, fsAuthHeaders(link, d.base, d.token)),
      });
    } catch (e) {
      return { ok: true, imageId, available: false, reason: 'could not reach FamilySearch for the image' };
    }
    try {
      if (r.body && r.body.cancel) r.body.cancel();
    } catch (e) {
      /* nothing to stop */
    }
    return r.ok ? { ok: true, imageId, available: true }
      : { ok: true, imageId, available: false, reason: fsStatusMessage(r.status, 'image', !!d.token),
        slowDown: r.status === 429 || r.status === 503 };
  }

  // The full image behind ark:/61903/3:1:{imageId}: { ok, base64, ext,
  // contentType } or { ok:false, error }.
  async function fsImage(imageId, d, desc) {
    if (!imageId) return { ok: false, error: 'no image named' };
    let link;
    try {
      link = fsImageLink(desc || await fsImageDescription(imageId, d));
    } catch (e) {
      return { ok: false, error: e.message, transient: !!e.transient, slowDown: !!e.slowDown };
    }
    if (!link) {
      return { ok: false, error: 'FamilySearch offers no download of this image: it is restricted to a ' +
        'FamilySearch center or affiliate library' };
    }
    let r;
    try {
      r = await d.fetch(new URL(link, d.base).href.split('?')[0], {
        headers: Object.assign({ Accept: 'image/jpeg' }, fsAuthHeaders(link, d.base, d.token)),
      });
    } catch (e) {
      return { ok: false, transient: true, error: 'could not reach FamilySearch for the image - check your internet connection' };
    }
    if (!r.ok) {
      const f = fsFailure(fsStatusMessage(r.status, 'image', !!d.token), r.status);
      return { ok: false, error: f.message, transient: f.transient, slowDown: f.slowDown };
    }
    let blob;
    try {
      blob = await r.blob();
    } catch (e) {
      // The headers came and then the picture broke off (PR #224 Codex 4504508005).
      return { ok: false, transient: true,
        error: 'the image broke off while it was coming down from FamilySearch - press Capture again later' };
    }
    if (!/^image\//.test(blob.type || '') || blob.size < 30 * 1024) {
      return { ok: false, error: 'the image came back too small to be the full record (' +
        Math.round((blob.size || 0) / 1024) + ' KB) - use FamilySearch’s Download button and drop the file in' };
    }
    let base64;
    try {
      base64 = await d.toBase64(blob);
    } catch (e) {
      return { ok: false, transient: true, error: 'the image could not be read once it came - press Capture again later' };
    }
    return { ok: true, base64, ext: 'jpg', contentType: blob.type || 'image/jpeg' };
  }
  // FHA-SYNC-END familysearch

  // The researcher's FamilySearch login, read from the page at the moment of
  // each request - whoever is signed in now - and handed only to
  // fsAuthHeaders, which sends it to FamilySearch's own https addresses and
  // nowhere else. Never stored, logged, or put into anything returned.
  function fsToken() {
    const m = /(?:^|;\s*)fssessionid=([^;]+)/.exec(document.cookie || '');
    if (!m) return null;
    try {
      return decodeURIComponent(m[1]);
    } catch (e) {
      return m[1];
    }
  }

  function fsDeps() {
    return {
      fetch: (url, init) => fetch(url, init),
      base: location.href,
      token: fsToken(),
      toBase64: blobToBase64,
    };
  }

  // The image on a FamilySearch image viewer: its address, its canonical link,
  // or - on a film viewer (/search/film/…?i=N), which does not name the image
  // in its address - the ONE 3:1 image the page links to. Two different
  // images on the page is not an identification, so that answers null. The
  // Seen It add-on's badges are not the page's links and are skipped.
  // Only a FamilySearch viewer is read this way - an image address, or a film
  // viewer (/search/film/…) - never any other page that merely LINKS one
  // FamilySearch image (a blog, a forum, a tree site): that page is not a
  // FamilySearch viewer, and treating it as one would drop its own file and
  // send a request to the wrong host (PR #214 review).
  const FS_VIEWER_PATH_RE = /\/ark:\/61903\/3:1:|^\/(?:[a-z]{2}\/)?search\/film\//i;

  function fsViewerImageId(href) {
    const own = fsImageIdFromHref(href || location.href);
    if (own) return own;
    if (href && href.split('#')[0] !== location.href.split('#')[0]) return null;
    if (!FS_HOST_RE.test(location.hostname) || !FS_VIEWER_PATH_RE.test(location.pathname)) return null;
    const canon = document.querySelector('link[rel="canonical"], meta[property="og:url"]');
    let fromCanon = null;
    try {
      // An unparseable canonical or og:url is the page's mistake, never a
      // reason the whole prefill fails.
      fromCanon = canon && fsImageIdFromHref(new URL(canon.getAttribute('href') || canon.getAttribute('content') || '', location.href).href);
    } catch (e) {
      fromCanon = null;
    }
    if (fromCanon) return fromCanon;
    const found = new Set();
    for (const el of document.querySelectorAll('a[href*="3:1:"], input[value*="3:1:"]')) {
      if (el.closest(seenItSelector())) continue;
      let id = null;
      try {
        id = fsImageIdFromHref(new URL(el.getAttribute('href') || el.value || '', location.href).href);
      } catch (e) {
        id = null;
      }
      if (id) found.add(id);
      if (found.size > 1) return null;
    }
    return found.size === 1 ? [...found][0] : null;
  }

  // The FamilySearch image a file address is of, for the panel's second try
  // after that address refused: an ark (ark:/61903/3:1:{id}), or one of
  // FamilySearch's own image addresses - a viewer tile (…/dz/v1/3:1:{id}/…)
  // or the full image (…/das/v2/{id}/dist.jpg). https on familysearch.org
  // only; anything else names no FamilySearch image (null).
  function fsImageIdFromAddress(href) {
    const ark = fsImageIdFromHref(href);
    if (ark) return ark;
    let u;
    try {
      u = new URL(href);
    } catch (e) {
      return null;
    }
    if (u.protocol !== 'https:' || u.username || u.password || !FS_HOST_RE.test(u.hostname)) return null;
    let p = u.pathname;
    try {
      p = decodeURIComponent(p);
    } catch (e) {
      /* read it as it is */
    }
    const m = /(?:^|\/)3:1:([A-Z0-9-]+)(?:\/|$)/i.exec(p) || /\/das\/v2\/([A-Z0-9-]+)\/[^/]*$/i.exec(p);
    return m ? m[1].toUpperCase() : null;
  }

  // The record, plus the bundle's title: the human's `formTitle` when she
  // typed one, else FamilySearch's own "{name} - {collection}" (fsTitle).
  async function familysearchRecordInfo(formTitle) {
    const info = await fsRecordInfo(location.href, fsDeps());
    if (info.ok) info.bundleTitle = fsTitle(info.record, formTitle, '');
    return info;
  }

  // Before Capture: can this page's image be saved from here? On a record,
  // also FamilySearch's own name for it (the panel's title).
  async function familysearchImageCheck() {
    if (fsParseRecordPage(location.href)) {
      const info = await fsRecordInfo(location.href, fsDeps());
      if (!info.ok) return { ok: false, error: info.error, slowDown: !!info.slowDown };
      const r = info.record;
      const check = await fsImageCheck(r.imageId, fsDeps());
      // An image at a partner archive (the "View on wvculture.org" badge):
      // the status line says Capture brings it, and from where.
      return Object.assign({}, check, {
        record: true, fsId: r.fsId, memorialId: r.memorialId || null,
        partner: r.partnerImage ? r.partnerImage.archive : null,
        title: [r.name, r.collection].filter(Boolean).join(' - ') || null,
      });
    }
    const imageId = fsViewerImageId();
    if (!imageId) return { ok: true, record: false, imageId: null, available: false, reason: 'no single image was found on this page' };
    // A bare film image: the description answers "can it be saved?" and is
    // kept for the image fetch. The panel's first title is the page's own
    // catalog title; the film and image number are asked for at Capture only.
    if (fsParseImagePage(location.href)) {
      let desc;
      try {
        desc = await fsImageDescription(imageId, fsDeps());
      } catch (e) {
        return { ok: true, record: false, imageId, available: false, reason: e.message, slowDown: !!e.slowDown };
      }
      fsImageDescCache = { id: imageId, desc };
      const info = fsImageInfoFrom(imageId, null, { title: document.title });
      return Object.assign({ record: false, filmImage: true, title: info.catalogTitle ? fsImageTitle(info) : null },
        await fsImageCheck(imageId, fsDeps(), desc));
    }
    return Object.assign({ record: false }, await fsImageCheck(imageId, fsDeps()));
  }

  // The image description Capture's first question read, kept for the image
  // fetch that follows it so FamilySearch is asked for it once (no token in it).
  let fsImageDescCache = null;

  // The bare film image on this page: its film, DGS, image number, catalog
  // title and citation (what Capture files with the picture). FamilySearch
  // not answering is never a failed capture: the image and its 3:1 link are
  // still saved, and `warning` says what is missing and why.
  async function familysearchImageInfo(accessed) {
    const page = fsParseImagePage(location.href);
    if (!page) return { ok: false, error: 'this is not a FamilySearch film image page' };
    const film = await fsFilmData(page.imageId, fsDeps());
    const info = fsImageInfoFrom(page.imageId, film, { title: document.title });
    return {
      ok: true, info, title: fsImageTitle(info), citation: fsImageCitation(info, accessed),
      warning: film.error ? 'the film and image number were not saved: ' + film.error : null,
      slowDown: film.slowDown,
    };
  }

  // ── Find a Grave memorials (the memorial and its photos) ─────────────────────
  // On a memorial (/memorial/{id}/{slug}) Capture saves what the memorial
  // says - names, dates, places, cemetery, plot, inscription, family, bio and
  // Find a Grave's own citation - and its photos at full size. This side reads
  // the memorial from the page and its photo list from the gallery page, on
  // Find a Grave, WITHOUT cookies (a memorial is public; no login is needed
  // or sent). The photos themselves live on images.findagrave.com, which the
  // page cannot read, so the panel downloads them (lib/findagrave.js). The
  // readers are a COPY of lib/findagrave-record.js (the canonical, tested
  // module); tests/test-sync.js fails if they drift.
  // FHA-SYNC-BEGIN findagrave
  const FAG_HOST_RE = /(^|\.)findagrave\.com$/i;
  // Find a Grave serves its photos, full size, from this one host.
  const FAG_PHOTO_HOST = 'images.findagrave.com';

  // A memorial: /memorial/{id}/{slug} (also its /photo and /flower pages).
  function fagParseMemorial(href, base) {
    let u;
    try {
      u = new URL(href, base);
    } catch (e) {
      return null;
    }
    if (!FAG_HOST_RE.test(u.hostname)) return null;
    const m = /^\/(?:[a-z]{2}\/)?memorial\/(\d+)(?:\/([^/?#]+))?/i.exec(u.pathname);
    if (!m) return null;
    const slug = m[2] && !/^(photo|photos|flower|flowers|edit)$/i.test(m[2]) ? m[2] : null;
    return { memorialId: m[1], slug };
  }

  // A photo is taken only from Find a Grave's own image host, over https:
  // a gallery entry naming anywhere else is skipped, never fetched.
  function fagPhotoUrlOk(url) {
    let u;
    try {
      u = new URL(url);
    } catch (e) {
      return false;
    }
    return u.protocol === 'https:' && u.hostname.toLowerCase() === FAG_PHOTO_HOST && !u.port && !u.username && !u.password &&
      /^\/photos\//.test(u.pathname);
  }

  // What an HTTP answer from Find a Grave means, in words that end in what to do.
  // `what`: 'memorial', 'gallery' or 'photo'.
  function fagStatusMessage(status, what) {
    if (status === 404 || status === 410) {
      return what === 'photo'
        ? 'Find a Grave no longer has this photo (HTTP ' + status + ')'
        : 'Find a Grave has no memorial at this address (HTTP ' + status + '): it may have been removed or ' +
          'merged into another. Search Find a Grave for the person and open the memorial it shows now';
    }
    if (status === 429) return 'Find a Grave asked us to slow down (HTTP 429). Wait a few minutes, then press Capture again';
    if (status === 401 || status === 403) {
      return 'Find a Grave refused the ' + (what === 'photo' ? 'photo' : what === 'gallery' ? 'photo list' : 'memorial') +
        ' (HTTP ' + status + '). Reload the page, and try again later if it keeps happening';
    }
    if (status >= 500) return 'Find a Grave is having trouble (HTTP ' + status + '). Try again in a little while';
    return 'Find a Grave answered HTTP ' + status + ' for the ' + (what === 'photo' ? 'photo' : what) +
      '. Reload the page and press Capture again';
  }

  function fagClassHas(el, name) {
    return String((el && el.className) || '').split(/\s+/).indexOf(name) >= 0;
  }

  function fagAttr(el, name) {
    return el && el.getAttribute ? el.getAttribute(name) : null;
  }

  // The memorial on a page (`root`: the document, or a stand-in tree) at
  // `href`. `opts`: { title (the document's), hidden / block (ancText's: the
  // live page passes its CSS), today (YYYY-MM-DD, for the citation's "accessed") }.
  // { ok, record, citation } or { ok:false, error }.
  function fagMemorialFrom(root, href, opts) {
    const o = opts || {};
    const id = fagParseMemorial(href);
    if (!id) return { ok: false, error: 'this is not a Find a Grave memorial page' };
    const text = (el, skip) => (el ? ancText(el, { hidden: o.hidden, block: o.block, skip }) || null : null);
    const byId = (x) => text(ancById(root, x));
    const name = byId('bio-name');
    if (!name) {
      return { ok: false, error: 'this page shows no memorial - it may have been removed or merged into another. ' +
        'Search Find a Grave for the person and open the memorial it shows now' };
    }
    const canon = ancElements(root, (e) => ancTag(e) === 'LINK' && /canonical/i.test(fagAttr(e, 'rel') || ''))[0];
    const cid = canon ? fagParseMemorial(fagAttr(canon, 'href') || '', href) : null;
    const slug = (cid && cid.memorialId === id.memorialId && cid.slug) || id.slug;

    // The value under a section heading ("Inscription", "Gravesite Details"):
    // the heading's own box, less the heading itself - or, when the heading
    // sits alone in a box of its own, the box that follows it.
    const section = (label) => {
      for (const h of ancElements(root, (e) => /^H[234]$/.test(ancTag(e)))) {
        if ((text(h) || '').toLowerCase() !== label.toLowerCase()) continue;
        let t = text(h.parentNode, h);
        const box = h.parentNode;
        const around = box && box.parentNode ? Array.from(box.parentNode.childNodes || []) : [];
        if (!t) t = text(around.slice(around.indexOf(box) + 1).find((n) => n.nodeType === 1));
        if (t) return t.slice(0, 4000);
      }
      return null;
    };
    // A date as Find a Grave shows it, less the age it adds ("4 Jan 1918 (aged 75)").
    const dateOf = (x) => {
      const v = byId(x);
      return v ? v.replace(/\s*\(aged[^)]*\)\s*$/i, '').trim() || null : null;
    };
    const ageAt = (x) => {
      const m = /\(aged\s+([^)]*)\)\s*$/i.exec(byId(x) || '');
      return m ? m[1].trim() : null;
    };

    const cemLabel = ancById(root, 'cemeteryNameLabel');
    let cemA = cemLabel;
    while (cemA && cemA.nodeType === 1 && ancTag(cemA) !== 'A') cemA = cemA.parentNode;
    const cemM = cemA && cemA.nodeType === 1 ? /\/cemetery\/(\d+)/.exec(fagAttr(cemA, 'href') || '') : null;

    const family = [];
    const grid = ancById(root, 'family-grid');
    for (const ul of grid ? ancElements(grid, (e) => ancTag(e) === 'UL' && fagClassHas(e, 'member-family')) : []) {
      const lid = fagAttr(ul, 'aria-labelledby');
      const relation = lid ? byId(lid) : null;
      for (const a of ancElements(ul, (e) => ancTag(e) === 'A' && /\/memorial\//.test(fagAttr(e, 'href') || ''))) {
        const mm = fagParseMemorial(fagAttr(a, 'href'), href);
        if (!mm || mm.memorialId === id.memorialId || family.some((f) => f.memorialId === mm.memorialId)) continue;
        const nameEl = ancElements(a, (e) => fagAttr(e, 'itemprop') === 'name')[0];
        const lifeEl = ancElements(a, (e) => fagClassHas(e, 'life'))[0];
        const lines = (text(a) || '').split('\n').map((x) => x.trim()).filter(Boolean);
        family.push({
          relation, memorialId: mm.memorialId, slug: mm.slug,
          name: text(nameEl) || lines[0] || null,
          dates: text(lifeEl) || lines.slice(1).join(' ') || null,
        });
      }
    }

    let citation = byId('citationInfo');
    if (citation && o.today) citation = citation.replace(/accessed\s*\)/, 'accessed ' + o.today + ')');
    const inputValue = (x) => {
      const el = ancById(root, x);
      return el ? String(fagAttr(el, 'value') || text(el) || '').trim() || null : null;
    };
    return {
      ok: true,
      record: {
        memorialId: id.memorialId,
        slug,
        url: 'https://www.findagrave.com/memorial/' + id.memorialId + (slug ? '/' + slug : ''),
        name,
        birth: { date: dateOf('birthDateLabel'), place: byId('birthLocationLabel') },
        death: { date: dateOf('deathDateLabel'), age: ageAt('deathDateLabel'), place: byId('deathLocationLabel') },
        burial: {
          cemetery: byId('cemeteryNameLabel'),
          cemeteryId: cemM ? cemM[1] : null,
          city: byId('cemeteryCityName'),
          county: byId('cemeteryCountyName'),
          state: byId('cemeteryStateName'),
          country: byId('cemeteryCountryName'),
          plot: byId('plotValueLabel'),
        },
        inscription: section('Inscription'),
        gravesiteDetails: section('Gravesite Details'),
        bio: byId('fullBio') || byId('partBio'),
        family,
        createdBy: (inputValue('createdBy') || '').replace(/^Created by:\s*/i, '') || null,
        added: (inputValue('addedDate') || '').replace(/^Added:\s*/i, '') || null,
        collection: 'Find a Grave',
      },
      citation,
    };
  }

  // The photos on one gallery page: { photos: [{ photoId, url, caption,
  // addedBy, type }], total }. Only full-size photos on Find a Grave's own image
  // host are listed (fagPhotoUrlOk); the address's query is dropped.
  function fagPhotosFrom(root, base) {
    const photos = [];
    for (const item of ancElements(root, (e) => fagClassHas(e, 'viewer-item') && fagAttr(e, 'data-photo-id'))) {
      const photoId = fagAttr(item, 'data-photo-id');
      if (photos.some((p) => p.photoId === photoId)) continue;
      const img = ancElements(item, (e) => ancTag(e) === 'IMG')[0];
      let src = img ? fagAttr(img, 'data-src') || fagAttr(img, 'src') : null;
      try {
        src = src ? new URL(src, base).href.replace(/\?.*$/, '') : null;
      } catch (e) {
        src = null;
      }
      if (!src || !fagPhotoUrlOk(src)) continue;
      const typeP = ancElements(item, (e) => ancTag(e) === 'P' && /Photo type:/i.test(ancText(e)))[0];
      const caption = ancElements(item, (e) => fagClassHas(e, 'photo-text'))[0];
      const added = ancElements(item, (e) => fagClassHas(e, 'added-by'))[0];
      photos.push({
        photoId,
        url: src,
        caption: caption ? ancText(caption) || null : null,
        addedBy: added ? ancText(added).replace(/^Added by:\s*/i, '') || null : null,
        type: typeP ? ancText(typeP).replace(/^[\s\S]*Photo type:\s*/i, '').trim() || null : null,
      });
    }
    const cnt = ancElements(root, (e) => fagClassHas(e, 'photosCount'))[0];
    const total = cnt ? parseInt(ancText(cnt), 10) : NaN;
    return { photos, total: isNaN(total) ? null : total };
  }

  // A memorial's slug as one safe path segment: Find a Grave's own slugs carry
  // apostrophes ("mary-o'hartley") and percent-escaped accented letters
  // ("j%C3%B3zefa-hartley"), so the slug is decoded and encoded again -
  // never refused for them. '' when there is none; null when it cannot be one
  // (a broken escape, "." or ".."), and the address is then built from the
  // memorial id alone.
  function fagSlugSegment(slug) {
    if (slug == null || slug === '') return '';
    let s;
    try {
      s = decodeURIComponent(String(slug));
    } catch (e) {
      return null;
    }
    if (!s || s === '.' || s === '..' || s.length > 300) return null;
    return encodeURIComponent(s);
  }

  const FAG_GALLERY_PAGES = 20;

  // Every photo on the memorial, from its gallery pages (at most 20), without
  // cookies: { ok, photos, total, partial, partialStatus, partialError } or
  // { ok:false, error, status }.
  //   total    how many photos Find a Grave says the memorial has: its count,
  //            or - with no count - the photos listed when the whole list was
  //            read; null when that is not known (a later page failed, or the
  //            first page listed nothing and gave no count: an unrecognised
  //            page is not "no photos");
  //   partial  true when the list stopped before its end (a later page failed,
  //            repeated itself short of the count, or ran past 20 pages): the
  //            photos listed are only some of them.
  async function fagGallery(memorialId, slug, d) {
    const seg = fagSlugSegment(slug);
    const path = '/memorial/' + memorialId + (seg ? '/' + seg : '') + '/photo';
    const photos = [];
    let count = null;
    let partial = false;
    let partialStatus = null;
    let partialError = null;
    let readAll = false;
    for (let page = 1; page <= FAG_GALLERY_PAGES; page++) {
      let r;
      let html;
      try {
        r = await d.fetch(new URL(path + (page > 1 ? '?page=' + page : ''), d.base).href, { credentials: 'omit' });
        html = r.ok ? await r.text() : null;
      } catch (e) {
        const error = 'could not reach Find a Grave for the photos - check your internet connection';
        if (page === 1) return { ok: false, error, status: null };
        partial = true;
        partialError = error;
        break;
      }
      if (!r.ok) {
        if (page === 1) return { ok: false, error: fagStatusMessage(r.status, 'gallery'), status: r.status };
        partial = true;
        partialStatus = r.status;
        partialError = fagStatusMessage(r.status, 'gallery');
        break;
      }
      const one = fagPhotosFrom(d.parse(html), d.base);
      if (count == null) count = one.total;
      let added = 0;
      for (const p of one.photos) {
        if (photos.some((x) => x.photoId === p.photoId)) continue;
        photos.push(p);
        added++;
      }
      if (count != null && photos.length >= count) {
        readAll = true;
        break;
      }
      if (!added) {
        // A page with nothing new is the end of the list - unless the count
        // says there are more (Find a Grave ignoring ?page=, say).
        if (count != null) partial = true;
        else readAll = photos.length > 0;
        break;
      }
      if (page === FAG_GALLERY_PAGES) partial = true;
    }
    const total = count != null ? count : readAll && !partial ? photos.length : null;
    return { ok: true, photos, total, partial, partialStatus, partialError };
  }

  // What the memorial says about itself (TI-066): always `derivative` - a
  // memorial is a volunteer's compilation from the stone, records and family
  // knowledge, not the original record - and so undated: it was made long after
  // the death, and the death date is a fact for a claim, not the memorial's.
  function fagHints(record) {
    const r = record || {};
    const h = recordHints(r.collection || 'Find a Grave', (word) =>
      (word === 'Death' ? ancDateToEdtf(r.death && r.death.date) : null));
    return { sourceType: h.sourceType, sourceDate: null, sourceClass: 'derivative' };
  }

  // "{name} ({born}-{died}) - Find a Grave memorial", the years from its own dates.
  function fagTitle(record) {
    const r = record || {};
    const year = (d) => (/(\d{4})/.exec(String(d || '')) || [])[1] || '?';
    const life = [year(r.birth && r.birth.date), year(r.death && r.death.date)];
    return (r.name || 'Memorial ' + r.memorialId) +
      (life.some((y) => y !== '?') ? ' (' + life.join('-') + ')' : '') + ' - Find a Grave memorial';
  }

  // A photo's role, from Find a Grave's photo type: "grave-1", "person-1",
  // "family-1", "document-1", … and "photo-1" for a type it does not name (or
  // none) - never a word the archive already uses for another kind of file
  // ("record", "page", "back"). `counts` numbers each kind.
  const FAG_PHOTO_KINDS = ['grave', 'person', 'family', 'document', 'flowers', 'cemetery', 'other'];

  function fagPhotoRole(type, counts) {
    const word = String(type || '').trim().toLowerCase();
    const kind = FAG_PHOTO_KINDS.indexOf(word) >= 0 ? word : 'photo';
    counts[kind] = (counts[kind] || 0) + 1;
    return kind + '-' + counts[kind];
  }
  // FHA-SYNC-END findagrave

  // An Internet Archive item page that is a text (a book), by the page's own
  // <meta property="mediatype">: a recording or a film is captured as a plain
  // page, never filed as a book. A page with no such tag is read as a book.
  function iaBookPage(url) {
    if (!iaParsePage(url)) return false;
    const m = document.querySelector('meta[property="mediatype"]');
    const type = m ? String(m.getAttribute('content') || '').trim().toLowerCase() : '';
    return !type || type === 'texts';
  }

  // Internet Archive book pages (lib/archiveorg-record.js is canonical): which page an
  // address is, for the panel; the requests run in the panel, not here.
  // FHA-SYNC-BEGIN archiveorg
  const IA_HOST_RE = /(^|\.)archive\.org$/i;
  const IA_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$/;

  // A book page: /details/{id}, with /page/{p} when a page is open. `p` is
  // BookReader's "n{K}" (the scan at position K from 0) or a printed page
  // ("11", "xii"). Returns { itemId, leaf (K, or null), printedPage (the
  // segment, or null), segment (the page segment as the address wrote it, or
  // null) }; null for any other address.
  function iaParsePage(href, base) {
    let u;
    try {
      u = new URL(href, base);
    } catch (e) {
      return null;
    }
    if (u.protocol !== 'https:' || !/^(www\.)?archive\.org$/i.test(u.hostname)) return null;
    const m = /^\/details\/([^/?#]+)(?:\/page\/([^/?#]+))?/i.exec(u.pathname);
    if (!m) return null;
    let itemId;
    try {
      itemId = decodeURIComponent(m[1]);
    } catch (e) {
      return null;
    }
    if (!IA_ID_RE.test(itemId)) return null;
    let segment = null;
    if (m[2]) {
      try {
        segment = decodeURIComponent(m[2]);
      } catch (e) {
        segment = null;
      }
    }
    const n = segment ? /^n(\d+)$/i.exec(segment) : null;
    return {
      itemId,
      leaf: n ? parseInt(n[1], 10) : null,
      printedPage: segment && !n ? segment : null,
      segment: segment || null,
    };
  }

  // The author as archive.org's catalogue gives it ("[Mason, Jesse D] [from old
  // catalog]", "Smith, John, 1840-1910") written as a citation names a person:
  // "Jesse D. Mason". More than one author: the first two, "and"; more, "et al.".
  function iaCleanCreator(raw) {
    const list = (Array.isArray(raw) ? raw : [raw]).map((x) => String(x == null ? '' : x)).filter((x) => x.trim());
    const names = [];
    for (const item of list) {
      let s = item.replace(/\[from old catalog\]/gi, ' ').replace(/[[\]]/g, ' ').replace(/\s+/g, ' ').trim();
      s = s.replace(/,?\s*\d{4}\??\s*-\s*(\d{4})?\??\.?$/, '').replace(/[.,;\s]+$/, '').trim();
      if (!s) continue;
      const parts = s.split(/\s*,\s*/);
      if (parts.length === 2 && parts[0] && parts[1] && !/\b(inc|co|company|society|committee|association)\b/i.test(s)) {
        const given = parts[1].replace(/\b([A-Z])\b(?!\.)/g, '$1.');
        s = given + ' ' + parts[0];
      }
      if (!names.includes(s)) names.push(s);
    }
    if (!names.length) return null;
    if (names.length === 1) return names[0];
    return names.length === 2 ? names[0] + ' and ' + names[1] : names[0] + ' et al.';
  }

  function iaFirst(v) {
    const x = Array.isArray(v) ? v[0] : v;
    const s = x == null ? '' : String(x).replace(/\s+/g, ' ').trim();
    return s || null;
  }

  // What /metadata/{id} says about a book: { ok, item: { itemId, title,
  // creator, publisher, date, year, volume, imageCount, mediatype }, files:
  // [{ name, size }] } or { ok:false, error }. An item that does not exist
  // answers with an empty object.
  function iaMetadataFrom(json, itemId) {
    const md = json && json.metadata;
    if (!md || typeof md !== 'object' || !Object.keys(md).length) {
      return { ok: false, error: 'the Internet Archive has no item called ' + itemId + ' (it may have been removed or renamed)' };
    }
    const date = iaFirst(md.date);
    const year = iaYear(date);
    const count = parseInt(iaFirst(md.imagecount), 10);
    return {
      ok: true,
      item: {
        itemId,
        title: iaFirst(md.title),
        creator: iaCleanCreator(md.creator),
        publisher: iaFirst(md.publisher),
        date,
        year,
        volume: iaFirst(md.volume),
        imageCount: isNaN(count) ? null : count,
        mediatype: iaFirst(md.mediatype),
        // A lending-library book: only lent, so none of its pages is kept.
        lent: String(iaFirst(md['access-restricted-item']) || '').toLowerCase() === 'true',
      },
      // Where the item's files sit, for the reader's page list.
      server: typeof json.server === 'string' ? json.server : null,
      dir: typeof json.dir === 'string' ? json.dir : null,
      files: (Array.isArray(json.files) ? json.files : []).map((f) => ({ name: String(f.name || ''), size: Number(f.size) || 0 })),
    };
  }

  // The one year a catalogue date names: "1881", "1881-06-15",
  // "1881-06-15T00:00:00Z", "c1881", "[1881?]". A range ("1890-1895",
  // "1890/91") dates no single year, so it gives none.
  function iaYear(date) {
    const s = String(date || '').trim();
    const iso = /^(\d{4})(?:-\d{2}(?:-\d{2})?)?(?:[T ].*)?$/.exec(s);
    if (iso) return iso[1];
    if (/(?<!\d)\d{4}\s*[-/–]\s*\d{2,4}(?!\d)/.test(s)) return null;
    return (/(?<!\d)(1[0-9]{3}|20[0-9]{2})(?!\d)/.exec(s) || [])[1] || null;
  }

  // The byte range [start, end) of scan `leafNum` in {id}_hocr.html, from the
  // parsed {id}_hocr_pageindex.json (entry 0 is the file's header, entry N the
  // OCR of leaf N); null when the index has no usable entry.
  function iaPageIndexRange(index, leafNum) {
    const e = Array.isArray(index) ? index[leafNum] : null;
    if (!Array.isArray(e) || e.length < 4) return null;
    const start = Number(e[2]);
    const end = Number(e[3]);
    return start >= 0 && end > start ? [start, end] : null;
  }

  function iaEntities(s) {
    return s.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos);/gi, (all, k) => {
      const key = k.toLowerCase();
      if (key === 'amp') return '&';
      if (key === 'lt') return '<';
      if (key === 'gt') return '>';
      if (key === 'quot') return '"';
      if (key === 'apos') return '\'';
      const code = key[1] === 'x' ? parseInt(key.slice(2), 16) : parseInt(key.slice(1), 10);
      try {
        return String.fromCodePoint(code);
      } catch (e) {
        return all;
      }
    });
  }

  // One page of archive.org's hOCR as plain text: a line for each printed
  // line, a blank line between paragraphs. Words stay as the OCR read them.
  function iaHocrText(html) {
    const paras = String(html || '').split(/<p\b[^>]*class="[^"]*\bocr_par\b/);
    const out = [];
    for (const para of paras.slice(1)) {
      const lines = [];
      for (const line of para.split(/<span\b[^>]*class="[^"]*\bocr_line\b/).slice(1)) {
        const words = [];
        const re = /<span\b[^>]*class="[^"]*\bocrx_word\b[^"]*"[^>]*>([\s\S]*?)<\/span>/g;
        let w;
        while ((w = re.exec(line))) {
          const t = iaEntities(w[1].replace(/<[^>]*>/g, '')).replace(/\s+/g, ' ').trim();
          if (t) words.push(t);
        }
        if (words.length) lines.push(words.join(' '));
      }
      if (lines.length) out.push(lines.join('\n'));
    }
    return out.join('\n\n');
  }

  // The scan at `leafNum` (counting from 1) at full size, from archive.org's
  // IIIF image service.
  function iaImageUrl(itemId, leafNum) {
    return 'https://iiif.archive.org/iiif/' + itemId + '$' + leafNum + '/full/full/0/default.jpg';
  }

  // The address of a page, the way the page segment was written: n20 stays
  // n20 and 11 stays 11.
  function iaPageUrl(itemId, segment) {
    return 'https://archive.org/details/' + itemId + (segment ? '/page/' + encodeURIComponent(segment) : '');
  }

  function iaTitle(item) {
    const i = item || {};
    return (i.title || i.itemId || 'Internet Archive book') + (i.volume ? ', vol. ' + i.volume : '');
  }

  // The citation: "{author}, {title} ({publisher}, {date}), p. {printed};
  // digital images, Internet Archive ({url} : accessed {date})." `pages`:
  // [{ leaf (K), printed }] in order; `url`: the link to the first page, in the
  // form the human was looking at. A scan whose printed page is not known is
  // named by its place in the address (n20) and said so.
  function iaCitation(item, pages, url, accessed) {
    const i = item || {};
    const who = i.creator ? i.creator + ', ' : '';
    const pub = [i.publisher, i.date].filter(Boolean).join(', ');
    const head = who + iaTitle(i) + (pub ? ' (' + pub + ')' : '');
    const list = pages || [];
    const known = list.length > 0 && list.every((p) => p.printed);
    let where;
    if (!list.length) where = '';
    else if (known) {
      const nums = list.map((p) => p.printed);
      where = (nums.length === 1 ? 'p. ' : 'pp. ') + (nums.length === 1 ? nums[0] : nums[0] + '-' + nums[nums.length - 1]);
    } else {
      const names = list.map((p) => (p.printed ? 'p. ' + p.printed : 'scan n' + p.leaf));
      where = names.join(', ') + ' (printed page not known)';
    }
    return head + (where ? ', ' + where : '') + '; digital images, Internet Archive (' + url + ' : accessed ' + accessed + ').';
  }

  // What the book says about itself for filing: a printed book, so `book`;
  // a digitised copy of a published work is `derivative`; dated by its
  // publication year when it names one.
  function iaHints(item) {
    return { sourceType: 'book', sourceClass: 'derivative', sourceDate: (item && item.year) || null };
  }
  // FHA-SYNC-END archiveorg

  // FHA-SYNC-BEGIN wvculture
  const WV_VIEW_HOST = 'archive.wvculture.org';
  const WV_IMAGE_HOST = 'images.wvculture.org';
  // A death certificate image is a 7-digit film path, a register page 5-digit;
  // the extension is .gif or .jpg. Anything else is not an image of a record.
  const WV_IMAGE_PATH_RE = /^\/\d+\/\d+\.(gif|jpe?g)$/i;

  // The film and image numbers of a view page (va_view2.aspx), or null for any
  // other address or a view page with no usable image number.
  function wvParsePage(href) {
    let u;
    try {
      u = new URL(href);
    } catch (e) {
      return null;
    }
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
    if (u.hostname.toLowerCase() !== WV_VIEW_HOST) return null;
    if (!/^\/vrr\/va_view2\.aspx$/i.test(u.pathname)) return null;
    let filmNumber = null;
    let imageNumber = null;
    for (const [k, v] of u.searchParams) {
      const key = k.toLowerCase();
      if (key === 'filmnumber') filmNumber = v;
      else if (key === 'imagenumber') imageNumber = v;
    }
    if (!/^\d+$/.test(filmNumber || '') || !/^\d+$/.test(imageNumber || '')) return null;
    return { filmNumber, imageNumber };
  }

  // The page's image: the first img src that is an image on images.wvculture.org,
  // upgraded to https. `srcs` is the list of the page's img src values. Null when
  // none is one (the page then gives nothing to save).
  function wvImageUrl(srcs) {
    for (const src of srcs || []) {
      let u;
      try {
        u = new URL(String(src));
      } catch (e) {
        continue;
      }
      if (u.protocol !== 'http:' && u.protocol !== 'https:') continue;
      if (u.hostname.toLowerCase() !== WV_IMAGE_HOST || u.port || u.username || u.password) continue;
      if (!WV_IMAGE_PATH_RE.test(u.pathname)) continue;
      return 'https://' + WV_IMAGE_HOST + u.pathname;
    }
    return null;
  }

  // 'gif' or 'jpg' from an image address, or null.
  function wvExtension(url) {
    const m = /\.(gif|jpe?g)$/i.exec(String(url || ''));
    if (!m) return null;
    return m[1].toLowerCase() === 'gif' ? 'gif' : 'jpg';
  }

  // The citation line: the archive, the page as the human opened it, the film and
  // the image. `accessed` is YYYY-MM-DD.
  function wvCitation(info, accessed) {
    return 'West Virginia Archives and History, Vital Research Records (' + info.pageUrl +
      ' : accessed ' + accessed + '), film ' + info.filmNumber + ', image ' + info.imageNumber + '.';
  }

  // A FamilySearch West Virginia index entry has no image of its own; its
  // "View on wvculture.org" badge leads to the certificate or register page on
  // the state archive's site, and Capture follows it so the record itself is
  // saved beside the index. `url` is the address the badge opens (FamilySearch's
  // own data names it: lib/familysearch-record.js fsPartnerImageUrl). The page
  // to fetch (always https), its film and image number, and the citation part
  // naming where the original is; null when the address is not a view page.
  function wvPartnerPage(url) {
    const parsed = wvParsePage(url);
    if (!parsed) return null;
    const u = new URL(url);
    // Fetched by the panel, so only the archive's own page: no other port, no
    // name or password in the address.
    if (u.port || u.username || u.password) return null;
    u.protocol = 'https:';
    return {
      site: 'wvculture',
      archive: 'West Virginia Archives and History',
      filmNumber: parsed.filmNumber,
      imageNumber: parsed.imageNumber,
      pageUrl: u.href,
      cite: 'image at West Virginia Archives and History, film ' + parsed.filmNumber + ', image ' + parsed.imageNumber,
    };
  }
  // FHA-SYNC-END wvculture

  // ── West Virginia Archives view pages ──────────────────────────────────────
  // A view page (archive.wvculture.org/vrr/va_view2.aspx) shows one image, an
  // <img> on images.wvculture.org. The numbers come from the address, the image
  // from the page's own img src (lib/wvculture-record.js: never a built URL).
  // The panel downloads the image (lib/wvculture.js): the image host sends no
  // cross-origin permission, so a fetch from here fails on the live site.
  // imageUrl is null when the page shows no image this recognises; the numbers
  // and the citation still stand, so an image he drops in is saved with them.
  function wvcultureImageInfo(today) {
    const parsed = wvParsePage(location.href);
    if (!parsed) return { ok: false, error: 'this is not a West Virginia Archives image page' };
    const srcs = Array.prototype.map.call(document.querySelectorAll('img'),
      (img) => absUrl(img.getAttribute('src') || '') || '');
    const imageUrl = wvImageUrl(srcs);
    const record = Object.assign({}, parsed, {
      pageUrl: location.href, imageUrl, ext: imageUrl ? wvExtension(imageUrl) : null,
    });
    return { ok: true, record, citation: wvCitation(record, today) };
  }

  // FHA-SYNC-BEGIN wikitree
  const WT_HOST = 'www.wikitree.com';
  const WT_ORIGIN = 'https://www.wikitree.com';
  const WT_API = 'https://api.wikitree.com/api.php';
  const WT_APP_ID = 'fha-capture';
  const WT_MAX_PHOTOS = 10;
  const WT_MAX_HOSTED = 10;
  const WT_PROFILE_FIELDS = 'Id,PageId,Name,FirstName,MiddleName,LastNameAtBirth,LastNameCurrent,Nicknames,' +
    'Prefix,Suffix,RealName,BirthDate,DeathDate,BirthLocation,DeathLocation,Gender,IsLiving,Touched,Created,' +
    'Father,Mother,Parents,Spouses,Children,Bio,Title';
  // Pages that are WikiTree's own, never a person: [[Space:…]] is a free-space
  // page (it may hold a transcription), [[Image:…]] / [[File:…]] an uploaded
  // file; the rest are help, categories and the like.
  // A sub-heading under Sources that holds thanks or research notes, not sources.
  const WT_NOT_SOURCES_RE = /^(acknowledge?ments?|thanks|thank you|credits?|research notes?|see also|disclaimers?)\s*:?$/i;
  const WT_NAMESPACES = /^(space|image|file|category|help|project|template|special|docs|wikitree|user|media)$/i;
  // The original of an uploaded file: /photo.php/{h}/{hh}/{name}, never a /thumb/.
  const WT_ORIGINAL_PATH_RE = /^\/photo\.php\/[0-9a-f]\/[0-9a-f]{2}\/[^/]+\.[A-Za-z0-9]{2,5}$/;

  // The page an address is: { kind: 'profile', wikitreeId, key, url } for
  // /wiki/{Surname-123}, { kind: 'space', spaceName, key, url } for
  // /wiki/Space:{name}, or null for anything else (a search, a category, a
  // photo page, another site).
  function wtParsePage(href) {
    let u;
    try {
      u = new URL(href);
    } catch (e) {
      return null;
    }
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
    const host = u.hostname.toLowerCase();
    if ((host !== WT_HOST && host !== 'wikitree.com') || u.port || u.username || u.password) return null;
    const m = /^\/wiki\/([^/]+)\/?$/.exec(u.pathname);
    if (!m) return null;
    let name;
    try {
      name = decodeURIComponent(m[1]).replace(/ /g, '_');
    } catch (e) {
      return null;
    }
    const space = /^Space:(.+)$/i.exec(name);
    if (space) {
      const spaceName = space[1];
      return { kind: 'space', spaceName, key: 'Space:' + spaceName,
        url: WT_ORIGIN + '/wiki/Space:' + encodeURIComponent(spaceName).replace(/%2C/g, ',') };
    }
    const p = /^(?:Profile:)?(\p{L}[^:#?]*-\d+)$/u.exec(name);
    if (!p) return null;
    return { kind: 'profile', wikitreeId: p[1], key: p[1], url: WT_ORIGIN + '/wiki/' + encodeURIComponent(p[1]) };
  }

  // The one API request: getProfile for the page (`what` omitted) or getPhotos
  // for a profile's photos (`what` 'photos'). A GET, so it carries no body.
  function wtApiUrl(key, what) {
    const q = what === 'photos'
      ? { action: 'getPhotos', key, limit: String(WT_MAX_PHOTOS + WT_MAX_HOSTED) }
      : { action: 'getProfile', key, fields: WT_PROFILE_FIELDS, bioFormat: 'wiki' };
    q.appId = WT_APP_ID;
    return WT_API + '?' + Object.keys(q).map((k) => k + '=' + encodeURIComponent(q[k])).join('&');
  }

  // A WikiTree date as the archive writes one: "1681-10-26" stays, "1635-00-00"
  // is "1635", "1681-10-00" is "1681-10", "0000-00-00" (not known) is null.
  function wtDate(v) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(v || '').trim());
    if (!m || m[1] === '0000') return null;
    if (m[2] === '00') return m[1];
    if (m[3] === '00') return m[1] + '-' + m[2];
    return m[0];
  }

  // "20230526144512" (WikiTree's Touched and Created) as "2023-05-26".
  function wtStamp(v) {
    const m = /^(\d{4})(\d{2})(\d{2})/.exec(String(v || '').trim());
    return m && m[1] !== '0000' ? m[1] + '-' + m[2] + '-' + m[3] : null;
  }

  function wtStr(v) {
    return typeof v === 'string' || typeof v === 'number' ? String(v).trim() : '';
  }

  // A person's name the way WikiTree shows it: "Mary (Cole) Hartley".
  function wtFullName(p) {
    if (!p || typeof p !== 'object') return null;
    const first = wtStr(p.FirstName) || wtStr(p.RealName);
    const birth = wtStr(p.LastNameAtBirth);
    const current = wtStr(p.LastNameCurrent) || birth;
    const parts = [wtStr(p.Prefix), first, wtStr(p.MiddleName),
      birth && birth !== current ? '(' + birth + ')' : '', current, wtStr(p.Suffix)];
    const name = parts.filter(Boolean).join(' ');
    return name || wtStr(p.LongName) || null;
  }

  function wtNameParts(p) {
    const out = {};
    for (const [k, key] of [['prefix', 'Prefix'], ['first', 'FirstName'], ['middle', 'MiddleName'],
      ['lastAtBirth', 'LastNameAtBirth'], ['lastCurrent', 'LastNameCurrent'], ['suffix', 'Suffix'],
      ['nicknames', 'Nicknames'], ['realName', 'RealName']]) {
      const v = wtStr(p[key]);
      if (v) out[k] = v;
    }
    return out;
  }

  // What the API says, as the record the capture keeps:
  // { ok: true, record, bio } with `bio` the biography's wiki text (null when
  // WikiTree does not share it), or { ok: false, error } when WikiTree did not
  // answer for this page (a private profile, a page that is not there).
  function wtRecordFromApi(json, parsed) {
    const entry = Array.isArray(json) ? json[0] : null;
    if (!entry || typeof entry !== 'object') return { ok: false, error: 'WikiTree sent an answer the add-on could not read' };
    const p = entry.profile;
    if ((entry.status !== 0 && entry.status !== '0' && entry.status !== undefined) || !p || typeof p !== 'object' ||
        Array.isArray(p) || !Object.keys(p).length) {
      return { ok: false, error: 'WikiTree did not share this page through its public data service' +
        (typeof entry.status === 'string' && entry.status ? ' ("' + entry.status.slice(0, 120) + '")' : '') };
    }
    const bio = typeof p.bio === 'string' ? p.bio : typeof p.Bio === 'string' ? p.Bio : null;
    if (parsed.kind === 'space') {
      const title = p.Title && typeof p.Title === 'object' ? wtStr(p.Title.Text) : wtStr(p.Title);
      return {
        ok: true,
        bio,
        record: {
          kind: 'space', spaceName: parsed.spaceName, pageUrl: parsed.url, pageId: p.PageId || null,
          name: title || parsed.spaceName.replace(/_/g, ' '),
          lastEdited: wtStamp(p.Touched), created: wtStamp(p.Created), from: 'api',
        },
      };
    }
    if (wtStr(p.Name) && wtStr(p.Name).toLowerCase() !== parsed.wikitreeId.toLowerCase()) {
      // WikiTree answers for a merged profile with the one it was merged into.
      parsed = Object.assign({}, parsed, { wikitreeId: wtStr(p.Name), url: WT_ORIGIN + '/wiki/' + encodeURIComponent(wtStr(p.Name)) });
    }
    const kin = (o, extra) => (o && typeof o === 'object' ? Object.values(o) : [])
      .filter((x) => x && typeof x === 'object' && wtStr(x.Name))
      .map((x) => Object.assign({ wikitreeId: wtStr(x.Name), name: wtFullName(x), birthDate: wtDate(x.BirthDate),
        deathDate: wtDate(x.DeathDate) }, extra ? extra(x) : {}));
    const father = wtStr(p.Father);
    const mother = wtStr(p.Mother);
    return {
      ok: true,
      bio,
      record: {
        kind: 'profile',
        wikitreeId: parsed.wikitreeId,
        pageUrl: parsed.url,
        pageId: p.PageId || null,
        name: wtFullName(p) || parsed.wikitreeId,
        nameParts: wtNameParts(p),
        gender: wtStr(p.Gender) || null,
        isLiving: p.IsLiving === 1 || p.IsLiving === '1',
        birth: { date: wtDate(p.BirthDate), place: wtStr(p.BirthLocation) || null },
        death: { date: wtDate(p.DeathDate), place: wtStr(p.DeathLocation) || null },
        lastEdited: wtStamp(p.Touched),
        created: wtStamp(p.Created),
        parents: kin(p.Parents, (x) => ({
          relation: father && wtStr(x.Id) === father ? 'father' : mother && wtStr(x.Id) === mother ? 'mother' : 'parent',
        })),
        spouses: kin(p.Spouses, (x) => ({
          marriageDate: wtDate(x.marriage_date || x.MarriageDate),
          marriagePlace: wtStr(x.marriage_location || x.MarriageLocation) || null,
        })),
        children: kin(p.Children),
        from: 'api',
      },
    };
  }

  // ── reading the page itself (a node tree: the page's document in the
  // browser, tests/mini-dom.js in tests) ─────────────────────────────────────

  function wtAttr(el, name) {
    return el && el.getAttribute ? el.getAttribute(name) : null;
  }

  function wtTag(el) {
    return el && el.nodeType === 1 ? String(el.tagName || '').toUpperCase() : '';
  }

  function wtElements(root, pred, out) {
    out = out || [];
    for (const c of (root && root.childNodes) || []) {
      if (c.nodeType !== 1) continue;
      if (pred(c)) out.push(c);
      wtElements(c, pred, out);
    }
    return out;
  }

  const WT_BLOCK = /^(P|DIV|LI|UL|OL|H[1-6]|TR|TD|DD|DT|BLOCKQUOTE|SECTION|TABLE|PRE)$/;

  // An element's text as a reader sees it: a line per block and per <br>, no
  // scripts, no edit links (WikiTree's "edit" and pencil links are not page text).
  function wtText(node) {
    const parts = [];
    const walk = (n) => {
      if (n.nodeType === 3) {
        parts.push(String(n.nodeValue || '').replace(/\s+/g, ' '));
        return;
      }
      if (n.nodeType !== 1) return;
      const tag = wtTag(n);
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'BUTTON') return;
      const cls = String(wtAttr(n, 'class') || '');
      if (/(^|\s)(EDIT|icon--edit)(\s|$)/.test(cls)) return;
      if (tag === 'A' && /Special:Edit|action=edit/i.test(wtAttr(n, 'href') || '')) return;
      if (tag === 'BR') {
        parts.push('\n');
        return;
      }
      const block = WT_BLOCK.test(tag);
      if (block) parts.push('\n');
      for (const c of n.childNodes || []) walk(c);
      if (block) parts.push('\n');
    };
    if (node) walk(node);
    return parts.join('').split('\n').map((l) => l.replace(/[ \t ]+/g, ' ').trim()).filter(Boolean).join('\n');
  }

  function wtLinksIn(node) {
    return wtElements(node, (e) => wtTag(e) === 'A' && !!wtAttr(e, 'href')).map((a) => wtAttr(a, 'href'));
  }

  // The text of the page's biography box (`.body-text`) from the element after
  // `from` up to the element `to` (a heading), or the whole box.
  function wtBoxText(box, from, to) {
    const kids = Array.from((box && box.childNodes) || []);
    const start = from ? kids.indexOf(from) + 1 : 0;
    const end = to ? kids.indexOf(to) : kids.length;
    const holder = { nodeType: 1, tagName: 'DIV', childNodes: kids.slice(start, end < 0 ? kids.length : end),
      getAttribute: () => null };
    return wtText(holder);
  }

  // The page's biography box: the element holding the "Biography" heading (a
  // profile), or the first .body-text (a free-space page).
  function wtBodyBox(root) {
    const bioHead = wtElements(root, (e) => /^H[23]$/.test(wtTag(e)) && wtAttr(e, 'id') === 'Biography')[0];
    if (bioHead) return { box: bioHead.parentNode, bioHead };
    const box = wtElements(root, (e) => /(^|\s)body-text(\s|$)/.test(wtAttr(e, 'class') || ''))[0];
    return { box: box || null, bioHead: null };
  }

  // What the page itself says, when the API will not: { ok, record, bioText,
  // sources } (sources from wtSourcesFromPage). `root` is the page's document.
  function wtRecordFromPage(root, parsed) {
    const h1 = wtElements(root, (e) => wtTag(e) === 'H1')[0];
    const name = h1 ? wtText(h1).split('\n')[0] : '';
    if (!name) return { ok: false, error: 'the page shows no WikiTree profile or free-space page to save' };
    const { box, bioHead } = wtBodyBox(root);
    const srcHead = box ? Array.from(box.childNodes || []).find((e) => /^H[23]$/.test(wtTag(e)) &&
      /^sources?$/i.test(wtText(e))) : null;
    const bioText = box ? wtBoxText(box, bioHead, srcHead) : '';
    const sources = wtSourcesFromPage(root);
    const edits = wtElements(root, (e) => wtAttr(e, 'id') === 'Edits')[0];
    const editTime = edits ? wtElements(edits, (e) => wtTag(e) === 'TIME')[0] : null;
    const modified = wtElements(root, (e) => wtAttr(e, 'itemprop') === 'dateModified')[0];
    const lastEdited = wtDate(wtAttr(editTime, 'datetime')) || wtDate(wtAttr(modified, 'content'));
    if (parsed.kind === 'space') {
      return { ok: true, bioText, sources,
        record: { kind: 'space', spaceName: parsed.spaceName, pageUrl: parsed.url, pageId: null, name,
          lastEdited, created: null, from: 'page' } };
    }
    const prop = (p) => wtElements(root, (e) => wtAttr(e, 'itemprop') === p)[0];
    const vital = (dateProp, placeProp) => {
      const t = prop(dateProp);
      const place = prop(placeProp);
      return { date: wtDate(wtAttr(t, 'datetime')) || null, place: place ? wtText(place) || null : null };
    };
    const kin = { parents: [], spouses: [], children: [] };
    const seen = new Set();
    for (const el of wtElements(root, (e) => /^(parent|spouse|children|child)$/.test(wtAttr(e, 'itemprop') || ''))) {
      const a = wtElements(el, (e) => wtTag(e) === 'A' && !!wtAttr(e, 'href'))[0];
      let id = null;
      try {
        id = a ? wtParsePage(new URL(wtAttr(a, 'href'), WT_ORIGIN).href) : null;
      } catch (e) {
        id = null;
      }
      if (!id || id.kind !== 'profile') continue;
      const p = wtAttr(el, 'itemprop');
      const key = p + ':' + id.wikitreeId.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      const who = { wikitreeId: id.wikitreeId, name: wtText(a) || null };
      if (p === 'parent') {
        const box2 = wtAttr(el, 'id');
        who.relation = box2 === 'Father' ? 'father' : box2 === 'Mother' ? 'mother' : 'parent';
        kin.parents.push(who);
      } else if (p === 'spouse') {
        kin.spouses.push(who);
      } else {
        kin.children.push(who);
      }
    }
    return {
      ok: true,
      bioText,
      sources,
      record: Object.assign({
        kind: 'profile', wikitreeId: parsed.wikitreeId, pageUrl: parsed.url, pageId: null, name,
        birth: vital('birthDate', 'birthPlace'), death: vital('deathDate', 'deathPlace'),
        lastEdited, created: null, from: 'page',
      }, kin),
    };
  }

  // ── the cited sources ───────────────────────────────────────────────────────

  // A few templates WikiTree editors use for a record on another site, and the
  // address each names. Any other template is not taken as a link.
  function wtTemplateLink(name, args) {
    const n = name.trim().toLowerCase().replace(/[_\s]+/g, ' ');
    const a = args.map((x) => x.trim());
    const ok = (v) => /^[A-Za-z0-9:-]+$/.test(v || '');
    if (n === 'findagrave' && /^\d+$/.test(a[0] || '')) return 'https://www.findagrave.com/memorial/' + a[0];
    if (n === 'familysearch record' && ok(a[0])) return 'https://www.familysearch.org/ark:/61903/1:1:' + a[0];
    if (n === 'familysearch image' && ok(a[0])) return 'https://www.familysearch.org/ark:/61903/3:1:' + a[0];
    if (n === 'familysearch' && ok(a[0])) return 'https://www.familysearch.org/tree/person/details/' + a[0];
    if (n === 'ancestry record' && /^\d+$/.test(a[0] || '') && /^\d+$/.test(a[1] || '')) {
      return 'https://www.ancestry.com/discoveryui-content/view/' + a[1] + ':' + a[0];
    }
    return null;
  }

  // The interwiki prefixes WikiTree turns into an address on another site.
  function wtInterwikiLink(prefix, target) {
    const p = prefix.toLowerCase();
    const t = target.trim();
    if (p === 'findagrave' && /^\d+$/.test(t)) return 'https://www.findagrave.com/memorial/' + t;
    if (p === 'familysearch' && /^[A-Za-z0-9-]+$/.test(t)) return 'https://www.familysearch.org/tree/person/details/' + t;
    if (p === 'wikipedia' && t) return 'https://en.wikipedia.org/wiki/' + encodeURIComponent(t.replace(/ /g, '_'));
    return null;
  }

  function wtTrimUrl(u) {
    return u.replace(/[.,;:!?)\]}'"]+$/, '');
  }

  // What one cited entry links to: { links: [url…], interwiki: ['FamilySearch:…'],
  // hosted: [{ kind: 'file'|'space', … }] }. `hosted` names files and free-space
  // pages on WikiTree itself; their addresses are in `links` too.
  function wtEntryLinks(text) {
    const links = [];
    const interwiki = [];
    const hosted = [];
    const addLink = (u) => {
      if (u && links.indexOf(u) < 0) links.push(u);
    };
    const addHosted = (h) => {
      if (!h) return;
      const key = h.kind + ':' + (h.fileName || h.spaceName).toLowerCase();
      if (!hosted.some((x) => x.kind + ':' + (x.fileName || x.spaceName).toLowerCase() === key)) hosted.push(h);
      addLink(h.pageUrl);
    };
    // [[Prefix:Target|label]] and [[:Image:Name-1.jpg]]
    const wikiRe = /\[\[\s*:?\s*([^\]|]+?)\s*(?:\|[^\]]*)?\]\]/g;
    let m;
    while ((m = wikiRe.exec(text))) {
      const target = m[1];
      const c = target.indexOf(':');
      if (c < 0) continue; // another WikiTree profile, not a source elsewhere
      const prefix = target.slice(0, c).trim();
      const rest = target.slice(c + 1).trim();
      if (/^(image|file)$/i.test(prefix)) {
        addHosted(wtHostedFile(rest));
      } else if (/^space$/i.test(prefix)) {
        addHosted(wtHostedSpace(rest));
      } else if (!WT_NAMESPACES.test(prefix) && rest) {
        const url = wtInterwikiLink(prefix, rest);
        if (url) addLink(url);
        if (interwiki.indexOf(prefix + ':' + rest) < 0) interwiki.push(prefix + ':' + rest);
      }
    }
    // {{FindAGrave|123}}, {{FamilySearch Record|…}}, …
    const tplRe = /\{\{\s*([^{}|]+?)\s*((?:\|[^{}|]*)*)\}\}/g;
    while ((m = tplRe.exec(text))) {
      const url = wtTemplateLink(m[1], m[2].split('|').slice(1));
      if (url) addLink(url);
    }
    // Every web address, bare or in [https://… label].
    const urlRe = /https?:\/\/[^\s\]|<>"{}]+/gi;
    while ((m = urlRe.exec(text))) {
      const url = wtTrimUrl(m[0]);
      const h = wtHostedFromUrl(url);
      if (h) addHosted(h);
      else if (!(wtParsePage(url) && wtParsePage(url).kind === 'profile')) addLink(url); // another profile is not a source elsewhere
    }
    return { links, interwiki, hosted };
  }

  // An uploaded file by its name ("Hartley-6084-2.jpg"): its own page is
  // /photo/{ext}/{name less ext}.
  function wtHostedFile(name) {
    const file = String(name || '').trim().replace(/ /g, '_');
    const m = /^([^/\\]+)\.([A-Za-z0-9]{2,5})$/.exec(file);
    if (!m) return null;
    return { kind: 'file', fileName: file, ext: m[2].toLowerCase(),
      pageUrl: WT_ORIGIN + '/photo/' + m[2].toLowerCase() + '/' + encodeURIComponent(m[1]) };
  }

  function wtHostedSpace(name) {
    const spaceName = String(name || '').trim().replace(/ /g, '_').replace(/#.*$/, '');
    if (!spaceName) return null;
    const p = wtParsePage(WT_ORIGIN + '/wiki/Space:' + encodeURIComponent(spaceName));
    return p ? { kind: 'space', spaceName: p.spaceName, pageUrl: p.url } : null;
  }

  // A WikiTree address that is a file it holds or a free-space page, or null.
  function wtHostedFromUrl(url) {
    let u;
    try {
      u = new URL(url);
    } catch (e) {
      return null;
    }
    const host = u.hostname.toLowerCase();
    if (host !== WT_HOST && host !== 'wikitree.com') return null;
    let path;
    try {
      path = decodeURIComponent(u.pathname);
    } catch (e) {
      return null;
    }
    let m = /^\/photo\/([A-Za-z0-9]{2,5})\/([^/]+)$/.exec(path);
    if (m) return wtHostedFile(m[2] + '.' + m[1]);
    m = /^\/photo\.php\/(?:thumb\/)?[0-9a-f]\/[0-9a-f]{2}\/([^/]+\.[A-Za-z0-9]{2,5})(?:\/.*)?$/.exec(path);
    if (m) return wtHostedFile(m[1]);
    const p = wtParsePage(u.href);
    if (p && p.kind === 'space') return { kind: 'space', spaceName: p.spaceName, pageUrl: p.url };
    return null;
  }

  // One entry as the archive keeps it: the text as written, what it links to,
  // and whether it links anywhere at all (`linked`: false is a source to find).
  function wtEntry(text, from) {
    const l = wtEntryLinks(text);
    return { text, from, links: l.links, interwiki: l.interwiki, hosted: l.hosted,
      linked: l.links.length > 0 || l.interwiki.length > 0 };
  }

  // Two entries are one work cited twice when their words match, whatever the
  // list numbering, spacing, case or closing full stop.
  function wtEntryKey(text) {
    return String(text || '').replace(/^\s*\d+\.\s*/, '').replace(/<[^>]+>/g, ' ').replace(/'{2,}/g, '')
      .replace(/\s+/g, ' ').trim().replace(/[.\s]+$/, '').toLowerCase();
  }

  function wtDedupe(entries) {
    const seen = new Set();
    const out = [];
    for (const e of entries) {
      const key = wtEntryKey(e.text);
      if (!key || seen.has(key)) continue;
      seen.add(key);
      out.push(e);
    }
    return out;
  }

  // Every cited source in the biography's wiki text, in page order: each
  // <ref>…</ref> as it first appears, then each entry of the Sources section,
  // one work once. The text is kept as written: author, title and year are not
  // read out here (#416's citation parser does that later, from this text).
  function wtSources(wikitext) {
    let t = String(wikitext || '').replace(/\r\n?/g, '\n').replace(/<!--[\s\S]*?-->/g, '');
    const entries = [];
    t = t.replace(/<ref\b[^>]*\/\s*>/gi, '');
    t = t.replace(/<ref\b[^>]*>([\s\S]*?)<\/ref\s*>/gi, (all, inner) => {
      const text = inner.trim();
      if (text) entries.push(wtEntry(text, 'ref'));
      return '';
    });
    const lines = t.split('\n');
    const head = lines.findIndex((l) => /^\s*(={2,6})\s*'*\s*sources?\s*'*\s*\1\s*$/i.test(l) ||
      /^\s*={2,6}\s*sources?\s*={2,6}\s*$/i.test(l));
    if (head >= 0) {
      const level = (/^\s*(=+)/.exec(lines[head]) || ['', '=='])[1].length;
      let cur = null;
      let skip = false;
      for (let i = head + 1; i < lines.length; i++) {
        const line = lines[i];
        const h = /^\s*(={2,6})\s*([^=].*?)\s*={2,6}\s*$/.exec(line);
        // A heading at the Sources level ends the list. A sub-heading such as
        // Primary sources continues it; Acknowledgements (thanks, the import
        // note) is skipped up to the next heading. The page path does the same.
        if (h && h[1].length <= level) break;
        if (h) skip = WT_NOT_SOURCES_RE.test(h[2].replace(/'{2,}/g, '').trim());
        const s = line.replace(/<\/?references\b[^>]*>/gi, '').trim();
        if (!s || h || skip) {
          cur = null;
          continue;
        }
        const bullet = /^[*#:]+\s*/.exec(s);
        const body = bullet ? s.slice(bullet[0].length).trim() : s;
        if (!body) continue;
        // "See also:" and the like label the list; they cite nothing.
        if (/^(see also|sources?|references?|notes?|research notes?|footnotes?)\s*:?\s*$/i.test(body.replace(/'{2,}/g, ''))) {
          cur = null;
          continue;
        }
        if (bullet || !cur) {
          cur = wtEntry(body, 'list');
          entries.push(cur);
        } else {
          const joined = wtEntry(cur.text + '\n' + body, 'list');
          entries[entries.indexOf(cur)] = joined;
          cur = joined;
        }
      }
    }
    return wtDedupe(entries);
  }

  // The same list from the page itself (when the API refuses): the footnotes
  // (ol.references) first, then each paragraph or item under the Sources heading.
  // Links are the page's own <a href>s; the text is what the page shows.
  function wtSourcesFromPage(root) {
    const entries = [];
    const fromNode = (node, from) => {
      const text = wtText(node);
      if (!text || /^(see also|sources?|references?|notes?)\s*:?$/i.test(text)) return;
      const hrefs = wtLinksIn(node).map((h) => {
        try {
          return new URL(h, WT_ORIGIN).href;
        } catch (e) {
          return null;
        }
      }).filter((h) => h && /^https?:/.test(h));
      const e = wtEntry(text + (hrefs.length ? '\n' + hrefs.join('\n') : ''), from);
      e.text = text;
      // A link to another WikiTree profile is not a source elsewhere.
      e.links = e.links.filter((u) => !(wtParsePage(u) && wtParsePage(u).kind === 'profile'));
      e.linked = e.links.length > 0 || e.interwiki.length > 0;
      entries.push(e);
    };
    for (const ol of wtElements(root, (e) => wtTag(e) === 'OL' && /(^|\s)references(\s|$)/.test(wtAttr(e, 'class') || ''))) {
      for (const li of (ol.childNodes || []).filter((c) => wtTag(c) === 'LI')) {
        const ref = wtElements(li, (e) => /(^|\s)reference-text(\s|$)/.test(wtAttr(e, 'class') || ''))[0];
        fromNode(ref || li, 'ref');
      }
    }
    const head = wtElements(root, (e) => /^H[23]$/.test(wtTag(e)) && /^sources?$/i.test(wtText(e)))[0];
    if (head && head.parentNode) {
      const kids = Array.from(head.parentNode.childNodes || []);
      const level = Number(wtTag(head).slice(1));
      let skip = false;
      for (let i = kids.indexOf(head) + 1; i < kids.length; i++) {
        const n = kids[i];
        const hm = /^H([1-6])$/.exec(wtTag(n));
        // Same rule as wtSources: a heading at the Sources level ends the list,
        // a sub-heading of thanks or research notes is skipped.
        if (hm && Number(hm[1]) <= level) break;
        if (hm) {
          skip = WT_NOT_SOURCES_RE.test(wtText(n));
          continue;
        }
        if (skip) continue;
        if (wtTag(n) === 'P') fromNode(n, 'list');
        else if (wtTag(n) === 'UL' || wtTag(n) === 'OL') {
          if (/(^|\s)references(\s|$)/.test(wtAttr(n, 'class') || '')) continue;
          for (const li of (n.childNodes || []).filter((c) => wtTag(c) === 'LI')) fromNode(li, 'list');
        }
      }
    }
    return wtDedupe(entries);
  }

  // Every file or free-space page WikiTree itself holds that a cited source
  // points to, once each, numbered in page order: [{ n, kind, fileName|spaceName,
  // ext, pageUrl, entries: [index…] }]. The caller saves at most
  // WT_MAX_HOSTED of them.
  function wtHostedFiles(sources) {
    const out = [];
    (sources || []).forEach((s, i) => {
      for (const h of s.hosted || []) {
        const key = h.kind + ':' + (h.fileName || h.spaceName).toLowerCase();
        let got = out.find((x) => x.key === key);
        if (!got) {
          got = Object.assign({ key, n: out.length + 1, entries: [] }, h);
          out.push(got);
        }
        if (got.entries.indexOf(i) < 0) got.entries.push(i);
      }
    });
    return out.map((h) => {
      const c = Object.assign({}, h);
      delete c.key;
      return c;
    });
  }

  // Is this an original uploaded file on www.wikitree.com (never a thumbnail,
  // never another host)? The one rule for every file the capture downloads.
  function wtOriginalUrlOk(url) {
    let u;
    try {
      u = new URL(url);
    } catch (e) {
      return false;
    }
    return u.protocol === 'https:' && u.hostname.toLowerCase() === WT_HOST && !u.port && !u.username && !u.password &&
      !u.search && WT_ORIGINAL_PATH_RE.test(u.pathname);
  }

  // A held file's own page (/photo/{ext}/{name}): its caption (the uploader's
  // title, in the <h1>) and the address of the original, or null for each that
  // the page does not show.
  function wtFileFromPage(root, fileName) {
    const h1 = wtElements(root, (e) => wtTag(e) === 'H1')[0];
    const caption = h1 ? wtText(h1).split('\n')[0] || null : null;
    const want = String(fileName || '').toLowerCase();
    let original = null;
    for (const href of wtLinksIn(root)) {
      let abs;
      try {
        abs = new URL(href, WT_ORIGIN);
      } catch (e) {
        continue;
      }
      if (!wtOriginalUrlOk(abs.href)) continue;
      let last;
      try {
        last = decodeURIComponent(abs.pathname.split('/').pop()).toLowerCase();
      } catch (e) {
        continue;
      }
      if (!want || last === want) {
        original = abs.href;
        break;
      }
    }
    return { caption, originalUrl: original };
  }

  // A thumbnail address (/photo.php/thumb/{h}/{hh}/{name}/{N}px-{name}) as its
  // original's (/photo.php/{h}/{hh}/{name}), or null.
  function wtOriginalFromThumb(thumb) {
    const m = /^(?:https?:\/\/(?:www\.)?wikitree\.com)?\/photo\.php\/thumb\/([0-9a-f])\/([0-9a-f]{2})\/([^/]+)\/[^/]+$/i
      .exec(String(thumb || ''));
    if (!m) return null;
    const url = WT_ORIGIN + '/photo.php/' + m[1] + '/' + m[2] + '/' + m[3];
    return wtOriginalUrlOk(url) ? url : null;
  }

  // A profile's photos from getPhotos: [{ imageName, caption, date, place,
  // pageUrl, originalUrl }], each with an original on www.wikitree.com.
  function wtPhotosFromApi(json) {
    const entry = Array.isArray(json) ? json[0] : null;
    const list = entry && Array.isArray(entry.photos) ? entry.photos : [];
    const out = [];
    for (const p of list) {
      if (!p || typeof p !== 'object') continue;
      const originalUrl = wtOriginalFromThumb(p.URL_300) || wtOriginalFromThumb(p.URL_75);
      if (!originalUrl) continue;
      out.push({
        imageName: wtStr(p.ImageName) || null,
        caption: wtStr(p.Title) || null,
        date: wtDate(p.Date),
        place: wtStr(p.Location) || null,
        pageUrl: /^\/photo\//.test(wtStr(p.URL)) ? WT_ORIGIN + wtStr(p.URL) : null,
        originalUrl,
      });
    }
    return out;
  }

  // Wiki text as plain text a person reads: headings as "## Heading", links as
  // their words, footnotes and templates left out, bold and italics unmarked.
  // Used for the biography (page.md) and a free-space page's transcription.
  function wtPlainText(wikitext) {
    let t = String(wikitext || '').replace(/\r\n?/g, '\n').replace(/<!--[\s\S]*?-->/g, '');
    t = t.replace(/<ref\b[^>]*\/\s*>/gi, '').replace(/<ref\b[^>]*>[\s\S]*?<\/ref\s*>/gi, '');
    t = t.replace(/<\/?references\b[^>]*>/gi, '');
    t = t.replace(/\[\[\s*:?\s*category\s*:[^\]]*\]\]/gi, '');
    // Templates, innermost first (a few levels deep is all a page uses).
    for (let i = 0; i < 4; i++) t = t.replace(/\{\{[^{}]*\}\}/g, '');
    t = t.replace(/\[\[\s*:?\s*(?:image|file)\s*:([^\]|]+)(?:\|[^\]]*)?\]\]/gi, (a, f) => '[file: ' + f.trim() + ']');
    t = t.replace(/\[\[([^\]|]+)\|([^\]]*)\]\]/g, '$2');
    t = t.replace(/\[\[([^\]]+)\]\]/g, '$1');
    t = t.replace(/\[(https?:\/\/[^\s\]]+)\s+([^\]]+)\]/g, '$2 ($1)');
    t = t.replace(/\[(https?:\/\/[^\s\]]+)\]/g, '$1');
    t = t.replace(/'{2,}/g, '');
    t = t.replace(/<br\s*\/?>/gi, '\n').replace(/<\/?(?:span|div|p|b|i|u|small|big|sup|sub|center|font)\b[^>]*>/gi, '');
    t = t.replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
    const out = t.split('\n').map((line) => {
      const h = /^\s*(={2,6})\s*(.*?)\s*={2,6}\s*$/.exec(line);
      if (h) return '\n' + '#'.repeat(Math.min(h[1].length, 6)) + ' ' + h[2] + '\n';
      return line.replace(/^\s*[*#]+\s*/, (b) => (b.trim().startsWith('*') ? '- ' : '')).replace(/[ \t]+$/g, '')
        .replace(/^[ \t]+/, '');
    }).join('\n');
    return out.replace(/\n{3,}/g, '\n\n').trim();
  }

  // The citation: WikiTree, "{name} ({WikiTree ID})," ({address} : last edited
  // {date}; accessed {date}). A free-space page's name stands in for the person.
  function wtCitation(record, accessed) {
    const label = record.kind === 'space'
      ? 'Space:' + String(record.name || record.spaceName)
      : String(record.name || record.wikitreeId) + ' (' + record.wikitreeId + ')';
    return 'WikiTree, "' + label + '," (' + record.pageUrl + ' : ' +
      (record.lastEdited ? 'last edited ' + record.lastEdited + '; ' : '') + 'accessed ' + accessed + ').';
  }

  // One entry on one line, as plain text.
  function wtEntryLine(text) {
    return wtPlainText(String(text || '').replace(/\[(https?:\/\/[^\s\]]+)\s+([^\]]+)\]/g, '$2')).replace(/\s*\n\s*/g, ' ').replace(/^\d+\.\s+/, '').trim();
  }

  // The "Cited sources" list for the notes: linked entries first, each with
  // where it points (and, for a file WikiTree holds, the file this capture saved
  // it as), then "To find:" with the ones that point nowhere. Each is a "- [ ]"
  // line he can tick in any Markdown editor. `saved` maps a hosted file's `n`
  // to { file, caption, pageUrl } for the ones saved.
  function wtCitedSourcesNotes(sources, hosted, saved) {
    if (!sources || !sources.length) return '';
    const savedFor = (i) => (hosted || []).filter((h) => h.entries.indexOf(i) >= 0 && saved && saved[h.n])
      .map((h) => saved[h.n]);
    const linked = [];
    const toFind = [];
    sources.forEach((s, i) => {
      const words = wtEntryLine(s.text) || s.text.replace(/\s+/g, ' ').trim();
      if (!s.linked) {
        toFind.push('- [ ] ' + words);
        return;
      }
      const where = s.links.concat(s.interwiki.filter((w) => !s.links.some((u) => u.indexOf(w.split(':')[1]) >= 0)));
      const keep = savedFor(i).map((f) => 'saved in this source as ' + f.file +
        (f.caption ? ' ("' + f.caption + '")' : '') + ', from ' + f.pageUrl);
      linked.push('- [ ] ' + words + (where.length ? ' - ' + where.join(' ; ') : '') +
        (keep.length ? '; ' + keep.join('; ') : ''));
    });
    const out = ['### Cited sources', ''];
    if (linked.length) out.push('Linked (open each and check it):', ...linked, '');
    if (toFind.length) out.push('To find:', ...toFind, '');
    return out.join('\n').trim();
  }
  // FHA-SYNC-END wikitree

  // ── WikiTree profiles and free-space pages ─────────────────────────────────
  // A profile is saved with its ID, its last edit, its family as WikiTree IDs
  // and its cited sources as a list he can tick through. What WikiTree says
  // comes from its public data service (one getProfile call, no login), or
  // from the page itself when the service will not answer. Then, only when the
  // page shows photos, one getPhotos call; and for each cited source that is a
  // file WikiTree holds, at most WT_MAX_HOSTED of them, that file's own page
  // (its caption and the original's address) or, for a free-space page, its
  // text. One request each, never with cookies. The panel downloads the files
  // themselves (FHA.wikitree.wtFetchFile); files held on other sites are never fetched.
  async function wtGetJson(url) {
    const r = await fetch(url, { credentials: 'omit', referrerPolicy: 'no-referrer' });
    if (!r.ok) throw Object.assign(new Error('HTTP ' + r.status), { status: r.status });
    return JSON.parse(await r.text());
  }

  function wtHostedBlank(h) {
    return { n: h.n, kind: h.kind, fileName: h.fileName || null, spaceName: h.spaceName || null,
      ext: h.ext || null, pageUrl: h.pageUrl, entries: h.entries, caption: null, originalUrl: null, text: null };
  }

  async function wtHostedRead(h) {
    const out = wtHostedBlank(h);
    try {
      if (h.kind === 'space') {
        const parsed = wtParsePage(h.pageUrl);
        let json;
        try {
          json = await wtGetJson(wtApiUrl(parsed.key));
        } catch (e) {
          if (e && (e.status === 429 || e.status === 503)) out.slowDown = true;
          throw e;
        }
        const got = wtRecordFromApi(json, parsed);
        if (!got.ok || !got.bio) {
          out.error = got.ok ? 'WikiTree shows no text on that free-space page' : got.error;
          return out;
        }
        out.caption = got.record.name;
        out.text = wtPlainText(got.bio);
        return out;
      }
      const r = await fetch(h.pageUrl, { credentials: 'omit', referrerPolicy: 'no-referrer' });
      if (!r.ok) {
        out.error = 'WikiTree did not show the file’s page (HTTP ' + r.status + ')';
        if (r.status === 429 || r.status === 503) out.slowDown = true;
        return out;
      }
      const f = wtFileFromPage(new DOMParser().parseFromString(await r.text(), 'text/html'), h.fileName);
      out.caption = f.caption;
      out.originalUrl = f.originalUrl;
      if (!f.originalUrl) out.error = 'the file’s page on WikiTree did not link its original';
    } catch (e) {
      out.error = 'WikiTree did not answer (' + ((e && e.message) || e) + ')';
    }
    return out;
  }

  async function wikitreeInfo(today) {
    const parsed = wtParsePage(location.href);
    if (!parsed) return { ok: false, error: 'this is not a WikiTree profile or free-space page' };
    // A "slow down" (429/503) from WikiTree stops the requests that follow, and
    // the panel is told, so it downloads nothing more from WikiTree this time.
    let slowDown = false;
    let api = null;
    let apiError = null;
    try {
      api = wtRecordFromApi(await wtGetJson(wtApiUrl(parsed.key)), parsed);
      if (!api.ok) apiError = api.error;
    } catch (e) {
      apiError = 'WikiTree’s public data service did not answer (' + ((e && e.message) || e) + ')';
      if (e && (e.status === 429 || e.status === 503)) slowDown = true;
    }
    // The page is read when the service refused, or shared no biography.
    const page = api && api.ok && api.bio != null ? null : wtRecordFromPage(document, parsed);
    let record;
    let sources = [];
    let bioText = '';
    let via = 'api';
    if (api && api.ok) {
      record = api.record;
      if (api.bio != null) {
        sources = wtSources(api.bio);
        bioText = wtPlainText(api.bio);
      } else if (page && page.ok) {
        sources = page.sources;
        bioText = page.bioText;
        via = 'api+page';
      }
    } else if (page && page.ok) {
      record = page.record;
      sources = page.sources;
      bioText = page.bioText;
      via = 'page';
    } else {
      return { ok: false, error: (page && page.error) || apiError };
    }
    let photos = [];
    let photosTotal = null;
    let photosError = null;
    if (record.kind === 'profile') {
      const head = wtElements(document, (e) => wtAttr(e, 'id') === 'wt-photos')[0];
      const shown = head ? Number(wtAttr(head, 'data-photo-total')) : NaN;
      if (Number.isFinite(shown)) photosTotal = shown;
      if (shown !== 0 && !slowDown) {
        try {
          photos = wtPhotosFromApi(await wtGetJson(wtApiUrl(record.wikitreeId, 'photos')));
        } catch (e) {
          photosError = 'WikiTree’s list of photos did not come (' + ((e && e.message) || e) + ')';
          if (e && (e.status === 429 || e.status === 503)) slowDown = true;
        }
      }
    }
    const hostedAll = wtHostedFiles(sources);
    const hosted = [];
    for (const h of hostedAll.slice(0, WT_MAX_HOSTED)) {
      if (slowDown) {
        hosted.push(Object.assign(wtHostedBlank(h), { error: 'not tried: WikiTree asked us to slow down' }));
        continue;
      }
      const got = await wtHostedRead(h);
      if (got.slowDown) slowDown = true;
      hosted.push(got);
    }
    const name = record.name;
    const title = record.kind === 'space' ? name + ' - WikiTree free-space page'
      : name + ' (' + record.wikitreeId + ') - WikiTree profile';
    return {
      ok: true, via, apiError: via === 'api' ? null : apiError, key: parsed.key, record,
      bioWiki: api && api.ok ? api.bio : null, bioText, sources, citation: wtCitation(record, today), title,
      photos, photosTotal, photosError, hosted, hostedTotal: hostedAll.length, slowDown,
    };
  }

  // The memorial page itself - not its photo gallery or its flowers.
  function fagMemorialPage(href) {
    if (!fagParseMemorial(href)) return false;
    try {
      return /^\/(?:[a-z]{2}\/)?memorial\/\d+(?:\/(?!(?:photos?|flowers?)\/?$)[^/]+)?\/?$/i.test(new URL(href).pathname);
    } catch (e) {
      return false;
    }
  }

  // The memorial, plus what it says about itself (fagHints) and its title.
  function findagraveMemorialInfo(today) {
    const info = fagMemorialFrom(document, location.href, {
      title: document.title, hidden: ANC_CSS.hidden, block: ANC_CSS.block, today,
    });
    if (!info.ok) return info;
    info.hints = fagHints(info.record);
    info.title = fagTitle(info.record);
    return info;
  }

  // The memorial's photos, from its gallery pages on Find a Grave, each with
  // its role ("grave-1", "person-1", …).
  async function findagraveGallery(memorialId, slug) {
    const got = await fagGallery(memorialId, slug, {
      fetch: (url, init) => fetch(url, init),
      parse: (html) => new DOMParser().parseFromString(html, 'text/html'),
      base: location.origin,
    });
    if (!got.ok) return got;
    const counts = {};
    for (const p of got.photos) p.role = fagPhotoRole(p.type, counts);
    return got;
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


  // ── Newspapers.com clippings (page + clipping, full resolution) ──────────────
  // Newspapers.com has no downloadable original: its "Download PDF" fetches a
  // 2048px JPEG and builds the PDF in the browser, and its viewer draws the page
  // from tiles. What it DOES expose, in the page's own embedded data, is every
  // clipping's rectangle on the page, the page's full size, and a day-long page
  // pass that its tile service accepts. So the panel can rebuild the page at full
  // resolution from tiles and cut each clipping out of it (see lib/newspapers.js).
  // This side only READS: the embedded data, plus the one same-origin list of
  // clippings on the page the viewer itself requests on load.
  // The pure parsers below are a COPY of lib/newspapers-page-data.js (the
  // canonical, tested module); tests/test-sync.js fails if they drift.
  // FHA-SYNC-BEGIN newspapers
  // Which kind of Newspapers.com address this is: a clipping page (`article`,
  // also the older /clip/{id}/) or the page viewer (`image`); null otherwise.
  function npPageKind(href) {
    let u;
    try {
      u = new URL(href);
    } catch (e) {
      return null;
    }
    if (!/(^|\.)newspapers\.com$/i.test(u.hostname)) return null;
    let m = u.pathname.match(/^\/article\/[^/]+\/(\d+)\/?/);
    if (m) return { kind: 'article', clippingId: Number(m[1]), pageId: null };
    m = u.pathname.match(/^\/(?:article|clip)\/(\d+)\/?/);
    if (m) return { kind: 'article', clippingId: Number(m[1]), pageId: null };
    m = u.pathname.match(/^\/image\/(\d+)\/?/);
    if (m) {
      return {
        kind: 'image',
        pageId: Number(m[1]),
        clippingId: Number(u.searchParams.get('clipping_id')) || null,
      };
    }
    return null;
  }

  // The site is a Next.js app: its data rides in `self.__next_f.push([1,"…"])`
  // script strings. Decoding those strings yields plain JSON text to search.
  function npFlightFrom(scriptTexts) {
    let out = '';
    const re = /self\.__next_f\.push\(\[\d+,("(?:[^"\\]|\\.)*")\]\)/g;
    for (const t of scriptTexts) {
      if (!t || t.indexOf('__next_f') < 0) continue;
      let m;
      re.lastIndex = 0;
      while ((m = re.exec(t))) {
        try {
          out += JSON.parse(m[1]);
        } catch (e) {
          /* skip a chunk that is not a plain string */
        }
      }
    }
    return out;
  }

  // The JSON object starting at text[start] (a '{'), parsed, or null. Braces
  // inside strings do not count, so a title holding "}" cannot cut it short.
  function npObjectAt(text, start) {
    let depth = 0;
    let inStr = false;
    for (let k = start; k < text.length; k++) {
      const c = text[k];
      if (inStr) {
        if (c === '\\') k++;
        else if (c === '"') inStr = false;
        continue;
      }
      if (c === '"') inStr = true;
      else if (c === '{') depth++;
      else if (c === '}') {
        depth--;
        if (depth === 0) {
          try {
            return JSON.parse(text.slice(start, k + 1));
          } catch (e) {
            return null;
          }
        }
      }
    }
    return null;
  }

  function npObjectAfter(text, key, from) {
    const needle = '"' + key + '":{';
    const i = text.indexOf(needle, from || 0);
    return i < 0 ? null : npObjectAt(text, i + needle.length - 1);
  }

  function npJwtPayload(token) {
    try {
      const part = String(token).split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      return JSON.parse(atob(part + '==='.slice((part.length + 3) % 4)));
    } catch (e) {
      return null;
    }
  }

  // The site's page label is usually "4" but sometimes "Page 4" (or "p. 4");
  // keep only the label after any such prefix, so a title never reads "p. Page 4".
  function npPageNo(v) {
    if (v == null) return null;
    const t = String(v).trim().replace(/^(?:page\s+|page(?=\d)|pg\.\s*|pg\s+|p\.\s*)/i, '').trim();
    return t || null;
  }

  // One clipping, from the page data or the clipping list, in the shape the
  // panel uses. `toAbs` makes the site's relative clipping url absolute.
  function npClip(c, toAbs) {
    const raw = Array.isArray(c.rectangles) && c.rectangles.length ? c.rectangles : [c.rectangle];
    const rects = raw
      .filter((r) => r && r.width > 0 && r.height > 0)
      .map((r) => ({
        x: Math.max(0, Math.round(r.x)),
        y: Math.max(0, Math.round(r.y)),
        w: Math.round(r.width),
        h: Math.round(r.height),
        rotation: r.rotation || 0,
      }));
    return {
      id: c.clipping_id,
      pageId: c.page_id,
      title: String(c.title || '').trim(),
      tags: c.tags || '',
      rects,
      userId: c.user_id,
      username: c.user_username || '',
      created: c.created || null,
      pageDate: c.page_date || null,
      pageNo: npPageNo(c.page_title),
      pageWidth: c.page_width || null,
      pageHeight: c.page_height || null,
      publication: c.publication
        ? { name: c.publication.name || null, location: c.publication.location || null }
        : null,
      url: c.url ? (toAbs ? toAbs(c.url) : c.url) : null,
      // The site's machine OCR of the clipping: a "$<id>" pointer to a text
      // chunk in the page data (resolved by npTextChunk), or occasionally inline.
      ocrRef: typeof c.ocr === 'string' && c.ocr.charAt(0) === '$' ? c.ocr.slice(1) : null,
      ocr: typeof c.ocr === 'string' && c.ocr.charAt(0) !== '$' ? c.ocr : null,
    };
  }

  // Resolve a "$<id>" text chunk in the page data: `<id>:T<hex byte length>,<text>`.
  // The length counts UTF-8 bytes, so walk characters until the bytes run out.
  function npTextChunk(text, ref) {
    if (!ref || !/^[0-9a-z]+$/i.test(ref)) return null;
    const re = new RegExp('(?:^|[^0-9a-z])' + ref + ':T([0-9a-f]+),');
    const m = re.exec(text);
    if (!m) return null;
    let bytes = parseInt(m[1], 16);
    let i = m.index + m[0].length;
    const start = i;
    while (bytes > 0 && i < text.length) {
      const cp = text.codePointAt(i);
      bytes -= cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
      i += cp > 0xffff ? 2 : 1;
    }
    return text.slice(start, i);
  }

  // Did the signed-in member make this clipping? By user id, else by username.
  function npIsMine(c, me) {
    return (
      (!!me.id && c.userId === me.id) ||
      (!!me.username && !!c.username && c.username.toLowerCase() === me.username.toLowerCase())
    );
  }

  // Can this clipping be cut out of the page? Only if it marks at least one
  // real area on it. A deleted or damaged list entry can come back with no
  // rectangle at all, and offering it would let a capture download the whole
  // page only to fail at the cut.
  function npUsable(c) {
    return !!(c && Array.isArray(c.rects) && c.rects.length);
  }

  // Which clippings to offer (owner decision, 2026-10-08):
  //   - on a clipping page, THAT clipping, whoever made it - opening it is the
  //     choice - first, then your own other clippings on the page;
  //   - on the page viewer (`opened` null), only your own clippings.
  // A clipping with no usable area is never offered; it is listed in
  // `unusable` instead, so the panel can say which one and why.
  // Returns { offered, unusable, openedUnusable, otherClipCount, focusOwner }:
  // focusOwner names the clipper of an opened clipping that is not yours, and
  // otherClipCount counts the other members' clippings that are skipped.
  function npOffered(clips, opened, me) {
    const candidates = clips.filter((c) => npIsMine(c, me));
    if (opened && !npIsMine(opened, me)) candidates.unshift(opened);
    const offered = candidates.filter(npUsable);
    return {
      offered,
      unusable: candidates
        .filter((c) => !npUsable(c))
        .map((c) => ({ id: c.id, title: c.title || '' })),
      openedUnusable: !!opened && !npUsable(opened),
      otherClipCount: clips.filter((c) => !candidates.some((o) => o.id === c.id)).length,
      focusOwner:
        opened && !npIsMine(opened, me) && npUsable(opened)
          ? opened.username || 'another member'
          : null,
    };
  }
  // FHA-SYNC-END newspapers

  function npFlightText() {
    const live = npFlightFrom([...document.scripts].map((s) => s.textContent || ''));
    return live || document.documentElement.outerHTML.replace(/\\"/g, '"');
  }

  // FHA-SYNC-BEGIN batch-sources
  const BATCH_FS_PERSON_RE = /\/tree\/person\/(?:[a-z-]+\/)?([A-Z0-9]{4}-[A-Z0-9]{2,4})(?![A-Z0-9])/i;
  const BATCH_ANC_HOST_RE = /(^|\.)ancestry\.[a-z.]+$/i;
  const BATCH_FS_HOST_RE = /(^|\.)familysearch\.org$/i;
  const BATCH_NP_HOST_RE = /(^|\.)newspapers\.com$/i;
  // A Newspapers.com clipping's own page: /article/{slug}/{id}/, /article/{id}/
  // or the older /clip/{id}/{slug}/.
  const BATCH_NP_CLIP_PATH_RE = /^\/article\/[^/]+\/(\d+)\/?$|^\/(?:article|clip)\/(\d+)(?:\/[^/]*)?\/?$/;

  // What a person's other sources are, counted: "links to example.org (2),
  // a note (1)". Labels only, for the panel to say what it did not queue.
  function batchDescribeOthers(list) {
    const n = {};
    for (const x of list) n[x] = (n[x] || 0) + 1;
    return Object.keys(n).map((k) => k + (n[k] > 1 ? ' (' + n[k] + ')' : '')).join(', ');
  }

  function batchFirstLine(el) {
    return el ? (ancText(el).split('\n')[0] || '').slice(0, 200) : '';
  }

  // An Ancestry tree person's Facts page (`root`, at `href`): the Ancestry
  // records its sources link to, as { dbId, recordId, title }; the
  // Newspapers.com clippings they link to, as { id, url, title }, for the
  // Newspapers.com batch; and a count of the sources that are something else
  // (a web link), so the panel can say what it left out rather than "no
  // sources". Read from the person's
  // own sources list (#sourcesSection) and nowhere else on the page: a record
  // page links its household's records, which are not this person's sources
  // (PR #224 Codex 4504294850). No such list: nothing (`sectionFound: false`).
  // Seen It's badges are skipped (ancText), and nothing is fetched.
  function batchAncPersonSources(root, href) {
    const seen = new Set();
    const sources = [];
    const box = ancById(root, 'sourcesSection');
    const h1 = ancElements(root, (e) => ancTag(e) === 'H1')[0];
    if (!box) {
      return { ok: true, person: batchFirstLine(h1) || null, sources, sectionFound: false, total: 0, other: 0, otherWhat: '' };
    }
    for (const a of ancElements(box, (e) => ancTag(e) === 'A' && !!e.getAttribute('href'))) {
      let u;
      try {
        u = new URL(a.getAttribute('href'), href);
      } catch (e) {
        continue;
      }
      // An image-only collection's source opens into its image:
      // /interactive/{dbId}/{imageId}/{recordId} is that record.
      const inter = BATCH_ANC_HOST_RE.test(u.hostname) && /\/interactive\/(\d+)\/[^/?#]+\/(\d+)/.exec(u.pathname);
      const id = inter ? { dbId: inter[1], recordId: inter[2] } : ancParseRecordPage(u.href);
      if (!id) continue;
      const key = id.dbId + ':' + id.recordId;
      if (seen.has(key)) continue;
      seen.add(key);
      sources.push({ dbId: id.dbId, recordId: id.recordId, title: batchFirstLine(a) });
    }
    const others = [];
    const clippings = [];
    for (const a of ancElements(box, (e) => ancTag(e) === 'A' && !!e.getAttribute('href'))) {
      let u;
      try {
        u = new URL(a.getAttribute('href'), href);
      } catch (e) {
        continue;
      }
      if (!/^https?:$/.test(u.protocol)) continue;
      const clip = BATCH_NP_HOST_RE.test(u.hostname) ? BATCH_NP_CLIP_PATH_RE.exec(u.pathname) : null;
      const clipId = clip ? String(Number(clip[1] || clip[2])) : null;
      if (clipId && clipId !== '0') {
        if (!clippings.some((c) => c.id === clipId)) {
          clippings.push({ id: clipId, url: 'https://www.newspapers.com' + u.pathname, title: batchFirstLine(a) });
        }
      } else if (BATCH_NP_HOST_RE.test(u.hostname) && /\/(clip|article)\//.test(u.pathname)) others.push('Newspapers.com links');
      else if (!/(^|\.)(ancestry|ancestrycdn)\.[a-z.]+$/i.test(u.hostname)) others.push('links to ' + u.hostname.replace(/^www\./, ''));
    }
    return {
      ok: true,
      person: batchFirstLine(h1) || null,
      sources,
      clippings,
      sectionFound: true,
      total: sources.length + clippings.length + others.length,
      other: others.length,
      otherWhat: batchDescribeOthers(others),
    };
  }

  // An Ancestry tree person's Gallery page (`root`, at `href`:
  // /family-tree/person/tree/{treeId}/person/{personId}/gallery): the photos,
  // clippings and documents its tiles open, as { mediaId, url, title } - the
  // media viewer's address without its query, which is how Seen It knows a
  // shared item (med:{mediaId}), and the tile's caption. Read from the tiles
  // (.mediaGalleryItem) and nowhere else on the page, once each, on the
  // page's own site; Seen It's badges are skipped (ancText), and nothing is
  // fetched. The gallery shows so many tiles a page: `more` says this page
  // links to the next one, which she opens herself - the batch never pages
  // through a gallery on its own. Not a gallery page: nothing
  // (`sectionFound: false`).
  const BATCH_ANC_GALLERY_RE = /^\/family-tree\/person\/tree\/\d+\/person\/\d+\/gallery\/?$/i;

  function batchAncGallery(root, href) {
    const person = batchFirstLine(ancElements(root, (e) => ancTag(e) === 'H1')[0]) || null;
    const sources = [];
    let here = null;
    try {
      here = new URL(href);
    } catch (e) {
      here = null;
    }
    if (!here || !BATCH_ANC_HOST_RE.test(here.hostname) || !BATCH_ANC_GALLERY_RE.test(here.pathname)) {
      return { ok: true, person, sources, sectionFound: false, total: 0, other: 0, otherWhat: '', more: false, page: 1 };
    }
    const hasClass = (e, c) => String(e.className || '').split(/\s+/).indexOf(c) >= 0;
    const link = (a) => {
      try {
        return new URL(a.getAttribute('href'), href);
      } catch (e) {
        return null;
      }
    };
    const seen = new Set();
    for (const tile of ancElements(root, (e) => hasClass(e, 'mediaGalleryItem'))) {
      const caption = ancElements(tile, (e) => hasClass(e, 'mediaCardCaption'))[0];
      const titled = ancElements(tile, (e) => !!(e.getAttribute && e.getAttribute('title')))[0];
      for (const a of ancElements(tile, (e) => ancTag(e) === 'A' && !!e.getAttribute('href'))) {
        const u = link(a);
        if (!u || u.origin !== here.origin) continue;
        const id = ancParseMediaPage(u.href);
        if (!id || !id.treeId || !id.personId || seen.has(id.mediaId)) continue;
        seen.add(id.mediaId);
        const title = batchFirstLine(caption) || String((titled && titled.getAttribute('title')) || '').slice(0, 200) ||
          batchFirstLine(a);
        sources.push({ mediaId: id.mediaId, url: id.url, title });
      }
    }
    // The next page: a link to this gallery's galleryPage={this one + 1}.
    const page = Math.max(1, Number(here.searchParams.get('galleryPage')) || 1);
    const more = ancElements(root, (e) => {
      if (ancTag(e) !== 'A' || !e.getAttribute('href')) return false;
      const u = link(e);
      return !!u && u.origin === here.origin && u.pathname === here.pathname &&
        Number(u.searchParams.get('galleryPage')) === page + 1;
    }).length > 0;
    return { ok: true, person, sources, sectionFound: true, total: sources.length, other: 0, otherWhat: '', more, page };
  }

  // The FamilySearch tree person an address names (/tree/person/details/{PID}),
  // or null.
  function batchFsPersonPid(href) {
    let u;
    try {
      u = new URL(href);
    } catch (e) {
      return null;
    }
    if (!BATCH_FS_HOST_RE.test(u.hostname)) return null;
    const m = BATCH_FS_PERSON_RE.exec(u.pathname);
    return m ? m[1].toUpperCase() : null;
  }

  // A FamilySearch tree person's sources (the Sources tab shows no links, so
  // FamilySearch's own data service is asked, with `d` - { fetch, base, token }
  // - as fsJson takes it): the FamilySearch records among them as { fsId,
  // title }, and a count of the rest. Two requests, one after the other.
  async function batchFsPersonSources(href, d) {
    const pid = batchFsPersonPid(href);
    if (!pid) return { ok: false, error: 'open a person in the FamilySearch Family Tree, then press the button again' };
    let j;
    try {
      j = await fsJson('/platform/tree/persons/' + pid + '/sources', 'application/x-fs-v1+json', d);
    } catch (e) {
      return { ok: false, error: e.message, slowDown: !!e.slowDown };
    }
    const sources = [];
    const others = [];
    const seen = new Set();
    const all = Array.isArray(j && j.sourceDescriptions) ? j.sourceDescriptions : [];
    for (const s of all) {
      const r = /ark:\/61903\/1:1:([A-Z0-9-]+)/i.exec(String((s && s.about) || ''));
      if (!r) {
        let host = null;
        try {
          host = s && s.about ? new URL(s.about).hostname.replace(/^www\./, '') : null;
        } catch (e) {
          /* not a web address */
        }
        others.push(host ? 'links to ' + host : 'a note or citation with no link');
        continue;
      }
      const fsId = r[1].toUpperCase();
      if (seen.has(fsId)) continue;
      seen.add(fsId);
      sources.push({ fsId, title: String((s.titles && s.titles[0] && s.titles[0].value) || '').slice(0, 200) });
    }
    let person = null;
    let slowDown = false;
    try {
      const p = await fsJson('/platform/tree/persons/' + pid, 'application/x-fs-v1+json', d);
      const disp = p && p.persons && p.persons[0] && p.persons[0].display;
      person = (disp && disp.name) || null;
    } catch (e) {
      // The name is a label only - but a "slow down" is still said.
      slowDown = !!e.slowDown;
    }
    return { ok: true, person, pid, sources, total: all.length, other: others.length, otherWhat: batchDescribeOthers(others),
      slowDown };
  }

  // The Find a Grave memorials a page names, each only from where it means
  // "these memorials" (PR #224 Codex 4504294850):
  //   - on Find a Grave, every memorial the page links to (a memorial's own
  //     family, a search or cemetery listing), the memorial itself first;
  //   - on an Ancestry tree person, the person's own sources list
  //     (#sourcesSection) only - never the hints and records elsewhere on it;
  //   - on a FamilySearch tree person (`d` given), the person's own sources,
  //     from FamilySearch's data, never the page's links.
  // Anywhere else, nothing. [{ memorialId, slug, title }].
  async function batchFagLinks(root, href, d) {
    const out = [];
    const add = (m, name) => {
      if (m && !out.some((x) => x.memorialId === m.memorialId)) {
        out.push({ memorialId: m.memorialId, slug: m.slug, title: String(name || '').slice(0, 200) });
      }
    };
    let person = null;
    let host = '';
    try {
      host = new URL(href).hostname;
    } catch (e) {
      /* no host: nothing below matches */
    }
    let scope = null; // the part of the page whose links are its memorials
    let sectionFound = true;
    let slowDown = false;
    if (/(^|\.)findagrave\.com$/i.test(host)) {
      scope = root;
      const here = fagParseMemorial(href);
      if (here) {
        person = batchFirstLine(ancById(root, 'bio-name')) || null;
        add(here, person);
      }
    } else if (BATCH_ANC_HOST_RE.test(host)) {
      scope = ancById(root, 'sourcesSection');
      sectionFound = !!scope;
    }
    for (const a of scope ? ancElements(scope, (e) => ancTag(e) === 'A' && /\/memorial\//.test(e.getAttribute('href') || '')) : []) {
      add(fagParseMemorial(a.getAttribute('href'), href), batchFirstLine(a));
    }
    if (BATCH_FS_HOST_RE.test(host)) {
      sectionFound = !!batchFsPersonPid(href);
      if (d && sectionFound) {
        try {
          const j = await fsJson('/platform/tree/persons/' + batchFsPersonPid(href) + '/sources', 'application/x-fs-v1+json', d);
          for (const s of (j && j.sourceDescriptions) || []) {
            add(fagParseMemorial(String((s && s.about) || '')), s && s.titles && s.titles[0] && s.titles[0].value);
          }
        } catch (e) {
          if (e.slowDown) slowDown = true;
          else return { ok: false, error: e.message };
        }
      }
    }
    if (!person) person = batchFirstLine(ancElements(root, (e) => ancTag(e) === 'H1')[0]) || null;
    return { ok: true, person, sources: out, total: out.length, other: 0, sectionFound, slowDown };
  }

  // Who is signed in to Newspapers.com, from the page's own data (`text`, the
  // decoded page data): the site's `"user":{"id":…,"username":…}` object.
  function batchNpUsername(text) {
    const t = String(text || '');
    const um = /"user":\{"id":(\d+)/.exec(t);
    if (!um) return null;
    const o = npObjectAt(t, um.index + '"user":'.length);
    return (o && typeof o.username === 'string' && o.username) || null;
  }

  // One page of the signed-in member's own clippings list (24 at a time, from
  // `cursor`): { ok, user, clippings, nextCursor, more } or { ok:false, error }.
  // `d` = { fetch, base }; `base` must be an https Newspapers.com page.
  async function batchNpClippingsList(cursor, username, d) {
    let base;
    try {
      base = new URL(d.base);
    } catch (e) {
      base = null;
    }
    if (!base || base.protocol !== 'https:' || !BATCH_NP_HOST_RE.test(base.hostname)) {
      return { ok: false, error: 'the tab is not on Newspapers.com, so your clippings list was not read' };
    }
    if (!username) {
      return { ok: false, error: 'could not tell who is signed in to Newspapers.com - sign in, reload the page, then press Start again' };
    }
    const path = '/api/clipping/list?user=' + encodeURIComponent(username) + '&visibility=all&count=24' +
      '&cursor_mark=' + encodeURIComponent(String(cursor == null ? 0 : cursor)) + '&product_id=1';
    let r;
    try {
      r = await d.fetch(new URL(path, base).href, { credentials: 'same-origin', headers: { accept: 'application/json' } });
    } catch (e) {
      return { ok: false, error: 'could not reach Newspapers.com for your clippings list - check your internet connection' };
    }
    if (r.status === 429 || r.status === 503) {
      return { ok: false, error: 'Newspapers.com asked us to slow down (HTTP ' + r.status + ')' };
    }
    if (r.status === 401 || r.status === 403) {
      return { ok: false, error: 'Newspapers.com refused your clippings list (HTTP ' + r.status + ') - sign in and reload the page' };
    }
    if (!r.ok) return { ok: false, error: 'Newspapers.com would not send your clippings list (HTTP ' + r.status + ')' };
    let j;
    try {
      j = await r.json();
    } catch (e) {
      // Not JSON is something else; anything else is the answer breaking off.
      return e instanceof SyntaxError
        ? { ok: false, error: 'Newspapers.com sent something other than your clippings list' }
        : { ok: false, transient: true, error: 'the connection to Newspapers.com broke off while your clippings list was coming down' };
    }
    return {
      ok: true,
      user: username,
      clippings: Array.isArray(j && j.clippings) ? j.clippings : [],
      nextCursor: j && j.next_cursor_mark != null ? j.next_cursor_mark : null,
      more: !!(j && j.more_results),
    };
  }
  // FHA-SYNC-END batch-sources

  // Batch downloads are the owner's own tool, switched off unless this
  // browser has been told to show them (chrome.storage.local
  // `batchDownloads: true`; src/batch.js says how). Off, unreadable or
  // missing all mean off: the batch questions below are then answered
  // "switched off", and nothing is read or sent anywhere.
  function batchSwitchOn() {
    return new Promise((resolve) => {
      try {
        chrome.storage.local.get({ batchDownloads: false }, (o) => {
          resolve(!chrome.runtime.lastError && !!o && o.batchDownloads === true);
        });
      } catch (e) {
        resolve(false);
      }
    });
  }

  // The batch's questions to a page the owner has open: what to queue from
  // it. Each reads only its own site (batch-sources above says what each may
  // send).
  async function batchSourcesRoute(msg) {
    switch (msg.action) {
      case 'ancestryPersonSources':
        if (!BATCH_ANC_HOST_RE.test(location.hostname)) return { ok: false, error: 'this tab is not on Ancestry' };
        return batchAncPersonSources(document, location.href);
      case 'ancestryPersonGallery':
        if (!BATCH_ANC_HOST_RE.test(location.hostname)) return { ok: false, error: 'this tab is not on Ancestry' };
        return batchAncGallery(document, location.href);
      case 'familysearchPersonSources':
        if (!BATCH_FS_HOST_RE.test(location.hostname)) return { ok: false, error: 'this tab is not on FamilySearch' };
        return batchFsPersonSources(location.href, fsDeps());
      case 'findagraveLinks':
        if (!FAG_HOST_RE.test(location.hostname) && !BATCH_ANC_HOST_RE.test(location.hostname) &&
            !BATCH_FS_HOST_RE.test(location.hostname)) {
          return { ok: false, error: 'this tab is not on Find a Grave, Ancestry or FamilySearch' };
        }
        return batchFagLinks(document, location.href, BATCH_FS_HOST_RE.test(location.hostname) ? fsDeps() : null);
      case 'newspapersList': {
        // Whoever is signed in on this page, always: a name the batch stored
        // earlier must not fetch another member's clippings as hers.
        const signedIn = batchNpUsername(npFlightText());
        if (signedIn && typeof msg.user === 'string' && msg.user &&
            msg.user.toLowerCase() !== signedIn.toLowerCase()) {
          return { ok: false, error: 'a different member is signed in to Newspapers.com than the one this list ' +
            'was started for - sign in as ' + msg.user + ', or press "Read my clippings list again"' };
        }
        return batchNpClippingsList(msg.cursor, signedIn, { fetch: (u, init) => fetch(u, init), base: location.href });
      }
      default:
        return { ok: false, error: 'not a batch question' };
    }
  }

  // The site navigates between clippings and pages without reloading, so the
  // data embedded at first load can describe an EARLIER record. When it does not
  // match the address bar, re-read the current address (one same-origin GET).
  async function npFreshFlightText() {
    const resp = await fetch(location.href, { credentials: 'include' });
    if (!resp.ok) {
      if (resp.status === 429 || resp.status === 503) {
        const e = new Error('Newspapers.com asked us to slow down (HTTP ' + resp.status + ') while this page was re-read');
        e.slowDown = true;
        throw e;
      }
      return '';
    }
    const html = await resp.text();
    return npFlightFrom([html]) || html.replace(/\\"/g, '"');
  }

  // The clipping's OCR from page data `text` (full chunk), else the page's
  // visible preview in `doc` (the first few paragraphs only).
  function npOcrText(text, clip, doc) {
    const full = clip.ocr || npTextChunk(text, clip.ocrRef);
    if (full && full.trim()) return { text: full.trim(), complete: true };
    const el = doc && doc.querySelector('[class*="OCRText"]');
    if (el) {
      const paras = [...el.querySelectorAll('p')].map((p) => p.textContent.trim()).filter(Boolean);
      const preview = (paras.length ? paras.join('\n\n') : el.textContent).trim();
      if (preview) return { text: preview, complete: false };
    }
    return null;
  }

  // The OCR for one clipping, by reading its own clipping page (one same-origin
  // GET), for clippings offered from the page viewer whose text is not here.
  async function npClipText(url, clippingId) {
    if (!url) return { ok: false, error: 'no clipping address' };
    try {
      const resp = await fetch(url, { credentials: 'include' });
      if (!resp.ok) {
        // The text is optional, but a "slow down" here is still one
        // (PR #224 Codex 4504665602).
        return { ok: false, status: resp.status, slowDown: resp.status === 429 || resp.status === 503,
          error: 'HTTP ' + resp.status };
      }
      const html = await resp.text();
      const text = npFlightFrom([html]);
      const raw = npObjectAfter(text, 'clipping');
      if (!raw || raw.clipping_id !== clippingId) return { ok: false, error: 'clipping page did not match' };
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const ocr = npOcrText(text, npClip(raw, absUrl), doc);
      return { ok: true, ocr };
    } catch (e) {
      return { ok: false, error: String(e) };
    }
  }

  async function newspapersInfo() {
    const where = npPageKind(location.href);
    if (!where) return { ok: false, error: 'this is not a Newspapers.com page or clipping' };
    let text = npFlightText();
    const describesThisRecord = (t) => {
      if (where.kind === 'article') {
        const c = npObjectAfter(t, 'clipping');
        return !!(c && c.clipping_id === where.clippingId);
      }
      const im = t.match(/"iat":"(eyJ[\w.-]+)"/);
      const pl = im ? npJwtPayload(im[1]) : null;
      return !!(pl && Number(pl.p) === where.pageId);
    };
    if (!describesThisRecord(text)) {
      try {
        const fresh = await npFreshFlightText();
        if (fresh && describesThisRecord(fresh)) text = fresh;
        else return { ok: false, error: 'the page data does not match this address - reload the page and try again' };
      } catch (e) {
        if (e && e.slowDown) return { ok: false, slowDown: true, error: e.message + ' - wait a few minutes, then reload it' };
        return { ok: false, error: 'could not re-read this page - reload it and try again' };
      }
    }

    // The page pass the tile service accepts: `page_token` on a clipping page,
    // `iat` on the page viewer. Both last a day from page load.
    const tokenMatch =
      text.match(/"page_token":"(eyJ[\w.-]+)"/) || text.match(/"iat":"(eyJ[\w.-]+)"/);
    const token = tokenMatch ? tokenMatch[1] : null;
    if (!token) {
      return {
        ok: false,
        error: 'Newspapers.com did not give this page an image pass - make sure you are signed in, then reload the page',
      };
    }

    // Who is signed in: the site's own `"user":{"id":…}` object, else the viewer
    // pass's `u`. Used to keep only the clippings YOU made.
    let me = { id: null, username: null };
    const um = text.match(/"user":\{"id":(\d+)/);
    if (um) {
      const obj = npObjectAt(text, um.index + '"user":'.length);
      me = { id: Number(um[1]), username: (obj && obj.username) || null };
    } else {
      const pl = npJwtPayload(token);
      if (pl && pl.u) me = { id: Number(pl.u), username: null };
    }

    const clips = [];
    let pageId = where.pageId;
    // The clipping this page's data describes. It is the first entry in
    // `clips`, and the same object is offered, so its OCR travels with it.
    const ownRaw = npObjectAfter(text, 'clipping');
    const focusClip = ownRaw && ownRaw.clipping_id ? npClip(ownRaw, absUrl) : null;
    if (focusClip) {
      clips.push(focusClip);
      pageId = pageId || focusClip.pageId;
    }
    if (!pageId) {
      return { ok: false, error: 'could not find which newspaper page this clipping is on - reload and try again' };
    }

    // Every member's clippings on this page: the same same-origin list the page
    // viewer requests when it loads. The API serves at most 25 per request, so
    // page through it - gently, and never more than a few pages.
    const NP_LIST_PAGE = 25;
    const NP_LIST_MAX_PAGES = 8;
    // Why the list could not be read, if it could not. An unread list is not
    // evidence that none of the clippings are yours (#211 review).
    let listError = null;
    let listTruncated = false;
    try {
      for (let pg = 0; pg < NP_LIST_MAX_PAGES; pg++) {
        if (pg) await new Promise((r) => setTimeout(r, 800 + Math.random() * 1200));
        const r = await fetch(
          '/api/clipping/page?page_id=' + encodeURIComponent(pageId) +
            '&start=' + pg * NP_LIST_PAGE + '&count=' + NP_LIST_PAGE,
          { credentials: 'include', headers: { accept: 'application/json' } }
        );
        if (!r.ok) {
          // A 401/403 is her session or her subscription, not the page: say
          // so, so she signs in (and a batch never counts it against a record).
          listError = r.status === 401 || r.status === 403
            ? 'Newspapers.com refused the list of clippings on this page (HTTP ' + r.status +
              ': you may need to sign in, or your subscription may not cover this paper)'
            : 'Newspapers.com would not send the list of clippings on this page (HTTP ' + r.status + ')';
          break;
        }
        const j = await r.json();
        const batch = (j && j.clippings) || [];
        for (const c of batch) {
          if (!clips.some((x) => x.id === c.clipping_id)) clips.push(npClip(c, absUrl));
        }
        if (batch.length < NP_LIST_PAGE) break;
        if (pg === NP_LIST_MAX_PAGES - 1) {
          // Stopped at the cap with more to come: hers may be among the rest
          // (#211 audit).
          listTruncated = true;
          listError = 'this page has more than ' + NP_LIST_MAX_PAGES * NP_LIST_PAGE +
            ' clippings, more than the panel reads';
        }
      }
    } catch (e) {
      // The page's own clipping still works; only the list is missing.
      listError = 'the list of clippings on this page could not be read';
    }

    // The page viewer describes the page in an `"image":{imageId, width, height,
    // date, title (the page number), publicationTitle, location}` object.
    let pageObj = null;
    for (let at = text.indexOf('"image":{'); at >= 0; at = text.indexOf('"image":{', at + 1)) {
      const o = npObjectAt(text, at + '"image":'.length);
      if (o && Number(o.imageId) === Number(pageId)) {
        pageObj = o;
        break;
      }
    }

    // Page size: from any clipping that carries it, else the page's own data.
    let pageWidth = pageObj && pageObj.width ? Number(pageObj.width) : null;
    let pageHeight = pageObj && pageObj.height ? Number(pageObj.height) : null;
    for (const c of clips) {
      if (pageWidth) break;
      if (c.pageWidth && c.pageHeight) {
        pageWidth = c.pageWidth;
        pageHeight = c.pageHeight;
        break;
      }
    }
    if (!pageWidth) {
      const wm = text.match(/"page_width":(\d+)/);
      const hm = text.match(/"page_height":(\d+)/);
      if (wm && hm) {
        pageWidth = Number(wm[1]);
        pageHeight = Number(hm[1]);
      }
    }
    if (!pageWidth || !pageHeight) {
      return { ok: false, error: 'could not read the newspaper page size from this page' };
    }

    const first = clips.find((c) => c.publication) || clips[0] || {};
    const pagePub = pageObj && pageObj.publicationTitle
      ? { name: pageObj.publicationTitle, location: pageObj.location || null }
      : null;
    // Which clippings to offer is npOffered's rule (owner decision): on a
    // clipping page, the one opened plus your own; on the viewer, your own.
    const opened = where.kind === 'article' ? focusClip : null;
    if (opened) {
      const live = npOcrText(text, opened, document);
      if (live) opened.ocrText = live;
    }
    const { offered, unusable, openedUnusable, otherClipCount, focusOwner } =
      npOffered(clips, opened, me);
    return {
      ok: true,
      kind: where.kind,
      focusClipId: where.clippingId || (focusClip && focusClip.id) || null,
      pageId,
      pageWidth,
      pageHeight,
      token,
      me,
      publication: first.publication || pagePub,
      pageDate: (pageObj && pageObj.date) || first.pageDate || null,
      pageNo: npPageNo(pageObj && pageObj.title) || first.pageNo || null,
      ownClips: offered,
      listError,
      listTruncated,
      unusable,
      openedUnusable,
      otherClipCount,
      focusOwner,
    };
  }

  // A record she moves to on a single-page site (FamilySearch, Ancestry) can
  // otherwise be titled, dated and pictured as the FIRST record of the visit:
  // those sites change the page without reloading it, and leave the head's
  // og:url / canonical (and the og:title, og:image and date beside them)
  // describing that first record. When either address names a different page
  // from the one in the bar, the head is stale and none of it is used. Pages
  // are compared by path, read the same whether written %3A or ":", ignoring
  // a two-letter language prefix (/en/…), a trailing slash and letter case;
  // and, for sites that key the record in the query (?itemId=2) or a hash
  // route (#/record/2), by any query value both addresses name and by the
  // tag's hash route. An address that cannot be read is no evidence of
  // staleness. (PR #223 audit F8.)
  function headIsStale(url) {
    const decode = (s) => {
      try {
        return decodeURIComponent(s);
      } catch (e) {
        return s;
      }
    };
    const path = (u) => decode(u.pathname).replace(/^\/[a-z]{2}(?=\/)/i, '').replace(/\/+$/, '').toLowerCase();
    const samePage = (a) => {
      let here;
      let tag;
      try {
        here = new URL(url);
        tag = new URL(a, url);
      } catch (e) {
        return true;
      }
      if (path(tag) !== path(here)) return false;
      // A value the tag and the address both give, but differently, names
      // another record; one only the address carries (tracking, a language)
      // says nothing.
      for (const [k, v] of tag.searchParams) {
        if (here.searchParams.has(k) && here.searchParams.get(k) !== v) return false;
      }
      if (/^#!?\//.test(tag.hash) && decode(tag.hash) !== decode(here.hash)) return false;
      return true;
    };
    const ogUrl = metaContent('meta[property="og:url"]', 'meta[name="og:url"]');
    const canon = (document.querySelector('link[rel="canonical"]') || {}).href;
    return !!((ogUrl && !samePage(ogUrl)) || (canon && !samePage(canon)));
  }

  function buildPrefill() {
    const url = location.href;
    const stale = headIsStale(url);
    const canonical = stale ? null : absUrl(
      (document.querySelector('link[rel="canonical"]') || {}).href
    );
    const title =
      (stale ? null : metaContent('meta[property="og:title"]', 'meta[name="og:title"]')) ||
      (document.title || '').trim() ||
      ((document.querySelector('h1') || {}).textContent || '').trim() ||
      null;
    const sourceDate = stale ? null : metaContent(
      'meta[property="article:published_time"]',
      'meta[name="article:published_time"]',
      'meta[name="date"]'
    );
    // On an Ancestry image-viewer page the auto full-res path replaces the
    // detectImage() guess (which would be the thumbnail - the EX7 trap), so the
    // panel knows not to pre-fill the asset URL with it.
    const ancestry = parseAncestryImageViewer(url);
    // A Find a Grave memorial: the form starts from what the memorial says -
    // its own title, date (its death) and people (it and its family).
    let memorial = null;
    try {
      memorial = fagMemorialPage(url) ? findagraveMemorialInfo() : null;
    } catch (e) {
      memorial = null; // the page's own title and people, as anywhere else
    }
    const fagRec = memorial && memorial.ok ? memorial.record : null;
    // A photo someone shared on Ancestry: Capture saves it as a photo.
    const ancPhoto = ancParseMediaPage(url);
    // Its title, date and the people it is linked to start the form - the
    // people as names she ticks or unticks, never added behind her back.
    let photo = null;
    try {
      photo = ancPhoto ? ancestryPhotoInfo() : null;
    } catch (e) {
      photo = null; // the page's own title, as anywhere else
    }
    const photoInfo = photo && photo.ok ? photo : null;
    // An Internet Archive book page: the book's own title, not the tab's
    // "… : Free Download, Borrow, and Streaming : Internet Archive".
    const iaPage = iaBookPage(url);
    return {
      url,
      canonical,
      title: fagRec ? memorial.title : photoInfo && photoInfo.media.title ? photoInfo.media.title
        : iaPage ? String(title || '').split(' : ')[0] || title : title,
      sourceDate: fagRec ? memorial.hints.sourceDate : photoInfo ? photoInfo.hints.sourceDate || '' : sourceDate,
      // A shared item is a photo unless its Ancestry category says it is a
      // document (ancMediaKind: an obituary, a certificate…).
      sourceType: fagRec ? memorial.hints.sourceType
        : iaPage ? 'book'
        : ancPhoto ? (photoInfo && photoInfo.hints.sourceType) || 'photo' : undefined,
      repository: fagRec ? 'Find a Grave' : iaPage ? 'Internet Archive' : hostnameOf(url) || null,
      people: fagRec ? [fagRec.name].concat(fagRec.family.map((f) => f.name)).filter(Boolean)
        .filter((n, i, a) => a.indexOf(n) === i)
        // A shared item's people come only from its own Details - never
        // harvested from its page, which names other members.
        : photoInfo ? (photoInfo.details.linkedTo || []).slice() : ancPhoto ? [] : harvestPeople(),
      // A Find a Grave memorial page (not its gallery): Capture saves the
      // memorial's own facts and citation and its photos at full size.
      findagraveMemorial: !!fagMemorialPage(url),
      // A page of an Internet Archive book: Capture saves the page image, its
      // OCR text, the book's details and a citation (the panel reads them).
      archiveorgPage: iaPage,
      // A West Virginia Archives view page: Capture saves its image with its numbers.
      wvcultureImagePage: !!wvParsePage(url),
      // A WikiTree profile or free-space page: Capture saves what WikiTree says,
      // the page's own text and its cited sources as a list (the panel reads them).
      wikitreePage: !!wtParsePage(url),
      imageUrl: detectImage(stale),
      pdfUrl: detectPdf(),
      recipeHint: recipeHint(url),
      ancestryImageViewer: !!ancestry,
      // An Ancestry record page: Capture saves the record's own fields and
      // citation (record-data.json) and fetches its linked scan, if it has one.
      ancestryRecord: !!ancParseRecordPage(url),
      // A Find a Grave Index entry: { memorialId, url } of the memorial it
      // indexes, which the panel offers to open instead.
      indexedMemorial: ancRecordMemorial(document, ancParseRecordPage(url)),
      // A photo someone shared to their Ancestry tree, in the media viewer:
      // Capture saves the photo the page shows, with its Details.
      ancestryPhoto: !!ancPhoto,
      ancestryMediaId: ancPhoto ? ancPhoto.mediaId : null,
      // Which of its Details the form was filled from (the panel compares at
      // Capture, so nothing she was not shown is staged behind her back).
      ancestryPhotoShown: photoInfo ? Object.keys(photoInfo.details) : null,
      // Ancestry's Details still describe the item shown before (#261 audit
      // B1): the panel says to wait, and nothing of them fills the form.
      ancestryPhotoNotReady: photo && !photo.ok && photo.transient ? photo.error : null,
      // A Newspapers.com clipping saved to the gallery: { clippingId, url } of
      // the original, which the panel offers to open instead.
      ancestryNewspapersClip: photoInfo && photoInfo.newspapersClip ? photoInfo.newspapersClip : null,
      // A FamilySearch record (or the image viewer opened from one): Capture
      // saves FamilySearch's own data for it and its image.
      familysearchRecord: !!fsParseRecordPage(url),
      // A FamilySearch image viewer of its own: the full image, from the API.
      // A bare film image (no record behind it): film, image number and
      // citation come with the picture.
      familysearchImagePage: !!fsParseImagePage(url),
      familysearchImageViewer: !fsParseRecordPage(url) && !!fsViewerImageId(url),
      familysearchImageId: fsParseRecordPage(url) ? null : fsViewerImageId(url),
      // A Newspapers.com page or clipping: the panel asks for newspapersInfo
      // and saves the full page plus your clippings on it automatically.
      newspapers: !!npPageKind(url),
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
    const page = document.documentElement.cloneNode(true);
    stripSeenIt(page);
    return doctype + page.outerHTML;
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

  // The hosts that said "slow down" (429/503) while the page copy fetched
  // its pictures and styles: the copy goes on without them, but the panel is
  // told, so a batch site's batch still takes its break (PR #224 Codex 4504665602).
  let singleFileSlowHosts = new Set();
  function noteSingleFileAnswer(url, status) {
    if (status !== 429 && status !== 503) return;
    try {
      singleFileSlowHosts.add(new URL(url).hostname);
    } catch (e) {
      /* no host to name */
    }
  }

  async function fetchAsDataUri(url) {
    const abs = absUrl(url);
    if (!abs) return null;
    try {
      const resp = await fetch(abs, { credentials: 'include' });
      if (!resp.ok) {
        noteSingleFileAnswer(abs, resp.status);
        return null;
      }
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
    singleFileSlowHosts = new Set();
    const clone = document.documentElement.cloneNode(true);
    stripSeenIt(clone);

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
        if (!resp.ok) {
          noteSingleFileAnswer(sheetUrl, resp.status);
          continue;
        }
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

  // A question whose request threw - the connection dropped, an answer that
  // broke off - is answered with that said the structured way: a cause that
  // passes (`transient`), the browser's own words in brackets. Never only the
  // browser's words, which no one downstream could tell from a refusal
  // (PR #224 Codex 4504508005).
  // Only a real network failure is a cause that passes: fetch's own
  // TypeError ("Failed to fetch", "NetworkError…", "Load failed"), an abort
  // or a time-out. Anything else thrown is a fault in the add-on while it
  // read this record: said as such, and the batch sets that one record aside
  // rather than stopping (PR #224 second audit N3).
  function brokeOff(e) {
    const msg = String((e && e.message) || e);
    const name = String((e && e.name) || '');
    const network = name === 'AbortError' || name === 'TimeoutError' || name === 'NetworkError' ||
      (name === 'TypeError' && /fetch|network|load failed/i.test(msg));
    if (network) {
      return { ok: false, transient: true, status: null,
        error: 'the connection broke off (' + msg + ') - press Capture again later' };
    }
    return { ok: false, codeError: true,
      error: 'the capture add-on could not read this record (' + (name ? name + ': ' : '') + msg +
        ') - a fault in the add-on, not the site' };
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
      case 'wvcultureImageInfo':
        sendResponse(wvcultureImageInfo(msg.today));
        return;
      case 'wikitreeInfo':
        wikitreeInfo(msg.today)
          .then(sendResponse)
          .catch((e) => sendResponse(brokeOff(e)));
        return true; // async response
      case 'familysearchRecordInfo':
        familysearchRecordInfo(msg.formTitle)
          .then(sendResponse)
          .catch((e) => sendResponse(brokeOff(e)));
        return true; // async response
      case 'familysearchImageInfo':
        familysearchImageInfo(msg.accessed)
          .then(sendResponse)
          .catch((e) => sendResponse(brokeOff(e)));
        return true; // async response
      case 'familysearchFullText':
        // Reads the page's Full-Text panel only; asks FamilySearch nothing.
        try {
          sendResponse(FS_HOST_RE.test(location.hostname)
            ? fsFullTextFrom(document, location.href, msg.imageId) : { ok: true, text: null });
        } catch (e) {
          sendResponse({ ok: true, text: null });
        }
        return;
      case 'familysearchImageCheck':
        familysearchImageCheck()
          .then(sendResponse)
          .catch((e) => sendResponse(brokeOff(e)));
        return true; // async response
      case 'familysearchImage': {
        if (!FS_HOST_RE.test(location.hostname)) {
          sendResponse({ ok: false, error: 'the tab is no longer on FamilySearch, so the image was not fetched - ' +
            'go back to the record and press Capture again' });
          return;
        }
        // An id named must be one: FamilySearch image ids are letters, digits
        // and dashes, and it is put straight into an address (PR #221 sweep).
        if (msg.imageId && !/^[A-Z0-9-]+$/i.test(String(msg.imageId))) {
          sendResponse({ ok: false, error: 'that is not a FamilySearch image, so nothing was fetched' });
          return;
        }
        // `fromUrl` is the file address that just failed (the panel's second
        // try): the image is the one THAT address names - an ark, a viewer
        // tile, a dist.jpg on FamilySearch - and nothing else. An address
        // elsewhere, or not of an image, is not swapped for whatever the
        // viewer shows now: her own address stands refused (PR #223 audit
        // F5/F7). With no id and no address: the image the viewer shows now
        // (a single-page app, so re-read at Capture rather than trust the
        // prefill). Either way the request goes to FamilySearch only, under
        // fsAuthHeaders' rule.
        let fsImageWanted = msg.imageId;
        if (!fsImageWanted && msg.fromUrl) {
          fsImageWanted = fsImageIdFromAddress(absUrl(msg.fromUrl) || '');
          if (!fsImageWanted) {
            sendResponse({ ok: false, error: 'that address is not of a FamilySearch image, so FamilySearch was ' +
              'not asked for it' });
            return;
          }
        }
        if (!fsImageWanted) fsImageWanted = fsViewerImageId();
        if (!fsImageWanted) {
          sendResponse({ ok: false, error: 'this page does not show one FamilySearch image the panel can save' });
          return;
        }
        fsImage(fsImageWanted, fsDeps(),
          fsImageDescCache && fsImageDescCache.id === String(fsImageWanted).toUpperCase() ? fsImageDescCache.desc : undefined)
          .then((r) => {
            fsImageDescCache = null;
            return r;
          })
          .then(sendResponse)
          .catch((e) => sendResponse(brokeOff(e)));
        return true; // async response
      }
      case 'ancestryRecordInfo':
        try {
          sendResponse(ancestryRecordInfo(msg.formTitle));
        } catch (e) {
          sendResponse({ ok: false, error: String(e) });
        }
        return;
      case 'ancestryPhotoInfo':
        try {
          sendResponse(ancestryPhotoInfo());
        } catch (e) {
          sendResponse({ ok: false, error: String(e) });
        }
        return;
      case 'ancestryPhoto':
        // Only from an Ancestry page, and only the photo it still shows.
        if (!/(^|\.)ancestry\.[a-z.]+$/i.test(location.hostname)) {
          sendResponse({ ok: false, error: 'the tab is no longer on Ancestry, so the photo was not fetched - ' +
            'go back to the photo and press Capture again' });
          return;
        }
        fetchAncestryPhoto(msg.mediaId)
          .then(sendResponse)
          .catch((e) => sendResponse(brokeOff(e)));
        return true; // async response
      case 'findagraveMemorialInfo':
        try {
          sendResponse(findagraveMemorialInfo(msg.today));
        } catch (e) {
          sendResponse({ ok: false, error: String(e) });
        }
        return;
      case 'findagraveGallery':
        // Only from a Find a Grave page, and only for the memorial it shows.
        if (!FAG_HOST_RE.test(location.hostname)) {
          sendResponse({ ok: false, error: 'the tab is no longer on Find a Grave, so the photos were not read - ' +
            'go back to the memorial and press Capture again' });
          return;
        }
        // The id must be a memorial's number. The slug is any slug Find a
        // Grave uses - apostrophes, accented letters - made one safe path
        // segment by fagSlugSegment (PR #221 audit), never refused.
        if (!/^\d+$/.test(String(msg.memorialId || ''))) {
          sendResponse({ ok: false, error: 'no memorial was named for the photos' });
          return;
        }
        findagraveGallery(String(msg.memorialId), msg.slug == null ? null : String(msg.slug))
          .then(sendResponse)
          .catch((e) => sendResponse(brokeOff(e)));
        return true; // async response
      case 'ancestryImage':
        // Full-res Ancestry record fetch for the current page, in-session.
        // Same result shape as fetchAsset so the panel handles both alike.
        if (!/(^|\.)ancestry\.[a-z.]+$/i.test(location.hostname)) {
          sendResponse({ ok: false, error: 'the tab is no longer on Ancestry, so the image was not fetched - ' +
            'go back to the record and press Capture again' });
          return;
        }
        fetchAncestryFullImage(msg.dbId && msg.imageId ? { dbId: msg.dbId, imageId: msg.imageId } : null)
          .then(sendResponse)
          .catch((e) => sendResponse(brokeOff(e)));
        return true; // async response
      case 'iiifImage':
        // Full-res IIIF image fetch (open archives) for the current page.
        fetchIiifFullImage()
          .then(sendResponse)
          .catch((e) => sendResponse({ ok: false, error: String(e) }));
        return true; // async response
      case 'newspapersClipText':
        npClipText(msg.url, msg.clippingId)
          .then(sendResponse)
          .catch((e) => sendResponse(brokeOff(e)));
        return true; // async response
      case 'newspapersInfo':
        newspapersInfo()
          .then(sendResponse)
          .catch((e) => sendResponse(brokeOff(e)));
        return true; // async response
      case 'newspapersList':
      case 'ancestryPersonSources':
      case 'ancestryPersonGallery':
      case 'familysearchPersonSources':
      case 'findagraveLinks':
        batchSwitchOn()
          .then((on) => (on ? batchSourcesRoute(msg)
            : { ok: false, off: true, error: 'batch downloads are switched off in this browser' }))
          .then(sendResponse)
          .catch((e) => sendResponse(brokeOff(e)));
        return true; // async response
      case 'singlefile':
        buildSingleFile()
          .then((html) => sendResponse({ ok: true, html, slowHosts: [...singleFileSlowHosts] }))
          .catch((e) => sendResponse({ ok: false, error: String(e) }));
        return true; // async response
      default:
        return;
    }
  });
})();
