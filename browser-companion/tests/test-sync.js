// Sync guards for the companion's documented keep-in-sync pairs.
//
// content.js is an injected classic script that cannot import, so it carries
// COPIES of logic whose canonical, tested home is src/lib/ - and the browser
// capture-json.js has a pure CommonJS twin the node tests exercise. Until now
// those pairs were held together by comments alone ("keep in sync"); these
// tests make a drift a failing build instead of a hope.
//
//   node --test browser-companion/tests/test-sync.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const readSrc = (rel) =>
  fs.readFileSync(path.join(__dirname, '..', 'src', rel), 'utf8');

const pure = require('../src/lib/capture-json-pure.js');
const iiif = require('../src/lib/iiif.js');
const harvest = require('../src/lib/people-harvest.js');
const readiness = require('../src/lib/capture-readiness.js');
const srcset = require('../src/lib/srcset.js');

// ── capture-json.js ↔ capture-json-pure.js (functional equivalence) ──────────
// The browser file is an IIFE that attaches to window.FHA; run it against a
// stub window and drive both implementations through the same battery.

function loadBrowserCaptureJson() {
  const w = {};
  new Function('window', readSrc('lib/capture-json.js'))(w);
  return w.FHA.captureJson;
}

test('capture-json.js behaves identically to its pure twin', () => {
  const b = loadBrowserCaptureJson();
  assert.strictEqual(b.SCHEMA, pure.CAPTURE_JSON_SCHEMA);
  assert.strictEqual(b.DEFAULT_FOLDER, pure.DEFAULT_FOLDER);

  const when = new Date(2026, 0, 2, 3, 4, 5, 6);
  assert.strictEqual(b.timestamp(when), pure.timestamp(when));
  assert.strictEqual(b.accessedDate(when), pure.accessedDate(when));

  for (const s of ['1880 Census - Thomas!', '', null, '  A  B  ', 'Émile Zola']) {
    assert.strictEqual(b.slugify(s), pure.slugify(s), 'slugify(' + s + ')');
    assert.strictEqual(b.bundleName(s, when), pure.bundleName(s, when));
  }

  const dirs = [
    '', null, '/Users/me/Downloads/fha-inbox',
    'C:\\Users\\me\\OneDrive\\Downloads\\fha-inbox', '/home/me/my "downloads"',
  ];
  for (const f of ['fha-inbox', 'my captures', '../x', 'a\\b', '/abs/', '', null]) {
    assert.strictEqual(b.sanitizeFolder(f), pure.sanitizeFolder(f),
      'sanitizeFolder(' + f + ')');
    for (const d of dirs) {
      assert.strictEqual(b.ingestCommand(d), pure.ingestCommand(d),
        'ingestCommand(' + d + ')');
      assert.strictEqual(b.ingestHint(f, d), pure.ingestHint(f, d),
        'ingestHint(' + f + ', ' + d + ')');
    }
  }
  for (const p of ['/Users/me/Downloads/fha-inbox/c-1/page.html',
                   'C:\\dl\\fha-inbox\\c-1\\page.html', 'page.html', '', null]) {
    assert.deepStrictEqual(b.stagedPaths(p), pure.stagedPaths(p),
      'stagedPaths(' + p + ')');
  }

  const fields = {
    url: 'https://example.com/r/1',
    title: 'T',
    accessed: '2026-07-27',
    sourceDate: '1880',
    sourceType: 'census',
    repository: '  Ancestry.com  ',
    people: [' Thomas ', '', null, 'Margaret'],
    notes: 'a note',
    recipeHint: 'ancestry',
    assets: [
      { file: 'record.jpg', role: 'record', mode: 'fetch', provisional: false },
      { file: 'page-snapshot.html', role: 'webpage', mode: 'singlefile', provisional: true },
      { file: '', role: 'record' },
      null,
    ],
  };
  assert.deepStrictEqual(b.build(fields), pure.build(fields));
  assert.deepStrictEqual(
    b.build({ url: 'u', assets: [] }),
    pure.build({ url: 'u', assets: [] })
  );
});

// ── content.js copies ↔ lib canonical modules (literal extraction) ───────────
// Pull each duplicated literal out of content.js's source text and evaluate it
// in isolation (the file itself cannot be require()d - it registers a
// chrome.runtime listener at load). A marker that stops matching means the
// copy moved or was renamed: point the extractor at the new shape rather than
// deleting the guard.

const content = readSrc('content.js');

