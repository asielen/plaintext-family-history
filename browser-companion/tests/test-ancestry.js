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

// ── The photo's Details: read from the photo's own Details box only ─────────
// PR #258 rounds 3-9. Round 9 (a reversible decision under the owner's
// principle): fields come only from the photo's own Details box - the
// element whose own heading or aria-label says "Details" - read whole. When
// the page's layout is unclear, the add-on leaves text out rather than file
// someone else's words as hers; anything left out can still be read on
// Ancestry from the photo's saved address. Tests below marked "accepted cost"
// pin text that is now left out on purpose.

const details = (html) => anc.ancMediaDetails(parse('<html><body>' + html + '</body></html>'));
const inBox = (inner) => details('<aside aria-label="Details">' + inner + '</aside>');
const BOX = '<aside aria-label="Details"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>';

test('in the box: a "Saved by" list, however it is headed, never reads as linked people or description', () => {
  const linked = '<h3>Linked to</h3><ul><li>Thomas Hartley 1840–1910</li></ul>';
  for (const heading of ['Saved by', 'Saved by 2 members', 'Saved by (2)', 'Saved by:', 'Also saved by',
    'Saved to 3 trees', 'Members who saved this photo', '2 other members who saved this', 'Comments (4)']) {
    assert.deepStrictEqual(inBox(linked + '<h3>' + heading + '</h3><ul><li>cousin_member_one</li><li>cousin_member_two</li></ul>'),
      { linkedTo: ['Thomas Hartley'] }, heading);
  }
  assert.deepStrictEqual(inBox('<dl><dt>Description</dt><dd>On the porch.</dd><dt>Saved by (2)</dt><dd>cousin_member_one</dd></dl>'),
    { description: 'On the porch.' });
  assert.deepStrictEqual(inBox('<p>Originally added by <a>hartley_tree</a> on 3 Mar 2015</p>' +
    '<p>Saved by <a>cousin_member_one</a> and 3 others</p>'), { originallyAddedBy: 'hartley_tree on 3 Mar 2015' });
});

test('in the box: member rows in any layout are never kept', () => {
  for (const rows of ['<div>cousin_member_one</div><div>cousin_member_two</div>',
    '<div>cousin_member_one<br>Joined 2015</div><div>Cousin Member Two Jr.</div>',
    '<div>cousin member with a very long display name that goes on past sixty characters</div>',
    '<ul><li>cousin_member_one</li></ul><p>Hartley farm, 1925</p>',
    '<dl><dd>cousin_member_one</dd><dd>cousin_member_two</dd></dl>']) {
    assert.deepStrictEqual(inBox('<h3>Description</h3><p>On the porch.</p><h3>Linked to</h3><ul><li>Thomas Hartley</li>' +
      '</ul><h3>Saved by 2 members</h3>' + rows), { description: 'On the porch.', linkedTo: ['Thomas Hartley'] }, rows);
  }
});

test('in the box: linked people lose their years, and a living person is no name', () => {
  assert.deepStrictEqual(inBox('<dl><dt>Linked to</dt><dd>Thomas Hartley abt 1840–1910<br>Living Hartley<br>' +
    'Ethel Hartley (bef 1890–?)</dd></dl>'), { linkedTo: ['Thomas Hartley', 'Ethel Hartley'] });
});

test('in the box: family sentences stay the description (A, B, C, and one starting "Saved by")', () => {
  const box = (inner) => details('<aside><h2>Details</h2>' + inner +
    '<p class="attribution">Originally added by <a>hartley_family_tree</a> on 3 Mar 2015</p></aside>');
  assert.strictEqual(box('<dl><dt>Date</dt><dd>abt 1925</dd><dt>Description</dt><dd>Shared by Aunt Ethel Hartley at the ' +
    '1950 reunion; she is the girl on the left.</dd></dl>').description,
  'Shared by Aunt Ethel Hartley at the 1950 reunion; she is the girl on the left.');
  assert.strictEqual(box('<dl><dt>Description</dt><dd><p>Thomas and Margaret on the porch of the farmhouse.</p>' +
    '<p>Added by Grandma Ruth to her album in 1960.</p></dd></dl>').description,
  'Thomas and Margaret on the porch of the farmhouse.\nAdded by Grandma Ruth to her album in 1960.');
  assert.strictEqual(box('<section><h3>Description</h3><p>Thomas and Margaret on the porch.</p><p>Uploaded by me from ' +
    'Mother\'s album.</p><p>The dog is Shep, who lived to 1934.</p></section>').description,
  'Thomas and Margaret on the porch.\nUploaded by me from Mother\'s album.\nThe dog is Shep, who lived to 1934.');
  const html = '<dl><dt>Date</dt><dd>abt 1925</dd></dl><section><h3>Description</h3><p>Thomas and Margaret on the porch.</p>' +
    '<p>Saved by Aunt Ethel from the 1938 farmhouse fire.</p><p>The dog is Shep.</p></section><section><h3>Saved by</h3>' +
    '<ul><li>cousin_member_one</li></ul></section>';
  assert.strictEqual(inBox(html).description,
    'Thomas and Margaret on the porch.\nSaved by Aunt Ethel from the 1938 farmhouse fire.\nThe dog is Shep.');
  assert.ok(!JSON.stringify(inBox(html)).includes('cousin_member_one'));
  // A member's username in plain text is still a members line.
  assert.deepStrictEqual(inBox('<div><span>Description</span></div><div>On the porch.</div>' +
    '<div>Saved by cousin_member_one</div>'), { description: 'On the porch.' });
});

