// Find a Grave memorial capture: the pure readers in lib/findagrave-record.js,
// run on the same fixture pages the Python recipe tests read
// (tests/fixtures/capture-samples/findagrave-memorial*.html, invented
// memorials of the fictional Hartleys).
//
//   node --test browser-companion/tests/test-findagrave.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const fag = require('../src/lib/findagrave-record.js');
const { parse } = require('./mini-dom.js');

const SAMPLES = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'capture-samples');
const sample = (name) => fs.readFileSync(path.join(SAMPLES, name + '.html'), 'utf8');
const MEMORIAL = 'https://www.findagrave.com/memorial/900000001/calvin-george-hartley';

function readMemorial(html, href, opts) {
  return fag.fagMemorialFrom(parse(html || sample('findagrave-memorial')), href || MEMORIAL, opts || { today: '2026-11-06' });
}

// ── which memorial ───────────────────────────────────────────────────────────

test('fagParseMemorial: a memorial, its gallery and its flowers; nothing else, no other host', () => {
  assert.deepStrictEqual(fag.fagParseMemorial(MEMORIAL), { memorialId: '900000001', slug: 'calvin-george-hartley' });
  assert.deepStrictEqual(fag.fagParseMemorial(MEMORIAL + '/photo?page=2'), { memorialId: '900000001', slug: 'calvin-george-hartley' });
  assert.deepStrictEqual(fag.fagParseMemorial('https://www.findagrave.com/memorial/900000001'), { memorialId: '900000001', slug: null });
  assert.deepStrictEqual(fag.fagParseMemorial('https://de.findagrave.com/memorial/900000001/x'), { memorialId: '900000001', slug: 'x' });
  assert.deepStrictEqual(fag.fagParseMemorial('/memorial/900000003/w', 'https://www.findagrave.com/memorial/900000001/x'),
    { memorialId: '900000003', slug: 'w' });
  for (const u of [
    'https://www.findagrave.com/cemetery/9990001/fairview-cemetery',
    'https://www.findagrave.com/memorial/search?firstname=Calvin',
    'https://findagrave.com.evil.example/memorial/900000001/x',
    'https://www.ancestry.com/memorial/900000001/x',
    'not a url', '', null,
  ]) {
    assert.strictEqual(fag.fagParseMemorial(u), null, String(u));
  }
});

// ── the memorial ─────────────────────────────────────────────────────────────

test('fagMemorialFrom: names, dates, places, cemetery, plot, inscription, family, bio and its own citation', () => {
  const info = readMemorial();
  assert.ok(info.ok, info.error);
  const r = info.record;
  assert.strictEqual(r.memorialId, '900000001');
  assert.strictEqual(r.url, MEMORIAL);
  assert.strictEqual(r.name, 'Calvin George Hartley');
  assert.deepStrictEqual(r.birth, { date: '12 Mar 1842', place: 'Ohio, USA' });
  // Find a Grave's "(aged 75)" is kept apart from the date
  assert.deepStrictEqual(r.death, { date: '4 Jan 1918', age: '75', place: 'Fairview, Brown County, Kansas, USA' });
  assert.deepStrictEqual(r.burial, {
    cemetery: 'Fairview Cemetery', cemeteryId: '9990001', city: 'Fairview', county: 'Brown County',
    state: 'Kansas', country: 'USA', plot: 'Block 4, Lot 12',
  });
  assert.strictEqual(r.inscription, 'Gone home\nRest in peace');
  assert.strictEqual(r.gravesiteDetails, 'Buried beside his wife, Harriet.');
  assert.strictEqual(r.bio, 'Calvin farmed near Fairview for forty years.\nHe served on the school board.');
  assert.deepStrictEqual(r.family.map((f) => [f.relation, f.memorialId, f.name, f.dates]), [
    ['Parents', '900000004', 'Thomas Hartley', '1810–1880'],
    ['Spouse', '900000002', 'Harriet Frances Hartley', '1846–1915'],
    ['Children', '900000003', 'Warren Calvin Hartley', '1870–1951'],
  ], 'the memorial linking itself is not its own family');
  assert.strictEqual(r.createdBy, 'Fairview Cemetery Volunteers');
  assert.strictEqual(r.added, '3 Jun 2008');
  assert.strictEqual(r.collection, 'Find a Grave');
  assert.match(info.citation, /^Find a Grave, database and images \(https:\/\/www\.findagrave\.com\/memorial\/900000001\/calvin-george-hartley: accessed 2026-11-06\), memorial page for Calvin George Hartley/);
});

