// The capture panel driven end to end on a Find a Grave memorial, without a
// browser (tests/panel-harness.js). The content script is played by the real
// readers in lib/findagrave-record.js over the scrubbed fixture pages
// (tests/fixtures/capture-samples/findagrave-memorial*.html: invented
// memorials of the fictional Hartleys), and Find a Grave's image host by a
// stand-in that records every request.
//
// What it guards, in the human's terms:
//   - Capture on a memorial saves what it says (names, dates, places, cemetery,
//     plot, inscription, family, bio), Find a Grave's own citation, and every
//     photo at full size, filed as a website dated by the death (TI-066,
//     TI-068: never a burial date it does not give, never the birth);
//   - photos come from Find a Grave's own image host only, and nothing is
//     sent with them - no cookies, no login;
//   - a photo already saved is not downloaded again, in the same sitting or
//     when the archive already has it;
//   - Find a Grave asking us to slow down stops the downloads and says what
//     to do; the rest come on the next Capture;
//   - what she typed is filed as typed, kept on recapture, cleared on a new
//     memorial;
//   - a tab that moves on mid-capture never mixes two memorials.
//
//   node --test browser-companion/tests/test-panel-findagrave.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { openPanel, settle, tick, holdFirst } = require('./panel-harness.js');
const fag = require('../src/lib/findagrave-record.js');
const { parse } = require('./mini-dom.js');

const SAMPLES = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'capture-samples');
const sample = (name) => fs.readFileSync(path.join(SAMPLES, name + '.html'), 'utf8');

const CALVIN = 'https://www.findagrave.com/memorial/900000001/calvin-george-hartley';
const WARREN = 'https://www.findagrave.com/memorial/900000003/warren-calvin-hartley';
const SID = 'S-0a1b2c3d4e';

// Warren's memorial: Calvin's page, renamed (enough to tell the two apart).
function memorialHtml(url) {
  const html = sample('findagrave-memorial');
  return url === WARREN
    ? html.replace('id="bio-name">Calvin George Hartley', 'id="bio-name">Warren Calvin Hartley')
      .replace(/12 Mar 1842/g, '9 May 1870').replace(/4 Jan 1918 \(aged 75\)/g, '2 Feb 1951')
    : html;
}