test('in the box: a field it does not give stays unknown, and no value is read past the box', () => {
  assert.deepStrictEqual(details('<aside><h2>Details</h2><dl><dt>Title</dt><dd>Thomas and Margaret on the porch</dd></dl>' +
    '</aside><div class="person-details"><h4>Thomas Hartley</h4><dl><dt>Date</dt><dd>1840</dd><dt>Place</dt>' +
    '<dd>Yorkshire, England</dd></dl></div>'), { title: 'Thomas and Margaret on the porch' });
  assert.deepStrictEqual(details('<div><aside><h2>Details</h2><div><span>Date</span></div></aside><p>1840</p></div>'), {});
  // A search form's empty labels outside the box change nothing.
  assert.deepStrictEqual(details('<form><label>Location</label><input name="loc"><label>Date</label><input name="d"></form>' +
    '<aside><h2>Details</h2><dl><dt>Date</dt><dd>abt 1925</dd><dt>Place</dt><dd>Fairview, Kansas</dd></dl></aside>'),
  { date: 'abt 1925', place: 'Fairview, Kansas' });
});

test('in the box: upload lines (rounds 5-9) - "Originally" always, a shaped plain one, and a linked username with a short tail', () => {
  const empty = (line) => details('<aside><h2>Details</h2><dl><dt>Description</dt><dd></dd></dl><p>' + line + '</p></aside>');
  assert.deepStrictEqual(empty('Originally added by <a>cousin_member</a> on 3 Mar 2015'), { originallyAddedBy: 'cousin_member on 3 Mar 2015' });
  assert.deepStrictEqual(empty('Originally added by <span class="user-name">Mary Jones</span> on 3 Mar 2015'),
    { originallyAddedBy: 'Mary Jones on 3 Mar 2015' });
  assert.deepStrictEqual(empty('Originally added by Mary Jones on 3 Mar 2015'), { originallyAddedBy: 'Mary Jones on 3 Mar 2015' });
  assert.deepStrictEqual(empty('Added by <span class="user-name">Mary Jones</span> on 3 Mar 2015'),
    { originallyAddedBy: 'Mary Jones on 3 Mar 2015' });
  assert.deepStrictEqual(empty('Added by cousin_member_4 on 4 Apr 2019'), { originallyAddedBy: 'cousin_member_4 on 4 Apr 2019' });
  for (const line of ['Added by', 'Shared by', 'Uploaded by', 'Originally added by']) {
    assert.deepStrictEqual(inBox('<div><span>Description</span></div><p>' + line + ' <a>cousin_member</a></p>'),
      { originallyAddedBy: 'cousin_member' }, line);
  }
  const desc = (line) => inBox('<h3>Description</h3><p>' + line + '</p>');
  assert.deepStrictEqual(desc('Originally added by Ruth Hartley on 3 Mar 2015'), { originallyAddedBy: 'Ruth Hartley on 3 Mar 2015' });
  assert.deepStrictEqual(desc('Originally added by <a>hartley_family_tree</a> (Ruth Hartley) on 3 Mar 2015'),
    { originallyAddedBy: 'hartley_family_tree (Ruth Hartley) on 3 Mar 2015' });
  // V (round 9): a linked username with a short tail that is no sentence.
  assert.deepStrictEqual(desc('Added by <a>hartley_family_tree</a> 3 years ago'),
    { originallyAddedBy: 'hartley_family_tree 3 years ago' });
  // Still the description: no real date after "on", or a linked display name with a sentence.
  assert.strictEqual(desc('Shared by Aunt Ethel on her 90th birthday').description, 'Shared by Aunt Ethel on her 90th birthday');
  assert.strictEqual(desc('Shared by <a>Aunt Ethel</a> on her 90th birthday').description, 'Shared by Aunt Ethel on her 90th birthday');
  assert.strictEqual(desc('Added by Grandma Ruth to her album in 1960.').description, 'Added by Grandma Ruth to her album in 1960.');
});

