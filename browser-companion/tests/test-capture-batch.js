// Tests for the owner's batch downloads, the pure halves: the ledgers, the
// pacing and what a failure means (lib/capture-batch.js), the one writer of
// the ledgers (lib/batch-store.js, the service worker's), and the readers
// that fill a queue from a page (lib/batch-sources.js). Fictional Hartleys,
// invented ids.
//
// What it guards, in the owner's terms:
//   - a record done stays done, whatever is queued again, and a queue built
//     by the archive copy of the add-on (0.6.x), or by 0.8.0, carries over;
//   - no change one window asks for is lost to another's, and a change asked
//     for twice (its answer lost) is made once;
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
const STORE = require('../src/lib/batch-store.js');
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
  assert.deepStrictEqual(CB.counts(L), { total: 3, done: 1, queued: 1, failed: 1, partial: 0 });
  // Nothing in the library reads `images`: a page image counts as saved
  // only from a saved file (Seen It), never from a ledger's say-so.
  const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'lib', 'capture-batch.js'), 'utf8') +
    fs.readFileSync(path.join(__dirname, '..', 'src', 'batch.js'), 'utf8');
  assert.ok(!/\.images\b/.test(src));
  assert.strictEqual(CB.repair(null), null);
  assert.deepStrictEqual(CB.repair({ items: { a: { state: 'queued', url: 'x' } } }).order, ['a']);
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

test('classifyError: one record\'s cause sets it aside; the run\'s own cause stops it (PR #224 audit F1/F6/F13/F15)', () => {
  const table = [
    ['Newspapers.com kept asking us to slow down (HTTP 429), so the capture stopped', 'slowdown'],
    ['FamilySearch asked us to slow down (HTTP 429). Wait a few minutes', 'slowdown'],
    ['the Ancestry image service returned HTTP 503', 'slowdown'],
    // The run's own causes: stop.
    ['FamilySearch asked you to sign in (HTTP 401). Sign in to FamilySearch', 'stop'],
    ['FamilySearch asked you to sign in (HTTP 403). Sign in to FamilySearch in this tab', 'stop'],
    ['Newspapers.com showed a security check page', 'stop'],
    ['could not reach FamilySearch - check your internet connection', 'stop'],
    ['the page in the tab changed while this record was being saved', 'stop'],
    ['the download was blocked', 'stop'],
    ['a capture is already saving', 'stop'],
    ['a different member is signed in to Newspapers.com than the one this list was started for', 'stop'],
    // One record's causes: set aside, and go on.
    ['FamilySearch has no record at this address (HTTP 404) - check the page', 'skip'],
    ['Find a Grave has no memorial at this address (HTTP 404): it may have been removed or merged', 'skip'],
    ['the page shows Ancestry record 90001:1000002, not the one queued, so nothing was saved', 'skip'],
    ['the clipping shown is not the one queued (202), so nothing was saved', 'skip'],
    ['the page it opened is not a record page (it may have moved or been removed)', 'skip'],
    ['tick at least one of your clippings', 'skip'],
    ['Newspapers.com: this clipping marks no area on the newspaper page that can be cut out, so nothing is saved ' +
      'automatically.', 'skip'],
    ['FamilySearch will not show this record here (HTTP 403): it is restricted to a FamilySearch center or ' +
      'affiliate library', 'skip'],
    // A memorial page with no memorial on it may be a busy page: once more, never "removed".
    ['this page shows no memorial - it may have been removed or merged into another. Search Find a Grave', 'retry'],
    // Anything else: stop at once, and set aside if the same record is refused again.
    ['Find a Grave refused the memorial (HTTP 403). Reload the page', 'refused'],
    // A site having trouble (any 5xx) is the run's cause, never the record's (D3).
    ['FamilySearch is having trouble (HTTP 500). Try again in a little while', 'stop'],
    ['FamilySearch is having trouble (HTTP 502). Try again in a little while', 'stop'],
    ['something nobody expected', 'refused'],
    ['', 'refused'],
  ];
  for (const [m, want] of table) assert.strictEqual(CB.classifyError(m), want, m);
});

