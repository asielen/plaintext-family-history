// Tests for the Ancestry record capture's readers (lib/ancestry-record.js):
// which record a page is, its index fields, household and citation as the
// site shows them, the linked scan (including /interactive/ links), what the
// record says about itself for filing (TI-066), and that the Seen It
// add-on's injected badges are never read as the record (TI-067).
//
// The pages are the same scrubbed fixtures the Python recipe tests read
// (tests/fixtures/capture-samples/ancestry-record-*.html: fictional Hartleys,
// invented ids), and the hint cases are one shared table
// (ancestry-hints.json) so the companion and the recipe cannot drift apart.
//
//   node --test browser-companion/tests/test-ancestry.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const anc = require('../src/lib/ancestry-record.js');
const { parse, find } = require('./mini-dom.js');

const REPO = path.join(__dirname, '..', '..');
const SAMPLES = path.join(REPO, 'tests', 'fixtures', 'capture-samples');
const page = (name) => parse(fs.readFileSync(path.join(SAMPLES, name + '.html'), 'utf8'));
const CASES = JSON.parse(fs.readFileSync(path.join(SAMPLES, 'ancestry-hints.json'), 'utf8'));

const CENSUS_URL = 'https://www.ancestry.com/search/collections/90001/records/1000001';
const DEATH_URL = 'https://www.ancestry.com/search/collections/90002/records/2000001?tid=&pid=';

function readRecord(name, url) {
  const doc = page(name);
  return anc.ancRecordFrom(doc, url, { title: doc.title, origin: 'https://www.ancestry.com' });
}

// ── identity ─────────────────────────────────────────────────────────────────

test('ancParseRecordPage: today\'s record address and the older one, nothing else', () => {
  assert.deepStrictEqual(anc.ancParseRecordPage(CENSUS_URL), { dbId: '90001', recordId: '1000001' });
  assert.deepStrictEqual(anc.ancParseRecordPage('https://www.ancestry.co.uk/search/collections/90001/records/7?x=1'),
    { dbId: '90001', recordId: '7' });
  assert.deepStrictEqual(anc.ancParseRecordPage('https://www.ancestry.com/discoveryui-content/view/1000001:90001'),
    { dbId: '90001', recordId: '1000001' });
  for (const u of [
    'https://www.ancestry.com/imageviewer/collections/90001/images/4000123_00045',
    'https://www.ancestry.com/search/collections/90001/',
    'https://notancestry.example.org/search/collections/1/records/2',
    'https://www.familysearch.org/ark:/61903/1:1:X',
    'not a url', '', null,
  ]) {
    assert.strictEqual(anc.ancParseRecordPage(u), null, String(u));
  }
});

test('ancImageFromHref: the scan through the image viewer or an /interactive/ link, this collection only', () => {
  assert.strictEqual(anc.ancImageFromHref('/imageviewer/collections/90001/images/4000123_00045?pid=1', '90001'),
    '4000123_00045');
  assert.strictEqual(anc.ancImageFromHref('https://www.ancestry.com/interactive/90001/m-t0000-00012-00034/1000001', '90001'),
    'm-t0000-00012-00034');
  assert.strictEqual(anc.ancImageFromHref('/imageviewer/collections/90001/images/a%20b', '90001'), 'a b');
  assert.strictEqual(anc.ancImageFromHref('/imageviewer/collections/90001/images/a%zz', '90001'), 'a%zz');
  // Another collection's image (a linked household member elsewhere) is not this record's.
  assert.strictEqual(anc.ancImageFromHref('/imageviewer/collections/90009/images/x', '90001'), null);
  // An /interactive/ link without its record part is not the shape we know.
  assert.strictEqual(anc.ancImageFromHref('/interactive/90001/x', '90001'), null);
  assert.strictEqual(anc.ancImageFromHref('/search/collections/90001/records/2', '90001'), null);
  assert.strictEqual(anc.ancImageFromHref(null, '90001'), null);
});

// ── the record from today's layout ───────────────────────────────────────────

test('ancRecordFrom reads a census record: identity, name, collection, scan', () => {
  const r = readRecord('ancestry-record-census', CENSUS_URL);
  assert.strictEqual(r.ok, true);
  assert.deepStrictEqual(r.record, {
    dbId: '90001', recordId: '1000001', imageId: '4000123_00045',
    url: CENSUS_URL, name: 'Calvin Hartley', collection: '1880 United States Federal Census',
  });
});

