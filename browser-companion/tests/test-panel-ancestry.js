// The capture panel driven end to end on Ancestry record pages, without a
// browser (tests/panel-harness.js). The content script is played by the real
// readers in lib/ancestry-record.js run over the scrubbed fixture pages
// (tests/fixtures/capture-samples/ancestry-record-*.html), so what the panel
// stages is what the companion would stage from that page.
//
// What it guards, in the human's terms:
//   - Capture on a record page saves the record's index, its citation and its
//     scan, and files it as the census or death record it is (TI-066);
//   - what she typed in the form wins over what the record suggests;
//   - a record whose scan will not come still saves its index, and says so;
//   - the second person on the same census page does not download the page
//     again - in the same sitting, or when the archive already has it;
//   - nothing the Seen It add-on put on the page is saved as the record.
//
//   node --test browser-companion/tests/test-panel-ancestry.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');

const { openPanel, read } = require('./panel-harness.js');
const anc = require('../src/lib/ancestry-record.js');
const { parse } = require('./mini-dom.js');

const REPO = path.join(__dirname, '..', '..');
const SAMPLES = path.join(REPO, 'tests', 'fixtures', 'capture-samples');

const CALVIN = 'https://www.ancestry.com/search/collections/90001/records/1000001';
const HARRIET = 'https://www.ancestry.com/search/collections/90001/records/1000002';
const ETHEL = 'https://www.ancestry.com/search/collections/90002/records/2000001';
const PAGES = {
  [CALVIN]: 'ancestry-record-census',
  [HARRIET]: 'ancestry-record-census', // same household, same census page image
  [ETHEL]: 'ancestry-record-death-index',
};

// The content script for these pages. `image` is how the scan fetch answers.
function ancestrySite({ image } = {}) {
  return (msg, tab) => {
    const doc = parse(fs.readFileSync(path.join(SAMPLES, PAGES[tab.url] + '.html'), 'utf8'));
    switch (msg.action) {
      case 'prefill':
        return {
          ok: true,
          prefill: {
            url: tab.url, title: doc.title, people: [], recipeHint: 'ancestry',
            ancestryRecord: !!anc.ancParseRecordPage(tab.url),
          },
        };
      case 'ancestryRecordInfo': {
        // As content.js ancestryRecordInfo composes it.
        const info = anc.ancRecordFrom(doc, tab.url, { title: doc.title, origin: 'https://www.ancestry.com' });
        if (!info.ok) return info;
        info.hints = anc.ancHints(info.record, info.fields);
        info.title = anc.ancTitle(info.record, msg.formTitle);
        return info;
      }
      case 'ancestryImage':
        return image || { ok: true, base64: 'AAAA', ext: 'jpg', contentType: 'image/jpeg' };
      case 'pagehtml': return { ok: true, html: '<html>' + doc.title + '</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy</html>' };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

const open = (url, opts) => openPanel(Object.assign({ url, respond: ancestrySite(opts) }, opts || {}));
const asset = (spec, name) => spec.assets.find((a) => a.filename === name);
const recordData = async (spec) => JSON.parse(await asset(spec, 'record-data.json').blob.text());
const imageAsks = (p) => p.sent.filter((m) => m.action === 'ancestryImage');

// ── one record ───────────────────────────────────────────────────────────────

test('a census record saves its index, citation and scan, filed as a census', async () => {
  const p = await open(CALVIN);
  assert.match(p.$('asset-status').textContent, /the record’s index and citation \+ its scan/);
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'record.jpg', 'record-data.json']);
  const cap = p.capJson(b);
  assert.strictEqual(cap.url, CALVIN);
  assert.strictEqual(cap.title, 'Calvin Hartley - 1880 United States Federal Census');
  assert.strictEqual(cap.source_type, 'census');
  assert.strictEqual(cap.source_date, '1880');
  assert.ok(!('source_class' in cap));
  assert.strictEqual(cap.recipe_hint, 'ancestry');
  assert.deepStrictEqual(cap.assets.map((a) => [a.file, a.role, a.mode]), [
    ['page-snapshot.html', 'webpage', 'singlefile'],
    ['record.jpg', 'record', 'ancestry-api'],
    ['record-data.json', 'site-data', 'ancestry-page-data'],
  ]);
  assert.match(cap.notes, /Ancestry source citation: Year: 1880; Census Place: Fairview, Breton, Kansas/);
  assert.deepStrictEqual(imageAsks(p).map((m) => [m.dbId, m.imageId]), [['90001', '4000123_00045']]);
  assert.match(p.result(), /^Staged to \/dl\/fha-inbox\//);
});

test('record-data.json is the site\'s index, without the Seen It badges (TI-067)', async () => {
  const p = await open(CALVIN);
  await p.capture();
  const data = await recordData(p.written[0]);
  assert.deepStrictEqual(Object.keys(data), ['note', 'record', 'fields', 'household', 'citation']);
  assert.strictEqual(data.record.imageId, '4000123_00045');
  assert.deepStrictEqual(data.household[0][2], ['Harriet Hartley', '39', 'Wife']);
  const all = JSON.stringify(data);
  for (const badge of ['Captured, not filed', 'Seen 1×', 'Nothing here', 'Not in archive']) {
    assert.ok(!all.includes(badge), 'Seen It text in record-data.json: ' + badge);
  }
});

test('TI-066: a death-index entry files as a vital record, dated by the death, marked derivative', async () => {
  const p = await open(ETHEL);
  await p.capture();
  const b = p.written[0];
  const cap = p.capJson(b);
  assert.strictEqual(cap.title, 'Ethel Hartley - California, U.S., Death Index, 1940-1997');
  assert.strictEqual(cap.source_type, 'vital-record');
  assert.strictEqual(cap.source_date, '1957-01-07'); // not 1940, the index's first year
  assert.strictEqual(cap.source_class, 'derivative');
  // No scan on an index entry: nothing is fetched, and nothing is missing.
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'record-data.json']);
  assert.strictEqual(imageAsks(p).length, 0);
  assert.doesNotMatch(cap.notes, /image was not saved/);
});