test('an archive-era Newspapers.com queue (numeric order) is never doubled when the list is read again (F3/F7)', () => {
  // The archive copy (0.6.x) stored the order as numbers.
  const old = {
    version: 1, user: 'HartleyResearch', listComplete: false, listCursor: 0,
    items: {
      5: { id: 5, url: 'https://www.newspapers.com/article/x/5/', pageId: 10, created: '2025-01-01', state: 'done' },
      7: { id: 7, url: 'https://www.newspapers.com/article/x/7/', pageId: 10, created: '2025-01-02', state: 'queued' },
    },
    order: [5, 7, 5], log: [],
  };
  const L = CB.repair(JSON.parse(JSON.stringify(old)));
  assert.deepStrictEqual(L.order, ['5', '7']);
  const c = (id) => ({ clipping_id: id, page_id: 10, created: '2025-01-0' + id, url: '/article/x/' + id + '/',
    user_username: 'HartleyResearch' });
  assert.strictEqual(CB.addClippings(L, [c(5), c(7), c(9)], 'HartleyResearch'), 1);
  assert.deepStrictEqual(L.order, ['5', '7', '9']);
  CB.addClippings(L, [c(5), c(9)], 'HartleyResearch');
  assert.deepStrictEqual(L.order, ['5', '7', '9']);
  assert.deepStrictEqual(CB.counts(L), { total: 3, done: 1, queued: 2, failed: 0, partial: 0 });
  // A queue never reloaded still never counts twice.
  const raw = JSON.parse(JSON.stringify(old));
  CB.addClippings(raw, [c(9)], 'HartleyResearch');
  assert.deepStrictEqual(raw.order, ['5', '7', '9']);
});

test('saved in part: not done; tried again at the next Start, and counted apart (F2/F8/F14)', () => {
  const L = CB.newLedger();
  CB.addRecords('findagrave', L, [{ memorialId: '900000001' }, { memorialId: '900000002' }]);
  CB.markPartial(L, '900000001', 'b1', '0 photos saved, 3 not saved');
  assert.strictEqual(L.items['900000001'].state, 'partial');
  assert.deepStrictEqual(CB.counts(L), { total: 2, done: 0, queued: 1, failed: 0, partial: 1 });
  // Not again in the same run...
  assert.strictEqual(CB.nextPending(L).key, '900000002');
  CB.markDone(L, '900000002', 'b2');
  assert.strictEqual(CB.nextPending(L), null);
  // ...but at the next Start.
  assert.strictEqual(CB.requeuePartials(L), 1);
  assert.strictEqual(CB.nextPending(L).key, '900000001');
  assert.strictEqual(CB.nextPending(L).again, true);
  // Still in part after MAX_PARTIAL tries: done, saying what is missing.
  CB.markPartial(L, '900000001', 'b3', 'still missing');
  CB.requeuePartials(L);
  CB.markPartial(L, '900000001', 'b4', 'still missing');
  assert.strictEqual(L.items['900000001'].state, 'done');
  assert.match(L.items['900000001'].note, /still incomplete after 3 tries/);
});

test('set aside is never for ever: "Try the set-aside ones again" queues them all again (F9)', () => {
  const L = CB.newLedger();
  CB.addRecords('ancestry', L, [{ dbId: '1', recordId: '2' }, { dbId: '3', recordId: '4' }]);
  CB.markSetAside(L, '1:2', 'not the one queued');
  CB.markRetry(L, '3:4', 'this page shows no memorial');
  assert.strictEqual(L.items['3:4'].state, 'retry');
  CB.markRetry(L, '3:4', 'this page shows no memorial');
  assert.strictEqual(L.items['3:4'].state, 'failed');
  assert.strictEqual(CB.nextPending(L), null);
  assert.strictEqual(CB.requeueSetAside(L), 2);
  assert.strictEqual(L.items['1:2'].state, 'queued');
  assert.strictEqual(L.items['1:2'].tries, 0);
  assert.strictEqual(L.items['1:2'].setAsideBefore, 'not the one queued');
  assert.strictEqual(CB.nextPending(L).key, '1:2');
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
  // Stops the run, and never counts against a record: her clippings list is
  // no record, and its 403 says to sign in (PR #224 X1 correction).
  assert.ok(['stop', 'refused'].includes(CB.classifyError(refused.error)));
  assert.deepStrictEqual(CB.stopRule(refused.error), { counts: false });
});

