// Tests for the FamilySearch record capture's core (lib/familysearch-record.js):
// which record a page is, the login rule (the researcher's session goes only
// to FamilySearch's own https addresses), what each refusal means, the record
// read from FamilySearch's own data, and what it says about itself for filing.
//
// The data-service answers are fixtures in the shapes the live service sends
// (tests/fixtures/capture-samples/familysearch-gedcomx.json): fictional
// Hartleys, invented ARKs. The network is a scripted stand-in that records
// every request's address and whether it carried the login.
//
//   node --test browser-companion/tests/test-familysearch.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const fsr = require('../src/lib/familysearch-record.js');

const REPO = path.join(__dirname, '..', '..');
const FIX = JSON.parse(fs.readFileSync(
  path.join(REPO, 'tests', 'fixtures', 'capture-samples', 'familysearch-gedcomx.json'), 'utf8'));

const PAGE = 'https://www.familysearch.org/ark:/61903/1:1:HRT1-DTH1';
const TOKEN = 'fs-session-SECRET-0000';

// ── identity ─────────────────────────────────────────────────────────────────

test('fsParseRecordPage: a record, and the viewer opened from one, and nothing else', () => {
  assert.deepStrictEqual(fsr.fsParseRecordPage(PAGE), { fsId: 'HRT1-DTH1', viewerImageId: null });
  assert.deepStrictEqual(fsr.fsParseRecordPage('https://familysearch.org/ark:/61903/1:1:hrt1-dth1?lang=en'),
    { fsId: 'HRT1-DTH1', viewerImageId: null });
  assert.deepStrictEqual(fsr.fsParseRecordPage(
    'https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG1?view=index&personArk=%2Fark%3A%2F61903%2F1%3A1%3AHRT1-CEN1&action=view'),
  { fsId: 'HRT1-CEN1', viewerImageId: 'HRT1-IMG1' });
  for (const u of [
    'https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG1',        // a bare image: not a record
    'https://www.familysearch.org/tree/person/details/HRT1-PID',      // a tree person
    'https://familysearch.org.evil.com/ark:/61903/1:1:HRT1-DTH1',
    'https://evilfamilysearch.org/ark:/61903/1:1:HRT1-DTH1',
    'not a url', '', null,
  ]) {
    assert.strictEqual(fsr.fsParseRecordPage(u), null, String(u));
  }
});

test('fsImageIdFromHref: the image an address names, on FamilySearch only', () => {
  assert.strictEqual(fsr.fsImageIdFromHref('https://www.familysearch.org/ark:/61903/3:1:hrt1-img1?i=4'), 'HRT1-IMG1');
  assert.strictEqual(fsr.fsImageIdFromHref('https://www.familysearch.org/search/film/0000?cat=1&i=2&viewer=ark%3A%2F61903%2F3%3A1%3AHRT1-IMG9'),
    'HRT1-IMG9');
  assert.strictEqual(fsr.fsImageIdFromHref('https://www.familysearch.org/search/film/0000?q=%zz'), null);
  assert.strictEqual(fsr.fsImageIdFromHref('https://evil.example/ark:/61903/3:1:HRT1-IMG1'), null);
  assert.strictEqual(fsr.fsImageIdFromHref(PAGE), null);
});

// ── the login: only to FamilySearch, only over https ─────────────────────────

test('fsAuthHeaders sends the login to FamilySearch\'s own https addresses', () => {
  const want = { Authorization: 'Bearer ' + TOKEN };
  for (const href of [
    '/ark:/61903/1:1:HRT1-DTH1',                                  // relative to the page
    'das/v2/HRT1-IMG1/dist.jpg',
    'https://familysearch.org/ark:/61903/3:1:HRT1-IMG1',
    'https://www.familysearch.org/das/v2/HRT1-IMG1/dist.jpg',
    'https://sg30p0.familysearch.org/service/records/storage/das/HRT1-IMG1',
    'https://WWW.FamilySearch.ORG/x',
  ]) {
    assert.deepStrictEqual(fsr.fsAuthHeaders(href, PAGE, TOKEN), want, href);
  }
});