test('in the box: "Originally added by" wins; a commenter is never the uploader; an upload section gives its first name only', () => {
  assert.deepStrictEqual(inBox('<p>Shared by <a>tree_owner_two</a></p><p>Originally added by <a>hartley_family_tree</a></p>'),
    { originallyAddedBy: 'hartley_family_tree' });
  assert.deepStrictEqual(inBox('<section><h3>Comments (1)</h3><p>Shared by <a>cousin_member_two</a> on 4 Apr 2019</p></section>' +
    '<dl><dt>Date</dt><dd>abt 1925</dd></dl>'), { date: 'abt 1925' });
  assert.deepStrictEqual(inBox('<section><h3>Originally added by</h3><p><a>hartley_family_tree</a> on 3 Mar 2015</p>' +
    '<p>Ruth Hartley wrote: this is Grandpa Tom with his brother.</p></section>'),
  { originallyAddedBy: 'hartley_family_tree on 3 Mar 2015' });
  assert.deepStrictEqual(inBox('<section><h3>Originally added by</h3><ul><li><a>hartley_family_tree</a></li>' +
    '<li><a>cousin_member_one</a></li></ul></section>'), { originallyAddedBy: 'hartley_family_tree' });
});

test('round 9: nothing beside the box is read - a relative\'s card in any shape gives nothing (N1-N4, R1, R2, C1, C2)', () => {
  const NAME_IN_DIV = '<div class="css-hdr"><a href="/family-tree/person/1">Thomas Hartley</a> <span>1840–1910</span></div>';
  const cards = [
    '<div class="css-card">' + NAME_IN_DIV + '<dl><dt>Description</dt><dd>Born on the moors of Yorkshire.</dd></dl></div>',
    '<div class="css-card">' + NAME_IN_DIV + '<dl><dt>Attached to</dt><dd>Margaret Whitcombe (wife)<br>cousin_member_one</dd></dl></div>',
    '<section><header><h4>Thomas Hartley</h4></header><dl><dt>Description</dt><dd>Born on the moors of Yorkshire.</dd></dl></section>',
    '<div class="css-9z8y"><a href="/family-tree/person/1">Thomas Hartley</a><span>1840–1910</span><dl><dt>Description</dt>' +
      '<dd>Born on the moors of Yorkshire.</dd></dl></div>',
    '<div class="css-9z8y"><strong>Thomas Hartley</strong><dl><dt>Attached to</dt><dd>cousin_member_one</dd></dl></div>',
    '<div class="css-1q2w3e"><h4>Thomas Hartley</h4><dl><dt>Description</dt><dd>Born on the moors of Yorkshire.</dd></dl></div>',
    '<div class="member-card"><h4>cousin_member_one</h4><p>Shared by <a>cousin_member_one</a></p></div>',
  ];
  for (const card of cards) {
    assert.deepStrictEqual(details('<main>' + BOX + card + '</main>'), { date: 'abt 1925' }, card);
    assert.deepStrictEqual(details('<main>' + card + BOX + '</main>'), { date: 'abt 1925' }, card);
  }
});

