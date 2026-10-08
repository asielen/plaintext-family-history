// Tests for the Newspapers.com capture: reading the site's embedded page data
// (lib/newspapers-page-data.js) and the words the panel writes from it - titles,
// captions, the site's four citation styles, the tile grid (lib/newspapers.js).
//
// The fixture is synthetic: a page shaped like a real Newspapers.com clipping
// page (Next.js `self.__next_f.push` strings, a `"clipping":{…}` object, an OCR
// text chunk), with invented names and numbers.
//
//   node --test browser-companion/tests/test-newspapers.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const np = require('../src/lib/newspapers-page-data.js');

function loadPanelLib() {
  const w = {};
  const src = fs.readFileSync(path.join(__dirname, '..', 'src', 'lib', 'newspapers.js'), 'utf8');
  new Function('window', src)(w);
  return w.FHA.newspapers;
}
const lib = loadPanelLib();

// ── fixture ──────────────────────────────────────────────────────────────────

const OCR = 'SMITH - John Smith, 71, of Fairview, died Tuesday. Café owner for 40 years; “beloved”.';
const ocrBytes = Buffer.byteLength(OCR, 'utf8').toString(16);

const clipping = {
  clipping_id: 123456,
  page_id: 987654,
  title: 'Obituary for John Smith {1931}',
  tags: 'smith, obituary',
  rectangle: { x: 100.4, y: 200.6, width: 500, height: 1300, rotation: 0 },
  rectangles: [{ x: 100.4, y: 200.6, width: 500, height: 1300 }],
  user_id: 42,
  user_username: 'otherclipper',
  created: '2025-01-02T03:04:05Z',
  page_date: '1931-03-03',
  page_title: '5',
  page_width: 4463,
  page_height: 7239,
  publication: { name: 'The Fairview Gazette', location: 'Fairview, Ohio' },
  url: '/article/the-fairview-gazette-obituary-for-john-smith/123456/',
  page_token: 'eyJhbGciOiJIUzI1NiJ9.eyJwIjo5ODc2NTQsInUiOjd9.sig',
  ocr: '$3a',
};

// Next.js streams its data as JSON-encoded string chunks; the OCR chunk is
// preceded by `}` rather than a newline, as observed on the live site.
const flight =
  '1:{"user":{"id":7,"username":"MeMember"}}\n' +
  '2:{"clipping":' + JSON.stringify(clipping) + '}' +
  '3a:T' + ocrBytes + ',' + OCR +
  '4:{"other":"after the chunk"}\n';
const half = Math.floor(flight.length / 2);
const html =
  '<html><body><script>self.__next_f.push([1,' + JSON.stringify(flight.slice(0, half)) + '])</script>' +
  '<script>self.__next_f.push([1,' + JSON.stringify(flight.slice(half)) + '])</script>' +
  '<script>console.log("not page data")</script></body></html>';
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);

// ── lib/newspapers-page-data.js ──────────────────────────────────────────────

test('npPageKind recognizes clipping pages and the page viewer only', () => {
  const k = np.npPageKind;
  assert.deepStrictEqual(k('https://www.newspapers.com/article/the-gazette-x/123456/'),
    { kind: 'article', clippingId: 123456, pageId: null });
  assert.deepStrictEqual(k('https://www.newspapers.com/article/123456/'),
    { kind: 'article', clippingId: 123456, pageId: null });
  assert.deepStrictEqual(k('https://newspapers.com/clip/42/x/'),
    { kind: 'article', clippingId: 42, pageId: null });
  assert.deepStrictEqual(k('https://www.newspapers.com/image/987654/?clipping_id=5'),
    { kind: 'image', pageId: 987654, clippingId: 5 });
  assert.deepStrictEqual(k('https://www.newspapers.com/image/987654/'),
    { kind: 'image', pageId: 987654, clippingId: null });
  assert.strictEqual(k('https://www.newspapers.com/search/?query=smith'), null);
  assert.strictEqual(k('https://notnewspapers.com.example.org/article/x/1/'), null);
  assert.strictEqual(k('https://www.ancestry.com/image/987654/'), null);
  assert.strictEqual(k('not a url'), null);
});

test('npFlightFrom joins the page-data strings and skips other scripts', () => {
  assert.strictEqual(np.npFlightFrom(scripts), flight);
  assert.strictEqual(np.npFlightFrom([html]), flight);
  assert.strictEqual(np.npFlightFrom(['', null, 'var x = 1;']), '');
});