test('fsAuthHeaders never sends the login anywhere else', () => {
  for (const href of [
    'https://evil.com/ark:/61903/1:1:HRT1-DTH1',
    'https://familysearch.org.evil.com/das/v2/HRT1-IMG1/dist.jpg',
    'https://evilfamilysearch.org/das/v2/HRT1-IMG1/dist.jpg',
    'https://familysearch.org@evil.com/x',
    'https://user:pw@www.familysearch.org/x',
    'http://www.familysearch.org/das/v2/HRT1-IMG1/dist.jpg',          // plain http
    'http://familysearch.org/ark:/61903/1:1:HRT1-DTH1',
    '//evil.com/x',                                                   // protocol-relative
    'javascript:alert(1)',
    'data:text/plain,hi',
    'ftp://www.familysearch.org/x',
  ]) {
    assert.deepStrictEqual(fsr.fsAuthHeaders(href, PAGE, TOKEN), {}, href);
  }
  // A relative path from a page that is not FamilySearch's gets nothing either.
  assert.deepStrictEqual(fsr.fsAuthHeaders('/x', 'https://evil.com/page', TOKEN), {});
  assert.deepStrictEqual(fsr.fsAuthHeaders('/x', 'http://www.familysearch.org/page', TOKEN), {});
  // Nobody signed in: no header at all.
  assert.deepStrictEqual(fsr.fsAuthHeaders('/x', PAGE, null), {});
  assert.deepStrictEqual(fsr.fsAuthHeaders('/x', PAGE, ''), {});
});

// ── what a refusal means ─────────────────────────────────────────────────────

test('fsStatusMessage names the true cause and what to do next', () => {
  assert.match(fsr.fsStatusMessage(401, 'record', true), /asked you to sign in \(HTTP 401\)\. Sign in to FamilySearch/);
  assert.match(fsr.fsStatusMessage(403, 'record', false), /asked you to sign in \(HTTP 403\)/);
  assert.match(fsr.fsStatusMessage(403, 'image', true),
    /will not show this image here \(HTTP 403\): it is restricted to a FamilySearch center or affiliate library/);
  assert.match(fsr.fsStatusMessage(429, 'image', true), /asked us to slow down \(HTTP 429\)\. Wait a few minutes/);
  assert.match(fsr.fsStatusMessage(404, 'record', true), /no record at this address \(HTTP 404\)/);
  assert.match(fsr.fsStatusMessage(503, 'record', true), /having trouble \(HTTP 503\)\. Try again/);
  assert.match(fsr.fsStatusMessage(418, 'record', true), /answered HTTP 418 for this record\. Reload the page/);
});

// ── the record from FamilySearch's own data ──────────────────────────────────

test('fsRecordFrom reads the record, its collection, image and citation from the data', () => {
  const r = fsr.fsRecordFrom(FIX.records['HRT1-CEN1'], { fsId: 'HRT1-CEN1', viewerImageId: null });
  assert.deepStrictEqual(r.record, {
    fsId: 'HRT1-CEN1', url: 'https://www.familysearch.org/ark:/61903/1:1:HRT1-CEN1', imageId: 'HRT1-IMG1',
    recordArk: '1:2:HRT1-REC2', collectionId: '9000002', collection: 'United States, Census, 1880',
    name: 'Calvin Hartley',
  });
  assert.strictEqual(r.title, 'Calvin Hartley, "United States, Census, 1880"');
  assert.match(r.citation, /^"United States, Census, 1880," database with images, FamilySearch, Calvin Hartley/);
  // No image of its own: the one the viewer shows stands in; with neither, none.
  const d = fsr.fsRecordFrom(FIX.records['HRT1-DTH1'], { fsId: 'HRT1-DTH1', viewerImageId: 'HRT1-IMG7' });
  assert.strictEqual(d.record.imageId, 'HRT1-IMG7');
  assert.strictEqual(fsr.fsRecordFrom(FIX.records['HRT1-DTH1'], { fsId: 'HRT1-DTH1', viewerImageId: null }).record.imageId, null);
  assert.deepStrictEqual(fsr.fsRecordFrom({}, { fsId: 'X' }).record.name, null);
});

test('fsFormalToEdtf: GEDCOM X formal dates in the archive\'s form', () => {
  assert.strictEqual(fsr.fsFormalToEdtf('+1957-01-07'), '1957-01-07');
  assert.strictEqual(fsr.fsFormalToEdtf('+1957-01'), '1957-01');
  assert.strictEqual(fsr.fsFormalToEdtf('+1957'), '1957');
  assert.strictEqual(fsr.fsFormalToEdtf('A+1887'), '1887~');
  for (const v of ['+1880/+1881', '/+1900', '+1900/', '1957', '', null, 'A+18']) {
    assert.strictEqual(fsr.fsFormalToEdtf(v), null, String(v));
  }
});

