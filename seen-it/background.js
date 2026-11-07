// background.js - "Newspapers.com: Seen It"
//
// The single writer for everything the add-on keeps (chrome.storage.local):
//
//   views:    { [pageId]: { t: [lookStartMs, …], n?, last } }
//             How often each newspaper page (/image/{pageId}/) was opened. A
//             "look" starts more than 30 min after the previous visit, so
//             reloads and match-to-match clicks in one sitting count once.
//   pages:    { [pageId]: { paper, date, pageNo, place, dwellMs, fromSearches: [key],
//                           terms: [..], mark: 'nothing'|'later'|null, markAt,
//                           overlaps: [{ id, user, title, url, mine }] } }
//             What each page is, how long it was read, how it was found, the
//             owner's verdict, and other members' clippings that OVERLAP one of
//             the owner's own (unrelated clippings by others are not kept).
//   passes:   { [pageId]: { t: [passStartMs, …], last, keys: [searchKey] } }
//             Search results that sat on screen (60%+ visible for 1.5 s) but
//             were never opened - usually judged from the snippet. Counted once
//             per sitting, like looks. Local and in backups; not synced (bulky).
//   searches: { [key]: { params, url, runs: [{ at, count }], opened: [pageId], shown: [pageId] } }
//             Every search run: its terms and filters, result counts by date,
//             and which pages were opened from it - the research log.
//   clips:    { [pageId]: [{ id, title, url, created }] }   (content.js writes)
//   meta:     { user, clipsAt, clipsFullAt, historyAt, lastSearch, knownAt, … }
//
//   known:    { [key]: { state: 'filed'|'processed'|'captured', sid?, where } }
//             What the family archive already holds, read by the helper (op
//             'known') from capture bundles' data files - never written here.
//             Keys: clip:{id} and page:{id} (Newspapers.com), rec:{dbId}:{recordId},
//             img:{dbId}:{imageId} (Ancestry). Refreshed every 30 min and on demand.
//   anc:      { [rec:… | img:…]: { t: [lookStartMs], last, title, collection, name,
//                                   dwellMs, mark, markAt } }
//             Ancestry records and images you opened, and your verdict on each.
//             Kept apart from the Newspapers.com page data so neither schema
//             changes; it travels in backups and the data-folder files.
//   ancPeople: { ['{treeId}:{personId}' | 'fs:{PID}' | 'ged:{xref}']: { name, keys: ['rec:…'], at } }
//             A tree person's sources, as last seen on their Ancestry Facts page or
//             FamilySearch person page, or read from a GEDCOM file (ged:, replaced
//             whole on each load) - so search results can say "in your tree".
//   syncPending: ms - a Chrome Sync push is waiting (see schedulePushSync).
//   settings: { advanced: bool }
//             Advanced mode: the family-archive integration (the local helper,
//             the known list, the shared data folder). Off by default; the
//             add-on is complete without it.
//
// Durability: Chrome's history expires (~90 days) and can be cleared, so it is
// mined once into this storage, which survives clearing history (it is lost
// only by removing the add-on). A compact copy - per page: looks, last date and
// mark - is kept in chrome.storage.sync, which follows the Chrome account to
// other computers (~100 KB; most recent pages first). Merging takes the larger
// look count, so looks on two computers are not added together.
//
// Two computers, everything (advanced mode): see "the shared data folder"
// below - each computer keeps seen-it-<device>.json in a data folder of your
// choosing and merges the others' files automatically. Files carry the actual
// look times, so merging adds looks from both computers correctly and never
// double-counts.
//
// The manifest's fixed "key" gives this unpacked add-on the same ID on every
// computer - without it, Chrome's synced storage would not be shared, and the
// helper (which answers only that ID) could not be found.

const GAP_MS = 30 * 60 * 1000;
const MAX_LOOKS_KEPT = 200;
const SEARCH_ATTRIBUTION_MS = 3 * 60 * 60 * 1000;

function pageIdFrom(url) {
  const m = /^https:\/\/(?:www\.)?newspapers\.com\/image\/(\d+)/i.exec(String(url || ''));
  return m ? m[1] : null;
}

function looksFrom(times) {
  const ts = [...new Set(times)].filter((t) => t > 0).sort((a, b) => a - b);
  const starts = [];
  let prev = -Infinity;
  for (const t of ts) {
    if (t - prev > GAP_MS) starts.push(t);
    prev = t;
  }
  return starts;
}

function looksOf(v) {
  return Math.max(v && v.t ? v.t.length : 0, (v && v.n) || 0);
}

// One writer: every update is a read-modify-write on the queue, so concurrent
// messages from several tabs never lose each other's changes.
let chain = Promise.resolve();
function update(keys, fn) {
  const run = chain.then(async () => {
    const o = await chrome.storage.local.get(keys);
    for (const k of keys) if (!o[k]) o[k] = {};
    const out = await fn(o);
    await chrome.storage.local.set(o);
    return out;
  });
  chain = run.catch((e) => console.warn('seen-it:', e));
  return run;
}