test('round 9: with no Details box, no field comes from the page body - never a relative\'s 1840 as the photo\'s date (N3)', () => {
  const n3 = details('<main><dl><dt>Title</dt><dd>Hartley farm</dd></dl><div class="css-card"><div class="css-hdr">' +
    '<a>Thomas Hartley</a> <span>1840–1910</span></div><dl><dt>Date</dt><dd>12 Mar 1840</dd><dt>Place</dt>' +
    '<dd>Haworth, Yorkshire, England</dd></dl></div></main>');
  assert.deepStrictEqual(n3, {});
  assert.strictEqual(anc.ancMediaHints(n3).sourceDate, null);
  for (const page of ['<aside class="info"><dl><dt>Date</dt><dd>abt 1925</dd><dt>Place</dt><dd>Fairview, Kansas</dd></dl></aside>',
    '<dl><dt>Date</dt><dd>abt 1925</dd></dl>',
    '<section><h3>Description</h3><p>Thomas and Margaret on the porch.</p></section>',
    '<aside id="photoInfo"><dl><dt>Date</dt><dd>abt 1925</dd></dl></aside>']) {
    assert.deepStrictEqual(details(page), {}, page);
  }
  // The tab's own title still names the photo (ancMediaFrom), never the body.
  const r = anc.ancMediaFrom(parse('<html><body><dl><dt>Title</dt><dd>Body title</dd></dl></body></html>'), PHOTO_URL,
    { title: 'Hartley farm | Ancestry' });
  assert.strictEqual(r.media.title, 'Hartley farm');
});

test('round 9, accepted cost: text beside the box is left out even when it is hers (K, linked people, T, other heading)', () => {
  const panel = (beside) => details('<div class="panel">' + BOX + beside + '</div>');
  for (const beside of [
    '<section><h3>Description</h3><p>Thomas and Margaret on the porch.</p></section>', // K, T's besidePlain
    '<section class="linked-people"><h3>Linked to</h3><ul><li>Thomas Hartley 1840–1910</li></ul></section>', // N's linkedInPeopleBeside
    '<section><h3>About this photo</h3><h4>Description</h4><p>Thomas and Margaret on the porch.</p></section>',
    '<p class="attribution">Originally added by <a>hartley_family_tree</a> on 3 Mar 2015</p>',
  ]) {
    assert.deepStrictEqual(panel(beside), { date: 'abt 1925' }, beside);
  }
});

// ── Ancestry's own Details panel (#mediaViewerDetailsPanel) ─────────────────
// Issue #261: on a real photo page the Details reader found no box - the
// "Details" heading sits in a button in a mobile-only header - so a photo's
// title, uploader and linked people were all dropped. The page:
// tests/fixtures/capture-samples/ancestry-media-panel.html, modelled on a real
// page's panel (fictional Hartleys, invented ids). Read by Ancestry's own
// structure only; what Ancestry's AI estimates is never read as hers.

const PANEL_HTML = fs.readFileSync(path.join(SAMPLES, 'ancestry-media-panel.html'), 'utf8');
const panelPage = (edit) => parse(edit ? edit(PANEL_HTML) : PANEL_HTML);
const OTHERS = ['cousin_member_one', 'cousin_member_two', 'cousin_member_three', 'Saved by', 'Shared by', 'Comments',
  'Save to someone else', '1921', 'grandmother', 'HF', 'CO'];
// One field of the panel, to cut out or swap for something else.
const field = (cls) => new RegExp('<div class="mediaDataFieldWrapper"><div class="mediaDataField flex  "><span aria-label="[^"]*" ' +
  'class="icon ' + cls + '"></span><span class="mediaDataFieldValue[^"]*">[^<]*</span></div></div>');

test('#261: Ancestry\'s own Details panel gives the photo\'s title, date, place, description, uploader and linked people', () => {
  const d = anc.ancMediaDetails(panelPage());
  assert.deepStrictEqual(d, {
    title: 'Wedding of Thomas Hartley and Margaret Ellis',
    date: '12 Jun 1919',
    place: 'Fairview, Breton, Kansas, USA',
    category: 'Portrait / Family Photo',
    description: 'Thomas and Margaret outside the Fairview church.\nHer sister Ethel is on the left.',
    originallyAddedBy: 'hartley_family_tree on 3 Mar 2015',
    linkedTo: ['Thomas Hartley', 'Margaret Hartley'],
  });
  const all = JSON.stringify(d);
  for (const never of OTHERS) assert.ok(!all.includes(never), 'kept from the page: ' + never);
  assert.deepStrictEqual(anc.ancMediaHints(d), { sourceType: 'photo', sourceDate: '1919-06-12', sourceClass: null });
  // The live page's NodeLists read the same.
  assert.deepStrictEqual(anc.ancMediaDetails(liveLike(panelPage())), d);
  // The panel names the photo, whatever the tab's title says.
  const r = anc.ancMediaFrom(panelPage(), PHOTO_URL, { title: 'Other words | Ancestry' });
  assert.strictEqual(r.media.title, 'Wedding of Thomas Hartley and Margaret Ellis');
});