test('batchNpUsername: who is signed in, from the page\'s own data', () => {
  assert.strictEqual(BS.batchNpUsername('1:{"user":{"id":7,"username":"HartleyResearch","x":{"y":1}}}'), 'HartleyResearch');
  assert.strictEqual(BS.batchNpUsername('nothing here'), null);
});

// ── PR #224 Codex 4504294850: each page reader only in its own context ──────

test('Codex 4504294850: a record page (no sources list) is not a person\'s sources - nothing is read from it', async () => {
  const census = parse(fs.readFileSync(path.join(SAMPLES, 'ancestry-record-census.html'), 'utf8'));
  const url = 'https://www.ancestry.com/search/collections/90001/records/1000001';
  const r = BS.batchAncPersonSources(census, url);
  assert.deepStrictEqual([r.ok, r.sectionFound, r.sources.length, r.total], [true, false, 0, 0]);
  // Find a Grave links on an Ancestry page: only from the sources list too.
  const page = parse('<html><h1>Calvin Hartley</h1><div class="hints"><a href="https://www.findagrave.com/memorial/900000009">' +
    'A hint</a></div></html>');
  const f = await BS.batchFagLinks(page, url, null);
  assert.deepStrictEqual([f.sectionFound, f.sources.length], [false, 0]);
  // ...and on FamilySearch, the person's own sources only, never the page's links.
  const fsPage = parse('<html><h1>Calvin</h1><a href="https://www.findagrave.com/memorial/900000009">a hint</a></html>');
  const g = await BS.batchFagLinks(fsPage, 'https://www.familysearch.org/search/record/results', null);
  assert.deepStrictEqual([g.sectionFound, g.sources.length], [false, 0]);
  // Anywhere else, nothing.
  const h = await BS.batchFagLinks(parse(ANC_PERSON), 'https://blog.example.org/hartleys', null);
  assert.strictEqual(h.sources.length, 0);
});

test('Codex 4504294846: a FamilySearch "slow down" while sources are read is said, not lost', async () => {
  const slow = fsService({ '/platform/tree/persons/KWHR-T01/sources': 429 });
  const r = await BS.batchFsPersonSources(FS_PERSON, { fetch: slow.fetch, base: FS_PERSON, token: TOKEN });
  assert.deepStrictEqual([r.ok, r.slowDown], [false, true]);
  const busy = fsService({ '/platform/tree/persons/KWHR-T01/sources': 503 });
  const f = await BS.batchFagLinks(parse('<html><h1>Calvin</h1></html>'), FS_PERSON, { fetch: busy.fetch, base: FS_PERSON, token: TOKEN });
  assert.strictEqual(f.slowDown, true);
  const named = fsService(Object.assign({}, FS_SOURCES, { '/platform/tree/persons/KWHR-T01': 429 }));
  const n = await BS.batchFsPersonSources(FS_PERSON, { fetch: named.fetch, base: FS_PERSON, token: TOKEN });
  assert.deepStrictEqual([n.ok, n.slowDown, n.sources.length], [true, true, 2]);
});