// ── looks ───────────────────────────────────────────────────────────────────
// One more look at a newspaper page. A page's count is the larger of its
// look times (`t`) and `n`, a plain number for looks this computer has no
// times for: the Chrome Sync summary from another computer carries only a
// count, and `t` keeps at most MAX_LOOKS_KEPT times. Once `n` is in play it
// has to move with every new look too, or a page imported as "seen 10 times"
// would stay at 10 through the next ten visits here.
function addLook(v, when) {
  const before = looksOf(v);
  v.t.push(when);
  if (v.n || v.t.length > MAX_LOOKS_KEPT) v.n = before + 1;
  if (v.t.length > MAX_LOOKS_KEPT) v.t.splice(0, v.t.length - MAX_LOOKS_KEPT);
}

function recordLook(pageId, when, info) {
  return update(['views', 'pages', 'searches', 'meta'], ({ views, pages, searches, meta }) => {
    const v = views[pageId] || { t: [], last: 0 };
    const newLook = !v.t.length || when - (v.last || 0) > GAP_MS;
    if (newLook) addLook(v, when);
    v.last = when;
    views[pageId] = v;

    const p = pages[pageId] || { dwellMs: 0, fromSearches: [], terms: [], mark: null, overlaps: [] };
    if (info) {
      for (const k of ['paper', 'date', 'pageNo', 'place']) if (info[k]) p[k] = info[k];
      if (info.terms && !p.terms.includes(info.terms)) p.terms.push(info.terms);
      // Which search led here: the last search run, if recent and the page was
      // opened from search results (the address carries the search terms).
      const ls = meta.lastSearch;
      if (info.fromSearch && ls && when - ls.at < SEARCH_ATTRIBUTION_MS && searches[ls.key]) {
        if (!p.fromSearches.includes(ls.key)) p.fromSearches.push(ls.key);
        const s = searches[ls.key];
        s.opened = s.opened || [];
        if (!s.opened.includes(pageId)) s.opened.push(pageId);
      }
    }
    pages[pageId] = p;
  });
}

function recordDwell(pageId, ms) {
  if (!(ms > 0) || ms > 6 * 60 * 60 * 1000) return Promise.resolve();
  return update(['pages'], ({ pages }) => {
    const p = pages[pageId] || { dwellMs: 0, fromSearches: [], terms: [], mark: null, overlaps: [] };
    p.dwellMs = (p.dwellMs || 0) + Math.round(ms);
    pages[pageId] = p;
  });
}

function recordMark(pageId, mark) {
  return update(['pages'], ({ pages }) => {
    const p = pages[pageId] || { dwellMs: 0, fromSearches: [], terms: [], mark: null, overlaps: [] };
    p.mark = mark === 'nothing' || mark === 'later' ? mark : null;
    p.markAt = Date.now();
    pages[pageId] = p;
    return p.mark;
  });
}

function recordOverlaps(pageId, overlaps) {
  return update(['pages'], ({ pages }) => {
    const p = pages[pageId] || { dwellMs: 0, fromSearches: [], terms: [], mark: null, overlaps: [] };
    p.overlaps = overlaps || [];
    pages[pageId] = p;
  });
}

// ── Ancestry records and images ─────────────────────────────────────────────
function recordAncLook(key, when, info) {
  return update(['anc'], ({ anc }) => {
    const a = anc[key] || { t: [], last: 0, mark: null };
    if (!a.t.length || when - (a.last || 0) > GAP_MS) a.t.push(when);
    a.last = when;
    if (a.t.length > MAX_LOOKS_KEPT) a.t.splice(0, a.t.length - MAX_LOOKS_KEPT);
    if (info) for (const k of ['title', 'collection', 'name']) if (info[k]) a[k] = String(info[k]).slice(0, 200);
    anc[key] = a;
  });
}

function recordAncDwell(key, ms) {
  if (!(ms > 0) || ms > 6 * 60 * 60 * 1000) return Promise.resolve();
  return update(['anc'], ({ anc }) => {
    const a = anc[key] || { t: [], last: 0, mark: null };
    a.dwellMs = (a.dwellMs || 0) + Math.round(ms);
    anc[key] = a;
  });
}

function recordAncMark(key, mark) {
  return update(['anc'], ({ anc }) => {
    const a = anc[key] || { t: [], last: 0, mark: null };
    a.mark = mark === 'nothing' || mark === 'later' ? mark : null;
    a.markAt = Date.now();
    anc[key] = a;
    return a.mark;
  });
}

function recordAncPerson(key, name, keys) {
  return update(['ancPeople'], ({ ancPeople }) => {
    ancPeople[key] = { name: name ? String(name).slice(0, 120) : null, keys: [...new Set(keys || [])], at: Date.now() };
  });
}

