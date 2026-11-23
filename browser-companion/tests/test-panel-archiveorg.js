// The capture panel driven end to end on a page of an Internet Archive book,
// without a browser (tests/panel-harness.js). The content script is played by
// a stand-in that says what content.js says (a book page, its address), and
// archive.org by a stand-in that answers from the fixtures recorded from one
// public 1881 county history (tests/fixtures/capture-samples/archiveorg-*) and
// records every request.
//
// What it guards, in the human's terms:
//   - Capture on a book page saves the page image, the page's text marked an
//     unchecked machine reading from the Internet Archive, the book's details,
//     and a citation with the link to the page as she was looking at it;
//   - the printed page number comes from the book's own page-number list, and
//     "not known" is said when there is none;
//   - "this page and the next" saves two, in order, at most four;
//   - nothing is sent with the requests, only archive.org is asked, and the
//     whole book is never downloaded;
//   - a borrowed book, a busy site or a page with no text still saves what it
//     can and says what is missing.
//
//   node --test browser-companion/tests/test-panel-archiveorg.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');

const { openPanel } = require('./panel-harness.js');
const ia = require('../src/lib/archiveorg-record.js');

const SAMPLES = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'capture-samples');
const read = (name) => fs.readFileSync(path.join(SAMPLES, name), 'utf8');
const json = (name) => JSON.parse(read(name));
const ID = 'historyofamadorc00maso';
const BOOK = 'https://archive.org/details/' + ID;
const N20 = BOOK + '/page/n20/mode/1up'; // leaf 21, printed page 11
const P11 = BOOK + '/page/11/mode/1up';