// Find a Grave, as the content script sees it: the memorial in the tab, and
// its gallery fetched by id. `gallery` replaces the gallery's answer.
function fagSite({ gallery, galleryLog } = {}) {
  return async (msg, tab) => {
    const doc = parse(memorialHtml(tab.url));
    switch (msg.action) {
      case 'prefill': {
        const info = fag.fagMemorialFrom(doc, tab.url, {});
        return {
          ok: true,
          prefill: {
            url: tab.url, title: fag.fagTitle(info.record), sourceDate: fag.fagHints(info.record).sourceDate,
            sourceType: 'website', repository: 'Find a Grave', recipeHint: 'findagrave',
            people: [info.record.name].concat(info.record.family.map((f) => f.name)),
            findagraveMemorial: true,
          },
        };
      }
      case 'findagraveMemorialInfo': {
        // As content.js findagraveMemorialInfo composes it.
        const info = fag.fagMemorialFrom(doc, tab.url, { today: msg.today });
        if (!info.ok) return info;
        info.hints = fag.fagHints(info.record);
        info.title = fag.fagTitle(info.record);
        return info;
      }
      case 'findagraveGallery': {
        if (galleryLog) galleryLog.push(msg);
        if (gallery) return gallery(msg);
        // As content.js findagraveGallery does it, over the fixture gallery.
        const got = await fag.fagGallery(msg.memorialId, msg.slug, {
          fetch: async () => ({ ok: true, status: 200, text: async () => sample('findagrave-memorial-photos') }),
          parse, base: 'https://www.findagrave.com',
        });
        const counts = {};
        for (const p of got.photos || []) p.role = fag.fagPhotoRole(p.type, counts);
        return got;
      }
      case 'pagehtml': return { ok: true, html: '<html>' + tab.url + '</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy of ' + tab.url + '</html>' };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

// Find a Grave's image host: each photo answers as `answers[url]` says
// (a status, or a full response), else a JPEG or PNG.
function imageHost(answers = {}) {
  return async (url) => {
    const a = answers[url];
    if (typeof a === 'number') return { ok: false, status: a, url, headers: { get: () => 'text/html' } };
    if (a) return a;
    const type = /\.png$/.test(url) ? 'image/png' : 'image/jpeg';
    return { ok: true, status: 200, url, headers: { get: () => type }, blob: async () => new Blob(['photo ' + url], { type }) };
  };
}

const PHOTO = {
  grave: 'https://images.findagrave.com/photos/2020/45/900000001_aaaa1111.jpeg',
  person: 'https://images.findagrave.com/photos/2019/12/900000001_bbbb2222.jpg',
  family: 'https://images.findagrave.com/photos/2019/12/900000001_cccc3333.png',
};

const open = (url, opts = {}) => openPanel(Object.assign({
  url, respond: opts.wrap ? opts.wrap(fagSite(opts)) : fagSite(opts), fetch: imageHost(opts.answers),
}, opts));
const asset = (spec, name) => spec.assets.find((a) => a.filename === name);
const recordData = async (spec) => JSON.parse(await asset(spec, 'record-data.json').blob.text());
const today = () => {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
};

// ── one memorial ─────────────────────────────────────────────────────────────

test('a memorial saves what it says, its own citation and every photo at full size, filed as a website dated by the death', async () => {
  const p = await open(CALVIN);
  assert.match(p.$('asset-status').textContent, /the memorial’s facts and citation \+ its photos at full size \(Find a Grave, auto\)/);
  assert.strictEqual(p.$('f-asset-url').value, '');
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'grave-1.jpg', 'person-1.jpg', 'family-1.png', 'record-data.json']);
  const cap = p.capJson(b);
  assert.strictEqual(cap.url, CALVIN);
  assert.strictEqual(cap.title, 'Calvin George Hartley (1842-1918) - Find a Grave memorial');
  assert.strictEqual(cap.source_type, 'website');
  assert.strictEqual(cap.source_date, '1918-01-04'); // the death; no burial date, never the birth
  assert.strictEqual(cap.source_class, 'derivative');
  assert.strictEqual(cap.repository, 'Find a Grave');
  assert.strictEqual(cap.recipe_hint, 'findagrave');
  assert.match(cap.citation, new RegExp('^Find a Grave, database and images \\(' + CALVIN.replace(/[./]/g, '\\$&') +
    ': accessed ' + today() + '\\), memorial page for Calvin George Hartley'));
  assert.deepStrictEqual(cap.people, ['Calvin George Hartley', 'Thomas Hartley', 'Harriet Frances Hartley', 'Warren Calvin Hartley']);
  assert.deepStrictEqual(cap.assets.map((a) => [a.file, a.role, a.mode]), [
    ['page-snapshot.html', 'webpage', 'singlefile'],
    ['grave-1.jpg', 'grave-1', 'findagrave-photo'],
    ['person-1.jpg', 'person-1', 'findagrave-photo'],
    ['family-1.png', 'family-1', 'findagrave-photo'],
    ['record-data.json', 'site-data', 'findagrave-record-data'],
  ]);
  assert.match(cap.notes, /Find a Grave citation: Find a Grave, database and images/);
  assert.match(cap.notes, /3 photos saved from the memorial \(grave-1, person-1, family-1\)\./);
  assert.match(p.result(), /^Staged to \/dl\/fha-inbox\//);
  assert.strictEqual(p.resultClass(), 'stage-result ok');
});

test('record-data.json: the memorial\'s facts, family and photos, nothing from Seen It (TI-067)', async () => {
  const p = await open(CALVIN);
  await p.capture();
  const data = await recordData(p.written[0]);
  assert.deepStrictEqual(Object.keys(data), ['note', 'record', 'citation', 'title', 'photos', 'photosTotal']);
  assert.strictEqual(data.record.memorialId, '900000001');
  assert.strictEqual(data.record.burial.cemetery, 'Fairview Cemetery');
  assert.strictEqual(data.record.burial.plot, 'Block 4, Lot 12');
  assert.strictEqual(data.record.inscription, 'Gone home\nRest in peace');
  assert.deepStrictEqual(data.record.family.map((f) => f.relation + ': ' + f.name),
    ['Parents: Thomas Hartley', 'Spouse: Harriet Frances Hartley', 'Children: Warren Calvin Hartley']);
  assert.strictEqual(data.photosTotal, 3);
  assert.deepStrictEqual(data.photos.map((x) => [x.photoId, x.role, x.file, x.caption]), [
    ['990000101', 'grave-1', 'grave-1.jpg', 'Headstone, west side of the cemetery'],
    ['990000102', 'person-1', 'person-1.jpg', 'Calvin, about 1900'],
    ['990000103', 'family-1', 'family-1.png', null],
  ]);
  assert.doesNotMatch(JSON.stringify(data), /Seen 2|Not in archive/);
});

test('no login: the photos come only from Find a Grave\'s image host, and nothing is sent with them', async () => {
  const p = await open(CALVIN);
  await p.capture();
  assert.deepStrictEqual(p.fetched.map((f) => f.url), [PHOTO.grave, PHOTO.person, PHOTO.family]);
  for (const f of p.fetched) {
    assert.strictEqual(new URL(f.url).hostname, 'images.findagrave.com');
    assert.deepStrictEqual(f.init, { credentials: 'omit', referrerPolicy: 'no-referrer' });
  }
});

test('a photo the page lists on any other host is never fetched, and one that redirects off it is not saved', async () => {
  const evil = 'https://evil.example/photos/stolen.jpg';
  const gallery = () => ({
    ok: true, total: 2, photos: [
      { photoId: '1', url: evil, role: 'grave-1', type: 'Grave' },
      { photoId: '2', url: PHOTO.person, role: 'person-1', type: 'Person' },
    ],
  });
  const answers = {
    [PHOTO.person]: { ok: true, status: 200, url: 'https://evil.example/landing.jpg', headers: { get: () => 'image/jpeg' }, blob: async () => new Blob(['x']) },
  };
  const p = await open(CALVIN, { gallery, answers });
  await p.capture();
  assert.deepStrictEqual(p.fetched.map((f) => f.url), [PHOTO.person], 'the off-host photo is never requested');
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'record-data.json']);
  const data = await recordData(b);
  assert.match(data.photos[0].error, /not on Find a Grave’s own image host/);
  assert.match(data.photos[1].error, /led away from Find a Grave’s image host/);
  assert.strictEqual(p.resultClass(), 'stage-result warn');
});