test('Codex 4504508005: a clippings list that breaks off is said to pass; one that is not JSON is something else', async () => {
  const base = 'https://www.newspapers.com/clippings/my-clippings/';
  const cut = await BS.batchNpClippingsList(0, 'HartleyResearch', { base,
    fetch: async () => ({ ok: true, status: 200, json: async () => { throw new TypeError('network error'); } }) });
  assert.deepStrictEqual([cut.ok, cut.transient], [false, true]);
  assert.strictEqual(CB.classifyError(cut.error), 'stop');
  const odd = await BS.batchNpClippingsList(0, 'HartleyResearch', { base,
    fetch: async () => ({ ok: true, status: 200, json: async () => { throw new SyntaxError('Unexpected token <'); } }) });
  assert.deepStrictEqual([odd.ok, !!odd.transient], [false, false]);
  for (const m of ['Failed to fetch', 'NetworkError when attempting to fetch resource.', 'Load failed']) {
    assert.strictEqual(CB.classifyError(m), 'stop', m);
  }
});

test('third audit M1, X1: every stop cause is sorted on the cause - RUN causes never count, RECORD and unforeseen causes do', () => {
  const RUN = { counts: false };
  const TROUBLE = { counts: true, max: CB.MAX_TROUBLE_TRIES, trouble: true };
  const FAULT = { counts: true, max: CB.MAX_TRIES, trouble: false };
  const UNFORESEEN = { counts: true, max: CB.MAX_TROUBLE_TRIES, trouble: false };
  const SIGN_IN = { counts: false, signIn: true };
  const NP = (cause) => 'Newspapers.com: automatic saving is not available here: ' + cause +
    '. Reload the page and open the panel again, or drop in a file.';
  const table = [
    ['could not reach FamilySearch - check your internet connection, then press Capture again', RUN],
    ['the connection broke off (Failed to fetch) - press Capture again later', RUN],
    ['Failed to fetch', RUN],
    ['the download was blocked', RUN],
    ['download interrupted: NETWORK_FAILED', RUN],
    ['the page in the tab changed while this record was being saved, so nothing was saved', RUN],
    ['the batch’s tab was closed', RUN],
    ['a capture is already saving', RUN],
    ['a different member is signed in to Newspapers.com than the one this list was started for', RUN],
    ['FamilySearch asked you to sign in (HTTP 401). Sign in to FamilySearch', RUN],
    ['Ancestry asked us to slow down (HTTP 429)', RUN],
    ['FamilySearch asked you to sign in (HTTP 403). Sign in to FamilySearch in this tab, reload the page, then press Capture again', RUN],
    ['Newspapers.com asked us to slow down (HTTP 503)', RUN],
    ['Still reading your clippings on this Newspapers.com page - wait a moment, then press Capture again.', RUN],
    ['a file was dropped into the panel while the batch ran, so nothing was saved', RUN],
    [NP('could not reach Newspapers.com for your clippings list - check your internet connection'), RUN],
    // X1: the advice after a cause never decides it - "open the panel again".
    [NP('the capture add-on could not read this record (TypeError: x is undefined) - a fault in the add-on, not the site'), FAULT],
    [NP('Newspapers.com would not send your clippings list (HTTP 500)'), TROUBLE],
    [NP('the page data does not match this address'), UNFORESEEN],
    // X1 correction, fifth audit Y1: a 401/403 for HER OWN clippings list never
    // counts. One that points at signing in or subscribing - in its words or its
    // advice - counts for nothing yet: `signIn`, and the batch asks the next
    // record whose it is. A bare 403 for one record is the site refusing it.
    ['Newspapers.com refused your clippings list (HTTP 403) - sign in and reload the page', RUN],
    ['Ancestry refused (HTTP 403) - sign in on this page, or the collection may not allow downloads', SIGN_IN],
    ['Newspapers.com refused the page image (HTTP 403). Reload the page (its image pass lasts a day) and check you are signed in', SIGN_IN],
    [NP('Newspapers.com refused the list of clippings on this page (HTTP 403: you may need to sign in, or your subscription may not cover this paper)'), SIGN_IN],
    ['this paper is not in your subscription (HTTP 403)', SIGN_IN],
    [NP('Newspapers.com would not send the list of clippings on this page (HTTP 403)'), FAULT],
    ['Newspapers.com refused the page image (HTTP 403)', FAULT],
    // X1: a cause nobody foresaw counts, after MAX_TROUBLE_TRIES Starts.
    ['something nobody expected', UNFORESEEN],
    ['could not read the page (https://www.newspapers.com/article/x/1/)', UNFORESEEN],
    ['', UNFORESEEN],
    ['FamilySearch is having trouble (HTTP 500). Try again in a little while', TROUBLE],
    ['Ancestry answered with a busy or error page ("502 Bad Gateway")', TROUBLE],
    ['the page did not answer (The message port closed before a response was received.). Reload it and press Capture again', TROUBLE],
    ['the capture add-on could not read this record (TypeError: x is undefined) - a fault in the add-on, not the site', FAULT],
    ['Find a Grave refused the memorial (HTTP 403). Reload the page', FAULT],
  ];
  for (const [m, want] of table) assert.deepStrictEqual(CB.stopRule(m), want, m);
  // The cause is what is left once the advice after it is taken off.
  assert.strictEqual(CB.causeOf(NP('could not re-read this page')), 'Newspapers.com: automatic saving is not available here: could not re-read this page');
  assert.strictEqual(CB.causeOf('FamilySearch is having trouble (HTTP 502). Try again in a little while'), 'FamilySearch is having trouble (HTTP 502)');
  assert.strictEqual(CB.MAX_TRIES, 2);
  assert.strictEqual(CB.MAX_TROUBLE_TRIES, 3);
});