// What content.js says about a book page.
function bookSite() {
  return async (msg, tab) => {
    switch (msg.action) {
      case 'prefill': {
        const at = ia.iaParsePage(tab.url);
        return {
          ok: true,
          prefill: {
            url: tab.url, title: 'History of Amador County, California', sourceType: 'book',
            repository: 'Internet Archive', recipeHint: 'archiveorg', people: [], archiveorgPage: !!at,
          },
        };
      }
      case 'pagehtml': return { ok: true, html: '<html>' + tab.url + '</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy of ' + tab.url + '</html>' };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

// archive.org: the fixtures, a range of the OCR file, and an image per scan.
function archive({ answers = {}, log = [], noNumbers = false, noIndex = false } = {}) {
  const idx = Buffer.from(zlib.gzipSync(JSON.stringify(json('archiveorg-hocr-pageindex.json'))));
  const meta = json('archiveorg-metadata.json');
  if (noNumbers) meta.files = meta.files.filter((f) => !/page_numbers/.test(f.name));
  if (noIndex) meta.files = meta.files.filter((f) => !/hocr/.test(f.name));
  const ok = (body, extra) => Object.assign({
    ok: true, status: 200, url: 'https://archive.org/x', headers: { get: () => 'application/json' },
    json: async () => body, text: async () => String(body), arrayBuffer: async () => body,
    blob: async () => new Blob([body], { type: 'image/jpeg' }),
  }, extra || {});
  return async (url, init) => {
    log.push({ url, init });
    for (const k of Object.keys(answers)) {
      if (url.includes(k)) {
        const a = answers[k];
        return typeof a === 'number' ? ok('', { ok: false, status: a }) : a;
      }
    }
    if (/\/metadata\//.test(url)) return ok(meta);
    if (/_page_numbers\.json$/.test(url)) return ok(json('archiveorg-page-numbers.json'));
    if (/_hocr_pageindex\.json\.gz$/.test(url)) return ok(idx.buffer.slice(idx.byteOffset, idx.byteOffset + idx.byteLength));
    if (/_hocr\.html$/.test(url)) return ok(read('archiveorg-hocr-leaf20.html'), { status: 206 });
    const m = /\$(\d+)\/full\/full/.exec(url);
    if (m) {
      return ok(Buffer.alloc(60 * 1024, Number(m[1])), {
        url: 'https://ia800000.us.archive.org/x.jpg', headers: { get: () => 'image/jpeg' },
      });
    }
    return ok('', { ok: false, status: 404 });
  };
}

const open = (url, opts = {}) => openPanel(Object.assign({ url, respond: bookSite(), fetch: archive(opts) }, opts));
const asset = (spec, name) => spec.assets.find((a) => a.filename === name);
const today = () => {
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
};

test('a book page saves its image and its text (marked unchecked), the book\'s details and a citation', async () => {
  const log = [];
  const p = await open(N20, { log });
  assert.match(p.$('asset-status').textContent, /page image \+ its text, the book’s details and citation \(Internet Archive, auto\)/);
  assert.strictEqual(p.$('ia-note').style.display, 'block');
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'page-1.jpg', 'transcript.md', 'record-data.json']);
  const cap = p.capJson(b);
  assert.strictEqual(cap.url, N20.replace('/mode/1up', ''));
  assert.strictEqual(cap.title, 'History of Amador County, California, p. 11');
  assert.strictEqual(cap.source_type, 'book');
  assert.strictEqual(cap.source_date, '1881');
  assert.strictEqual(cap.source_class, 'derivative');
  assert.strictEqual(cap.repository, 'Internet Archive');
  assert.strictEqual(cap.recipe_hint, 'archiveorg');
  assert.strictEqual(cap.citation, 'Jesse D. Mason, History of Amador County, California (Oakland, Cal., Thompson & West, 1881), p. 11; ' +
    'digital images, Internet Archive (https://archive.org/details/' + ID + '/page/n20 : accessed ' + today() + ').');
  assert.deepStrictEqual(cap.assets.map((a) => [a.file, a.role, a.mode]), [
    ['page-snapshot.html', 'webpage', 'singlefile'],
    ['page-1.jpg', 'page-1', 'internet-archive-image'],
    ['transcript.md', 'transcript', 'internet-archive-ocr'],
    ['record-data.json', 'site-data', 'internet-archive-record-data'],
  ]);
  const text = await asset(b, 'transcript.md').blob.text();
  assert.match(text, /^<!-- AI-DRAFT Internet Archive OCR, unchecked, captured \d{4}-\d{2}-\d{2} -->\n\n10\n\nHISTORY OF AMADOR COUNTY, CALIFORNIA\.\n$/);
  const data = JSON.parse(await asset(b, 'record-data.json').blob.text());
  assert.strictEqual(data.item.title, 'History of Amador County, California');
  assert.strictEqual(data.item.itemId, ID);
  assert.deepStrictEqual(data.pages.map((x) => [x.leaf, x.printed, x.file, x.ocr]), [[20, '11', 'page-1.jpg', true]]);
  assert.match(p.result(), /^Staged to \/dl\/fha-inbox\//);
  assert.strictEqual(p.resultClass(), 'stage-result ok');
  // the image asked for is scan 21 at full size, from archive.org's image service
  assert.ok(log.some((l) => l.url === 'https://iiif.archive.org/iiif/' + ID + '$21/full/full/0/default.jpg'));
});

test('a printed-page address finds its scan through the page-number list, and the link keeps the form she looked at', async () => {
  const p = await open(P11);
  await p.capture();
  const b = p.written[0];
  assert.strictEqual(p.capJson(b).url, BOOK + '/page/11');
  assert.match(p.capJson(b).citation, /\), p\. 11; digital images, Internet Archive \(https:\/\/archive\.org\/details\/historyofamadorc00maso\/page\/11 : accessed/);
  const data = JSON.parse(await asset(b, 'record-data.json').blob.text());
  assert.deepStrictEqual(data.pages.map((x) => [x.leaf, x.printed]), [[20, '11']]);
});

test('"this page and the next" saves two pages in order, with a range in the citation', async () => {
  const p = await open(N20);
  p.$('ia-leaves').value = '2';
  await p.capture();
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'page-1.jpg', 'page-2.jpg', 'transcript.md', 'record-data.json']);
  assert.deepStrictEqual(p.capJson(b).assets.map((a) => a.role), ['webpage', 'page-1', 'page-2', 'transcript', 'site-data']);
  assert.match(p.capJson(b).citation, /\), pp\. 11-12; digital images/);
  const text = await asset(b, 'transcript.md').blob.text();
  assert.equal((text.match(/AI-DRAFT/g) || []).length, 1);
  assert.match(text, /## Page 11 \(scan n20\)[\s\S]*## Page 12 \(scan n21\)/);
});

test('never more than four pages, and never past the last page of the book', async () => {
  const p = await open(BOOK + '/page/n496');
  p.$('ia-leaves').value = '9';
  await p.capture();
  const data = JSON.parse(await asset(p.written[0], 'record-data.json').blob.text());
  assert.strictEqual(data.pages.length, 2); // scans 497 and 498 of 498
  const q = await open(N20);
  q.$('ia-leaves').value = '9';
  await q.capture();
  assert.strictEqual(JSON.parse(await asset(q.written[0], 'record-data.json').blob.text()).pages.length, 4);
});

test('no login and no whole-book download: every request omits cookies and the referrer, on archive.org only, the OCR by range', async () => {
  const log = [];
  const p = await open(N20, { log });
  await p.capture();
  for (const l of log) {
    assert.match(new URL(l.url).hostname, /(^|\.)archive\.org$/);
    assert.strictEqual(l.init.credentials, 'omit');
    assert.strictEqual(l.init.referrerPolicy, 'no-referrer');
  }
  const ocr = log.filter((l) => /_hocr\.html$/.test(l.url));
  assert.strictEqual(ocr.length, 1);
  assert.match(ocr[0].init.headers.Range, /^bytes=\d+-\d+$/);
  assert.ok(!log.some((l) => /_djvu\.txt|\.pdf/.test(l.url)), 'the whole book was asked for');
});

test('a book with no page-number list: the scan is named by its address and the printed page is said to be unknown', async () => {
  const p = await open(N20, { noNumbers: true });
  await p.capture();
  const cap = p.capJson(p.written[0]);
  assert.match(cap.citation, /\), scan n20 \(printed page not known\); digital images/);
  assert.strictEqual(cap.title, 'History of Amador County, California, scan n20');
  assert.match(cap.notes, /printed page number is not known for scan n20/);
  // and a printed-page address cannot be placed without the list
  const q = await open(P11, { noNumbers: true });
  await q.capture();
  assert.strictEqual(q.written.length, 0);
  assert.match(q.result(), /could not tell which scan is page 11/);
});

