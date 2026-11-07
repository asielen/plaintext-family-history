// Tests for the owner's batch downloads, the pure halves: the ledgers, the
// pacing and what a failure means (lib/capture-batch.js), and the readers
// that fill a queue from a page (lib/batch-sources.js). Fictional Hartleys,
// invented ids.
//
// What it guards, in the owner's terms:
//   - a record done stays done, whatever is queued again, and a queue built
//     by the archive copy of the add-on (0.6.x) carries over;
//   - only an address the batch builds from a checked id is ever visited;
//   - the first "slow down" means a long break, the third in a row a longer
//     one; a refusal stops; only "this is not the record" is set aside;
//   - a FamilySearch tree person's sources are read with the login sent only
//     to FamilySearch; Ancestry and Find a Grave pages are read without any
//     request; the Newspapers.com list is asked of Newspapers.com only.
//
//   node --test browser-companion/tests/test-capture-batch.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const CB = require('../src/lib/capture-batch.js');
const BS = require('../src/lib/batch-sources.js');
const { parse } = require('./mini-dom.js');

const SAMPLES = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'capture-samples');
const TOKEN = 'fs-session-SECRET-0000';

// ── what fills a queue ───────────────────────────────────────────────────────

test('parseGedcom: every _APID an Ancestry tree export cites, with the people it is cited for', () => {
  const ged = [
    '0 HEAD', '1 SOUR Ancestry.com Family Trees',
    '0 @I1@ INDI', '1 NAME Calvin George /Hartley/', '1 BIRT', '2 SOUR @S1@', '3 _APID 1,90001::1000001',
    '1 DEAT', '2 SOUR @S2@', '3 _APID 1,90002::2000009',
    '0 @I2@ INDI', '1 NAME Harriet /Hartley/', '1 RESI', '2 SOUR @S1@', '3 _APID 1,90001::1000001',
    '0 @F1@ FAM', '1 MARR', '2 SOUR @S3@', '3 _APID 1,90003::3000001',
    '1 NOTE _APID 1,bad::value', '0 TRLR',
  ].join('\n');
  assert.deepStrictEqual(CB.parseGedcom(ged), [
    { dbId: '90001', recordId: '1000001', persons: ['Calvin George Hartley', 'Harriet Hartley'] },
    { dbId: '90002', recordId: '2000009', persons: ['Calvin George Hartley'] },
    { dbId: '90003', recordId: '3000001', persons: [] },
  ]);
  assert.deepStrictEqual(CB.parseGedcom(''), []);
});

test('parseMemorialList: memorials named in any text, once each, never a photo page as a slug', () => {
  const text = 'See https://www.findagrave.com/memorial/900000001/calvin-george-hartley and ' +
    'findagrave.com/memorial/900000002/harriet-frances-hartley/photo, again ' +
    'https://www.findagrave.com/memorial/900000001/calvin-george-hartley#x, memorial ID: 900000003.';
  assert.deepStrictEqual(CB.parseMemorialList(text), [
    { memorialId: '900000001', slug: 'calvin-george-hartley' },
    { memorialId: '900000002', slug: 'harriet-frances-hartley' },
    { memorialId: '900000003', slug: null },
  ]);
});

// ── the ledger ───────────────────────────────────────────────────────────────