test('ancRecordFrom reads the index fields as the site labels them', () => {
  const r = readRecord('ancestry-record-census', CENSUS_URL);
  assert.deepStrictEqual(r.fields, [
    { label: 'Name', value: 'Calvin Hartley' },
    { label: 'Age in 1880', value: '42' },
    { label: 'Birth Date', value: 'abt 1838' },
    { label: 'Birthplace', value: 'Ohio' },
    { label: 'Home in 1880', value: 'Fairview, Breton, Kansas' },
    { label: 'Relation to Head of House', value: 'Self (Head)' },
    { label: 'Spouse\'s Name', value: 'Harriet Hartley' },
  ]);
});

test('ancRecordFrom reads the household once, without the layout wrapper or its buttons', () => {
  const r = readRecord('ancestry-record-census', CENSUS_URL);
  assert.deepStrictEqual(r.household, [[
    ['Household Members (Name)', 'Age', 'Relationship'],
    ['Calvin Hartley', '42', 'Self (Head)'],
    ['Harriet Hartley', '39', 'Wife'],
    ['Warren Hartley', '12', 'Son'],
    ['Edith Hartley', '9', 'Daughter'],
  ]]);
});

test('ancRecordFrom keeps the site\'s source citation, section by section', () => {
  const r = readRecord('ancestry-record-census', CENSUS_URL);
  assert.deepStrictEqual(Object.keys(r.citation), ['Citation', 'Information', 'Collection description']);
  assert.strictEqual(r.citation.Citation,
    'Year: 1880; Census Place: Fairview, Breton, Kansas; Roll: 0000; Page: 12A; Enumeration District: 042');
});

test('TI-067: nothing the Seen It add-on put on the page reaches the record', () => {
  const r = readRecord('ancestry-record-census', CENSUS_URL);
  const all = JSON.stringify(r);
  for (const badge of ['Captured, not filed', 'Seen 1×', 'Seen 2×', 'Nothing here', 'Not in archive', 'Later', '📥']) {
    assert.ok(!all.includes(badge), 'Seen It text leaked: ' + badge);
  }
  // The row Seen It only dimmed (its nsi-ruled-out modifier) is the site's, and is kept.
  assert.ok(r.household[0].some((row) => row[0] === 'Warren Hartley'));
});

test('ancRecordFrom reads a death-index record with no scan', () => {
  const r = readRecord('ancestry-record-death-index', DEATH_URL);
  assert.strictEqual(r.record.imageId, null);
  assert.strictEqual(r.record.name, 'Ethel Hartley');
  assert.strictEqual(r.record.collection, 'California, U.S., Death Index, 1940-1997');
  assert.strictEqual(r.record.url, 'https://www.ancestry.com/search/collections/90002/records/2000001');
  assert.deepStrictEqual(r.fields.find((f) => f.label === 'Death Date'), { label: 'Death Date', value: '7 Jan 1957' });
  assert.deepStrictEqual(r.household, []);
  assert.strictEqual(r.citation.Citation, 'Place: Riverside; Date: 7 Jan 1957');
});

test('ancRecordFrom refuses a page that is not a record, and survives a bare one', () => {
  assert.deepStrictEqual(anc.ancRecordFrom(parse('<html></html>'), 'https://www.ancestry.com/search/'),
    { ok: false, error: 'this is not an Ancestry record page' });
  const r = anc.ancRecordFrom(parse('<html><body></body></html>'), CENSUS_URL, {});
  assert.deepStrictEqual(r, {
    ok: true,
    record: { dbId: '90001', recordId: '1000001', imageId: null, url: CENSUS_URL, name: null, collection: null },
    fields: [], household: [], citation: {},
  });
});

test('ancRecordFrom skips the tree-hint prompt <h1> for the name', () => {
  const doc = parse('<html><head><title>1880 United States Federal Census - Ancestry.com</title></head><body>' +
    '<h1>Does Calvin Hartley match the person in your tree?</h1><h1 class="title">Calvin Hartley</h1></body></html>');
  const r = anc.ancRecordFrom(doc, CENSUS_URL, { title: doc.title });
  assert.strictEqual(r.record.name, 'Calvin Hartley');
});

test('ancRecordFrom finds a scan linked as /interactive/', () => {
  const doc = parse('<html><body><a href="/search/collections/90001/records/1000002">Harriet</a>' +
    '<a href="/interactive/90001/4000123_00046/1000001">View</a></body></html>');
  assert.strictEqual(anc.ancRecordFrom(doc, CENSUS_URL, {}).record.imageId, '4000123_00046');
});

// ── ancText: what a reader sees ──────────────────────────────────────────────