test('npObjectAt / npObjectAfter pull one object out, braces in strings and all', () => {
  const raw = np.npObjectAfter(flight, 'clipping');
  assert.strictEqual(raw.clipping_id, 123456);
  assert.strictEqual(raw.title, 'Obituary for John Smith {1931}');
  assert.deepStrictEqual(np.npObjectAfter(flight, 'user'), { id: 7, username: 'MeMember' });
  assert.strictEqual(np.npObjectAfter(flight, 'missing'), null);
  assert.strictEqual(np.npObjectAt('{"a":1', 0), null); // unterminated
  assert.strictEqual(np.npObjectAt('{"a":"\\"}"}', 0).a, '"}');
});

test('npJwtPayload reads the page pass, and survives junk', () => {
  assert.deepStrictEqual(np.npJwtPayload(clipping.page_token), { p: 987654, u: 7 });
  assert.strictEqual(np.npJwtPayload('nope'), null);
  assert.strictEqual(np.npJwtPayload(null), null);
});

test('npClip normalizes a clipping and makes its url absolute', () => {
  const c = np.npClip(clipping, (u) => 'https://www.newspapers.com' + u);
  assert.strictEqual(c.id, 123456);
  assert.deepStrictEqual(c.rects, [{ x: 100, y: 201, w: 500, h: 1300, rotation: 0 }]);
  assert.strictEqual(c.url,
    'https://www.newspapers.com/article/the-fairview-gazette-obituary-for-john-smith/123456/');
  assert.strictEqual(c.ocrRef, '3a');
  assert.strictEqual(c.ocr, null);
  assert.strictEqual(c.username, 'otherclipper');
  // A list entry: one rectangle, no page size, inline OCR, a zero-size rect dropped.
  const listed = np.npClip({
    clipping_id: 9, rectangle: { x: -3, y: 4, width: 10, height: 20 },
    rectangles: [], user_id: 7, ocr: 'inline text',
  });
  assert.deepStrictEqual(listed.rects, [{ x: 0, y: 4, w: 10, h: 20, rotation: 0 }]);
  assert.strictEqual(listed.ocr, 'inline text');
  assert.strictEqual(listed.url, null);
  assert.deepStrictEqual(
    np.npClip({ clipping_id: 1, rectangles: [{ x: 0, y: 0, width: 0, height: 5 }] }).rects, []);
});

test('npTextChunk resolves the full OCR by counting UTF-8 bytes', () => {
  assert.strictEqual(np.npTextChunk(flight, '3a'), OCR);
  assert.strictEqual(np.npTextChunk(flight, 'zz'), null);
  assert.strictEqual(np.npTextChunk(flight, null), null);
  // A ref is an id, never a pattern.
  assert.strictEqual(np.npTextChunk(flight, '.*'), null);
  // An astral character counts four bytes and two UTF-16 units.
  const t = '7:T6,a😀b' + 'tail';
  assert.strictEqual(np.npTextChunk(t, '7'), 'a😀b');
  // "13a" must not be found when asking for "3a".
  assert.strictEqual(np.npTextChunk('13a:T2,no}3a:T2,ok', '3a'), 'ok');
});

test('npIsMine matches by user id, else by username (case-insensitive)', () => {
  assert.ok(np.npIsMine({ userId: 7, username: '' }, { id: 7, username: null }));
  assert.ok(np.npIsMine({ userId: 1, username: 'memember' }, { id: null, username: 'MeMember' }));
  assert.ok(!np.npIsMine({ userId: 1, username: 'x' }, { id: 7, username: 'MeMember' }));
  assert.ok(!np.npIsMine({ userId: null, username: '' }, { id: null, username: null }));
});

