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
  assert.deepStrictEqual(CB.counts(L), { total: 3, done: 1, queued: 1, failed: 1, partial: 0 });
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
  assert.strictEqual(CB.classifyError(refused.error), 'stop');
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

// ── PR #224 Codex 4504415455: the whole ledger merges, no window's write lost ─

test('Codex 4504415455: mergeStored never shortens or erases a stored "slow down" break', () => {
  const now = Date.now();
  const mine = CB.newLedger();
  mine.pacing = { sinceShort: 4, shortEvery: 40, rateEvents: 0, resumeAt: now + 60e3, resumeReason: 'between' };
  const stored = CB.newLedger();
  stored.pacing = { sinceShort: 0, shortEvery: 33, rateEvents: 1, rateAt: now, resumeAt: now + 30 * 60e3, resumeReason: 'ratelimit' };
  CB.mergeStored(mine, stored);
  // The break wins, and the short-break rhythm it reset comes with it.
  assert.deepStrictEqual(mine.pacing, { sinceShort: 0, shortEvery: 33, rateEvents: 1, rateAt: now,
    resumeAt: now + 30 * 60e3, resumeReason: 'ratelimit' });
  // A run that finishes (resumeAt 0) keeps it too.
  mine.pacing.resumeAt = 0;
  mine.pacing.resumeReason = '';
  CB.mergeStored(mine, stored);
  assert.strictEqual(mine.pacing.resumeAt, now + 30 * 60e3);
  // A longer break of its own is never cut back to the stored one.
  mine.pacing.resumeAt = now + 4 * 3600e3;
  mine.pacing.resumeReason = 'long';
  CB.mergeStored(mine, stored);
  assert.deepStrictEqual([mine.pacing.resumeAt, mine.pacing.resumeReason], [now + 4 * 3600e3, 'long']);
  // An ordinary pause stored by another window is not a break to take.
  const quiet = CB.newLedger();
  quiet.pacing = { resumeAt: 1, resumeReason: '' };
  const other = CB.newLedger();
  other.pacing = { resumeAt: now + 60e3, resumeReason: 'between' };
  CB.mergeStored(quiet, other);
  assert.strictEqual(quiet.pacing.resumeAt, 1);
});

test('Codex 4504415455: an entry changed in two windows keeps the later change, whole; done stays done; people join', () => {
  const L = CB.newLedger();
  CB.addRecords('ancestry', L, [{ dbId: '1', recordId: '2', persons: ['Calvin Hartley'] }, { dbId: '3', recordId: '4' },
    { dbId: '5', recordId: '6' }]);
  const stored = JSON.parse(JSON.stringify(L));
  // This window: 1:2 refused once, 3:4 set aside, 5:6 done.
  CB.markRefused(L, '1:2');
  CB.markSetAside(L, '3:4', 'not the one queued');
  CB.markDone(L, '5:6', 'b56');
  // The other window, later: queued 1:2 again with another person, queued
  // the set-aside ones again, and saved 5:6 in part.
  stored.items['1:2'].persons.push('Harriet Hartley');
  stored.items['3:4'] = Object.assign({}, L.items['3:4']);
  CB.requeueSetAside(stored);
  CB.markPartial(stored, '5:6', 'b56b', 'image not saved');
  CB.mergeStored(L, stored);
  assert.deepStrictEqual(L.items['1:2'].persons, ['Calvin Hartley', 'Harriet Hartley']);
  assert.strictEqual(L.items['1:2'].stops, 1, 'this window\'s refusal, the later change, is kept');
  assert.deepStrictEqual([L.items['3:4'].state, L.items['3:4'].tries, L.items['3:4'].setAsideBefore],
    ['queued', 0, 'not the one queued']);
  assert.strictEqual(L.items['5:6'].state, 'done', 'done stays done');
});

test('Codex 4504415455: the clippings list progress follows the later change; both windows\' log lines are kept', () => {
  const L = CB.newLedger();
  Object.assign(L, { listComplete: true, listCursor: 48, user: 'HartleyResearch', listAt: 1000 });
  L.log = ['2026-11-07T10:00:00.000Z  list read'];
  const stored = CB.newLedger();
  Object.assign(stored, { listComplete: false, listCursor: 0, user: null, listAt: 2000 });
  stored.log = ['2026-11-07T10:05:00.000Z  queued again'];
  CB.mergeStored(L, stored);
  assert.deepStrictEqual([L.listComplete, L.listCursor, L.user], [false, 0, null]);
  assert.deepStrictEqual(L.log, ['2026-11-07T10:00:00.000Z  list read', '2026-11-07T10:05:00.000Z  queued again']);
  const older = CB.newLedger();
  Object.assign(older, { listComplete: true, listCursor: 96, user: 'HartleyResearch', listAt: 1500 });
  CB.mergeStored(L, older);
  assert.deepStrictEqual([L.listComplete, L.listCursor], [false, 0]);
});