// ── settings ────────────────────────────────────────────────────────────────
async function isAdvanced() {
  const { settings = {} } = await chrome.storage.local.get('settings');
  return !!settings.advanced;
}

async function setAdvanced(on) {
  await update(['settings'], ({ settings }) => { settings.advanced = !!on; });
  if (!on) return { advanced: false };
  scheduleFolderSync();
  const out = { advanced: true };
  // The archive scan can take minutes on a cold disk. Answer the popup within
  // 20 seconds either way; a scan still running finishes in the background and
  // its count (or error) shows in the popup's archive line when it lands.
  const scan = refreshKnown();
  scan.catch(() => {});
  const timeout = new Promise((resolve) => setTimeout(() => resolve('pending'), 20000));
  try {
    const r = await Promise.race([scan, timeout]);
    if (r === 'pending') out.pending = true;
    else out.known = r.total;
  } catch (e) {
    out.error = e.message;
  }
  syncFolder(false).catch(() => {});
  return out;
}

// A tree file (GEDCOM) replaces every earlier one: its people are kept as ged:{xref}.
function loadTreeFile(people, file) {
  return update(['ancPeople', 'meta'], ({ ancPeople, meta }) => {
    for (const k of Object.keys(ancPeople)) if (k.startsWith('ged:')) delete ancPeople[k];
    let records = 0;
    for (const p of people || []) {
      if (!p || !p.id || !(p.keys || []).length) continue;
      ancPeople['ged:' + p.id] = { name: p.name ? String(p.name).slice(0, 120) : null, keys: [...new Set(p.keys)], at: Date.now() };
      records += p.keys.length;
    }
    meta.treeFile = { name: file || null, at: Date.now(), people: (people || []).length };
    return { people: (people || []).length, records };
  });
}

// ── what the archive already holds (via the helper) ─────────────────────────
const KNOWN_MINUTES = 30;
const KNOWN_RANK = { captured: 1, processed: 2, filed: 3 };
let knownBusy = null;
function refreshKnown() {
  if (knownBusy) return knownBusy;
  knownBusy = (async () => {
    try {
      await update(['meta'], ({ meta }) => { meta.knownTriedAt = Date.now(); });
      const items = {};
      let offset = 0;
      let total = 1;
      let missing = [];
      while (offset < total) {
        const r = await hostCall({ op: 'known', offset });
        Object.assign(items, r.items || {});
        if (!offset) missing = r.missing || [];
        total = r.total;
        if (r.next <= offset) break;
        offset = r.next;
      }
      if (missing.length) {
        // Part of the archive could not be read (a drive or network share not
        // connected). What it held last time is still there, so keep those
        // marks rather than calling every filed source "not in archive" - and
        // rather than letting the capture add-on, which asks us, capture them
        // all again as new.
        const { known: prev = {} } = await chrome.storage.local.get('known');
        for (const [k, v] of Object.entries(prev)) {
          if (!items[k] || KNOWN_RANK[items[k].state] < KNOWN_RANK[v.state]) items[k] = v;
        }
      }
      await chrome.storage.local.set({ known: items });
      const counts = {};
      for (const [k, v] of Object.entries(items)) {
        const c = k.split(':')[0] + ':' + v.state;
        counts[c] = (counts[c] || 0) + 1;
      }
      await update(['meta'], ({ meta }) => {
        meta.knownAt = Date.now();
        meta.knownCounts = counts;
        meta.knownError = null;
        // Archive folders that could not be read (a drive not connected): the
        // popup names them, so "0 in archive" is never mistaken for the truth.
        meta.knownMissing = missing;
      });
      return { total: Object.keys(items).length, counts, missing };
    } catch (e) {
      const err = helperError(e);
      await update(['meta'], ({ meta }) => { meta.knownError = err.message; });
      throw err;
    } finally {
      knownBusy = null;
    }
  })();
  return knownBusy;
}

// ── passed over (shown in results, not opened) ──────────────────────────────
function recordPass(pageId, key, when) {
  return update(['passes', 'searches'], ({ passes, searches }) => {
    const p = passes[pageId] || { t: [], last: 0, keys: [] };
    if (!p.t.length || when - (p.last || 0) > GAP_MS) p.t.push(when);
    p.last = when;
    if (p.t.length > MAX_LOOKS_KEPT) p.t.splice(0, p.t.length - MAX_LOOKS_KEPT);
    if (key && !p.keys.includes(key)) p.keys.push(key);
    passes[pageId] = p;
    if (key) {
      // The search may not be logged yet (it waits for the result count);
      // start its record, and recordSearch fills in the rest.
      const s = searches[key] || (searches[key] = { params: null, url: null, runs: [], opened: [] });
      s.shown = s.shown || [];
      if (!s.shown.includes(pageId)) s.shown.push(pageId);
    }
  });
}

