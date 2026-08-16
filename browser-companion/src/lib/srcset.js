// srcset.js - the pure, importable srcset parser the capture snapshot relies on.
//
// WHY THIS FILE EXISTS
// --------------------
// The single-file snapshot (content.js, case (b) of TOOLING_INGESTION §5.6) has
// to rewrite every relative URL in the cloned page to its live absolute form,
// or the saved copy resolves those references against the local folder once it
// is opened from file:// and every image and link in it is dead.
//
// `srcset` is the one URL attribute that cannot be handled with a split. It
// looks like a comma-separated list, but a comma is a perfectly legal character
// INSIDE a candidate URL - `data:image/svg+xml,<svg …>` carries one by
// construction, and parameterized CDN URLs (`…/resize,w_600/photo.jpg`) carry
// them routinely. Splitting on commas therefore chops one URL into several
// fragments and rewrites each fragment as if it were a path of its own, which
// silently corrupts the attribute: the saved `<source srcset>` and any image the
// inliner's budget did not reach end up pointing at nothing.
//
// So the parsing here follows the HTML Standard's "parse a srcset attribute"
// algorithm, whose whole trick is that a URL run ends at WHITESPACE, never at a
// comma. A comma only separates candidates when it trails the URL run, or when
// it terminates the descriptor list that follows the whitespace.
//
// The browser-side content.js DOES NOT import this file (it is an injected
// classic script that cannot use ES imports); the logic here is the canonical
// reference that the content.js copy is kept in sync with, and the node tests
// (tests/test-srcset.js, tests/test-sync.js) run against this module.

'use strict';

// The five characters the HTML Standard calls ASCII whitespace. Deliberately
// not \s: \s also matches NBSP and other Unicode spaces, which the spec treats
// as ordinary URL characters, so using it here would truncate a URL that legally
// contains one.
const SRCSET_WHITESPACE = '\t\n\f\r ';

function isSrcsetWhitespace(ch) {
  return SRCSET_WHITESPACE.indexOf(ch) !== -1;
}

/**
 * Parse a `srcset` (or `imagesrcset`) attribute into its candidates.
 *
 * Returns `[{ url, descriptor }]` in source order, where `descriptor` is the
 * candidate's width/density/type descriptors re-joined with single spaces (an
 * empty string when the candidate had none). Candidates with an empty URL are
 * dropped, matching the spec's "if url is empty, continue".
 *
 * The algorithm is the HTML Standard's, kept in the same three moves so it can
 * be checked against the spec text:
 *
 *   1. skip any run of whitespace and commas (the candidate separator),
 *   2. take the URL as everything up to the next whitespace; if that run ends
 *      in one or more commas, strip them and the candidate has no descriptors,
 *   3. otherwise tokenize descriptors, which end at a comma - but a comma
 *      inside parentheses belongs to the descriptor (the spec keeps that door
 *      open for future media-query-ish descriptors), so parens are tracked.
 */
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

/**
 * Render candidates back into an attribute value.
 *
 * Joined with ", " rather than "," because a candidate URL may itself end in a
 * character a following comma would glue onto; the space keeps the output
 * re-parseable by this same parser and by every browser.
 */
function serializeSrcset(candidates) {
  return (candidates || [])
    .filter(function (c) { return c && c.url; })
    .map(function (c) { return c.descriptor ? c.url + ' ' + c.descriptor : c.url; })
    .join(', ');
}

/**
 * Rewrite every candidate URL through `rewrite`, preserving descriptors.
 *
 * `rewrite` returns the replacement URL, or a falsy value to leave the original
 * in place - which is how the snapshot keeps `data:` and fragment URLs untouched
 * while absolutizing the rest. Returns the new attribute value.
 */
function rewriteSrcset(value, rewrite) {
  const candidates = parseSrcset(value).map(function (c) {
    const replaced = rewrite(c.url);
    return replaced ? { url: replaced, descriptor: c.descriptor } : c;
  });
  return serializeSrcset(candidates);
}

/** Just the URLs, in source order - what the IIIF detector wants. */
function srcsetUrls(value) {
  return parseSrcset(value).map(function (c) { return c.url; });
}

module.exports = {
  parseSrcset,
  serializeSrcset,
  rewriteSrcset,
  srcsetUrls,
};
