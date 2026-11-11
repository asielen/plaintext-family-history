// The owner's batch downloads, driven end to end in the capture panel
// without a browser (tests/panel-harness.js). The sites are played by the
// real readers in src/lib over the scrubbed fixture pages (the fictional
// Hartleys of Fairview, invented ids), as the single-capture tests play them.
//
// What it guards, in the owner's terms:
//   - switched off (every ordinary install), the panel shows no batch box,
//     reads no batch queue, asks no page a batch question and moves no tab;
//     a record saved by hand is not noted anywhere for a batch;
//   - switched on, each record is saved as Capture saves it - the same
//     files, the same capture.json - into the download folder, even when the
//     archive connection is on;
//   - the first "slow down" stops the run, and the next Start waits out a
//     long break before its first request;
//   - a record the archive already holds (Seen It, asked about that record
//     only) is never downloaded;
//   - a page showing another record than the one queued saves nothing, and
//     that record is set aside with the reason.
//
//   node --test browser-companion/tests/test-panel-batch.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { openPanel, settle, PORT_CLOSED } = require('./panel-harness.js');
const anc = require('../src/lib/ancestry-record.js');
const fag = require('../src/lib/findagrave-record.js');
const BS = require('../src/lib/batch-sources.js');
const { parse } = require('./mini-dom.js');

const SAMPLES = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'capture-samples');
const sample = (name) => fs.readFileSync(path.join(SAMPLES, name + '.html'), 'utf8');

const CALVIN = 'https://www.ancestry.com/search/collections/90001/records/1000001';
const HARRIET = 'https://www.ancestry.com/search/collections/90001/records/1000002';
const ETHEL = 'https://www.ancestry.com/search/collections/90002/records/2000001';
const PAGES = {
  [CALVIN]: 'ancestry-record-census',
  [HARRIET]: 'ancestry-record-census',
  [ETHEL]: 'ancestry-record-death-index',
};
const ON = { batchDownloads: true };
const BATCH_ACTIONS = ['newspapersList', 'ancestryPersonSources', 'ancestryPersonGallery', 'familysearchPersonSources',
  'findagraveLinks'];

// Ancestry, as content.js composes the lib for each record page. `image` is
// how the scan fetch answers; `shows` makes every page show that record
// (a site that redirects the batch to another record).
function ancestrySite({ image, shows } = {}) {
  return (msg, tab) => {
    const url = shows || tab.url;
    const doc = parse(sample(PAGES[url] || 'ancestry-record-census'));
    switch (msg.action) {
      case 'prefill':
        return { ok: true, prefill: { url: tab.url, title: doc.title, people: [], recipeHint: 'ancestry',
          ancestryRecord: !!anc.ancParseRecordPage(tab.url) } };
      case 'ancestryRecordInfo': {
        const info = anc.ancRecordFrom(doc, url, { title: doc.title, origin: 'https://www.ancestry.com' });
        if (!info.ok) return info;
        info.hints = anc.ancHints(info.record, info.fields);
        info.title = anc.ancTitle(info.record, msg.formTitle);
        return info;
      }
      case 'ancestryImage':
        return image || { ok: true, base64: 'AAAA', ext: 'jpg', contentType: 'image/jpeg' };
      case 'pagehtml': return { ok: true, html: '<html>' + doc.title + '</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy of ' + tab.url + '</html>' };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

// A queue already in storage, as the batch (or the archive copy's 0.6.x
// batch) leaves it.
function ancLedger(urls) {
  const L = { version: 1, created: '2026-11-01T00:00:00.000Z', items: {}, order: [], pacing: null, log: [] };
  for (const u of urls) {
    const id = anc.ancParseRecordPage(u);
    const key = id.dbId + ':' + id.recordId;
    L.items[key] = { key, url: u, dbId: id.dbId, recordId: id.recordId, persons: [], title: '', state: 'queued', tries: 0 };
    L.order.push(key);
  }
  return L;
}

// No waiting in tests: the gaps and settles are zero, the breaks stay real -
// in the panel (settles, list pauses) and in the service worker (the gaps and
// breaks it plans). Answers both: set a range on each to change it.
function quick(p) {
  const both = [p.window.FHA.captureBatch.PACING, p.worker.PACING];
  for (const P of both) {
    P.betweenMs = [0, 0];
    P.settleMs = [0, 0];
    P.listPauseMs = [0, 0];
    P.shortBreakMs = [0, 0];
    P.askRetryMs = 1;
  }
  // Reads as the service worker's PACING; set() changes a range in both.
  return Object.assign(Object.create(p.worker.PACING), { set(k, v) { for (const P of both) P[k] = v; } });
}

async function start(p, site) {
  await p.batchEl(site + '-start').fire('click');
  await settle();
}

// The bundle name's time and random token differ by construction.
const stripName = (n) => n.replace(/-\d{8}-\d{6}(-\d{3})?-[0-9a-z]{6}$/, '');
async function snapshot(spec) {
  const files = [];
  for (const a of spec.assets) files.push([a.filename, await a.blob.text()]);
  return { name: stripName(spec.bundleName), pageHtml: spec.pageHtml, captureJson: spec.captureJson, files };
}

// ── switched off ─────────────────────────────────────────────────────────────

test('switched off: no batch box, no queue read, no batch question, no tab moved - and on, the box is there', async () => {
  for (const storage of [{}, { batchDownloads: false }, { batchDownloads: 'true' }, { batchDownloads: 1 }]) {
    // A queue left in storage by an earlier install changes nothing.
    const p = await openPanel({ url: CALVIN, respond: ancestrySite(),
      storage: Object.assign({ ancBatchLedger: ancLedger([CALVIN, ETHEL]) }, storage) });
    assert.strictEqual(p.batchEl('box'), undefined, 'batch box shown with ' + JSON.stringify(storage));
    // Nothing but the drop zone's file picker is added to the page.
    assert.deepStrictEqual(p.doc.body.children.map((c) => c.tagName), ['input']);
    assert.ok(p.storageReads.includes('batchDownloads'), 'the switch is read');
    for (const k of ['npBatchLedger', 'ancBatchLedger', 'fsBatchLedger', 'fagBatchLedger']) {
      assert.ok(!p.storageReads.includes(k), k + ' read with the switch off');
    }
    // An ordinary capture: saved as always, and noted in no batch queue.
    await p.capture();
    assert.strictEqual(p.written.length, 1, p.result());
    assert.deepStrictEqual(p.storage.ancBatchLedger, ancLedger([CALVIN, ETHEL]));
    assert.deepStrictEqual(p.sent.filter((m) => BATCH_ACTIONS.includes(m.action)), []);
    assert.deepStrictEqual(p.navigations, []);
    assert.deepStrictEqual(p.downloads, []);
  }
  // The same panel with the switch on shows the box (so the test above
  // could see one if it were there).
  const on = await openPanel({ url: CALVIN, respond: ancestrySite(), storage: ON });
  assert.ok(on.batchEl('box'));
  assert.ok(on.batchEl('anc-start') && on.batchEl('np-start') && on.batchEl('fs-start') && on.batchEl('fag-start'));
});

test('switched off, the panel never calls the batch code at all; switched on, it does', async () => {
  for (const on of [false, true]) {
    let installed = 0;
    const p = await openPanel({ url: CALVIN, respond: ancestrySite(), storage: on ? ON : {},
      beforePanel: (w) => {
        const real = w.FHA.batchPanel.install;
        w.FHA.batchPanel.install = (hooks) => {
          installed++;
          return real(hooks);
        };
      } });
    await p.capture();
    assert.strictEqual(p.written.length, 1, p.result());
    assert.strictEqual(installed, on ? 1 : 0);
  }
});

// ── switched on: each record as Capture saves it ─────────────────────────────

test('switched on: each queued record is staged exactly as a single Capture of it stages it', async () => {
  // Single captures, one fresh panel each, the ordinary way.
  const single = [];
  for (const url of [CALVIN, ETHEL]) {
    const s = await openPanel({ url, respond: ancestrySite() });
    await s.capture();
    assert.strictEqual(s.written.length, 1, s.result());
    single.push(await snapshot(s.written[0]));
  }
  // The batch, from a panel opened on another Ancestry record.
  const p = await openPanel({ url: HARRIET, respond: ancestrySite(), storage: Object.assign({}, ON,
    { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }) });
  quick(p);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN, ETHEL]);
  assert.strictEqual(p.written.length, 2, p.batchEl('anc-status').textContent);
  for (let i = 0; i < 2; i++) assert.deepStrictEqual(await snapshot(p.written[i]), single[i]);
  // Done, each with the bundle that holds it; the reading copy written.
  const L = p.storage.ancBatchLedger;
  assert.deepStrictEqual(L.order.map((k) => L.items[k].state), ['done', 'done']);
  assert.strictEqual(L.items['90001:1000001'].bundle, p.written[0].bundleName);
  assert.ok(p.downloads.some((d) => d.filename === 'fha-inbox/_ancestry-batch-ledger.json' &&
    d.conflictAction === 'overwrite'));
  assert.match(p.batchEl('anc-status').textContent, /^Finished\. 2 done, 0 to go\./);
  // Capture is the owner's again.
  assert.strictEqual(p.$('btn-capture').disabled, false);
});

test('switched on: a batch stages to the download folder even with the archive connection on', async () => {
  const p = await openPanel({ url: CALVIN, respond: ancestrySite(), host: true,
    storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([ETHEL]) }) });
  quick(p);
  await start(p, 'anc');
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.filed.length, 0);
  // ...while Capture by hand still files straight in.
  p.$('btn-capture').disabled = false;
  await p.capture();
  assert.strictEqual(p.filed.length, 1, p.result());
});

test('switched on: two people on one census page - the page is downloaded once', async () => {
  const p = await openPanel({ url: ETHEL, respond: ancestrySite(), storage: Object.assign({}, ON,
    { ancBatchLedger: ancLedger([CALVIN, HARRIET]) }) });
  quick(p);
  await start(p, 'anc');
  assert.strictEqual(p.written.length, 2);
  assert.deepStrictEqual(p.sent.filter((m) => m.action === 'ancestryImage').length, 1);
  assert.match(p.storage.ancBatchLedger.items['90001:1000002'].note, /^page already saved in the capture of/);
});

// ── slow down ────────────────────────────────────────────────────────────────

test('the first "slow down" stops the run; the next Start waits out the break before any request', async () => {
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON,
    { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
  respond: ancestrySite({ image: { ok: false, error: 'the Ancestry image service returned HTTP 429' } }) });
  const P = quick(p);
  const t0 = Date.now();
  await start(p, 'anc');
  // Calvin's index was saved (as Capture saves it when the scan will not
  // come) and the run stopped there: Ethel was never asked for.
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
  assert.strictEqual(p.written.length, 1);
  const L = p.storage.ancBatchLedger;
  // Saved without its scan: in part, so a later Start comes back for it.
  assert.strictEqual(L.items['90001:1000001'].state, 'partial');
  assert.strictEqual(L.items['90001:1000001'].imageKey, 'img:90001:4000123_00045');
  assert.match(L.items['90001:1000001'].note, /HTTP 429/);
  assert.strictEqual(L.items['90002:2000001'].state, 'queued');
  assert.ok(L.pacing.resumeAt >= t0 + P.rateBreakMs[0], 'a long break before the next request');
  assert.match(p.batchEl('anc-status').textContent, /^Stopped: Ancestry asked us to slow down/);
  assert.strictEqual(p.$('btn-capture').disabled, false);

  // Start again at once: it waits, asking nothing of anyone, until Pause.
  P.rateBreakMs = [60 * 60 * 1000, 60 * 60 * 1000];
  const run = p.batchEl('anc-start').fire('click');
  await settle();
  assert.match(p.batchEl('anc-status').textContent, /asked us to slow down, so the batch waits/);
  const asked = p.sent.length;
  await p.batchEl('anc-pause').fire('click');
  await run;
  await settle();
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
  assert.strictEqual(p.sent.length, asked);
  assert.match(p.batchEl('anc-status').textContent, /^Paused\./);
});