test('#261: a panel with only a title and a category (the owner\'s page) still gives its title, uploader and linked people', () => {
  const html = PANEL_HTML.replace(field('iconCalendar'), '').replace(field('iconMap'), '').replace(field('iconNote'), '');
  assert.ok(!/iconCalendar|iconMap|iconNote/.test(html) && /iconTileLarge/.test(html), 'the fixture edit took');
  const d = anc.ancMediaDetails(parse(html));
  assert.deepStrictEqual(d, {
    title: 'Wedding of Thomas Hartley and Margaret Ellis',
    category: 'Portrait / Family Photo',
    originallyAddedBy: 'hartley_family_tree on 3 Mar 2015',
    linkedTo: ['Thomas Hartley', 'Margaret Hartley'],
  });
  assert.strictEqual(anc.ancMediaHints(d).sourceDate, null);
});

test('#261: a date or description Ancestry\'s AI suggests is never read as hers - the photo stays undated', () => {
  // The estimated date Ancestry draws INSTEAD of an empty date - even one
  // that carries a value span.
  const autodate = panelPage((h) => h.replace(field('iconCalendar'),
    '<div class="mediaDataFieldWrapper autodateField"><div class="mediaDataField flex"><button aria-label="Estimated date: 1920s" ' +
    'class="ancBtn icon iconAi" type="button">1920s</button><span aria-label="Date" class="icon iconCalendar"></span>' +
    '<span class="mediaDataFieldValue textWrap ">1920s</span></div></div>'));
  const a = anc.ancMediaDetails(autodate);
  assert.strictEqual(a.date, undefined);
  assert.ok(!JSON.stringify(a).includes('1920s'));
  assert.strictEqual(anc.ancMediaHints(a).sourceDate, null);
  assert.strictEqual(a.place, 'Fairview, Breton, Kansas, USA');
  // A suggested description, in its insight button.
  const insight = panelPage((h) => h.replace(field('iconNote'),
    '<div class="photoInsightWrapper flex"><span aria-label="Description" class="icon iconNote"></span>' +
    '<button type="button" class="insightBtn ancBtn silver sml longText"><span class="insightIcon icon iconEnhanced"></span>' +
    '<span class="suggestion">A bride and groom stand before a church door.</span>' +
    '<span class="mediaDataFieldValue">A bride and groom stand before a church door.</span></button></div>'));
  const i = anc.ancMediaDetails(insight);
  assert.strictEqual(i.description, undefined);
  assert.ok(!JSON.stringify(i).includes('bride'));
  assert.strictEqual(i.date, '12 Jun 1919');
  // An AI date button standing in a date field.
  const iconAi = panelPage((h) => h.replace('<span class="mediaDataFieldValue textWrap ">12 Jun 1919</span>',
    '<button class="ancBtn icon iconAi" type="button"><span class="mediaDataFieldValue">1920s</span></button>'));
  assert.strictEqual(anc.ancMediaDetails(iconAi).date, undefined);
});

test('#261: on her own photo, Ancestry\'s "add a date" prompt in an empty field is no date', () => {
  const own = panelPage((h) => h.replace('<div class="mediaDataField flex  "><span aria-label="Date" class="icon iconCalendar"></span>' +
    '<span class="mediaDataFieldValue textWrap ">12 Jun 1919</span>',
  '<div class="mediaDataField flex  isEditable" role="button" tabindex="0"><span aria-label="Date" class="icon iconCalendar"></span>' +
    '<span class="mediaDataFieldValue textWrap "><span class="link">add a date</span>' +
    '<span aria-hidden="true" class="icon iconEdit hideIcon"></span></span>'));
  const d = anc.ancMediaDetails(own);
  assert.strictEqual(d.date, undefined);
  assert.ok(!JSON.stringify(d).includes('add a'));
  assert.strictEqual(d.place, 'Fairview, Breton, Kansas, USA');
});

test('#261: on Ancestry in another language, the fields are known by their icons', () => {
  const de = panelPage((h) => h.replace('aria-label="Date"', 'aria-label="Datum"').replace('aria-label="Location"', 'aria-label="Ort"')
    .replace('aria-label="Description"', 'aria-label="Beschreibung"').replace('aria-label="Category"', 'aria-label="Kategorie"'));
  const d = anc.ancMediaDetails(de);
  assert.strictEqual(d.date, '12 Jun 1919');
  assert.strictEqual(d.place, 'Fairview, Breton, Kansas, USA');
  assert.strictEqual(d.description, 'Thomas and Margaret outside the Fairview church.\nHer sister Ethel is on the left.');
  assert.strictEqual(d.category, 'Portrait / Family Photo');
});

