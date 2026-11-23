// Internet Archive book pages: the readers in lib/archiveorg-record.js, run on
// fixtures recorded from one public 1881 county history on archive.org
// (tests/fixtures/capture-samples/archiveorg-*; the item's description, its
// page-number list and page index trimmed to the first leaves, and the OCR of
// one leaf).
//
// What it guards, in the human's terms: an address says which page she is
// looking at (the scan number, or the printed number BookReader writes); the
// book's author, title, publisher and date come out as a citation names them;
// the printed page number is found for the scan, and "not known" is said when
// it is not; the page's text comes from one small range of the OCR file, never
// the whole book; the image is archive.org's own.
//
//   node --test browser-companion/tests/test-archiveorg.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');

const ia = require('../src/lib/archiveorg-record.js');

const SAMPLES = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'capture-samples');
const read = (name) => fs.readFileSync(path.join(SAMPLES, name), 'utf8');
const json = (name) => JSON.parse(read(name));
const ID = 'historyofamadorc00maso';

// ── which page ───────────────────────────────────────────────────────────────

test('iaParsePage: the scan number, the printed page, no page, and the viewer modes', () => {
  const base = 'https://archive.org/details/' + ID;
  assert.deepStrictEqual(ia.iaParsePage(base + '/page/n12'), { itemId: ID, leaf: 12, printedPage: null, segment: 'n12' });
  assert.deepStrictEqual(ia.iaParsePage(base + '/page/12'), { itemId: ID, leaf: null, printedPage: '12', segment: '12' });
  assert.deepStrictEqual(ia.iaParsePage(base + '/page/xii/mode/2up?q=Hartley'), { itemId: ID, leaf: null, printedPage: 'xii', segment: 'xii' });
  assert.deepStrictEqual(ia.iaParsePage(base + '/page/n12/mode/2up'), { itemId: ID, leaf: 12, printedPage: null, segment: 'n12' });
  assert.deepStrictEqual(ia.iaParsePage(base), { itemId: ID, leaf: null, printedPage: null, segment: null });
  assert.deepStrictEqual(ia.iaParsePage(base + '/mode/2up'), { itemId: ID, leaf: null, printedPage: null, segment: null });
  assert.strictEqual(ia.iaParsePage('https://www.archive.org/details/' + ID + '/page/n0').leaf, 0);
  assert.deepStrictEqual(ia.iaParsePage('/details/' + ID + '/page/n5', 'https://archive.org/'),
    { itemId: ID, leaf: 5, printedPage: null, segment: 'n5' });
  for (const bad of [
    'https://archive.org/search?query=hartley', 'https://archive.org/details/', 'https://archive.org/stream/' + ID,
    'http://archive.org/details/' + ID, 'https://archive.org.evil.example/details/' + ID,
    'https://evil.example/details/' + ID, 'https://archive.org/details/-bad id', 'not a url', '', null,
  ]) assert.strictEqual(ia.iaParsePage(bad), null, String(bad));
});

// ── the book ─────────────────────────────────────────────────────────────────

test('iaCleanCreator: the catalogue\'s "[Last, First] [from old catalog]" as a citation writes a person', () => {
  assert.strictEqual(ia.iaCleanCreator('[Mason, Jesse D] [from old catalog]'), 'Jesse D. Mason');
  assert.strictEqual(ia.iaCleanCreator('Smith, John, 1840-1910'), 'John Smith');
  assert.strictEqual(ia.iaCleanCreator(['Hartley, Calvin', 'Hartley, Warren']), 'Calvin Hartley and Warren Hartley');
  assert.strictEqual(ia.iaCleanCreator(['A, B', 'C, D', 'E, F']), 'B. A et al.');
  assert.strictEqual(ia.iaCleanCreator('Thompson & West'), 'Thompson & West');
  assert.strictEqual(ia.iaCleanCreator('Fairview Historical Society, Inc.'), 'Fairview Historical Society, Inc');
  assert.strictEqual(ia.iaCleanCreator(undefined), null);
  assert.strictEqual(ia.iaCleanCreator('  '), null);
});