test('what the human typed in the form wins over what the record suggests', async () => {
  const p = await open(ETHEL);
  p.$('f-title').value = 'Ethel Hartley death index entry';
  p.$('f-date').value = '1957';
  p.$('f-type').value = 'other';
  p.$('f-repo').value = 'Ancestry (library edition)';
  await p.capture();
  const cap = p.capJson(p.written[0]);
  assert.strictEqual(cap.title, 'Ethel Hartley death index entry');
  assert.strictEqual(cap.source_date, '1957');
  assert.strictEqual(cap.source_type, 'other');
  assert.strictEqual(cap.repository, 'Ancestry (library edition)');
  assert.strictEqual(cap.source_class, 'derivative'); // the form has no class box
});

test('a scan Ancestry will not hand over still saves the index, and says why', async () => {
  const p = await open(CALVIN, { image: { ok: false, error: 'Ancestry refused (HTTP 403) - sign in on this page' } });
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'record-data.json']);
  assert.match(p.capJson(b).notes, /The record’s image was not saved: Ancestry refused \(HTTP 403\)/);
  assert.match(p.result(), /The full record image was not saved: Ancestry refused \(HTTP 403\)/);
  assert.match(p.resultClass(), /warn/);
});

function dropFile(p, name) {
  const file = Object.assign(new Blob(['scan'], { type: 'image/jpeg' }), { name });
  return p.$('dropzone').listeners.drop[0]({ preventDefault() {}, dataTransfer: { files: [file] } });
}

test('an image she downloads and drops in is saved WITH the record\'s index, not instead of it', async () => {
  const p = await open(CALVIN, { image: { ok: false, error: 'Ancestry refused (HTTP 403)' } });
  await p.capture();
  assert.match(p.result(), /drop the file into the panel and press Capture again/);
  dropFile(p, 'calvin-1880.jpg');
  await p.capture();
  assert.strictEqual(p.written.length, 2);
  const b = p.written[1];
  assert.deepStrictEqual(p.capJson(b).assets.map((a) => [a.file, a.role, a.mode]), [
    ['page-snapshot.html', 'webpage', 'singlefile'],
    ['record.jpg', 'record', 'manual'],
    ['record-data.json', 'site-data', 'ancestry-page-data'],
  ]);
  assert.strictEqual(imageAsks(p).length, 1, 'her file is used; Ancestry is not asked again');
  assert.strictEqual(p.capJson(b).source_type, 'census');
});