function extractBlock(startRe, endStr) {
  const m = startRe.exec(content);
  assert.ok(m, 'marker not found in content.js: ' + startRe);
  const from = m.index + m[0].length;
  const to = content.indexOf(endStr, from);
  assert.ok(to > from, 'end marker not found for: ' + startRe);
  return content.slice(from, to);
}

test('content.js EMPTY_DETAIL_PHRASES matches lib/capture-readiness.js', () => {
  const inner = extractBlock(/const EMPTY_DETAIL_PHRASES = \[/, '];');
  const copy = new Function('return [' + inner + ']')();
  assert.deepStrictEqual(copy, readiness.EMPTY_DETAIL_PHRASES);
});

test('content.js NON_PERSON_TYPES matches lib/people-harvest.js', () => {
  const inner = extractBlock(/const NON_PERSON_TYPES = new Set\(\[/, ']);');
  const copy = new Function('return [' + inner + ']')();
  assert.deepStrictEqual(
    [...copy].sort(),
    [...harvest.NON_PERSON_TYPES].sort()
  );
});

test('content.js IIIF_IMAGE_RE matches lib/iiif.js', () => {
  const inner = extractBlock(/const IIIF_IMAGE_RE = new RegExp\(/, '\n  );');
  const copy = new Function('return new RegExp(' + inner + ')')();
  assert.strictEqual(copy.source, iiif.IIIF_IMAGE_RE.source);
  assert.strictEqual(copy.flags, iiif.IIIF_IMAGE_RE.flags);
});

test('content.js srcset parser matches lib/srcset.js', () => {
  // Not a literal comparison but a behavioural one: the copy is a block of
  // function declarations between explicit sync markers, so it can be evaluated
  // on its own and driven through the same battery as the canonical module. A
  // drift in either direction fails here rather than in a saved snapshot months
  // later, which is the only place it would otherwise show up.
  const block = extractBlock(/\/\/ FHA-SYNC-BEGIN srcset\n/, '// FHA-SYNC-END srcset');
  const copy = new Function(
    block + '\nreturn { parseSrcset, serializeSrcset, rewriteSrcset, srcsetUrls };'
  )();

  const battery = [
    'small.png 1x, large.png 2x',
    'photo.jpg',
    'data:image/svg+xml;utf8,%3Csvg%20viewBox%3D%220,0,10,10%22%3E 1x, real.png 2x',
    'data:image/gif;base64,R0lGODlhAQABAAAAACw= 1x',
    'https://cdn.example.org/c_fill,w_600/scan.jpg 600w, https://cdn.example.org/c_fill,w_1200/scan.jpg 1200w',
    'a.png,b.png 2x',
    'a.png, b.png 2x',
    'a.png 100w (min-width:10px,max-width:20px), b.png',
    '\n  a.png   1x ,\t\n b.png\t2x\n',
    '',
    ' , , ',
    null,
  ];
  for (const value of battery) {
    assert.deepStrictEqual(copy.parseSrcset(value), srcset.parseSrcset(value),
      'parseSrcset(' + JSON.stringify(value) + ')');
    assert.deepStrictEqual(copy.srcsetUrls(value), srcset.srcsetUrls(value),
      'srcsetUrls(' + JSON.stringify(value) + ')');
    const abs = (u) => (u.startsWith('data:') ? null : 'https://site.test/' + u);
    assert.strictEqual(copy.rewriteSrcset(value, abs),
      srcset.rewriteSrcset(value, abs),
      'rewriteSrcset(' + JSON.stringify(value) + ')');
  }
  assert.strictEqual(
    copy.serializeSrcset([{ url: 'a.png', descriptor: '1x' }, { url: '', descriptor: '' }]),
    srcset.serializeSrcset([{ url: 'a.png', descriptor: '1x' }, { url: '', descriptor: '' }])
  );
});

test('content.js rewrites to the same IIIF full-image candidates as the lib', () => {
  // The candidate suffixes are two string literals; their presence pins the
  // content.js copy of iiifFullImageCandidates to the lib's outputs.
  assert.ok(content.includes("'/full/full/0/default.jpg'"));
  assert.ok(content.includes("'/full/max/0/default.jpg'"));
  const seed = 'https://tile.example.org/iiif/id123/full/pct:25/0/default.jpg';
  assert.deepStrictEqual(iiif.iiifFullImageCandidates(seed), [
    'https://tile.example.org/iiif/id123/full/full/0/default.jpg',
    'https://tile.example.org/iiif/id123/full/max/0/default.jpg',
  ]);
});