test('an answer that is not an image (a page instead of the photo) is not saved as the photo', async () => {
  const answers = { [PHOTO.grave]: { ok: true, status: 200, url: PHOTO.grave, headers: { get: () => 'text/html; charset=utf-8' }, blob: async () => new Blob(['<html>']) } };
  const p = await open(CALVIN, { answers });
  await p.capture();
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'person-1.jpg', 'family-1.png', 'record-data.json']);
  assert.match((await recordData(b)).photos[0].error, /sent a page instead of the photo/);
  assert.match(p.result(), /One photo was not saved \(grave-1: Find a Grave sent a page instead of the photo\)\. Press Capture again/);
});

// ── typed corrections ────────────────────────────────────────────────────────

async function type(p, id, value) {
  p.$(id).value = value;
  await p.$(id).fire(id === 'f-type' ? 'change' : 'input');
}

test('what she typed is filed as typed, kept when she captures again, and cleared on the next memorial', async () => {
  const p = await open(CALVIN);
  assert.strictEqual(p.$('f-title').value, 'Calvin George Hartley (1842-1918) - Find a Grave memorial');
  assert.strictEqual(p.$('f-date').value, '1918-01-04');
  await type(p, 'f-title', 'Find a Grave - Hartley, Calvin G.');
  await type(p, 'f-date', '1918-01');
  await type(p, 'f-type', 'other');
  await type(p, 'f-repo', 'Find a Grave (volunteer memorial)');
  await p.capture();
  await p.capture();
  for (const b of p.written) {
    const cap = p.capJson(b);
    assert.strictEqual(cap.title, 'Find a Grave - Hartley, Calvin G.');
    assert.strictEqual(cap.source_date, '1918-01');
    assert.strictEqual(cap.source_type, 'other');
    assert.strictEqual(cap.repository, 'Find a Grave (volunteer memorial)');
  }
  await p.navigate(WARREN);
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Warren Calvin Hartley (1870-1951) - Find a Grave memorial');
  await p.capture();
  const cap = p.capJson(p.written[2]);
  assert.strictEqual(cap.title, 'Warren Calvin Hartley (1870-1951) - Find a Grave memorial');
  assert.strictEqual(cap.source_date, '1951-02-02');
  assert.strictEqual(cap.repository, 'Find a Grave');
});

// ── photos already saved ─────────────────────────────────────────────────────