// ── the one writer (lib/batch-store.js) ──────────────────────────────────────

const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
// chrome.storage.local as the service worker sees it, kept as JSON. `fault`
// makes the browser refuse: fault(op, key) answers its reason, or null.
function memory(init) {
  const m = { data: clone(init || {}), fault: null, writes: 0 };
  const refuse = (op, key) => {
    const why = m.fault && m.fault(op, key);
    if (why) throw new Error(why);
  };
  m.deps = {
    captureBatch: CB,
    get: async (k) => { refuse('get', k); return clone(m.data[k]); },
    set: async (o) => { for (const k of Object.keys(o)) refuse('set', k); m.writes++; Object.assign(m.data, clone(o)); },
    remove: async (k) => { refuse('set', k); delete m.data[k]; },
  };
  return m;
}
let ids = 0;
const intent = (site, ops, id) => ({ type: 'fha-batch', id: id || 'w.' + ++ids, site, ops });
const anc = (dbId, recordId, persons) => ({ dbId, recordId, persons: persons || [] });

test('one writer: what two windows ask for at the same moment is all kept - two imports and a record saved by hand', async () => {
  const mem = memory();
  const store = STORE.create(mem.deps);
  const answers = await Promise.all([
    store.handle(intent('ancestry', [['addRecords', [anc('90001', '1000001', ['Calvin Hartley'])], 'file: a.ged', 'a.ged']])),
    store.handle(intent('ancestry', [['addRecords', [anc('90002', '2000001', ['Ethel Hartley'])], 'file: b.ged', 'b.ged']])),
    store.handle(intent('ancestry', [['manual', { ok: true, site: 'ancestry', bundle: 'harriet-bundle',
      record: { dbId: '90001', recordId: '1000002', name: 'Harriet Hartley' } }]])),
  ]);
  assert.ok(answers.every((a) => a.ok));
  const L = mem.data.ancBatchLedger;
  assert.deepStrictEqual(L.order, ['90001:1000001', '90002:2000001', '90001:1000002']);
  assert.strictEqual(L.items['90001:1000002'].state, 'done');
  assert.strictEqual(L.items['90001:1000002'].note, 'saved by hand');
  // Each answer is the ledger as stored once that change was made.
  assert.deepStrictEqual(answers[2].L.order, L.order);
  assert.ok(!('applied' in answers[2].L), 'the ids of applied changes stay in the service worker');
});