test('fagMemorialFrom: Seen It\'s badges are never read as the memorial (TI-067)', () => {
  const info = readMemorial();
  assert.doesNotMatch(JSON.stringify(info), /Seen 2|Not in archive/);
});

test('fagMemorialFrom: a page with no memorial says it may have been removed or merged, and what to do', () => {
  const info = readMemorial('<html><body><h1>Memorial has been removed</h1></body></html>');
  assert.strictEqual(info.ok, false);
  assert.match(info.error, /removed or merged/);
  assert.match(info.error, /Search Find a Grave/);
  assert.match(fag.fagMemorialFrom(parse('<p>x</p>'), 'https://www.findagrave.com/cemetery/1/x').error, /not a Find a Grave memorial/);
});

test('fagMemorialFrom: the slug comes from the canonical link when the address has none', () => {
  const info = readMemorial(null, 'https://www.findagrave.com/memorial/900000001');
  assert.strictEqual(info.record.slug, 'calvin-george-hartley');
  // a canonical naming ANOTHER memorial is not this one's
  const other = sample('findagrave-memorial').replace(
    'href="https://www.findagrave.com/memorial/900000001/calvin-george-hartley">',
    'href="https://www.findagrave.com/memorial/900000009/someone-else">');
  assert.strictEqual(readMemorial(other, 'https://www.findagrave.com/memorial/900000001').record.slug, null);
});

// ── what it says about itself (TI-066, TI-068) ──────────────────────────────

test('fagHints: a website, derivative, dated by its own death - never its birth, never a burial date it lacks', () => {
  const r = readMemorial().record;
  assert.deepStrictEqual(fag.fagHints(r), { sourceType: 'website', sourceDate: '1918-01-04', sourceClass: 'derivative' });
  const born = Object.assign({}, r, { death: { date: null, place: null } });
  assert.deepStrictEqual(fag.fagHints(born), { sourceType: 'website', sourceDate: null, sourceClass: 'derivative' });
  const unsure = Object.assign({}, r, { death: { date: 'unknown' } });
  assert.strictEqual(fag.fagHints(unsure).sourceDate, null);
  assert.strictEqual(fag.fagHints(Object.assign({}, r, { death: { date: 'Jan 1918' } })).sourceDate, '1918-01');
  assert.strictEqual(fag.fagHints(Object.assign({}, r, { death: { date: '1918' } })).sourceDate, '1918');
});

test('fagTitle: name and years from its own dates', () => {
  const r = readMemorial().record;
  assert.strictEqual(fag.fagTitle(r), 'Calvin George Hartley (1842-1918) - Find a Grave memorial');
  assert.strictEqual(fag.fagTitle(Object.assign({}, r, { birth: {} })), 'Calvin George Hartley (?-1918) - Find a Grave memorial');
  assert.strictEqual(fag.fagTitle({ memorialId: '900000001', birth: {}, death: {} }), 'Memorial 900000001 - Find a Grave memorial');
});

// ── photos ───────────────────────────────────────────────────────────────────

test('fagPhotoUrlOk: only full-size photos on Find a Grave\'s own image host, over https', () => {
  assert.strictEqual(fag.FAG_PHOTO_HOST, 'images.findagrave.com');
  for (const u of [
    'https://images.findagrave.com/photos/2020/45/900000001_aaaa1111.jpeg',
    'https://IMAGES.findagrave.com/photos/2020/45/x.jpg',
  ]) assert.strictEqual(fag.fagPhotoUrlOk(u), true, u);
  for (const u of [
    'http://images.findagrave.com/photos/2020/45/x.jpg',
    'https://images.findagrave.com.evil.example/photos/x.jpg',
    'https://evil.example/images.findagrave.com/photos/x.jpg',
    'https://www.findagrave.com/photos/x.jpg',
    'https://cdn.findagrave.com/photos/x.jpg',
    'https://images.findagrave.com/photos250/photos/2020/45/x.jpg',
    'https://user:pw@images.findagrave.com/photos/x.jpg',
    'https://images.findagrave.com:8443/photos/x.jpg',
    '/photos/x.jpg', 'not a url', '', null,
  ]) assert.strictEqual(fag.fagPhotoUrlOk(u), false, String(u));
});

