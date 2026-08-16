// Tests for the snapshot's URL anchoring and disarming helpers, extracted from
// content.js.
//
// content.js cannot be require()d (it registers a chrome.runtime listener at
// load), so the helpers are lifted out of its source text between the sync
// markers and evaluated on their own - the same trick tests/test-sync.js uses.
// What is checked here is the contract the saved page depends on:
//
//   • fragment-only URLs are LEFT ALONE, so `#facts` still scrolls inside the
//     snapshot and an SVG `<use href="#icon">` sprite still renders,
//   • everything the page needs in order to SHOW what it showed is absolutized
//     against the live page, including the url()s inside CSS, whose base is the
//     stylesheet's own address,
//   • anything that would act on the reader's behalf - a meta refresh, an
//     onload handler, a speculative fetch, a frame that can retarget the top
//     window - is DISARMED instead, with its original value kept where a reader
//     can still find it,
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

// Both blocks are evaluated together: the rewrite helpers call absolutizeUrl.
const {
  absolutizeUrl, absolutizeCss,
  anchorBaseElement, disarmMetaPragma, disarmInlineHandlers,
  disarmSpeculativeLink, limitFrameNavigation, disarmNoscriptText,
  disarmAttribute,
} = new Function(
  extractBlock('// FHA-SYNC-BEGIN snapshot-urls\n', '// FHA-SYNC-END snapshot-urls') +
  extractBlock('// FHA-SYNC-BEGIN snapshot-rewrites\n', '// FHA-SYNC-END snapshot-rewrites') +
  '\nreturn { absolutizeUrl, absolutizeCss, anchorBaseElement, disarmMetaPragma,' +
  ' disarmInlineHandlers, disarmSpeculativeLink, limitFrameNavigation,' +
  ' disarmNoscriptText, disarmAttribute };'
)();

// A stand-in for one cloned element. The rewrite helpers deliberately take a
// single element and touch it only through the five attribute methods below, so
// the whole pass is testable in node, which has no DOM.
function el(attrs) {
  const map = new Map(Object.entries(attrs || {}));
  return {
    getAttributeNames: () => Array.from(map.keys()),
    hasAttribute: (name) => map.has(name),
    getAttribute: (name) => (map.has(name) ? map.get(name) : null),
    setAttribute: (name, value) => { map.set(name, String(value)); },
    removeAttribute: (name) => { map.delete(name); },
  };
}

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

// ── anchorBaseElement: the double-resolution guard ───────────────────────────
//
// document.baseURI has ALREADY absorbed the page's own <base href>. Resolving
// that raw attribute against it a second time applies the same relative path
// twice, and every URL the snapshot left relative then points one directory too
// deep - silently, in a file nobody opens for years.

test('a path-relative <base> is written resolved, not resolved against itself', () => {
  // A page at /collections/detail carrying `<base href="records/">`: the browser
  // already reports /collections/records/ as the base.
  const pageBase = 'https://records.example.org/collections/records/';
  const base = el({ href: 'records/' });
  anchorBaseElement(base, pageBase);
  assert.strictEqual(base.getAttribute('href'), pageBase);
  assert.ok(!base.getAttribute('href').includes('/records/records/'),
    'the base path was applied twice: ' + base.getAttribute('href'));
});

test('the doubled path the old formula produced is still what it produces', () => {
  // Pins the arithmetic behind the test above, so a future reader can see what
  // "resolved against itself" actually cost rather than take it on trust.
  assert.strictEqual(
    absolutizeUrl('records/', 'https://records.example.org/collections/records/'),
    'https://records.example.org/collections/records/records/');
  // And the shape the fix relies on: an absolute URL resolved against itself is
  // the identity, which is what makes it safe to write document.baseURI back.
  const pageBase = 'https://records.example.org/collections/records/';
  assert.strictEqual(absolutizeUrl(pageBase, pageBase), pageBase);
});

