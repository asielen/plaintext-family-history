// The capture panel driven end to end on a FamilySearch index entry whose
// image is at a partner archive (the "View on wvculture.org" badge), without
// a browser (tests/panel-harness.js). The content script is played by the
// real lib/familysearch-record.js over a scripted FamilySearch answering from
// tests/fixtures/capture-samples/familysearch-gedcomx-partner.json (fictional
// Hartleys, invented ids); the West Virginia Archives site is played by the
// view page recorded live (wvculture-view-live.html) and a stand-in picture.
//
// What it guards, in the human's terms:
//   - Capture on a West Virginia index entry also saves the death certificate
//     or register page behind its badge, beside FamilySearch's index, and the
//     citation says where the original is (film and image);
//   - before Capture, the panel says the image will come, and from where;
//   - the badge is followed with two plain requests to the state archive (its
//     view page, then the picture it shows), no cookies, no referrer, no
//     redirect, and her FamilySearch login goes nowhere near it;
//   - when the archive cannot give the image, FamilySearch's index is still
//     saved, and the notes and the message say the image did not come and
//     where it is;
//   - a badge to an archive the panel cannot read is never followed.
//
//   node --test browser-companion/tests/test-panel-familysearch-partner.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { openPanel } = require('./panel-harness.js');
const fsr = require('../src/lib/familysearch-record.js');

const SAMPLES = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'capture-samples');
const FIX = JSON.parse(fs.readFileSync(path.join(SAMPLES, 'familysearch-gedcomx-partner.json'), 'utf8'));
const VIEW_HTML = fs.readFileSync(path.join(SAMPLES, 'wvculture-view-live.html'), 'utf8');
const TOKEN = 'fs-session-SECRET-0000';

const ark = (id) => 'https://www.familysearch.org/ark:/61903/1:1:' + id;
const AMOS = ark('HRT1-WVD1');
const NORA = ark('HRT1-OTH1'); // a badge to an archive the panel does not read
const VIEW = 'https://archive.wvculture.org/vrr/va_view2.aspx?FilmNumber=1984148&ImageNumber=2530';
const IMG = 'https://images.wvculture.org/1984148/0002530.gif';
const BIG = 'x'.repeat(40 * 1024);

// FamilySearch's data service, scripted: the record's GEDCOM X only.
function familysearch() {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, auth: !!((init && init.headers) || {}).Authorization });
    const m = /\/ark:\/61903\/1:1:([A-Z0-9-]+)$/.exec(url);
    if (m && FIX.records[m[1]]) return { ok: true, status: 200, text: async () => JSON.stringify(FIX.records[m[1]]) };
    return { ok: false, status: 404, text: async () => '' };
  };
  return { calls, fetch };
}

