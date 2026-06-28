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
    document
      .querySelectorAll('[itemtype$="schema.org/Person"] [itemprop="name"]')
      .forEach((el) => add(el.textContent));
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