// ── PR #224 Codex 4504468051: a deliberate decrease is never undone ─────────

test('Codex 4504468051: the slow-down count merges by its last change, so a reset to 0 is kept and a later slow-down still counts', () => {
  const now = Date.now();
  const L = CB.newLedger();
  CB.ensurePacing(L);
  CB.planRateLimit(L, now - 5000);
  const stored = JSON.parse(JSON.stringify(L));
  // This window: a clean capture, later - the count starts again.
  CB.planAfterCapture(L, now);
  assert.strictEqual(L.pacing.rateEvents, 0);
  CB.mergeStored(L, stored);
  assert.strictEqual(L.pacing.rateEvents, 0, 'the reset was undone');
  // Another window, later still: a slow-down. It counts.
  const other = JSON.parse(JSON.stringify(L));
  CB.planRateLimit(other, now + 5000);
  CB.mergeStored(L, other);
  assert.strictEqual(L.pacing.rateEvents, 1);
  // The third slow-down's reset keeps its long break.
  const T = CB.newLedger();
  CB.ensurePacing(T);
  CB.planRateLimit(T, now);
  CB.planRateLimit(T, now);
  const before = JSON.parse(JSON.stringify(T));
  const plan = CB.planRateLimit(T, now);
  assert.deepStrictEqual([plan.kind, T.pacing.rateEvents], ['long', 0]);
  CB.mergeStored(T, before);
  assert.deepStrictEqual([T.pacing.rateEvents, T.pacing.resumeAt], [0, plan.until]);
});

test('Codex 4504468051: "Read my clippings list again" pressed while a page was on its way wins over that page', () => {
  const asked = Date.now();
  const L = CB.newLedger();
  Object.assign(L, { listComplete: false, listCursor: 24, user: 'HartleyResearch', listAt: asked });
  const relist = CB.newLedger();
  Object.assign(relist, { listComplete: false, listCursor: 0, user: null, listAt: asked + 10 });
  CB.mergeStored(L, relist);
  assert.deepStrictEqual([L.listCursor, L.user], [0, null]);
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

test('second audit N2: a stamp two windows share is broken the same way everywhere - a save, a requeue, a slow-down and a restart win', () => {
  const T = 5e12;
  const mine = CB.newLedger();
  CB.addRecords('ancestry', mine, [{ dbId: '1', recordId: '2' }]);
  Object.assign(mine.items['1:2'], { state: 'failed', stateAt: T });
  mine.pacing = { rateEvents: 0, rateAt: T, resumeAt: 0, resumeReason: '' };
  Object.assign(mine, { listComplete: false, listCursor: 24, listAt: T });
  const theirs = JSON.parse(JSON.stringify(mine));
  Object.assign(theirs.items['1:2'], { state: 'queued', tries: 0 });
  theirs.pacing.rateEvents = 1;
  Object.assign(theirs, { listCursor: 0 });
  CB.mergeStored(mine, theirs);
  assert.strictEqual(mine.items['1:2'].state, 'queued');
  assert.strictEqual(mine.pacing.rateEvents, 1);
  assert.strictEqual(mine.listCursor, 0);
  // And the other way round: the stored side keeps the same winners.
  const back = JSON.parse(JSON.stringify(theirs));
  const loser = JSON.parse(JSON.stringify(theirs));
  Object.assign(loser.items['1:2'], { state: 'failed' });
  loser.pacing.rateEvents = 0;
  Object.assign(loser, { listCursor: 24 });
  CB.mergeStored(back, loser);
  assert.deepStrictEqual([back.items['1:2'].state, back.pacing.rateEvents, back.listCursor], ['queued', 1, 0]);
});

test('third audit M1: every stop cause is sorted - RUN causes never count against a record, RECORD causes do', () => {
  const RUN = { counts: false };
  const TROUBLE = { counts: true, max: CB.MAX_TROUBLE_TRIES, trouble: true };
  const FAULT = { counts: true, max: CB.MAX_TRIES, trouble: false };
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
    ['something nobody expected', RUN],
    ['', RUN],
    ['FamilySearch is having trouble (HTTP 500). Try again in a little while', TROUBLE],
    ['Ancestry answered with a busy or error page ("502 Bad Gateway")', TROUBLE],
    ['the page did not answer (The message port closed before a response was received.). Reload it and press Capture again', TROUBLE],
    ['the capture add-on could not read this record (TypeError: x is undefined) - a fault in the add-on, not the site', FAULT],
    ['Find a Grave refused the memorial (HTTP 403). Reload the page', FAULT],
  ];
  for (const [m, want] of table) assert.deepStrictEqual(CB.stopRule(m), want, m);
  assert.strictEqual(CB.MAX_TRIES, 2);
  assert.strictEqual(CB.MAX_TROUBLE_TRIES, 3);
});