// ── searches ────────────────────────────────────────────────────────────────
function recordSearch(key, params, url, count, when) {
  return update(['searches', 'meta'], ({ searches, meta }) => {
    const s = searches[key] || { params, url, runs: [], opened: [] };
    s.params = params;
    s.url = url;
    const last = s.runs[s.runs.length - 1];
    if (last && when - last.at < GAP_MS) {
      if (count != null) last.count = count; // same sitting: refresh the count only
    } else {
      s.runs.push({ at: when, count: count == null ? null : count });
    }
    searches[key] = s;
    meta.lastSearch = { key, at: when };
  });
}

// ── history import ──────────────────────────────────────────────────────────
async function importHistory() {
  const items = await chrome.history.search({ text: 'newspapers.com/image/', startTime: 0, maxResults: 100000 });
  const times = {};
  let urls = 0;
  for (const it of items) {
    const id = pageIdFrom(it.url);
    if (!id) continue;
    urls++;
    const visits = await chrome.history.getVisits({ url: it.url });
    for (const v of visits) (times[id] = times[id] || []).push(Math.round(v.visitTime));
  }
  let pages = 0;
  await update(['views', 'meta'], ({ views, meta }) => {
    for (const id of Object.keys(times)) {
      const v = views[id] || { t: [], last: 0 };
      v.t = looksFrom(v.t.concat(times[id])).slice(-MAX_LOOKS_KEPT);
      v.last = Math.max(v.last || 0, ...times[id]);
      views[id] = v;
      pages++;
    }
    meta.historyAt = Date.now();
  });
  return { urls, pages };
}

// ── the synced compact copy ─────────────────────────────────────────────────
// chrome.storage.sync: ~100 KB total, 8 KB per item. Text shards "nsiV0"… of
// lines "pageId,looks,lastDay(base36),mark" (mark: x = nothing here, l = later).
const SYNC_PREFIX = 'nsiV';
const SYNC_SHARD_BYTES = 7500;
const SYNC_BUDGET_BYTES = 95000;
const DAY = 24 * 60 * 60 * 1000;
const MARK_CODE = { nothing: 'x', later: 'l' };
const CODE_MARK = { x: 'nothing', l: 'later' };

async function pushSync() {
  const { views = {}, pages = {} } = await chrome.storage.local.get(['views', 'pages']);
  const ids = new Set([...Object.keys(views), ...Object.keys(pages).filter((id) => pages[id].mark)]);
  const rows = [...ids]
    .map((id) => ({
      id,
      n: looksOf(views[id]),
      last: (views[id] && views[id].last) || (pages[id] && pages[id].markAt) || 0,
      m: (pages[id] && MARK_CODE[pages[id].mark]) || '',
    }))
    .filter((r) => r.n > 0 || r.m)
    .sort((a, b) => b.last - a.last);
  const shards = [];
  let cur = '';
  let total = 0;
  for (const r of rows) {
    const line = r.id + ',' + r.n + ',' + Math.floor(r.last / DAY).toString(36) + ',' + r.m + '\n';
    if (total + line.length > SYNC_BUDGET_BYTES) break;
    if (cur.length + line.length > SYNC_SHARD_BYTES) {
      shards.push(cur);
      cur = '';
    }
    cur += line;
    total += line.length;
  }
  if (cur) shards.push(cur);
  const old = await chrome.storage.sync.get(null);
  const toSet = {};
  shards.forEach((txt, i) => { toSet[SYNC_PREFIX + i] = txt; });
  const toRemove = Object.keys(old).filter((k) => k.startsWith(SYNC_PREFIX) && !(k in toSet));
  await chrome.storage.sync.set(toSet);
  if (toRemove.length) await chrome.storage.sync.remove(toRemove);
  await chrome.storage.local.remove('syncPending');
}

// The push waits a minute so a burst of changes goes up as one write. It is
// an alarm, not a timer: Chrome stops an idle service worker after about 30
// seconds and a pending timer dies with it, so the other computers never
// heard about the look. The alarm is set only when none is pending - reading
// time is saved every 30 seconds while a page is open, and re-arming on every
// save would push the write back forever. `syncPending` in storage covers a
// browser closed before the alarm fires: the next start pushes.
const SYNC_ALARM = 'pushSync';
async function schedulePushSync() {
  if (await chrome.alarms.get(SYNC_ALARM)) return;
  chrome.alarms.create(SYNC_ALARM, { delayInMinutes: 1 });
  await chrome.storage.local.set({ syncPending: Date.now() });
}

function runPushSync() {
  return pushSync().catch((e) => console.warn('seen-it: sync push', e));
}

async function pushSyncIfPending() {
  const { syncPending } = await chrome.storage.local.get('syncPending');
  if (syncPending && !(await chrome.alarms.get(SYNC_ALARM))) await runPushSync();
}

