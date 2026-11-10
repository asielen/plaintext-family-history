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
  const scripts = fs.readdirSync(dir).filter((n) => /^content.*\.js$/.test(n));
  // Every site the capture reads: Ancestry, FamilySearch, Find a Grave, Newspapers.com.
  for (const site of ['content-ancestry.js', 'content-familysearch.js', 'content-findagrave.js', 'content.js']) {
    assert.ok(scripts.includes(site), 'Seen It\'s ' + site + ' not read - has seen-it/ moved?');
  }
  for (const f of scripts) {
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
  const fn = /  function seenItSelector\(\) \{[\s\S]*?\n  \}\n  function stripSeenIt\(root\) \{[\s\S]*?\n  \}\n/.exec(content);
  assert.ok(fn, 'seenItSelector / stripSeenIt not found in content.js');
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

// ── a photo someone shared to their tree (the media viewer) ──────────────────
// The page: tests/fixtures/capture-samples/ancestry-media-photo.html, a
// synthetic viewer page (the live page's Details markup was not recorded, so
// it carries the shapes a details panel is built from).

const MEDIA_ID = '7c0ffee0-1234-4abc-8def-0123456789ab';
const PHOTO_URL = 'https://www.ancestry.com/mediaui-viewer/tree/90000001/person/90000002/media/' + MEDIA_ID;

test('ancParseMediaPage: a shared photo is its media id; the address kept has no query', () => {
  assert.deepStrictEqual(
    anc.ancParseMediaPage(PHOTO_URL.replace(MEDIA_ID, MEDIA_ID.toUpperCase()) +
      '?destTreeId=90000003&destPersonId=90000004&hid=abc&usePUBJs=true#top'),
    { mediaId: MEDIA_ID, treeId: '90000001', personId: '90000002', url: PHOTO_URL });
  // Another Ancestry site keeps its own address; a trailing slash is the same photo.
  assert.strictEqual(anc.ancParseMediaPage('https://www.ancestry.co.uk/mediaui-viewer/tree/1/person/2/media/' +
    MEDIA_ID + '/').url, 'https://www.ancestry.co.uk/mediaui-viewer/tree/1/person/2/media/' + MEDIA_ID);
  for (const u of [
    'https://www.ancestry.com/mediaui-viewer/tree/1/person/2/media/not-a-guid',
    'https://www.ancestry.com/mediaui-viewer/tree/1/person/2/media/' + MEDIA_ID + '/comments',
    'https://www.ancestry.com/family-trees/1/people/2/hints',
    'https://www.ancestry.com/search/collections/90001/records/1000001',
    'https://example.org/mediaui-viewer/tree/1/person/2/media/' + MEDIA_ID,
    'not a url', '', null,
  ]) {
    assert.strictEqual(anc.ancParseMediaPage(u), null, String(u));
  }
});

test('ancMediaImageUrl: this photo at the viewer\'s size, on the page\'s own site; never another photo', () => {
  const doc = page('ancestry-media-photo');
  assert.strictEqual(anc.ancMediaImageUrl(doc, MEDIA_ID, 'https://www.ancestry.com'),
    'https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/4242/media/' + MEDIA_ID +
    '?client=trees-mediaservice&imageQuality=hq');
  // Only a thumbnail on the page: its namespace still names the full image.
  const thumbOnly = parse('<img src="https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/77/media/' +
    MEDIA_ID + '.jpg?client=trees-mediaservice&MaxSide=160">');
  assert.strictEqual(anc.ancMediaImageUrl(thumbOnly, MEDIA_ID, 'https://www.ancestry.com'),
    'https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/77/media/' + MEDIA_ID +
    '?client=trees-mediaservice&imageQuality=hq');
  // An image served from elsewhere is still fetched from the page's own site.
  const elsewhere = parse('<img src="https://cdn.example.net/api/media/retrieval/v2/image/namespaces/5/media/' +
    MEDIA_ID + '">');
  assert.match(anc.ancMediaImageUrl(elsewhere, MEDIA_ID, 'https://www.ancestry.com'), /^https:\/\/www\.ancestry\.com\//);
  // Another photo's picture is never this one's; no picture yet is null.
  assert.strictEqual(anc.ancMediaImageUrl(doc, '1badf00d-5678-4def-9abc-ba9876543210', 'https://www.ancestry.com'),
    'https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/4242/media/1badf00d-5678-4def-9abc-ba9876543210' +
    '?client=trees-mediaservice&imageQuality=hq'); // (its own thumbnail is on the page)
  assert.strictEqual(anc.ancMediaImageUrl(parse('<img src="/x.jpg">'), MEDIA_ID, 'https://www.ancestry.com'), null);
});

test('ancMediaDetails reads the photo\'s Details, and never the "Saved by" list or Seen It\'s badges', () => {
  const details = anc.ancMediaDetails(page('ancestry-media-photo'));
  assert.deepStrictEqual(details, {
    title: 'Thomas and Margaret Hartley at the Fairview farm, about 1925',
    date: 'abt 1925',
    place: 'Fairview, Breton, Kansas, USA',
    description: 'Thomas and Margaret on the porch of the farmhouse.\nThe dog is Shep.',
    originallyAddedBy: 'hartley_family_tree on 3 Mar 2015',
    linkedTo: ['Thomas Hartley', 'Margaret Hartley'],
  });
  const all = JSON.stringify(details);
  for (const never of ['cousin_member_one', 'cousin_member_two', 'Saved by', 'Seen 1×', 'Show more', 'Private']) {
    assert.ok(!all.includes(never), 'kept from the page: ' + never);
  }
  // A closed Details panel (hidden on the live page) gives nothing, not a guess.
  const doc = page('ancestry-media-photo');
  assert.deepStrictEqual(anc.ancMediaDetails(doc, { hidden: (el) => el.id === 'mediaDetails' }), {});
});

// The page as the browser hands it over: childNodes is a NodeList - it can
// be iterated and indexed, but has no indexOf, filter or map.
function liveLike(node) {
  const kids = node.childNodes || [];
  for (const c of kids) liveLike(c);
  const list = { length: kids.length, item: (i) => kids[i] || null, [Symbol.iterator]: () => kids[Symbol.iterator]() };
  kids.forEach((c, i) => { list[i] = c; });
  node.childNodes = list;
  return node;
}

test('the photo\'s readers work on the live page\'s NodeLists, not only on arrays', () => {
  const want = anc.ancMediaFrom(page('ancestry-media-photo'), PHOTO_URL, { origin: 'https://www.ancestry.com' });
  const live = anc.ancMediaFrom(liveLike(page('ancestry-media-photo')), PHOTO_URL, { origin: 'https://www.ancestry.com' });
  assert.deepStrictEqual(live, want);
  assert.strictEqual(live.details.date, 'abt 1925');
});

test('ancMediaFrom + ancMediaHints: the photo, its details, and a date only when the archive can read it', () => {
  const doc = page('ancestry-media-photo');
  const r = anc.ancMediaFrom(doc, PHOTO_URL + '?hid=1', { title: doc.title, origin: 'https://www.ancestry.com' });
  assert.strictEqual(r.ok, true);
  assert.deepStrictEqual(r.media, {
    mediaId: MEDIA_ID, treeId: '90000001', personId: '90000002', url: PHOTO_URL,
    title: 'Thomas and Margaret Hartley at the Fairview farm, about 1925',
    imageUrl: 'https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/4242/media/' + MEDIA_ID +
      '?client=trees-mediaservice&imageQuality=hq',
    fileStem: 'ancestry-' + MEDIA_ID + '-photo',
  });
  assert.deepStrictEqual(anc.ancMediaHints(r.details), { sourceType: 'photo', sourceDate: '1925~', sourceClass: null });
  assert.strictEqual(anc.ancMediaHints({ date: 'summer, the year the barn burned' }).sourceDate, null);
  assert.strictEqual(anc.ancMediaHints({}).sourceDate, null);
  // No Details on the page: the tab title is the photo's title, without the site's name.
  const bare = anc.ancMediaFrom(parse('<p>x</p>'), PHOTO_URL, { title: 'Hartley farm | Ancestry' });
  assert.strictEqual(bare.media.title, 'Hartley farm');
  assert.deepStrictEqual(bare.details, {});
  assert.strictEqual(anc.ancMediaFrom(doc, 'https://www.ancestry.com/family-trees/1/people/2/hints').ok, false);
});

test('ancPhotoFileStem: the photo library name is the media id, ending in a word the archive reads as no print or back', () => {
  assert.strictEqual(anc.ancPhotoFileStem(MEDIA_ID.toUpperCase()), 'ancestry-' + MEDIA_ID + '-photo');
  for (const bad of ['../x', 'Thomas Hartley', '', null, MEDIA_ID + '/x']) {
    assert.strictEqual(anc.ancPhotoFileStem(bad), null, String(bad));
  }
  // A media id ending in a digit and a letter would read as print "b" on its own.
  assert.match(anc.ancPhotoFileStem('00000000-0000-4000-8000-00000000000b'), /-photo$/);
});

test('ancLinkedNames: names without their years, never "Private"', () => {
  assert.deepStrictEqual(anc.ancLinkedNames('Thomas Hartley 1840–1910\nMargaret Hartley (1845-1920)\nPrivate\n' +
    'Ethel Hartley 1890–\nThomas Hartley 1840-1910'), ['Thomas Hartley', 'Margaret Hartley', 'Ethel Hartley']);
  assert.deepStrictEqual(anc.ancLinkedNames(''), []);
});

// ── PR #258 review: other members' names, and a label elsewhere on the page ───

const details = (html) => anc.ancMediaDetails(parse('<html><body>' + html + '</body></html>'));

test('PR #258 review: a "Saved by" list, however it is headed, never reads as linked people or description', () => {
  const linked = '<aside><h3>Linked to</h3><ul><li>Thomas Hartley 1840–1910</li></ul>';
  for (const heading of ['Saved by', 'Saved by 2 members', 'Saved by (2)', 'Saved by:', 'Also saved by',
    'Saved to 3 trees', 'Members who saved this photo', '2 other members who saved this', 'Comments (4)']) {
    const d = details(linked + '<h3>' + heading + '</h3><ul><li>cousin_member_one</li><li>cousin_member_two</li></ul></aside>');
    assert.deepStrictEqual(d, { linkedTo: ['Thomas Hartley'] }, heading);
  }
  assert.deepStrictEqual(details('<dl><dt>Description</dt><dd>On the porch.</dd><dt>Saved by (2)</dt>' +
    '<dd>cousin_member_one</dd></dl>'), { description: 'On the porch.' });
  assert.deepStrictEqual(details('<div><span>Description</span></div><div>On the porch.</div>' +
    '<div>Saved by cousin_member_one</div>'), { description: 'On the porch.' });
  assert.deepStrictEqual(details('<p>Originally added by <a>hartley_tree</a> on 3 Mar 2015</p>' +
    '<p>Saved by <a>cousin_member_one</a> and 3 others</p>'), { originallyAddedBy: 'hartley_tree on 3 Mar 2015' });
});

test('PR #258 review: an empty label elsewhere on the page (a search form) does not hide the photo\'s own Details', () => {
  const form = '<form class="quick-search"><label>Location</label><input name="loc"><label>Date</label><input name="d"></form>';
  assert.deepStrictEqual(details(form + '<aside><dl><dt>Date</dt><dd>abt 1925</dd><dt>Place</dt>' +
    '<dd>Fairview, Kansas</dd></dl></aside>'), { date: 'abt 1925', place: 'Fairview, Kansas' });
  // A label with a value elsewhere loses to the photo's own Details box.
  assert.deepStrictEqual(details('<div class="search-bar"><span>Location</span><span>Anywhere</span></div>' +
    '<section class="media-panel"><h2>Details</h2><dl><dt>Location</dt><dd>Fairview, Kansas</dd></dl></section>'),
  { place: 'Fairview, Kansas' });
});

test('PR #258 review: linked people lose their "abt" years, and a living person is no name', () => {
  assert.deepStrictEqual(details('<dl><dt>Linked to</dt><dd>Thomas Hartley abt 1840–1910<br>Living Hartley<br>' +
    'Ethel Hartley (bef 1890–?)</dd></dl>'), { linkedTo: ['Thomas Hartley', 'Ethel Hartley'] });
});

// ── PR #258 round 3/4 ────────────────────────────────────────────────────────
// No copy of the page is kept for a shared photo (round 4), so what keeps
// other members' names out of the archive is this reader: it stops at every
// members-list label, and keeps only what it extracts.

test('round 3/4: the reader stops at a "Saved by" list however its members are laid out, and keeps none of them', () => {
  for (const rows of ['<div>cousin_member_one</div><div>cousin_member_two</div>',
    '<div>cousin_member_one<br>Joined 2015</div><div>Cousin Member Two Jr.</div>',
    '<div>cousin member with a very long display name that goes on past sixty characters</div>',
    '<ul><li>cousin_member_one</li></ul><p>Hartley farm, 1925</p>',
    '<dl><dd>cousin_member_one</dd><dd>cousin_member_two</dd></dl>']) {
    const d = details('<aside><h2>Details</h2><h3>Description</h3><p>On the porch.</p><h3>Linked to</h3>' +
      '<ul><li>Thomas Hartley</li></ul><h3>Saved by 2 members</h3>' + rows + '</aside>');
    assert.deepStrictEqual(d, { description: 'On the porch.', linkedTo: ['Thomas Hartley'] }, rows);
  }
});

test('round 3 (audit A): a family sentence starting "Saved by" is the description, kept in the notes', () => {
  const html = '<aside id="mediaDetails"><dl><dt>Date</dt><dd>abt 1925</dd></dl><section><h3>Description</h3>' +
    '<p>Thomas and Margaret on the porch.</p><p>Saved by Aunt Ethel from the 1938 farmhouse fire.</p>' +
    '<p>The dog is Shep.</p></section><section><h3>Saved by</h3><ul><li>cousin_member_one</li></ul></section></aside>';
  assert.strictEqual(details(html).description,
    'Thomas and Margaret on the porch.\nSaved by Aunt Ethel from the 1938 farmhouse fire.\nThe dog is Shep.');
  assert.ok(!JSON.stringify(details(html)).includes('cousin_member_one'));
  // A member's username in plain text is still a members line.
  assert.deepStrictEqual(details('<div><span>Description</span></div><div>On the porch.</div>' +
    '<div>Saved by cousin_member_one</div>'), { description: 'On the porch.' });
});

test('round 3 (audit D): a linked person\'s "person-details" card is not the photo\'s Details box', () => {
  assert.deepStrictEqual(details('<aside id="photoInfo"><dl><dt>Date</dt><dd>abt 1925</dd><dt>Place</dt>' +
    '<dd>Fairview, Kansas</dd></dl></aside><div class="person-details"><h4>Thomas Hartley</h4><dl><dt>Date</dt>' +
    '<dd>1840</dd><dt>Place</dt><dd>Yorkshire, England</dd></dl></div>'), { date: 'abt 1925', place: 'Fairview, Kansas' });
  // A box labelled "Details" is read first, wherever it sits.
  assert.deepStrictEqual(details('<div class="person-details"><h4>Thomas Hartley</h4><dl><dt>Date</dt><dd>1840</dd>' +
    '</dl></div><aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>'), { date: 'abt 1925' });
});

test('round 4 (Codex PRRT_kwDOS6vm6c61xYYi): a field the photo\'s Details box does not give stays unknown', () => {
  // The photo's own box gives only its title; a linked relative's card says 1840 and Yorkshire.
  const html = '<aside><h2>Details</h2><dl><dt>Title</dt><dd>Thomas and Margaret on the porch</dd></dl></aside>' +
    '<div class="person-details"><h4>Thomas Hartley</h4><dl><dt>Date</dt><dd>1840</dd><dt>Place</dt>' +
    '<dd>Yorkshire, England</dd><dt>Description</dt><dd>Born on the moors.</dd></dl></div>';
  const d = details(html);
  assert.deepStrictEqual(d, { title: 'Thomas and Margaret on the porch' });
  assert.strictEqual(anc.ancMediaHints(d).sourceDate, null);
  // A label at the end of the box with nothing after it does not read on past the box.
  assert.deepStrictEqual(details('<div><aside><h2>Details</h2><div><span>Date</span></div></aside><p>1840</p></div>'), {});
  // Who it is linked to and who added it may sit beside the box.
  assert.deepStrictEqual(details('<aside><h2>Details</h2><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>' +
    '<section><h3>Linked to</h3><ul><li>Thomas Hartley</li></ul></section>'),
  { date: 'abt 1925', linkedTo: ['Thomas Hartley'] });
  // No Details box at all: the page is read in order, as before.
  assert.deepStrictEqual(details('<dl><dt>Date</dt><dd>abt 1925</dd></dl>'), { date: 'abt 1925' });
});

// ── PR #258 round 5 ──────────────────────────────────────────────────────────

test('round 5 (Codex PRRT_kwDOS6vm6c61yqx6): an upload line ends a field - an empty description never reads it', () => {
  const d = details('<aside><h2>Details</h2><dl><dt>Description</dt><dd></dd></dl>' +
    '<p>Originally added by <a>cousin_member</a> on 3 Mar 2015</p></aside>');
  assert.deepStrictEqual(d, { originallyAddedBy: 'cousin_member on 3 Mar 2015' });
  // Every attribution form is a boundary, with no Details box too.
  for (const line of ['Added by', 'Shared by', 'Uploaded by', 'Originally added by']) {
    assert.deepStrictEqual(details('<div><span>Description</span></div><p>' + line + ' <a>cousin_member</a></p>'),
      { originallyAddedBy: 'cousin_member' }, line);
  }
});

test('round 5 (audit K): a description beside the Details box, in its panel, is kept; a linked person\'s card is not read', () => {
  assert.deepStrictEqual(details('<aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd><dt>Place</dt>' +
    '<dd>Fairview, Kansas</dd></dl></aside><section><h3>Description</h3><p>Thomas and Margaret on the porch.</p></section>'),
  { date: 'abt 1925', place: 'Fairview, Kansas', description: 'Thomas and Margaret on the porch.' });
  assert.deepStrictEqual(details('<aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>' +
    '<div class="person-details"><h4>Thomas Hartley</h4><dl><dt>Description</dt><dd>Born on the moors.</dd></dl></div>'),
  { date: 'abt 1925' });
  // Nor a card's "Attached to" list as the photo's linked people.
  assert.deepStrictEqual(details('<div class="person-card"><h4>Thomas Hartley</h4><dl><dt>Attached to</dt>' +
    '<dd>Margaret Whitcombe (wife)<br>cousin_member_one</dd></dl></div>' +
    '<aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>'), { date: 'abt 1925' });
});

test('round 5 (audit): a commenter is never recorded as who added the photo; "Originally added by" wins', () => {
  assert.deepStrictEqual(details('<section class="comments"><h3>Comments (1)</h3><div><p>Shared by ' +
    '<a>cousin_member_two</a> on 4 Apr 2019</p><p>Lovely photo!</p></div></section>' +
    '<aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>' +
    '<p class="attribution">Originally added by <a>hartley_family_tree</a> on 3 Mar 2015</p>'),
  { date: 'abt 1925', originallyAddedBy: 'hartley_family_tree on 3 Mar 2015' });
  // A plain "Shared by" read first gives way to "Originally added by".
  assert.deepStrictEqual(details('<p>Shared by <a>tree_owner_two</a></p><p>Originally added by <a>hartley_family_tree</a></p>'),
    { originallyAddedBy: 'hartley_family_tree' });
  // Comments without a Details box are not read either.
  assert.deepStrictEqual(details('<section><h3>Comments (2)</h3><p>Shared by <a>cousin_member_two</a></p>' +
    '<h3>Description</h3><p>A note from a member</p></section>'), {});
});

// ── PR #258 round 6 ──────────────────────────────────────────────────────────

test('round 6: a sentence that starts "Shared by" / "Added by" / "Uploaded by" is the description, not an upload line', () => {
  const box = (inner) => details('<aside><h2>Details</h2>' + inner +
    '<p class="attribution">Originally added by <a>hartley_family_tree</a> on 3 Mar 2015</p></aside>');
  assert.deepStrictEqual(box('<dl><dt>Date</dt><dd>abt 1925</dd><dt>Description</dt><dd>Shared by Aunt Ethel Hartley ' +
    'at the 1950 reunion; she is the girl on the left.</dd></dl>'), {
    date: 'abt 1925', originallyAddedBy: 'hartley_family_tree on 3 Mar 2015',
    description: 'Shared by Aunt Ethel Hartley at the 1950 reunion; she is the girl on the left.' });
  assert.strictEqual(box('<dl><dt>Date</dt><dd>abt 1925</dd><dt>Description</dt><dd><p>Thomas and Margaret on the ' +
    'porch of the farmhouse.</p><p>Added by Grandma Ruth to her album in 1960.</p></dd></dl>').description,
  'Thomas and Margaret on the porch of the farmhouse.\nAdded by Grandma Ruth to her album in 1960.');
  assert.strictEqual(box('<section><h3>Description</h3><p>Thomas and Margaret on the porch.</p><p>Uploaded by me from ' +
    'Mother\'s album.</p><p>The dog is Shep, who lived to 1934.</p></section>').description,
  'Thomas and Margaret on the porch.\nUploaded by me from Mother\'s album.\nThe dog is Shep, who lived to 1934.');
  // The real upload line - a link, or one username, and a date - still ends a field (Codex PRRT_kwDOS6vm6c61yqx6).
  assert.deepStrictEqual(details('<aside><h2>Details</h2><dl><dt>Description</dt><dd></dd></dl>' +
    '<p>Originally added by <a>cousin_member</a> on 3 Mar 2015</p></aside>'), { originallyAddedBy: 'cousin_member on 3 Mar 2015' });
  assert.deepStrictEqual(details('<aside><h2>Details</h2><dl><dt>Description</dt><dd></dd></dl>' +
    '<p>Added by cousin_member_4 on 4 Apr 2019</p></aside>'), { originallyAddedBy: 'cousin_member_4 on 4 Apr 2019' });
});

test('round 6 (audit N): a "Linked to" or "Description" beside the box is read whatever its wrapper is called', () => {
  const panel = (beside) => details('<div class="panel"><aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl>' +
    '</aside>' + beside + '</div>');
  assert.deepStrictEqual(panel('<section class="linked-people"><h3>Linked to</h3><ul><li>Thomas Hartley 1840–1910</li>' +
    '</ul></section>'), { date: 'abt 1925', linkedTo: ['Thomas Hartley'] });
  assert.deepStrictEqual(panel('<section class="card"><h3>Description</h3><p>Thomas and Margaret on the porch.</p></section>'),
    { date: 'abt 1925', description: 'Thomas and Margaret on the porch.' });
  // A section headed by anything else is left out whole (round 7): its
  // description is the accepted cost; it can still be read on Ancestry.
  assert.deepStrictEqual(panel('<section><h3>About this photo</h3><h4>Description</h4><p>Thomas and Margaret on the ' +
    'porch.</p></section>'), { date: 'abt 1925' });
  // A person's card - headed by a name - is never read.
  assert.deepStrictEqual(panel('<div class="person-card"><h4>Thomas Hartley</h4><dl><dt>Description</dt>' +
    '<dd>Born on the moors.</dd><dt>Attached to</dt><dd>cousin_member_one</dd></dl></div>'), { date: 'abt 1925' });
});

// ── PR #258 round 7: leave text out rather than file someone else's words ───

test('round 7: a line starting "Originally added by" is always the uploader, never a description', () => {
  const box = (line) => details('<aside aria-label="Details"><h3>Description</h3><p>' + line + '</p></aside>');
  assert.deepStrictEqual(box('Originally added by Ruth Hartley on 3 Mar 2015'),
    { originallyAddedBy: 'Ruth Hartley on 3 Mar 2015' });
  assert.deepStrictEqual(box('Originally added by <a>hartley_family_tree</a> (Ruth Hartley) on 3 Mar 2015'),
    { originallyAddedBy: 'hartley_family_tree (Ruth Hartley) on 3 Mar 2015' });
  const empty = (line) => details('<aside><h2>Details</h2><dl><dt>Description</dt><dd></dd></dl><p>' + line + '</p></aside>');
  assert.deepStrictEqual(empty('Originally added by <span class="user-name">Mary Jones</span> on 3 Mar 2015'),
    { originallyAddedBy: 'Mary Jones on 3 Mar 2015' });
  assert.deepStrictEqual(empty('Originally added by Mary Jones on 3 Mar 2015'), { originallyAddedBy: 'Mary Jones on 3 Mar 2015' });
  // Without "Originally", a sentence stays the description (round 6's A, B, C still hold).
  assert.strictEqual(box('Shared by Aunt Ethel Hartley at the 1950 reunion; she is the girl on the left.').description,
    'Shared by Aunt Ethel Hartley at the 1950 reunion; she is the girl on the left.');
});

test('round 7: beside the box, only a section headed by one of the photo\'s fields is read - no class-word guessing', () => {
  const main = (html) => details('<main>' + html + '</main>');
  // A relative's card, whatever its class: nothing from it.
  assert.deepStrictEqual(main('<aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>' +
    '<div class="css-1q2w3e"><h4>Thomas Hartley</h4><dl><dt>Description</dt><dd>Born on the moors of Yorkshire.</dd></dl></div>'),
  { date: 'abt 1925' });
  assert.deepStrictEqual(main('<div class="relative-card"><h4>Thomas Hartley</h4><dl><dt>Attached to</dt>' +
    '<dd>Margaret Whitcombe (wife)<br>cousin_member_one</dd></dl></div>' +
    '<aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>'), { date: 'abt 1925' });
  // A member's card headed by a username: no uploader from it.
  assert.deepStrictEqual(main('<aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>' +
    '<div class="member-card"><h4>cousin_member_one</h4><p>Shared by <a>cousin_member_one</a></p></div>'), { date: 'abt 1925' });
  // A lone upload line beside the box, in no such section, is still the uploader.
  assert.deepStrictEqual(main('<aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>' +
    '<p class="attribution">Originally added by <a>hartley_family_tree</a> on 3 Mar 2015</p>'),
  { date: 'abt 1925', originallyAddedBy: 'hartley_family_tree on 3 Mar 2015' });
  // Comments are still never read.
  assert.deepStrictEqual(main('<aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>' +
    '<section><h3>Comments (1)</h3><p>Shared by <a>cousin_member_two</a></p></section>'), { date: 'abt 1925' });
});

// ── PR #258 round 8: one rule - a section is read when its first visible text is a field label ──

const R8_BOX = '<aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>';

test('round 8 (R1, R2): a relative\'s card whose name is a link or bold type is left out whole', () => {
  assert.deepStrictEqual(details('<main>' + R8_BOX + '<div class="css-9z8y"><a href="/family-tree/person/1">Thomas ' +
    'Hartley</a><span>1840–1910</span><dl><dt>Description</dt><dd>Born on the moors of Yorkshire.</dd></dl></div></main>'),
  { date: 'abt 1925' });
  assert.deepStrictEqual(details('<main>' + R8_BOX + '<div class="css-9z8y"><strong>Thomas Hartley</strong><dl>' +
    '<dt>Attached to</dt><dd>Margaret Whitcombe (wife)<br>cousin_member_one</dd></dl></div></main>'), { date: 'abt 1925' });
});

test('round 8 (R4): with no Details box, each section is read by the same rule - never a relative\'s card', () => {
  assert.deepStrictEqual(details('<main><dl><dt>Date</dt><dd>abt 1925</dd></dl><div class="css-1q2w3e"><h4>Thomas ' +
    'Hartley</h4><dl><dt>Description</dt><dd>Born on the moors of Yorkshire.</dd></dl></div></main>'), { date: 'abt 1925' });
  // The photo's own labelled sections are still read.
  assert.deepStrictEqual(details('<main><header><nav><a>Trees</a><a>Search</a></nav></header><section><h3>Description</h3>' +
    '<p>Thomas and Margaret on the porch.</p></section><section><h3>Linked to</h3><ul><li>Thomas Hartley 1840–1910</li>' +
    '</ul></section></main>'), { description: 'Thomas and Margaret on the porch.', linkedTo: ['Thomas Hartley'] });
});

test('round 8: what the one rule still keeps - N\'s linked people, K\'s description, the family sentences', () => {
  const panel = (beside) => details('<div class="panel">' + R8_BOX + beside + '</div>');
  assert.deepStrictEqual(panel('<section class="linked-people"><h3>Linked to</h3><ul><li>Thomas Hartley 1840–1910</li>' +
    '</ul></section>'), { date: 'abt 1925', linkedTo: ['Thomas Hartley'] });
  assert.deepStrictEqual(panel('<section><h3>Description</h3><p>Thomas and Margaret on the porch.</p></section>'),
    { date: 'abt 1925', description: 'Thomas and Margaret on the porch.' });
  const box = (inner) => details('<aside><h2>Details</h2>' + inner + '</aside>');
  assert.strictEqual(box('<dl><dt>Description</dt><dd>Shared by Aunt Ethel Hartley at the 1950 reunion; she is the girl ' +
    'on the left.</dd></dl>').description, 'Shared by Aunt Ethel Hartley at the 1950 reunion; she is the girl on the left.');
});

test('round 8: a section headed by the upload label gives only the first name - no member list, no sentence after it', () => {
  const beside = (section) => details('<div>' + R8_BOX + section + '</div>');
  assert.deepStrictEqual(beside('<section><h3>Originally added by</h3><p><a>hartley_family_tree</a> on 3 Mar 2015</p>' +
    '<p>Ruth Hartley wrote: this is Grandpa Tom with his brother.</p></section>'),
  { date: 'abt 1925', originallyAddedBy: 'hartley_family_tree on 3 Mar 2015' });
  assert.deepStrictEqual(beside('<section><h3>Originally added by</h3><ul><li><a>hartley_family_tree</a></li>' +
    '<li><a>cousin_member_one</a></li><li><a>cousin_member_two</a></li></ul></section>'),
  { date: 'abt 1925', originallyAddedBy: 'hartley_family_tree' });
});

test('round 8 (R3): "Added by <Name> on <date>" is an upload line; "on her 90th birthday" is not a date', () => {
  const empty = (line) => details('<aside><h2>Details</h2><dl><dt>Description</dt><dd></dd></dl><p>' + line + '</p></aside>');
  assert.deepStrictEqual(empty('Added by <span class="user-name">Mary Jones</span> on 3 Mar 2015'),
    { originallyAddedBy: 'Mary Jones on 3 Mar 2015' });
  assert.deepStrictEqual(empty('Shared by Mary Hartley Jones on March 3, 2015'),
    { originallyAddedBy: 'Mary Hartley Jones on March 3, 2015' });
  const desc = (line) => details('<aside><h2>Details</h2><section><h3>Description</h3><p>' + line + '</p></section></aside>');
  assert.strictEqual(desc('Shared by Aunt Ethel on her 90th birthday').description, 'Shared by Aunt Ethel on her 90th birthday');
  assert.strictEqual(desc('Shared by <a>Aunt Ethel</a> on her 90th birthday').description,
    'Shared by Aunt Ethel on her 90th birthday');
  assert.strictEqual(desc('Added by Grandma Ruth to her album in 1960.').description, 'Added by Grandma Ruth to her album in 1960.');
});