test('iaMetadataFrom: title, author, publisher, date, year and the files, from the live description', () => {
  const m = ia.iaMetadataFrom(json('archiveorg-metadata.json'), ID);
  assert.strictEqual(m.ok, true);
  assert.deepStrictEqual(m.item, {
    itemId: ID, title: 'History of Amador County, California', creator: 'Jesse D. Mason',
    publisher: 'Oakland, Cal., Thompson & West', date: '1881', year: '1881', volume: null, imageCount: 498, mediatype: 'texts',
  });
  assert.ok(m.files.some((f) => f.name === ID + '_page_numbers.json'));
});

test('iaMetadataFrom: no author, no date, a volume, a date range; an item that does not exist', () => {
  const m = ia.iaMetadataFrom({ metadata: { title: 'Annals', volume: '2', date: 'c1890-1895', mediatype: 'texts' }, files: [] }, 'annals02x');
  assert.strictEqual(m.item.creator, null);
  assert.strictEqual(m.item.publisher, null);
  assert.strictEqual(m.item.volume, '2');
  assert.strictEqual(m.item.year, null); // a range dates no one year
  assert.strictEqual(m.item.imageCount, null);
  const none = ia.iaMetadataFrom({}, 'gone');
  assert.strictEqual(none.ok, false);
  assert.match(none.error, /no item called gone/);
  assert.strictEqual(ia.iaMetadataFrom(null, 'gone').ok, false);
});

// ── the printed page ─────────────────────────────────────────────────────────

test('the printed page for a scan, and the scan for a printed page', () => {
  const pages = ia.iaPageNumbersFrom(json('archiveorg-page-numbers.json'));
  assert.strictEqual(pages.length, 25);
  assert.strictEqual(ia.iaPrintedFor(pages, 20), '10');
  assert.strictEqual(ia.iaPrintedFor(pages, 21), '11');
  assert.strictEqual(ia.iaPrintedFor(pages, 1), null); // a cover: no number
  assert.strictEqual(ia.iaPrintedFor(pages, 400), null);
  assert.strictEqual(ia.iaLeafForPrinted(pages, '11'), 21);
  assert.strictEqual(ia.iaLeafForPrinted(pages, ' 11 '), 21);
  assert.strictEqual(ia.iaLeafForPrinted(pages, '999'), null);
  assert.strictEqual(ia.iaLeafForPrinted(pages, ''), null);
  assert.strictEqual(ia.iaLeafForPrinted(null, '11'), null);
  assert.strictEqual(ia.iaPageNumbersFrom({}), null);
  // a printed number that appears twice (a plate and a page): the first wins
  assert.strictEqual(ia.iaLeafForPrinted([{ leafNum: 5, printed: 'iv' }, { leafNum: 9, printed: 'iv' }], 'IV'), 5);
});

// ── the text ─────────────────────────────────────────────────────────────────

test('iaPageIndexRange: entry N is leaf N, entry 0 the file\'s header', () => {
  const idx = json('archiveorg-hocr-pageindex.json');
  assert.deepStrictEqual(ia.iaPageIndexRange(idx, 20), [idx[20][2], idx[20][3]]);
  assert.strictEqual(ia.iaPageIndexRange(idx, 400), null);
  assert.strictEqual(ia.iaPageIndexRange(null, 1), null);
  assert.strictEqual(ia.iaPageIndexRange([[0, 0, 5, 5]], 0), null); // an empty range
});

test('iaHocrText: a line for each printed line, a blank line between paragraphs, entities decoded', () => {
  const text = ia.iaHocrText(read('archiveorg-hocr-leaf20.html'));
  assert.strictEqual(text, '10\n\nHISTORY OF AMADOR COUNTY, CALIFORNIA.');
  const html = '<p class="ocr_par"><span class="ocr_line"><span class="ocrx_word">Smith</span>' +
    '<span class="ocrx_word">&amp;</span><span class="ocrx_word">Co.</span></span>' +
    '<span class="ocr_line"><span class="ocrx_word">&lt;-->&#233;</span></span></p>';
  assert.strictEqual(ia.iaHocrText(html), 'Smith & Co.\n<-->é');
  assert.strictEqual(ia.iaHocrText(''), '');
  assert.strictEqual(ia.iaHocrText(null), '');
});

