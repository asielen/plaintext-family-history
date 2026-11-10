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

const { openPanel, read, settle, tick, holdFirst, PORT_CLOSED } = require('./panel-harness.js');
const anc = require('../src/lib/ancestry-record.js');
const { parse, serialize } = require('./mini-dom.js');

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
      case 'singlefile': return { ok: true, html: '<html>copy of ' + tab.url + '</html>' };
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
  // A pointer that outlives the staging folder (ingest re-slugs it, filing
  // renames it): the record that holds the page, by key and address
  // (PR #213 audit #5). Never a path on this computer.
  assert.deepStrictEqual(data.record.same_image_as,
    { record: 'rec:90001:1000001', url: CALVIN, name: 'Calvin Hartley' });
  assert.strictEqual(data.record.imageSaved, false);
  assert.match(p.capJson(second).notes,
    /Same page \(image 4000123_00045\) as the capture of Calvin Hartley \(https:\/\/www\.ancestry\.com\/search\/collections\/90001\/records\/1000001\)/);
  assert.match(p.result(), /not downloaded again: it is already saved in the capture of Calvin Hartley/);
  assert.doesNotMatch(JSON.stringify(data), /\/dl\/|fha-inbox/);
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
  assert.deepStrictEqual(data.record.same_image_as, { where: 'archive' });
  assert.deepStrictEqual(p.external.map((e) => e.msg),
    [{ type: 'knownKeys', keys: ['img:90001:4000123_00045'] }]);
});

test('a page the archive holds is named by its source id when Seen It knows it (audit #5)', async () => {
  const p = await open(CALVIN, { seenIt: { 'img:90001:4000123_00045': { state: 'filed', sid: 'S-a1b2c3d4e5' } } });
  await p.capture();
  const data = await recordData(p.written[0]);
  assert.deepStrictEqual(data.record.same_image_as, { where: 'archive', source: 'S-a1b2c3d4e5' });
  assert.match(p.capJson(p.written[0]).notes, /as the archive, source S-a1b2c3d4e5; the image was not downloaded again/);
  assert.match(p.result(), /already saved in the archive, source S-a1b2c3d4e5\./);
});

test('a page waiting in the inbox (Seen It: captured) is named as such', async () => {
  const p = await open(CALVIN, { seenIt: { 'img:90001:4000123_00045': 'captured' } });
  await p.capture();
  assert.deepStrictEqual((await recordData(p.written[0])).record.same_image_as, { where: 'inbox' });
  assert.match(p.result(), /a capture waiting in the inbox to be filed/);
});

// ── PR #213 review: what a bundle says about its own scan ────────────────────

test('record-data.json says whether THIS capture holds the scan (Codex 4500176561, audit #1)', async () => {
  // imageId is only the image the record links to. imageSaved is what Seen
  // It's helper may count as "this page is saved".
  const good = await open(CALVIN);
  await good.capture();
  let rec = (await recordData(good.written[0])).record;
  assert.strictEqual(rec.imageId, '4000123_00045');
  assert.strictEqual(rec.imageSaved, true);

  const refused = await open(CALVIN, { image: { ok: false, error: 'Ancestry refused (HTTP 403)' } });
  await refused.capture();
  rec = (await recordData(refused.written[0])).record;
  assert.strictEqual(rec.imageId, '4000123_00045'); // still the link
  assert.strictEqual(rec.imageSaved, false);

  const shot = await open(CALVIN);
  dropFile(shot, 'screenshot.png');
  shot.$('f-provisional').checked = true;
  await shot.capture();
  assert.strictEqual((await recordData(shot.written[0])).record.imageSaved, false);

  const index = await open(ETHEL);
  await index.capture();
  assert.strictEqual((await recordData(index.written[0])).record.imageSaved, false);
});