test('npOffered: the opened clipping whoever made it, plus only your own', () => {
  const me = { id: 7, username: 'MeMember' };
  const theirs = { id: 1, userId: 42, username: 'otherclipper' };
  const mine1 = { id: 2, userId: 7, username: 'MeMember' };
  const mine2 = { id: 3, userId: 7, username: 'MeMember' };
  const stranger = { id: 4, userId: 99, username: 'someone' };
  const clips = [theirs, mine1, mine2, stranger];

  // A clipping page opened on someone else's clipping: it leads, yours follow.
  let r = np.npOffered(clips, theirs, me);
  assert.deepStrictEqual(r.offered.map((c) => c.id), [1, 2, 3]);
  assert.strictEqual(r.otherClipCount, 1);
  assert.strictEqual(r.focusOwner, 'otherclipper');

  // Opened on your own clipping: no owner note, not listed twice.
  r = np.npOffered(clips, mine1, me);
  assert.deepStrictEqual(r.offered.map((c) => c.id), [2, 3]);
  assert.strictEqual(r.focusOwner, null);
  assert.strictEqual(r.otherClipCount, 2);

  // The page viewer: only yours.
  r = np.npOffered(clips, null, me);
  assert.deepStrictEqual(r.offered.map((c) => c.id), [2, 3]);
  assert.strictEqual(r.focusOwner, null);

  // Nobody's yours, nothing opened: nothing offered.
  r = np.npOffered([stranger], null, me);
  assert.deepStrictEqual(r.offered, []);
  assert.strictEqual(r.otherClipCount, 1);

  // An opened clipping with no clipper name still says whose it is not.
  r = np.npOffered([], { id: 5, userId: 1, username: '' }, me);
  assert.strictEqual(r.focusOwner, 'another member');
});

// ── lib/newspapers.js (panel side, the words) ────────────────────────────────

const info = {
  pageId: 987654, pageDate: '1931-03-03', pageNo: '5',
  publication: { name: 'The Fairview Gazette', location: 'Fairview, Ohio' },
};
const clip = np.npClip(clipping, (u) => 'https://www.newspapers.com' + u);

test('dates: short for titles, long for citations, anything else untouched', () => {
  assert.strictEqual(lib.shortDate('1931-03-03'), '3 Mar 1931');
  assert.strictEqual(lib.longDate('1931-03-03'), 'March 3, 1931');
  assert.strictEqual(lib.shortDate('1931'), '1931');
  assert.strictEqual(lib.longDate(null), '');
});

test('title and caption name the clipping, paper, date and page', () => {
  assert.strictEqual(lib.title(clip, info),
    'Obituary for John Smith {1931} - The Fairview Gazette, 3 Mar 1931, p. 5');
  assert.deepStrictEqual(lib.caption(clip, info), [
    'Obituary for John Smith {1931}',
    'The Fairview Gazette (Fairview, Ohio)  ·  3 Mar 1931  ·  page 5  ·  Newspapers.com',
  ]);
  // A bare list entry on an unknown page still gets a usable title.
  assert.strictEqual(lib.title({ title: '' }, {}), 'Newspapers.com clipping');
});

test('citations reproduce the site\'s four styles', () => {
  const c = lib.citations(clip, info, '2026-10-08');
  const url = 'https://www.newspapers.com/article/the-fairview-gazette-obituary-for-john-smith/123456/';
  assert.strictEqual(c.genealogy,
    'The Fairview Gazette, March 3, 1931, Page 5. via Newspapers.com (' + url +
    ' : accessed October 8, 2026), clip page for Obituary for John Smith {1931} by user otherclipper');
  assert.strictEqual(c.apa,
    'The Fairview Gazette. (March 3, 1931). Obituary for John Smith {1931}. Newspapers.com. ' +
    'Retrieved October 8, 2026, from ' + url);
  assert.strictEqual(c.mla,
    '"Obituary for John Smith {1931}" Newspapers.com, The Fairview Gazette, March 3, 1931, ' + url);
  assert.strictEqual(c.chicago,
    '"Obituary for John Smith {1931}" Newspapers.com. The Fairview Gazette, March 3, 1931. ' + url + '.');
});

test('tileGrid covers the page exactly, trimming the last row and column', () => {
  const tiles = lib.tileGrid(4463, 7239);
  assert.strictEqual(tiles.length, 3 * 5); // ceil(4463/1488) x ceil(7239/1448): 15, as live
  let area = 0;
  for (const [x, y, w, h] of tiles) {
    assert.ok(w > 0 && h > 0 && x + w <= 4463 && y + h <= 7239);
    area += w * h;
  }
  assert.strictEqual(area, 4463 * 7239);
  assert.deepStrictEqual(lib.tileGrid(1488, 1448), [[0, 0, 1488, 1448]]);
  assert.deepStrictEqual(lib.tileGrid(1489, 10), [[0, 0, 1488, 10], [1488, 0, 1, 10]]);
});
