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

const OCR = 'HARTLEY - Calvin Hartley, 71, of Fairview, died Tuesday. Café owner for 40 years; “beloved”.';
const ocrBytes = Buffer.byteLength(OCR, 'utf8').toString(16);

const clipping = {
  clipping_id: 123456,
  page_id: 987654,
  title: 'Obituary for Calvin Hartley {1931}',
  tags: 'hartley, obituary',
  rectangle: { x: 100.4, y: 200.6, width: 500, height: 1300, rotation: 0 },
  rectangles: [{ x: 100.4, y: 200.6, width: 500, height: 1300 }],
  user_id: 42,
  user_username: 'otherclipper',
  created: '2025-01-02T03:04:05Z',
  page_date: '1931-03-03',
  page_title: '5',
  page_width: 4463,
  page_height: 7239,
  publication: { name: 'The Fairview Gazette', location: 'Fairview, Kansas' },
  url: '/article/the-fairview-gazette-obituary-for-calvin-hartley/123456/',
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

// One usable clipping area, for clippings built by hand below.
const area = { x: 1, y: 2, w: 30, h: 40, rotation: 0 };

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
  assert.strictEqual(k('https://www.newspapers.com/search/?query=hartley'), null);
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
  assert.strictEqual(raw.title, 'Obituary for Calvin Hartley {1931}');
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
    'https://www.newspapers.com/article/the-fairview-gazette-obituary-for-calvin-hartley/123456/');
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
  const theirs = { id: 1, userId: 42, username: 'otherclipper', rects: [area] };
  const mine1 = { id: 2, userId: 7, username: 'MeMember', rects: [area] };
  const mine2 = { id: 3, userId: 7, username: 'MeMember', rects: [area] };
  const stranger = { id: 4, userId: 99, username: 'someone', rects: [area] };
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
  r = np.npOffered([], { id: 5, userId: 1, username: '', rects: [area] }, me);
  assert.strictEqual(r.focusOwner, 'another member');
});


test('npOffered never offers a clipping with nothing to cut out, and names it', () => {
  // A deleted or damaged list entry normalizes to rects: [] (npClip drops
  // zero-size rectangles). Offering it would download the whole page and then
  // fail at the cut, so it is left out and reported instead.
  const me = { id: 7, username: 'HartleyResearch' };
  const good = { id: 2, userId: 7, username: 'HartleyResearch', title: 'Farm sale', rects: [area] };
  const empty = { id: 3, userId: 7, username: 'HartleyResearch', title: 'Card of thanks', rects: [] };
  const broken = np.npClip({ clipping_id: 4, user_id: 7, title: 'Gone', rectangles: [{ width: 0, height: 0 }] });
  const stranger = { id: 9, userId: 99, username: 'someone', title: 'Not mine', rects: [] };

  let r = np.npOffered([good, empty, broken, stranger], null, me);
  assert.deepStrictEqual(r.offered.map((c) => c.id), [2]);
  assert.deepStrictEqual(r.unusable, [{ id: 3, title: 'Card of thanks' }, { id: 4, title: 'Gone' }]);
  assert.strictEqual(r.openedUnusable, false);
  assert.strictEqual(r.otherClipCount, 1, 'only the other member\'s clipping is "not yours"');

  // The opened clipping itself has no area: nothing about it is offered, and
  // the panel is told so rather than "this was made by someone else".
  const opened = { id: 5, userId: 42, username: 'otherclipper', title: 'Obituary', rects: [] };
  r = np.npOffered([opened, good], opened, me);
  assert.deepStrictEqual(r.offered.map((c) => c.id), [2]);
  assert.strictEqual(r.openedUnusable, true);
  assert.strictEqual(r.focusOwner, null);
  assert.deepStrictEqual(r.unusable, [{ id: 5, title: 'Obituary' }]);

  assert.strictEqual(np.npUsable(good), true);
  assert.strictEqual(np.npUsable(empty), false);
  assert.strictEqual(np.npUsable({ id: 1 }), false);
  assert.strictEqual(np.npUsable(null), false);
});

test('npPageNo keeps the page label only', () => {
  assert.strictEqual(np.npPageNo('4'), '4');
  assert.strictEqual(np.npPageNo('Page 4'), '4');
  assert.strictEqual(np.npPageNo('page4'), '4');
  assert.strictEqual(np.npPageNo('p. 12'), '12');
  assert.strictEqual(np.npPageNo('Pg. 3'), '3');
  assert.strictEqual(np.npPageNo(' A3 '), 'A3');
  assert.strictEqual(np.npPageNo('Page'), 'Page');
  assert.strictEqual(np.npPageNo(''), null);
  assert.strictEqual(np.npPageNo(null), null);
  assert.strictEqual(np.npPageNo(7), '7');
  // ...so a title never reads "p. Page 5".
  const c = np.npClip(Object.assign({}, clipping, { page_title: 'Page 5' }));
  assert.strictEqual(c.pageNo, '5');
});