// ── end to end: the panel's bundles, Seen It's helper, the next capture ──────
// The real chain behind "already saved": the panel stages a bundle, Seen It's
// helper (seen-it/host/seenit_host.py scan_known) reads the download folder,
// and its answer decides whether the next household member's capture
// downloads the page. Runs the helper with the Python on this computer; skipped
// (and says so) if there is none.

const { spawnSync } = require('node:child_process');
const os = require('node:os');

function findPython() {
  const tries = process.env.FHA_PYTHON ? [[process.env.FHA_PYTHON]]
    : [['py', '-3'], ['python3'], ['python']];
  for (const cmd of tries) {
    const r = spawnSync(cmd[0], cmd.slice(1).concat(['-c', 'import sys; print(sys.version_info[0])']), { encoding: 'utf8' });
    if (r.status === 0 && r.stdout.trim() === '3') return cmd;
  }
  return null;
}
const PYTHON = findPython();

// Write what the panel staged, as the browser's download would, and ask the helper.
async function helperAnswerFor(bundles) {
  const staging = fs.mkdtempSync(path.join(os.tmpdir(), 'fha-seenit-e2e-'));
  try {
    for (const spec of bundles) {
      const dir = path.join(staging, spec.bundleName);
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, 'page.html'), spec.pageHtml);
      for (const a of spec.assets) fs.writeFileSync(path.join(dir, a.filename), Buffer.from(await a.blob.arrayBuffer()));
      fs.writeFileSync(path.join(dir, 'capture.json'), JSON.stringify(spec.captureJson, null, 2));
    }
    const host = path.join(REPO, 'seen-it', 'host');
    const code = 'import json, sys; sys.path.insert(0, sys.argv[1]); import seenit_host as h; ' +
      'items, _ = h.scan_known({"captured": [sys.argv[2]]}); print(json.dumps(items))';
    const r = spawnSync(PYTHON[0], PYTHON.slice(1).concat(['-c', code, host, staging]), { encoding: 'utf8' });
    assert.strictEqual(r.status, 0, r.stderr);
    return JSON.parse(r.stdout);
  } finally {
    fs.rmSync(staging, { recursive: true, force: true });
  }
}

test('Seen It counts a census page as saved only after a capture really saved it (Codex 4500176561)',
  { skip: PYTHON ? false : 'no Python 3 on this computer to run seen-it/host/seenit_host.py' }, async () => {
    const scenarios = [
      ['a good capture', {}, null, true],
      ['a refused scan', { image: { ok: false, error: 'Ancestry refused (HTTP 403)' } }, null, false],
      ['a screen capture only', {}, 'screenshot.png', false],
    ];
    for (const [what, opts, shot, saved] of scenarios) {
      const first = await open(CALVIN, opts);
      if (shot) {
        dropFile(first, shot);
        first.$('f-provisional').checked = true;
      }
      await first.capture();
      const items = await helperAnswerFor(first.written);
      // The record itself is captured either way; its page only when saved.
      assert.strictEqual(items['rec:90001:1000001'].state, 'captured', what);
      assert.strictEqual('img:90001:4000123_00045' in items, saved, what);

      // Seen It answers the next capture from exactly what the helper found.
      const seenIt = {};
      for (const [k, v] of Object.entries(items)) seenIt[k] = v.sid ? { state: v.state, sid: v.sid } : v.state;
      const next = await open(HARRIET, { seenIt });
      await next.capture();
      assert.strictEqual(imageAsks(next).length, saved ? 0 : 1, what + ': the next person\'s page');
      assert.strictEqual(next.files(next.written[0]).includes('record.jpg'), !saved, what);

      // And the household member's bundle never claims the page for itself.
      if (saved) {
        const again = await helperAnswerFor([first.written[0], next.written[0]]);
        assert.ok('img:90001:4000123_00045' in again);
        const only = await helperAnswerFor([next.written[0]]);
        assert.ok(!('img:90001:4000123_00045' in only), 'a bundle pointing at another holds no page');
        assert.strictEqual(only['rec:90001:1000002'].state, 'captured');
      }
    }
  });