test('a book with no text index still saves the image and says the text was not saved', async () => {
  const p = await open(N20, { noIndex: true });
  await p.capture();
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'page-1.jpg', 'record-data.json']);
  assert.match(p.capJson(b).notes, /page’s text was not saved: this book has no page-by-page text index/);
  assert.strictEqual(p.resultClass(), 'stage-result warn');
});

test('a borrowed book: nothing is saved but its details and a page copy, and she is told to borrow it', async () => {
  const p = await open(N20, { answers: { '$21/full/full': 403 } });
  p.$('ia-leaves').value = '2';
  await p.capture();
  const b = p.written[0];
  assert.deepStrictEqual(p.files(b), ['page-snapshot.html', 'record-data.json']);
  assert.match(p.capJson(b).notes, /without borrowing it/);
  assert.match(p.result(), /Borrow it on archive\.org/);
  // the second page is not tried once the first is refused
  const data = JSON.parse(await asset(b, 'record-data.json').blob.text());
  assert.match(data.pages[1].error, /not tried/);
});

test('the Internet Archive asking us to slow down stops the downloads and says what to do', async () => {
  const p = await open(N20, { answers: { '$21/full/full': 429 } });
  p.$('ia-leaves').value = '2';
  await p.capture();
  assert.match(p.result(), /slow down/);
  assert.strictEqual(p.written[0].assets.some((a) => /^page-[0-9]/.test(a.filename)), false);
});

test('a book that is gone, or an address with no page, saves nothing and says why', async () => {
  const gone = await open(N20, { answers: { '/metadata/': 404 } });
  await gone.capture();
  assert.strictEqual(gone.written.length, 0);
  assert.match(gone.result(), /has no item at this address/);
  const none = await open(BOOK);
  await none.capture();
  assert.strictEqual(none.written.length, 0);
  assert.match(none.result(), /names no page of the book/);
});

test('what she typed is filed as typed: a title, a date and the Kind', async () => {
  const p = await open(N20);
  p.$('f-title').value = 'Amador history, the preface';
  p.$('f-title').fire('input');
  p.$('f-date').value = '1881-06';
  p.$('f-type').value = 'newspaper';
  await p.capture();
  const cap = p.capJson(p.written[0]);
  assert.strictEqual(cap.source_date, '1881-06');
  assert.strictEqual(cap.source_type, 'newspaper');
});