test('addRecords: a done record stays done; a malformed id never becomes an address', () => {
  const L = CB.newLedger();
  assert.strictEqual(CB.addRecords('ancestry', L, [
    { dbId: '90001', recordId: '1000001', persons: ['Calvin Hartley'] },
    { dbId: '90001/../../evil', recordId: '1' },
    { dbId: '90001', recordId: '1000001?x=1' },
    null,
  ], 'test'), 1);
  assert.strictEqual(L.items['90001:1000001'].url, 'https://www.ancestry.com/search/collections/90001/records/1000001');
  CB.markDone(L, '90001:1000001', 'calvin-bundle');
  assert.strictEqual(CB.addRecords('ancestry', L, [{ dbId: '90001', recordId: '1000001', persons: ['Harriet Hartley'] }]), 0);
  assert.strictEqual(L.items['90001:1000001'].state, 'done');
  assert.deepStrictEqual(L.items['90001:1000001'].persons, ['Calvin Hartley', 'Harriet Hartley']);
  assert.strictEqual(CB.nextPending(L), null);

  const F = CB.newLedger();
  CB.addRecords('familysearch', F, [{ fsId: 'hrt1-cen1' }, { fsId: 'HRT1/../x' }]);
  assert.deepStrictEqual(F.order, ['HRT1-CEN1']);
  assert.strictEqual(F.items['HRT1-CEN1'].url, 'https://www.familysearch.org/ark:/61903/1:1:HRT1-CEN1');
  const G = CB.newLedger();
  CB.addRecords('findagrave', G, [{ memorialId: '900000001', slug: 'calvin-george-hartley' },
    { memorialId: '900000002', slug: '../../x?y' }, { memorialId: 'abc' }]);
  assert.deepStrictEqual(G.order.map((k) => G.items[k].url), [
    'https://www.findagrave.com/memorial/900000001/calvin-george-hartley',
    'https://www.findagrave.com/memorial/900000002',
  ]);
});

test('addClippings: only her own, only on Newspapers.com, grouped by newspaper page', () => {
  const L = CB.newLedger();
  const c = (id, page, created, url, who) => ({ clipping_id: id, page_id: page, created, url, title: 'clip ' + id,
    user_username: who || 'HartleyResearch' });
  CB.addClippings(L, [
    c(3, 20, '2025-01-03', '/article/x/3/'),
    c(1, 10, '2025-01-01', '/article/x/1/'),
    c(2, 20, '2025-01-02', '/article/x/2/'),
    c(4, 10, '2025-01-04', '/article/x/4/'),
    c(5, 10, '2025-01-05', '/article/x/5/', 'otherclipper'),
    c(6, 10, '2025-01-06', 'https://evil.example/article/x/6/'),
    c(7, 10, '2025-01-07', '/image/5550001/'),
  ], 'hartleyresearch');
  assert.deepStrictEqual(L.order, ['1', '4', '2', '3']);
  assert.strictEqual(L.items['1'].url, 'https://www.newspapers.com/article/x/1/');
  // No signed-in name, nothing queued.
  const M = CB.newLedger();
  assert.strictEqual(CB.addClippings(M, [c(1, 10, '', '/article/x/1/')], null), 0);
});

test('a queue built by the archive copy (0.6.x) carries over: "retry" is still to do, its image list is never used', () => {
  const old = {
    version: 1, created: '2026-10-12T00:00:00.000Z',
    items: {
      '90001:1000001': { key: '90001:1000001', url: 'https://www.ancestry.com/search/collections/90001/records/1000001',
        dbId: '90001', recordId: '1000001', persons: [], state: 'done', bundle: 'b1' },
      '90001:1000002': { key: '90001:1000002', url: 'https://www.ancestry.com/search/collections/90001/records/1000002',
        dbId: '90001', recordId: '1000002', persons: [], state: 'retry', tries: 1 },
      '90002:2000001': { key: '90002:2000001', url: 'https://www.ancestry.com/search/collections/90002/records/2000001',
        dbId: '90002', recordId: '2000001', persons: [], state: 'failed', tries: 2 },
    },
    order: ['90001:1000001', '90001:1000002', '90002:2000001'],
    images: { '90001:4000123_00045': 'b1' },
    pacing: { sinceShort: 3, shortEvery: 40, rateEvents: 1, resumeAt: 0, resumeReason: '' },
    log: [],
  };
  const L = CB.repair(JSON.parse(JSON.stringify(old)));
  assert.strictEqual(CB.nextPending(L).key, '90001:1000002');
  assert.deepStrictEqual(CB.counts(L), { total: 3, done: 1, queued: 1, failed: 1 });
  // Nothing in the library reads `images`: a page image counts as saved
  // only from a saved file (Seen It), never from a ledger's say-so.
  const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'lib', 'capture-batch.js'), 'utf8') +
    fs.readFileSync(path.join(__dirname, '..', 'src', 'batch.js'), 'utf8');
  assert.ok(!/\.images\b/.test(src));
  assert.strictEqual(CB.repair(null), null);
  assert.deepStrictEqual(CB.repair({ items: { a: { state: 'queued', url: 'x' } } }).order, ['a']);
});

