// iiif.js — the pure, importable core of the IIIF full-image auto-fetch
// (capture-frontend-07 Workstream B).
//
// IIIF is an OPEN STANDARD, so a IIIF image fetcher is *generic asset
// acquisition*, not per-site logic — it does not break the "browser stays
// generic" line (unlike the Ancestry/FamilySearch Option A, which is per-site).
//
// The browser-side content.js DOES NOT import this file (it is an injected
// classic script that cannot use ES imports); the logic here is the canonical
// reference that the content.js copy must be kept in sync with. Tests run
// against this module.
//
// A IIIF Image-API URL is:
//   {scheme}://{server}/{prefix}/{identifier}/{region}/{size}/{rotation}/{quality}.{format}
// The full-res image is region `full`, size `full` (IIIF 2.x) or `max` (3.x),
// rotation `0`, quality `default`, format `jpg`. We get there by rewriting the
// trailing `/{region}/{size}/{rotation}/{quality}.{format}` off any IIIF URL
// already present in the DOM — no manifest/info.json round-trip needed for v1.

'use strict';

// Anchored on IIIF-specific region/rotation/quality/format shapes so a plain
// `/a/b/c/d/e.jpg` path does NOT masquerade as a IIIF image URL.
const IIIF_IMAGE_RE = new RegExp(
  '^(.+?)' +                                              // 1: {base}/{identifier}
  '/(full|square|\\d+,\\d+,\\d+,\\d+|pct:[\\d.]+,[\\d.]+,[\\d.]+,[\\d.]+)' +  // region
  '/([^/]+)' +                                            // size
  '/(!?[\\d.]+)' +                                        // rotation
  '/(default|color|colour|gray|grey|bitonal|native)' +   // quality
  '\\.(jpe?g|tiff?|png|gif|jp2|webp|pdf)$',               // format
  'i'
);

/** True when `url` is a IIIF Image-API request URL. */
function isIiifImageUrl(url) {
  return IIIF_IMAGE_RE.test(String(url || ''));
}

/**
 * Full-image candidate URLs for a IIIF image URL, most-compatible first:
 * `full/full` (IIIF 2.x) then `full/max` (3.x). `[]` for a non-IIIF URL.
 * `full/full` still yields the largest the server allows when it caps size.
 */
function iiifFullImageCandidates(url) {
  const m = IIIF_IMAGE_RE.exec(String(url || ''));
  if (!m) return [];
  const base = m[1];
  return [base + '/full/full/0/default.jpg', base + '/full/max/0/default.jpg'];
}

/** The first IIIF image URL found in a list of candidate URLs (or null). */
function findIiifImageUrl(urls) {
  for (const u of urls || []) {
    if (isIiifImageUrl(u)) return u;
  }
  return null;
}

module.exports = {
  IIIF_IMAGE_RE,
  isIiifImageUrl,
  iiifFullImageCandidates,
  findIiifImageUrl,
};