test('fsHints: dated by the record\'s own event, never the collection\'s first year (TI-066)', () => {
  const hint = (id) => {
    const g = FIX.records[id];
    return fsr.fsHints(fsr.fsRecordFrom(g, { fsId: id }).record, g);
  };
  assert.deepStrictEqual(hint('HRT1-DTH1'), { sourceType: 'vital-record', sourceDate: '1957-01-07', sourceClass: 'derivative' });
  assert.deepStrictEqual(hint('HRT1-CEN1'), { sourceType: 'census', sourceDate: '1880', sourceClass: null });
  // A marriage's date is on the couple, not the person: 1866, not 1855.
  assert.deepStrictEqual(hint('HRT1-MAR1'), { sourceType: 'vital-record', sourceDate: '1866-10-12', sourceClass: null });
  // The fact's own words when FamilySearch gives no formal date.
  const g = { persons: [{ principal: true, facts: [{ type: 'http://gedcomx.org/Death', date: { original: 'abt 1901' } }] }] };
  assert.strictEqual(fsr.fsHints({ collection: 'Kansas, Deaths, 1885-1930' }, g).sourceDate, '1901~');
  assert.deepStrictEqual(fsr.fsHints(null, null), { sourceType: null, sourceDate: null, sourceClass: null });
});

test('fsTitle: FamilySearch\'s own name for the record, unless the human typed a title', () => {
  const rec = { fsId: 'HRT1-DTH1', name: 'Ethel Hartley', collection: 'California Death Index, 1940-1997' };
  const auto = 'Ethel Hartley - California Death Index, 1940-1997';
  assert.strictEqual(fsr.fsTitle(rec, '', 'x'), auto);
  assert.strictEqual(fsr.fsTitle(rec, 'An earlier record • FamilySearch', 'An earlier record • FamilySearch'), auto);
  assert.strictEqual(fsr.fsTitle(rec, 'Some record - FamilySearch', ''), auto);
  assert.strictEqual(fsr.fsTitle(rec, 'Ethel\'s death index entry', 'The tab title'), 'Ethel\'s death index entry');
  assert.strictEqual(fsr.fsTitle({ fsId: 'HRT1-X' }, ''), 'FamilySearch record HRT1-X');
});

// ── the data service, scripted ───────────────────────────────────────────────

// A stand-in FamilySearch: answers from the fixture, `over` rewrites answers
// by address. Records every request (address, whether the login rode along).
function service(over) {
  const calls = [];
  const fetch = async (url, init) => {
    const auth = !!(init && init.headers && init.headers.Authorization);
    calls.push({ url, auth, init });
    const o = over && Object.keys(over).find((k) => url.indexOf(k) >= 0);
    if (o) {
      const v = over[o];
      if (v === 'network') throw new TypeError('Failed to fetch');
      if (typeof v === 'number') return { ok: false, status: v, text: async () => '', blob: async () => new Blob([]) };
      return v;
    }
    let m = /\/ark:\/61903\/1:1:([A-Z0-9-]+)$/.exec(url);
    if (m && FIX.records[m[1]]) return json(FIX.records[m[1]]);
    m = /\/ark:\/61903\/3:1:([A-Z0-9-]+)$/.exec(url);
    if (m && FIX.images[m[1]]) return json(FIX.images[m[1]]);
    if (/dist\.jpg$/.test(url)) {
      return { ok: true, status: 200, blob: async () => new Blob([Buffer.alloc(40 * 1024)], { type: 'image/jpeg' }) };
    }
    return { ok: false, status: 404, text: async () => '' };
  };
  return {
    calls,
    deps: { fetch, base: PAGE, token: TOKEN, toBase64: async (b) => Buffer.from(await b.arrayBuffer()).toString('base64') },
  };
}
const json = (o) => ({ ok: true, status: 200, text: async () => JSON.stringify(o) });

test('fsRecordInfo reads the record with the login, and returns nothing of the login', async () => {
  const s = service();
  const info = await fsr.fsRecordInfo(PAGE, s.deps);
  assert.strictEqual(info.ok, true);
  assert.strictEqual(info.record.name, 'Ethel Hartley');
  assert.deepStrictEqual(info.hints, { sourceType: 'vital-record', sourceDate: '1957-01-07', sourceClass: 'derivative' });
  assert.deepStrictEqual(s.calls.map((c) => [c.url, c.auth]),
    [['https://www.familysearch.org/ark:/61903/1:1:HRT1-DTH1', true]]);
  assert.ok(!JSON.stringify(info).includes(TOKEN), 'the login must never be in what is returned');
});

