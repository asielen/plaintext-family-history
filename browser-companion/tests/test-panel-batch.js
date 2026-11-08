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

const { openPanel, settle, makeLocks } = require('./panel-harness.js');
const anc = require('../src/lib/ancestry-record.js');
const fag = require('../src/lib/findagrave-record.js');
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
const BATCH_ACTIONS = ['newspapersList', 'ancestryPersonSources', 'familysearchPersonSources', 'findagraveLinks'];

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

// No waiting in tests: the gaps and settles are zero, the breaks stay real.
function quick(p) {
  const P = p.window.FHA.captureBatch.PACING;
  P.betweenMs = [0, 0];
  P.settleMs = [0, 0];
  P.listPauseMs = [0, 0];
  P.shortBreakMs = [0, 0];
  P.leaseSettleMs = 0;
  return P;
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
      case 'familysearchImage': return fsr.fsImage(msg.imageId, deps);
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

test('a record refused twice on two Starts is set aside, so it never holds up the queue (F13)', async () => {
  const base = ancestrySite();
  const p = await openPanel({ url: HARRIET, storage: Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) }),
    respond: (msg, tab) => (msg.action === 'ancestryRecordInfo' && tab.url === CALVIN
      ? { ok: false, error: 'Ancestry answered something nobody expected' } : base(msg, tab)) });
  quick(p);
  await start(p, 'anc');
  assert.match(p.batchEl('anc-status').textContent, /^Stopped: Ancestry answered something nobody expected/);
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

test('D1: a record whose address shows a busy page Start after Start is set aside, and the queue moves on', async () => {
  const p = await openPanel({ url: FAG_CALVIN, respond: fagSite(), storage: Object.assign({}, ON, { fagBatchLedger: fagLedger(2) }),
    titleFor: (u) => (/900000001$/.test(u) ? '503 Service Temporarily Unavailable' : 'A memorial - Find a Grave'),
    fetch: async (url) => ({ ok: true, status: 200, url, headers: { get: () => 'image/jpeg' },
      blob: async () => new Blob(['photo'], { type: 'image/jpeg' }) }) });
  quick(p);
  await start(p, 'fag');
  assert.strictEqual(p.storage.fagBatchLedger.items['900000001'].state, 'queued');
  p.storage.fagBatchLedger.pacing.resumeAt = 0;
  await start(p, 'fag');
  assert.strictEqual(p.storage.fagBatchLedger.items['900000001'].state, 'failed');
  assert.match(p.storage.fagBatchLedger.items['900000001'].lastError, /busy or error page/);
  p.storage.fagBatchLedger.pacing.resumeAt = 0;
  await start(p, 'fag');
  assert.deepStrictEqual(p.navigations.map((n) => n.url.replace(/^.*\//, '')), ['900000001', '900000001', '900000002']);
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

test('D3: FamilySearch having trouble (HTTP 500) stops the run without counting against the record', async () => {
  const calls = [];
  const p = await openPanel({ url: ark('HRT1-CEN2'), storage: Object.assign({}, ON,
    { fsBatchLedger: fsLedger(['HRT1-CEN1', 'HRT1-DTH1']) }),
  respond: familysearchSite(calls, (u) => (/\/ark:\/61903\/1:1:HRT1-CEN1$/.test(u) ? 500 : null)) });
  quick(p);
  await start(p, 'fs');
  await start(p, 'fs');
  await start(p, 'fs');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [ark('HRT1-CEN1'), ark('HRT1-CEN1'), ark('HRT1-CEN1')]);
  assert.strictEqual(p.storage.fsBatchLedger.items['HRT1-CEN1'].state, 'queued');
  assert.match(p.batchEl('fs-status').textContent, /^Stopped: FamilySearch is having trouble \(HTTP 500\)/);
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
  const locks = makeLocks();
  const a = await openPanel({ url: HARRIET, respond: ancestrySite(), sharedStore, locks });
  const b = await openPanel({ url: HARRIET, respond: ancestrySite(), sharedStore, locks });
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
  assert.strictEqual(locks.held.size, 0, 'the lock was let go');
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

test('Codex 4503786101: without Web Locks, two Starts together still stage each record once (a claim that must survive a settle)', async () => {
  const sharedStore = JSON.parse(JSON.stringify(Object.assign({}, ON, { ancBatchLedger: ancLedger([CALVIN, ETHEL]) })));
  const a = await openPanel({ url: HARRIET, respond: ancestrySite(), sharedStore });
  const b = await openPanel({ url: HARRIET, respond: ancestrySite(), sharedStore });
  quick(a).leaseSettleMs = 50;
  quick(b).leaseSettleMs = 50;
  await Promise.all([a.batchEl('anc-start').fire('click'), b.batchEl('anc-start').fire('click')]);
  await settle();
  const staged = a.written.concat(b.written).map((spec) => spec.captureJson.url).sort();
  assert.deepStrictEqual(staged, [CALVIN, ETHEL].sort());
  assert.strictEqual(a.navigations.length + b.navigations.length, 2);
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
  const locks = makeLocks();
  const a = await openPanel({ url: CALVIN, respond: ancestrySite(), sharedStore, locks });
  const b = await openPanel({ url: CALVIN, respond: ancestrySite(), sharedStore, locks });
  await Promise.all([importGed(a, 'calvin.ged', gedFor('90001', '1000001', 'Calvin /Hartley/')),
    importGed(b, 'ethel.ged', gedFor('90002', '2000001', 'Ethel /Hartley/'))]);
  await settle();
  const L = sharedStore.ancBatchLedger;
  assert.deepStrictEqual(Object.keys(L.items).sort(), ['90001:1000001', '90002:2000001']);
  assert.deepStrictEqual(L.order.slice().sort(), ['90001:1000001', '90002:2000001']);
  assert.ok(locks.granted.includes('fha-batch:ancBatchLedger'), 'the ledger lock was taken');
});

test('Codex 4504189301: a GEDCOM queued in one window while another notes a record saved by hand keeps both', async () => {
  const sharedStore = JSON.parse(JSON.stringify(ON));
  const locks = makeLocks();
  let manualB = null;
  const a = await openPanel({ url: CALVIN, respond: ancestrySite(), sharedStore, locks });
  const b = await openPanel({ url: CALVIN, respond: ancestrySite(), sharedStore, locks,
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

test('Codex 4504189301: without Web Locks, two imports overlapping in one window keep both records', async () => {
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