test('capturing the memorial again in the same sitting downloads no photo twice', async () => {
  const p = await open(CALVIN);
  await p.capture();
  assert.strictEqual(p.fetched.length, 3);
  await p.capture();
  assert.strictEqual(p.fetched.length, 3, 'nothing downloaded again');
  const b = p.written[1];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'record-data.json']);
  const data = await recordData(b);
  for (const x of data.photos) {
    assert.strictEqual(x.file, null);
    assert.deepStrictEqual(x.same_image_as, { record: 'fag:900000001', url: CALVIN, name: 'Calvin George Hartley' });
  }
  assert.match(p.capJson(b).notes, /Photo grave-1 \(Find a Grave photo 990000101\) is already saved in the capture of Calvin George Hartley/);
  assert.match(p.result(), /3 photos were already saved, so not downloaded again/);
});

test('a photo the archive already holds (Seen It, from a saved file) is not downloaded again; the rest are', async () => {
  const p = await open(CALVIN, { seenIt: { 'fagphoto:990000101': { state: 'filed', sid: SID }, 'fag:900000001': 'filed' } });
  await p.capture();
  const ask = p.external.find((e) => e.msg.type === 'knownKeys');
  assert.deepStrictEqual(ask.msg.keys, ['fagphoto:990000101', 'fagphoto:990000102', 'fagphoto:990000103'],
    'only the photos are asked about - the memorial alone says nothing about its photos');
  assert.deepStrictEqual(p.fetched.map((f) => f.url), [PHOTO.person, PHOTO.family]);
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'person-1.jpg', 'family-1.png', 'record-data.json']);
  const data = await recordData(b);
  assert.deepStrictEqual(data.photos[0].same_image_as, { where: 'archive', source: SID });
  assert.match(p.capJson(b).notes, /Photo grave-1 \(Find a Grave photo 990000101\) is already saved in the archive, source S-0a1b2c3d4e/);
});

test('without Seen It, every photo is downloaded', async () => {
  const p = await open(CALVIN);
  await p.capture();
  assert.strictEqual(p.fetched.length, 3);
});

// ── slow down, refusals, removed memorials ──────────────────────────────────

test('Find a Grave asking us to slow down stops the downloads, says what to do, and the next Capture gets the rest', async () => {
  const answers = { [PHOTO.person]: 429 };
  const p = await open(CALVIN, { answers });
  await p.capture();
  assert.deepStrictEqual(p.fetched.map((f) => f.url), [PHOTO.grave, PHOTO.person], 'no request after the 429');
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'grave-1.jpg', 'record-data.json']);
  const data = await recordData(b);
  assert.deepStrictEqual(data.photos.map((x) => [x.file, x.error || null]), [
    ['grave-1.jpg', null],
    [null, 'Find a Grave asked us to slow down (HTTP 429)'],
    [null, 'not tried: Find a Grave asked us to slow down'],
  ]);
  assert.strictEqual(p.resultClass(), 'stage-result warn');
  assert.match(p.result(), /Wait a few minutes, then press Capture again: the photos already saved are not downloaded again\./);
  delete answers[PHOTO.person];
  await p.capture();
  assert.deepStrictEqual(p.fetched.slice(2).map((f) => f.url), [PHOTO.person, PHOTO.family]);
  assert.deepStrictEqual(p.files(p.written[1]), ['page-snapshot.html', 'person-1.jpg', 'family-1.png', 'record-data.json']);
});

test('a refused photo is said with its cause; the others are saved', async () => {
  const p = await open(CALVIN, { answers: { [PHOTO.family]: 403 } });
  await p.capture();
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'grave-1.jpg', 'person-1.jpg', 'record-data.json']);
  assert.match(p.result(), /One photo was not saved \(family-1: Find a Grave refused the photo \(HTTP 403\)\)\. Press Capture again to try again/);
  assert.match(p.capJson(b).notes, /Not saved: One photo was not saved/);
});

test('a photo list Find a Grave will not give still saves the memorial, with its page, and says why', async () => {
  const gallery = () => ({ ok: false, error: fag.fagStatusMessage(429, 'gallery') });
  const p = await open(CALVIN, { gallery });
  p.$('cb-pagecopy').checked = false; // even unticked: the facts are never filed alone
  await p.capture();
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'record-data.json']);
  const data = await recordData(b);
  assert.deepStrictEqual([data.photos, data.photosTotal], [[], null]);
  assert.match(p.result(), /The memorial’s photos were not saved: Find a Grave asked us to slow down \(HTTP 429\)\. Wait a few minutes, then press Capture again\. Its facts and citation are saved/);
  assert.match(p.result(), /A copy of the web page was kept too/);
});