test('one writer: a change asked for again - its answer lost when the service worker stopped - is made once, even after a restart', async () => {
  const mem = memory();
  const store = STORE.create(mem.deps);
  await store.handle(intent('ancestry', [['addRecords', [anc('90001', '1000001')], 'test']]));
  const ask = intent('ancestry', [['markRefused', '90001:1000001']], 'panel-1.7');
  const first = await store.handle(ask);
  const again = await store.handle(clone(ask));
  assert.deepStrictEqual([first.results, again.results, !!again.again], [[1], [1], true]);
  // The browser stops the service worker and starts it again: the ids are
  // kept with the ledger, written with the change they record.
  const restarted = STORE.create(mem.deps);
  const third = await restarted.handle(clone(ask));
  assert.deepStrictEqual(third.results, [1]);
  assert.strictEqual(mem.data.ancBatchLedger.items['90001:1000001'].stops, 1);
  // A new change is made.
  assert.deepStrictEqual((await restarted.handle(intent('ancestry', [['markRefused', '90001:1000001']]))).results, [2]);
  // Only the last few dozen ids are kept.
  for (let i = 0; i < STORE.APPLIED_MAX + 5; i++) await restarted.handle(intent('ancestry', [['log', 'line ' + i]]));
  assert.strictEqual(mem.data.ancBatchLedger.applied.length, STORE.APPLIED_MAX);
});

test('one writer: a "slow down" break another window recorded is never shortened by a run\'s gap, nor erased when a run ends', async () => {
  const mem = memory();
  const store = STORE.create(mem.deps);
  const [plan] = (await store.handle(intent('familysearch', [['rateLimit']]))).results;
  assert.strictEqual(plan.kind, 'ratelimit');
  const pacing = clone(mem.data.fsBatchLedger.pacing);
  // A run's ordinary gap after a capture: the break stands, whole, and its
  // count of slow-downs in a row is not reset.
  const [gap] = (await store.handle(intent('familysearch', [['afterCapture']]))).results;
  assert.deepStrictEqual(gap, { kind: 'ratelimit', until: plan.until });
  assert.deepStrictEqual(mem.data.fsBatchLedger.pacing, pacing);
  // A run ending keeps it too.
  await store.handle(intent('familysearch', [['clearGap']]));
  assert.strictEqual(mem.data.fsBatchLedger.pacing.resumeAt, plan.until);
  // An ordinary gap is dropped when a run ends; a break already ahead is not
  // counted twice by a run stopping on it.
  const other = memory({ fsBatchLedger: { version: 1, items: {}, order: [], log: [],
    pacing: { sinceShort: 1, shortEvery: 40, rateEvents: 0, resumeAt: Date.now() + 60e3, resumeReason: 'between' } } });
  const s2 = STORE.create(other.deps);
  await s2.handle(intent('familysearch', [['clearGap']]));
  assert.strictEqual(other.data.fsBatchLedger.pacing.resumeAt, 0);
  await s2.handle(intent('familysearch', [['rateLimit']]));
  assert.deepStrictEqual((await s2.handle(intent('familysearch', [['rateLimit', { unlessAhead: true }]]))).results, [null]);
  assert.strictEqual(other.data.fsBatchLedger.pacing.rateEvents, 1);
});

test('one writer: the slow-down count - a clean capture starts it again, a slow-down after it counts, the third gets the long break', async () => {
  const mem = memory();
  const store = STORE.create(mem.deps);
  const P = () => mem.data.ancBatchLedger.pacing;
  await store.handle(intent('ancestry', [['start'], ['rateLimit']]));
  assert.strictEqual(P().rateEvents, 1);
  // The break passes; a clean capture starts the count again.
  mem.data.ancBatchLedger.pacing.resumeAt = Date.now() - 1;
  await store.handle(intent('ancestry', [['afterCapture']]));
  assert.strictEqual(P().rateEvents, 0);
  await store.handle(intent('ancestry', [['rateLimit']]));
  await store.handle(intent('ancestry', [['rateLimit']]));
  const [third] = (await store.handle(intent('ancestry', [['rateLimit']]))).results;
  assert.deepStrictEqual([third.kind, P().rateEvents, P().resumeAt], ['long', 0, third.until]);
});