test('mergeDone: what another window stored is folded in, and a done record stays done', () => {
  const L = CB.newLedger();
  CB.addRecords('ancestry', L, [{ dbId: '1', recordId: '2' }]);
  const stored = JSON.parse(JSON.stringify(L));
  CB.addRecords('ancestry', stored, [{ dbId: '3', recordId: '4' }]);
  CB.markDone(stored, '1:2', 'by hand');
  assert.strictEqual(CB.mergeDone(L, stored), 2);
  assert.strictEqual(L.items['1:2'].state, 'done');
  assert.deepStrictEqual(L.order, ['1:2', '3:4']);
  // The other way round, a stored "queued" never undoes a done one.
  const back = JSON.parse(JSON.stringify(L));
  back.items['1:2'].state = 'queued';
  CB.mergeDone(L, back);
  assert.strictEqual(L.items['1:2'].state, 'done');
});

// ── pacing and what a failure means ──────────────────────────────────────────

test('a "slow down" means a half-hour break; the third in a row about four hours; a clean capture ends the run of them', () => {
  const L = CB.newLedger();
  const now = 1e12;
  const mid = () => 0.5;
  let plan = CB.planRateLimit(L, now, mid);
  assert.strictEqual(plan.kind, 'ratelimit');
  assert.ok(plan.until - now >= 30 * 60e3 && plan.until - now <= 35 * 60e3);
  assert.strictEqual(L.pacing.resumeAt, plan.until);
  CB.planRateLimit(L, now, mid);
  plan = CB.planRateLimit(L, now, mid);
  assert.strictEqual(plan.kind, 'long');
  assert.ok(plan.until - now >= 3.7 * 3600e3);
  assert.strictEqual(L.pacing.rateEvents, 0);
  CB.planRateLimit(L, now, mid);
  CB.planAfterCapture(L, now, mid);
  assert.strictEqual(L.pacing.rateEvents, 0);
  plan = CB.planAfterCapture(L, now, mid);
  assert.strictEqual(plan.kind, 'between');
  assert.ok(plan.until - now >= 45e3 && plan.until - now <= 90e3);
});

test('a short break every 30-60 records', () => {
  const L = CB.newLedger();
  CB.ensurePacing(L, () => 0);
  assert.strictEqual(L.pacing.shortEvery, 30);
  const kinds = [];
  for (let i = 0; i < 30; i++) kinds.push(CB.planAfterCapture(L, 0, () => 0).kind);
  assert.deepStrictEqual(kinds.slice(0, 29).every((k) => k === 'between'), true);
  assert.strictEqual(kinds[29], 'short');
});

test('classifyError: slow down, stop, or set aside - and anything unknown stops', () => {
  const table = [
    ['Newspapers.com kept asking us to slow down (HTTP 429), so the capture stopped', 'slowdown'],
    ['FamilySearch asked us to slow down (HTTP 429). Wait a few minutes', 'slowdown'],
    ['the Ancestry image service returned HTTP 503', 'slowdown'],
    ['FamilySearch asked you to sign in (HTTP 401). Sign in to FamilySearch', 'stop'],
    ['Find a Grave refused the memorial (HTTP 403). Reload the page', 'stop'],
    ['Newspapers.com showed a security check page', 'stop'],
    ['could not reach FamilySearch - check your internet connection', 'stop'],
    ['the page in the tab changed while this record was being saved', 'stop'],
    ['something nobody expected', 'stop'],
    ['', 'stop'],
    ['FamilySearch has no record at this address (HTTP 404) - check the page', 'skip'],
    ['Find a Grave has no memorial at this address (HTTP 404): it may have been removed or merged', 'skip'],
    ['the page shows Ancestry record 90001:1000002, not the one queued, so nothing was saved', 'skip'],
    ['the page it opened is not a record page (it may have moved or been removed)', 'skip'],
  ];
  for (const [m, want] of table) assert.strictEqual(CB.classifyError(m), want, m);
});

test('the reading copy of a ledger carries no login and says what "done" means', () => {
  const L = CB.newLedger();
  CB.addRecords('familysearch', L, [{ fsId: 'HRT1-CEN1', title: 'Calvin Hartley' }]);
  const body = CB.ledgerFileBody('familysearch', L);
  assert.match(body.note, /^FamilySearch batch download ledger/);
  assert.strictEqual(body.entries.length, 1);
  assert.ok(!JSON.stringify(body).includes(TOKEN));
});