// The content script on a record page, as content.js composes the lib.
function site(service) {
  return async (msg, tab) => {
    const deps = { fetch: service.fetch, base: tab.url, token: TOKEN, toBase64: async () => '' };
    switch (msg.action) {
      case 'prefill':
        return { ok: true, prefill: { url: tab.url, title: 'FamilySearch', people: [], recipeHint: 'familysearch',
          familysearchRecord: !!fsr.fsParseRecordPage(tab.url) } };
      case 'familysearchImageCheck': {
        const info = await fsr.fsRecordInfo(tab.url, deps);
        if (!info.ok) return { ok: false, error: info.error };
        const r = info.record;
        const check = await fsr.fsImageCheck(r.imageId, deps);
        return Object.assign({}, check, { record: true, fsId: r.fsId, memorialId: null,
          partner: r.partnerImage ? r.partnerImage.archive : null,
          title: [r.name, r.collection].filter(Boolean).join(' - ') || null });
      }
      case 'familysearchRecordInfo': {
        const info = await fsr.fsRecordInfo(tab.url, deps);
        if (info.ok) info.bundleTitle = fsr.fsTitle(info.record, msg.formTitle, '');
        return info;
      }
      case 'pagehtml': return { ok: true, html: '<html>FamilySearch page ' + tab.url + '</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy of ' + tab.url + '</html>' };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

// The West Virginia Archives site the panel reaches: `answer(url, init)`
// overrides; else the recorded view page and a full-size picture.
function wvSite(answer) {
  return async (url, init) => {
    const over = answer ? await answer(url, init) : null;
    if (over) return over;
    if (url === VIEW) {
      return { ok: true, status: 200, url, headers: { get: () => 'text/html; charset=utf-8' }, text: async () => VIEW_HTML };
    }
    if (url === IMG) {
      return { ok: true, status: 200, url, headers: { get: () => 'image/gif' }, blob: async () => new Blob([BIG], { type: 'image/gif' }) };
    }
    return { ok: false, status: 404, url, headers: { get: () => 'text/html' } };
  };
}

async function open(url, answer) {
  const service = familysearch();
  const p = await openPanel({ url, respond: site(service), fetch: wvSite(answer) });
  p.service = service;
  return p;
}
const asset = (spec, name) => spec.assets.find((a) => a.filename === name);
const recordData = async (spec) => JSON.parse(await asset(spec, 'record-data.json').blob.text());

test('a West Virginia index entry saves the image behind its badge beside FamilySearch\'s index', async () => {
  const p = await open(AMOS);
  assert.match(p.$('asset-status').textContent, /its image from West Virginia Archives and History/);
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const b = p.written[0];
  const cap = b.captureJson;
  assert.deepStrictEqual(cap.assets.map((a) => [a.file, a.role, a.mode]), [
    ['page-snapshot.html', 'webpage', 'singlefile'],
    ['record.gif', 'record', 'wvculture'],
    ['record-data.json', 'site-data', 'familysearch-record-data'],
  ]);
  assert.strictEqual(asset(b, 'record.gif').blob.size, BIG.length);
  // FamilySearch's citation, with where the original is as its second part.
  assert.strictEqual(cap.citation, '"West Virginia, Deaths, 1804-1999," database, FamilySearch ' +
    '(https://familysearch.org/ark:/61903/1:1:HRT1-WVD1), Amos Hartley, 06 Jan 1921; citing Logan, West Virginia; ' +
    'image at West Virginia Archives and History, film 1984148, image 2530.');
  assert.strictEqual(cap.recipe_hint, 'familysearch');
  assert.strictEqual(cap.url, AMOS);
  // The index stays beside the image: that is how the transcriber finds his line on a register page.
  const data = await recordData(b);
  assert.ok(Array.isArray(data.gedcomx.persons));
  assert.strictEqual(data.record.partnerImageUrl, VIEW);
  assert.deepStrictEqual(
    [data.record.partnerImage.filmNumber, data.record.partnerImage.imageNumber, data.record.partnerImage.imageUrl],
    ['1984148', '2530', IMG]);
});

test('the badge is followed with two plain requests to the state archive, and her login stays at FamilySearch', async () => {
  const p = await open(AMOS);
  await p.capture();
  assert.deepStrictEqual(p.fetched.map((f) => f.url), [VIEW, IMG]);
  for (const f of p.fetched) {
    assert.strictEqual(f.init.credentials, 'omit');
    assert.strictEqual(f.init.redirect, 'error');
    assert.strictEqual(f.init.referrerPolicy, 'no-referrer');
    assert.ok(!f.init.headers, 'no headers, so no login');
  }
  assert.ok(p.service.calls.every((c) => /^https:\/\/www\.familysearch\.org\//.test(c.url)));
  const b = p.written[0];
  const parts = [JSON.stringify(b.captureJson), b.pageHtml];
  for (const a of b.assets) parts.push(await a.blob.text());
  assert.ok(!parts.join('\n').includes(TOKEN));
});

for (const [what, answer, said] of [
  ['the archive refuses the page', async (u) => (u === VIEW ? { ok: false, status: 503, url: u, headers: { get: () => 'text/html' } } : null),
    /refused the page \(HTTP 503\)/],
  ['the page shows no image', async (u) => (u === VIEW ? { ok: true, status: 200, url: u, headers: { get: () => 'text/html' },
    text: async () => '<html><body><p>No image</p></body></html>' } : null), /shows no image the panel can save/],
  ['the picture is a placeholder', async (u) => (u === IMG ? { ok: true, status: 200, url: u, headers: { get: () => 'image/gif' },
    blob: async () => new Blob(['tiny'], { type: 'image/gif' }) } : null), /not the full image/],
  ['the site cannot be reached', async () => { throw new TypeError('Failed to fetch'); }, /could not be reached/],
]) {
  test('when ' + what + ', FamilySearch\'s index is still saved and the notes say where the image is', async () => {
    const p = await open(AMOS, answer);
    await p.capture();
    assert.strictEqual(p.written.length, 1, p.result());
    const cap = p.written[0].captureJson;
    assert.ok(!cap.assets.some((a) => a.role === 'record'), 'no image saved');
    assert.ok(cap.assets.some((a) => a.role === 'site-data'), 'the index is saved');
    // With no image, the page copy comes too, so the data is never the main file.
    assert.ok(cap.assets.some((a) => a.role === 'webpage'));
    assert.match(cap.notes, /Image not saved: the West Virginia Archives and History image could not be fetched: /);
    assert.match(cap.notes, said);
    assert.ok(cap.notes.includes('The image is at ' + VIEW + ' (image at West Virginia Archives and History, film 1984148, image 2530).'));
    // The citation is FamilySearch's alone: this source does not hold the image.
    assert.ok(!/image at West Virginia/.test(cap.citation));
    assert.match(p.result(), /could not be fetched/);
    assert.match(p.result(), /drop it into the panel and press Capture again/);
  });
}

test('a badge to an archive the panel cannot read is not followed; its address is kept in the record data', async () => {
  const p = await open(NORA);
  assert.match(p.$('asset-status').textContent, /no image/);
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  assert.strictEqual(p.fetched.length, 0);
  const data = await recordData(p.written[0]);
  assert.strictEqual(data.record.partnerImageUrl, 'https://records.example-state-archive.example/view?id=42');
  assert.strictEqual(data.record.partnerImage, undefined);
});