// Find a Grave, as content.js composes the lib, and its image host.
function fagSite() {
  return async (msg, tab) => {
    const doc = parse(sample('findagrave-memorial'));
    switch (msg.action) {
      case 'prefill': {
        const info = fag.fagMemorialFrom(doc, tab.url, {});
        return { ok: true, prefill: { url: tab.url, title: fag.fagTitle(info.record), recipeHint: 'findagrave',
          people: [info.record.name], findagraveMemorial: !!fag.fagParseMemorial(tab.url) } };
      }
      case 'findagraveMemorialInfo': {
        const info = fag.fagMemorialFrom(doc, tab.url, { today: msg.today });
        if (!info.ok) return info;
        info.hints = fag.fagHints(info.record);
        info.title = fag.fagTitle(info.record);
        return info;
      }
      case 'findagraveGallery': {
        const got = await fag.fagGallery(msg.memorialId, msg.slug, {
          fetch: async () => ({ ok: true, status: 200, text: async () => sample('findagrave-memorial-photos') }),
          parse, base: 'https://www.findagrave.com',
        });
        const counts = {};
        for (const ph of got.photos || []) ph.role = fag.fagPhotoRole(ph.type, counts);
        return got;
      }
      case 'pagehtml': return { ok: true, html: '<html>' + tab.url + '</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy of ' + tab.url + '</html>' };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}
const FAG_CALVIN = 'https://www.findagrave.com/memorial/900000001/calvin-george-hartley';

test('Find a Grave: a 429 from the photo host stops the run after that memorial, no photo asked for after it', async () => {
  const L = { version: 1, items: {}, order: [], log: [] };
  for (const [id, slug] of [['900000001', 'calvin-george-hartley'], ['900000002', 'harriet-frances-hartley']]) {
    L.items[id] = { key: id, memorialId: id, slug, url: 'https://www.findagrave.com/memorial/' + id + '/' + slug,
      persons: [], title: '', state: 'queued', tries: 0 };
    L.order.push(id);
  }
  const p = await openPanel({ url: FAG_CALVIN, respond: fagSite(), storage: Object.assign({}, ON, { fagBatchLedger: L }),
    fetch: async (url) => ({ ok: false, status: 429, url, headers: { get: () => 'text/html' } }) });
  quick(p);
  await start(p, 'fag');
  // One photo asked for, refused with 429; no other photo, no other memorial.
  assert.strictEqual(p.fetched.length, 1);
  assert.match(p.fetched[0].url, /^https:\/\/images\.findagrave\.com\/photos\//);
  assert.strictEqual(p.fetched[0].init.credentials, 'omit');
  assert.strictEqual(p.fetched[0].init.redirect, 'error');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [FAG_CALVIN]);
  assert.strictEqual(p.written.length, 1);
  const out = p.storage.fagBatchLedger;
  assert.strictEqual(out.items['900000001'].state, 'partial');
  assert.strictEqual(out.items['900000002'].state, 'queued');
  assert.ok(out.pacing.resumeAt > Date.now() + 25 * 60 * 1000);
  assert.match(p.batchEl('fag-status').textContent, /^Stopped: Find a Grave asked us to slow down/);
});

// ── FamilySearch: the login stays with FamilySearch ─────────────────────────

const fsr = require('../src/lib/familysearch-record.js');
const FIX = JSON.parse(fs.readFileSync(path.join(SAMPLES, 'familysearch-gedcomx.json'), 'utf8'));
const TOKEN = 'fs-session-SECRET-0000';
const ark = (id) => 'https://www.familysearch.org/ark:/61903/1:1:' + id;

// FamilySearch, as content.js composes the lib, with every request recorded.
// `over(url)` may answer a request instead (a status number, or a response).
function familysearchSite(calls, over) {
  const fetch = async (url, init) => {
    calls.push({ url, auth: !!(init && init.headers && init.headers.Authorization) });
    const o = over && over(url);
    if (typeof o === 'number') return { ok: false, status: o, text: async () => '' };
    if (o) return o;
    const j = (x) => ({ ok: true, status: 200, text: async () => JSON.stringify(x) });
    let m = /\/ark:\/61903\/1:1:([A-Z0-9-]+)$/.exec(url);
    if (m && FIX.records[m[1]]) return j(FIX.records[m[1]]);
    m = /\/ark:\/61903\/3:1:([A-Z0-9-]+)$/.exec(url);
    if (m && FIX.images[m[1]]) return j(FIX.images[m[1]]);
    if (/dist\.jpg$/.test(url)) {
      return { ok: true, status: 200, blob: async () => new Blob([Buffer.alloc(40 * 1024)], { type: 'image/jpeg' }) };
    }
    return { ok: false, status: 404, text: async () => '' };
  };
  return async (msg, tab) => {
    const deps = { fetch, base: tab.url, token: TOKEN, toBase64: async (b) => Buffer.from(await b.arrayBuffer()).toString('base64') };
    const rec = fsr.fsParseRecordPage(tab.url);
    switch (msg.action) {
      case 'prefill':
        return { ok: true, prefill: { url: tab.url, title: 'A FamilySearch page', people: [], recipeHint: 'familysearch',
          familysearchRecord: !!rec } };
      case 'familysearchImageCheck': {
        const info = await fsr.fsRecordInfo(tab.url, deps);
        if (!info.ok) return { ok: false, error: info.error };
        return Object.assign({}, await fsr.fsImageCheck(info.record.imageId, deps), { record: true, fsId: info.record.fsId });
      }
      case 'familysearchRecordInfo': {
        const info = await fsr.fsRecordInfo(tab.url, deps);
        if (info.ok) info.bundleTitle = fsr.fsTitle(info.record, msg.formTitle, '');
        return info;
      }
      case 'familysearchImage':
        return fsr.fsImage(msg.imageId, deps).catch((e) => ({ ok: false, error: String((e && e.message) || e) }));
      case 'pagehtml': return { ok: true, html: '<html>FamilySearch page ' + tab.url + '</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy of ' + tab.url + '</html>' };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

// A record whose image description points off FamilySearch (a moved CDN, a
// tampered answer): the census record, re-pointed at the fixture's HRT1-EVIL.
FIX.records['HRT1-EVR1'] = JSON.parse(JSON.stringify(FIX.records['HRT1-CEN1'])
  .replace(/1:1:HRT1-CEN1/g, '1:1:HRT1-EVR1').replace(/3:1:HRT1-IMG1/g, '3:1:HRT1-EVIL'));
const fsLedger = (ids) => {
  const L = { version: 1, items: {}, order: [], log: [] };
  for (const id of ids) {
    L.items[id] = { key: id, fsId: id, url: ark(id), persons: [], title: '', state: 'queued', tries: 0 };
    L.order.push(id);
  }
  return L;
};

test('FamilySearch: each record saved with its data; the login went to FamilySearch only, never to the address its answer named elsewhere', async () => {
  const calls = [];
  const p = await openPanel({ url: ark('HRT1-CEN2'), respond: familysearchSite(calls),
    storage: Object.assign({}, ON, { fsBatchLedger: fsLedger(['HRT1-EVR1', 'HRT1-DTH1']) }) });
  quick(p);
  await start(p, 'fs');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [ark('HRT1-EVR1'), ark('HRT1-DTH1')]);
  assert.strictEqual(p.written.length, 2, p.batchEl('fs-status').textContent);
  assert.ok(p.files(p.written[0]).includes('record-data.json'));
  // Both kinds of request happened: with the login, to FamilySearch...
  assert.ok(calls.some((c) => c.auth), 'no request carried the login');
  for (const c of calls) {
    if (c.auth) assert.match(c.url, /^https:\/\/([a-z0-9-]+\.)*familysearch\.org\//, c.url);
  }
  // ...and to the address elsewhere, without it.
  const away = calls.filter((c) => /familysearch\.org\.evil\.com/.test(c.url));
  assert.ok(away.length > 0, 'the off-site image address was never asked');
  assert.ok(away.every((c) => !c.auth), 'the login went off FamilySearch');
});

test('FamilySearch: a record restricted to a FamilySearch center is set aside, and the run goes on (F6/F13)', async () => {
  const calls = [];
  const p = await openPanel({ url: ark('HRT1-CEN2'), storage: Object.assign({}, ON,
    { fsBatchLedger: fsLedger(['HRT1-CEN1', 'HRT1-DTH1']) }),
  respond: familysearchSite(calls, (u) => (/\/ark:\/61903\/1:1:HRT1-CEN1$/.test(u) ? 403 : null)) });
  quick(p);
  await start(p, 'fs');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [ark('HRT1-CEN1'), ark('HRT1-DTH1')]);
  const L = p.storage.fsBatchLedger;
  assert.strictEqual(L.items['HRT1-CEN1'].state, 'failed');
  assert.match(L.items['HRT1-CEN1'].lastError, /restricted to a FamilySearch center/);
  assert.strictEqual(L.items['HRT1-DTH1'].state, 'done');
  assert.strictEqual(p.written.length, 1);
});

// ── never twice ──────────────────────────────────────────────────────────────

test('a record Seen It says the archive holds is never downloaded; Seen It is asked about that record only', async () => {
  const p = await openPanel({ url: HARRIET, respond: ancestrySite(),
    seenIt: { 'rec:90001:1000001': { state: 'filed', sid: 'S-0a1b2c3d4e' } },
    storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }) });
  quick(p);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [ETHEL]);
  assert.strictEqual(p.written.length, 1);
  const L = p.storage.ancBatchLedger;
  assert.strictEqual(L.items['90001:1000001'].state, 'done');
  assert.strictEqual(L.items['90001:1000001'].bundle, '(already in the archive, source S-0a1b2c3d4e)');
  // Only narrow questions, by key - never "everything you know".
  assert.ok(p.external.length >= 2);
  for (const e of p.external) {
    assert.ok(Array.isArray(e.msg.keys) && !('prefix' in e.msg), JSON.stringify(e.msg));
  }
  assert.deepStrictEqual(p.external[0].msg.keys, ['rec:90001:1000001']);
});

test('a record saved by hand joins the queue as done, so a batch never downloads it again', async () => {
  const p = await openPanel({ url: CALVIN, respond: ancestrySite(), storage: Object.assign({}, ON,
    { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }) });
  quick(p);
  await p.capture();
  await settle();
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'done');
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].note, 'saved by hand');
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [ETHEL]);
  assert.strictEqual(p.written.length, 2);
});

// ── the wrong record ─────────────────────────────────────────────────────────

test('a page showing another record than the one queued saves nothing; that one is set aside and the run goes on', async () => {
  const p = await openPanel({ url: ETHEL, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN]) }),
    respond: ancestrySite({ shows: HARRIET }) });
  quick(p);
  await start(p, 'anc');
  assert.strictEqual(p.written.length, 0);
  assert.strictEqual(p.sent.filter((m) => m.action === 'ancestryImage').length, 0, 'nothing downloaded');
  const it = p.storage.ancBatchLedger.items['90001:1000001'];
  assert.strictEqual(it.state, 'failed');
  assert.match(it.lastError, /Ancestry record 90001:1000002, not the one queued/);
  assert.match(p.batchEl('anc-status').textContent, /^Finished\. 0 done, 0 to go, 1 set aside\./);
});

test('only an address built from a checked id is visited, whatever an older queue stored', async () => {
  const L = ancLedger([CALVIN]);
  // A queue from an older copy: a stored address elsewhere, and an id that is not one.
  L.items['90001:1000001'].url = 'https://evil.example/steal?from=ancestry';
  L.items['9/../x:1'] = { key: '9/../x:1', dbId: '9/../x', recordId: '1', url: 'https://evil.example/2',
    persons: [], state: 'queued', tries: 0 };
  L.order.push('9/../x:1');
  const p = await openPanel({ url: ETHEL, respond: ancestrySite(), storage: Object.assign({}, ON, { ancBatchLedger: L }) });
  quick(p);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.storage.ancBatchLedger.items['9/../x:1'].state, 'failed');
});

test('one batch at a time: a batch running in another panel window keeps this Start from asking anything', async () => {
  const lease = { owner: 'another-window', site: 'findagrave', until: Date.now() + 5 * 60 * 1000 };
  const p = await openPanel({ url: ETHEL, respond: ancestrySite(), storage: Object.assign({}, ON,
    { ancBatchLedger: ancLedger([CALVIN]), batchLease: lease }) });
  quick(p);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations, []);
  assert.strictEqual(p.written.length, 0);
  assert.match(p.batchEl('anc-status').textContent, /^A batch is running in another window/);
  assert.deepStrictEqual(p.storage.batchLease, lease);
  // A lease run out (its panel was closed mid-run) holds nothing up; this
  // run's own lease is let go when it ends.
  p.storage.batchLease = Object.assign({}, lease, { until: Date.now() - 1 });
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
  assert.strictEqual(p.storage.batchLease, undefined);
});

test('a GEDCOM from the tree fills the queue, with the people each record is cited for', async () => {
  const p = await openPanel({ url: CALVIN, respond: ancestrySite(), storage: ON });
  const ged = [
    '0 HEAD', '0 @I1@ INDI', '1 NAME Calvin /Hartley/', '1 BIRT', '2 SOUR @S1@', '3 _APID 1,90001::1000001',
    '0 @I2@ INDI', '1 NAME Ethel /Hartley/', '1 DEAT', '2 SOUR @S2@', '3 _APID 1,90002::2000001',
    '1 RESI', '2 SOUR @S1@', '3 _APID 1,90001::1000001', '0 TRLR',
  ].join('\r\n');
  const input = p.batchEl('anc-file');
  input.files = [{ name: 'hartley-tree.ged', text: async () => ged }];
  await input.fire('change');
  await settle();
  const L = p.storage.ancBatchLedger;
  assert.deepStrictEqual(L.order, ['90001:1000001', '90002:2000001']);
  assert.deepStrictEqual(L.items['90001:1000001'].persons, ['Calvin Hartley', 'Ethel Hartley']);
  assert.strictEqual(L.items['90001:1000001'].url, CALVIN);
  assert.match(p.batchEl('anc-status').textContent, /hartley-tree\.ged: 2 found, 2 new to the queue/);
});

test('Capture by hand waits while a batch moves the tab', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const base = ancestrySite();
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN]) }),
    respond: async (msg, tab) => {
      if (msg.action === 'ancestryRecordInfo') await gate;
      return base(msg, tab);
    } });
  quick(p);
  const run = p.batchEl('anc-start').fire('click');
  await settle();
  p.$('btn-capture').disabled = false; // even if she could press it
  await p.capture();
  assert.match(p.result(), /^A batch download is moving this tab/);
  release();
  await run;
  await settle();
  assert.strictEqual(p.written.length, 1);
});

// ── Newspapers.com: every clipping she made ─────────────────────────────────

const ME = { id: 7, username: 'HartleyResearch' };
const NP_A = 'https://www.newspapers.com/article/the-fairview-gazette-obituary/101/';
const NP_B = 'https://www.newspapers.com/article/the-fairview-gazette-wedding/202/';
const NP_LIST = 'https://www.newspapers.com/clippings/my-clippings/';

function npClip(id, title, url) {
  return {
    id, pageId: 5550001, title, tags: '', userId: ME.id, username: ME.username,
    rects: [{ x: 10, y: 20, w: 300, h: 400, rotation: 0 }], pageDate: '1931-03-03', pageNo: '5',
    publication: { name: 'The Fairview Gazette', location: 'Fairview, Kansas' }, url,
  };
}
function npInfo(clip) {
  return {
    ok: true, kind: 'article', focusClipId: clip.id, pageId: 5550001, pageWidth: 3000, pageHeight: 4000,
    token: 'eyJ.test.pass', me: ME, publication: { name: 'The Fairview Gazette', location: 'Fairview, Kansas' },
    pageDate: '1931-03-03', pageNo: '5', ownClips: [clip], unusable: [], openedUnusable: false,
    otherClipCount: 0, focusOwner: null,
  };
}
function npSite(lists) {
  const pages = { [NP_A]: npClip(101, 'Obituary for Calvin Hartley', NP_A), [NP_B]: npClip(202, 'Hartley wedding', NP_B) };
  return (msg, tab) => {
    switch (msg.action) {
      case 'prefill':
        return { ok: true, prefill: { url: tab.url, title: 'A page', people: [], recipeHint: 'newspapers', newspapers: true } };
      case 'newspapersInfo': return pages[tab.url] ? npInfo(pages[tab.url]) : { ok: false, error: 'not a clipping page' };
      case 'newspapersList': return lists.shift();
      case 'pagehtml': return { ok: true, html: '<html>' + tab.url + '</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy of ' + tab.url + '</html>' };
      case 'newspapersClipText': return { ok: true, ocr: { text: 'machine text', complete: true } };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}
const listed = (id, title, url, who) => ({ clipping_id: id, page_id: 5550001, title, url,
  user_username: who || ME.username, created: '2025-01-0' + (id % 9) + 'T00:00:00Z' });

test('Newspapers.com: only her own clippings are queued, and a page that met a "slow down" stops the run once saved', async () => {
  const lists = [{ ok: true, user: ME.username, more: false, nextCursor: null, clippings: [
    listed(101, 'Obituary for Calvin Hartley', '/article/the-fairview-gazette-obituary/101/'),
    listed(202, 'Hartley wedding', NP_B),
    listed(303, 'Someone else\'s clipping', '/article/x/303/', 'otherclipper'),
    listed(404, 'An address off the site', 'https://example.org/article/404/'),
  ] }];
  let pageDownloads = 0;
  const p = await openPanel({ url: NP_A, respond: npSite(lists), storage: ON,
    fullPage: () => ({ rateHits: ++pageDownloads === 1 ? 1 : 0 }) });
  quick(p);
  await start(p, 'np');
  const L = p.storage.npBatchLedger;
  assert.deepStrictEqual(L.order, ['101', '202']);
  assert.strictEqual(L.user, ME.username);
  assert.deepStrictEqual(p.sent.filter((m) => m.action === 'newspapersList').map((m) => m.cursor), [0]);
  // The list page, then the first clipping - saved, then the run stopped.
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [NP_LIST, NP_A]);
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.capJson(p.written[0]).url, NP_A);
  assert.strictEqual(L.items['101'].state, 'done');
  assert.strictEqual(L.items['202'].state, 'queued');
  assert.ok(L.pacing.resumeAt > Date.now() + 25 * 60 * 1000);
  assert.match(p.batchEl('np-status').textContent, /^Stopped: Newspapers\.com asked us to slow down while this one was saved/);
});

// ── PR #224 audit: one record never holds up the queue ──────────────────────

const NP_C = 'https://www.newspapers.com/article/the-fairview-gazette-reunion/505/';
function npPages(infoFor) {
  const base = npSite([]);
  return (lists) => (msg, tab) => {
    if (msg.action === 'newspapersList') return lists.shift();
    if (msg.action === 'newspapersInfo' && infoFor[tab.url]) return infoFor[tab.url]();
    return base(msg, tab);
  };
}

test('Newspapers.com: a clipping that marks no area is set aside (alone, or beside a usable one) and the run goes on (F1/F6/F13)', async () => {
  const lists = [{ ok: true, user: ME.username, more: false, nextCursor: null, clippings: [
    listed(101, 'Unusable alone', NP_A), listed(202, 'Unusable beside another', NP_B), listed(505, 'Reunion', NP_C),
  ] }];
  const other = npClip(303, 'A usable one on the same page', 'https://www.newspapers.com/article/x/303/');
  const respond = npPages({
    [NP_A]: () => Object.assign(npInfo(npClip(101, 'Unusable alone', NP_A)), { ownClips: [], openedUnusable: true }),
    [NP_B]: () => Object.assign(npInfo(npClip(202, 'Unusable beside another', NP_B)), { ownClips: [other], openedUnusable: true }),
    [NP_C]: () => npInfo(npClip(505, 'Reunion', NP_C)),
  })(lists);
  const p = await openPanel({ url: NP_A, respond, storage: ON });
  quick(p);
  await start(p, 'np');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [NP_LIST, NP_A, NP_B, NP_C]);
  const L = p.storage.npBatchLedger;
  assert.strictEqual(L.items['101'].state, 'failed');
  assert.match(L.items['101'].lastError, /marks no area/);
  assert.strictEqual(L.items['202'].state, 'failed');
  assert.match(L.items['202'].lastError, /tick at least one|marks no area/);
  assert.strictEqual(L.items['505'].state, 'done');
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.capJson(p.written[0]).url, NP_C);
  assert.match(p.batchEl('np-status').textContent, /^Finished\. 1 done, 0 to go, 2 set aside\./);
});

test('Newspapers.com: a page showing another of her clippings than the one queued saves nothing, and fetches no page (F18)', async () => {
  const lists = [{ ok: true, user: ME.username, more: false, nextCursor: null, clippings: [listed(202, 'Hartley wedding', NP_B)] }];
  let pageDownloads = 0;
  const respond = npPages({ [NP_B]: () => npInfo(npClip(101, 'Obituary for Calvin Hartley', NP_A)) })(lists);
  const p = await openPanel({ url: NP_A, respond, storage: ON, fullPage: () => { pageDownloads++; return {}; } });
  quick(p);
  await start(p, 'np');
  assert.strictEqual(p.written.length, 0);
  assert.strictEqual(pageDownloads, 0, 'the newspaper page was downloaded');
  const it = p.storage.npBatchLedger.items['202'];
  assert.strictEqual(it.state, 'failed');
  assert.match(it.lastError, /not the one queued \(202\)/);
});