test('one writer: "Read my clippings list again" makes a list page already on its way stale - the list is read again from its start', async () => {
  const mem = memory();
  const store = STORE.create(mem.deps);
  const clip = (id) => ({ clipping_id: id, page_id: 5550001, url: '/article/x/' + id + '/', user_username: 'HartleyResearch' });
  await store.handle(intent('newspapers', [['listPage', { epoch: 0, user: 'HartleyResearch', clippings: [clip(101)],
    nextCursor: 24, more: true }]]));
  assert.deepStrictEqual([mem.data.npBatchLedger.listCursor, mem.data.npBatchLedger.listComplete], [24, false]);
  // Another window: read the list again. The page asked for before it is dropped.
  await store.handle(intent('newspapers', [['relist']]));
  const [late] = (await store.handle(intent('newspapers', [['listPage', { epoch: 0, user: 'HartleyResearch',
    clippings: [clip(202)], nextCursor: null, more: false }]]))).results;
  assert.deepStrictEqual(late, { stale: true });
  const L = mem.data.npBatchLedger;
  assert.deepStrictEqual([L.listCursor, L.listComplete, L.user, L.listEpoch], [0, false, null, 1]);
  const [fresh] = (await store.handle(intent('newspapers', [['listPage', { epoch: 1, user: 'HartleyResearch',
    clippings: [clip(101), clip(202)], nextCursor: null, more: false }]]))).results;
  assert.deepStrictEqual([fresh.added, fresh.complete, mem.data.npBatchLedger.order], [1, true, ['101', '202']]);
});

test('an owner upgrading from 0.8.0 loses nothing: the ledger loads as stored, its old stamps decide nothing, and a change touches only its own entry', async () => {
  // As 0.8.0 left it: stamps on every entry, the pacing and the list.
  const T = Date.now() + 10 * 60 * 1000; // a clock that ran ahead
  const v080 = {
    version: 1, created: '2026-11-01T00:00:00.000Z',
    items: {
      101: { id: 101, url: 'https://www.newspapers.com/article/x/101/', pageId: 5550001, title: 'Obituary', state: 'done',
        bundle: 'obit-bundle', doneAt: '2026-11-02T00:00:00.000Z', tries: 0, stops: 0, stateAt: T },
      202: { id: 202, url: 'https://www.newspapers.com/article/x/202/', pageId: 5550001, title: 'Wedding', state: 'partial',
        bundle: 'wed-bundle', note: 'saved without all of its image', partialTries: 1, tries: 0, stateAt: T },
      303: { id: 303, url: 'https://www.newspapers.com/article/x/303/', pageId: 5550002, title: 'Reunion', state: 'failed',
        lastError: 'Newspapers.com would not give this clipping - your subscription may not cover this paper',
        ownRefusal: true, stops: 2, tries: 1, stateAt: T, setAsideBefore: null },
      404: { id: 404, url: 'https://www.newspapers.com/article/x/404/', pageId: 5550002, title: 'Notice', state: 'queued',
        stops: 1, tries: 0, stateAt: T },
    },
    order: ['101', '202', '303', '404'],
    pacing: { sinceShort: 7, shortEvery: 44, rateEvents: 1, rateAt: T, resumeAt: 0, resumeReason: 'between' },
    log: ['2026-11-02T00:00:00.000Z  done 101 -> obit-bundle'],
    listComplete: true, listCursor: 96, user: 'HartleyResearch', listAt: T,
  };
  const mem = memory({ npBatchLedger: v080, batchLease: { owner: 'old-window', token: 'old-window.x', site: 'ancestry',
    until: Date.now() - 1 } });
  const store = STORE.create(mem.deps);
  const got = await store.handle(intent('newspapers', [['get']]));
  assert.deepStrictEqual(got.L, v080);
  assert.strictEqual(mem.writes, 0, 'reading wrote');
  // "Try the set-aside ones again", though the old entry's stamp is later than now.
  const [n] = (await store.handle(intent('newspapers', [['requeueSetAside']]))).results;
  assert.strictEqual(n, 1);
  const L = mem.data.npBatchLedger;
  assert.strictEqual(L.items['303'].state, 'queued');
  for (const k of ['101', '202', '404']) assert.deepStrictEqual(L.items[k], v080.items[k], k + ' changed');
  assert.deepStrictEqual([L.pacing, L.listComplete, L.listCursor, L.user, L.order],
    [v080.pacing, true, 96, 'HartleyResearch', v080.order]);
  // The lease 0.8.0 left behind (run out) holds nothing up.
  assert.ok((await store.handle({ type: 'fha-batch-lease', action: 'claim', owner: 'me', site: 'newspapers' })).ok);
});