test('fagPhotosFrom: each photo once, full size (query dropped), with caption, contributor and type', () => {
  const got = fag.fagPhotosFrom(parse(sample('findagrave-memorial-photos')), MEMORIAL + '/photo');
  assert.strictEqual(got.total, 3);
  assert.deepStrictEqual(got.photos, [
    { photoId: '990000101', url: 'https://images.findagrave.com/photos/2020/45/900000001_aaaa1111.jpeg',
      caption: 'Headstone, west side of the cemetery', addedBy: 'Fairview Cemetery Volunteers', type: 'Grave' },
    { photoId: '990000102', url: 'https://images.findagrave.com/photos/2019/12/900000001_bbbb2222.jpg',
      caption: 'Calvin, about 1900', addedBy: 'Hartley Family', type: 'Person' },
    { photoId: '990000103', url: 'https://images.findagrave.com/photos/2019/12/900000001_cccc3333.png',
      caption: null, addedBy: 'Hartley Family', type: 'Family' },
  ]);
});

test('fagPhotosFrom: a photo on any other host, a thumbnail, or a repeat is never listed', () => {
  const html = '<div class="viewer-item" data-photo-id="1"><img src="https://evil.example/photos/a.jpg"></div>' +
    '<div class="viewer-item" data-photo-id="2"><img src="https://images.findagrave.com/photos250/photos/b.jpg"></div>' +
    '<div class="viewer-item" data-photo-id="3"><img src="http://images.findagrave.com/photos/c.jpg"></div>' +
    '<div class="viewer-item" data-photo-id="4"><img src="https://images.findagrave.com/photos/d.jpg"></div>' +
    '<div class="viewer-item" data-photo-id="4"><img src="https://images.findagrave.com/photos/d.jpg"></div>' +
    '<div class="viewer-item"><img src="https://images.findagrave.com/photos/e.jpg"></div>';
  const got = fag.fagPhotosFrom(parse(html), MEMORIAL);
  assert.deepStrictEqual(got.photos.map((p) => p.photoId), ['4']);
  assert.strictEqual(got.total, null);
});

test('fagPhotoRole: one numbered role per Find a Grave photo type, "photo" for anything else', () => {
  const counts = {};
  assert.deepStrictEqual(['Grave', 'Person', 'Grave', 'Family', 'Document', null, 'Record', 'page'].map((t) => fag.fagPhotoRole(t, counts)),
    ['grave-1', 'person-1', 'grave-2', 'family-1', 'document-1', 'photo-1', 'photo-2', 'photo-3']);
});

// ── the gallery, page by page, without cookies ──────────────────────────────

function galleryFetch(pages, log) {
  return async (url, init) => {
    log.push({ url, init });
    const u = new URL(url);
    const n = Number(u.searchParams.get('page') || 1);
    const body = pages[n - 1];
    if (typeof body === 'number') return { ok: false, status: body, text: async () => '' };
    return { ok: true, status: 200, text: async () => body || '<html></html>' };
  };
}

const item = (id, file) => '<div class="viewer-item" data-photo-id="' + id + '"><img src="https://images.findagrave.com/photos/' +
  file + '"><p>Photo type: Grave</p></div>';

test('fagGallery: every page until the count is reached, each request on Find a Grave and without cookies', async () => {
  const log = [];
  const pages = [
    '<p class="photosCount">3 photos</p>' + item(1, 'a.jpg') + item(2, 'b.jpg'),
    '<p class="photosCount">3 photos</p>' + item(3, 'c.jpg'),
    '<p>never asked</p>',
  ];
  const got = await fag.fagGallery('900000001', 'calvin-george-hartley',
    { fetch: galleryFetch(pages, log), parse, base: 'https://www.findagrave.com' });
  assert.ok(got.ok);
  assert.deepStrictEqual(got.photos.map((p) => p.photoId), ['1', '2', '3']);
  assert.strictEqual(got.total, 3);
  assert.deepStrictEqual(log.map((l) => l.url), [
    'https://www.findagrave.com/memorial/900000001/calvin-george-hartley/photo',
    'https://www.findagrave.com/memorial/900000001/calvin-george-hartley/photo?page=2',
  ]);
  for (const l of log) assert.deepStrictEqual(l.init, { credentials: 'omit' });
});

