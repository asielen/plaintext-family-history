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

// Three blocks are evaluated together: the rewrite helpers call absolutizeUrl,
// and the srcdoc pass re-uses the srcset parser the element sweep uses.
const {
  absolutizeUrl, absolutizeCss,
  anchorBaseElement, disarmMetaPragma, disarmInlineHandlers,
  disarmSpeculativeLink, limitFrameNavigation, disarmNoscriptText,
  disarmAttribute, disarmSrcdocMarkup,
} = new Function(
  extractBlock('// FHA-SYNC-BEGIN srcset\n', '// FHA-SYNC-END srcset') +
  extractBlock('// FHA-SYNC-BEGIN snapshot-urls\n', '// FHA-SYNC-END snapshot-urls') +
  extractBlock('// FHA-SYNC-BEGIN snapshot-rewrites\n', '// FHA-SYNC-END snapshot-rewrites') +
  '\nreturn { absolutizeUrl, absolutizeCss, anchorBaseElement, disarmMetaPragma,' +
  ' disarmInlineHandlers, disarmSpeculativeLink, limitFrameNavigation,' +
  ' disarmNoscriptText, disarmAttribute, disarmSrcdocMarkup };'
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

// ── iframe srcdoc: the second document a snapshot carries ────────────────────
//
// `srcdoc` holds a whole document inside an ATTRIBUTE VALUE, so the script
// sweep, the handler sweep and the URL sweep - all of which walk elements -
// never see a line of it. Opening the saved file parses it fresh and runs it.

test('a srcdoc <script> cannot execute from the saved snapshot', () => {
  const out = disarmSrcdocMarkup(
    '<p>Baptism, 1871</p><script>location="https://live.example.org/"</script>',
    PAGE);
  // Every <script> left in the markup carries a type no browser will run.
  assert.ok(!/<script(?![^>]*type="text\/fha-disabled-script")/i.test(out), out);
  assert.ok(out.includes('data-fha-disabled-type=""'), out);
  // And the frame itself is denied the permission, so anything the pass above
  // failed to recognise stays inert too.
  const frame = el({ srcdoc: '<script>alert(1)<\/script>' });
  limitFrameNavigation(frame);
  assert.ok(!frame.getAttribute('sandbox').split(' ').includes('allow-scripts'),
    frame.getAttribute('sandbox'));
});

test('a srcdoc inline handler is disarmed', () => {
  const out = disarmSrcdocMarkup(
    '<body onload="top.location=\'https://live.example.org/\'"><img src="a.gif" ' +
    'onerror="fetch(\'https://track.example.org/\')"></body>', PAGE);
  assert.ok(!/\son(load|error)\s*=/i.test(out), out);
  assert.ok(out.includes('data-fha-disabled-onload='), out);
  assert.ok(out.includes('data-fha-disabled-onerror='), out);
});

test('a disarmed srcdoc is still readable as evidence', () => {
  // Disarm, not delete: the page said what it said, and a reader (or a grep)
  // has to be able to see all of it in the saved file.
  const srcdoc = '<h1>Marriage register</h1><script type="text/javascript">' +
    'track("view")</script><p>Page 214</p>';
  const out = disarmSrcdocMarkup(srcdoc, PAGE);
  assert.ok(out.includes('<h1>Marriage register</h1>'), out);
  assert.ok(out.includes('<p>Page 214</p>'), out);
  assert.ok(out.includes('track("view")'), out);
  // The author's own type is parked, not dropped.
  assert.ok(out.includes('data-fha-disabled-type="text/javascript"'), out);
});

test('a srcdoc JSON data script is kept exactly as it was', () => {
  // The snapshot deliberately keeps non-executable data scripts so the ingest
  // recipe can still read JSON-LD out of the saved file.
  const srcdoc = '<script type="application/ld+json">{"name":"Ann"}</script>';
  assert.strictEqual(disarmSrcdocMarkup(srcdoc, PAGE), srcdoc);
});

test('a meta refresh inside srcdoc is disarmed, not armed', () => {
  const out = disarmSrcdocMarkup(
    '<meta http-equiv="refresh" content="0;url=/login"><p>Record</p>', PAGE);
  assert.ok(!/\shttp-equiv\s*=/i.test(out), out);
  assert.ok(out.includes('data-fha-disabled-http-equiv="refresh"'), out);
  assert.ok(out.includes('0;url=/login'), out);
});

test('a captured CSP and a speculative link inside srcdoc are disarmed', () => {
  const out = disarmSrcdocMarkup(
    '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'">' +
    '<link rel="prerender" href="/next"><link rel="stylesheet" href="/s.css">', PAGE);
  assert.ok(out.includes('data-fha-disabled-http-equiv="Content-Security-Policy"'), out);
  assert.ok(out.includes('data-fha-disabled-rel="prerender"'), out);
  assert.ok(out.includes('rel="stylesheet"'), out);
});

test('a ping beacon inside srcdoc does not reach the live server', () => {
  const out = disarmSrcdocMarkup(
    '<a href="/r/2" ping="https://track.example.org/p">next</a>', PAGE);
  assert.ok(!/\sping\s*=/i.test(out), out);
  assert.ok(out.includes('data-fha-disabled-ping="https://track.example.org/p"'), out);
});

test('relative references inside srcdoc are anchored to the live page', () => {
  // Same file:// problem as the outer document: an about:srcdoc document
  // inherits the PARENT's base URL, so opened from disk every one of these
  // resolves into whatever folder the snapshot was saved in.
  const out = disarmSrcdocMarkup(
    '<img src="scan.jpg"><a href="../up.html">up</a>' +
    '<img srcset="a.jpg 1x, b.jpg 2x">' +
    '<div style="background:url(paper.png)"></div>' +
    '<style>body{background:url(tile.png)}</style>', PAGE);
  assert.ok(out.includes('src="https://records.example.org/collections/1880/scan.jpg"'), out);
  assert.ok(out.includes('href="https://records.example.org/collections/up.html"'), out);
  assert.ok(out.includes('https://records.example.org/collections/1880/a.jpg 1x'), out);
  assert.ok(out.includes('url(https://records.example.org/collections/1880/paper.png)'), out);
  assert.ok(out.includes('url(https://records.example.org/collections/1880/tile.png)'), out);
});

test('a <base> inside srcdoc is honoured once, not applied twice', () => {
  const out = disarmSrcdocMarkup('<base href="records/"><img src="scan.jpg">', PAGE);
  assert.ok(out.includes('href="https://records.example.org/collections/1880/records/"'), out);
  assert.ok(out.includes('src="https://records.example.org/collections/1880/records/scan.jpg"'),
    out);
  assert.ok(!out.includes('/records/records/'), out);
});

test('an ampersand in a srcdoc URL survives one round trip', () => {
  const out = disarmSrcdocMarkup('<img src="img.php?a=1&amp;b=2">', PAGE);
  assert.ok(out.includes('src="https://records.example.org/collections/1880/img.php?a=1&amp;b=2"'),
    out);
});

test('a srcdoc captured from disk leaks no local path', () => {
  const out = disarmSrcdocMarkup(
    '<base href="sub/"><img src="scan.jpg">',
    'file:///home/someone/Documents/record.html');
  assert.ok(!out.includes('file:'), out);
  assert.ok(out.includes('href="sub/"'), out);
  assert.ok(out.includes('src="scan.jpg"'), out);
});

test('srcdoc markup no rule touches comes back byte for byte', () => {
  const plain = '<table class="record"><tr><td>Ann Hartley</td></tr></table>';
  assert.strictEqual(disarmSrcdocMarkup(plain, PAGE), plain);
  assert.strictEqual(disarmSrcdocMarkup('', PAGE), '');
  assert.strictEqual(disarmSrcdocMarkup(null, PAGE), '');
});

test('an already-absolute srcdoc reference is not re-written for the sake of it', () => {
  const plain = '<img src="https://cdn.example.net/a.png" class=thumb>';
  assert.strictEqual(disarmSrcdocMarkup(plain, PAGE), plain);
});

test('disarming a srcdoc twice is the same as disarming it once', () => {
  const srcdoc = '<script>go()</script><img src="scan.jpg" onerror="retry()">' +
    '<meta http-equiv="refresh" content="0;url=/login">';
  const once = disarmSrcdocMarkup(srcdoc, PAGE);
  assert.strictEqual(disarmSrcdocMarkup(once, PAGE), once);
});

test('a frame nested inside srcdoc is sandboxed too, and deeper ones inherit', () => {
  // The recursion bound: this pass gives the nested frame a sandbox and does not
  // unescape its own srcdoc, because sandbox flags are inherited by every nested
  // browsing context - nothing below a frame denied allow-scripts can run script.
  const out = disarmSrcdocMarkup(
    '<iframe src="https://viewer.example.org/1"></iframe>' +
    '<iframe srcdoc="&lt;script&gt;alert(1)&lt;/script&gt;"></iframe>', PAGE);
  const sandboxes = out.match(/sandbox="[^"]*"/g) || [];
  assert.strictEqual(sandboxes.length, 2, out);
  for (const sandbox of sandboxes) {
    assert.ok(!sandbox.includes('allow-top-navigation'), sandbox);
  }
  assert.ok(!sandboxes[1].includes('allow-scripts'), sandboxes[1]);
  assert.ok(sandboxes[0].includes('allow-scripts'), sandboxes[0]);
  // The nested markup is left escaped and readable, not unescaped and re-run.
  assert.ok(out.includes('srcdoc="&lt;script&gt;alert(1)&lt;/script&gt;"'), out);
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

test('script permission is withheld from a frame we carry the markup of', () => {
  // A live `src` frame keeps allow-scripts - the framed thing is often the
  // record viewer, and most viewers are a blank box without it. A `srcdoc`
  // frame does not: that document is markup we captured and disarmed, so there
  // is nothing legitimate left in it to run.
  const inline = el({ srcdoc: '<p>Record</p>' });
  limitFrameNavigation(inline);
  assert.strictEqual(inline.getAttribute('sandbox'),
    'allow-same-origin allow-forms allow-popups');

  const live = el({ src: 'https://viewer.example.org/1' });
  limitFrameNavigation(live);
  assert.ok(live.getAttribute('sandbox').split(' ').includes('allow-scripts'));

  // An author who granted it explicitly does not get it back either.
  const declared = el({ srcdoc: '<p>Record</p>', sandbox: 'allow-scripts allow-forms' });
  limitFrameNavigation(declared);
  assert.strictEqual(declared.getAttribute('sandbox'), 'allow-forms');
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

test('the rest of what a <noscript> can carry is disarmed as well', () => {
  // Everything in here is live for a reader with scripting off - the audience
  // most likely to open an archived file - and none of it waits to be asked.
  const out = disarmNoscriptText(
    '<meta http-equiv="Content-Security-Policy" content="default-src \'self\'">' +
    '<link rel="prefetch" href="/next">' +
    '<iframe src="https://viewer.example.org/1"></iframe>');
  assert.ok(out.includes('data-fha-disabled-http-equiv="Content-Security-Policy"'), out);
  assert.ok(out.includes('data-fha-disabled-rel="prefetch"'), out);
  assert.ok(/sandbox="[^"]*"/.test(out), out);
  assert.ok(!/allow-top-navigation/.test(out), out);
  // Still an absolutize-free zone: a relative reference in here stays relative
  // rather than becoming a working request to the live server.
  assert.ok(out.includes('href="/next"'), out);
});