test('ancText tidies text, breaks lines at blocks, and leaves out what is not shown', () => {
  const doc = parse('<div id="x"><p>One <b>two</b></p><p>three<br>four</p>' +
    '<script>no()</script><button>Save</button><span class="gone">hidden</span>' +
    '<span class="nsi-pills"><span class="nsi-pill">Seen 1×</span></span>' +
    '<div id="nsi-anc-badge">badge</div></div>');
  const x = find(doc, (e) => e.id === 'x');
  assert.strictEqual(anc.ancText(x, { hidden: (el) => el.className === 'gone' }), 'One two\nthree\nfour');
  // The live page's CSS decides what starts a line; here nothing does (a <br> still breaks).
  assert.strictEqual(anc.ancText(x, { block: () => false, hidden: (el) => el.className === 'gone' }),
    'One twothree\nfour');
});

test('ancIsSeenItNode: Seen It\'s own elements, never the site\'s rows it marks', () => {
  const el = (id, cls) => ({ nodeType: 1, tagName: 'SPAN', id, className: cls });
  assert.ok(anc.ancIsSeenItNode(el('nsi-anc-badge', '')));
  assert.ok(anc.ancIsSeenItNode(el('nsi-page-badge', '')));
  assert.ok(anc.ancIsSeenItNode(el('', 'nsi-pill nsi-captured')));
  assert.ok(anc.ancIsSeenItNode(el('', 'nsi-pills')));
  assert.ok(anc.ancIsSeenItNode(el('', 'nsi-mark nsi-on')));
  assert.ok(anc.ancIsSeenItNode(el('', 'nsi-node-chip nsi-node-all')));
  assert.ok(!anc.ancIsSeenItNode(el('', 'record nsi-ruled-out')));
  assert.ok(!anc.ancIsSeenItNode(el('', 'nsi-was-seen')));
  assert.ok(!anc.ancIsSeenItNode(el('recordData', 'nsi')));
  assert.ok(!anc.ancIsSeenItNode({ nodeType: 3, nodeValue: 'nsi-pill' }));
  assert.ok(anc.ancIsSeenItNode({ nodeType: 1, tagName: 'svg', id: '', className: { baseVal: 'nsi-pill' } }));
});

