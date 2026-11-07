// The capture panel driven end to end on FamilySearch pages, without a browser
// (tests/panel-harness.js). The content script is played by the real
// lib/familysearch-record.js over a scripted FamilySearch that answers from
// the fixture (tests/fixtures/capture-samples/familysearch-gedcomx.json:
// fictional Hartleys, invented ARKs), so what the panel stages is what the
// companion would stage from those answers.
//
// What it guards, in the human's terms:
//   - Capture on a record saves FamilySearch's own data and citation with the
//     image, titled and filed from the record's own data (a census as a
//     census, a death index by the death, not the index's first year);
//   - a restricted (center-only) image is said up front - the line turns
//     amber, "data only" - and the record still saves, with a note saying why;
//   - what she typed wins, and is what was in the form when she pressed
//     Capture; a new record starts fresh;
//   - the second person on the same image does not download it again;
//   - her FamilySearch login is in nothing the panel saves.
//
//   node --test browser-companion/tests/test-panel-familysearch.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { openPanel, settle } = require('./panel-harness.js');
const fsr = require('../src/lib/familysearch-record.js');

const FIX = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'tests', 'fixtures',
  'capture-samples', 'familysearch-gedcomx.json'), 'utf8'));
const TOKEN = 'fs-session-SECRET-0000';

const ark = (id) => 'https://www.familysearch.org/ark:/61903/1:1:' + id;
const CALVIN = ark('HRT1-CEN1');
const HARRIET = ark('HRT1-CEN2');
const ETHEL = ark('HRT1-DTH1');
const RESTRICTED = ark('HRT1-RST1'); // Calvin's record, pointing at a restricted image
const VIEWER = 'https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG1';
const FROM_RECORD = VIEWER + '?view=index&personArk=%2Fark%3A%2F61903%2F1%3A1%3AHRT1-CEN1&action=view';

// A record whose image is restricted: the census record, re-pointed at IMG2.
FIX.records['HRT1-RST1'] = JSON.parse(JSON.stringify(FIX.records['HRT1-CEN1'])
  .replace(/1:1:HRT1-CEN1/g, '1:1:HRT1-RST1').replace(/3:1:HRT1-IMG1/g, '3:1:HRT1-IMG2'));