// ── the image, the link, the citation ────────────────────────────────────────

test('iaImageUrl is archive.org\'s own image service; iaPageUrl keeps the form she was looking at', () => {
  assert.strictEqual(ia.iaImageUrl(ID, 21), 'https://iiif.archive.org/iiif/' + ID + '$21/full/full/0/default.jpg');
  assert.strictEqual(ia.iaPageUrl(ID, 'n20'), 'https://archive.org/details/' + ID + '/page/n20');
  assert.strictEqual(ia.iaPageUrl(ID, '11'), 'https://archive.org/details/' + ID + '/page/11');
  assert.strictEqual(ia.iaPageUrl(ID, null), 'https://archive.org/details/' + ID);
});

test('iaCitation: author, title, publisher and date, printed page, the page link and the accessed date', () => {
  const item = ia.iaMetadataFrom(json('archiveorg-metadata.json'), ID).item;
  const url = 'https://archive.org/details/' + ID + '/page/11';
  assert.strictEqual(ia.iaCitation(item, [{ leaf: 20, printed: '11' }], url, '2026-11-22'),
    'Jesse D. Mason, History of Amador County, California (Oakland, Cal., Thompson & West, 1881), p. 11; ' +
    'digital images, Internet Archive (' + url + ' : accessed 2026-11-22).');
  // two pages: a range
  assert.match(ia.iaCitation(item, [{ leaf: 20, printed: '11' }, { leaf: 21, printed: '12' }], url, '2026-11-22'), /\), pp\. 11-12; digital/);
  // a scan with no printed page: named by its place in the address, and said so
  assert.match(ia.iaCitation(item, [{ leaf: 20, printed: null }], url, '2026-11-22'), /\), scan n20 \(printed page not known\); digital/);
  // no author, no publisher, a volume
  const bare = { itemId: 'annals02x', title: 'Annals', volume: '2', date: '1890' };
  assert.strictEqual(ia.iaCitation(bare, [{ leaf: 3, printed: '4' }], 'https://archive.org/details/annals02x/page/4', '2026-11-22'),
    'Annals, vol. 2 (1890), p. 4; digital images, Internet Archive (https://archive.org/details/annals02x/page/4 : accessed 2026-11-22).');
});

test('iaHints: a printed book, a digitised copy, dated by its publication year when it has one', () => {
  assert.deepStrictEqual(ia.iaHints({ year: '1881' }), { sourceType: 'book', sourceClass: 'derivative', sourceDate: '1881' });
  assert.strictEqual(ia.iaHints({ year: null }).sourceDate, null);
});

// ── the requests ─────────────────────────────────────────────────────────────

const resp = (body, extra) => Object.assign({
  ok: true, status: 200, url: 'https://archive.org/x', headers: { get: () => 'application/json' },
  json: async () => body, text: async () => String(body), blob: async () => new Blob([body]),
  arrayBuffer: async () => body,
}, extra || {});