test('Newspapers.com: a capture that met a "slow down" but saved nothing still starts the break (F10/F16)', async () => {
  const lists = [{ ok: true, user: ME.username, more: false, nextCursor: null, clippings: [
    listed(101, 'Obituary for Calvin Hartley', NP_A), listed(202, 'Hartley wedding', NP_B)] }];
  const p = await openPanel({ url: NP_A, respond: npSite(lists), storage: ON, fullPage: () => ({ rateHits: 1 }) });
  quick(p);
  p.writeFails.add(0);
  await start(p, 'np');
  assert.strictEqual(p.written.length, 0);
  const L = p.storage.npBatchLedger;
  assert.strictEqual(L.items['101'].state, 'queued');
  assert.ok(L.pacing.resumeAt > Date.now() + 25 * 60 * 1000, 'no break after the slow down');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [NP_LIST, NP_A]);
  assert.match(p.batchEl('np-status').textContent, /^Stopped: Newspapers\.com asked us to slow down/);
});

test('saved in part: the next Start asks Seen It about the missing scan, not the record, and captures it again (F2/F8/F14)', async () => {
  let image = { ok: false, error: 'the Ancestry image service returned HTTP 429' };
  const base = ancestrySite();
  const seenIt = {};
  const p = await openPanel({ url: HARRIET, seenIt, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => (msg.action === 'ancestryImage' && image ? image : base(msg, tab)) });
  quick(p);
  await start(p, 'anc');
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'partial');
  assert.match(p.batchEl('anc-status').textContent, /1 saved in part/);
  // The break is over, the record's index is now in the inbox (Seen It
  // knows rec:), and Ancestry hands the scan over.
  p.storage.ancBatchLedger.pacing.resumeAt = 0;
  seenIt['rec:90001:1000001'] = 'captured';
  image = null;
  const asked = p.external.length;
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN, CALVIN, ETHEL]);
  assert.deepStrictEqual(p.external[asked].msg.keys, ['img:90001:4000123_00045']);
  const L = p.storage.ancBatchLedger;
  assert.strictEqual(L.items['90001:1000001'].state, 'done');
  assert.strictEqual(L.items['90002:2000001'].state, 'done');
  assert.strictEqual(p.written.length, 3);
  assert.ok(p.files(p.written[1]).includes('record.jpg'));
});

test('Find a Grave: a memorial whose photos were cut short is captured again at the next Start (F14)', async () => {
  const L = { version: 1, items: {}, order: [], log: [] };
  for (const [id, slug] of [['900000001', 'calvin-george-hartley'], ['900000002', 'harriet-frances-hartley']]) {
    L.items[id] = { key: id, memorialId: id, slug, url: 'https://www.findagrave.com/memorial/' + id + '/' + slug,
      persons: [], title: '', state: 'queued', tries: 0 };
    L.order.push(id);
  }
  let slow = true;
  const p = await openPanel({ url: FAG_CALVIN, respond: fagSite(), storage: Object.assign({}, ON, { fagBatchLedger: L }),
    fetch: async (url) => (slow ? { ok: false, status: 429, url, headers: { get: () => 'text/html' } }
      : { ok: true, status: 200, url, headers: { get: () => 'image/jpeg' }, blob: async () => new Blob(['photo'], { type: 'image/jpeg' }) }) });
  quick(p);
  await start(p, 'fag');
  assert.strictEqual(p.storage.fagBatchLedger.items['900000001'].state, 'partial');
  p.storage.fagBatchLedger.pacing.resumeAt = 0;
  slow = false;
  const asked = p.external.length;
  await start(p, 'fag');
  assert.deepStrictEqual(p.navigations.map((n) => n.url).slice(1), [FAG_CALVIN,
    'https://www.findagrave.com/memorial/900000002/harriet-frances-hartley']);
  assert.deepStrictEqual(p.external[asked].msg.keys, ['fag:900000001']);
  assert.strictEqual(p.storage.fagBatchLedger.items['900000001'].state, 'done');
});

function fagLedger(n) {
  const L = { version: 1, items: {}, order: [], log: [] };
  for (let i = 1; i <= n; i++) {
    const id = '90000000' + i;
    L.items[id] = { key: id, memorialId: id, url: 'https://www.findagrave.com/memorial/' + id, persons: [], title: '',
      state: 'queued', tries: 0 };
    L.order.push(id);
  }
  return L;
}

test('Find a Grave: a busy page at a memorial\'s address stops the run with the break; nothing is set aside as removed (F15)', async () => {
  const p = await openPanel({ url: FAG_CALVIN, respond: fagSite(), titleFor: () => '503 Service Temporarily Unavailable',
    storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(3) }) });
  quick(p);
  await start(p, 'fag');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), ['https://www.findagrave.com/memorial/900000001']);
  assert.strictEqual(p.written.length, 0);
  const L = p.storage.fagBatchLedger;
  assert.deepStrictEqual(L.order.map((k) => L.items[k].state), ['queued', 'queued', 'queued']);
  assert.ok(L.pacing.resumeAt > Date.now() + 25 * 60 * 1000);
  assert.match(p.batchEl('fag-status').textContent, /^Stopped: Find a Grave answered with a busy or error page/);
});

test('Find a Grave: a memorial page with no memorial on it is tried once more, never set aside as removed at once; three in a row stop the run (F15)', async () => {
  const base = fagSite();
  const noMemorial = { ok: false, error: 'this page shows no memorial - it may have been removed or merged into another. ' +
    'Search Find a Grave for the person and open the memorial it shows now' };
  // Memorial 1 never shows; 2 does.
  const p = await openPanel({ url: FAG_CALVIN, storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(2) }),
    respond: (msg, tab) => (msg.action === 'findagraveMemorialInfo' && /900000001$/.test(tab.url) ? noMemorial : base(msg, tab)),
    fetch: async (url) => ({ ok: true, status: 200, url, headers: { get: () => 'image/jpeg' },
      blob: async () => new Blob(['photo'], { type: 'image/jpeg' }) }) });
  quick(p);
  await start(p, 'fag');
  assert.deepStrictEqual(p.navigations.map((n) => n.url.replace(/^.*\//, '')), ['900000001', '900000002', '900000001']);
  const L = p.storage.fagBatchLedger;
  assert.strictEqual(L.items['900000002'].state, 'done');
  assert.strictEqual(L.items['900000001'].state, 'failed');
  // Every memorial page empty: the third in a row stops the run.
  const q = await openPanel({ url: FAG_CALVIN, storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(4) }),
    respond: (msg, tab) => (msg.action === 'findagraveMemorialInfo' ? noMemorial : base(msg, tab)) });
  quick(q);
  await start(q, 'fag');
  assert.strictEqual(q.navigations.length, 3);
  assert.match(q.batchEl('fag-status').textContent, /^Stopped: 3 records in a row could not be saved/);
});

test('a record the site refuses on two Starts is set aside, so it never holds up the queue (F13, third audit M1)', async () => {
  const base = ancestrySite();
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN
      ? { ok: false, error: 'the site refused this record (HTTP 403)' } : base(msg, tab)) });
  quick(p);
  await start(p, 'anc');
  assert.match(p.batchEl('anc-status').textContent, /^Stopped: the site refused this record/);
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'queued');
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN, CALVIN, ETHEL]);
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'failed');
  assert.strictEqual(p.storage.ancBatchLedger.items['90002:2000001'].state, 'done');
});

test('"Try the set-aside ones again" queues them again for the next Start (F9)', async () => {
  const L = ancLedger([CALVIN]);
  L.items['90001:1000001'].state = 'failed';
  L.items['90001:1000001'].lastError = 'the page shows Ancestry record 90001:1000002, not the one queued';
  const p = await openPanel({ url: ETHEL, respond: ancestrySite(), storage: Object.assign({}, ON, { ancBatchLedger: L }) });
  quick(p);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations, []);
  await p.batchEl('anc-retry').fire('click');
  await settle();
  assert.match(p.batchEl('anc-status').textContent, /^1 set-aside record is queued again/);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'done');
});

// ── PR #224 audit: Start, the lease and the switch ──────────────────────────

test('Start waits while a Capture by hand is still saving: the tab is never moved under it (F4/F11/F17)', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const base = ancestrySite();
  const p = await openPanel({ url: CALVIN, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([ETHEL]) }),
    respond: async (msg, tab) => {
      if (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN) await gate;
      return base(msg, tab);
    } });
  quick(p);
  const hand = p.$('btn-capture').fire('click');
  await settle();
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations, []);
  assert.match(p.batchEl('anc-status').textContent, /^A capture is still saving/);
  release();
  await hand;
  await settle();
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.capJson(p.written[0]).url, CALVIN);
});

test('another window that takes the batch over stops this run at its next step; the lease is not written over (F5/F12)', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const base = ancestrySite();
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: async (msg, tab) => {
      if (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN) await gate;
      return base(msg, tab);
    } });
  quick(p);
  const run = p.batchEl('anc-start').fire('click');
  await settle();
  const theirs = { owner: 'another-window', site: 'ancestry', until: Date.now() + 5 * 60 * 1000 };
  p.storage.batchLease = theirs;
  release();
  await run;
  await settle();
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
  assert.deepStrictEqual(p.storage.batchLease, theirs);
  assert.match(p.batchEl('anc-status').textContent, /^Stopped: another panel window started a batch/);
});

test('batch downloads switched off mid-run: the run stops after the record being saved (F5)', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const base = ancestrySite();
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: async (msg, tab) => {
      if (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN) await gate;
      return base(msg, tab);
    } });
  quick(p);
  const run = p.batchEl('anc-start').fire('click');
  await settle();
  p.chrome.storage.local.remove('batchDownloads');
  release();
  await run;
  await settle();
  assert.strictEqual(p.written.length, 1);
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
  assert.match(p.batchEl('anc-status').textContent, /^Stopped: batch downloads were switched off/);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
});

// ── PR #224 stand-in review D1-D4 ───────────────────────────────────────────

test('D1: a clipping whose title holds a number like 503 is a record, not a busy page', async () => {
  const lists = [{ ok: true, user: ME.username, more: false, nextCursor: null, clippings: [
    listed(101, 'Fire at 503 Elm Street', NP_A), listed(202, 'Hartley wedding', NP_B)] }];
  const p = await openPanel({ url: NP_A, respond: npSite(lists), storage: ON,
    titleFor: (u) => (u === NP_A ? 'Fire at 503 Elm Street - Newspapers.com' : 'Clipping 500 of 504 - Newspapers.com') });
  quick(p);
  await start(p, 'np');
  assert.strictEqual(p.written.length, 2, p.batchEl('np-status').textContent);
  assert.match(p.batchEl('np-status').textContent, /^Finished\. 2 done/);
});

test('D1 (third audit M2): a record whose address shows a busy page Start after Start is set aside after 3, as site trouble, and the queue moves on', async () => {
  const p = await openPanel({ url: FAG_CALVIN, respond: fagSite(), storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(2) }),
    titleFor: (u) => (/900000001$/.test(u) ? '503 Service Temporarily Unavailable' : 'A memorial - Find a Grave'),
    fetch: async (url) => ({ ok: true, status: 200, url, headers: { get: () => 'image/jpeg' },
      blob: async () => new Blob(['photo'], { type: 'image/jpeg' }) }) });
  quick(p);
  for (let i = 0; i < 2; i++) {
    await start(p, 'fag');
    assert.strictEqual(p.storage.fagBatchLedger.items['900000001'].state, 'queued', 'set aside after ' + (i + 1));
    p.storage.fagBatchLedger.pacing.resumeAt = 0;
  }
  await start(p, 'fag');
  const it = p.storage.fagBatchLedger.items['900000001'];
  assert.strictEqual(it.state, 'failed');
  assert.match(it.lastError, /^Find a Grave reported trouble with this record 3 times; it is set aside\./);
  p.storage.fagBatchLedger.pacing.resumeAt = 0;
  await start(p, 'fag');
  assert.deepStrictEqual(p.navigations.map((n) => n.url.replace(/^.*\//, '')), ['900000001', '900000001', '900000001', '900000002']);
  assert.strictEqual(p.storage.fagBatchLedger.items['900000002'].state, 'done');
});

test('D2: a scan refused for good is saved once, done without it - no false "sign in" stop, no second bundle', async () => {
  for (const error of [
    'Ancestry refused (HTTP 403) - sign in on this page, or the collection may not allow downloads',
    'Ancestry refused the image download (HTTP 403)',
    'the image came back too small to be the full record (12 KB)',
  ]) {
    const base = ancestrySite();
    const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
      respond: (msg, tab) => (msg.action === 'ancestryImage' ? { ok: false, error } : base(msg, tab)) });
    quick(p);
    await start(p, 'anc');
    await start(p, 'anc');
    assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN, ETHEL], error);
    assert.strictEqual(p.written.length, 2, error);
    const it = p.storage.ancBatchLedger.items['90001:1000001'];
    assert.strictEqual(it.state, 'done', error);
    assert.match(it.note, /^image not saved: /);
    assert.doesNotMatch(p.batchEl('anc-status').textContent, /sign in/, error);
  }
});

test('D2: a lost login (HTTP 401) on the scan still stops the run, and the record is tried again', async () => {
  const base = ancestrySite();
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => (msg.action === 'ancestryImage'
      ? { ok: false, error: 'Ancestry refused (HTTP 401) - sign in on this page, or the collection may not allow downloads' }
      : base(msg, tab)) });
  quick(p);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'partial');
  assert.match(p.batchEl('anc-status').textContent, /^Stopped: Ancestry asked you to sign in/);
});

test('D3 (revised): FamilySearch having trouble (HTTP 500) stops the run; a short outage over two Starts sets nothing aside', async () => {
  const calls = [];
  const p = await openPanel({ url: ark('HRT1-CEN2'), storage: Object.assign({}, ON,
    { fsBatchLedger: fsLedger(['HRT1-CEN1', 'HRT1-DTH1']) }),
  respond: familysearchSite(calls, (u) => (/\/ark:\/61903\/1:1:HRT1-CEN1$/.test(u) ? 500 : null)) });
  quick(p);
  await start(p, 'fs');
  await start(p, 'fs');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [ark('HRT1-CEN1'), ark('HRT1-CEN1')]);
  assert.strictEqual(p.storage.fsBatchLedger.items['HRT1-CEN1'].state, 'queued');
  assert.match(p.batchEl('fs-status').textContent, /^Stopped: FamilySearch is having trouble \(HTTP 500\)/);
});

test('a record FamilySearch persistently reports trouble with (HTTP 5xx) is set aside after 3 Starts, and the next record is reached', async () => {
  const calls = [];
  const p = await openPanel({ url: ark('HRT1-CEN2'), storage: Object.assign({}, ON,
    { fsBatchLedger: fsLedger(['HRT1-CEN1', 'HRT1-DTH1']) }),
  respond: familysearchSite(calls, (u) => (/\/ark:\/61903\/1:1:HRT1-CEN1$/.test(u) ? 502 : null)) });
  quick(p);
  for (let i = 0; i < 3; i++) await start(p, 'fs');
  const it = p.storage.fsBatchLedger.items['HRT1-CEN1'];
  assert.strictEqual(it.state, 'failed');
  assert.match(it.lastError, /^FamilySearch reported trouble with this record 3 times; it is set aside\. When the site is working again, press 'Try the set-aside ones again'\./);
  assert.strictEqual(p.storage.fsBatchLedger.items['HRT1-DTH1'].state, 'done');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [ark('HRT1-CEN1'), ark('HRT1-CEN1'), ark('HRT1-CEN1'), ark('HRT1-DTH1')]);
  // A lost login never counts: the same record, signed out, is never set aside.
  const q = await openPanel({ url: ark('HRT1-CEN2'), storage: Object.assign({}, ON,
    { fsBatchLedger: fsLedger(['HRT1-CEN1', 'HRT1-DTH1']) }),
  respond: familysearchSite([], (u) => (/\/ark:\/61903\/1:1:HRT1-CEN1$/.test(u) ? 401 : null)) });
  quick(q);
  for (let i = 0; i < 4; i++) await start(q, 'fs');
  assert.strictEqual(q.storage.fsBatchLedger.items['HRT1-CEN1'].state, 'queued');
});

test('D4: switching batches off during a slow-down break keeps the break owed', async () => {
  const L = ancLedger([CALVIN]);
  const until = Date.now() + 30 * 60 * 1000;
  L.pacing = { sinceShort: 0, shortEvery: 40, rateEvents: 1, resumeAt: until, resumeReason: 'ratelimit' };
  const p = await openPanel({ url: ETHEL, respond: ancestrySite(), storage: Object.assign({}, ON, { ancBatchLedger: L }) });
  quick(p);
  const run = p.batchEl('anc-start').fire('click');
  await settle();
  assert.match(p.batchEl('anc-status').textContent, /asked us to slow down, so the batch waits/);
  p.chrome.storage.local.remove('batchDownloads');
  await run;
  await settle();
  assert.match(p.batchEl('anc-status').textContent, /^Stopped: batch downloads were switched off/);
  assert.strictEqual(p.storage.ancBatchLedger.pacing.resumeAt, until);
  assert.deepStrictEqual(p.navigations, []);
});