test('a record with no scan keeps the page copy even when it was unticked (audit #4)', async () => {
  // Filed alone, record-data.json would become the source's main file and
  // lose its role; the page copy is what the index is filed with.
  for (const [url, opts] of [[ETHEL, {}], [CALVIN, { image: { ok: false, error: 'no answer' } }]]) {
    const p = await open(url, opts);
    p.$('cb-pagecopy').checked = false;
    await p.capture();
    const cap = p.capJson(p.written[0]);
    assert.deepStrictEqual(cap.assets.map((a) => [a.file, a.role]),
      [['page-snapshot.html', 'webpage'], ['record-data.json', 'site-data']], url);
    assert.match(p.result(), /A copy of the web page was kept too/);
  }
  // With a scan, an unticked page copy stays off.
  const p = await open(CALVIN);
  p.$('cb-pagecopy').checked = false;
  await p.capture();
  assert.deepStrictEqual(p.files(p.written[0]), ['record.jpg', 'record-data.json']);
  assert.doesNotMatch(p.result(), /copy of the web page was kept/);
});

test('a household member pointing at another capture also keeps the page copy (audit #4)', async () => {
  const p = await open(CALVIN);
  p.$('cb-pagecopy').checked = false;
  await p.capture();
  await p.navigate(HARRIET);
  await p.capture();
  assert.deepStrictEqual(p.files(p.written[1]), ['page-snapshot.html', 'record-data.json']);
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

// ── PR #214 review sweep: the tab moves mid-capture; a recapture ────────────

async function untilHeld(h) {
  for (let i = 0; i < 200 && !h.held; i++) await tick();
  assert.ok(h.held, 'the held request was never made');
}

test('moving to the next record while the scan downloads: the bundle is all the record captured (#214 sweep)', async () => {
  const hold = holdFirst(ancestrySite(), 'ancestryImage');
  const p = await open(CALVIN, { respond: hold.respond });
  const pending = p.capture();
  await untilHeld(hold);
  await p.navigate(ETHEL);
  hold.release();
  await pending;
  await settle();
  assert.strictEqual(p.written.length, 1, p.result());
  const b = p.written[0];
  assert.strictEqual(b.captureJson.url, CALVIN);
  assert.match(await asset(b, 'page-snapshot.html').blob.text(), /records\/1000001/);
  assert.strictEqual((await recordData(b)).record.recordId, '1000001');
});

test('moving to another record while it is being read: nothing is saved (#214 sweep)', async () => {
  const hold = holdFirst(ancestrySite(), 'ancestryRecordInfo');
  const p = await open(CALVIN, { respond: hold.respond });
  const pending = p.capture();
  await untilHeld(hold);
  await p.navigate(ETHEL);
  hold.release();
  await pending;
  await settle();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /the page in the tab changed while this record was being saved, so nothing was saved/);
});

test('a recapture of the same record keeps her corrections (#214 sweep)', async () => {
  const p = await open(CALVIN, { image: { ok: false, error: 'Ancestry refused (HTTP 403)' } });
  p.$('f-title').value = 'Calvin Hartley, Fairview, 1880';
  await p.$('f-title').fire('input');
  p.$('f-date').value = '1880-06';
  await p.$('f-date').fire('input');
  await p.capture();
  dropFile(p, 'calvin-1880.jpg');
  await p.capture();
  assert.strictEqual(p.written.length, 2);
  assert.strictEqual(p.written[1].captureJson.title, 'Calvin Hartley, Fairview, 1880');
  assert.strictEqual(p.written[1].captureJson.source_date, '1880-06');
});

// ── PR #214, second Codex round (sweep) ──────────────────────────────────────

const failingSingleFile = (respond) => (msg, tab) =>
  (msg.action === 'singlefile' ? { ok: false, error: 'the page is too large to copy' } : respond(msg, tab));

test('a title she typed is filed as typed, even one ending like the tab\'s (#214 sweep)', async () => {
  for (const typed of ['Calvin Hartley census page - Ancestry.com', 'Calvin in 1880 | Ancestry']) {
    const p = await open(CALVIN);
    p.$('f-title').value = typed;
    await p.$('f-title').fire('input');
    await p.capture();
    assert.strictEqual(p.written[0].captureJson.title, typed);
  }
  const p = await open(CALVIN);   // untouched: the tab's title gives way
  await p.capture();
  assert.strictEqual(p.written[0].captureJson.title, 'Calvin Hartley - 1880 United States Federal Census');
});

test('page copy unticked and it cannot be made, but the scan is there: the record is still saved (#214 sweep)', async () => {
  const p = await open(CALVIN, { respond: failingSingleFile(ancestrySite()) });
  p.$('cb-pagecopy').checked = false;
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  assert.deepStrictEqual(p.files(p.written[0]), ['record.jpg', 'record-data.json']);
  assert.ok(!p.sent.some((m) => m.action === 'singlefile'));
});

test('an index entry needs its page copy: if it cannot be made, nothing half-filed is saved (#214 sweep)', async () => {
  const p = await open(ETHEL, { respond: failingSingleFile(ancestrySite()) });
  p.$('cb-pagecopy').checked = false;
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /the copy of the web page could not be made/);
});