// FamilySearch, scripted. `over`: answers to replace, by part of the address
// (a status number, or a function returning a promise to hold one back).
function familysearch(over = {}) {
  const calls = [];
  const fetch = async (url, init) => {
    const h = (init && init.headers) || {};
    calls.push({ url, auth: !!h.Authorization, range: !!h.Range });
    const k = Object.keys(over).find((x) => url.indexOf(x) >= 0);
    if (k) {
      const v = typeof over[k] === 'function' ? await over[k]() : over[k];
      if (typeof v === 'number') return { ok: false, status: v, text: async () => '' };
      if (v) return v;
    }
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
  return { calls, fetch };
}

// Hold back the FIRST answer for an address until release(); later ones answer
// normally (a capture asks again, and must not wait forever).
function holdOnce() {
  const h = { release: null, used: false };
  h.answer = () => {
    if (h.used) return null;
    h.used = true;
    return new Promise((r) => { h.release = () => r(null); });
  };
  return h;
}

// The content script on these pages, as content.js composes the lib.
function site(service) {
  return async (msg, tab) => {
    const deps = { fetch: service.fetch, base: tab.url, token: TOKEN,
      toBase64: async (b) => Buffer.from(await b.arrayBuffer()).toString('base64') };
    const rec = fsr.fsParseRecordPage(tab.url);
    switch (msg.action) {
      case 'prefill':
        return {
          ok: true,
          prefill: {
            url: tab.url, title: 'Calvin Hartley • United States Census, 1880 • FamilySearch', people: [],
            recipeHint: 'familysearch', familysearchRecord: !!rec,
            familysearchImageViewer: !rec && !!fsr.fsImageIdFromHref(tab.url),
            familysearchImageId: rec ? null : fsr.fsImageIdFromHref(tab.url),
          },
        };
      case 'familysearchImageCheck': {
        if (rec) {
          const info = await fsr.fsRecordInfo(tab.url, deps);
          if (!info.ok) return { ok: false, error: info.error };
          const check = await fsr.fsImageCheck(info.record.imageId, deps);
          return Object.assign({}, check, { record: true, fsId: info.record.fsId,
            title: [info.record.name, info.record.collection].filter(Boolean).join(' - ') || null });
        }
        return Object.assign({ record: false }, await fsr.fsImageCheck(fsr.fsImageIdFromHref(tab.url), deps));
      }
      case 'familysearchRecordInfo': {
        const info = await fsr.fsRecordInfo(tab.url, deps);
        if (info.ok) info.bundleTitle = fsr.fsTitle(info.record, msg.formTitle, '');
        return info;
      }
      case 'familysearchImage':
        return fsr.fsImage(msg.imageId || fsr.fsImageIdFromHref(tab.url), deps);
      case 'pagehtml': return { ok: true, html: '<html>FamilySearch page</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy</html>' };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

async function open(url, opts = {}) {
  const service = opts.service || familysearch(opts.over);
  const p = await openPanel({ url, respond: site(service), seenIt: opts.seenIt });
  p.service = service;
  return p;
}
const asset = (spec, name) => spec.assets.find((a) => a.filename === name);
const recordData = async (spec) => JSON.parse(await asset(spec, 'record-data.json').blob.text());
// The full image downloads (not the 1 KB "can it be saved?" checks).
const fullImageFetches = (p) => p.service.calls.filter((c) => /dist\.jpg/.test(c.url) && !c.range).length;
async function everything(spec) {
  const parts = [JSON.stringify(spec.captureJson), spec.pageHtml];
  for (const a of spec.assets) parts.push(await a.blob.text());
  return parts.join('\n');
}

// ── one record ───────────────────────────────────────────────────────────────

test('a census record saves FamilySearch\'s data, citation and image, titled and filed from its own data', async () => {
  const p = await open(CALVIN);
  assert.strictEqual(p.$('f-title').value, 'Calvin Hartley - United States, Census, 1880', 'FamilySearch\'s own name');
  assert.match(p.$('asset-status').textContent, /its image HRT1-IMG1 \(FamilySearch, auto\)/);
  assert.match(p.$('asset-status').className, /ok/);
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const b = p.written[0];
  assert.deepStrictEqual(b.captureJson.assets.map((a) => [a.file, a.role, a.mode]), [
    ['page-snapshot.html', 'webpage', 'singlefile'],
    ['record.jpg', 'record', 'familysearch-api'],
    ['record-data.json', 'site-data', 'familysearch-record-data'],
  ]);
  const cap = b.captureJson;
  assert.strictEqual(cap.url, CALVIN);
  assert.strictEqual(cap.title, 'Calvin Hartley - United States, Census, 1880');
  assert.strictEqual(cap.source_type, 'census');
  assert.strictEqual(cap.source_date, '1880');
  assert.strictEqual(cap.repository, 'FamilySearch');
  assert.strictEqual(cap.recipe_hint, 'familysearch');
  assert.match(cap.notes, /FamilySearch citation: "United States, Census, 1880," database with images/);
  const data = await recordData(b);
  assert.deepStrictEqual(Object.keys(data), ['note', 'record', 'citation', 'title', 'gedcomx']);
  assert.strictEqual(data.record.imageId, 'HRT1-IMG1');
  assert.strictEqual(data.record.imageSaved, true);
  assert.ok(Array.isArray(data.gedcomx.persons));
});

test('her FamilySearch login is in nothing the capture saves', async () => {
  const p = await open(CALVIN);
  await p.capture();
  assert.ok(!(await everything(p.written[0])).includes(TOKEN));
  // ...and it went only to FamilySearch.
  assert.ok(p.service.calls.every((c) => !c.auth || /^https:\/\/www\.familysearch\.org\//.test(c.url)));
});

test('TI-066: a death-index entry files as a vital record dated by the death, marked derivative', async () => {
  const p = await open(ETHEL);
  assert.match(p.$('asset-status').textContent, /no image: there is no image for it on FamilySearch/);
  await p.capture();
  const cap = p.written[0].captureJson;
  assert.strictEqual(cap.title, 'Ethel Hartley - California Death Index, 1940-1997');
  assert.strictEqual(cap.source_type, 'vital-record');
  assert.strictEqual(cap.source_date, '1957-01-07'); // not 1940
  assert.strictEqual(cap.source_class, 'derivative');
  assert.strictEqual((await recordData(p.written[0])).record.imageSaved, false);
});

test('a record with no image keeps the page copy even when it was unticked', async () => {
  const p = await open(ETHEL);
  p.$('cb-pagecopy').checked = false;
  await p.capture();
  assert.deepStrictEqual(p.written[0].captureJson.assets.map((a) => [a.file, a.role]),
    [['page-snapshot.html', 'webpage'], ['record-data.json', 'site-data']]);
  assert.match(p.result(), /A copy of the web page was kept too/);
});

// ── a restricted image ───────────────────────────────────────────────────────

test('a restricted image is said up front - amber, "data only" - and the record still saves, with a note', async () => {
  const p = await open(RESTRICTED);
  const status = p.$('asset-status');
  assert.match(status.textContent, /the record’s data only \(FamilySearch\)\. Its image HRT1-IMG2 can’t be saved from here - it is restricted: FamilySearch shows it only at a FamilySearch center or affiliate library/);
  assert.match(status.className, /warn/);
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  const b = p.written[0];
  assert.deepStrictEqual(b.captureJson.assets.map((a) => a.role), ['webpage', 'site-data']);
  assert.match(b.captureJson.notes,
    /Image not saved: FamilySearch image HRT1-IMG2 \(https:\/\/www\.familysearch\.org\/ark:\/61903\/3:1:HRT1-IMG2\) could not be downloaded - FamilySearch offers no download of this image: it is restricted to a FamilySearch center or affiliate library\. Checked \d{4}-\d{2}-\d{2}\./);
  assert.strictEqual((await recordData(b)).record.imageSaved, false);
  assert.match(p.result(), /The full record image was not saved: FamilySearch offers no download/);
});

test('signed in but refused the image (HTTP 403): restricted, not "sign in"', async () => {
  const p = await open(CALVIN, { over: { 'dist.jpg': 403 } });
  assert.match(p.$('asset-status').textContent, /HTTP 403\): it is restricted to a FamilySearch center/);
  await p.capture();
  assert.match(p.result(), /restricted to a FamilySearch center or affiliate library/);
  assert.deepStrictEqual(p.written[0].captureJson.assets.map((a) => a.role), ['webpage', 'site-data']);
});

test('refused the record itself: the true cause and what to do, and nothing half-saved', async () => {
  for (const [status, want] of [[401, /asked you to sign in \(HTTP 401\)/], [429, /slow down \(HTTP 429\)/]]) {
    const p = await open(CALVIN, { over: { '1:1:': status } });
    await p.capture();
    assert.strictEqual(p.written.length, 0);
    assert.match(p.result(), want);
    assert.match(p.result(), /^Could not capture: /);
  }
});

// ── the form ─────────────────────────────────────────────────────────────────

test('what she typed wins over FamilySearch\'s data, even typed before the data arrived', async () => {
  const hold = holdOnce();
  const p = await open(CALVIN, { over: { '3:1:HRT1-IMG1': hold.answer } });
  // The image check is still out: she types a title meanwhile.
  p.$('f-title').value = 'Calvin in 1880';
  await p.$('f-title').fire('input');
  hold.release();
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Calvin in 1880', 'the late answer did not overwrite her title');
  p.$('f-date').value = '1880-06';
  p.$('f-type').value = 'other';
  p.$('f-repo').value = 'FamilySearch (library)';
  await p.$('f-repo').fire('input');
  await p.capture();
  const cap = p.written[0].captureJson;
  assert.strictEqual(cap.title, 'Calvin in 1880');
  assert.strictEqual(cap.source_date, '1880-06');
  assert.strictEqual(cap.source_type, 'other');
  assert.strictEqual(cap.repository, 'FamilySearch (library)');
});

test('what is saved is the form as it stood when Capture was pressed', async () => {
  const p = await open(CALVIN);
  // Hold the record's data back during the capture and change the form meanwhile.
  const hold = holdOnce();
  const held = familysearch({ '1:1:HRT1-CEN1': hold.answer });
  p.service.fetch = held.fetch;
  const pending = p.$('btn-capture').fire('click');
  await settle();
  p.$('f-title').value = 'typed after Capture';
  await p.$('f-title').fire('input');
  p.$('f-notes').value = 'a note typed after Capture';
  hold.release();
  await pending;
  await settle();
  assert.strictEqual(p.written.length, 1, p.result());
  assert.strictEqual(p.written[0].captureJson.title, 'Calvin Hartley - United States, Census, 1880');
  assert.doesNotMatch(p.written[0].captureJson.notes, /typed after Capture/);
});

test('a new record starts fresh: its own FamilySearch title, her old correction gone', async () => {
  const p = await open(CALVIN);
  p.$('f-title').value = 'Calvin in 1880';
  await p.$('f-title').fire('input');
  await p.navigate(ETHEL);
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Ethel Hartley - California Death Index, 1940-1997');
});

test('a late answer for the record the tab has left is dropped', async () => {
  const hold = holdOnce();
  const service = familysearch({ '1:1:HRT1-CEN1': hold.answer });
  const p = await open(CALVIN, { service });
  await p.navigate(ETHEL);
  await settle();
  hold.release(); // Calvin's answer arrives after Ethel's page was read
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Ethel Hartley - California Death Index, 1940-1997');
  assert.match(p.$('asset-status').textContent, /no image/);
});

// ── one image, a whole household ─────────────────────────────────────────────

test('the next person on the same image does not download it again; the bundle points at the record holding it', async () => {
  const p = await open(CALVIN);
  await p.capture();
  await p.navigate(HARRIET);
  await settle();
  await p.capture();
  assert.strictEqual(p.written.length, 2);
  assert.strictEqual(fullImageFetches(p), 1, 'the image is downloaded once');
  assert.strictEqual(p.written.filter((b) => b.assets.some((a) => a.filename === 'record.jpg')).length, 1);
  const data = await recordData(p.written[1]);
  assert.deepStrictEqual(data.record.same_image_as, { record: 'fsrec:HRT1-CEN1', url: CALVIN, name: 'Calvin Hartley' });
  assert.strictEqual(data.record.imageSaved, false);
  assert.match(p.written[1].captureJson.notes, /Same image \(HRT1-IMG1\) as the capture of Calvin Hartley/);
  assert.deepStrictEqual(p.written[1].captureJson.assets.map((a) => a.role), ['webpage', 'site-data']);
});

test('an image the archive holds (Seen It knows it, with its source) is not downloaded', async () => {
  const p = await open(CALVIN, { seenIt: { 'fsimg:HRT1-IMG1': { state: 'filed', sid: 'S-a1b2c3d4e5' } } });
  await p.capture();
  assert.ok(!p.files(p.written[0]).includes('record.jpg'));
  assert.deepStrictEqual((await recordData(p.written[0])).record.same_image_as, { where: 'archive', source: 'S-a1b2c3d4e5' });
  assert.deepStrictEqual(p.external.map((e) => e.msg), [{ type: 'knownKeys', keys: ['fsimg:HRT1-IMG1'] }]);
  assert.match(p.result(), /already saved in the archive, source S-a1b2c3d4e5/);
});

test('a screen capture dropped in is flagged, and never stands in for the image for the next person', async () => {
  const p = await open(RESTRICTED);
  const file = Object.assign(new Blob(['shot'], { type: 'image/png' }), { name: 'screenshot.png' });
  p.$('dropzone').listeners.drop[0]({ preventDefault() {}, dataTransfer: { files: [file] } });
  p.$('f-provisional').checked = true;
  await p.capture();
  const cap = p.written[0].captureJson;
  assert.deepStrictEqual(cap.assets.find((a) => a.role === 'record'),
    { file: 'record.png', role: 'record', mode: 'manual', provisional: true });
  assert.strictEqual((await recordData(p.written[0])).record.imageSaved, false);
});

// ── the image viewer ─────────────────────────────────────────────────────────

test('the image viewer opened from a record is captured as that record', async () => {
  const p = await open(FROM_RECORD);
  await p.capture();
  const b = p.written[0];
  assert.strictEqual(b.captureJson.url, CALVIN);
  assert.strictEqual(b.captureJson.source_type, 'census');
  assert.ok(p.files(b).includes('record-data.json'));
  assert.ok(p.files(b).includes('record.jpg'));
});

test('a bare image viewer saves the full image from FamilySearch\'s API, not the tile on screen', async () => {
  const p = await open(VIEWER);
  assert.match(p.$('asset-status').textContent, /Record file: full image HRT1-IMG1 \(FamilySearch, auto\)/);
  await p.capture();
  const b = p.written[0];
  assert.deepStrictEqual(b.captureJson.assets.map((a) => [a.file, a.role, a.mode]),
    [['page-snapshot.html', 'webpage', 'singlefile'], ['record.jpg', 'record', 'familysearch-api']]);
  assert.ok(p.service.calls.some((c) => c.url === 'https://www.familysearch.org/das/v2/HRT1-IMG1/dist.jpg' && c.auth));
});

test('a restricted bare image says so up front, and Capture keeps the page copy with the reason', async () => {
  const p = await open('https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG2');
  assert.match(p.$('asset-status').textContent, /This image can’t be saved from here - it is restricted/);
  assert.match(p.$('asset-status').className, /warn/);
  await p.capture();
  assert.deepStrictEqual(p.files(p.written[0]), ['page-snapshot.html']);
  assert.match(p.result(), /restricted to a FamilySearch center or affiliate library/);
});