test('a dropped screen capture is flagged, and does not stand in for the page for the next person', async () => {
  const p = await open(CALVIN);
  dropFile(p, 'screenshot.png');
  p.$('f-provisional').checked = true;
  await p.capture();
  const cap = p.capJson(p.written[0]);
  assert.deepStrictEqual(cap.assets.find((a) => a.role === 'record'),
    { file: 'record.png', role: 'record', mode: 'manual', provisional: true });
  assert.match(cap.notes, /provisional/i);
  await p.navigate(HARRIET);
  await p.capture();
  assert.strictEqual(imageAsks(p).length, 1, 'the real page is still fetched for the next person');
});

// ── one census page, a whole household ───────────────────────────────────────

test('the next person on the same census page does not download the page again', async () => {
  const p = await open(CALVIN);
  await p.capture();
  await p.navigate(HARRIET);
  await p.capture();
  assert.strictEqual(p.written.length, 2);
  assert.strictEqual(imageAsks(p).length, 1, 'the page image is fetched once');
  const second = p.written[1];
  assert.ok(!p.files(second).includes('record.jpg'));
  const data = await recordData(second);
  assert.strictEqual(data.record.recordId, '1000002');
  assert.strictEqual(data.record.same_image_as, 'the capture ' + p.written[0].bundleName);
  assert.match(p.capJson(second).notes, /Same page \(image 4000123_00045\) as the capture /);
  assert.match(p.result(), /not downloaded again: it is already saved in the capture /);
  // Only a folder name is kept, never a path on this computer.
  assert.doesNotMatch(data.record.same_image_as, /\/dl\//);
});

test('a page whose first capture lost its image is fetched for the next person', async () => {
  const p = await open(CALVIN, { image: { ok: false, error: 'no answer' } });
  await p.capture();
  await p.navigate(HARRIET);
  await p.capture();
  assert.strictEqual(imageAsks(p).length, 2);
});

test('a page the archive already holds (Seen It knows it) is not downloaded', async () => {
  const p = await open(CALVIN, { seenIt: { 'img:90001:4000123_00045': 'filed' } });
  await p.capture();
  assert.strictEqual(imageAsks(p).length, 0);
  const data = await recordData(p.written[0]);
  assert.strictEqual(data.record.same_image_as, 'the archive');
  assert.deepStrictEqual(p.external.map((e) => e.msg),
    [{ type: 'knownKeys', keys: ['img:90001:4000123_00045'] }]);
});

test('a page waiting in the inbox (Seen It: captured) is named as such', async () => {
  const p = await open(CALVIN, { seenIt: { 'img:90001:4000123_00045': 'captured' } });
  await p.capture();
  assert.strictEqual((await recordData(p.written[0])).record.same_image_as, 'a capture waiting to be filed');
});

test('without Seen It, or when it does not know the page, the page is downloaded', async () => {
  for (const seenIt of [undefined, {}, { 'img:90001:other': 'filed' }]) {
    const p = await open(CALVIN, { seenIt });
    await p.capture();
    assert.strictEqual(imageAsks(p).length, 1, JSON.stringify(seenIt));
    assert.ok(p.files(p.written[0]).includes('record.jpg'));
  }
});

test('the Seen It id the panel asks is the one its manifest key gives everyone', () => {
  // Chrome derives an unpacked extension's id from its manifest "key": the
  // first 32 hex digits of the key's SHA-256, written a-p.
  const key = JSON.parse(fs.readFileSync(path.join(REPO, 'seen-it', 'manifest.json'), 'utf8')).key;
  const hex = crypto.createHash('sha256').update(Buffer.from(key, 'base64')).digest('hex').slice(0, 32);
  const id = [...hex].map((c) => String.fromCharCode(97 + parseInt(c, 16))).join('');
  assert.ok(read('panel.js').includes("const SEEN_IT_ID = '" + id + "';"), 'panel.js should ask ' + id);
});

// ── the panel elsewhere is untouched ─────────────────────────────────────────

test('choosing "the page copy is the record" on a record page saves just the page copy', async () => {
  const p = await open(CALVIN);
  p.doc.radios.forEach((r) => { r.checked = r.value === 'none'; });
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.ok(!p.sent.some((m) => m.action === 'ancestryRecordInfo'));
  assert.deepStrictEqual(p.files(p.written[0]), ['page-snapshot.html']);
});
