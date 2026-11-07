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

const { openPanel, settle } = require('./panel-harness.js');
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
  assert.strictEqual(L.items['90001:1000001'].state, 'done');
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
  assert.strictEqual(out.items['900000001'].state, 'done');
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
function familysearchSite(calls) {
  const fetch = async (url, init) => {
    calls.push({ url, auth: !!(init && init.headers && init.headers.Authorization) });
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

test('FamilySearch: each record saved with its data and image; the login went to FamilySearch only and is in no file', async () => {
  const calls = [];
  const L = { version: 1, items: {}, order: [], log: [] };
  for (const id of ['HRT1-CEN1', 'HRT1-DTH1']) {
    L.items[id] = { key: id, fsId: id, url: ark(id), persons: [], title: '', state: 'queued', tries: 0 };
    L.order.push(id);
  }
  const p = await openPanel({ url: ark('HRT1-CEN2'), respond: familysearchSite(calls),
    storage: Object.assign({}, ON, { fsBatchLedger: L }) });
  quick(p);
  await start(p, 'fs');
  assert.deepStrictEqual(p.navigations.map((n) => n.url), [ark('HRT1-CEN1'), ark('HRT1-DTH1')]);
  assert.strictEqual(p.written.length, 2, p.batchEl('fs-status').textContent);
  assert.ok(p.files(p.written[0]).includes('record-data.json'));
  assert.ok(calls.length > 0);
  for (const c of calls) {
    if (c.auth) assert.match(c.url, /^https:\/\/([a-z0-9-]+\.)*familysearch\.org\//, c.url);
  }
  for (const spec of p.written) {
    const all = [spec.pageHtml, JSON.stringify(spec.captureJson)];
    for (const a of spec.assets) all.push(await a.blob.text());
    assert.ok(!all.join('\n').includes(TOKEN), 'the login is in a saved file');
  }
  assert.ok(!JSON.stringify(p.storage).includes(TOKEN), 'the login is in storage');
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