test('fsImage fetches the full image with the login only from FamilySearch', async () => {
  const s = service();
  const img = await fsr.fsImage('HRT1-IMG1', s.deps);
  assert.strictEqual(img.ok, true);
  assert.deepStrictEqual(s.calls.map((c) => [c.url, c.auth]), [
    ['https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG1', true],
    ['https://www.familysearch.org/das/v2/HRT1-IMG1/dist.jpg', true],    // query dropped, as the viewer does
  ]);
  // A relative image link resolves against the page: still FamilySearch, still signed in.
  const r = service();
  assert.strictEqual((await fsr.fsImage('HRT1-IMG3', r.deps)).ok, true);
  assert.deepStrictEqual(r.calls[1].url, 'https://www.familysearch.org/das/v2/HRT1-IMG3/dist.jpg');
  assert.strictEqual(r.calls[1].auth, true);
});

test('an image link that points away from FamilySearch gets the request, never the login', async () => {
  const s = service();
  await fsr.fsImage('HRT1-EVIL', s.deps);
  const away = s.calls.find((c) => /evil\.com/.test(c.url));
  assert.ok(away, 'the image was asked for');
  assert.strictEqual(away.auth, false);
  assert.ok(!JSON.stringify(away.init).includes(TOKEN));
  // The pre-Capture check follows the same rule.
  const c = service();
  await fsr.fsImageCheck('HRT1-EVIL', c.deps);
  assert.strictEqual(c.calls.find((x) => /evil\.com/.test(x.url)).auth, false);
});

test('a restricted image says so, before and at Capture', async () => {
  // No download link in its description:
  let s = service();
  assert.deepStrictEqual(await fsr.fsImageCheck('HRT1-IMG2', s.deps), {
    ok: true, imageId: 'HRT1-IMG2', available: false,
    reason: 'it is restricted: FamilySearch shows it only at a FamilySearch center or affiliate library',
  });
  assert.match((await fsr.fsImage('HRT1-IMG2', s.deps)).error, /restricted to a FamilySearch center or affiliate library/);
  // A link that a signed-in session is refused:
  s = service({ 'dist.jpg': 403 });
  const check = await fsr.fsImageCheck('HRT1-IMG1', s.deps);
  assert.strictEqual(check.available, false);
  assert.match(check.reason, /restricted to a FamilySearch center/);
  assert.match((await fsr.fsImage('HRT1-IMG1', s.deps)).error, /HTTP 403\): it is restricted/);
  // The check asks for the first kilobyte only.
  assert.strictEqual(s.calls.find((c) => /dist\.jpg/.test(c.url)).init.headers.Range, 'bytes=0-1023');
  // An image available from here:
  assert.deepStrictEqual(await fsr.fsImageCheck('HRT1-IMG1', service().deps),
    { ok: true, imageId: 'HRT1-IMG1', available: true });
  assert.deepStrictEqual(await fsr.fsImageCheck(null, service().deps),
    { ok: true, imageId: null, available: false, reason: 'there is no image for it on FamilySearch' });
});

test('every refusal is told in plain words, and none of them carries the login', async () => {
  const cases = [
    [{ '1:1:': 401 }, /asked you to sign in \(HTTP 401\)/],
    [{ '1:1:': 403 }, /restricted to a FamilySearch center/],
    [{ '1:1:': 429 }, /slow down \(HTTP 429\)/],
    [{ '1:1:': 500 }, /having trouble \(HTTP 500\)/],
    [{ '1:1:': 'network' }, /could not reach FamilySearch - check your internet connection/],
    [{ '1:1:': { ok: true, status: 200, text: async () => '<html>' } }, /something other than its record data/],
  ];
  for (const [over, want] of cases) {
    const info = await fsr.fsRecordInfo(PAGE, service(over).deps);
    assert.strictEqual(info.ok, false);
    assert.match(info.error, want);
    assert.ok(!info.error.includes(TOKEN));
  }
  // Signed out: a 403 means "sign in", not "restricted".
  const out = service({ '1:1:': 403 });
  out.deps.token = null;
  assert.match((await fsr.fsRecordInfo(PAGE, out.deps)).error, /asked you to sign in \(HTTP 403\)/);
  for (const over of [{ 'dist.jpg': 429 }, { 'dist.jpg': 'network' }, { '3:1:': 401 }]) {
    const r = await fsr.fsImage('HRT1-IMG1', service(over).deps);
    assert.strictEqual(r.ok, false);
    assert.ok(!r.error.includes(TOKEN));
  }
  const small = service({ 'dist.jpg': { ok: true, status: 200, blob: async () => new Blob(['tiny'], { type: 'image/jpeg' }) } });
  assert.match((await fsr.fsImage('HRT1-IMG1', small.deps)).error, /too small to be the full record/);
});

// ── PR #214 review ───────────────────────────────────────────────────────────