// ── lib/newspapers.js (panel side, the words) ────────────────────────────────

const info = {
  pageId: 987654, pageDate: '1931-03-03', pageNo: '5',
  publication: { name: 'The Fairview Gazette', location: 'Fairview, Kansas' },
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
    'Obituary for Calvin Hartley {1931} - The Fairview Gazette, 3 Mar 1931, p. 5');
  assert.deepStrictEqual(lib.caption(clip, info), [
    'Obituary for Calvin Hartley {1931}',
    'The Fairview Gazette (Fairview, Kansas)  ·  3 Mar 1931  ·  page 5  ·  Newspapers.com',
  ]);
  // A bare list entry on an unknown page still gets a usable title.
  assert.strictEqual(lib.title({ title: '' }, {}), 'Newspapers.com clipping');
});

test('citations reproduce the site\'s four styles', () => {
  const c = lib.citations(clip, info, '2026-10-08');
  const url = 'https://www.newspapers.com/article/the-fairview-gazette-obituary-for-calvin-hartley/123456/';
  assert.strictEqual(c.genealogy,
    'The Fairview Gazette, March 3, 1931, Page 5. via Newspapers.com (' + url +
    ' : accessed October 8, 2026), clip page for Obituary for Calvin Hartley {1931} by user otherclipper');
  assert.strictEqual(c.apa,
    'The Fairview Gazette. (March 3, 1931). Obituary for Calvin Hartley {1931}. Newspapers.com. ' +
    'Retrieved October 8, 2026, from ' + url);
  assert.strictEqual(c.mla,
    '"Obituary for Calvin Hartley {1931}" Newspapers.com, The Fairview Gazette, March 3, 1931, ' + url);
  assert.strictEqual(c.chicago,
    '"Obituary for Calvin Hartley {1931}" Newspapers.com. The Fairview Gazette, March 3, 1931. ' + url + '.');
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

// ── fetchTile: only "slow down" answers are worth waiting for ────────────────

// A scripted tile service: each call takes the next answer - an HTTP status,
// 'net' for a network failure, or 'ok' for an image.
function tileService(answers) {
  const calls = { fetches: 0, sleeps: [] };
  const deps = {
    async fetch() {
      const a = answers[Math.min(calls.fetches++, answers.length - 1)];
      if (a === 'net') throw new TypeError('Failed to fetch');
      if (a === 'ok') {
        return { ok: true, status: 200, blob: async () => ({ type: 'image/jpeg' }) };
      }
      return { ok: false, status: a, headers: { get: () => null } };
    },
    async sleep(ms) { calls.sleeps.push(ms); },
  };
  return { deps, calls };
}

test('fetchTile stops at once on a refusal that waiting cannot fix', async () => {
  for (const status of [400, 404, 410, 500, 502]) {
    const { deps, calls } = tileService([status]);
    await assert.rejects(lib.fetchTile('u', null, deps), (e) => {
      assert.match(e.message, new RegExp('HTTP ' + status));
      assert.doesNotMatch(e.message, /slow down/);
      assert.match(e.message, /Nothing was saved for this page/);
      return true;
    });
    assert.strictEqual(calls.fetches, 1, 'HTTP ' + status + ' is asked once');
    assert.deepStrictEqual(calls.sleeps, [], 'HTTP ' + status + ' earns no backoff');
  }
});

test('fetchTile says a network failure is a network failure, not a slow-down', async () => {
  const { deps, calls } = tileService(['net']);
  await assert.rejects(lib.fetchTile('u', null, deps), (e) => {
    assert.match(e.message, /could not reach Newspapers\.com/);
    assert.match(e.message, /internet connection/);
    assert.doesNotMatch(e.message, /slow down/);
    return true;
  });
  assert.deepStrictEqual(calls.sleeps, []);
});

test('fetchTile stops at once when the image pass is refused', async () => {
  const { deps, calls } = tileService([403]);
  await assert.rejects(lib.fetchTile('u', null, deps), /HTTP 403\)\. Reload the page/);
  assert.deepStrictEqual(calls.sleeps, []);
});

test('fetchTile backs off on 429 and 503, then gets the tile', async () => {
  const { deps, calls } = tileService([429, 503, 'ok']);
  const waits = [];
  const blob = await lib.fetchTile('u', (s, status) => waits.push(status), deps);
  assert.strictEqual(blob.type, 'image/jpeg');
  assert.deepStrictEqual(waits, [429, 503]);
  assert.strictEqual(calls.sleeps.length, 2);
  // About one minute, then about two (randomized 0.7-1.3x).
  assert.ok(calls.sleeps[0] >= 42000 && calls.sleeps[0] <= 78000, String(calls.sleeps[0]));
  assert.ok(calls.sleeps[1] >= 84000 && calls.sleeps[1] <= 156000, String(calls.sleeps[1]));
});

test('fetchTile gives up after three backoffs and says the site asked us to slow down', async () => {
  const { deps, calls } = tileService([429]);
  await assert.rejects(lib.fetchTile('u', null, deps), /kept asking us to slow down \(HTTP 429\)/);
  assert.strictEqual(calls.fetches, 4);
  assert.strictEqual(calls.sleeps.length, 3);
});

test('fullPage counts the "slow down" answers it waited out, so a batch can stop after the page', async () => {
  // A page of two tiles; the first answers 429 once, then comes.
  const { deps, calls } = tileService([429, 'ok', 'ok']);
  const drawn = [];
  Object.assign(deps, {
    canvas: (w, h) => ({ width: w, height: h, getContext: () => ({ fillRect() {}, drawImage: (b, x, y) => drawn.push([x, y]) }) }),
    bitmap: async () => ({ width: 1488, height: 1448 }),
  });
  const info = { pageId: 'rate-test-1', pageWidth: 2976, pageHeight: 1448, token: 't', me: { id: 7 } };
  const page = await lib.fullPage(info, null, deps);
  assert.strictEqual(page.rateHits, 1);
  assert.strictEqual(page.tiles, 2);
  assert.strictEqual(calls.fetches, 3);
  assert.deepStrictEqual(drawn, [[0, 0], [1488, 0]]);
  // The same page again is the stitched one: no request, nothing waited out.
  const again = await lib.fullPage(info, null, deps);
  assert.strictEqual(again.rateHits, 0);
  assert.strictEqual(calls.fetches, 3);
  assert.strictEqual(again.canvas, page.canvas);
  // A clean page counts none.
  const clean = tileService(['ok']);
  Object.assign(clean.deps, { canvas: deps.canvas, bitmap: deps.bitmap });
  const p2 = await lib.fullPage(Object.assign({}, info, { pageId: 'rate-test-2' }), null, clean.deps);
  assert.strictEqual(p2.rateHits, 0);
});

// ── a clipping with nothing to cut out ───────────────────────────────────────

test('clipCanvas names a clipping with no area instead of failing as a canvas error', () => {
  assert.throws(() => lib.clipCanvas({ canvas: null }, { id: 3, title: 'Card of thanks', rects: [] }),
    /"Card of thanks" marks no area on the page/);
  assert.throws(() => lib.clipCanvas({ canvas: null }, { id: 3, title: '' }), /"3" marks no area/);
});

// ── which bundle gets the page copy ──────────────────────────────────────────

test('pageCopyFits: on a clipping page only that clipping; on the page viewer every one', () => {
  const article = { kind: 'article', focusClipId: 101 };
  assert.strictEqual(lib.pageCopyFits(article, { id: 101 }), true);
  assert.strictEqual(lib.pageCopyFits(article, { id: 102 }), false);
  assert.strictEqual(lib.pageCopyFits({ kind: 'image', focusClipId: null }, { id: 102 }), true);
  assert.strictEqual(lib.pageCopyFits({ kind: 'article', focusClipId: null }, { id: 102 }), false);
});

// ── the closing report ───────────────────────────────────────────────────────

test('summarizeStaged: one clipping reads like any other capture', () => {
  let s = lib.summarizeStaged([{ title: 'T', ok: true, viaHost: false, where: '/dl/fha-inbox/t' }], [null]);
  assert.deepStrictEqual(s, { text: 'Staged to /dl/fha-inbox/t', cls: 'ok', showHandoff: true });
  s = lib.summarizeStaged([{ title: 'T', ok: true, viaHost: true, where: 'inbox/t' }], []);
  assert.deepStrictEqual(s, { text: 'Filed straight into your archive: inbox/t', cls: 'ok', showHandoff: false });
  s = lib.summarizeStaged([{ title: 'T', ok: true, viaHost: false, where: 'd', hostWarning: 'H' }],
    ['soft tiles']);
  assert.strictEqual(s.text, 'Staged to d\nH\nsoft tiles');
  assert.strictEqual(s.cls, 'warn');
});

test('summarizeStaged: every bundle says where it went, and a failure never hides the saved ones', () => {
  const s = lib.summarizeStaged([
    { title: 'Farm sale', ok: true, viaHost: true, where: 'inbox/farm-sale' },
    { title: 'Visit', ok: true, viaHost: false, where: '/dl/fha-inbox/visit', hostWarning: 'H' },
    { title: 'Card', ok: false, error: 'the download was blocked' },
  ], []);
  assert.strictEqual(s.text, [
    'Saved 2 of 3 clippings, each as its own source:',
    '- "Farm sale": filed straight into your archive (inbox/farm-sale)',
    '- "Visit": staged to /dl/fha-inbox/visit',
    '- "Card": NOT saved - the download was blocked',
    'The clipping that was not saved is still ticked - press Capture to try again.',
    'H',
  ].join('\n'));
  assert.strictEqual(s.cls, 'warn');
  assert.strictEqual(s.showHandoff, true);
});

test('summarizeStaged: all saved to the archive, or none saved at all', () => {
  let s = lib.summarizeStaged([
    { title: 'A', ok: true, viaHost: true, where: 'inbox/a' },
    { title: 'B', ok: true, viaHost: true, where: 'inbox/b' },
  ]);
  assert.match(s.text, /^Saved all 2 clippings/);
  assert.strictEqual(s.cls, 'ok');
  assert.strictEqual(s.showHandoff, false);
  s = lib.summarizeStaged([
    { title: 'A', ok: false, error: 'x' },
    { title: 'B', ok: false, error: 'y' },
  ]);
  assert.match(s.text, /^Nothing was saved:/);
  assert.match(s.text, /The 2 clippings that were not saved are still ticked/);
  assert.strictEqual(s.cls, 'error');
  assert.strictEqual(s.showHandoff, false);
});

// ── Retry-After is a minimum (#211 review) ────────────────────────────────────

test('retryAfterSeconds reads both forms of the header', () => {
  assert.strictEqual(lib.retryAfterSeconds('120'), 120);
  const now = Date.parse('2026-11-06T12:00:00Z');
  assert.strictEqual(lib.retryAfterSeconds('Fri, 06 Nov 2026 12:10:00 GMT', now), 600);
  assert.strictEqual(lib.retryAfterSeconds('not a date', now), 0);
  assert.strictEqual(lib.retryAfterSeconds(null, now), 0);
});

test('fetchTile never retries before the site\'s Retry-After', async () => {
  const calls = { fetches: 0, sleeps: [] };
  const deps = {
    async fetch() {
      if (calls.fetches++ === 0) {
        return { ok: false, status: 429, headers: { get: (h) => (h === 'retry-after' ? '120' : null) } };
      }
      return { ok: true, status: 200, blob: async () => ({ type: 'image/jpeg' }) };
    },
    async sleep(ms) { calls.sleeps.push(ms); },
  };
  for (let i = 0; i < 20; i++) {
    calls.fetches = 0;
    calls.sleeps = [];
    await lib.fetchTile('u', null, deps);
    assert.ok(calls.sleeps[0] >= 120000, 'waited ' + calls.sleeps[0] + ' ms');
  }
});

test('Codex 4503849177: a page that met a "slow down" and then lost a later tile says it met one', async () => {
  // Three tiles: the first answers 429 once and then comes; the second
  // comes; the third's connection drops.
  const { deps } = tileService([429, 'ok', 'ok', 'net']);
  Object.assign(deps, {
    canvas: (w, h) => ({ width: w, height: h, getContext: () => ({ fillRect() {}, drawImage() {} }) }),
    bitmap: async () => ({ width: 1488, height: 1448 }),
  });
  const info = { pageId: 'rate-test-lost', pageWidth: 1488 * 3, pageHeight: 1448, token: 't', me: { id: 7 } };
  await assert.rejects(lib.fullPage(info, null, deps), (e) => {
    assert.match(e.message, /could not reach Newspapers\.com/);
    assert.strictEqual(e.rateHits, 1);
    assert.strictEqual(e.slowDown, true);
    return true;
  });
  // A page that lost a tile with no "slow down" before it says none.
  const clean = tileService(['net']);
  Object.assign(clean.deps, { canvas: deps.canvas, bitmap: deps.bitmap });
  await assert.rejects(lib.fullPage(Object.assign({}, info, { pageId: 'rate-test-lost-2' }), null, clean.deps), (e) => {
    assert.strictEqual(e.rateHits, 0);
    assert.strictEqual(e.slowDown, false);
    return true;
  });
});