test('fagGallery: a page that repeats the last stops the walk; only a count of 0 is an empty gallery', async () => {
  const log = [];
  const same = item(1, 'a.jpg');
  const got = await fag.fagGallery('900000001', null, { fetch: galleryFetch([same, same, same], log), parse, base: 'https://www.findagrave.com' });
  assert.deepStrictEqual(got.photos.map((p) => p.photoId), ['1']);
  assert.strictEqual(log.length, 2);
  assert.strictEqual(got.total, 1);
  assert.strictEqual(got.partial, false);
  const empty = await fag.fagGallery('900000001', null, { fetch: galleryFetch(['<p class="photosCount">0 photos</p>'], []), parse, base: 'https://www.findagrave.com' });
  assert.deepStrictEqual([empty.photos, empty.total, empty.partial], [[], 0, false]);
  // Codex 4502262326: a page with no photo and no count (an interstitial,
  // a new layout) is not "no photos": the total is not known
  const unknown = await fag.fagGallery('900000001', null, { fetch: galleryFetch(['<p>Checking your browser</p>'], []), parse, base: 'https://www.findagrave.com' });
  assert.deepStrictEqual([unknown.ok, unknown.photos, unknown.total], [true, [], null]);
});

test('fagGallery: a refusal says what it means and what to do', async () => {
  const d = (status) => ({ fetch: galleryFetch([status], []), parse, base: 'https://www.findagrave.com' });
  assert.match((await fag.fagGallery('1', null, d(429))).error, /slow down.*Wait a few minutes/);
  assert.match((await fag.fagGallery('1', null, d(404))).error, /removed or merged/);
  assert.match((await fag.fagGallery('1', null, d(403))).error, /refused the photo list/);
  assert.match((await fag.fagGallery('1', null, d(503))).error, /having trouble/);
  const offline = { fetch: async () => { throw new TypeError('Failed to fetch'); }, parse, base: 'https://www.findagrave.com' };
  assert.match((await fag.fagGallery('1', null, offline)).error, /internet connection/);
  // a later page failing keeps what the first pages gave
  const later = await fag.fagGallery('1', null, { fetch: galleryFetch(['<p class="photosCount">3</p>' + item(1, 'a.jpg'), 500], []), parse, base: 'https://www.findagrave.com' });
  assert.deepStrictEqual([later.ok, later.photos.length, later.total], [true, 1, 3]);
});

// ── PR #221 audit ────────────────────────────────────────────────────────────

test('F9/F12, Codex 4502262323: a later gallery page failing says the list is partial, with its cause; no count stays unknown', async () => {
  const base = 'https://www.findagrave.com';
  const withCount = await fag.fagGallery('1', null, { fetch: galleryFetch(['<p class="photosCount">30</p>' + item(1, 'a.jpg') + item(2, 'b.jpg'), 429], []), parse, base });
  assert.deepStrictEqual([withCount.ok, withCount.photos.length, withCount.total, withCount.partial, withCount.partialStatus],
    [true, 2, 30, true, 429]);
  assert.match(withCount.partialError, /slow down/);
  const noCount = await fag.fagGallery('1', null, { fetch: galleryFetch([item(1, 'a.jpg') + item(2, 'b.jpg'), 503], []), parse, base });
  assert.deepStrictEqual([noCount.photos.length, noCount.total, noCount.partial], [2, null, true],
    'never "2 of 2" when page 2 was never read');
  let n = 0;
  const offline = await fag.fagGallery('1', null, {
    fetch: async (url) => {
      if (n++) throw new TypeError('Failed to fetch');
      return { ok: true, status: 200, text: async () => item(1, 'a.jpg') };
    }, parse, base,
  });
  assert.deepStrictEqual([offline.total, offline.partial], [null, true]);
  assert.match(offline.partialError, /internet connection/);
  // a count the pages fall short of (Find a Grave ignoring ?page=) is partial too
  const short = await fag.fagGallery('1', null, { fetch: galleryFetch(['<p class="photosCount">5</p>' + item(1, 'a.jpg'), '<p class="photosCount">5</p>' + item(1, 'a.jpg')], []), parse, base });
  assert.deepStrictEqual([short.photos.length, short.total, short.partial], [1, 5, true]);
  // more than 20 pages is partial
  const pages = Array.from({ length: 21 }, (_, i) => item(i + 1, i + '.jpg'));
  const long = await fag.fagGallery('1', null, { fetch: galleryFetch(pages, []), parse, base });
  assert.deepStrictEqual([long.photos.length, long.total, long.partial], [20, null, true]);
});

