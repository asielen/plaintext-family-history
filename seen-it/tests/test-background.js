// test-background.js - the service worker (background.js), run under a
// pretend Chrome (harness.js). Fictional Hartley family data only.
const test = require('node:test');
const assert = require('node:assert');
const { load } = require('./harness.js');

const DAY = 24 * 60 * 60 * 1000;
const MIN = 60 * 1000;
const looksOf = (v) => Math.max(v && v.t ? v.t.length : 0, (v && v.n) || 0);
const SID = 'S-0a1b2c3d4e';

// ── PR #212 review: looks after a count imported from another computer ──
test('a page seen 10 times on another computer counts on from 10 here', async () => {
  const h = load();
  const today = Math.floor(Date.now() / DAY).toString(36);
  await h.sync.set({ nsiV0: '101,10,' + today + ',\n' }); // Chrome Sync delivers the summary
  await h.settle();
  assert.strictEqual(looksOf((await h.local.get('views')).views['101']), 10);
  const now = Date.now();
  await h.context.recordLook('101', now);
  await h.context.recordLook('101', now + 40 * MIN); // a later sitting
  await h.context.recordLook('101', now + 41 * MIN); // same sitting: not a new look
  await h.context.recordLook('101', now + 90 * MIN);
  assert.strictEqual(looksOf((await h.local.get('views')).views['101']), 13);
});

test('a page looked at more often than the look times kept still counts every look', async () => {
  const h = load();
  const t0 = Date.now() - 400 * 40 * MIN;
  for (let i = 0; i < 205; i++) await h.context.recordLook('202', t0 + i * 40 * MIN);
  const v = (await h.local.get('views')).views['202'];
  assert.strictEqual(v.t.length, 200);
  assert.strictEqual(looksOf(v), 205);
});

// ── PR #212 review: a newer tree file's removals survive a merge ─────────
test("an older tree file from another computer does not bring back people a newer one dropped", async () => {
  const h = load();
  const r = await h.send({ type: 'loadTreeFile', file: 'hartley-2026.ged',
    people: [{ id: 'I1', name: 'Thomas Hartley', keys: ['rec:1001:2002'] }] });
  assert.ok(r.ok, JSON.stringify(r));
  const now = Date.now();
  const olderFile = {
    app: 'newspapers-seen-it', version: 3,
    ancPeople: {
      'ged:I1': { name: 'Thomas Hartley', keys: ['rec:1001:2002'], at: now - DAY },
      'ged:I2': { name: 'Margaret Hartley', keys: ['rec:3003:4004'], at: now - DAY },
      'fs:ABCD-123': { name: 'Ethel Hartley', keys: ['fsrec:EFGH-456'], at: now },
    },
    meta: { treeFile: { name: 'hartley-2025.ged', at: now - DAY, people: 2 } },
  };
  assert.ok((await h.send({ type: 'import', data: olderFile })).ok);
  let { ancPeople, meta } = await h.local.get(['ancPeople', 'meta']);
  assert.deepStrictEqual(Object.keys(ancPeople).sort(), ['fs:ABCD-123', 'ged:I1']);
  assert.strictEqual(meta.treeFile.name, 'hartley-2026.ged');

  // and a NEWER file from the other computer replaces this one's tree people whole
  const newerFile = {
    app: 'newspapers-seen-it', version: 3,
    ancPeople: { 'ged:I9': { name: 'Ruth Hartley', keys: ['rec:5005:6006'], at: now + DAY } },
    meta: { treeFile: { name: 'hartley-2027.ged', at: now + DAY, people: 1 } },
  };
  await h.send({ type: 'import', data: newerFile });
  ({ ancPeople, meta } = await h.local.get(['ancPeople', 'meta']));
  assert.deepStrictEqual(Object.keys(ancPeople).sort(), ['fs:ABCD-123', 'ged:I9']);
  assert.strictEqual(meta.treeFile.name, 'hartley-2027.ged');
});

test('an old backup without a tree-file date is dated by its people', async () => {
  const h = load();
  await h.send({ type: 'loadTreeFile', file: 'new.ged', people: [{ id: 'I1', name: 'A', keys: ['rec:1:2'] }] });
  const old = { app: 'newspapers-seen-it', version: 3,
    ancPeople: { 'ged:I2': { name: 'B', keys: ['rec:3:4'], at: Date.now() - DAY } } };
  await h.send({ type: 'import', data: old });
  assert.deepStrictEqual(Object.keys((await h.local.get('ancPeople')).ancPeople), ['ged:I1']);
});

// ── PR #212 review: Chrome Sync writes survive the worker being stopped ──
test('a look schedules ONE sync alarm, and the alarm pushes even outside Advanced mode', async () => {
  const h = load();
  await h.send({ type: 'look', pageId: '101' });
  await h.settle();
  assert.ok(h.alarms.pushSync, 'an alarm, not a timer that dies with the worker');
  for (let i = 0; i < 5; i++) await h.send({ type: 'dwell', pageId: '101', ms: 30000 }); // reading time, every 30 s
  await h.settle();
  assert.strictEqual(h.alarmLog.filter((n) => n === 'pushSync').length, 1, 'not pushed back by every save');
  assert.ok((await h.local.get('syncPending')).syncPending);
  await h.fireAlarm('pushSync');
  const synced = await h.sync.get(null);
  assert.match(synced.nsiV0 || '', /^101,1,/);
  assert.strictEqual((await h.local.get('syncPending')).syncPending, undefined);
});

