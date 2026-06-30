// Tests for the pure IIIF helper (capture-frontend-07 Workstream B).
// Built-in node:test + node:assert only — no deps, no browser.
//   node --test browser-companion/tests/test-iiif.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const {
  isIiifImageUrl,
  iiifFullImageCandidates,
  findIiifImageUrl,
} = require('../src/lib/iiif.js');

// A real loc.gov IIIF URL (EX17 shape): region full, size pct:25.
const LOC = 'https://tile.loc.gov/image-services/iiif/service:gmd:gmd432:g4364:ar131300/full/pct:25/0/default.jpg';
const LOC_BASE = 'https://tile.loc.gov/image-services/iiif/service:gmd:gmd432:g4364:ar131300';

test('recognizes a IIIF image URL', () => {
  assert.ok(isIiifImageUrl(LOC));
  assert.ok(isIiifImageUrl(LOC_BASE + '/0,0,512,512/512,/90/gray.png'));
  assert.ok(isIiifImageUrl(LOC_BASE + '/full/max/0/default.jpg'));
});

test('rejects non-IIIF URLs that merely have slashes and an extension', () => {
  assert.ok(!isIiifImageUrl('https://example.com/a/b/c/d/photo.jpg'));
  assert.ok(!isIiifImageUrl('https://example.com/full/thumb.jpg'));
  assert.ok(!isIiifImageUrl('https://www.newspapers.com/img/img?iat=JWT'));
  assert.ok(!isIiifImageUrl(''));
  assert.ok(!isIiifImageUrl(null));
});

test('rewrites the suffix to full/full then full/max candidates', () => {
  assert.deepStrictEqual(iiifFullImageCandidates(LOC), [
    LOC_BASE + '/full/full/0/default.jpg',
    LOC_BASE + '/full/max/0/default.jpg',
  ]);
});

test('a non-IIIF URL yields no candidates', () => {
  assert.deepStrictEqual(iiifFullImageCandidates('https://example.com/x.jpg'), []);
});

test('findIiifImageUrl returns the first IIIF-shaped URL', () => {
  const urls = [
    'https://example.com/logo.png',
    'https://www.newspapers.com/img/img?iat=JWT',
    LOC,
  ];
  assert.strictEqual(findIiifImageUrl(urls), LOC);
  assert.strictEqual(findIiifImageUrl(['https://example.com/x.jpg']), null);
  assert.strictEqual(findIiifImageUrl([]), null);
});