test('every element Seen It creates is one the capture skips (seen-it/ drift guard)', () => {
  // Read Seen It's content scripts: each class it gives an element it creates
  // must be in ANC_SEEN_IT_CLASSES (or the element's id starts "nsi-"), and
  // each class it only toggles on the SITE's rows must NOT be, or capture
  // would drop part of the record. A new one fails here until it is sorted.
  const dir = path.join(REPO, 'seen-it');
  const own = new Set();
  const ids = new Set();
  const toggled = new Set();
  for (const f of fs.readdirSync(dir).filter((n) => /^content.*\.js$/.test(n))) {
    const src = fs.readFileSync(path.join(dir, f), 'utf8');
    for (const m of src.matchAll(/\b(?:className|cls)\s*=\s*(['"])([^'"]+)\1/g)) own.add(m[2].trim().split(/\s+/)[0]);
    for (const m of src.matchAll(/\.id\s*=\s*(['"])([^'"]+)\1/g)) ids.add(m[2]);
    for (const m of src.matchAll(/classList\.(?:add|toggle)\(\s*(['"])([^'"]+)\1/g)) toggled.add(m[2]);
  }
  assert.ok(own.size >= 4, 'did not find Seen It\'s element classes - has seen-it/ moved?');
  for (const c of own) {
    if (!c.startsWith('nsi-')) continue;
    assert.ok(anc.ANC_SEEN_IT_CLASSES.includes(c), 'Seen It creates .' + c + ', which capture does not skip');
  }
  for (const id of ids) assert.match(id, /^nsi-/, 'Seen It creates #' + id + ', which capture does not skip');
  for (const c of toggled) {
    assert.ok(!anc.ANC_SEEN_IT_CLASSES.includes(c), '.' + c + ' marks a site row; skipping it would drop the record');
  }
});

// ── TI-066: what the record says about itself ────────────────────────────────

test('ancDateToEdtf: the shared date table', () => {
  for (const [value, want] of CASES.dates) {
    assert.strictEqual(anc.ancDateToEdtf(value), want, JSON.stringify(value));
  }
  assert.strictEqual(anc.ancDateToEdtf(null), null);
});

test('ancHints: the shared record table (census, death index, and the rest)', () => {
  for (const c of CASES.records) {
    const fields = c.fields.map(([label, value]) => ({ label, value }));
    assert.deepStrictEqual(anc.ancHints({ collection: c.collection }, fields), c.expect, c.collection);
  }
  assert.deepStrictEqual(anc.ancHints(null, null), { sourceType: null, sourceDate: null, sourceClass: null });
});

test('ancHints on the two fixture pages: census, and a 1957 death in a 1940-1997 index', () => {
  const census = readRecord('ancestry-record-census', CENSUS_URL);
  assert.deepStrictEqual(anc.ancHints(census.record, census.fields),
    { sourceType: 'census', sourceDate: '1880', sourceClass: null });
  const death = readRecord('ancestry-record-death-index', DEATH_URL);
  assert.deepStrictEqual(anc.ancHints(death.record, death.fields),
    { sourceType: 'vital-record', sourceDate: '1957-01-07', sourceClass: 'derivative' });
});

test('ancTitle: the person and collection, unless the human typed a title', () => {
  const rec = { dbId: '90001', recordId: '1', name: 'Calvin Hartley', collection: '1880 United States Federal Census' };
  const auto = 'Calvin Hartley - 1880 United States Federal Census';
  assert.strictEqual(anc.ancTitle(rec, ''), auto);
  assert.strictEqual(anc.ancTitle(rec, '1880 United States Federal Census - Ancestry.com'), auto);
  assert.strictEqual(anc.ancTitle(rec, '  Calvin in 1880  '), 'Calvin in 1880');
  assert.strictEqual(anc.ancTitle({ dbId: '9', recordId: '8' }, null), 'Ancestry record 9:8');
});

test('ancTitle knows the tab\'s own title on every Ancestry site (PR #213 audit #3)', () => {
  // Left as typed, each household member's bundle would get the same
  // nameless collection title on ancestry.co.uk, .ca or .com.au.
  const rec = { dbId: '90001', recordId: '1', name: 'Calvin Hartley', collection: '1881 England Census' };
  const auto = 'Calvin Hartley - 1881 England Census';
  for (const tab of [
    '1881 England Census - Ancestry.co.uk',
    '1881 England Census - Ancestry.ca',
    '1881 England Census - Ancestry.com.au',
    '1881 England Census | Ancestry',
    '1881 England Census | Ancestry.com',
    '1881 England Census – Ancestry.de',
  ]) {
    assert.strictEqual(anc.ancTitle(rec, tab), auto, tab);
    const doc = parse('<html><head><title>' + tab + '</title></head><body><h1>Calvin Hartley</h1></body></html>');
    assert.strictEqual(anc.ancRecordFrom(doc, CENSUS_URL, { title: doc.title }).record.collection,
      '1881 England Census', tab);
  }
  // A title that merely mentions the site is still hers.
  assert.strictEqual(anc.ancTitle(rec, 'Found on Ancestry, 1881'), 'Found on Ancestry, 1881');
});

// ── the saved copies of the page (content.js stripSeenIt) ────────────────────

test('page.html and the page copy are saved without Seen It\'s badges, on every site', () => {
  // content.js cannot be required (it registers a listener at load), so lift
  // stripSeenIt out of it and run it over the fixture, with a small
  // querySelectorAll for the two selector shapes it builds.
  const content = fs.readFileSync(path.join(__dirname, '..', 'src', 'content.js'), 'utf8');
  const fn = /  function stripSeenIt\(root\) \{[\s\S]*?\n  \}\n/.exec(content);
  assert.ok(fn, 'stripSeenIt not found in content.js');
  const stripSeenIt = new Function('ANC_SEEN_IT_CLASSES', fn[0] + 'return stripSeenIt;')(anc.ANC_SEEN_IT_CLASSES);
  // Both saved copies go through it.
  assert.match(content, /const page = document\.documentElement\.cloneNode\(true\);\n    stripSeenIt\(page\);/);
  assert.match(content, /const clone = document\.documentElement\.cloneNode\(true\);\n    stripSeenIt\(clone\);/);

  const doc = page('ancestry-record-census');
  const all = (n) => (n.childNodes || []).flatMap((c) => (c.nodeType === 1 ? [c, ...all(c)] : []));
  doc.querySelectorAll = (sel) => {
    const matchers = sel.split(',').map((part) => {
      const id = /^\[id\^="([^"]+)"\]$/.exec(part);
      if (id) return (e) => e.id.startsWith(id[1]);
      const cls = /^\.([\w-]+)$/.exec(part);
      assert.ok(cls, 'unexpected selector part ' + part);
      return (e) => e.className.split(/\s+/).includes(cls[1]);
    });
    return all(doc).filter((e) => matchers.some((m) => m(e)));
  };
  for (const e of all(doc)) {
    e.remove = () => {
      e.parentNode.childNodes = e.parentNode.childNodes.filter((c) => c !== e);
    };
  }
  stripSeenIt(doc);
  const left = all(doc);
  assert.ok(!left.some((e) => anc.ancIsSeenItNode(e)), 'a Seen It element survived');
  assert.ok(left.some((e) => /nsi-ruled-out/.test(e.className)), 'the site row Seen It marked must stay');
  assert.ok(left.some((e) => e.id === 'recordFieldsContainer'));
});
