// Tests for the snapshot's URL anchoring helpers, extracted from content.js.
//
// content.js cannot be require()d (it registers a chrome.runtime listener at
// load), so the helpers are lifted out of its source text between the sync
// markers and evaluated on their own - the same trick tests/test-sync.js uses.
// What is checked here is the contract the saved page depends on:
//
//   • fragment-only URLs are LEFT ALONE, so `#facts` still scrolls inside the
//     snapshot and an SVG `<use href="#icon">` sprite still renders,
//   • everything else is absolutized against the live page, including the
//     url()s inside CSS, whose base is the stylesheet's own address,
//   • nothing that resolves to a `file:` URL is ever written into the snapshot
//     (the privacy rule: no local absolute paths in archive files).
//
//   node --test browser-companion/tests/test-snapshot-urls.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const content = fs.readFileSync(
  path.join(__dirname, '..', 'src', 'content.js'), 'utf8');

function extractBlock(startMarker, endMarker) {
  const from = content.indexOf(startMarker);
  assert.ok(from !== -1, 'marker not found in content.js: ' + startMarker);
  const to = content.indexOf(endMarker, from);
  assert.ok(to > from, 'end marker not found: ' + endMarker);
  return content.slice(from + startMarker.length, to);
}

const { absolutizeUrl, absolutizeCss } = new Function(
  extractBlock('// FHA-SYNC-BEGIN snapshot-urls\n', '// FHA-SYNC-END snapshot-urls') +
  '\nreturn { absolutizeUrl, absolutizeCss };'
)();

const PAGE = 'https://records.example.org/collections/1880/detail?id=7';

// ── absolutizeUrl ────────────────────────────────────────────────────────────

test('a relative reference is anchored to the live page', () => {
  assert.strictEqual(absolutizeUrl('scan.jpg', PAGE),
    'https://records.example.org/collections/1880/scan.jpg');
  assert.strictEqual(absolutizeUrl('/img/logo.png', PAGE),
    'https://records.example.org/img/logo.png');
  assert.strictEqual(absolutizeUrl('../up.html', PAGE),
    'https://records.example.org/collections/up.html');
});

test('a fragment-only reference is left exactly as it was', () => {
  // The whole reason the snapshot rewrites attributes instead of injecting a
  // <base>: `#facts` must keep scrolling within the saved page, and an SVG
  // `<use href="#icon">` must keep finding its sprite.
  assert.strictEqual(absolutizeUrl('#facts', PAGE), null);
  assert.strictEqual(absolutizeUrl('#icon-star', PAGE), null);
});

test('self-contained and non-navigational schemes are left alone', () => {
  for (const value of ['data:image/gif;base64,AAA=', 'blob:https://x/y',
                       'javascript:void(0)', 'mailto:a@b.test', 'tel:+1234',
                       'about:blank']) {
    assert.strictEqual(absolutizeUrl(value, PAGE), null, value);
  }
});

test('an already-absolute URL comes back absolute', () => {
  assert.strictEqual(absolutizeUrl('https://cdn.example.net/a.png', PAGE),
    'https://cdn.example.net/a.png');
});

test('empty, blank and unparseable values are declined', () => {
  for (const value of ['', '   ', null, undefined]) {
    assert.strictEqual(absolutizeUrl(value, PAGE), null, JSON.stringify(value));
  }
  assert.strictEqual(absolutizeUrl('relative.png', 'not a url'), null);
});

test('a local disk path is never written into a snapshot', () => {
  // Capturing a page opened from disk would otherwise bake this machine's
  // folder names into a file that lands in the archive.
  const localPage = 'file:///home/someone/Documents/record.html';
  assert.strictEqual(absolutizeUrl('scan.jpg', localPage), null);
  assert.strictEqual(absolutizeUrl('file:///etc/passwd', PAGE), null);
});

// ── absolutizeCss ────────────────────────────────────────────────────────────

const SHEET = 'https://records.example.org/theme/site.css';

test('url() references resolve against the stylesheet, not the document', () => {
  // The half a naive inliner forgets: moving CSS text into a <style> block
  // re-bases every relative url() from the sheet onto the document.
  assert.strictEqual(
    absolutizeCss('body { background: url(../img/paper.png); }', SHEET),
    'body { background: url(https://records.example.org/img/paper.png); }');
});

test('quoting style is preserved', () => {
  assert.strictEqual(absolutizeCss("a { background: url('x.png'); }", SHEET),
    "a { background: url('https://records.example.org/theme/x.png'); }");
  assert.strictEqual(absolutizeCss('a { background: url("x.png"); }', SHEET),
    'a { background: url("https://records.example.org/theme/x.png"); }');
});

test('data: and fragment url() references are untouched', () => {
  const css = 'a{background:url(data:image/gif;base64,AAA=)}b{fill:url(#grad)}';
  assert.strictEqual(absolutizeCss(css, SHEET), css);
});

test('@import is anchored in both spellings', () => {
  assert.strictEqual(absolutizeCss('@import "reset.css";', SHEET),
    '@import "https://records.example.org/theme/reset.css";');
  assert.strictEqual(absolutizeCss('@import url(reset.css);', SHEET),
    '@import url(https://records.example.org/theme/reset.css);');
});

test('every url() in a sheet is rewritten, not just the first', () => {
  const out = absolutizeCss('a{background:url(a.png)} b{background:url(b.png)}', SHEET);
  assert.ok(out.includes('https://records.example.org/theme/a.png'), out);
  assert.ok(out.includes('https://records.example.org/theme/b.png'), out);
});

test('CSS from a local file is left relative rather than leaking a path', () => {
  const css = 'body { background: url(paper.png); }';
  assert.strictEqual(absolutizeCss(css, 'file:///home/someone/site.css'), css);
});

test('unparseable url() syntax is left exactly as written', () => {
  // Garbling a stylesheet is worse than leaving one rule unresolved.
  const css = 'a { background: url("a(b).png"); }';
  assert.strictEqual(absolutizeCss(css, SHEET), css);
  assert.strictEqual(absolutizeCss('', SHEET), '');
  assert.strictEqual(absolutizeCss(null, SHEET), '');
});
