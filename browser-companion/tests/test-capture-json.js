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
  ingestHint,
  stagedPaths,
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
  // Characters the downloads API rejects, or that would break the copied
  // shell command inside double quotes, are dropped at typing time - the
  // setting is self-correcting, not a raw 'Invalid filename' at capture time.
  assert.strictEqual(sanitizeFolder('C:\\Users\\me\\Downloads\\inbox'), 'C/Users/me/Downloads/inbox');
  assert.strictEqual(sanitizeFolder('inbox: 2026'), 'inbox 2026');
  assert.strictEqual(sanitizeFolder('photos?'), 'photos');
  assert.strictEqual(sanitizeFolder('inbox.'), 'inbox');
  assert.strictEqual(sanitizeFolder('Family $Photos'), 'Family Photos');
  assert.strictEqual(sanitizeFolder('my "quoted" inbox'), 'my quoted inbox');
  // Nothing left after cleaning -> the default, never Downloads' root.
  assert.strictEqual(sanitizeFolder('..'), DEFAULT_FOLDER);
  assert.strictEqual(sanitizeFolder(''), DEFAULT_FOLDER);
  assert.strictEqual(sanitizeFolder(null), DEFAULT_FOLDER);
});

test('stagedPaths reads the real location out of the download path', () => {
  // The browser reports the absolute path it wrote. The bundle folder is its
  // parent; the folder --ingest must sweep is the one above that, whatever the
  // download directory turned out to be and however many segments the staging
  // folder setting has.
  assert.deepStrictEqual(
    stagedPaths('/Users/me/Downloads/fha-inbox/census-20260727-101500-007/page.html'),
    {
      bundle: '/Users/me/Downloads/fha-inbox/census-20260727-101500-007',
      staging: '/Users/me/Downloads/fha-inbox',
    }
  );
  // Moved to OneDrive - the case a synthesized ~/Downloads path got wrong.
  assert.deepStrictEqual(
    stagedPaths('C:\\Users\\me\\OneDrive\\Downloads\\fha-inbox\\c-1\\page.html'),
    {
      bundle: 'C:/Users/me/OneDrive/Downloads/fha-inbox/c-1',
      staging: 'C:/Users/me/OneDrive/Downloads/fha-inbox',
    }
  );
  // A nested staging folder still yields the folder that HOLDS the bundles.
  assert.strictEqual(
    stagedPaths('/vol/dl/genealogy/staging/c-1/page.html').staging,
    '/vol/dl/genealogy/staging'
  );
  // Nothing to go on - never a guess.
  for (const unknown of ['', null, undefined, 'page.html', 'C:/page.html']) {
    assert.deepStrictEqual(stagedPaths(unknown), { bundle: '', staging: '' },
      'stagedPaths(' + unknown + ')');
  }
});

test('ingestCommand asserts a location only when the browser reported one', () => {
  // With no reported path (the card is pre-filled before the first capture)
  // the bare command stands: the Python side resolves `capture_staging:` from
  // fha.yaml, else its own default, and says which folder it looked in. What
  // it must never do is synthesize `~/Downloads/<folder>` - the download
  // directory is a browser setting, and a guess that reads as fact sends the
  // sweep to a folder the bundle was never written to.
  assert.strictEqual(ingestCommand(''), 'fha capture --ingest');
  assert.strictEqual(ingestCommand(null), 'fha capture --ingest');
  assert.strictEqual(ingestCommand(undefined), 'fha capture --ingest');
  assert.strictEqual(
    ingestCommand('/Users/me/Downloads/fha-inbox'),
    'fha capture --ingest "/Users/me/Downloads/fha-inbox"'
  );
  assert.strictEqual(
    ingestCommand('D:/Family/captures'),
    'fha capture --ingest "D:/Family/captures"'
  );
  // A path that cannot survive being pasted between double quotes is not
  // pasted between double quotes; the hint carries it as plain text instead.
  for (const hostile of ['/home/me/my "downloads"', '/home/me/$HOME dl', '/home/me/`x`']) {
    assert.strictEqual(ingestCommand(hostile), 'fha capture --ingest', hostile);
  }
});

test('ingestHint names a location the command could not carry', () => {
  // Silent when the command already tells the whole truth.
  assert.strictEqual(ingestHint(DEFAULT_FOLDER, '/Users/me/Downloads/fha-inbox'), '');
  assert.strictEqual(ingestHint(DEFAULT_FOLDER, ''), '');
  // A renamed folder with no reported path: name the folder, and point at the
  // browser's own download setting and fha.yaml's capture_staging: - never at
  // an invented home-directory path.
  const renamed = ingestHint('my-captures', '');
  assert.ok(renamed.includes('my-captures'), renamed);
  assert.ok(renamed.includes('capture_staging'), renamed);
  assert.ok(!renamed.includes('~/Downloads'), renamed);
  // An unquotable real path is still reported, as text rather than as command.
  const hostile = ingestHint(DEFAULT_FOLDER, '/home/me/my "downloads"/fha-inbox');
  assert.ok(hostile.includes('/home/me/my "downloads"/fha-inbox'), hostile);
  assert.ok(hostile.includes('capture_staging'), hostile);
});

test('slugify stays in step with capture.py _slugify', () => {
  assert.strictEqual(slugify('1880 U.S. Census - Thomas!'), '1880-u-s-census-thomas');
  assert.strictEqual(slugify('   '), 'capture');
  assert.strictEqual(slugify(null), 'capture');
});