test('a record a batch saves never carries a field she typed for the record the panel showed before', async () => {
  const p = await openPanel({ url: HARRIET, respond: ancestrySite(), storage: Object.assign({}, ON,
    { ancBatchLedger: ancLedger([ETHEL]) }) });
  quick(p);
  p.$('f-title').value = 'Harriet, typed by hand';
  await p.$('f-title').fire('input');
  p.$('f-notes').value = 'A note for Harriet';
  await start(p, 'anc');
  assert.strictEqual(p.written.length, 1);
  const cap = p.capJson(p.written[0]);
  assert.strictEqual(cap.title, 'Ethel Hartley - California, U.S., Death Index, 1940-1997');
  assert.ok(!String(cap.notes || '').includes('A note for Harriet'));
});

// ── PR #224 Codex review (4503786095, -098, -101, -102) ─────────────────────

const PHOTO_OK = async (url) => ({ ok: true, status: 200, url, headers: { get: () => 'image/jpeg' },
  blob: async () => new Blob(['photo'], { type: 'image/jpeg' }) });

test('Codex 4503786095: a Find a Grave 503, for a photo or a later page of the photo list, stops the run with the break', async () => {
  // A photo answers 503.
  const p = await openPanel({ url: FAG_CALVIN, respond: fagSite(), storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(2) }),
    fetch: async (url) => ({ ok: false, status: 503, url, headers: { get: () => 'text/html' } }) });
  quick(p);
  await start(p, 'fag');
  assert.strictEqual(p.fetched.length, 1, 'photos asked for after the 503');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), ['https://www.findagrave.com/memorial/900000001']);
  const L = p.storage.fagBatchLedger;
  assert.strictEqual(L.items['900000001'].state, 'partial');
  assert.strictEqual(L.items['900000002'].state, 'queued');
  assert.ok(L.pacing.resumeAt > Date.now() + 25 * 60 * 1000);
  assert.match(p.batchEl('fag-status').textContent, /^Stopped: Find a Grave asked us to slow down/);
  // A later page of the photo list answers 503: no photo is asked for.
  const base = fagSite();
  const q = await openPanel({ url: FAG_CALVIN, fetch: PHOTO_OK, storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(2) }),
    respond: async (msg, tab) => {
      if (msg.action !== 'findagraveGallery') return base(msg, tab);
      const got = await base(msg, tab);
      return Object.assign(got, { partial: true, partialStatus: 503,
        partialError: 'Find a Grave is having trouble (HTTP 503). Try again in a little while' });
    } });
  quick(q);
  await start(q, 'fag');
  assert.strictEqual(q.fetched.length, 0);
  assert.strictEqual(q.navigations.length, 1);
  assert.ok(q.storage.fagBatchLedger.pacing.resumeAt > Date.now() + 25 * 60 * 1000);
});

test('Codex 4503786098: photos lost to a dropped connection leave the memorial to be fetched again, not done', async () => {
  const p = await openPanel({ url: FAG_CALVIN, respond: fagSite(), storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(2) }),
    fetch: async () => { throw new TypeError('Failed to fetch'); } });
  quick(p);
  await start(p, 'fag');
  const L = p.storage.fagBatchLedger;
  assert.strictEqual(L.items['900000001'].state, 'partial');
  assert.strictEqual(L.items['900000002'].state, 'partial');
  assert.match(p.batchEl('fag-status').textContent, /2 saved in part/);
});

test('Codex 4503786098: a scan the fetcher marks transient is saved in part, whatever its wording', async () => {
  const base = ancestrySite();
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN]) }),
    respond: (msg, tab) => (msg.action === 'ancestryImage'
      ? { ok: false, transient: true, error: 'the scan stopped coming part way' } : base(msg, tab)) });
  quick(p);
  await start(p, 'anc');
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'partial');
});

test('Codex 4503786101: two windows pressing Start together stage each record once', async () => {
  const sharedStore = JSON.parse(JSON.stringify(Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) })));
  const a = await openPanel({ url: HARRIET, respond: ancestrySite(), sharedStore });
  const b = await openPanel({ url: HARRIET, respond: ancestrySite(), sharedStore });
  quick(a);
  quick(b);
  await Promise.all([a.batchEl('anc-start').fire('click'), b.batchEl('anc-start').fire('click')]);
  await settle();
  const staged = a.written.concat(b.written).map((spec) => spec.captureJson.url).sort();
  assert.deepStrictEqual(staged, [CALVIN, ETHEL].sort());
  assert.strictEqual(a.navigations.length + b.navigations.length, 2);
  const said = [a.batchEl('anc-status').textContent, b.batchEl('anc-status').textContent];
  assert.ok(said.some((t) => /^A batch is running in another window/.test(t)), said.join(' | '));
  assert.ok(said.some((t) => /^Finished\. 2 done/.test(t)), said.join(' | '));
  assert.strictEqual(sharedStore.batchLease, undefined, 'the lease was let go');
});

test('Codex 4503786102: a ledger the browser will not save keeps a batch from starting', async () => {
  const p = await openPanel({ url: HARRIET, respond: ancestrySite(), storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN]) }) });
  quick(p);
  p.setStorageFault((k) => (k === 'ancBatchLedger' ? 'QUOTA_BYTES quota exceeded' : null));
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations, []);
  assert.strictEqual(p.written.length, 0);
  assert.match(p.batchEl('anc-status').textContent,
    /^Did not start: the browser could not save the batch’s progress \(QUOTA_BYTES quota exceeded\), so nothing was downloaded/);
});

test('Codex 4503786102: a ledger write that fails mid-run stops before the next download', async () => {
  const base = ancestrySite();
  let p = null;
  p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => {
      // The browser's storage fills up while Calvin's record is saved.
      if (msg.action === 'ancestryImage') p.setStorageFault((k) => (k === 'ancBatchLedger' ? 'QUOTA_BYTES quota exceeded' : null));
      return base(msg, tab);
    } });
  quick(p);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
  assert.strictEqual(p.written.length, 1);
  assert.match(p.batchEl('anc-status').textContent,
    /^Stopped: the browser could not save the batch’s progress \(QUOTA_BYTES quota exceeded\), so nothing more was downloaded/);
});

// ── PR #224 Codex 4503849177: a slow-down in a capture that then failed ─────

test('Codex 4503849177: a Newspapers.com page that met a "slow down" and then lost a tile still starts the break', async () => {
  const lists = [{ ok: true, user: ME.username, more: false, nextCursor: null, clippings: [
    listed(101, 'Obituary for Calvin Hartley', NP_A), listed(202, 'Hartley wedding', NP_B)] }];
  // As lib/newspapers.js fullPage throws it: 429, then the tile came, then a later tile's connection dropped.
  const p = await openPanel({ url: NP_A, respond: npSite(lists), storage: ON,
    fullPage: () => {
      throw Object.assign(new Error('could not reach Newspapers.com for the page image, so the capture stopped'),
        { rateHits: 1, slowDown: true });
    } });
  quick(p);
  await start(p, 'np');
  assert.strictEqual(p.written.length, 0);
  const L = p.storage.npBatchLedger;
  assert.strictEqual(L.items['101'].state, 'queued');
  assert.ok(L.pacing.resumeAt > Date.now() + 25 * 60 * 1000, 'no break after the slow down');
  assert.match(p.batchEl('np-status').textContent, /^Stopped: Newspapers\.com asked us to slow down/);
});

test('Codex 4503849177: a slow-down met by the Ancestry scan or a Find a Grave photo reaches the batch when the save then fails', async () => {
  // Ancestry: the scan answered 429, then the bundle could not be written.
  const base = ancestrySite();
  const a = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => (msg.action === 'ancestryImage'
      ? { ok: false, slowDown: true, transient: true, error: 'the Ancestry image service returned HTTP 429' } : base(msg, tab)) });
  quick(a);
  a.writeFails.add(0);
  await start(a, 'anc');
  assert.strictEqual(a.written.length, 0);
  assert.deepStrictEqual(a.navigations.map((n) => n.url), [CALVIN]);
  assert.ok(a.storage.ancBatchLedger.pacing.resumeAt > Date.now() + 25 * 60 * 1000, 'Ancestry: no break');
  // Find a Grave: a photo answered 429, then the bundle could not be written.
  const f = await openPanel({ url: FAG_CALVIN, respond: fagSite(), storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(2) }),
    fetch: async (url) => ({ ok: false, status: 429, url, headers: { get: () => 'text/html' } }) });
  quick(f);
  f.writeFails.add(0);
  await start(f, 'fag');
  assert.strictEqual(f.written.length, 0);
  assert.strictEqual(f.navigations.length, 1);
  assert.ok(f.storage.fagBatchLedger.pacing.resumeAt > Date.now() + 25 * 60 * 1000, 'Find a Grave: no break');
});

// ── PR #224 Codex 4504189301: one ledger writer at a time ───────────────────

const gedFor = (dbId, recordId, name) => ['0 HEAD', '0 @I1@ INDI', '1 NAME ' + name, '1 BIRT', '2 SOUR @S1@',
  '3 _APID 1,' + dbId + '::' + recordId, '0 TRLR'].join('\n');
async function importGed(p, name, text) {
  const input = p.batchEl('anc-file');
  input.files = [{ name, text: async () => text }];
  await input.fire('change');
}

test('Codex 4504189301: two windows queuing a GEDCOM each at the same moment keep both records', async () => {
  const sharedStore = JSON.parse(JSON.stringify(ON));
  const a = await openPanel({ url: CALVIN, respond: ancestrySite(), sharedStore });
  const b = await openPanel({ url: CALVIN, respond: ancestrySite(), sharedStore });
  await Promise.all([importGed(a, 'calvin.ged', gedFor('90001', '1000001', 'Calvin /Hartley/')),
    importGed(b, 'ethel.ged', gedFor('90002', '2000001', 'Ethel /Hartley/'))]);
  await settle();
  const L = sharedStore.ancBatchLedger;
  assert.deepStrictEqual(Object.keys(L.items).sort(), ['90001:1000001', '90002:2000001']);
  assert.deepStrictEqual(L.order.slice().sort(), ['90001:1000001', '90002:2000001']);
});

test('Codex 4504189301: a GEDCOM queued in one window while another notes a record saved by hand keeps both', async () => {
  const sharedStore = JSON.parse(JSON.stringify(ON));
  let manualB = null;
  const a = await openPanel({ url: CALVIN, respond: ancestrySite(), sharedStore });
  const b = await openPanel({ url: CALVIN, respond: ancestrySite(), sharedStore,
    beforePanel: (w) => {
      const real = w.FHA.batchPanel.install;
      w.FHA.batchPanel.install = (hooks) => {
        const got = real(hooks);
        manualB = got.recordManual;
        return got;
      };
    } });
  assert.ok(manualB);
  await Promise.all([
    importGed(a, 'ethel.ged', gedFor('90002', '2000001', 'Ethel /Hartley/')),
    manualB({ ok: true, site: 'ancestry', key: 'rec:90001:1000001', bundle: 'calvin-bundle',
      record: { dbId: '90001', recordId: '1000001', name: 'Calvin Hartley' } }),
  ]);
  await settle();
  const L = sharedStore.ancBatchLedger;
  assert.strictEqual(L.items['90002:2000001'].state, 'queued');
  assert.strictEqual(L.items['90001:1000001'].state, 'done');
  assert.strictEqual(L.items['90001:1000001'].note, 'saved by hand');
});

test('Codex 4504189301: two imports overlapping in one window keep both records', async () => {
  const p = await openPanel({ url: CALVIN, respond: ancestrySite(), storage: ON });
  const input = p.batchEl('anc-file');
  const one = (name, text) => { input.files = [{ name, text: async () => text }]; return input.fire('change'); };
  await Promise.all([one('calvin.ged', gedFor('90001', '1000001', 'Calvin /Hartley/')),
    one('ethel.ged', gedFor('90002', '2000001', 'Ethel /Hartley/'))]);
  await settle();
  assert.deepStrictEqual(Object.keys(p.storage.ancBatchLedger.items).sort(), ['90001:1000001', '90002:2000001']);
});

// ── PR #224 Codex 4504294846: a slow-down while the page is read ────────────

test('Codex 4504294846: FamilySearch saying slow down to the image check stops the run before the capture', async () => {
  const calls = [];
  const p = await openPanel({ url: ark('HRT1-CEN2'), storage: Object.assign({}, ON,
    { fsBatchLedger: fsLedger(['HRT1-CEN1', 'HRT1-DTH1']) }),
  respond: familysearchSite(calls, (u) => (/dist\.jpg$/.test(u) ? 429 : null)) });
  quick(p);
  // The panel's own read of the page it opened on asked too; count from Start.
  const before = calls.filter((c) => /dist\.jpg$/.test(c.url)).length;
  await start(p, 'fs');
  assert.strictEqual(p.written.length, 0, 'a capture went on after the slow down');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [ark('HRT1-CEN1')]);
  assert.strictEqual(calls.filter((c) => /dist\.jpg$/.test(c.url)).length - before, 1, 'the image was asked for again');
  const L = p.storage.fsBatchLedger;
  assert.strictEqual(L.items['HRT1-CEN1'].state, 'queued');
  assert.ok(L.pacing.resumeAt > Date.now() + 25 * 60 * 1000);
  assert.match(p.batchEl('fs-status').textContent, /^Stopped: FamilySearch asked us to slow down while the page was being read/);
});

test('Codex 4504294846: Newspapers.com saying slow down to the clippings list on the page stops the run before the page download', async () => {
  const lists = [{ ok: true, user: ME.username, more: false, nextCursor: null, clippings: [listed(101, 'Obituary', NP_A)] }];
  let pageDownloads = 0;
  const respond = npPages({ [NP_A]: () => Object.assign(npInfo(npClip(101, 'Obituary', NP_A)),
    { listError: 'Newspapers.com would not send the list of clippings on this page (HTTP 429)' }) })(lists);
  const p = await openPanel({ url: NP_A, respond, storage: ON, fullPage: () => { pageDownloads++; return {}; } });
  quick(p);
  await start(p, 'np');
  assert.strictEqual(pageDownloads, 0);
  assert.strictEqual(p.written.length, 0);
  assert.ok(p.storage.npBatchLedger.pacing.resumeAt > Date.now() + 25 * 60 * 1000);
});

test('Codex 4504294846: a slow-down while a person\'s sources are read holds that site\'s batch back, and queues nothing', async () => {
  const person = 'https://www.familysearch.org/tree/person/details/KWHR-T01';
  const p = await openPanel({ url: person, storage: ON, respond: (msg) => (msg.action === 'familysearchPersonSources'
    ? { ok: false, slowDown: true, error: 'FamilySearch asked us to slow down (HTTP 429). Wait a few minutes, then press Capture again' }
    : msg.action === 'prefill' ? { ok: true, prefill: { url: person, title: 'A person', people: [] } } : { ok: false }) });
  await p.batchEl('fs-page').fire('click');
  await settle();
  const L = p.storage.fsBatchLedger;
  assert.ok(L && L.pacing.resumeAt > Date.now() + 25 * 60 * 1000, 'no break was kept');
  assert.strictEqual(L.order.length, 0);
  assert.match(p.batchEl('fs-status').textContent, /^FamilySearch asked us to slow down while this page was read/);
});

// ── PR #224 Codex 4504294850: a person's sources come from their sources list only

test('Codex 4504294850: on a census record, "Add the sources of the person in front of you" queues nothing', async () => {
  const BS = require('../src/lib/batch-sources.js');
  const base = ancestrySite();
  const p = await openPanel({ url: CALVIN, storage: ON, respond: (msg, tab) => (msg.action === 'ancestryPersonSources'
    ? BS.batchAncPersonSources(parse(sample('ancestry-record-census')), tab.url) : base(msg, tab)) });
  await p.batchEl('anc-page').fire('click');
  await settle();
  assert.ok(!p.storage.ancBatchLedger || p.storage.ancBatchLedger.order.length === 0, 'the household was queued');
  assert.match(p.batchEl('anc-status').textContent, /^This is not a person’s page with a sources list/);
});

// ── PR #224 Codex 4504415455: another window's writes survive a running batch

// A batch in window A, held at its first record's details until release().
async function heldRun(site, url, respondFor, storeExtra, gateAction, gateUrl) {
  const sharedStore = JSON.parse(JSON.stringify(Object.assign({}, ON, storeExtra)));
  let release;
  const gate = new Promise((r) => { release = r; });
  const base = respondFor();
  const a = await openPanel({ url, sharedStore, respond: async (msg, tab) => {
    if (msg.action === gateAction && tab.url === gateUrl) await gate;
    return base(msg, tab);
  } });
  quick(a);
  const run = a.batchEl(site + '-start').fire('click');
  await settle();
  return { a, run, release, sharedStore };
}