test('a record with two principals is named and dated for the persona opened (audit #6)', () => {
  // A marriage: the bride's entry and the groom's are both principals.
  const persist = (id) => ({ 'http://gedcomx.org/Persistent': ['https://familysearch.org/ark:/61903/1:1:' + id] });
  const g = {
    persons: [
      { id: 'p1', principal: true, identifiers: persist('HRT1-GRM1'),
        names: [{ nameForms: [{ fullText: 'Calvin Hartley' }] }], facts: [] },
      { id: 'p2', principal: true, identifiers: persist('HRT1-BRD1'),
        names: [{ nameForms: [{ fullText: 'Harriet Webb' }] }],
        facts: [{ type: 'http://gedcomx.org/Birth', date: { formal: '+1845' } }] },
    ],
    relationships: [{ person1: { resource: '#p1' }, person2: { resource: '#p2' },
      facts: [{ type: 'http://gedcomx.org/Marriage', date: { formal: '+1866-10-12' } }] }],
    sourceDescriptions: [
      { about: 'https://familysearch.org/platform/records/collections/9000003', resourceType: 'http://gedcomx.org/Collection',
        titles: [{ value: 'Kansas, County Marriages, 1855-1911' }] },
    ],
  };
  const bride = fsr.fsRecordFrom(g, { fsId: 'HRT1-BRD1' });
  assert.strictEqual(bride.record.name, 'Harriet Webb');
  assert.strictEqual(fsr.fsRecordFrom(g, { fsId: 'HRT1-GRM1' }).record.name, 'Calvin Hartley');
  assert.strictEqual(fsr.fsHints(bride.record, g).sourceDate, '1866-10-12');
  assert.strictEqual(fsr.fsTitle(bride.record, ''), 'Harriet Webb - Kansas, County Marriages, 1855-1911');
  // No identifiers at all: the principal, as before.
  const bare = JSON.parse(JSON.stringify(g));
  bare.persons.forEach((p) => delete p.identifiers);
  assert.strictEqual(fsr.fsRecordFrom(bare, { fsId: 'HRT1-BRD1' }).record.name, 'Calvin Hartley');
});

test('a refused image description is told as the image, not the record (audit #8)', async () => {
  let r = await fsr.fsImage('HRT1-IMG1', service({ '3:1:': 403 }).deps);
  assert.match(r.error, /will not show this image here \(HTTP 403\)/);
  assert.doesNotMatch(r.error, /record/);
  r = await fsr.fsImage('HRT1-IMG1', service({ '3:1:': 404 }).deps);
  assert.match(r.error, /no image at this address \(HTTP 404\)/);
  const check = await fsr.fsImageCheck('HRT1-IMG1', service({ '3:1:': 403 }).deps);
  assert.match(check.reason, /will not show this image/);
  r = await fsr.fsImage('HRT1-IMG1', service({ '3:1:': { ok: true, status: 200, text: async () => '<html>' } }).deps);
  assert.match(r.error, /something other than the image’s description/);
  // The record's own refusal still says record.
  assert.match((await fsr.fsRecordInfo(PAGE, service({ '1:1:': 403 }).deps)).error, /will not show this record/);
});

test('Codex 4503786095/-098: an image FamilySearch will not send now says so (slow down, passes), and one it never will does not', async () => {
  const deps = (answer) => ({
    base: PAGE, token: TOKEN, toBase64: async () => 'AAAA',
    fetch: async (url) => (/\/ark:\/61903\/3:1:/.test(url) ? json(FIX.images['HRT1-IMG1']) : answer()),
  });
  for (const [status, slowDown, transient] of [[429, true, true], [503, true, true], [500, false, true],
    [403, false, false], [404, false, false]]) {
    const got = await fsr.fsImage('HRT1-IMG1', deps(async () => ({ ok: false, status })));
    assert.strictEqual(got.ok, false);
    assert.strictEqual(!!got.slowDown, slowDown, 'HTTP ' + status + ' slowDown');
    assert.strictEqual(!!got.transient, transient, 'HTTP ' + status + ' transient');
  }
  const dropped = await fsr.fsImage('HRT1-IMG1', deps(async () => { throw new TypeError('Failed to fetch'); }));
  assert.strictEqual(dropped.transient, true);
  // The image's description itself refused with a 503: the same.
  const desc = await fsr.fsImage('HRT1-IMG1', { base: PAGE, token: TOKEN, toBase64: async () => 'AAAA',
    fetch: async () => ({ ok: false, status: 503, text: async () => '' }) });
  assert.deepStrictEqual([desc.slowDown, desc.transient], [true, true]);
});