function site(log) {
  const idx = Buffer.from(zlib.gzipSync(JSON.stringify(json('archiveorg-hocr-pageindex.json'))));
  return async (url, init) => {
    log.push({ url, init });
    if (/\/metadata\//.test(url)) return resp(json('archiveorg-metadata.json'));
    if (/_page_numbers\.json$/.test(url)) return resp(json('archiveorg-page-numbers.json'));
    if (/_hocr_pageindex\.json\.gz$/.test(url)) return resp(idx.buffer.slice(idx.byteOffset, idx.byteOffset + idx.byteLength));
    if (/_hocr\.html$/.test(url)) return resp(read('archiveorg-hocr-leaf20.html'), { status: 206 });
    if (/^https:\/\/iiif\.archive\.org\//.test(url)) {
      return resp(Buffer.alloc(60 * 1024), { url: 'https://ia800000.us.archive.org/x.jpg', headers: { get: () => 'image/jpeg' } });
    }
    return resp('', { ok: false, status: 404 });
  };
}

test('a page is four small requests: description, page numbers, text index, and one range of the OCR', async () => {
  const log = [];
  const d = { fetch: site(log) };
  const meta = await ia.iaFetchMetadata(ID, d);
  assert.strictEqual(meta.ok, true);
  const nums = await ia.iaFetchPageNumbers(ID, meta.files, d);
  assert.strictEqual(ia.iaPrintedFor(nums.pages, 20), '10');
  const index = await ia.iaFetchPageIndex(ID, meta.files, d);
  assert.strictEqual(index.ok, true);
  const text = await ia.iaFetchLeafText(ID, index.index, 20, d);
  assert.strictEqual(text.text, '10\n\nHISTORY OF AMADOR COUNTY, CALIFORNIA.');
  const range = log[log.length - 1];
  assert.match(range.url, /_hocr\.html$/);
  assert.strictEqual(range.init.headers.Range, 'bytes=' + json('archiveorg-hocr-pageindex.json')[20][2] + '-' +
    (json('archiveorg-hocr-pageindex.json')[20][3] - 1));
  // no cookies and no referrer, on every request
  for (const l of log) {
    assert.strictEqual(l.init.credentials, 'omit');
    assert.strictEqual(l.init.referrerPolicy, 'no-referrer');
  }
});

test('the whole book is never downloaded: a server that ignores the range is not read', async () => {
  const log = [];
  const base = site(log);
  let cancelled = false;
  const d = { fetch: async (u, i) => (/_hocr\.html$/.test(u)
    ? resp('x'.repeat(10), { status: 200, body: { cancel: () => { cancelled = true; } }, text: async () => { throw new Error('read the whole book'); } })
    : base(u, i)) };
  const meta = await ia.iaFetchMetadata(ID, d);
  const index = await ia.iaFetchPageIndex(ID, meta.files, d);
  const text = await ia.iaFetchLeafText(ID, index.index, 20, d);
  assert.strictEqual(text.ok, false);
  assert.match(text.error, /just this page/);
  assert.ok(cancelled);
});

test('an item with no page numbers or no text index says so without failing', async () => {
  const d = { fetch: site([]) };
  assert.deepStrictEqual(await ia.iaFetchPageNumbers(ID, [], d), { ok: true, pages: null });
  assert.deepStrictEqual(await ia.iaFetchPageIndex(ID, [{ name: ID + '_hocr.html' }], d), { ok: true, index: null });
});

test('the image: full size from archive.org\'s image service; small, wrong type, another host and borrowed books are refused', async () => {
  const log = [];
  const d = { fetch: site(log) };
  const got = await ia.iaFetchImage(ID, 21, d);
  assert.strictEqual(got.ok, true);
  assert.strictEqual(got.ext, 'jpg');
  assert.strictEqual(log[0].url, 'https://iiif.archive.org/iiif/' + ID + '$21/full/full/0/default.jpg');
  const bad = async (r) => ia.iaFetchImage(ID, 1, { fetch: async () => r });
  const small = await bad(resp(Buffer.alloc(100), { headers: { get: () => 'image/jpeg' } }));
  assert.match(small.error, /small picture/);
  assert.match((await bad(resp('<html>', { headers: { get: () => 'text/html' } }))).error, /page instead of the image/);
  assert.match((await bad(resp(Buffer.alloc(60000), { url: 'https://evil.example/x.jpg', headers: { get: () => 'image/jpeg' } }))).error,
    /outside archive\.org/);
  const borrowed = await bad(resp('', { ok: false, status: 403 }));
  assert.match(borrowed.error, /without borrowing it/);
  const slow = await bad(resp('', { ok: false, status: 429 }));
  assert.strictEqual(slow.slowDown, true);
  assert.match(slow.error, /slow down/);
});

test('a description that is not a book, or an item that is gone, is refused with a reason', async () => {
  const audio = await ia.iaFetchMetadata('radio01', { fetch: async () => resp({ metadata: { title: 'x', mediatype: 'audio' } }) });
  assert.match(audio.error, /not a book/);
  const gone = await ia.iaFetchMetadata('gone01', { fetch: async () => resp({}) });
  assert.match(gone.error, /no item called gone01/);
  const down = await ia.iaFetchMetadata('x01', { fetch: async () => { throw new TypeError('Failed to fetch'); } });
  assert.strictEqual(down.transient, true);
  assert.strictEqual((await ia.iaFetchMetadata('../x', { fetch: async () => resp({}) })).ok, false);
});