test('one writer: the run lease - one batch at a time, however many Starts are pressed together', async () => {
  const mem = memory();
  const store = STORE.create(mem.deps);
  const lease = (action, owner, site) => store.handle({ type: 'fha-batch-lease', action, owner, site });
  const claims = await Promise.all(['a', 'b', 'c'].map((w) => lease('claim', w, 'ancestry')));
  assert.deepStrictEqual(claims.map((c) => c.ok), [true, false, false]);
  assert.strictEqual(claims[1].lease.owner, 'a');
  assert.deepStrictEqual(await lease('renew', 'a', 'ancestry'), { ok: true });
  const lost = await lease('renew', 'b', 'ancestry');
  assert.deepStrictEqual([lost.ok, lost.lost], [false, true]);
  await lease('release', 'b');
  assert.strictEqual(mem.data.batchLease.owner, 'a', 'another window let this one\'s lease go');
  await lease('release', 'a');
  assert.strictEqual(mem.data.batchLease, undefined);
  // A lease run out (its service worker stopped, its panel gone) holds nothing up.
  mem.data.batchLease = { owner: 'gone', site: 'ancestry', until: Date.now() - 1 };
  assert.ok((await lease('claim', 'b', 'ancestry')).ok);
});

test('one writer: storage the browser refuses is said, never taken for an empty queue, and nothing half-done is kept', async () => {
  const mem = memory();
  const store = STORE.create(mem.deps);
  await store.handle(intent('ancestry', [['addRecords', [anc('90001', '1000001')], 'test']]));
  const before = clone(mem.data.ancBatchLedger);
  mem.fault = (op) => (op === 'get' ? 'the profile is locked' : null);
  const r = await store.handle(intent('ancestry', [['addRecords', [anc('90002', '2000001')], 'test']]));
  assert.deepStrictEqual([r.ok, r.storage, r.what, r.error], [false, true, 'read', 'the profile is locked']);
  mem.fault = (op) => (op === 'set' ? 'QUOTA_BYTES quota exceeded' : null);
  const w = await store.handle(intent('ancestry', [['markDone', '90001:1000001', 'b']]));
  assert.deepStrictEqual([w.ok, w.what, w.error], [false, 'save', 'QUOTA_BYTES quota exceeded']);
  assert.deepStrictEqual(mem.data.ancBatchLedger, before);
  // Nothing it does not know is ever applied, and nothing is written for it.
  mem.fault = null;
  assert.strictEqual((await store.handle(intent('ancestry', [['markDone', '90001:1000001', 'b'], ['deleteEverything']]))).ok, false);
  assert.strictEqual((await store.handle(intent('elsewhere', [['get']]))).ok, false);
  assert.deepStrictEqual(mem.data.ancBatchLedger, before);
});