test('PR #221 sweep (F8): the page going away while the scan downloads says "the page changed", not Chrome\'s port error', async () => {
  const site = ancestrySite();
  const hold = holdFirst((msg, tab) => (msg.action === 'ancestryImage' ? PORT_CLOSED : site(msg, tab)), 'ancestryImage');
  const p = await openPanel({ url: CALVIN, respond: hold.respond });
  const pending = p.capture();
  for (let i = 0; i < 200 && !hold.held; i++) await tick();
  assert.ok(hold.held);
  await p.navigate(ETHEL);
  hold.release();
  await pending;
  await settle();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /^Could not capture: the page in the tab changed while this record was being saved, so nothing was saved/);
  assert.doesNotMatch(p.result(), /message port/);
});

// ── a photo someone shared to their tree ─────────────────────────────────────
// The owner: "we should be able to download shared photos as sources". On a
// photo's viewer page Capture saves the photo and its Details as a bundle
// that files as a PHOTO source, under the address Seen It knows it by. The
// page is the synthetic viewer page tests/fixtures/capture-samples/
// ancestry-media-photo.html; the bundle it stages is the committed sample
// in browser-companion/test-bundle/, which tests/test_browser_companion.py
// files through `fha process`.

const MEDIA_ID = '7c0ffee0-1234-4abc-8def-0123456789ab';
const PHOTO = 'https://www.ancestry.com/mediaui-viewer/tree/90000001/person/90000002/media/' + MEDIA_ID;
const PHOTO_OPEN = PHOTO + '?destTreeId=90000003&destPersonId=90000004&hid=h1&usePUBJs=true';
const HINTS = 'https://www.ancestry.com/family-trees/90000001/people/90000002/hints';
const PHOTO_BYTES = Buffer.from('\xff\xd8\xff\xe0\x00\x10JFIF fictional shared photo\xff\xd9', 'latin1');
const SAMPLE_BUNDLE = path.join(REPO, 'browser-companion', 'test-bundle',
  'thomas-and-margaret-hartley-at-the-fairview-farm-about-1925-20261110-101500-k3m9px');
const PHOTO_FILE = 'ancestry-' + MEDIA_ID + '-photo.jpg';