test('a memorial with no photos saves its facts with its page', async () => {
  const p = await open(CALVIN, { gallery: () => ({ ok: true, photos: [], total: 0 }) });
  p.$('cb-pagecopy').checked = false;
  await p.capture();
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'record-data.json']);
  assert.match(p.capJson(b).notes, /The memorial has no photos\./);
  assert.strictEqual(p.fetched.length, 0);
});

test('with photos saved, an unticked page copy stays off', async () => {
  const p = await open(CALVIN);
  p.$('cb-pagecopy').checked = false;
  await p.capture();
  assert.deepStrictEqual(p.files(p.written[0]), ['grave-1.jpg', 'person-1.jpg', 'family-1.png', 'record-data.json']);
});

test('a memorial that was removed or merged says so, and what to do; nothing is saved', async () => {
  const p = await openPanel({
    url: CALVIN,
    respond: (msg, tab) => {
      if (msg.action === 'findagraveMemorialInfo') {
        return fag.fagMemorialFrom(parse('<html><body><h1>This memorial has been removed</h1></body></html>'), tab.url, {});
      }
      return fagSite()(msg, tab);
    },
    fetch: imageHost(),
  });
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /^Could not capture: this page shows no memorial - it may have been removed or merged into another\. Search Find a Grave/);
  assert.strictEqual(p.fetched.length, 0);
});

// ── the tab moving on mid-capture ───────────────────────────────────────────

async function untilHeld(h) {
  for (let i = 0; i < 200 && !h.held; i++) await tick();
  assert.ok(h.held, 'the held request was never made');
}

test('moving to the next memorial while this one is read saves nothing of either', async () => {
  let hold = null;
  const p = await open(CALVIN, { wrap: (r) => { hold = holdFirst(r, 'findagraveMemorialInfo'); return hold.respond; } });
  const pending = p.capture();
  await untilHeld(hold);
  await p.navigate(WARREN);
  hold.release();
  await pending;
  await settle();
  assert.strictEqual(p.written.length, 0, 'never Calvin\'s form with Warren\'s memorial');
  assert.match(p.result(), /^Could not capture: the page in the tab changed while this memorial was being saved, so nothing was saved/);
  assert.strictEqual(p.$('f-title').value, 'Warren Calvin Hartley (1870-1951) - Find a Grave memorial', 'the form now describes the new page');
  assert.strictEqual(p.fetched.length, 0);
});

test('moving on while the photo list is read: the page copy is never the next memorial\'s', async () => {
  let hold = null;
  const p = await open(CALVIN, { wrap: (r) => { hold = holdFirst(r, 'findagraveGallery'); return hold.respond; } });
  const pending = p.capture();
  await untilHeld(hold);
  await p.navigate(WARREN);
  hold.release();
  await pending;
  await settle();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /the page in the tab changed while this record was being saved, so nothing was saved/);
  assert.strictEqual(p.fetched.length, 0, 'no photo downloaded for a capture that cannot be kept');
});

test('moving on while the photos download: the bundle is all Calvin\'s, photos fetched by their own address', async () => {
  let release = null;
  let held = false;
  const answers = {};
  const host = imageHost();
  const p = await openPanel({
    url: CALVIN, respond: fagSite(),
    fetch: async (url, init) => {
      if (url === PHOTO.person && !release) {
        held = true;
        await new Promise((r) => { release = r; });
      }
      return (answers[url] || host)(url, init);
    },
  });
  const pending = p.capture();
  for (let i = 0; i < 200 && !held; i++) await tick();
  assert.ok(held);
  await p.navigate(WARREN);
  release();
  await pending;
  await settle();
  assert.strictEqual(p.written.length, 1, p.result());
  const b = p.written[0];
  assert.strictEqual(b.captureJson.url, CALVIN);
  assert.match(await asset(b, 'page-snapshot.html').blob.text(), /900000001/);
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'grave-1.jpg', 'person-1.jpg', 'family-1.png', 'record-data.json']);
  assert.strictEqual((await recordData(b)).record.name, 'Calvin George Hartley');
});