test('#261: the panel\'s id is what marks it - without it the old rules apply, and a closed panel gives nothing', () => {
  assert.ok(Object.keys(anc.ancMediaDetails(panelPage())).length > 0);
  // The same markup without Ancestry's id: its "Details" heading is in a
  // button in a mobile-only header, which the old rules do not take as a
  // box's own heading - nothing is read, as before.
  assert.deepStrictEqual(anc.ancMediaDetails(panelPage((h) => h.replace(' id="mediaViewerDetailsPanel"', ''))), {});
  // Another id merely containing it is not it.
  assert.deepStrictEqual(anc.ancMediaDetails(panelPage((h) => h.replace('id="mediaViewerDetailsPanel"',
    'id="mediaViewerDetailsPanelOld"'))), {});
  // Closed (hidden on the live page): nothing, not a guess.
  assert.deepStrictEqual(anc.ancMediaDetails(panelPage(), { hidden: (el) => el.id === 'mediaViewerDetailsPanel' }), {});
  // A hidden part of it is left out, the rest read.
  const noCard = anc.ancMediaDetails(panelPage(), { hidden: (el) => /mediaOwnerCard/.test(el.className) });
  assert.strictEqual(noCard.originallyAddedBy, undefined);
  assert.strictEqual(noCard.date, '12 Jun 1919');
});

// A newspaper clipping shared from Newspapers.com (#261, a second real
// page): tests/fixtures/capture-samples/ancestry-media-clipping.html.
const CLIP_ID = '5eed0000-1234-4abc-8def-0123456789ab';
const CLIP_IMAGE = 'a55e7000-1234-4abc-8def-0123456789ab';
const CLIP_URL = 'https://www.ancestry.com/mediaui-viewer/collection/90003/tree/90000001/person/90000002/media/' + CLIP_ID;
const CLIP_HTML = fs.readFileSync(path.join(SAMPLES, 'ancestry-media-clipping.html'), 'utf8');

test('#261: a clipping whose picture has an image id of its own is fetched by that id, and filed under the photo\'s', () => {
  const doc = parse(CLIP_HTML);
  const want = 'https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/4242/media/' + CLIP_IMAGE +
    '?client=trees-mediaservice&imageQuality=hq';
  assert.strictEqual(anc.ancMediaImageUrl(doc, CLIP_ID, 'https://www.ancestry.com'), want);
  assert.strictEqual(anc.ancMediaImageUrl(doc, CLIP_ID.toUpperCase(), 'https://www.ancestry.com'), want);
  const r = anc.ancMediaFrom(doc, CLIP_URL, { title: doc.title, origin: 'https://www.ancestry.com' });
  assert.strictEqual(r.ok, true);
  assert.deepStrictEqual(r.media, {
    mediaId: CLIP_ID, treeId: '90000001', personId: '90000002', url: CLIP_URL,
    title: 'Obituary of Thomas Hartley', imageUrl: want, fileStem: 'ancestry-' + CLIP_ID + '-photo',
  });
  // Another photo's picture on the page - marked with its own id - is never this one's.
  assert.strictEqual(anc.ancMediaImageUrl(doc, '1badf00d-5678-4def-9abc-ba9876543210', 'https://www.ancestry.com'),
    'https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/4242/media/0dd00000-5678-4def-9abc-ba9876543210' +
    '?client=trees-mediaservice&imageQuality=hq');
  // A picture not marked with the photo's id is no picture of it.
  assert.strictEqual(anc.ancMediaImageUrl(parse(CLIP_HTML.split('id="' + CLIP_ID + '"').join('')), CLIP_ID,
    'https://www.ancestry.com'), null);
  assert.strictEqual(anc.ancMediaImageUrl(doc, '00000000-0000-4000-8000-0000000000ff', 'https://www.ancestry.com'), null);
});

test('#261: on a clipping she may edit, Ancestry\'s "Add a description" prompts are no details, and the article link is no uploader', () => {
  const d = anc.ancMediaDetails(parse(CLIP_HTML));
  assert.deepStrictEqual(d, {
    title: 'Obituary of Thomas Hartley',
    date: '4 Feb 1958',
    place: 'Fairview, Breton, Kansas, USA',
    category: 'Document / Certificate',
    originallyAddedBy: 'hartley_family_tree on 9 Sep 2021',
    linkedTo: ['Thomas Hartley'],
  });
  const all = JSON.stringify(d);
  for (const never of ['Add a', 'description', 'transcription', 'this article', 'Newspapers.com',
    'cousin_member_one', 'Saved by', 'Save to someone else']) {
    assert.ok(!all.includes(never), 'kept from the page: ' + never);
  }
  assert.strictEqual(anc.ancMediaHints(d).sourceDate, '1958-02-04');
});