test('Codex 4504415455: a "slow down" another window met during the first record holds the run, and its break is kept', async () => {
  const calls = [];
  const h = await heldRun('fs', ark('HRT1-CEN2'), () => familysearchSite(calls),
    { fsBatchLedger: fsLedger(['HRT1-CEN1', 'HRT1-DTH1']) }, 'familysearchRecordInfo', ark('HRT1-CEN1'));
  // Window B: FamilySearch says slow down while a person's sources are read.
  const person = 'https://www.familysearch.org/tree/person/details/KWHR-T01';
  const b = await openPanel({ url: person, sharedStore: h.sharedStore, respond: (msg) => (msg.action === 'familysearchPersonSources'
    ? { ok: false, slowDown: true, error: 'FamilySearch asked us to slow down (HTTP 429). Wait a few minutes' }
    : msg.action === 'prefill' ? { ok: true, prefill: { url: person, title: 'A person', people: [] } } : { ok: false }) });
  await b.batchEl('fs-page').fire('click');
  await settle();
  const owed = h.sharedStore.fsBatchLedger.pacing.resumeAt;
  assert.ok(owed > Date.now() + 25 * 60 * 1000);
  h.release();
  await settle();
  await settle();
  assert.match(h.a.batchEl('fs-status').textContent, /asked us to slow down, so the batch waits/);
  await h.a.batchEl('fs-pause').fire('click');
  await h.run;
  await settle();
  assert.deepStrictEqual(h.a.navigations.map((n) => n.url), [ark('HRT1-CEN1')]);
  assert.strictEqual(h.sharedStore.fsBatchLedger.pacing.resumeAt, owed);
  assert.strictEqual(h.sharedStore.fsBatchLedger.items['HRT1-DTH1'].state, 'queued');
});

test('Codex 4504415455: "Try the set-aside ones again" pressed in another window during a run is not undone', async () => {
  const L = ancLedger([CALVIN, ETHEL]);
  L.items['90002:2000001'].state = 'failed';
  L.items['90002:2000001'].lastError = 'not the one queued';
  const h = await heldRun('anc', HARRIET, () => ancestrySite(), { ancBatchLedger: L }, 'ancestryRecordInfo', CALVIN);
  const b = await openPanel({ url: HARRIET, respond: ancestrySite(), sharedStore: h.sharedStore });
  await b.batchEl('anc-retry').fire('click');
  await settle();
  assert.strictEqual(h.sharedStore.ancBatchLedger.items['90002:2000001'].state, 'queued');
  h.release();
  await h.run;
  await settle();
  assert.deepStrictEqual(h.a.navigations.map((n) => n.url), [CALVIN, ETHEL]);
  assert.strictEqual(h.sharedStore.ancBatchLedger.items['90002:2000001'].state, 'done');
});

test('Codex 4504415455: a person another window adds to a queued record during a run is kept', async () => {
  const h = await heldRun('anc', HARRIET, () => ancestrySite(), { ancBatchLedger: ancLedger([CALVIN]) }, 'ancestryRecordInfo', CALVIN);
  const b = await openPanel({ url: HARRIET, respond: ancestrySite(), sharedStore: h.sharedStore });
  await importGed(b, 'harriet.ged', gedFor('90001', '1000001', 'Harriet /Hartley/'));
  await settle();
  h.release();
  await h.run;
  await settle();
  assert.deepStrictEqual(h.sharedStore.ancBatchLedger.items['90001:1000001'].persons, ['Harriet Hartley']);
  assert.strictEqual(h.sharedStore.ancBatchLedger.items['90001:1000001'].state, 'done');
});

// ── PR #224 Codex 4504468051: the batch's own resets survive its saves ──────

function withPacing(L, pacing) {
  L.pacing = Object.assign({ sinceShort: 0, shortEvery: 40, resumeReason: 'ratelimit', resumeAt: Date.now() - 1000,
    rateAt: Date.now() - 60 * 1000 }, pacing);
  return L;
}

test('Codex 4504468051: a clean capture after a slow-down resets the count to 0, and it stays 0 once saved', async () => {
  const p = await openPanel({ url: HARRIET, respond: ancestrySite(), storage: Object.assign({}, ON,
    { ancBatchLedger: withPacing(ancLedger([CALVIN]), { rateEvents: 1 }) }) });
  quick(p);
  await start(p, 'anc');
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.storage.ancBatchLedger.pacing.rateEvents, 0);
});

test('Codex 4504468051: the third slow-down resets the count to 0, keeps it 0, and keeps its long break', async () => {
  const base = ancestrySite();
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON,
    { ancBatchLedger: withPacing(ancLedger([CALVIN, ETHEL]), { rateEvents: 2 }) }),
  respond: (msg, tab) => (msg.action === 'ancestryImage'
    ? { ok: false, slowDown: true, transient: true, error: 'the Ancestry image service returned HTTP 429' } : base(msg, tab)) });
  quick(p);
  await start(p, 'anc');
  const pacing = p.storage.ancBatchLedger.pacing;
  assert.strictEqual(pacing.rateEvents, 0);
  assert.strictEqual(pacing.resumeReason, 'long');
  assert.ok(pacing.resumeAt > Date.now() + 3.5 * 3600 * 1000, 'the long break was shortened');
});

test('Codex 4504468051: a slow-down another window met after this run\'s reset survives this run\'s next save', async () => {
  const sharedStore = JSON.parse(JSON.stringify(Object.assign({}, ON, { fsBatchLedger: fsLedger(['HRT1-CEN1', 'HRT1-DTH1']) })));
  const calls = [];
  const a = await openPanel({ url: ark('HRT1-CEN2'), respond: familysearchSite(calls), sharedStore });
  quick(a).set('betweenMs', [60 * 1000, 60 * 1000]);
  const run = a.batchEl('fs-start').fire('click');
  await settle();
  // A has saved CEN1 (its count reset to 0) and waits before the next record.
  assert.strictEqual(sharedStore.fsBatchLedger.items['HRT1-CEN1'].state, 'done');
  const person = 'https://www.familysearch.org/tree/person/details/KWHR-T01';
  const b = await openPanel({ url: person, sharedStore, respond: (msg) => (msg.action === 'familysearchPersonSources'
    ? { ok: false, slowDown: true, error: 'FamilySearch asked us to slow down (HTTP 429)' }
    : msg.action === 'prefill' ? { ok: true, prefill: { url: person, title: 'A person', people: [] } } : { ok: false }) });
  await b.batchEl('fs-page').fire('click');
  await settle();
  const owed = sharedStore.fsBatchLedger.pacing.resumeAt;
  assert.strictEqual(sharedStore.fsBatchLedger.pacing.rateEvents, 1);
  await a.batchEl('fs-pause').fire('click');
  await run;
  await settle();
  assert.strictEqual(sharedStore.fsBatchLedger.pacing.rateEvents, 1, 'B\'s slow-down was lost');
  assert.strictEqual(sharedStore.fsBatchLedger.pacing.resumeAt, owed);
});

test('Codex 4504468051: "Read my clippings list again" pressed in another window while a list page is on its way restarts the list', async () => {
  const sharedStore = JSON.parse(JSON.stringify(ON));
  let release = null;
  const base = npSite([]);
  const page = (cursor) => (cursor === 24
    ? { ok: true, user: ME.username, more: false, nextCursor: null, clippings: [listed(202, 'Hartley wedding', NP_B)] }
    : { ok: true, user: ME.username, more: true, nextCursor: 24, clippings: [listed(101, 'Obituary', NP_A)] });
  const a = await openPanel({ url: NP_A, sharedStore, respond: async (msg, tab) => {
    if (msg.action !== 'newspapersList') return base(msg, tab);
    if (!release) await new Promise((r) => { release = r; });
    return page(msg.cursor);
  } });
  quick(a);
  const run = a.batchEl('np-start').fire('click');
  await settle();
  assert.ok(release, 'the first list page was not asked for');
  const b = await openPanel({ url: NP_A, respond: base, sharedStore });
  await b.batchEl('np-relist').fire('click');
  await settle();
  release();
  await run;
  await settle();
  assert.deepStrictEqual(a.sent.filter((m) => m.action === 'newspapersList').map((m) => m.cursor), [0, 0, 24]);
  assert.deepStrictEqual(sharedStore.npBatchLedger.order.slice().sort(), ['101', '202']);
});

// ── PR #224 Codex 4504508005: a file whose body broke off is tried again ────

const brokenBody = (type) => ({ ok: true, status: 200, headers: { get: () => type },
  blob: async () => { throw new TypeError('Failed to fetch'); }, text: async () => { throw new TypeError('Failed to fetch'); } });

test('Codex 4504508005: FamilySearch - the image\'s headers come, its body breaks off: partial, and the next Start saves it', async () => {
  const calls = [];
  let broken = true;
  const p = await openPanel({ url: ark('HRT1-CEN2'), storage: Object.assign({}, ON, { fsBatchLedger: fsLedger(['HRT1-CEN1']) }),
    respond: familysearchSite(calls, (u) => (broken && /dist\.jpg$/.test(u) ? brokenBody('image/jpeg') : null)) });
  quick(p);
  await start(p, 'fs');
  assert.strictEqual(p.storage.fsBatchLedger.items['HRT1-CEN1'].state, 'partial', p.batchEl('fs-status').textContent);
  broken = false;
  await start(p, 'fs');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [ark('HRT1-CEN1'), ark('HRT1-CEN1')]);
  assert.strictEqual(p.storage.fsBatchLedger.items['HRT1-CEN1'].state, 'done');
  assert.ok(p.files(p.written[1]).includes('record.jpg'));
});

test('Codex 4504508005: Ancestry - a scan that failed in words no rule knows is partial, never done, and the next Start saves it', async () => {
  const base = ancestrySite();
  let image = { ok: false, error: 'Failed to fetch' };
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN]) }),
    respond: (msg, tab) => (msg.action === 'ancestryImage' && image ? image : base(msg, tab)) });
  quick(p);
  await start(p, 'anc');
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'partial');
  image = null;
  await start(p, 'anc');
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'done');
  assert.ok(p.files(p.written[1]).includes('record.jpg'));
});

test('Codex 4504508005: Find a Grave - photos whose bodies break off, or a photo list that broke off, leave the memorial partial', async () => {
  const p = await openPanel({ url: FAG_CALVIN, respond: fagSite(), storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(1) }),
    fetch: async (url) => Object.assign(brokenBody('image/jpeg'), { url }) });
  quick(p);
  await start(p, 'fag');
  assert.strictEqual(p.storage.fagBatchLedger.items['900000001'].state, 'partial');
  const base = fagSite();
  const q = await openPanel({ url: FAG_CALVIN, storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(1) }),
    respond: (msg, tab) => (msg.action === 'findagraveGallery' ? { ok: false, error: 'Failed to fetch' } : base(msg, tab)) });
  quick(q);
  await start(q, 'fag');
  assert.strictEqual(q.storage.fagBatchLedger.items['900000001'].state, 'partial');
});

test('Codex 4504508005: Newspapers.com - a page whose tile broke off saves nothing, keeps the clipping queued, and the next Start saves it', async () => {
  const lists = [{ ok: true, user: ME.username, more: false, nextCursor: null, clippings: [listed(101, 'Obituary', NP_A)] }];
  let broken = true;
  const p = await openPanel({ url: NP_A, respond: npSite(lists), storage: ON, fullPage: () => {
    if (broken) throw new Error('the connection to Newspapers.com broke off while the page image was coming down, so the capture stopped');
    return {};
  } });
  quick(p);
  await start(p, 'np');
  assert.strictEqual(p.written.length, 0);
  assert.strictEqual(p.storage.npBatchLedger.items['101'].state, 'queued');
  broken = false;
  await start(p, 'np');
  assert.strictEqual(p.storage.npBatchLedger.items['101'].state, 'done');
});

test('Codex 4504508005 / third audit M1: a record\'s details lost to "Failed to fetch" stop the run, Start after Start, and never count against it', async () => {
  const base = ancestrySite();
  let offline = true;
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => (offline && msg.action === 'ancestryRecordInfo'
      ? { ok: false, error: 'Failed to fetch' } : base(msg, tab)) });
  quick(p);
  for (let i = 0; i < 3; i++) await start(p, 'anc');
  assert.match(p.batchEl('anc-status').textContent, /^Stopped: Failed to fetch/);
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'queued');
  assert.ok(!p.storage.ancBatchLedger.items['90001:1000001'].stops);
  offline = false;
  await start(p, 'anc');
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'done');
  assert.strictEqual(p.storage.ancBatchLedger.items['90002:2000001'].state, 'done');
});

// ── PR #224 audit of 21179bc (ported from the auditor's repro scripts) ──────

const nap = (ms) => new Promise((r) => setTimeout(r, ms));
const personPage = 'https://www.familysearch.org/tree/person/details/KWHR-T01';
function slowSourcesWindow(url, sharedStore, site) {
  const action = site === 'anc' ? 'ancestryPersonSources' : 'familysearchPersonSources';
  return openPanel({ url, sharedStore, respond: (msg) => (msg.action === action
    ? { ok: false, slowDown: true, error: 'the site asked us to slow down (HTTP 429)' }
    : msg.action === 'prefill' ? { ok: true, prefill: { url, title: 'A person', people: [] } } : { ok: false }) });
}

test('audit F1: a longer break another window stores while the batch sleeps out an earlier one is taken too', async () => {
  const sharedStore = JSON.parse(JSON.stringify(Object.assign({}, ON, { fsBatchLedger: fsLedger(['HRT1-CEN1', 'HRT1-DTH1']) })));
  let release;
  const gate = new Promise((r) => { release = r; });
  const calls = [];
  const base = familysearchSite(calls);
  const a = await openPanel({ url: ark('HRT1-CEN2'), sharedStore, respond: async (msg, tab) => {
    if (msg.action === 'familysearchRecordInfo' && tab.url === ark('HRT1-CEN1')) await gate;
    return base(msg, tab);
  } });
  quick(a);
  const run = a.batchEl('fs-start').fire('click');
  try {
    await settle();
    const b = await slowSourcesWindow(personPage, sharedStore, 'fs');
    const BP = b.worker.PACING;
    const realBreak = BP.rateBreakMs.slice();
    BP.rateBreakMs = [1500, 1500]; // B's first break: short, so the test is quick
    await b.batchEl('fs-page').fire('click');
    await settle();
    release();
    await settle();
    await nap(200);
    assert.match(a.batchEl('fs-status').textContent, /asked us to slow down, so the batch waits/);
    BP.rateBreakMs = realBreak; // B's second break, while A sleeps out the first: the real one
    await b.batchEl('fs-page').fire('click');
    await settle();
    await nap(2500);
    await settle();
    assert.ok(!a.navigations.some((n) => /DTH1/.test(n.url)), 'A asked FamilySearch inside the second break');
    assert.ok(sharedStore.fsBatchLedger.pacing.resumeAt > Date.now() + 25 * 60 * 1000);
  } finally {
    release();
    await a.batchEl('fs-pause').fire('click');
    await Promise.race([run, nap(3000)]);
  }
});

test('audit F2: two slow-downs close together in two windows count as two, so the third in a row gets the long break', async () => {
  const L = ancLedger([CALVIN, ETHEL]);
  L.pacing = { sinceShort: 0, shortEvery: 40, rateEvents: 1, rateAt: Date.now() - 60 * 60 * 1000,
    resumeReason: 'ratelimit', resumeAt: Date.now() - 1000 };
  const sharedStore = JSON.parse(JSON.stringify(Object.assign({}, ON, { ancBatchLedger: L })));
  let release;
  const gate = new Promise((r) => { release = r; });
  const base = ancestrySite();
  const a = await openPanel({ url: CALVIN, sharedStore, respond: async (msg, tab) => {
    if (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN) await gate;
    if (msg.action === 'ancestryImage') return { ok: false, slowDown: true, transient: true, error: 'the Ancestry image service returned HTTP 429' };
    return base(msg, tab);
  } });
  quick(a);
  const run = a.batchEl('anc-start').fire('click');
  await settle();
  const b = await slowSourcesWindow('https://www.ancestry.com/family-tree/person/tree/1/person/2/facts', sharedStore, 'anc');
  await b.batchEl('anc-page').fire('click');
  await settle();
  assert.strictEqual(sharedStore.ancBatchLedger.pacing.rateEvents, 2);
  release();
  await run;
  await settle();
  const p = sharedStore.ancBatchLedger.pacing;
  assert.strictEqual(p.resumeReason, 'long', 'the third slow-down in a row got only ' + p.resumeReason);
  assert.ok(p.resumeAt > Date.now() + 3.5 * 3600 * 1000);
});

