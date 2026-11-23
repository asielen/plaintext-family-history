// The capture panel driven end to end on a West Virginia Archives view page,
// without a browser (tests/panel-harness.js). The content script is played by
// the real readers in lib/wvculture-record.js, and the image host by a
// stand-in that records every request.
//
// What it guards, in the human's terms:
//   - Capture on a view page saves the record image, its film and image
//     number and a citation dated the day he looked (kept when the bundle is
//     filed on a later day);
//   - the image is downloaded by the panel, not the page: the live image host
//     refuses a fetch made from the page, so a page-side fetch never worked;
//   - when the image cannot be saved, the message says what to do next, and
//     an image he drops in is saved with the page's numbers and citation, even
//     on a page whose image the reader does not recognise;
//   - an error page or a redirect off the image host is never saved as his
//     death certificate.
//
//   node --test browser-companion/tests/test-panel-wvculture.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');

const { openPanel } = require('./panel-harness.js');
const wv = require('../src/lib/wvculture-record.js');

const PAGE = 'https://archive.wvculture.org/vrr/va_view2.aspx?FilmNumber=1984148&ImageNumber=2530';
const IMG = 'https://images.wvculture.org/1984148/0002530.gif';
const BIG = 'x'.repeat(40 * 1024);

// The view page, as the content script sees it: `srcs` are the page's img
// srcs (absolute), read by the real wvImageUrl.
function wvSite({ srcs = ['http://images.wvculture.org/1984148/0002530.gif'] } = {}) {
  return async (msg, tab) => {
    switch (msg.action) {
      case 'prefill':
        return { ok: true, prefill: { url: tab.url, title: 'West Virginia Vital Research Records - Record Image',
          repository: 'archive.wvculture.org', recipeHint: 'wvculture', people: [],
          wvcultureImagePage: !!wv.wvParsePage(tab.url) } };
      case 'wvcultureImageInfo': {
        // As content.js wvcultureImageInfo composes it.
        const parsed = wv.wvParsePage(tab.url);
        if (!parsed) return { ok: false, error: 'this is not a West Virginia Archives image page' };
        const imageUrl = wv.wvImageUrl(srcs);
        const record = Object.assign({}, parsed, { pageUrl: tab.url, imageUrl, ext: imageUrl ? wv.wvExtension(imageUrl) : null });
        return { ok: true, record, citation: wv.wvCitation(record, msg.today) };
      }
      case 'pagehtml': return { ok: true, html: '<html>' + tab.url + '</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy of ' + tab.url + '</html>' };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

// The image host: `answer(url, init)` or a full-size GIF.
function imageHost(answer) {
  return async (url, init) => {
    if (answer) return answer(url, init);
    return { ok: true, status: 200, url, headers: { get: () => 'image/gif' }, blob: async () => new Blob([BIG], { type: 'image/gif' }) };
  };
}

const open = (opts = {}) => openPanel(Object.assign({ url: PAGE, respond: wvSite(opts), fetch: imageHost(opts.answer) }, opts));
const asset = (spec, name) => spec.assets.find((a) => a.filename === name);
const dropFile = (p, name) => {
  const file = Object.assign(new Blob(['scan'], { type: 'image/gif' }), { name });
  return p.$('dropzone').listeners.drop[0]({ preventDefault() {}, dataTransfer: { files: [file] } });
};

test('a view page saves its image, film and image number and the capture-day citation', async () => {
  const p = await open();
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const b = p.written[0];
  const cap = p.capJson(b);
  assert.strictEqual(cap.title, 'West Virginia Vital Research Records, film 1984148, image 2530');
  assert.strictEqual(cap.recipe_hint, 'wvculture');
  assert.strictEqual(cap.repository, 'West Virginia Archives and History');
  // Codex 4660426759: the citation travels in capture.json, so a later ingest keeps its date.
  assert.match(cap.citation, /^West Virginia Archives and History, Vital Research Records \(.+ : accessed \d{4}-\d{2}-\d{2}\), film 1984148, image 2530\.$/);
  assert.deepStrictEqual(cap.assets.filter((a) => a.role !== 'webpage').map((a) => [a.file, a.role, a.mode]), [
    ['record.gif', 'record', 'wvculture'],
    ['record-data.json', 'site-data', 'wvculture-data'],
  ]);
  assert.strictEqual(asset(b, 'record.gif').blob.size, BIG.length);
  const data = JSON.parse(await asset(b, 'record-data.json').blob.text());
  assert.strictEqual(data.imageUrl, IMG);
  // The panel downloads it, once, with no cookies, no referrer, no redirect.
  assert.deepStrictEqual(p.fetched.map((f) => f.url), [IMG]);
  assert.strictEqual(p.fetched[0].init.credentials, 'omit');
  assert.strictEqual(p.fetched[0].init.redirect, 'error');
  assert.strictEqual(p.fetched[0].init.referrerPolicy, 'no-referrer');
  assert.ok(!p.sent.some((m) => m.action === 'wvcultureImage'), 'the page is not asked to download it');
});

test('an image that cannot be reached says to drop it in, and the dropped image is saved with the citation', async () => {
  const p = await open({ answer: async () => { throw new TypeError('Failed to fetch'); } });
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /could not be reached.*drop it into the panel and press Capture again/);
  await dropFile(p, '0002530.gif');
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const cap = p.capJson(p.written[0]);
  assert.deepStrictEqual(cap.assets.filter((a) => a.role === 'record').map((a) => [a.file, a.mode]), [['record.gif', 'manual']]);
  assert.match(cap.citation, /film 1984148, image 2530\.$/);
});

test('a page whose image the reader does not recognise still saves a dropped image (audit F1)', async () => {
  const p = await open({ srcs: ['https://archive.wvculture.org/vrr/ImageHandler.ashx?film=1984148&image=2530'] });
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /no image the panel can save.*drop it into the panel and press Capture again/);
  assert.strictEqual(p.fetched.length, 0);
  await dropFile(p, 'certificate.gif');
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const cap = p.capJson(p.written[0]);
  assert.strictEqual(cap.title, 'West Virginia Vital Research Records, film 1984148, image 2530');
  assert.match(cap.citation, /film 1984148, image 2530\.$/);
});

for (const [what, answer, said] of [
  ['an error page answered with 200', async (url) => ({ ok: true, status: 200, url, headers: { get: () => 'text/html; charset=utf-8' },
    blob: async () => new Blob([BIG], { type: 'text/html' }) }), /sent a page instead of the picture/],
  ['a redirect off the image host', async () => ({ ok: true, status: 200, url: 'https://www.wvculture.org/notfound.gif',
    headers: { get: () => 'image/gif' }, blob: async () => new Blob([BIG], { type: 'image/gif' }) }), /led away from the West Virginia Archives image host/],
  ['a placeholder under 30 KB', async (url) => ({ ok: true, status: 200, url, headers: { get: () => 'image/gif' },
    blob: async () => new Blob(['tiny'], { type: 'image/gif' }) }), /only 0 KB, not the full image/],
  ['a refusal', async (url) => ({ ok: false, status: 403, url, headers: { get: () => 'text/html' } }), /refused the picture \(HTTP 403\)/],
]) {
  test(what + ' is never saved as the record, and the message says what to do (audit F2, Codex 4660426757)', async () => {
    const p = await open({ answer });
    await p.capture();
    assert.strictEqual(p.written.length, 0, 'nothing filed');
    assert.match(p.result(), said);
    assert.match(p.result(), /drop it into the panel and press Capture again/);
  });
}