async function pullSync() {
  const all = await chrome.storage.sync.get(null);
  const rows = [];
  for (const [k, txt] of Object.entries(all)) {
    if (!k.startsWith(SYNC_PREFIX) || typeof txt !== 'string') continue;
    for (const line of txt.split('\n')) {
      const [id, n, day, m] = line.split(',');
      if (id && n != null) rows.push({ id, n: Number(n) || 0, last: (parseInt(day, 36) || 0) * DAY, mark: CODE_MARK[m] || null });
    }
  }
  if (!rows.length) return 0;
  await update(['views', 'pages'], ({ views, pages }) => {
    for (const r of rows) {
      if (r.n > 0) {
        const v = views[r.id] || { t: [], last: 0 };
        if (r.n > looksOf(v)) v.n = r.n;
        if (r.last > (v.last || 0)) v.last = r.last;
        views[r.id] = v;
      }
      if (r.mark) {
        const p = pages[r.id] || { dwellMs: 0, fromSearches: [], terms: [], mark: null, overlaps: [] };
        // The synced copy carries marks only (no clears, day-level dates), so it
        // fills in a mark only where this computer has never decided anything
        // about the page; the data folder files carry exact decision times.
        if (!p.markAt) {
          p.mark = r.mark;
          p.markAt = r.last;
        }
        pages[r.id] = p;
      }
    }
  });
  return rows.length;
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && (changes.views || changes.pages)) schedulePushSync().catch(() => {});
  if (area === 'sync' && Object.keys(changes).some((k) => k.startsWith(SYNC_PREFIX))) pullSync().catch(() => {});
});

// ── export / import ─────────────────────────────────────────────────────────
async function exportAll() {
  const o = await chrome.storage.local.get(['views', 'pages', 'passes', 'searches', 'clips', 'anc', 'ancPeople', 'meta']);
  if (o.meta) delete o.meta.lock;
  return { app: 'newspapers-seen-it', version: 3, exported: new Date().toISOString(), ...o };
}

function mergeList(a, b) {
  const out = (a || []).slice();
  for (const x of b || []) if (!out.includes(x)) out.push(x);
  return out;
}

// When the tree-file people were loaded: meta.treeFile.at, written by every
// load (even of a file whose people had nothing attached), or for a backup
// made before that field mattered, the newest of its ged: people.
function treeGeneration(meta, people) {
  if (meta && meta.treeFile && meta.treeFile.at) return meta.treeFile.at;
  let at = 0;
  for (const [k, p] of Object.entries(people || {})) if (k.startsWith('ged:')) at = Math.max(at, (p && p.at) || 0);
  return at;
}

async function importAll(obj) {
  if (!obj || obj.app !== 'newspapers-seen-it') throw new Error('not a Seen It backup file');
  let n = 0;
  await update(['views', 'pages', 'passes', 'searches', 'clips', 'anc', 'ancPeople', 'meta'], ({ views, pages, passes, searches, clips, anc, ancPeople, meta }) => {
    // The people from a tree file (ged:…) are one snapshot: the newest tree
    // file loaded on any computer wins whole. Merged person by person, an
    // older file from another computer would bring back everyone a newer
    // file had dropped.
    const mine = treeGeneration(meta, ancPeople);
    const theirs = treeGeneration(obj.meta, obj.ancPeople);
    if (theirs > mine) {
      for (const k of Object.keys(ancPeople)) if (k.startsWith('ged:')) delete ancPeople[k];
      for (const [k, ip] of Object.entries(obj.ancPeople || {})) if (k.startsWith('ged:')) ancPeople[k] = ip;
      const tf = obj.meta && obj.meta.treeFile;
      meta.treeFile = tf && tf.at ? tf : { name: null, at: theirs, people: null };
    }
    // People seen on their own Ancestry or FamilySearch pages: newest look at each wins.
    for (const [k, ip] of Object.entries(obj.ancPeople || {})) {
      if (k.startsWith('ged:')) continue;
      if (!ancPeople[k] || (ip.at || 0) > (ancPeople[k].at || 0)) ancPeople[k] = ip;
    }
    for (const [k, ia] of Object.entries(obj.anc || {})) {
      const a = anc[k] || { t: [], last: 0, mark: null };
      a.t = looksFrom((a.t || []).concat(ia.t || [])).slice(-MAX_LOOKS_KEPT);
      a.last = Math.max(a.last || 0, ia.last || 0);
      a.dwellMs = Math.max(a.dwellMs || 0, ia.dwellMs || 0);
      for (const f of ['title', 'collection', 'name']) if (!a[f] && ia[f]) a[f] = ia[f];
      if ((ia.markAt || 0) > (a.markAt || 0)) {
        a.mark = ia.mark || null;
        a.markAt = ia.markAt;
      }
      anc[k] = a;
    }
    for (const [id, ip] of Object.entries(obj.passes || {})) {
      const p = passes[id] || { t: [], last: 0, keys: [] };
      p.t = looksFrom((p.t || []).concat(ip.t || [])).slice(-MAX_LOOKS_KEPT);
      p.last = Math.max(p.last || 0, ip.last || 0);
      p.keys = mergeList(p.keys, ip.keys);
      passes[id] = p;
    }
    for (const [id, iv] of Object.entries(obj.views || {})) {
      const v = views[id] || { t: [], last: 0 };
      const all = looksFrom((v.t || []).concat(iv.t || []));
      v.t = all.slice(-MAX_LOOKS_KEPT);
      // past MAX_LOOKS_KEPT the times are trimmed, so the count moves to n
      v.n = Math.max(v.n || 0, iv.n || 0, all.length > MAX_LOOKS_KEPT ? all.length : 0);
      v.last = Math.max(v.last || 0, iv.last || 0);
      views[id] = v;
      n++;
    }
    for (const [id, ip] of Object.entries(obj.pages || {})) {
      const p = pages[id] || { dwellMs: 0, fromSearches: [], terms: [], mark: null, overlaps: [] };
      for (const k of ['paper', 'date', 'pageNo', 'place']) if (!p[k] && ip[k]) p[k] = ip[k];
      p.dwellMs = Math.max(p.dwellMs || 0, ip.dwellMs || 0);
      p.fromSearches = mergeList(p.fromSearches, ip.fromSearches);
      p.terms = mergeList(p.terms, ip.terms);
      // The later decision wins - including clearing a mark - so a mark you
      // removed on one computer is not brought back by another computer's file.
      if ((ip.markAt || 0) > (p.markAt || 0)) {
        p.mark = ip.mark || null;
        p.markAt = ip.markAt;
      }
      if ((!p.overlaps || !p.overlaps.length) && ip.overlaps) p.overlaps = ip.overlaps;
      pages[id] = p;
    }
    for (const [k, is] of Object.entries(obj.searches || {})) {
      const s = searches[k] || { params: is.params, url: is.url, runs: [], opened: [] };
      const seen = new Set(s.runs.map((r) => r.at));
      for (const r of is.runs || []) if (!seen.has(r.at)) s.runs.push(r);
      s.runs.sort((a, b) => a.at - b.at);
      s.opened = mergeList(s.opened, is.opened);
      s.shown = mergeList(s.shown, is.shown);
      searches[k] = s;
    }
    for (const [pid, list] of Object.entries(obj.clips || {})) {
      const cur = clips[pid] || [];
      for (const c of list) if (!cur.some((x) => x.id === c.id)) cur.push(c);
      clips[pid] = cur;
    }
  });
  return { pages: n };
}