test('audit F3: a Capture by hand in another window that met a slow-down - saved or failed - holds the running batch back', async () => {
  for (const failed of [false, true]) {
    const sharedStore = JSON.parse(JSON.stringify(Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) })));
      let release;
    const gate = new Promise((r) => { release = r; });
    const base = ancestrySite();
    const a = await openPanel({ url: CALVIN, sharedStore, respond: async (msg, tab) => {
      if (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN) await gate;
      return base(msg, tab);
    } });
    quick(a);
    const run = a.batchEl('anc-start').fire('click');
    try {
      await settle();
      // Window B: Harriet by hand. Ancestry says slow down - to the scan (saved
      // without it), or to the record's details (nothing saved).
      const b = await openPanel({ url: HARRIET, sharedStore, respond: (msg, tab) => {
        if (!failed && msg.action === 'ancestryImage') return { ok: false, slowDown: true, transient: true, error: 'the Ancestry image service returned HTTP 429' };
        if (failed && msg.action === 'ancestryRecordInfo') return { ok: false, error: 'Ancestry asked us to slow down (HTTP 429)' };
        return base(msg, tab);
      } });
      await b.capture();
      await settle();
      assert.ok(sharedStore.ancBatchLedger.pacing && sharedStore.ancBatchLedger.pacing.resumeAt > Date.now() + 25 * 60 * 1000,
        (failed ? 'failed' : 'saved') + ' hand capture: no break recorded');
      release();
      await settle();
      await settle();
      assert.ok(!a.navigations.some((n) => n.url === ETHEL), 'the batch asked Ancestry for Ethel straight after');
    } finally {
      release();
      await a.batchEl('anc-pause').fire('click');
      await Promise.race([run, nap(3000)]);
    }
  }
});

test('audit F4: a stamp an 0.8.0 ledger kept (here from a clock that ran ahead) decides nothing - the requeue holds', async () => {
  const L = ancLedger([CALVIN, ETHEL]);
  Object.assign(L.items['90002:2000001'], { state: 'failed', tries: 1, lastError: 'not the one queued',
    stateAt: Date.now() + 10 * 60 * 1000 });
  const p = await openPanel({ url: CALVIN, respond: ancestrySite(), storage: Object.assign({}, ON, { ancBatchLedger: L }) });
  await p.batchEl('anc-retry').fire('click');
  await settle();
  assert.match(p.batchEl('anc-status').textContent, /^1 set-aside record is queued again/);
  assert.strictEqual(p.storage.ancBatchLedger.items['90002:2000001'].state, 'queued');
});

test('a Start refused once it has claimed the batch (a capture began meanwhile) lets its lease go at once', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const base = ancestrySite();
  const p = await openPanel({ url: CALVIN, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([ETHEL]) }),
    respond: async (msg, tab) => {
      if (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN) await gate;
      return base(msg, tab);
    } });
  quick(p);
  const answer = p.worker.holdNext(); // the claim waits in the service worker
  const started = p.batchEl('anc-start').fire('click');
  await nap(10);
  const hand = p.$('btn-capture').fire('click'); // a hand Capture starts during the claim
  answer();
  await started;
  await settle();
  assert.deepStrictEqual(p.navigations, []);
  assert.match(p.batchEl('anc-status').textContent, /^A capture is still saving/);
  assert.strictEqual(p.storage.batchLease, undefined, 'the refused Start left its lease behind');
  release();
  await hand;
  await settle();
});

// ── PR #224 second audit of 21179bc..2116f51 (ported from the auditor's repros)

test('second audit N1: a slow-down met while the page was read counts once, however many Captures are pressed on it', async () => {
  const calls = [];
  const base = familysearchSite(calls);
  let checks = 0;
  const p = await openPanel({ url: ark('HRT1-CEN1'), storage: Object.assign({}, ON), respond: async (msg, tab) => {
    if (msg.action === 'familysearchImageCheck') {
      checks++;
      if (checks === 1) return { ok: false, slowDown: true, error: 'FamilySearch asked us to slow down (HTTP 429)' };
    }
    return base(msg, tab);
  } });
  for (let i = 1; i <= 3; i++) {
    await p.capture();
    await settle();
    await nap(50);
  }
  assert.strictEqual(p.written.length, 3);
  const pc = p.storage.fsBatchLedger.pacing;
  assert.strictEqual(pc.rateEvents, 1, 'one slow-down counted ' + pc.rateEvents + ' times');
  assert.strictEqual(pc.resumeReason, 'ratelimit');
});

test('second audit N2: with stamps pinned ahead of the clock, "Read my clippings list again" still wins over a page on its way', async () => {
  const ahead = Date.now() + 24 * 3600 * 1000; // saved while the clock ran a day fast
  const L = { version: 1, items: {}, order: [], log: [], listComplete: true, listCursor: 0, user: ME.username, listAt: ahead };
  const sharedStore = JSON.parse(JSON.stringify(Object.assign({}, ON, { npBatchLedger: L })));
  sharedStore.npBatchLedger.listComplete = false;
  let release = null;
  const base = npSite([]);
  const page = (cursor) => (cursor === 24
    ? { ok: true, user: ME.username, more: false, nextCursor: null, clippings: [listed(202, 'Hartley wedding', NP_B)] }
    : { ok: true, user: ME.username, more: true, nextCursor: 24, clippings: [listed(101, 'Obituary', NP_A)] });
  const a = await openPanel({ url: NP_A, sharedStore, respond: async (msg, tab) => {
    if (msg.action !== 'newspapersList') return base(msg, tab);
    if (!release) await new Promise((r) => { release = r; });
    return page(msg.cursor);
  } });
  quick(a);
  const run = a.batchEl('np-start').fire('click');
  await settle();
  assert.ok(release, 'the first list page was not asked for');
  const b = await openPanel({ url: NP_A, respond: base, sharedStore });
  await b.batchEl('np-relist').fire('click');
  await settle();
  release();
  await run;
  await settle();
  assert.deepStrictEqual(a.sent.filter((m) => m.action === 'newspapersList').map((m) => m.cursor), [0, 0, 24]);
});

test('second audit N3 / third audit M1: a fault in the add-on at one record is set aside on the second Start, the next is reached; a real outage still stops', async () => {
  const base = ancestrySite();
  const fault = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN
      ? { ok: false, codeError: true, error: 'the capture add-on could not read this record (TypeError: x is undefined) - a fault in the add-on, not the site' }
      : base(msg, tab)) });
  quick(fault);
  await start(fault, 'anc');
  assert.strictEqual(fault.storage.ancBatchLedger.items['90001:1000001'].state, 'queued');
  await start(fault, 'anc');
  assert.strictEqual(fault.storage.ancBatchLedger.items['90001:1000001'].state, 'failed');
  assert.match(fault.storage.ancBatchLedger.items['90001:1000001'].lastError, /stopped the batch 2 times at this record/);
  assert.strictEqual(fault.storage.ancBatchLedger.items['90002:2000001'].state, 'done');
  // A real outage - every record fails: the run still stops, and sets nothing aside.
  const out = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => (msg.action === 'ancestryRecordInfo'
      ? { ok: false, transient: true, error: 'the connection broke off (Failed to fetch) - press Capture again later' }
      : base(msg, tab)) });
  quick(out);
  await start(out, 'anc');
  await start(out, 'anc');
  await start(out, 'anc');
  assert.match(out.batchEl('anc-status').textContent, /^Stopped: the connection broke off/);
  assert.strictEqual(out.written.length, 0);
  assert.strictEqual(out.storage.ancBatchLedger.items['90001:1000001'].state, 'queued');
});

test('second audit N4: a slow-down from a host that is not a batch site holds no batch back; one from a batch site holds back only that site', async () => {
  const base = ancestrySite();
  const asked = [];
  const p = await openPanel({ url: CALVIN, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([ETHEL]), fsBatchLedger: fsLedger(['HRT1-DTH1']) }),
    respond: (msg, tab) => {
      if (msg.action === 'fetchAsset') {
        asked.push(msg.url);
        return { ok: false, status: 429, error: 'the page refused that download (HTTP 429)' };
      }
      return base(msg, tab);
    } });
  p.$('f-asset-url').value = 'https://records.example-county.gov/scans/1880/calvin.jpg';
  await p.capture();
  await settle();
  await nap(50);
  assert.deepStrictEqual(asked, ['https://records.example-county.gov/scans/1880/calvin.jpg']);
  assert.ok(!(p.storage.ancBatchLedger.pacing && p.storage.ancBatchLedger.pacing.resumeAt > Date.now()),
    'the Ancestry batch was held back for another host\'s 429');
  // A typed address on FamilySearch: FamilySearch's batch, not Ancestry's.
  p.$('f-asset-url').value = 'https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG9';
  await p.capture();
  await settle();
  await nap(50);
  assert.ok(p.storage.fsBatchLedger.pacing && p.storage.fsBatchLedger.pacing.resumeAt > Date.now() + 25 * 60 * 1000);
  assert.ok(!(p.storage.ancBatchLedger.pacing && p.storage.ancBatchLedger.pacing.resumeAt > Date.now()));
});

// ── PR #224 Codex 4504665602: a tolerated secondary request's slow-down ─────

test('Codex 4504665602: two clippings whose text requests get 429 while the tiles come - the batch takes the break', async () => {
  const lists = [{ ok: true, user: ME.username, more: false, nextCursor: null, clippings: [
    listed(101, 'Obituary for Calvin Hartley', NP_A), listed(202, 'Hartley wedding', NP_B)] }];
  const base = npSite(lists);
  const textAsks = [];
  const p = await openPanel({ url: NP_A, storage: ON, respond: (msg, tab) => {
    if (msg.action === 'newspapersClipText') {
      textAsks.push(msg.url);
      return { ok: false, status: 429, slowDown: true, error: 'HTTP 429' };
    }
    return base(msg, tab);
  } });
  quick(p);
  await start(p, 'np');
  assert.ok(textAsks.length >= 1, 'no clipping text was asked for');
  // The clipping was saved (its text is optional), and then the break.
  assert.strictEqual(p.written.length, 1);
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [NP_LIST, NP_A]);
  const L = p.storage.npBatchLedger;
  assert.strictEqual(L.items['202'].state, 'queued');
  assert.ok(L.pacing.resumeAt > Date.now() + 25 * 60 * 1000, 'no break after the text requests met 429');
});

test('Codex 4504665602 sweep: a batch site\'s host saying slow down to the page copy\'s pictures holds that batch back', async () => {
  const base = fagSite();
  const p = await openPanel({ url: FAG_CALVIN, storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(2) }),
    fetch: PHOTO_OK,
    respond: (msg, tab) => (msg.action === 'singlefile'
      ? { ok: true, html: '<html>copy</html>', slowHosts: ['images.findagrave.com'] } : base(msg, tab)) });
  quick(p);
  await start(p, 'fag');
  assert.strictEqual(p.navigations.length, 1);
  assert.ok(p.storage.fagBatchLedger.pacing.resumeAt > Date.now() + 25 * 60 * 1000);
  // A host that is no batch site's holds nothing back.
  const q = await openPanel({ url: FAG_CALVIN, storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(2) }),
    fetch: PHOTO_OK,
    respond: (msg, tab) => (msg.action === 'singlefile'
      ? { ok: true, html: '<html>copy</html>', slowHosts: ['cdn.example.org'] } : base(msg, tab)) });
  quick(q);
  await start(q, 'fag');
  assert.strictEqual(q.navigations.length, 2);
});

test('Codex 4504894210: another batch site\'s host saying slow down during a capture holds THAT site back; this batch goes on', async () => {
  // A Find a Grave page whose pictures come from ancestrycdn.com (Ancestry's).
  const base = fagSite();
  const p = await openPanel({ url: FAG_CALVIN, storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(2) }),
    fetch: PHOTO_OK,
    respond: (msg, tab) => (msg.action === 'singlefile'
      ? { ok: true, html: '<html>copy</html>', slowHosts: ['www.ancestrycdn.com'] } : base(msg, tab)) });
  quick(p);
  await start(p, 'fag');
  const F = p.storage.fagBatchLedger;
  assert.strictEqual(p.navigations.length, 2);
  assert.deepStrictEqual(F.order.map((k) => F.items[k].state), ['done', 'done']);
  assert.ok(!(F.pacing && F.pacing.resumeAt > Date.now()), 'Find a Grave held back for Ancestry\'s slow-down');
  const A = p.storage.ancBatchLedger;
  assert.ok(A && A.pacing && A.pacing.resumeAt > Date.now() + 25 * 60 * 1000, 'no Ancestry break stored');
});

// ── PR #224 third audit (M1, M2), ported from the auditor's repros ──────────

// Over four Starts: past MAX_TROUBLE_TRIES, so a cause that ever counted would show (X1).
test('third audit M1, X1: no internet over four Starts sets nothing aside, and everything is saved once it is back', async () => {
  const OFFLINE = { ok: false, transient: true, error: 'could not reach FamilySearch - check your internet connection, then press Capture again' };
  let online = false;
  const calls = [];
  const base = familysearchSite(calls);
  const p = await openPanel({ url: ark('HRT1-CEN2'), storage: Object.assign({}, ON, { fsBatchLedger: fsLedger(['HRT1-CEN1', 'HRT1-DTH1']) }),
    respond: (msg, tab) => (!online && /^familysearch/.test(msg.action) ? OFFLINE : base(msg, tab)) });
  quick(p);
  for (let i = 1; i <= 5; i++) {
    if (i === 5) online = true;
    await start(p, 'fs');
    const L = p.storage.fsBatchLedger;
    assert.notStrictEqual(L.items['HRT1-CEN1'].state, 'failed', 'CEN1 set aside on Start ' + i);
    if (i < 5) assert.strictEqual(L.items['HRT1-CEN1'].stops || 0, 0, 'CEN1 counted on Start ' + i);
  }
  assert.strictEqual(p.storage.fsBatchLedger.items['HRT1-CEN1'].state, 'done');
  assert.strictEqual(p.storage.fsBatchLedger.items['HRT1-DTH1'].state, 'done');
});

test('third audit M1, X1: downloads the browser blocks over four Starts set nothing aside', async () => {
  const p = await openPanel({ url: HARRIET, respond: ancestrySite(), storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }) });
  quick(p);
  for (let i = 1; i <= 4; i++) {
    p.writeFails.add(p.written.length);
    await start(p, 'anc');
    const it = p.storage.ancBatchLedger.items['90001:1000001'];
    assert.deepStrictEqual([it.state, it.stops || 0], ['queued', 0], 'Start ' + i);
  }
  await start(p, 'anc');
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'done');
  assert.strictEqual(p.storage.ancBatchLedger.items['90002:2000001'].state, 'done');
});

test('third audit M2: "502 Bad Gateway" at a record over two Starts sets nothing aside; when the site is back it is saved', async () => {
  let down = true;
  const p = await openPanel({ url: HARRIET, respond: ancestrySite(),
    storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    titleFor: () => (down ? '502 Bad Gateway' : 'Calvin Hartley - 1880 census - Ancestry') });
  quick(p);
  for (let i = 1; i <= 3; i++) {
    if (i === 3) down = false;
    if (p.storage.ancBatchLedger.pacing) p.storage.ancBatchLedger.pacing.resumeAt = 0;
    await start(p, 'anc');
    if (i < 3) assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'queued', 'Start ' + i);
  }
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'done');
  assert.strictEqual(p.storage.ancBatchLedger.items['90002:2000001'].state, 'done');
});

test('third audit M1: a page that goes away before it answers (message port closed) counts as site trouble - 3 Starts, not 2', async () => {
  const base = ancestrySite();
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN ? PORT_CLOSED : base(msg, tab)) });
  quick(p);
  await start(p, 'anc');
  await start(p, 'anc');
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'queued');
  await start(p, 'anc');
  assert.strictEqual(p.storage.ancBatchLedger.items['90001:1000001'].state, 'failed');
  assert.match(p.storage.ancBatchLedger.items['90001:1000001'].lastError, /^Ancestry reported trouble with this record 3 times/);
  assert.strictEqual(p.storage.ancBatchLedger.items['90002:2000001'].state, 'done');
});

// ── PR #224 X1: a stop is sorted on its cause, never on the advice after it ──

// What content.js brokeOff answers for a fault thrown reading one clipping.
const NP_FAULT = { ok: false, codeError: true,
  error: "the capture add-on could not read this record (TypeError: Cannot read properties of undefined (reading 'rects')) - a fault in the add-on, not the site" };
const npQueue = () => ({ version: 1, items: {
  101: { id: 101, url: NP_A, pageId: 5550001, title: 'Obituary', state: 'queued', tries: 0 },
  202: { id: 202, url: NP_B, pageId: 5550001, title: 'Hartley wedding', state: 'queued', tries: 0 },
}, order: ['101', '202'], log: [], listComplete: true, listCursor: 0, user: ME.username });

