// Tests for the pure capture.json builder and its bundle/command helpers.
// Built-in node:test + node:assert only - no deps, no browser.
//   node --test browser-companion/tests/test-capture-json.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const {
  CAPTURE_JSON_SCHEMA,
  DEFAULT_FOLDER,
  slugify,
  timestamp,
  bundleName,
  build,
  sanitizeFolder,
  ingestCommand,
} = require('../src/lib/capture-json-pure.js');

// A fixed local time so timestamp assertions are deterministic.
const WHEN = new Date(2026, 6, 27, 10, 15, 0, 7); // 2026-07-27 10:15:00.007

test('timestamp carries milliseconds so same-second captures cannot share a folder', () => {
  assert.strictEqual(timestamp(WHEN), '20260727-101500-007');
  const plus1ms = new Date(WHEN.getTime() + 1);
  assert.notStrictEqual(timestamp(WHEN), timestamp(plus1ms));
});

test('bundleName is slug + timestamp', () => {
  assert.strictEqual(
    bundleName('1880 Census - Thomas Hartley!', WHEN),
    '1880-census-thomas-hartley-20260727-101500-007'
  );
  assert.strictEqual(bundleName('', WHEN), 'capture-20260727-101500-007');
});

test('build emits an EMPTY assets list for the pointer-only capture', () => {
  // Page-copy off + "No, the page copy is the record" (TOOLING_INGESTION
  // §5.3's "none"): ingest files this as a pointer stub (asset_elsewhere).
  const cap = build({ url: 'https://example.gov/deeds/1854', assets: [] });
  assert.strictEqual(cap.schema, CAPTURE_JSON_SCHEMA);
  assert.deepStrictEqual(cap.assets, []);
  assert.ok(Object.prototype.hasOwnProperty.call(cap, 'assets'),
    'assets must be PRESENT and empty - "no assets" and "field missing" differ');
});

test('build normalizes asset entries and omits a false provisional flag', () => {
  const cap = build({
    url: 'u',
    assets: [
      { file: 'record.jpg', role: 'record', mode: 'fetch', provisional: false },
      { file: 'page-snapshot.html', role: 'webpage', mode: 'singlefile', provisional: true },
      { file: '', role: 'record' },   // no file -> dropped
      null,                            // junk -> dropped
    ],
  });
  assert.deepStrictEqual(cap.assets, [
    { file: 'record.jpg', role: 'record', mode: 'fetch' },
    { file: 'page-snapshot.html', role: 'webpage', mode: 'singlefile', provisional: true },
  ]);
});

test('sanitizeFolder confines the setting to a Downloads-relative subpath', () => {
  assert.strictEqual(sanitizeFolder('fha-inbox'), 'fha-inbox');
  assert.strictEqual(sanitizeFolder(' my captures '), 'my captures');
  assert.strictEqual(sanitizeFolder('genealogy/staging'), 'genealogy/staging');
  assert.strictEqual(sanitizeFolder('..\\..\\evil'), 'evil');
  assert.strictEqual(sanitizeFolder('/abs/path/'), 'abs/path');
  assert.strictEqual(sanitizeFolder('a/./b'), 'a/b');
  // Nothing left after cleaning -> the default, never Downloads' root.
  assert.strictEqual(sanitizeFolder('..'), DEFAULT_FOLDER);
  assert.strictEqual(sanitizeFolder(''), DEFAULT_FOLDER);
  assert.strictEqual(sanitizeFolder(null), DEFAULT_FOLDER);
});

test('ingestCommand names the custom staging folder, and only then', () => {
  // The bare command sweeps the default folder - correct only when the
  // setting still points there.
  assert.strictEqual(ingestCommand(DEFAULT_FOLDER), 'fha capture --ingest');
  assert.strictEqual(ingestCommand(''), 'fha capture --ingest');
  // A renamed folder MUST surface as the DIR argument (the Python side
  // `~`-expands it on every OS), or the copied command finds nothing.
  assert.strictEqual(
    ingestCommand('my-captures'),
    'fha capture --ingest "~/Downloads/my-captures"'
  );
  assert.strictEqual(
    ingestCommand('genealogy/staging'),
    'fha capture --ingest "~/Downloads/genealogy/staging"'
  );
  // Escape attempts collapse to the sanitized folder first.
  assert.strictEqual(
    ingestCommand('../x'),
    'fha capture --ingest "~/Downloads/x"'
  );
});

test('slugify stays in step with capture.py _slugify', () => {
  assert.strictEqual(slugify('1880 U.S. Census - Thomas!'), '1880-u-s-census-thomas');
  assert.strictEqual(slugify('   '), 'capture');
  assert.strictEqual(slugify(null), 'capture');
});
