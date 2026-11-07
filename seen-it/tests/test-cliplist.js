// test-cliplist.js - keeping the list of your own Newspapers.com clippings.
// (PR #212 review: a long list must not be cut short by a full refresh.)
const test = require('node:test');
const assert = require('node:assert');
const { refreshClipList } = require('../cliplist.js');

// A pretend clippings list, newest first, 24 to a page like the site's.
function site(total) {
  const all = [];
  for (let i = total; i >= 1; i--) all.push({ clipping_id: i, page_id: 9000 + (i % 50), title: 'Hartley ' + i, url: '/article/x/' + i + '/', user_username: 'hartleyfan' });
  const calls = [];
  const fetchPage = async (cursor) => {
    calls.push(cursor);
    const start = Number(cursor) || 0;
    const batch = all.slice(start, start + 24);
    return { clippings: batch, more_results: start + 24 < all.length, next_cursor_mark: String(start + 24) };
  };
  return { all, calls, fetchPage };
}
const count = (clips) => Object.values(clips).reduce((n, l) => n + l.length, 0);

test('a full refresh that hits the page limit keeps every clipping already known', async () => {
  const s = site(100);
  // everything was known before (an earlier, complete read)
  const first = await refreshClipList({ full: true, stored: {}, meta: {}, username: 'hartleyfan', fetchPage: s.fetchPage, maxPages: 10 });
  assert.strictEqual(count(first.clips), 100);
  assert.ok(first.meta.clipsFullAt);
  // a later full refresh can read only 2 pages (48) before its limit
  const r = await refreshClipList({ full: true, stored: first.clips, meta: { clipsFullAt: 1 }, username: 'hartleyfan', fetchPage: s.fetchPage, maxPages: 2 });
  assert.strictEqual(count(r.clips), 100, 'the 52 older clippings are not forgotten');
  assert.strictEqual(r.meta.clipsFullAt, 1, 'not marked as fully refreshed');
  assert.strictEqual(r.meta.clipsFullResume, '48');
  assert.strictEqual(r.complete, false);
});

test('the next full refresh carries on where the last one stopped, and then finishes', async () => {
  const s = site(100);
  let stored = {};
  let meta = { fullRefreshRequested: true };
  let rounds = 0;
  for (;;) {
    const r = await refreshClipList({ full: true, stored, meta, username: 'hartleyfan', fetchPage: s.fetchPage, maxPages: 2 });
    stored = r.clips;
    meta = r.meta;
    rounds++;
    if (r.complete) break;
    assert.ok(rounds < 10);
  }
  assert.strictEqual(rounds, 3);
  assert.strictEqual(count(stored), 100);
  assert.ok(meta.clipsFullAt);
  assert.strictEqual(meta.fullRefreshRequested, undefined);
  assert.strictEqual(meta.clipsFullResume, undefined);
  assert.deepStrictEqual(s.calls, ['0', '24', '48', '72', '96'].map((c, i) => (i ? c : 0)));
});

test('a full refresh that reads the whole list still drops a clipping deleted on the site', async () => {
  const s = site(30);
  const stored = { 1: [{ id: 999, title: 'deleted on the site', url: null, created: null }] };
  const r = await refreshClipList({ full: true, stored, meta: {}, username: 'hartleyfan', fetchPage: s.fetchPage });
  assert.strictEqual(count(r.clips), 30);
  assert.ok(!Object.values(r.clips).some((l) => l.some((c) => c.id === 999)));
});

test("an incremental refresh stops at the first page with nothing new, and skips others' clippings", async () => {
  const s = site(60);
  s.all.unshift({ clipping_id: 61, page_id: 1, title: 'not mine', user_username: 'someone-else' });
  const known = await refreshClipList({ full: true, stored: {}, meta: {}, username: 'hartleyfan', fetchPage: s.fetchPage });
  assert.strictEqual(count(known.clips), 60);
  s.calls.length = 0;
  const r = await refreshClipList({ full: false, stored: known.clips, meta: known.meta, username: 'HartleyFan', fetchPage: s.fetchPage });
  assert.deepStrictEqual(s.calls, [0]);
  assert.strictEqual(count(r.clips), 60);
});

// PR #212 review: a newspaper page with more than 100 members' clippings is
// read in part; a part must never make your own known clippings disappear.
test('a page read cut short keeps your clippings already known; a complete read may drop one', async () => {
  const { readPageClips, mergePageClips } = require('../cliplist.js');
  const crowded = async (start) => ({ clippings: Array.from({ length: 25 }, (_, i) => ({ clipping_id: start + i })) });
  const part = await readPageClips(crowded, null, 4);
  assert.strictEqual(part.all.length, 100);
  assert.strictEqual(part.complete, false);
  const short = await readPageClips(async (start) => ({ clippings: start ? [{ clipping_id: 99 }] : Array(25).fill({ clipping_id: 1 }) }), null, 4);
  assert.strictEqual(short.complete, true);

  const stored = { 9001: [{ id: 501, title: 'Hartley wedding' }], 9002: [{ id: 502, title: 'other page' }] };
  // the first 100 were all other members': nothing of yours was seen
  assert.deepStrictEqual(mergePageClips(stored, 9001, [], false), stored);
  // a part that found a new one of yours adds it, keeping the old one
  const added = mergePageClips(stored, 9001, [{ id: 503, title: 'Hartley obituary' }], false);
  assert.deepStrictEqual(added['9001'].map((c) => c.id), [501, 503]);
  // a complete read is the truth for that page, and only that page
  assert.deepStrictEqual(mergePageClips(stored, 9001, [{ id: 503 }], true)['9001'].map((c) => c.id), [503]);
  const gone = mergePageClips(stored, 9001, [], true);
  assert.ok(!('9001' in gone));
  assert.deepStrictEqual(gone['9002'], stored['9002']);
});

test('a list pass merges into the list as it is now, unless it read the whole list', async () => {
  const { refreshClipList, mergeClipList } = require('../cliplist.js');
  const s = site(30);
  const latest = { 1: [{ id: 999, title: 'saved while the pass was reading' }] };
  const inc = await refreshClipList({ full: false, stored: {}, meta: {}, username: 'hartleyfan', fetchPage: s.fetchPage });
  assert.strictEqual(inc.replace, false);
  assert.ok(mergeClipList(latest, inc)['1'].some((c) => c.id === 999));
  const full = await refreshClipList({ full: true, stored: {}, meta: {}, username: 'hartleyfan', fetchPage: s.fetchPage });
  assert.strictEqual(full.replace, true);
  assert.deepStrictEqual(full.changes.clear, ['fullRefreshRequested', 'clipsFullResume']);
  assert.ok(full.changes.set.clipsFullAt);
});
