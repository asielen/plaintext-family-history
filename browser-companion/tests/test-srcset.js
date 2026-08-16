// Tests for the HTML-compatible srcset parser (src/lib/srcset.js).
//
// The bug these exist to prevent: srcset was parsed by splitting on commas.
// Commas are legal inside candidate URLs, so that split cut one URL into
// several fragments, and the snapshot writer then "absolutized" each fragment
// as an unrelated path. Every `<source srcset>` and every image the inliner's
// budget did not reach was silently left pointing at nothing.
//
//   node --test browser-companion/tests/test-srcset.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');

const {
  parseSrcset,
  serializeSrcset,
  rewriteSrcset,
  srcsetUrls,
} = require('../src/lib/srcset.js');

test('the ordinary case: url + density descriptors', () => {
  assert.deepStrictEqual(parseSrcset('small.png 1x, large.png 2x'), [
    { url: 'small.png', descriptor: '1x' },
    { url: 'large.png', descriptor: '2x' },
  ]);
});

test('a lone candidate needs neither descriptor nor comma', () => {
  assert.deepStrictEqual(parseSrcset('photo.jpg'), [
    { url: 'photo.jpg', descriptor: '' },
  ]);
});

test('a data: URL keeps its own commas', () => {
  // The regression case. `data:image/svg+xml,<svg …>` always contains a comma
  // after the media type, and inline SVG placeholders add more.
  const value =
    'data:image/svg+xml;utf8,%3Csvg%20viewBox%3D%220,0,10,10%22%3E 1x, real.png 2x';
  assert.deepStrictEqual(parseSrcset(value), [
    {
      url: 'data:image/svg+xml;utf8,%3Csvg%20viewBox%3D%220,0,10,10%22%3E',
      descriptor: '1x',
    },
    { url: 'real.png', descriptor: '2x' },
  ]);
});

test('a base64 data: URL survives as one candidate', () => {
  const value = 'data:image/gif;base64,R0lGODlhAQABAAAAACw= 1x';
  assert.deepStrictEqual(parseSrcset(value), [
    { url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=', descriptor: '1x' },
  ]);
});

test('parameterized CDN URLs with commas stay whole', () => {
  const value =
    'https://cdn.example.org/c_fill,w_600,h_400/scan.jpg 600w, ' +
    'https://cdn.example.org/c_fill,w_1200,h_800/scan.jpg 1200w';
  assert.deepStrictEqual(srcsetUrls(value), [
    'https://cdn.example.org/c_fill,w_600,h_400/scan.jpg',
    'https://cdn.example.org/c_fill,w_1200,h_800/scan.jpg',
  ]);
});

test('a comma TRAILING the url run does separate candidates', () => {
  // This is the one place a comma ends a URL: immediately after it, with no
  // descriptor between. Both spellings mean the same two candidates.
  assert.deepStrictEqual(parseSrcset('a.png,b.png 2x'), [
    { url: 'a.png,b.png', descriptor: '2x' },
  ]);
  assert.deepStrictEqual(parseSrcset('a.png, b.png 2x'), [
    { url: 'a.png', descriptor: '' },
    { url: 'b.png', descriptor: '2x' },
  ]);
  assert.deepStrictEqual(parseSrcset('a.png,, b.png'), [
    { url: 'a.png', descriptor: '' },
    { url: 'b.png', descriptor: '' },
  ]);
});

test('multiple descriptors on one candidate are kept in order', () => {
  assert.deepStrictEqual(parseSrcset('a.png 100w 2x, b.png 200w 3x'), [
    { url: 'a.png', descriptor: '100w 2x' },
    { url: 'b.png', descriptor: '200w 3x' },
  ]);
});

test('a comma inside descriptor parentheses does not split the candidate', () => {
  assert.deepStrictEqual(parseSrcset('a.png 100w (min-width:10px,max-width:20px), b.png'), [
    { url: 'a.png', descriptor: '100w (min-width:10px,max-width:20px)' },
    { url: 'b.png', descriptor: '' },
  ]);
});

test('runs of whitespace, tabs and newlines are separators too', () => {
  assert.deepStrictEqual(parseSrcset('\n  a.png   1x ,\t\n b.png\t2x\n'), [
    { url: 'a.png', descriptor: '1x' },
    { url: 'b.png', descriptor: '2x' },
  ]);
});

test('empty and junk inputs yield no candidates', () => {
  for (const junk of ['', '   ', ',', ' , , ', null, undefined]) {
    assert.deepStrictEqual(parseSrcset(junk), [], JSON.stringify(junk));
  }
});

test('serialize round-trips through parse', () => {
  const value =
    'data:image/svg+xml,%3Csvg%20a%3D%221,2%22%3E 1x, ' +
    'https://cdn.example.org/w_600,h_400/x.jpg 600w 2x';
  assert.deepStrictEqual(
    parseSrcset(serializeSrcset(parseSrcset(value))),
    parseSrcset(value)
  );
});

test('rewrite absolutizes only what the callback claims', () => {
  const abs = (u) => (u.startsWith('data:') ? null : 'https://site.test/r/' + u);
  assert.strictEqual(
    rewriteSrcset('thumb.png 1x, data:image/gif;base64,AAA= 2x, full.png 3x', abs),
    'https://site.test/r/thumb.png 1x, data:image/gif;base64,AAA= 2x, ' +
      'https://site.test/r/full.png 3x'
  );
});

test('rewrite leaves a candidate alone when the callback declines', () => {
  assert.strictEqual(rewriteSrcset('#frag 1x', () => null), '#frag 1x');
});