test('#261: an item Ancestry calls a document is filed as one - an obituary as a newspaper; a photo stays a photo', () => {
  // The owner: an item whose Ancestry category is a document "should be
  // saved as a document". The clipping: "Document / Certificate", titled
  // as an obituary.
  const clip = anc.ancMediaDetails(parse(CLIP_HTML));
  assert.strictEqual(clip.category, 'Document / Certificate');
  assert.deepStrictEqual(anc.ancMediaHints(clip), { sourceType: 'newspaper', sourceDate: '1958-02-04', sourceClass: null });
  assert.strictEqual(anc.ancMediaHints(anc.ancMediaDetails(panelPage())).sourceType, 'photo');
  // Ancestry's category words (the English site's), and what each files as.
  const doc = (category, title) => anc.ancMediaKind(category, title || 'Thomas Hartley');
  for (const [category, kind] of [
    ['Obituary', 'newspaper'], ['Newspaper Article', 'newspaper'],
    ['Birth Certificate', 'vital-record'], ['Death Certificate', 'vital-record'], ['Marriage Certificate', 'vital-record'],
    ['Property Record', 'land-record'],
    ['Document / Certificate', 'other'], ['Legal Document', 'other'], ['Personal Paper', 'other'], ['Personal Papers', 'other'],
    ['Diploma', 'other'], ['Immigration', 'other'],
    // The owner: these often hold portraits - a photo, unless she changes the Kind.
    ['Military Service', 'photo'], ['Occupation', 'photo'], ['Education', 'photo'], ['Yearbook', 'photo'],
    // When unsure, nothing changes: a photo.
    ['Portrait / Family Photo', 'photo'], ['Site', 'photo'], ['Headstone', 'photo'], ['Heirloom', 'photo'],
    ['Recipe', 'photo'], ['Award / Medal', 'photo'], ['Other', 'photo'], ['', 'photo'], [undefined, 'photo'],
    ['Dokument / Urkunde', 'photo'], ['Portrait / Photo de famille', 'photo'],
  ]) {
    assert.strictEqual(doc(category), kind, String(category));
  }
  // Another document is what its title says it is.
  assert.strictEqual(doc('Document / Certificate', 'Obituary for Thomas Hartley'), 'newspaper');
  assert.strictEqual(doc('Document / Certificate', 'Death record of Margaret Hartley'), 'vital-record');
  assert.strictEqual(doc('Legal Document', '1880 United States Federal Census'), 'census');
  // The title alone never makes a photo a document.
  assert.strictEqual(doc('Portrait / Family Photo', 'Obituary for Thomas Hartley'), 'photo');
  // Every kind is one the archive's Kind list knows.
  const KINDS = ['census', 'vital-record', 'newspaper', 'photo', 'military-record', 'land-record', 'website', 'other'];
  for (const c of ['Obituary', 'Birth Certificate', 'Military Service', 'Property Record', 'Diploma', 'Site', 'Yearbook']) {
    assert.ok(KINDS.indexOf(doc(c)) >= 0, c);
  }
});