// ── the shared data folder (via the helper) ─────────────────────────────────
// Chrome only lets an extension save into Downloads, which gets purged. The
// helper in host/ (installed once per computer with host/install.cmd or
// host/install.sh) keeps the data in a folder you choose instead - by default
// a per-user folder, never inside the add-on, a git checkout or the archive,
// because the files are a record of what you browsed. Each computer writes its
// own seen-it-<device>.json there and merges in every other computer's file
// whenever a newer one arrives (carried by a sync tool such as OneDrive or
// Dropbox). Merging never double-counts, so the computers converge on the
// same data.
const HOST = 'com.genealogy.seenit';
const HELPER_MISSING = 'the helper is not installed on this computer - run host\\install.cmd (Windows) or ' +
  'host/install.sh (Mac, Linux) once, then reload the add-on in chrome://extensions';

// A plain-words version of the errors Chrome gives when the helper is not set
// up. An answer the helper itself sent ("the archive folder … was not found")
// is already plain and names its own fix, so it is passed through untouched.
function helperError(e) {
  if (e.fromHelper) return e;
  if (/native messaging host|forbidden/i.test(e.message)) return new Error(HELPER_MISSING);
  if (/native host has exited|communicating with the native/i.test(e.message)) {
    return new Error('the helper stopped without answering - run host\\install.cmd (Windows) or host/install.sh ' +
      '(Mac, Linux) again; that fixes it when Python was updated or moved');
  }
  return e;
}
const FOLDER_CHECK_MINUTES = 15;
const REWRITE_ANYWAY_MS = 24 * 60 * 60 * 1000;

async function deviceId() {
  const { meta = {} } = await chrome.storage.local.get('meta');
  if (!meta.device) {
    meta.device = Math.random().toString(36).slice(2, 8);
    await chrome.storage.local.set({ meta });
  }
  return meta.device;
}

function hostCall(msg) {
  return new Promise((resolve, reject) => {
    if (!chrome.runtime.sendNativeMessage) {
      return reject(new Error('the add-on may not talk to the helper yet - turn Advanced mode off and on again to allow it'));
    }
    chrome.runtime.sendNativeMessage(HOST, msg, (r) => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (!r || !r.ok) {
        const err = new Error((r && r.error) || 'no answer from the helper');
        err.fromHelper = !!r;
        return reject(err);
      }
      resolve(r);
    });
  });
}

async function hostReadAll(name) {
  let text = '';
  let offset = 0;
  let total = 1;
  while (offset < total) {
    const r = await hostCall({ op: 'read', name, offset });
    text += r.text;
    total = r.total;
    if (r.next <= offset) break;
    offset = r.next;
  }
  return text;
}