test('X1: a fault reading one Newspapers.com clipping is set aside on the second Start, though the panel\'s advice names the panel; the next is saved', async () => {
  const p = await openPanel({ url: NP_B, respond: npPages({ [NP_A]: () => NP_FAULT })([]),
    storage: Object.assign({}, ON, { npBatchLedger: npQueue() }) });
  quick(p);
  await start(p, 'np');
  // The reason she sees still carries its advice; it decides nothing.
  assert.match(p.batchEl('np-status').textContent, /could not read this record.*open the panel again/);
  assert.deepStrictEqual([p.storage.npBatchLedger.items['101'].state, p.storage.npBatchLedger.items['101'].stops], ['queued', 1]);
  assert.strictEqual(p.written.length, 0);
  await start(p, 'np');
  const L = p.storage.npBatchLedger;
  assert.strictEqual(L.items['101'].state, 'failed');
  assert.match(L.items['101'].lastError, /stopped the batch 2 times at this record/);
  assert.strictEqual(L.items['202'].state, 'done');
  assert.deepStrictEqual(p.written.map((w) => p.capJson(w).url), [NP_B]);
});

test('X1: a clipping page the panel cannot use, in words no rule knows, is set aside after 3 Starts - "open the panel again" never makes it a run cause', async () => {
  for (const words of ['the page data does not match this address', 'could not re-read this page']) {
    const p = await openPanel({ url: NP_B, respond: npPages({ [NP_A]: () => ({ ok: false, error: words }) })([]),
      storage: Object.assign({}, ON, { npBatchLedger: npQueue() }) });
    quick(p);
    for (let i = 1; i <= 2; i++) {
      await start(p, 'np');
      const it = p.storage.npBatchLedger.items['101'];
      assert.deepStrictEqual([it.state, it.stops], ['queued', i], words + ', Start ' + i);
    }
    await start(p, 'np');
    const L = p.storage.npBatchLedger;
    assert.strictEqual(L.items['101'].state, 'failed', words);
    assert.match(L.items['101'].lastError, /stopped the batch 3 times at this record, so it is set aside/);
    assert.strictEqual(L.items['202'].state, 'done', words);
  }
});

test('X1: a stop cause nobody foresaw counts against its record - set aside after 3 Starts, and the next record is reached', async () => {
  const base = ancestrySite();
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN
      ? { ok: false, error: 'something nobody expected happened here' } : base(msg, tab)) });
  quick(p);
  for (let i = 1; i <= 2; i++) {
    await start(p, 'anc');
    const it = p.storage.ancBatchLedger.items['90001:1000001'];
    assert.deepStrictEqual([it.state, it.stops], ['queued', i], 'Start ' + i);
    assert.strictEqual(p.storage.ancBatchLedger.items['90002:2000001'].state, 'queued', 'Start ' + i);
  }
  await start(p, 'anc');
  const L = p.storage.ancBatchLedger;
  assert.strictEqual(L.items['90001:1000001'].state, 'failed');
  assert.match(L.items['90001:1000001'].lastError, /^something nobody expected happened here - it stopped the batch 3 times/);
  assert.strictEqual(L.items['90002:2000001'].state, 'done');
});

// ── PR #224 X1 correction: a 403 that says "sign in" is her session's, never a record's ──

test('X1 correction: every page image answering 403 "sign in" over four Starts sets nothing aside; once she signs in, all are saved', async () => {
  let signedIn = false;
  const q = npQueue();
  q.items['505'] = { id: 505, url: NP_C, pageId: 5550001, title: 'Hartley reunion', state: 'queued', tries: 0 };
  q.order.push('505');
  const p = await openPanel({ url: NP_B, storage: Object.assign({}, ON, { npBatchLedger: q }),
    respond: npPages({ [NP_C]: () => npInfo(npClip(505, 'Hartley reunion', NP_C)) })([]),
    fullPage: () => {
      if (signedIn) return {};
      // As lib/newspapers.js fullPage throws it for a 403 tile.
      throw new Error('Newspapers.com refused the page image (HTTP 403). Reload the page ' +
        '(its image pass lasts a day) and check you are signed in');
    } });
  quick(p);
  for (let i = 1; i <= 4; i++) {
    await start(p, 'np');
    const L = p.storage.npBatchLedger;
    for (const k of ['101', '202', '505']) {
      assert.deepStrictEqual([L.items[k].state, L.items[k].stops || 0], ['queued', 0], k + ', Start ' + i);
    }
    assert.match(p.batchEl('np-status').textContent, /^Stopped: .*signed in/, 'Start ' + i);
  }
  assert.strictEqual(p.written.length, 0);
  signedIn = true;
  await start(p, 'np');
  const L = p.storage.npBatchLedger;
  assert.deepStrictEqual(['101', '202', '505'].map((k) => L.items[k].state), ['done', 'done', 'done']);
  assert.strictEqual(p.written.length, 3);
});

test('X1 correction: a 403 for her own clippings list stops the run and counts nothing', async () => {
  const lists = [{ ok: false, error: 'Newspapers.com refused your clippings list (HTTP 403) - sign in and reload the page' }];
  // Her queue, its list not yet read to the end: the Start reads on.
  const r = await openPanel({ url: NP_A, respond: npSite(lists),
    storage: Object.assign({}, ON, { npBatchLedger: Object.assign(npQueue(), { listComplete: false }) }) });
  quick(r);
  await start(r, 'np');
  assert.match(r.batchEl('np-status').textContent, /^Stopped: could not read your clippings list: .*sign in/);
  assert.strictEqual(r.written.length, 0);
  const L = r.storage.npBatchLedger;
  assert.deepStrictEqual(['101', '202'].map((k) => [L.items[k].state, L.items[k].stops || 0]), [['queued', 0], ['queued', 0]]);
});

// Fifth audit Y1 (the auditor's r14): Newspapers.com coverage is per paper.
test('Y1: a clipping on a paper her subscription does not cover - the next one is saved, and it is set aside on the second Start', async () => {
  const uncovered = () => Object.assign(npInfo(npClip(101, 'Obituary', NP_A)), { ownClips: [],
    listError: 'Newspapers.com refused the list of clippings on this page (HTTP 403: you may need to sign in, ' +
      'or your subscription may not cover this paper)' });
  const p = await openPanel({ url: NP_B, respond: npPages({ [NP_A]: uncovered })([]),
    storage: Object.assign({}, ON, { npBatchLedger: npQueue() }) });
  quick(p);
  await start(p, 'np');
  let L = p.storage.npBatchLedger;
  assert.strictEqual(L.items['202'].state, 'done');
  assert.deepStrictEqual([L.items['101'].state, L.items['101'].stops], ['queued', 1]);
  assert.match(L.items['101'].lastError, /^Newspapers\.com would not give this clipping - your subscription may not cover this paper/);
  assert.deepStrictEqual(p.written.map((w) => p.capJson(w).url), [NP_B]);
  await start(p, 'np');
  L = p.storage.npBatchLedger;
  assert.strictEqual(L.items['101'].state, 'failed');
  assert.match(L.items['101'].lastError, /^Newspapers\.com would not give this clipping - your subscription may not cover this paper - it was refused on 2 Starts, so it is set aside/);
  assert.match(p.batchEl('np-status').textContent, /^Finished/);
});

// Fifth audit Y2 (the auditor's r15): offline, the tab shows the browser's own error page.
test('Y2: offline, a record page the add-on cannot read at all over three Starts sets nothing aside; once online, both are saved', async () => {
  let offline = true;
  const base = ancestrySite();
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    // On the browser's error page no content script answers.
    respond: (msg, tab) => (offline && tab.url !== HARRIET ? PORT_CLOSED : base(msg, tab)),
    titleFor: () => (offline ? 'www.ancestry.com' : 'A record - Ancestry') });
  quick(p);
  for (let i = 1; i <= 3; i++) {
    await start(p, 'anc');
    const it = p.storage.ancBatchLedger.items['90001:1000001'];
    assert.deepStrictEqual([it.state, it.stops || 0], ['queued', 0], 'Start ' + i);
  }
  offline = false;
  await start(p, 'anc');
  const L = p.storage.ancBatchLedger;
  assert.deepStrictEqual([L.items['90001:1000001'].state, L.items['90002:2000001'].state], ['done', 'done']);
});

// Sixth audit Z1 (the auditor's r16): signed out, with Seen It holding every
// other record - nothing asked of the site for those settles the probe.
test('Z1: signed out of FamilySearch, with every other record already held, two Starts count nothing; once signed in, all are saved', async () => {
  const SIGNED_OUT = { ok: false, error: 'FamilySearch asked you to sign in (HTTP 403). Sign in to FamilySearch in this tab, ' +
    'reload the page, then press Capture again' };
  // The ones not held are the fixture's records, so they can be saved once she signs in.
  const ids = ['HRT1-CEN1', 'HRT1-BBB1', 'HRT1-DTH1', 'HRT1-DDD1', 'HRT1-MAR1', 'HRT1-FFF1'];
  const held = { 'fsrec:HRT1-BBB1': 'captured', 'fsrec:HRT1-DDD1': 'captured', 'fsrec:HRT1-FFF1': 'captured' };
  let signedOut = true;
  const base = familysearchSite([]);
  const p = await openPanel({ url: ark('HRT1-CEN2'), seenIt: held, storage: Object.assign({}, ON, { fsBatchLedger: fsLedger(ids) }),
    respond: (msg, tab) => (signedOut && /^familysearch/.test(msg.action) && tab.url !== ark('HRT1-CEN2') ? SIGNED_OUT : base(msg, tab)) });
  quick(p);
  for (let i = 1; i <= 2; i++) {
    await start(p, 'fs');
    const L = p.storage.fsBatchLedger;
    for (const id of ['HRT1-CEN1', 'HRT1-DTH1', 'HRT1-MAR1']) {
      const it = L.items[id];
      assert.deepStrictEqual([it.state, it.stops || 0, !!it.ownRefusal], ['queued', 0, false], id + ', Start ' + i);
    }
    assert.match(p.batchEl('fs-status').textContent, /^Stopped: FamilySearch asked you to sign in/, 'Start ' + i);
  }
  signedOut = false;
  await start(p, 'fs');
  const L = p.storage.fsBatchLedger;
  assert.deepStrictEqual(ids.map((id) => L.items[id].state), ids.map(() => 'done'));
});

test('Z1: a probe passes only on a record the site answered - one Seen It holds is passed over, and the next is asked', async () => {
  // Every page image refused "...check you are signed in"; the clipping
  // between them is already in the archive, so nothing is asked of the site for it.
  let signedIn = false;
  const q = npQueue();
  q.items['505'] = { id: 505, url: NP_C, pageId: 5550001, title: 'Hartley reunion', state: 'queued', tries: 0 };
  q.order.push('505');
  const p = await openPanel({ url: NP_B, seenIt: { 'clip:202': 'captured' }, storage: Object.assign({}, ON, { npBatchLedger: q }),
    respond: npPages({ [NP_C]: () => npInfo(npClip(505, 'Hartley reunion', NP_C)) })([]),
    fullPage: () => {
      if (signedIn) return {};
      throw new Error('Newspapers.com refused the page image (HTTP 403). Reload the page ' +
        '(its image pass lasts a day) and check you are signed in');
    } });
  quick(p);
  for (let i = 1; i <= 2; i++) {
    await start(p, 'np');
    const L = p.storage.npBatchLedger;
    assert.strictEqual(L.items['202'].state, 'done', 'Start ' + i);
    for (const k of ['101', '505']) {
      assert.deepStrictEqual([L.items[k].state, L.items[k].stops || 0, !!L.items[k].ownRefusal], ['queued', 0, false], k + ', Start ' + i);
    }
    assert.match(p.batchEl('np-status').textContent, /^Stopped: .*signed in/, 'Start ' + i);
  }
  signedIn = true;
  await start(p, 'np');
  const L = p.storage.npBatchLedger;
  assert.deepStrictEqual(['101', '202', '505'].map((k) => L.items[k].state), ['done', 'done', 'done']);
});

// ── the one writer: the service worker (lib/batch-store.js) ─────────────────

const isOp = (name) => (msg) => msg.type === 'fha-batch' && (msg.ops || []).some((o) => o[0] === name);

test('one writer: the service worker stopped after counting a stop, before it answered - the stop counts once, and the next Start sets the clipping aside', async () => {
  const p = await openPanel({ url: NP_B, respond: npPages({ [NP_A]: () => NP_FAULT })([]),
    storage: Object.assign({}, ON, { npBatchLedger: npQueue() }) });
  quick(p);
  p.worker.stopAfter = isOp('markRefused');
  await start(p, 'np');
  assert.strictEqual(p.worker.restarts, 1);
  const L = p.storage.npBatchLedger;
  assert.deepStrictEqual([L.items['101'].state, L.items['101'].stops], ['queued', 1]);
  assert.match(p.batchEl('np-status').textContent, /^Stopped: Newspapers\.com: .*could not read this record/);
  // Sent again with its own id, and answered from what was stored.
  const asked = p.worker.delivered.filter(isOp('markRefused'));
  assert.deepStrictEqual([asked.length, asked[0].id === asked[1].id], [2, true]);
  await start(p, 'np');
  assert.deepStrictEqual([p.storage.npBatchLedger.items['101'].state, p.storage.npBatchLedger.items['202'].state],
    ['failed', 'done']);
});

test('one writer: the service worker restarted between two records - the run goes on, saves both, and lets its lease go', async () => {
  const p = await openPanel({ url: HARRIET, respond: ancestrySite(), storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }) });
  quick(p);
  p.worker.stopAfter = isOp('markDone');
  await start(p, 'anc');
  assert.strictEqual(p.worker.restarts, 1);
  const L = p.storage.ancBatchLedger;
  assert.deepStrictEqual([L.items['90001:1000001'].state, L.items['90002:2000001'].state], ['done', 'done']);
  assert.strictEqual(p.written.length, 2, 'a record was saved twice');
  assert.strictEqual(L.log.filter((l) => / done 90001:1000001 /.test(l)).length, 1, 'the save was noted twice');
  assert.match(p.batchEl('anc-status').textContent, /^Finished\. 2 done/);
  assert.strictEqual(p.storage.batchLease, undefined);
});

test('one writer: a service worker that does not answer stops the run plainly - nothing more is downloaded, and Start says why', async () => {
  const base = ancestrySite();
  let p = null;
  p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => {
      if (msg.action === 'ancestryImage') p.worker.down = true; // gone while Calvin's record is saved
      return base(msg, tab);
    } });
  quick(p);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
  assert.strictEqual(p.written.length, 1);
  assert.match(p.batchEl('anc-status').textContent,
    /^Stopped: the browser could not keep the batch’s progress \(the add-on’s background part did not answer: Could not establish connection\. Receiving end does not exist\.\), so nothing more was downloaded/);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [CALVIN]);
  assert.match(p.batchEl('anc-status').textContent, /^Did not start: the browser could not keep the batch’s progress/);
  // Back: the record saved but never noted is saved again (never lost), and the run goes on.
  p.worker.down = false;
  await start(p, 'anc');
  assert.deepStrictEqual(['90001:1000001', '90002:2000001'].map((k) => p.storage.ancBatchLedger.items[k].state), ['done', 'done']);
});

test('one writer: a panel closed mid-run lets the batch go at once - another window\'s Start is not refused', async () => {
  const sharedStore = JSON.parse(JSON.stringify(Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) })));
  const base = ancestrySite();
  let release;
  const gate = new Promise((r) => { release = r; });
  // Window A's run is held at Calvin's record when its panel is closed.
  const a = await openPanel({ url: HARRIET, sharedStore, respond: async (msg, tab) => {
    if (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN) await gate;
    return base(msg, tab);
  } });
  quick(a);
  const run = a.batchEl('anc-start').fire('click');
  try {
    await settle();
    assert.strictEqual(sharedStore.batchLease.site, 'ancestry');
    a.close();
    await settle();
    assert.strictEqual(sharedStore.batchLease, undefined, 'the closed panel\'s lease was kept');
    const b = await openPanel({ url: HARRIET, sharedStore, respond: base });
    quick(b);
    await start(b, 'anc');
    assert.match(b.batchEl('anc-status').textContent, /^Finished\. 2 done/);
  } finally {
    // (A closed panel runs nothing more; here its held run is let finish.)
    release();
    await a.batchEl('anc-pause').fire('click');
    await Promise.race([run, nap(3000)]);
  }
});