test('#261 audit B1: while Ancestry\'s Details still describe the item shown before, nothing of them is taken', () => {
  // She clicked the viewer's arrow to photo B: the address and B's picture
  // (its alt text is B's title) are there, the panel still shows photo A.
  const B = '0b0b0b0b-1234-4abc-8def-0123456789ab';
  const B_URL = 'https://www.ancestry.com/mediaui-viewer/tree/90000001/person/90000002/media/' + B;
  const withPicture = (img) => panelPage((h) => h.replace('<div class="viewerStage">', '<div class="viewerStage">' + img));
  const src = 'https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/4242/media/';
  const stale = anc.ancMediaFrom(withPicture('<img id="' + B + '" alt="Photo B" src="' + src + B + '?client=x">'), B_URL);
  assert.strictEqual(stale.ok, false);
  assert.strictEqual(stale.transient, true);
  assert.match(stale.error, /have not caught up with the picture yet .* nothing was saved - wait a moment, then press Capture again/);
  assert.ok(!JSON.stringify(stale).includes('Wedding'), 'nothing of photo A is passed on');
  // B's picture known by its address alone (no id), or under an image id of its own.
  assert.strictEqual(anc.ancMediaFrom(withPicture('<img alt="Photo B" src="' + src + B + '?client=x">'), B_URL).ok, false);
  assert.strictEqual(anc.ancMediaFrom(withPicture('<img id="' + B.toUpperCase() + '" alt="Photo B" src="' + src +
    'a55e7000-1234-4abc-8def-0123456789ab?client=x">'), B_URL).ok, false);
  // The panel caught up: its title is the picture's alt text (spacing and case aside) - read.
  const caught = anc.ancMediaFrom(withPicture('<img id="' + B + '" alt="  wedding of Thomas Hartley   and Margaret Ellis" src="' +
    src + B + '?client=x">'), B_URL);
  assert.strictEqual(caught.ok, true);
  assert.strictEqual(caught.details.date, '12 Jun 1919');
  // No alt text, a thumbnail's alt, or another photo's picture: no check - read, as before.
  assert.strictEqual(anc.ancMediaFrom(withPicture('<img id="' + B + '" alt="" src="' + src + B + '?client=x">'), B_URL).ok, true);
  assert.strictEqual(anc.ancMediaFrom(withPicture('<img id="' + B + '" src="' + src + B + '?client=x">'), B_URL).ok, true);
  assert.strictEqual(anc.ancMediaFrom(withPicture('<img alt="Photo B" src="' + src + B + '.jpg?MaxSide=80">'), B_URL).ok, true);
  assert.strictEqual(anc.ancMediaFrom(withPicture('<img id="1badf00d-5678-4def-9abc-ba9876543210" alt="Another" src="' + src +
    '1badf00d-5678-4def-9abc-ba9876543210?client=x">'), B_URL).ok, true);
  // Without Ancestry's own panel (another layout), nothing is checked.
  const other = parse(fs.readFileSync(path.join(SAMPLES, 'ancestry-media-photo.html'), 'utf8')
    .replace('alt="Thomas and Margaret Hartley at the Fairview farm, about 1925"', 'alt="Something else"'));
  assert.strictEqual(anc.ancMediaFrom(other, PHOTO_URL).ok, true);
  // Both real shapes: the clipping's picture carries its title.
  assert.strictEqual(anc.ancMediaFrom(parse(CLIP_HTML), CLIP_URL).ok, true);
});

test('#261 audit A2: a description with markup keeps every word and its line breaks', () => {
  const DESC = 'Thomas and Margaret outside the Fairview church.\nHer sister Ethel is on the left.';
  const desc = (inner) => anc.ancMediaDetails(panelPage((h) => h.replace(DESC, inner))).description;
  assert.strictEqual(desc('Thomas and <em>not</em> Margaret.'), 'Thomas and not Margaret.');
  assert.strictEqual(desc('Thomas outside <a href="/x">Fairview Church</a> in June.'), 'Thomas outside Fairview Church in June.');
  assert.strictEqual(desc('Thomas and Margaret outside the church.<br>Her sister Ethel is on the left.'),
    'Thomas and Margaret outside the church.\nHer sister Ethel is on the left.');
  // Ancestry's own prompt and icons inside a value are still never words of hers.
  assert.strictEqual(desc('<span class="link">Add a description</span><span aria-hidden="true" class="icon iconEdit"></span>'),
    undefined);
  // A value Ancestry marks as its AI's estimate is not hers.
  assert.strictEqual(anc.ancMediaDetails(panelPage((h) => h.replace('>12 Jun 1919<',
    '>1920s<span class="icon iconAi" aria-label="Estimated"></span><'))).date, undefined);
});

test('#261: inside Ancestry\'s panel, only its own fields are read - never a label-shaped line in the comments', () => {
  // A comment laid out like a Details label is still a member's comment.
  const doc = panelPage((h) => h.replace('<p class="commentBody">Shared by cousin_member_three</p>',
    '<dl><dt>Originally added by</dt><dd>cousin_member_three</dd><dt>Date</dt><dd>1921</dd></dl>'));
  const d = anc.ancMediaDetails(doc);
  assert.strictEqual(d.originallyAddedBy, 'hartley_family_tree on 3 Mar 2015');
  assert.strictEqual(d.date, '12 Jun 1919');
  const all = JSON.stringify(d);
  for (const never of OTHERS) assert.ok(!all.includes(never), 'kept from the page: ' + never);
});