test('a root-relative <base> still works', () => {
  // `<base href="/records/">` on the same page: baseURI is the site root path.
  // This one behaved correctly before the fix too - it is here so the fix cannot
  // quietly break the case that already worked.
  const pageBase = 'https://records.example.org/records/';
  const base = el({ href: '/records/' });
  anchorBaseElement(base, pageBase);
  assert.strictEqual(base.getAttribute('href'), pageBase);
});

test('an absolute <base> survives untouched', () => {
  const pageBase = 'https://cdn.example.net/mirror/';
  const base = el({ href: 'https://cdn.example.net/mirror/' });
  anchorBaseElement(base, pageBase);
  assert.strictEqual(base.getAttribute('href'), pageBase);
});

test('a page with no <base> at all is a no-op', () => {
  assert.doesNotThrow(() => anchorBaseElement(null, PAGE));
});

test('a base on a page opened from disk is left as the author wrote it', () => {
  // The privacy rule: nothing else on such a page gets absolutized either, so
  // the snapshot stays internally consistent instead of half-anchored to
  // someone's home directory.
  const base = el({ href: 'sub/' });
  anchorBaseElement(base, 'file:///home/someone/Documents/records/');
  assert.strictEqual(base.getAttribute('href'), 'sub/');
});

// ── disarmMetaPragma: navigation away is neutralized, not armed ──────────────

test('a meta refresh cannot navigate the snapshot away from itself', () => {
  const meta = el({ 'http-equiv': 'refresh', content: '0;url=/login' });
  disarmMetaPragma(meta, PAGE);
  assert.strictEqual(meta.getAttribute('http-equiv'), null);
  assert.strictEqual(meta.getAttribute('data-fha-disabled-http-equiv'), 'refresh');
});

test('a disarmed refresh still says where it pointed', () => {
  // Disarm, not delete: the page carried this directive and a reader has to be
  // able to see that, and to see the destination without resolving it by hand.
  const meta = el({ 'http-equiv': 'refresh', content: "0; url='/login'" });
  disarmMetaPragma(meta, PAGE);
  assert.strictEqual(meta.getAttribute('content'), "0; url='/login'");
  assert.strictEqual(meta.getAttribute('data-fha-refresh-target'),
    'https://records.example.org/login');
});

test('a refresh with no url is disarmed too', () => {
  // `content="5"` reloads the current document; left live it sits there
  // reloading the snapshot every five seconds.
  const meta = el({ 'http-equiv': 'refresh', content: '5' });
  disarmMetaPragma(meta, PAGE);
  assert.strictEqual(meta.getAttribute('http-equiv'), null);
  assert.strictEqual(meta.getAttribute('data-fha-refresh-target'), null);
});

test('a captured CSP cannot blank the preserved page', () => {
  // `default-src 'self'` forbids exactly what preservation depends on: the
  // data: images inlined into the snapshot and the <style> blocks that replaced
  // its stylesheets.
  const meta = el({ 'http-equiv': 'Content-Security-Policy', content: "default-src 'self'" });
  disarmMetaPragma(meta, PAGE);
  assert.strictEqual(meta.getAttribute('http-equiv'), null);
  // The pragma is parked verbatim, the author's capitals included: disarming is
  // meant to move the value, not edit it.
  assert.strictEqual(meta.getAttribute('data-fha-disabled-http-equiv'),
    'Content-Security-Policy');
});

test('the charset pragma is never touched', () => {
  // Losing this loses the encoding the parser prescans for, and the saved page
  // renders as mojibake.
  const meta = el({ 'http-equiv': 'Content-Type', content: 'text/html; charset=utf-8' });
  disarmMetaPragma(meta, PAGE);
  assert.strictEqual(meta.getAttribute('http-equiv'), 'Content-Type');
  assert.strictEqual(meta.getAttribute('data-fha-disabled-http-equiv'), null);
});

// ── the rest of the disarm sweep ─────────────────────────────────────────────

test('inline handlers are disarmed but still readable', () => {
  // With the executable scripts gone these are the only JavaScript left, and
  // `location = …` is the line most likely to be in one.
  const body = el({ onload: "location='https://live.example.org/'", class: 'record' });
  disarmInlineHandlers(body);
  assert.strictEqual(body.getAttribute('onload'), null);
  assert.strictEqual(body.getAttribute('data-fha-disabled-onload'),
    "location='https://live.example.org/'");
  assert.strictEqual(body.getAttribute('class'), 'record');
});

