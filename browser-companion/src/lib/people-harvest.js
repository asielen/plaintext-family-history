// people-harvest.js — the pure, importable core of content.js harvestPeople().
//
// This module exists so the harvest-guard logic (NON_PERSON_TYPES, isNonPersonLdNode,
// walkLd) can be unit-tested under `node --test` without any browser globals.  The
// browser-side content.js DOES NOT import this file (it is an injected classic
// script that cannot use ES imports).  Instead, the logic here is the *canonical
// reference* that was extracted from content.js and must be kept in sync with the
// copy there.  Tests run against this module; the content.js copy is verified by
// reading the same logic from the two places.
//
// API:
//   isNonPersonLdNode(node)  → boolean
//   walkLdForPeople(graph)   → string[]   (all harvested names from one LD blob)
//   harvestFromLd(scripts)   → string[]   (full list, deduplicated, capped at 12)
//   NON_PERSON_TYPES         → Set<string>

'use strict';

// Types that must never yield a name in the people harvest, regardless of
// whether they also carry a `name` property.
const NON_PERSON_TYPES = new Set([
  'Place', 'LocalBusiness', 'Organization', 'Cemetery',
  'LandmarksOrHistoricalBuildings', 'TouristAttraction',
  'BreadcrumbList', 'ListItem',
  'City', 'Country', 'State', 'AdministrativeArea',
  'CivicStructure', 'PlaceOfWorship', 'Museum', 'Park',
  'Hospital', 'School', 'CollegeOrUniversity',
]);

/**
 * Returns true when a JSON-LD node's @type indicates a non-person entity, or
 * when the node carries structural place markers (address/geo).
 *
 * @param {object} node  A single JSON-LD node object.
 * @returns {boolean}
 */
function isNonPersonLdNode(node) {
  if (!node || typeof node !== 'object' || Array.isArray(node)) return false;
  const type = node['@type'];
  const types = Array.isArray(type) ? type : (type ? [type] : []);
  // An explicit Person is a person even when it carries an address/geo (schema.org
  // Person inherits `address` from Thing, and obituary/genealogy pages routinely
  // attach a residence to the deceased) — never let the place heuristic drop it.
  if (types.includes('Person')) return false;
  if (types.some((t) => NON_PERSON_TYPES.has(t))) return true;
  if (node.address || node.geo) return true;
  return false;
}

/**
 * Walk a single JSON-LD graph node, collecting person names while pruning
 * non-person subtrees.
 *
 * @param {*}        node  Any JSON-LD value.
 * @param {Set}      seen  Dedup key set (mutated in place).
 * @param {string[]} out   Accumulator (mutated in place).
 */
function walkLd(node, seen, out) {
  if (!node || typeof node !== 'object') return;
  if (Array.isArray(node)) {
    node.forEach((child) => walkLd(child, seen, out));
    return;
  }
  // Skip the entire subtree for non-person entity types so their nested
  // `name` props don't leak into the people list.
  if (isNonPersonLdNode(node)) return;

  const type = node['@type'];
  const isPerson = type === 'Person' ||
    (Array.isArray(type) && type.includes('Person'));
  if (isPerson) {
    const raw = typeof node.name === 'string'
      ? node.name
      : (node.givenName || node.familyName)
        ? [node.givenName, node.familyName].filter(Boolean).join(' ')
        : null;
    if (raw) {
      const name = raw.trim().replace(/\s+/g, ' ');
      if (name.length >= 2 && name.length <= 80) {
        const key = name.toLowerCase();
        if (!seen.has(key)) {
          seen.add(key);
          out.push(name);
        }
      }
    }
  }

  for (const key of Object.keys(node)) {
    if (key === '@context') continue;
    walkLd(node[key], seen, out);
  }
}

/**
 * Harvest person names from an array of raw JSON-LD text strings (the
 * `textContent` of `<script type="application/ld+json">` elements).
 *
 * Silently skips invalid JSON (malformed LD is common in the wild).
 *
 * @param {string[]} scriptTexts
 * @returns {string[]}  Deduplicated, capped at 12.
 */
function harvestFromLd(scriptTexts) {
  const seen = new Set();
  const out = [];
  for (const text of scriptTexts) {
    try {
      walkLd(JSON.parse(text), seen, out);
    } catch (e) {
      // malformed JSON-LD — skip silently
    }
  }
  return out.slice(0, 12);
}

module.exports = { NON_PERSON_TYPES, isNonPersonLdNode, harvestFromLd };