// ── the readers that fill a queue ────────────────────────────────────────────

const ANC_PERSON = '<html><body><h1>Calvin George Hartley<span class="nsi-pills">Seen 1×</span></h1>' +
  '<section id="sourcesSection">' +
  '<a href="/search/collections/90001/records/1000001">1880 United States Federal Census</a>' +
  '<a href="/discoveryui-content/view/2000001:90002">California, U.S., Death Index</a>' +
  '<a href="/interactive/90003/img_0042/3000001">Kansas State Census</a>' +
  '<a href="https://www.ancestry.com/search/collections/90001/records/1000001">again</a>' +
  '<a href="https://www.newspapers.com/article/x/101/">clipping</a>' +
  '<a href="https://www.findagrave.com/memorial/900000001/calvin-george-hartley">grave</a>' +
  '<a href="https://www.findagrave.com/memorial/900000002">grave 2</a>' +
  '</section></body></html>';

test('batchAncPersonSources: the Ancestry records a tree person cites, the rest counted, Seen It badges skipped', () => {
  const r = BS.batchAncPersonSources(parse(ANC_PERSON), 'https://www.ancestry.com/family-tree/person/tree/1/person/2/facts');
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.person, 'Calvin George Hartley');
  assert.deepStrictEqual(r.sources.map((s) => [s.dbId, s.recordId, s.title]), [
    ['90001', '1000001', '1880 United States Federal Census'],
    ['90002', '2000001', 'California, U.S., Death Index'],
    ['90003', '3000001', 'Kansas State Census'],
  ]);
  assert.strictEqual(r.other, 3);
  assert.strictEqual(r.otherWhat, 'Newspapers.com clippings, links to findagrave.com (2)');
});

function fsService(answers) {
  const calls = [];
  return {
    calls,
    fetch: async (url, init) => {
      calls.push({ url, auth: !!(init.headers && init.headers.Authorization), credentials: init.credentials });
      const k = Object.keys(answers).find((x) => url.endsWith(x));
      const v = k ? answers[k] : 404;
      if (typeof v === 'number') return { ok: false, status: v, text: async () => '' };
      return { ok: true, status: 200, text: async () => JSON.stringify(v) };
    },
  };
}
const FS_PERSON = 'https://www.familysearch.org/tree/person/details/KWHR-T01';
const FS_SOURCES = {
  '/platform/tree/persons/KWHR-T01/sources': { sourceDescriptions: [
    { about: 'https://www.familysearch.org/ark:/61903/1:1:HRT1-CEN1', titles: [{ value: 'Calvin Hartley, 1880 census' }] },
    { about: 'https://www.familysearch.org/ark:/61903/1:1:hrt1-dth1' },
    { about: 'https://www.findagrave.com/memorial/900000001/calvin-george-hartley', titles: [{ value: 'Grave' }] },
    { about: 'https://example.org/hartleys' },
    { titles: [{ value: 'A note' }] },
  ] },
  '/platform/tree/persons/KWHR-T01': { persons: [{ display: { name: 'Calvin George Hartley' } }] },
};

test('batchFsPersonSources: FamilySearch records among a tree person\'s sources, the login sent to FamilySearch only', async () => {
  const svc = fsService(FS_SOURCES);
  const r = await BS.batchFsPersonSources(FS_PERSON, { fetch: svc.fetch, base: FS_PERSON, token: TOKEN });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.person, 'Calvin George Hartley');
  assert.deepStrictEqual(r.sources, [
    { fsId: 'HRT1-CEN1', title: 'Calvin Hartley, 1880 census' },
    { fsId: 'HRT1-DTH1', title: '' },
  ]);
  assert.strictEqual(r.other, 3);
  assert.strictEqual(svc.calls.length, 2);
  for (const c of svc.calls) {
    assert.match(c.url, /^https:\/\/www\.familysearch\.org\/platform\/tree\/persons\/KWHR-T01/);
    assert.strictEqual(c.auth, true);
  }
  assert.ok(!JSON.stringify(r).includes(TOKEN));
  // Not a tree person: nothing is asked.
  const none = fsService({});
  const no = await BS.batchFsPersonSources('https://www.familysearch.org/search/record/results', {
    fetch: none.fetch, base: FS_PERSON, token: TOKEN });
  assert.strictEqual(no.ok, false);
  assert.strictEqual(none.calls.length, 0);
  // A page that only looks like FamilySearch is not one.
  assert.strictEqual(BS.batchFsPersonPid('https://familysearch.org.evil.example/tree/person/details/KWHR-T01'), null);
});