test('an ordinary attribute that merely starts with "on" is not a handler', () => {
  const cell = el({ one: '1', once: 'yes', onclick: 'go()' });
  disarmInlineHandlers(cell);
  assert.strictEqual(cell.getAttribute('one'), '1');
  assert.strictEqual(cell.getAttribute('once'), 'yes');
  assert.strictEqual(cell.getAttribute('onclick'), null);
});

test('a ping beacon does not reach the live server on click', () => {
  const link = el({ href: 'https://x.test/', ping: 'https://track.example.org/p' });
  disarmAttribute(link, 'ping');
  assert.strictEqual(link.getAttribute('ping'), null);
  assert.strictEqual(link.getAttribute('data-fha-disabled-ping'),
    'https://track.example.org/p');
  assert.strictEqual(link.getAttribute('href'), 'https://x.test/');
});

test('speculative <link>s stop fetching, rendering ones are left alone', () => {
  const preload = el({ rel: 'preload', as: 'font', href: 'https://x.test/f.woff2' });
  disarmSpeculativeLink(preload);
  assert.strictEqual(preload.getAttribute('rel'), null);
  assert.strictEqual(preload.getAttribute('data-fha-disabled-rel'), 'preload');

  for (const rel of ['stylesheet', 'canonical', 'icon', 'stylesheet preload']) {
    const keep = el({ rel, href: 'https://x.test/a' });
    disarmSpeculativeLink(keep);
    assert.strictEqual(keep.getAttribute('rel'), rel, rel);
  }
});

test('a framed page cannot retarget the whole snapshot', () => {
  // The meta-refresh hazard one level down: a frame-busting `top.location = …`
  // takes the reader off the evidence just as effectively.
  const frame = el({ src: 'https://viewer.example.org/1' });
  limitFrameNavigation(frame);
  const tokens = frame.getAttribute('sandbox').split(' ');
  assert.ok(!tokens.some((t) => t.startsWith('allow-top-navigation')),
    frame.getAttribute('sandbox'));
  assert.ok(tokens.includes('allow-scripts'), frame.getAttribute('sandbox'));
  assert.strictEqual(frame.getAttribute('src'), 'https://viewer.example.org/1');
});

test("an author's own sandbox is kept, minus the one token that matters", () => {
  const frame = el({
    src: 'https://viewer.example.org/1',
    sandbox: 'allow-forms allow-top-navigation-by-user-activation allow-scripts',
  });
  limitFrameNavigation(frame);
  assert.strictEqual(frame.getAttribute('sandbox'), 'allow-forms allow-scripts');

  const locked = el({ src: 'https://viewer.example.org/1', sandbox: '' });
  limitFrameNavigation(locked);
  assert.strictEqual(locked.getAttribute('sandbox'), '');
});

test('a refresh hidden inside <noscript> is disarmed as well', () => {
  // With scripting on, a <noscript>'s contents are one raw text node, so no
  // element pass ever sees them - but they become live markup for a reader who
  // opens the archived file with JavaScript off.
  const out = disarmNoscriptText(
    '<meta http-equiv="refresh" content="0;url=/nojs"><p>Enable JS</p>');
  // No BARE http-equiv left (the disabled twin ends in the same letters, so the
  // check has to look at what precedes the attribute name).
  assert.ok(!/\shttp-equiv\s*=/i.test(out), out);
  assert.ok(out.includes('data-fha-disabled-http-equiv="refresh"'), out);
  assert.ok(out.includes('0;url=/nojs'), out);
  assert.ok(out.includes('<p>Enable JS</p>'), out);
});

test('other <noscript> markup passes through byte for byte', () => {
  const plain = '<img src="pixel.gif"><p>This page needs JavaScript.</p>';
  assert.strictEqual(disarmNoscriptText(plain), plain);
  assert.strictEqual(disarmNoscriptText(''), '');
  assert.strictEqual(disarmNoscriptText(null), '');
});