// The content script on the photo's page, answering as content.js does, or
// on a person's Hints page. `photo` is how the photo fetch answers;
// `detailsAtFirst: false` is a Details panel she opened only after the panel
// first read the page.
function photoSite({ photo, detailsAtFirst = true, detailsNever = false, edit } = {}) {
  const raw = fs.readFileSync(path.join(SAMPLES, 'ancestry-media-photo.html'), 'utf8');
  const html = edit ? edit(raw) : raw; // `edit`: the page as some other photo shows it
  let reads = 0;
  return (msg, tab) => {
    const full = parse(html);
    const doc = detailsNever ? parse('<html><title>' + full.title + '</title><body>' +
      '<img src="https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/4242/media/' + MEDIA_ID +
      '?client=trees-mediaservice&amp;imageQuality=hq"></body></html>') : full;
    const media = anc.ancParseMediaPage(tab.url);
    const read = (d) => {
      const info = anc.ancMediaFrom(d, tab.url, { title: d.title, origin: 'https://www.ancestry.com' });
      if (info.ok) info.hints = anc.ancMediaHints(info.details);
      return info;
    };
    switch (msg.action) {
      case 'prefill': {
        reads += 1;
        const closed = !detailsAtFirst && reads === 1;
        const info = media ? read(closed ? parse('<html><title>' + doc.title + '</title></html>') : doc) : null;
        return {
          ok: true,
          prefill: {
            url: tab.url, title: info ? info.media.title : 'Hints for Thomas Hartley',
            sourceDate: info ? info.hints.sourceDate || '' : null,
            people: info ? (info.details.linkedTo || []).slice() : [],
            repository: 'ancestry.com', recipeHint: 'ancestry', sourceType: media ? 'photo' : undefined,
            ancestryPhoto: !!media, ancestryMediaId: media ? media.mediaId : null,
            ancestryPhotoShown: info ? Object.keys(info.details) : null,
            ancestryRecord: !!anc.ancParseRecordPage(tab.url),
          },
        };
      }
      case 'ancestryPhotoInfo':
        return read(doc);
      case 'ancestryPhoto':
        return photo || { ok: true, base64: PHOTO_BYTES.toString('base64'), ext: 'jpg', contentType: 'image/jpeg' };
      case 'pagehtml':
        // content.js serializePage: the page, without the "Saved by" list
        // (guarded, so a commit without the stripper fails its own assertion
        // about the list, never here).
        if (anc.ancMediaMemberNodes) for (const el of anc.ancMediaMemberNodes(doc)) el.parentNode.removeChild(el);
        return { ok: true, html: serialize(doc) };
      case 'singlefile': return { ok: true, html: '<html>copy of the shared photo page</html>\n' };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}
const openPhoto = (url, opts) => openPanel(Object.assign({ url, respond: photoSite(opts) }, opts || {}));
const photoAsks = (p) => p.sent.filter((m) => m.action === 'ancestryPhoto');

test('a shared photo\'s page offers "Capture this photo"; a record page and a Hints page do not', async () => {
  const p = await openPhoto(PHOTO_OPEN);
  assert.strictEqual(p.$('btn-capture').textContent, 'Capture this photo');
  assert.match(p.$('recipe-banner').textContent, /^Looks like a photo shared on Ancestry\./);
  assert.match(p.$('asset-status').textContent, /Record files: the photo \+ its details \(Ancestry, auto\)/);
  assert.strictEqual(p.$('f-type').value, 'photo');
  // Out of scope: the "Evaluate photo" panel on a Hints page keeps the page's
  // own address, so the panel cannot tell which photo is open there.
  const hints = await openPhoto(HINTS);
  assert.strictEqual(hints.$('btn-capture').textContent, 'Capture & save');
  assert.doesNotMatch(hints.$('asset-status').textContent, /the photo/);
  const rec = await open(CALVIN);
  assert.strictEqual(rec.$('btn-capture').textContent, 'Capture & save');
});

test('the people the photo is linked to are offered as people she can untick, never added behind her back', async () => {
  const p = await openPhoto(PHOTO_OPEN);
  assert.deepStrictEqual(p.$('people-list').children.map((l) => l.children[1].textContent),
    ['Thomas Hartley', 'Margaret Hartley']);
  // She unticks one: it is not filed with the photo.
  p.$('people-list').children[1].children[0].checked = false;
  await p.capture();
  assert.deepStrictEqual(p.capJson(p.written[0]).people, ['Thomas Hartley']);
});

test('round 3 (Codex PRRT_kwDOS6vm6c61wZYJ): Details opened after the panel read the page are shown before anything is saved', async () => {
  const late = await openPhoto(PHOTO_OPEN, { detailsAtFirst: false });
  assert.deepStrictEqual(late.$('people-list').children, []);
  // The first Capture reads the page again, finds the Details, and saves nothing.
  await late.capture();
  assert.strictEqual(late.written.length, 0);
  assert.strictEqual(late.result(), 'I found more on the page — check the people below, then press Capture this photo.');
  assert.match(late.resultClass(), /warn/);
  assert.deepStrictEqual(late.$('people-list').children.map((l) => [l.children[1].textContent, l.children[0].checked]),
    [['Thomas Hartley', true], ['Margaret Hartley', true]]);
  assert.strictEqual(late.$('f-date').value, '1925~');
  assert.strictEqual(photoAsks(late).length, 0, 'the photo is not fetched before she has checked');
  // She unticks one and presses Capture: one bundle, with what she saw.
  late.$('people-list').children[1].children[0].checked = false;
  await late.capture();
  assert.strictEqual(late.written.length, 1);
  const cap = late.capJson(late.written[0]);
  assert.deepStrictEqual(cap.people, ['Thomas Hartley']);
  assert.strictEqual(cap.source_date, '1925~');
  // No advice anywhere to capture the same photo again once it is staged.
  assert.doesNotMatch(late.result(), /again/i);
});

test('round 3 (audit C): a photo with nothing new on the page is staged once, with no advice to capture it again', async () => {
  const p = await openPhoto(PHOTO_OPEN);
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.doesNotMatch(p.result(), /again/i);
  // Details closed on the page: said BEFORE Capture, never after.
  const closed = await openPhoto(PHOTO_OPEN, { detailsAtFirst: false, detailsNever: true });
  assert.match(closed.$('asset-status').textContent, /Its Details are not open on the page: open them on Ancestry first/);
  await closed.capture();
  assert.strictEqual(closed.written.length, 1);
  assert.doesNotMatch(closed.result(), /again/i);
});

test('Capture saves the shared photo as a photo source: the photo as served, its details, the address to keep', async () => {
  const p = await openPhoto(PHOTO_OPEN);
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', PHOTO_FILE, 'photo-data.json']);
  const cap = p.capJson(b);
  // The viewer's own address without how she got there: Seen It's med: key.
  assert.strictEqual(cap.url, PHOTO);
  assert.strictEqual(cap.title, 'Thomas and Margaret Hartley at the Fairview farm, about 1925');
  assert.strictEqual(cap.source_type, 'photo');
  assert.strictEqual(cap.source_date, '1925~');
  assert.deepStrictEqual(cap.people, ['Thomas Hartley', 'Margaret Hartley']);
  assert.deepStrictEqual(cap.assets, [
    { file: 'page-snapshot.html', role: 'webpage', mode: 'singlefile' },
    { file: PHOTO_FILE, role: 'front', mode: 'ancestry-media', is_primary: true },
    { file: 'photo-data.json', role: 'site-data', mode: 'ancestry-media-data' },
  ]);
  // The photo as Ancestry sent it, never re-encoded: the page's bytes go
  // straight to bundle.js base64ToBlob (which the harness leaves as text).
  const photo = b.assets.find((a) => a.filename === PHOTO_FILE);
  assert.strictEqual(await photo.blob.text(), PHOTO_BYTES.toString('base64'));
  assert.strictEqual(photo.blob.type, 'image/jpeg');
  assert.deepStrictEqual(photoAsks(p), [{ action: 'ancestryPhoto', mediaId: MEDIA_ID }]);
  const data = JSON.parse(await b.assets.find((a) => a.filename === 'photo-data.json').blob.text());
  assert.deepStrictEqual(data.media, {
    mediaId: MEDIA_ID, treeId: '90000001', personId: '90000002', url: PHOTO,
    title: 'Thomas and Margaret Hartley at the Fairview farm, about 1925',
    imageUrl: 'https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/4242/media/' + MEDIA_ID +
      '?client=trees-mediaservice&imageQuality=hq',
    photoSaved: true,
  });
  assert.strictEqual(data.details.place, 'Fairview, Breton, Kansas, USA');
  // Who first added it is kept with Ancestry's data, not in the record's notes.
  assert.strictEqual(data.details.originallyAddedBy, 'hartley_family_tree on 3 Mar 2015');
  assert.strictEqual(cap.notes, 'A photo shared on Ancestry by another member. Its details on Ancestry:\n' +
    'Date: abt 1925\nPlace: Fairview, Breton, Kansas, USA\nLinked to: Thomas Hartley; Margaret Hartley\n\n' +
    'Description on Ancestry:\nThomas and Margaret on the porch of the farmhouse.\nThe dog is Shep.');
  // Other members who saved it are never copied out of the page - page.html included.
  const kept = JSON.stringify(cap) + JSON.stringify(data) + b.pageHtml;
  for (const never of ['cousin_member_one', 'cousin_member_two', 'Saved by']) {
    assert.ok(!kept.includes(never), 'copied out of the page: ' + never);
  }
  assert.ok(b.pageHtml.includes('Linked to'), 'the rest of the page is kept');
  assert.match(p.result(), /^Staged to \/dl\/fha-inbox\//);
  assert.doesNotMatch(p.resultClass(), /warn|error/);
});

test('the staged photo bundle is the committed sample that tests/test_browser_companion.py files', async () => {
  const p = await openPhoto(PHOTO_OPEN);
  await p.capture();
  const b = p.written[0];
  const sample = (name) => fs.readFileSync(path.join(SAMPLE_BUNDLE, name));
  const want = JSON.parse(sample('capture.json'));
  assert.deepStrictEqual(Object.assign({}, p.capJson(b), { accessed: want.accessed }), want);
  assert.strictEqual(b.pageHtml, sample('page.html').toString('utf8'));
  for (const a of b.assets) {
    // (The harness's base64ToBlob keeps the photo as its base64 text.)
    const got = a.filename === PHOTO_FILE ? Buffer.from(await a.blob.text(), 'base64')
      : Buffer.from(await a.blob.arrayBuffer());
    assert.ok(got.equals(sample(a.filename)), a.filename + ' differs from the sample');
  }
});

test('what she typed wins; a file she drops in is the photo, saved with its details', async () => {
  const p = await openPhoto(PHOTO_OPEN);
  p.$('f-title').value = 'Grandpa Tom and Grandma Maggie on the farm';
  await p.$('f-title').fire('input');
  p.$('f-date').value = '1926';
  dropFile(p, 'Hartley farm original.png');
  await p.capture();
  const b = p.written[0];
  const cap = p.capJson(b);
  assert.strictEqual(cap.title, 'Grandpa Tom and Grandma Maggie on the farm');
  assert.strictEqual(cap.source_date, '1926');
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'ancestry-' + MEDIA_ID + '-photo.png', 'photo-data.json']);
  assert.deepStrictEqual(cap.assets[1], { file: 'ancestry-' + MEDIA_ID + '-photo.png', role: 'front', mode: 'manual',
    is_primary: true });
  assert.strictEqual(photoAsks(p).length, 0, 'her file is used; Ancestry is not asked');
});

test('PR #258 review: no photo, no capture - a refused photo stages nothing, and says how to add it by hand', async () => {
  const p = await openPhoto(PHOTO_OPEN, { photo: { ok: false, error: 'Ancestry refused the photo (HTTP 403) - sign in ' +
    'on this page and press Capture again' } });
  await p.capture();
  // A photo source without its picture would file as the photo (Seen It
  // would show it saved), and the retry with her own file a second source.
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /^Could not capture: the photo could not be fetched from Ancestry: Ancestry refused the photo \(HTTP 403\)/);
  assert.match(p.result(), /Nothing was saved\. If you can download the photo yourself \(right-click it, Save image as\), drop that file into the panel and press Capture this photo$/);
  assert.match(p.resultClass(), /error/);
  // Her downloaded file, dropped in: one capture, with her file as the photo.
  dropFile(p, 'downloaded.jpg');
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.deepStrictEqual(p.files(p.written[0]), ['page-snapshot.html', PHOTO_FILE, 'photo-data.json']);
  assert.strictEqual(p.capJson(p.written[0]).assets[1].mode, 'manual');
});