test('one writer: a window that is not running shows another window\'s change to its queue as it is made', async () => {
  const sharedStore = JSON.parse(JSON.stringify(ON));
  const a = await openPanel({ url: CALVIN, respond: ancestrySite(), sharedStore });
  const b = await openPanel({ url: CALVIN, respond: ancestrySite(), sharedStore });
  assert.match(b.batchEl('anc-status').textContent, /^Not running\. Nothing queued yet\./);
  await importGed(a, 'calvin.ged', gedFor('90001', '1000001', 'Calvin /Hartley/'));
  await settle();
  assert.match(b.batchEl('anc-status').textContent, /^Not running\. 0 done, 1 to go\./);
});

test('Codex 4505299727: a record saved by hand in another window while the batch was saving it stays done', async () => {
  const sharedStore = JSON.parse(JSON.stringify(Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) })));
  let release;
  const gate = new Promise((r) => { release = r; });
  const base = ancestrySite();
  // Window A's batch: Calvin's scan will not come, so it is saved in part.
  const a = await openPanel({ url: HARRIET, sharedStore, respond: async (msg, tab) => {
    if (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN) await gate;
    if (msg.action === 'ancestryImage') return { ok: false, transient: true, error: 'the scan stopped coming part way' };
    return base(msg, tab);
  } });
  quick(a);
  const run = a.batchEl('anc-start').fire('click');
  try {
    await settle();
    // Window B: Calvin by hand, whole.
    const b = await openPanel({ url: CALVIN, sharedStore, respond: base });
    await b.capture();
    await settle();
    assert.strictEqual(sharedStore.ancBatchLedger.items['90001:1000001'].note, 'saved by hand');
    release();
    await run;
    await settle();
    const it = sharedStore.ancBatchLedger.items['90001:1000001'];
    assert.deepStrictEqual([it.state, it.note], ['done', 'saved by hand']);
  } finally {
    release();
    await a.batchEl('anc-pause').fire('click');
    await Promise.race([run, nap(3000)]);
  }
});

test('Codex 4505299730: a first lease renewal that fails ends the run as any run ends - the lease is let go, and the next Start works', async () => {
  const p = await openPanel({ url: HARRIET, respond: ancestrySite(), storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN]) }) });
  quick(p);
  let leaseWrites = 0;
  // The claim is written; its first renewal is refused.
  p.setStorageFault((k) => (k === 'batchLease' && ++leaseWrites === 2 ? 'QUOTA_BYTES quota exceeded' : null));
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations, []);
  assert.match(p.batchEl('anc-status').textContent,
    /^Did not start: the browser could not save the batch’s progress \(QUOTA_BYTES quota exceeded\), so nothing was downloaded/);
  assert.strictEqual(p.storage.batchLease, undefined, 'the lease was kept');
  assert.strictEqual(p.batchEl('anc-start').disabled, false);
  p.setStorageFault(null);
  await start(p, 'anc');
  assert.match(p.batchEl('anc-status').textContent, /^Finished\. 1 done/);
});

test('PR #231 audit W1 (#224 F2 on the busy-page path): a "429" page met just after another window\'s slow-down is counted - the third in a row gets the long break', async () => {
  const L = ancLedger([CALVIN, ETHEL]);
  L.pacing = { sinceShort: 0, shortEvery: 40, rateEvents: 1, resumeReason: 'ratelimit', resumeAt: Date.now() - 1000 };
  const sharedStore = JSON.parse(JSON.stringify(Object.assign({}, ON, { ancBatchLedger: L })));
  const a = await openPanel({ url: HARRIET, sharedStore, respond: ancestrySite(),
    titleFor: (u) => (u === CALVIN ? '429 Too Many Requests' : 'Ancestry') });
  quick(a);
  const first = a.holdNextGet(a.tab.id);
  const run = a.batchEl('anc-start').fire('click');
  await settle();
  first();
  // A's tab has gone to Calvin; its "429" page is about to be read.
  const page = a.holdNextGet(a.tab.id);
  await settle();
  await nap(50);
  await settle();
  assert.deepStrictEqual(a.navigations.map((n) => n.url), [CALVIN]);
  // Window B: the second slow-down in a row.
  const b = await slowSourcesWindow('https://www.ancestry.com/family-tree/person/tree/1/person/2/facts', sharedStore, 'anc');
  await b.batchEl('anc-page').fire('click');
  await settle();
  assert.deepStrictEqual([sharedStore.ancBatchLedger.pacing.rateEvents, sharedStore.ancBatchLedger.pacing.resumeReason], [2, 'ratelimit']);
  page();
  await Promise.race([run, nap(3000)]);
  await settle();
  const p = sharedStore.ancBatchLedger.pacing;
  assert.match(a.batchEl('anc-status').textContent, /^Stopped: Ancestry answered with a busy or error page/);
  assert.deepStrictEqual([p.resumeReason, p.rateEvents], ['long', 0], 'the third slow-down in a row got ' + p.resumeReason);
  assert.ok(p.resumeAt > Date.now() + 3.5 * 3600 * 1000);
});

test('PR #231 audit W2: a record the batch saved in full stays done when a later Capture by hand of it misses its scan - the next Start never downloads it again', async () => {
  const L = ancLedger([CALVIN, ETHEL]);
  Object.assign(L.items['90001:1000001'], { state: 'done', bundle: 'calvin-1910-census-20261101', doneAt: '2026-11-01T00:00:00.000Z' });
  let image = { ok: false, transient: true, error: 'the scan stopped coming part way' };
  const base = ancestrySite();
  const p = await openPanel({ url: CALVIN, storage: Object.assign({}, ON, { ancBatchLedger: L }),
    respond: (msg, tab) => (msg.action === 'ancestryImage' && image ? image : base(msg, tab)) });
  quick(p);
  await p.capture();
  await settle();
  const it = p.storage.ancBatchLedger.items['90001:1000001'];
  assert.deepStrictEqual([it.state, it.bundle, 'imageKey' in it], ['done', 'calvin-1910-census-20261101', false]);
  image = null;
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [ETHEL]);
});

// ── a tree person's Ancestry gallery ─────────────────────────────────────────
// The owner: "batch-download a person's gallery too" - the photos, clippings
// and documents on the person's Gallery tab, each saved as Capture saves a
// shared item by hand, and never one the archive already holds.

const GALLERY = 'https://www.ancestry.com/family-tree/person/tree/90000001/person/90000002/gallery?galleryPage=1&tab=gallery';
const GAL_A = 'a1100000-1234-4abc-8def-0123456789ab';
const GAL_B = 'b2200000-1234-4abc-8def-0123456789ab';
const GAL_C = 'c3300000-1234-4abc-8def-0123456789ab';
const viewer = (guid) => 'https://www.ancestry.com/mediaui-viewer/collection/1030/tree/90000001/person/90000002/media/' + guid;
const FIXTURE_MEDIA = '7c0ffee0-1234-4abc-8def-0123456789ab'; // the photo in ancestry-media-photo.html

// Ancestry for a gallery's items, as content.js composes the lib: each item's
// viewer page is the shared-photo fixture, made that item's. `behind[guid]`:
// so many reads at Capture find Ancestry's Details still on the item shown
// before (Infinity: always). `away`: items whose address shows a page that is
// no photo at all (Ancestry sent the tab elsewhere). Records as ancestrySite.
function gallerySite({ behind = {}, away = [] } = {}) {
  const records = ancestrySite();
  const PANEL = sample('ancestry-media-panel');
  const left = Object.assign({}, behind);
  return (msg, tab) => {
    const media = anc.ancParseMediaPage(tab.url);
    if (msg.action === 'ancestryPersonGallery') {
      return BS.batchAncGallery(parse(fs.readFileSync(path.join(SAMPLES, 'ancestry-person-gallery.html'), 'utf8')), tab.url);
    }
    if (!media) return records(msg, tab);
    const guid = media.mediaId;
    if (away.includes(guid)) {
      return msg.action === 'prefill'
        ? { ok: true, prefill: { url: tab.url, title: 'Ancestry', people: [], recipeHint: 'ancestry', ancestryPhoto: false,
          ancestryRecord: false } }
        : { ok: false, error: 'unexpected ' + msg.action };
    }
    // The viewer's picture of this item, its alt text another item's title,
    // under Details that still describe that other item.
    const stale = PANEL.replace('<div class="viewerStage">', '<div class="viewerStage"><img id="' + guid +
      '" alt="Another item" src="https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/4242/media/' + guid +
      '?client=trees-mediaservice&amp;imageQuality=hq">');
    const html = (left[guid] || 0) > 0 ? stale : sample('ancestry-media-photo').split(FIXTURE_MEDIA).join(guid);
    const read = () => {
      const d = parse(html);
      const i = anc.ancMediaFrom(d, tab.url, { title: d.title, origin: 'https://www.ancestry.com' });
      if (i.ok) i.hints = anc.ancMediaHints(i.details);
      return i;
    };
    switch (msg.action) {
      case 'prefill': {
        const info = read();
        const ok = info.ok ? info : null;
        return { ok: true, prefill: { url: tab.url, title: ok ? ok.media.title : 'Ancestry', sourceDate: ok ? ok.hints.sourceDate || '' : null,
          people: ok ? (ok.details.linkedTo || []).slice() : [], repository: 'ancestry.com', recipeHint: 'ancestry',
          sourceType: (ok && ok.hints.sourceType) || 'photo', ancestryPhoto: true, ancestryMediaId: guid,
          ancestryPhotoShown: ok ? Object.keys(ok.details) : null,
          ancestryPhotoNotReady: !info.ok && info.transient ? info.error : null, ancestryRecord: false } };
      }
      case 'ancestryPhotoInfo': {
        const info = read();
        if ((left[guid] || 0) > 0) left[guid] -= 1;
        return info;
      }
      case 'ancestryPhoto':
        return { ok: true, base64: Buffer.from('fictional photo ' + guid).toString('base64'), ext: 'jpg', contentType: 'image/jpeg' };
      case 'pagehtml': return { ok: true, html };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

// A queue holding gallery items as the gallery button leaves them.
function galleryLedger(guids, L) {
  L = L || ancLedger([]);
  for (const g of guids) {
    const key = 'med:' + g;
    L.items[key] = { key, mediaId: g, url: viewer(g), persons: ['Thomas Hartley'], title: 'item ' + g.slice(0, 4),
      state: 'queued', tries: 0 };
    L.order.push(key);
  }
  return L;
}

test('a person\'s gallery: the button queues its photos and documents beside her records, and says when there is another page', async () => {
  const p = await openPanel({ url: GALLERY, respond: gallerySite(),
    storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN]) }) });
  await p.batchEl('anc-gallery').fire('click');
  await settle();
  const L = p.storage.ancBatchLedger;
  assert.deepStrictEqual(L.order, ['90001:1000001', 'med:' + GAL_A, 'med:' + GAL_B, 'med:' + GAL_C]);
  assert.deepStrictEqual(L.items['90001:1000001'], ancLedger([CALVIN]).items['90001:1000001']);
  assert.deepStrictEqual(L.items['med:' + GAL_A].persons, ['Thomas Hartley']);
  assert.strictEqual(L.items['med:' + GAL_A].title, 'Thomas Hartley at the Fairview farm');
  assert.strictEqual(L.items['med:' + GAL_C].url, 'https://www.ancestry.com/mediaui-viewer/tree/90000001/person/90000002/media/' + GAL_C);
  const said = p.batchEl('anc-status').textContent;
  assert.match(said, /^Thomas Hartley’s gallery, page 1: 3 found, 3 new to the queue\./);
  assert.match(said, /This gallery has another page: open it, then press “Add the photos and documents in this person’s gallery” again\.$/);
  // Nothing is followed on its own: the tab stays where she is.
  assert.deepStrictEqual(p.navigations, []);
  // On a person's Facts page, the gallery button queues nothing and says where to go.
  const facts = await openPanel({ url: 'https://www.ancestry.com/family-tree/person/tree/90000001/person/90000002/facts',
    respond: gallerySite(), storage: ON });
  await facts.batchEl('anc-gallery').fire('click');
  await settle();
  assert.ok(!facts.storage.ancBatchLedger || facts.storage.ancBatchLedger.order.length === 0);
  assert.match(facts.batchEl('anc-status').textContent, /^This is not a person’s Gallery page/);
});

test('a gallery item the archive holds is never fetched; the others are saved exactly as a Capture by hand saves them', async () => {
  const single = await openPanel({ url: viewer(GAL_B), respond: gallerySite() });
  await single.capture();
  assert.strictEqual(single.written.length, 1, single.result());
  const p = await openPanel({ url: CALVIN, respond: gallerySite(),
    seenIt: { ['med:' + GAL_A]: { state: 'filed', sid: 'S-0a1b2c3d4e' } },
    storage: Object.assign({}, ON, { ancBatchLedger: galleryLedger([GAL_A, GAL_B]) }) });
  quick(p);
  await start(p, 'anc');
  // Seen It was asked about the item by the key it knows it by; the held one was never opened.
  assert.deepStrictEqual(p.external[0].msg.keys, ['med:' + GAL_A]);
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [viewer(GAL_B)]);
  assert.strictEqual(p.written.length, 1, p.batchEl('anc-status').textContent);
  assert.deepStrictEqual(await snapshot(p.written[0]), await snapshot(single.written[0]));
  const cap = p.capJson(p.written[0]);
  assert.strictEqual(cap.url, 'https://www.ancestry.com/mediaui-viewer/collection/1030/tree/90000001/person/90000002/media/' + GAL_B);
  assert.strictEqual(cap.assets[0].role, 'front');
  const L = p.storage.ancBatchLedger;
  assert.deepStrictEqual([L.items['med:' + GAL_A].state, L.items['med:' + GAL_A].bundle],
    ['done', '(already in the archive, source S-0a1b2c3d4e)']);
  assert.deepStrictEqual([L.items['med:' + GAL_B].state, L.items['med:' + GAL_B].bundle], ['done', p.written[0].bundleName]);
  assert.match(p.batchEl('anc-status').textContent, /^Finished\. 2 done, 0 to go\./);
});

test('a gallery item whose details have not caught up is tried again - never saved without them', async () => {
  // Behind at the first try, caught up by the second: saved once, with its details.
  const p = await openPanel({ url: CALVIN, respond: gallerySite({ behind: { [GAL_A]: 1 } }),
    storage: Object.assign({}, ON, { ancBatchLedger: galleryLedger([GAL_A]) }) });
  quick(p);
  await start(p, 'anc');
  assert.strictEqual(p.written.length, 1, p.batchEl('anc-status').textContent);
  const cap = p.capJson(p.written[0]);
  assert.doesNotMatch(cap.notes || '', /left out/);
  assert.match(cap.notes, /Place: Fairview, Breton, Kansas, USA/);
  let L = p.storage.ancBatchLedger;
  assert.strictEqual(L.items['med:' + GAL_A].state, 'done');
  assert.ok(L.log.some((l) => /try again later med:a1100000.*have not caught up/.test(l)), L.log.join('\n'));
  // Never caught up: nothing saved, and set aside with the reason, after its tries.
  const q = await openPanel({ url: CALVIN, respond: gallerySite({ behind: { [GAL_A]: Infinity } }),
    storage: Object.assign({}, ON, { ancBatchLedger: galleryLedger([GAL_A]) }) });
  quick(q);
  await start(q, 'anc');
  assert.strictEqual(q.written.length, 0);
  assert.strictEqual(q.sent.filter((m) => m.action === 'ancestryPhoto').length, 0, 'nothing downloaded');
  L = q.storage.ancBatchLedger;
  assert.strictEqual(L.items['med:' + GAL_A].state, 'failed');
  assert.match(L.items['med:' + GAL_A].lastError, /have not caught up/);
});

test('a gallery item whose address shows no photo or document is set aside with the reason, and the run goes on', async () => {
  const p = await openPanel({ url: CALVIN, respond: gallerySite({ away: [GAL_A] }),
    storage: Object.assign({}, ON, { ancBatchLedger: galleryLedger([GAL_A, GAL_B]) }) });
  quick(p);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [viewer(GAL_A), viewer(GAL_B)]);
  assert.strictEqual(p.written.length, 1);
  const L = p.storage.ancBatchLedger;
  assert.strictEqual(L.items['med:' + GAL_A].state, 'failed');
  assert.match(L.items['med:' + GAL_A].lastError, /not a photo or document page/);
  assert.strictEqual(L.items['med:' + GAL_B].state, 'done');
});

test('a shared photo saved by hand joins the Ancestry queue as done, so a batch never downloads it again', async () => {
  const p = await openPanel({ url: viewer(GAL_A), respond: gallerySite(),
    storage: Object.assign({}, ON, { ancBatchLedger: galleryLedger([GAL_A, GAL_B]) }) });
  quick(p);
  await p.capture();
  await settle();
  assert.deepStrictEqual([p.storage.ancBatchLedger.items['med:' + GAL_A].state,
    p.storage.ancBatchLedger.items['med:' + GAL_A].note], ['done', 'saved by hand']);
  await start(p, 'anc');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [viewer(GAL_B)]);
  assert.strictEqual(p.written.length, 2);
});