test('a push still waiting when the browser closed goes out at the next start', async () => {
  const h = load();
  await h.local.set({ views: { 303: { t: [Date.now()], last: Date.now() } }, syncPending: Date.now() });
  await h.settle();
  for (const k of Object.keys(h.alarms)) delete h.alarms[k]; // the alarm did not survive
  await h.startup();
  assert.match((await h.sync.get(null)).nsiV0 || '', /^303,1,/);
});

// ── audit #3: an archive drive that is not connected ───────────────────────
function helperAnswering(state) {
  return (msg) => {
    if (msg.op !== 'known') return { ok: true };
    const keys = Object.keys(state.items).sort();
    return { ok: true, items: state.items, total: keys.length, next: keys.length, missing: state.missing };
  };
}

test('a documents drive not connected keeps the filed marks instead of "not in archive"', async () => {
  const state = { items: {}, missing: [] };
  const h = load({ native: helperAnswering(state) });
  await h.local.set({ settings: { advanced: true } });
  state.items = {
    'rec:1001:2002': { state: 'filed', where: 'census', sid: SID },
    'clip:7707': { state: 'captured', where: 'inbox' },
  };
  assert.ok((await h.send({ type: 'refreshKnown' })).ok);
  // the drive is unplugged: the helper can read only the inbox
  state.items = { 'clip:7707': { state: 'captured', where: 'inbox' } };
  state.missing = ['/Volumes/Hartley Docs'];
  await h.send({ type: 'refreshKnown' });
  const { known, meta } = await h.local.get(['known', 'meta']);
  assert.strictEqual(known['rec:1001:2002'].state, 'filed');
  assert.deepStrictEqual(meta.knownMissing, ['/Volumes/Hartley Docs']);
  // the capture add-on asking us is told the same, and that the read was partial
  const r = await h.sendExternal({ type: 'knownKeys', prefix: 'rec:' });
  assert.deepStrictEqual(r.keys, { 'rec:1001:2002': 'filed' });
  assert.deepStrictEqual(r.missing, ['/Volumes/Hartley Docs']);
  // with the drive back, what is really gone goes
  state.items = { 'clip:7707': { state: 'captured', where: 'inbox' } };
  state.missing = [];
  await h.send({ type: 'refreshKnown' });
  assert.deepStrictEqual(Object.keys((await h.local.get('known')).known), ['clip:7707']);
});

// ── audit #7: what another add-on may ask ─────────────────────────────────
test('another add-on cannot force a full archive re-read in a loop', async () => {
  const state = { items: { 'rec:1001:2002': { state: 'filed', where: 'census', sid: SID } }, missing: [] };
  const h = load({ native: helperAnswering(state) });
  await h.local.set({ settings: { advanced: true } });
  const reads = () => h.nativeCalls.filter((m) => m.op === 'known').length;
  const first = await h.sendExternal({ type: 'knownKeys', prefix: 'rec:', refresh: true });
  assert.deepStrictEqual(first.keys, { 'rec:1001:2002': 'filed' });
  assert.strictEqual(reads(), 1);
  for (let i = 0; i < 5; i++) await h.sendExternal({ type: 'knownKeys', prefix: 'rec:', refresh: true });
  assert.strictEqual(reads(), 1, 'the last answer, not a new full read');
  // the person's own "Refresh the archive list" is never limited
  await h.send({ type: 'refreshKnown' });
  assert.strictEqual(reads(), 2);
});

test('an add-on that names the keys it cares about is told only about those', async () => {
  const state = { items: {
    'rec:1001:2002': { state: 'filed', where: 'census', sid: SID },
    'rec:3003:4004': { state: 'captured', where: 'inbox' },
    'fsrec:ABCD-123': { state: 'filed', where: 'vital', sid: SID },
  }, missing: [] };
  const h = load({ native: helperAnswering(state) });
  await h.local.set({ settings: { advanced: true } });
  await h.send({ type: 'refreshKnown' });
  const r = await h.sendExternal({ type: 'knownKeys', keys: ['rec:3003:4004', 'rec:9:9', 42] });
  assert.deepStrictEqual(r.keys, { 'rec:3003:4004': 'captured' });
  // the capture add-on's own question (a prefix) still works as before
  const p = await h.sendExternal({ type: 'knownKeys', prefix: 'fsrec:', refresh: false });
  assert.deepStrictEqual(p.keys, { 'fsrec:ABCD-123': 'filed' });
  // and outside Advanced mode nothing is told
  await h.local.set({ settings: { advanced: false } });
  assert.deepStrictEqual((await h.sendExternal({ type: 'knownKeys', prefix: '' })).keys, {});
});