// A fingerprint of the data (not meta, which changes all the time), so this
// computer's file is rewritten only when something actually changed.
async function dataFingerprint(data) {
  const copy = Object.assign({}, data);
  delete copy.meta;
  delete copy.exported;
  const bytes = new TextEncoder().encode(JSON.stringify(copy));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].slice(0, 12).map((b) => b.toString(16).padStart(2, '0')).join('');
}

async function writeOwnFile(force) {
  const device = await deviceId();
  const data = await exportAll();
  data.device = device;
  const fp = await dataFingerprint(data);
  const { meta = {} } = await chrome.storage.local.get('meta');
  // Unchanged data is not rewritten (even by the button): a rewrite would only
  // make the other computers re-read a file with nothing new in it.
  if (meta.folderFingerprint === fp && meta.backupAt && Date.now() - meta.backupAt < REWRITE_ANYWAY_MS) {
    return { written: false };
  }
  const name = 'seen-it-' + device + '.json';
  const r = await hostCall({ op: 'write', name, text: JSON.stringify(data) });
  await update(['meta'], ({ meta: m }) => {
    m.backupAt = Date.now();
    m.backupFile = r.path;
    m.folderFingerprint = fp;
  });
  return { written: true, path: r.path };
}

async function mergeOtherFiles() {
  const device = await deviceId();
  const own = 'seen-it-' + device + '.json';
  const listing = await hostCall({ op: 'list' });
  const { meta = {} } = await chrome.storage.local.get('meta');
  const merged = Object.assign({}, meta.merged || {});
  const done = [];
  for (const f of listing.files || []) {
    if (f.name === own || (merged[f.name] && merged[f.name] >= f.mtime)) continue;
    const data = JSON.parse(await hostReadAll(f.name));
    await importAll(data);
    merged[f.name] = f.mtime;
    done.push(f.name);
  }
  await update(['meta'], ({ meta: m }) => {
    m.merged = merged;
    m.dataDir = listing.dir;
    if (done.length) m.mergedAt = Date.now();
  });
  return { dir: listing.dir, merged: done, others: (listing.files || []).filter((f) => f.name !== own).length };
}

let folderBusy = null;
function syncFolder(force) {
  if (folderBusy) return folderBusy;
  folderBusy = (async () => {
    try {
      const m = await mergeOtherFiles();
      const w = await writeOwnFile(force);
      await update(['meta'], ({ meta }) => {
        meta.folderError = null;
        meta.folderCheckedAt = Date.now();
      });
      return Object.assign({}, m, w);
    } catch (e) {
      const msg = helperError(e).message;
      await update(['meta'], ({ meta }) => {
        meta.folderError = msg;
        meta.folderCheckedAt = Date.now();
      });
      throw new Error(msg);
    } finally {
      folderBusy = null;
    }
  })();
  return folderBusy;
}

// ── the helper's settings: which archive, which data folder ────────────────
// They live in the helper's own per-user settings file (it is the one that
// reads the folders), so the popup asks the helper rather than keeping a copy.
async function hostConfig() {
  try {
    return await hostCall({ op: 'config' });
  } catch (e) {
    throw helperError(e);
  }
}

async function setHostConfig(change) {
  const msg = { op: 'setConfig' };
  if ('archive' in change) msg.archive = String(change.archive || '');
  if ('dataDir' in change) msg.dataDir = String(change.dataDir || '');
  let r;
  try {
    r = await hostCall(msg);
  } catch (e) {
    throw helperError(e);
  }
  if ('dataDir' in change) {
    // A different folder: forget which files were merged and when this
    // computer's file was last written, so the next sync reads everything
    // there and writes this computer's file into it straight away. Merging
    // never counts a look twice, so re-reading is safe.
    await update(['meta'], ({ meta }) => {
      meta.merged = {};
      meta.folderFingerprint = null;
      meta.backupAt = null;
    });
    syncFolder(true).catch(() => {});
  }
  if ('archive' in change) refreshKnown().catch(() => {});
  return r;
}

chrome.alarms.onAlarm.addListener(async (a) => {
  // Chrome Sync is part of the standalone add-on: it runs in every mode.
  if (a.name === SYNC_ALARM) return runPushSync();
  if (!(await isAdvanced())) return;
  if (a.name === 'folder') syncFolder(false).catch(() => {});
  if (a.name === 'known') refreshKnown().catch(() => {});
});

function scheduleFolderSync() {
  chrome.alarms.clear('backup');
  chrome.alarms.create('folder', { delayInMinutes: 1, periodInMinutes: FOLDER_CHECK_MINUTES });
  chrome.alarms.create('known', { delayInMinutes: 0.2, periodInMinutes: KNOWN_MINUTES });
}