// An undated photo, from the page to the inbox: the panel sends no date, and
// ingestion must not take one from the page - "Thomas Hartley 1840–1910" is
// a linked relative's life, not the photo's day (Codex PRRT_kwDOS6vm6c61wZYE).
test('round 3 (Codex PRRT_kwDOS6vm6c61wZYE): an undated shared photo reaches the inbox undated',
  { skip: PYTHON ? false : 'no Python 3 on this computer to run tools/capture.py' }, async () => {
    const undated = (h) => h.replace(/about 1925/g, 'on the porch')
      .replace('<div class="detail-row"><div><span>Date</span></div><div>abt 1925</div></div>', '');
    const p = await openPhoto(PHOTO_OPEN, { edit: undated });
    await p.capture();
    const b = p.written[0];
    assert.ok(!('source_date' in b.captureJson), 'the panel sent a date: ' + b.captureJson.source_date);
    const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'fha-undated-'));
    try {
      const dir = path.join(tmp, 'staging', b.bundleName);
      fs.mkdirSync(dir, { recursive: true });
      fs.writeFileSync(path.join(dir, 'page.html'), b.pageHtml);
      for (const a of b.assets) {
        const buf = a.filename === PHOTO_FILE ? Buffer.from(await a.blob.text(), 'base64')
          : Buffer.from(await a.blob.arrayBuffer());
        fs.writeFileSync(path.join(dir, a.filename), buf);
      }
      fs.writeFileSync(path.join(dir, 'capture.json'), JSON.stringify(b.captureJson, null, 2));
      fs.mkdirSync(path.join(tmp, 'archive'));
      fs.writeFileSync(path.join(tmp, 'archive', 'fha.yaml'), 'roots:\n  photos: photos\n  documents: documents\n');
      const code = 'import sys, io, contextlib, json; from pathlib import Path; sys.path.insert(0, sys.argv[1]); ' +
        'import capture; from _lib import load_fha_yaml, read_record; a = Path(sys.argv[2]); ' +
        'out = io.StringIO()\nwith contextlib.redirect_stdout(out):\n  capture.run_ingest(a, load_fha_yaml(a, strict=True), staging_dir=sys.argv[3])\n' +
        'm = read_record(next((a / "inbox").rglob("notes.md")))["meta"]; print(json.dumps({k: str(v) for k, v in m.items()}))';
      const r = spawnSync(PYTHON[0], PYTHON.slice(1).concat(['-c', code, path.join(REPO, 'tools'),
        path.join(tmp, 'archive'), path.join(tmp, 'staging')]), { encoding: 'utf8' });
      assert.strictEqual(r.status, 0, r.stderr);
      const meta = JSON.parse(r.stdout.trim().split('\n').pop());
      assert.strictEqual(meta.source_type, 'photo');
      assert.ok(!('source_date' in meta), 'filed with a date: ' + meta.source_date);
      assert.doesNotMatch(meta.citation, /1840|1910|2015/);
    } finally {
      fs.rmSync(tmp, { recursive: true, force: true });
    }
  });