test('batchFagLinks: the memorial first, then the ones it links to; on FamilySearch the login stays with FamilySearch', async () => {
  const page = 'https://www.findagrave.com/memorial/900000001/calvin-george-hartley';
  const r = await BS.batchFagLinks(parse(fs.readFileSync(path.join(SAMPLES, 'findagrave-memorial.html'), 'utf8')), page, null);
  assert.strictEqual(r.person, 'Calvin George Hartley');
  assert.deepStrictEqual(r.sources.map((s) => s.memorialId), ['900000001', '900000004', '900000002', '900000003']);
  // On an Ancestry person page: its web links, nothing fetched.
  const a = await BS.batchFagLinks(parse(ANC_PERSON), 'https://www.ancestry.com/family-tree/person/tree/1/person/2/facts', null);
  assert.deepStrictEqual(a.sources.map((s) => [s.memorialId, s.slug]), [['900000001', 'calvin-george-hartley'], ['900000002', null]]);
  // A FamilySearch tree person: the memorials among the sources.
  const svc = fsService(FS_SOURCES);
  const f = await BS.batchFagLinks(parse('<html><h1>Calvin</h1></html>'), FS_PERSON, { fetch: svc.fetch, base: FS_PERSON, token: TOKEN });
  assert.deepStrictEqual(f.sources.map((s) => s.memorialId), ['900000001']);
  assert.ok(svc.calls.every((c) => /^https:\/\/www\.familysearch\.org\//.test(c.url) && c.auth));
});

test('batchNpClippingsList: her own list, from Newspapers.com only; a 429 is a "slow down"', async () => {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init });
    return { ok: true, status: 200, json: async () => ({ clippings: [{ clipping_id: 1 }], next_cursor_mark: 24, more_results: true }) };
  };
  const base = 'https://www.newspapers.com/clippings/my-clippings/';
  const r = await BS.batchNpClippingsList(0, 'HartleyResearch', { fetch, base });
  assert.deepStrictEqual([r.ok, r.user, r.nextCursor, r.more, r.clippings.length], [true, 'HartleyResearch', 24, true, 1]);
  assert.strictEqual(calls[0].url, 'https://www.newspapers.com/api/clipping/list?user=HartleyResearch&visibility=all' +
    '&count=24&cursor_mark=0&product_id=1');
  assert.strictEqual(calls[0].init.credentials, 'same-origin');
  // Not on Newspapers.com, or nobody signed in: nothing is asked.
  for (const [b, u] of [['https://newspapers.com.evil.example/x', 'HartleyResearch'], ['http://www.newspapers.com/', 'HartleyResearch'],
    [base, null]]) {
    const before = calls.length;
    const no = await BS.batchNpClippingsList(0, u, { fetch, base: b });
    assert.strictEqual(no.ok, false);
    assert.strictEqual(calls.length, before);
  }
  const slow = await BS.batchNpClippingsList(0, 'HartleyResearch', { base, fetch: async () => ({ ok: false, status: 429 }) });
  assert.strictEqual(CB.classifyError(slow.error), 'slowdown');
  const refused = await BS.batchNpClippingsList(0, 'HartleyResearch', { base, fetch: async () => ({ ok: false, status: 403 }) });
  assert.strictEqual(CB.classifyError(refused.error), 'stop');
});

test('batchNpUsername: who is signed in, from the page\'s own data', () => {
  assert.strictEqual(BS.batchNpUsername('1:{"user":{"id":7,"username":"HartleyResearch","x":{"y":1}}}'), 'HartleyResearch');
  assert.strictEqual(BS.batchNpUsername('nothing here'), null);
});