test('F2/F4: an accented or apostrophe slug is used, safely encoded - never a reason to refuse the photos', async () => {
  assert.strictEqual(fag.fagSlugSegment('j%C3%B3zefa-hartley'), 'j%C3%B3zefa-hartley');
  assert.strictEqual(fag.fagSlugSegment('józefa-hartley'), 'j%C3%B3zefa-hartley');
  assert.strictEqual(fag.fagSlugSegment("mary-o'hartley"), "mary-o'hartley");
  assert.strictEqual(fag.fagSlugSegment('ann-o%27hartley'), "ann-o'hartley");
  assert.strictEqual(fag.fagSlugSegment('../x?y#z'), '..%2Fx%3Fy%23z', 'one segment, whatever it says');
  for (const bad of ['%E0%A4%A', '.', '..']) assert.strictEqual(fag.fagSlugSegment(bad), null, bad);
  assert.strictEqual(fag.fagSlugSegment(null), '');
  for (const [slug, want] of [
    ['j%C3%B3zefa-hartley', '/memorial/900000005/j%C3%B3zefa-hartley/photo'],
    ["mary-o'hartley", "/memorial/900000005/mary-o'hartley/photo"],
    ['%E0%A4%A', '/memorial/900000005/photo'],
  ]) {
    const log = [];
    const got = await fag.fagGallery('900000005', slug, { fetch: galleryFetch([item(1, 'a.jpg')], log), parse, base: 'https://www.findagrave.com' });
    assert.ok(got.ok, slug);
    assert.strictEqual(new URL(log[0].url).pathname, want, slug);
    assert.strictEqual(new URL(log[0].url).hostname, 'www.findagrave.com');
  }
  // the memorial reader takes such a slug from the address as Find a Grave gives it
  assert.deepStrictEqual(fag.fagParseMemorial("https://www.findagrave.com/memorial/900000005/mary-o'hartley"),
    { memorialId: '900000005', slug: "mary-o'hartley" });
});

test('fagStatusMessage: removed, refused, slow down, trouble', () => {
  assert.match(fag.fagStatusMessage(404, 'memorial'), /removed or merged/);
  assert.match(fag.fagStatusMessage(410, 'photo'), /no longer has this photo/);
  assert.match(fag.fagStatusMessage(403, 'photo'), /refused the photo/);
  assert.match(fag.fagStatusMessage(429, 'photo'), /slow down/);
  assert.match(fag.fagStatusMessage(502, 'gallery'), /having trouble/);
  assert.match(fag.fagStatusMessage(418, 'memorial'), /HTTP 418/);
});

test('Codex 4503786095/-098: a photo answer says when to slow down (429 and 503) and when its cause passes', async () => {
  const w = {};
  new Function('window', fs.readFileSync(path.join(__dirname, '..', 'src', 'lib', 'findagrave.js'), 'utf8'))(w);
  const fetchPhoto = w.FHA.findagrave.fetchPhoto;
  const url = 'https://images.findagrave.com/photos/2020/45/900000001_aaaa1111.jpeg';
  const answer = (status) => async () => ({ ok: status < 400, status, url, headers: { get: () => 'text/html' } });
  for (const [status, slowDown, transient] of [[429, true, true], [503, true, true], [500, false, true],
    [502, false, true], [403, false, false], [404, false, false]]) {
    const got = await fetchPhoto(url, answer(status));
    assert.strictEqual(got.ok, false);
    assert.strictEqual(!!got.slowDown, slowDown, 'HTTP ' + status + ' slowDown');
    assert.strictEqual(!!got.transient, transient, 'HTTP ' + status + ' transient');
  }
  const dropped = await fetchPhoto(url, async () => { throw new TypeError('Failed to fetch'); });
  assert.strictEqual(dropped.transient, true);
  const broke = await fetchPhoto(url, async () => ({ ok: true, status: 200, url, headers: { get: () => 'image/jpeg' },
    blob: async () => { throw new TypeError('network error'); } }));
  assert.strictEqual(broke.transient, true);
});