chrome.runtime.onInstalled.addListener(async () => {
  if (await isAdvanced()) scheduleFolderSync();
  await deviceId();
  await pullSync().catch(() => {});
  pushSyncIfPending().catch(() => {});
  importHistory().catch((e) => console.warn('seen-it: history import failed', e));
});
chrome.runtime.onStartup.addListener(async () => {
  pullSync().catch(() => {});
  pushSyncIfPending().catch(() => {});
  if (!(await isAdvanced())) return;
  scheduleFolderSync();
  syncFolder(false).catch(() => {});
});

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type) return;
  const reply = (p) => {
    p.then((r) => sendResponse(Object.assign({ ok: true }, r || {})))
      .catch((e) => sendResponse({ ok: false, error: String(e) }));
    return true;
  };
  switch (msg.type) {
    case 'look':
      return msg.pageId ? reply(recordLook(String(msg.pageId), Date.now(), msg.info)) : undefined;
    case 'dwell':
      return reply(recordDwell(String(msg.pageId), msg.ms));
    case 'mark':
      return reply(recordMark(String(msg.pageId), msg.mark).then((mark) => ({ mark })));
    case 'overlaps':
      return reply(recordOverlaps(String(msg.pageId), msg.overlaps));
    case 'passed':
      return reply(recordPass(String(msg.pageId), msg.key || null, Date.now()));
    case 'search':
      return reply(recordSearch(msg.key, msg.params, msg.url, msg.count, Date.now()));
    case 'export':
      return reply(exportAll().then((data) => ({ data })));
    case 'import':
      return reply(importAll(msg.data));
    case 'syncFolder':
      return reply(syncFolder(true));
    case 'importHistory':
      return reply(importHistory());
    case 'ancLook':
      return msg.key ? reply(recordAncLook(String(msg.key), Date.now(), msg.info)) : undefined;
    case 'ancDwell':
      return reply(recordAncDwell(String(msg.key), msg.ms));
    case 'ancMark':
      return reply(recordAncMark(String(msg.key), msg.mark).then((mark) => ({ mark })));
    case 'refreshKnown':
      return reply(refreshKnown());
    case 'hostConfig':
      return reply(hostConfig());
    case 'setHostConfig':
      return reply(setHostConfig(msg.change || {}));
    case 'setAdvanced':
      return reply(setAdvanced(msg.on));
    case 'loadTreeFile':
      return reply(loadTreeFile(msg.people, msg.file));
    case 'ancPerson':
      return msg.key ? reply(recordAncPerson(String(msg.key), msg.name, msg.keys)) : undefined;
    default:
      return undefined;
  }
});

// Other extensions (the archive's capture extension) may ask one read-only
// question: which item keys the archive already holds. Nothing else is
// exposed - not your looks, marks, searches or tree - and outside advanced
// mode the answer is empty. The capture extension is loaded unpacked without
// a fixed ID, so it cannot be named in the manifest; that is why
// externally_connectable allows any extension to ask this one question.
//
// The question: { type: 'knownKeys', prefix?, keys?, refresh? }.
//   keys     a list of item keys: only those are answered (the narrow form,
//            for a caller that knows what it is about to capture);
//   prefix   otherwise, every key that starts with it ('rec:', 'fsrec:', …) -
//            what the capture add-on's batches ask today;
//   refresh  re-read the archive first. A full read can take minutes, so an
//            outside caller gets at most one every EXTERNAL_REFRESH_MS; a
//            caller asking in a loop gets the last answer instead.
// `missing` names archive folders the last read could not reach: their old
// entries are kept (see refreshKnown), and a caller can tell the answer may
// be behind.
const EXTERNAL_REFRESH_MS = 5 * 60 * 1000;
const EXTERNAL_KEYS_MAX = 20000;
chrome.runtime.onMessageExternal.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== 'knownKeys') return;
  (async () => {
    if (!(await isAdvanced())) {
      sendResponse({ ok: true, keys: {}, knownAt: null, advanced: false });
      return;
    }
    const before = (await chrome.storage.local.get('meta')).meta || {};
    const last = Math.max(before.knownAt || 0, before.knownTriedAt || 0);
    if (msg.refresh && Date.now() - last >= EXTERNAL_REFRESH_MS) await refreshKnown().catch(() => {});
    const { known = {}, meta = {} } = await chrome.storage.local.get(['known', 'meta']);
    const keys = {};
    if (Array.isArray(msg.keys)) {
      for (const k of msg.keys.slice(0, EXTERNAL_KEYS_MAX)) {
        if (typeof k === 'string' && Object.prototype.hasOwnProperty.call(known, k)) keys[k] = known[k].state;
      }
    } else {
      const prefix = typeof msg.prefix === 'string' ? msg.prefix : '';
      for (const [k, v] of Object.entries(known)) if (k.startsWith(prefix)) keys[k] = v.state;
    }
    sendResponse({ ok: true, keys, knownAt: meta.knownAt || null, missing: meta.knownMissing || [] });
  })().catch((e) => sendResponse({ ok: false, error: String(e) }));
  return true;
});
