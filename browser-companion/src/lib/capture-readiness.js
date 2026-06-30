// capture-readiness.js — the pure core of the capture-timing guard
// (capture-frontend-08 item A).
//
// The content script serializes the LIVE DOM — only what is loaded and expanded
// at Capture time. A capture taken before the record detail is open hands the
// recipe nothing to extract, silently (EX20's page.html read "No record has
// been selected"). This is the worst kind of failure: the bundle looks fine.
//
// This module is the pure, node-tested signal: does the visible text look like
// an empty/unselected detail panel? The browser-side content.js keeps a synced
// copy and pairs it with a DOM element-count check; the panel surfaces the
// warning. Generic heuristic, NOT per-site.

'use strict';

// Phrases a record page shows when no detail is loaded/selected yet. Lowercased
// substring checks, so surrounding markup/casing doesn't matter.
const EMPTY_DETAIL_PHRASES = [
  'no record has been selected',
  'no record selected',
  'no record is selected',
  'select a record to',
  'no results to display',
  'no details to show',
];

/** True when `text` reads like an empty/unselected record-detail region. */
function detailLooksUnpopulated(text) {
  const t = String(text || '').toLowerCase();
  return EMPTY_DETAIL_PHRASES.some((p) => t.includes(p));
}

module.exports = { EMPTY_DETAIL_PHRASES, detailLooksUnpopulated };
