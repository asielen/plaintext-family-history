// capture-batch.js - the owner's batch downloads, the pure half: the
// "never twice" ledgers, the lists a queue is filled from, the pacing, and
// what a failure means for the run. src/batch.js is the half that drives the
// panel's tab; it presses the panel's own Capture for each queued record, so
// a batch bundle is the bundle a Capture of that record makes.
//
// Batch downloads are switched off unless the browser they run in has been
// told otherwise (src/batch.js says how). Nothing in this file runs on its
// own: it only answers when src/batch.js asks.
//
// One ledger per site, in chrome.storage.local (the authoritative copy, read
// and written by src/batch.js), mirrored for reading to
// `<staging>/_{site}-batch-ledger.json`: a FILE at the staging root, which
// `fha capture --ingest` never sweeps (it files sub-FOLDERS only). The
// storage keys and the entry shapes are the ones the owner's archive copy of
// the add-on (0.6.x) used, so a queue built there carries over.
//
// An entry is `done` only once its bundle finished staging, or when the Seen
// It add-on says the archive already holds the record; `failed` ("set aside")
// when the page shown was not a record that can be saved, with the reason.
// The ledger never records that an IMAGE is saved: only a file that exists
// can say that, and Seen It reads the files (seen-it/host/seenit_host.py).
//
// Pacing (owner, 2026-10-08), all randomized:
//   • 45-90 s of quiet after each record's capture finishes;
//   • a short break of 20-40 min every 30-60 records.
// Rate limiting: the first "slow down" (HTTP 429/503) stops the run at once.
// Before the next request the batch waits 30-35 min (the third such stop in
// a row: about 4 h, then the count starts again). Any other refusal stops the
// run with its real cause; nothing is retried on a timer.
//
// Works as a classic script (window.FHA.captureBatch) and as a CommonJS
// module (tests/test-capture-batch.js).
//
// Code map:
//   PACING, between           the rhythm, and one random draw from a range
//   SITES                     each site's ledger name, keys and record address
//   newLedger                 an empty ledger (the archive copy's shape)
//   addRecords                queue records; a done one stays done
//   addClippings              queue the member's own Newspapers.com clippings
//   markHeld / markDone / markSetAside   what became of one entry
//   mergeDone                 fold in what another panel window stored
//   nextPending / counts      the next entry to try; the tally
//   ensurePacing / planAfterCapture / planRateLimit   when the next request may go
//   classifyError             stop, slow down, or set this one aside
//   parseGedcom               Ancestry records cited in a GEDCOM (`_APID`)
//   parseMemorialList         Find a Grave memorials named in any text
//   ledgerFileBody            the reading copy written beside the bundles

(function (root) {
  const SEC = 1000;
  const MIN = 60 * SEC;
  const HOUR = 60 * MIN;

  const PACING = {
    betweenMs: [45 * SEC, 90 * SEC],
    shortEvery: [30, 60],
    shortBreakMs: [20 * MIN, 40 * MIN],
    rateBreakMs: [30 * MIN, 35 * MIN],
    longBreakMs: [4 * HOUR - 15 * MIN, 4 * HOUR + 15 * MIN],
    longAfterRateEvents: 3,
    // After the tab is sent to a page: let it settle as a person would.
    settleMs: [4 * SEC, 8 * SEC],
    // Between two pages of the Newspapers.com clippings list.
    listPauseMs: [3 * SEC, 8 * SEC],
    // A page that does not finish loading in this long stops the run.
    loadTimeoutMs: 90 * SEC,
  };

  // At most this many list pages are read in one go (24 clippings each):
  // a list that never says "no more" must not be read forever.
  const NP_LIST_MAX_PAGES = 250;
  // At most this many entries are looked at in one Start.
  const MAX_PER_RUN = 5000;

  function between(range, rand) {
    const r = typeof rand === 'function' ? rand() : Math.random();
    return Math.round(range[0] + r * (range[1] - range[0]));
  }

  // ── the sites ────────────────────────────────────────────────────────────
  // `key` is the ledger's own key for an entry (the archive copy's), `seenIt`
  // the Seen It key that says the archive holds it, `url` the only address
  // the batch ever sends the tab to for it. Every id is checked for shape
  // before it goes into an address.
  const ANC_ID = /^\d+$/;
  const FS_ID = /^[A-Z0-9]+(?:-[A-Z0-9]+)+$/i;
  const SLUG = /^[A-Za-z0-9_-]{1,120}$/;

  function npClipUrl(raw) {
    let u;
    try {
      u = new URL(String(raw || ''), 'https://www.newspapers.com');
    } catch (e) {
      return null;
    }
    if (u.protocol !== 'https:' || !/(^|\.)newspapers\.com$/i.test(u.hostname) ||
        u.username || u.password || !/^\/article\//.test(u.pathname)) return null;
    return 'https://www.newspapers.com' + u.pathname;
  }

  const SITES = {
    newspapers: {
      name: 'Newspapers.com',
      storageKey: 'npBatchLedger',
      ledgerFile: '_newspapers-batch-ledger.json',
      keyOf: (c) => (c && /^\d+$/.test(String(c.id)) ? String(Number(c.id)) : null),
      seenIt: (it) => 'clip:' + it.id,
      captureKey: (it) => 'clip:' + it.id,
      urlOf: (it) => npClipUrl(it.url),
    },
    ancestry: {
      name: 'Ancestry',
      storageKey: 'ancBatchLedger',
      ledgerFile: '_ancestry-batch-ledger.json',
      keyOf: (r) => (r && ANC_ID.test(String(r.dbId)) && ANC_ID.test(String(r.recordId))
        ? r.dbId + ':' + r.recordId : null),
      seenIt: (it) => 'rec:' + it.dbId + ':' + it.recordId,
      captureKey: (it) => 'rec:' + it.dbId + ':' + it.recordId,
      urlOf: (it) => 'https://www.ancestry.com/search/collections/' + it.dbId + '/records/' + it.recordId,
      fields: ['dbId', 'recordId'],
    },
    familysearch: {
      name: 'FamilySearch',
      storageKey: 'fsBatchLedger',
      ledgerFile: '_familysearch-batch-ledger.json',
      keyOf: (r) => (r && FS_ID.test(String(r.fsId || '')) ? String(r.fsId).toUpperCase() : null),
      seenIt: (it) => 'fsrec:' + String(it.fsId).toUpperCase(),
      captureKey: (it) => 'fsrec:' + String(it.fsId).toUpperCase(),
      urlOf: (it) => 'https://www.familysearch.org/ark:/61903/1:1:' + String(it.fsId).toUpperCase(),
      fields: ['fsId'],
    },
    findagrave: {
      name: 'Find a Grave',
      storageKey: 'fagBatchLedger',
      ledgerFile: '_findagrave-batch-ledger.json',
      keyOf: (r) => (r && ANC_ID.test(String(r.memorialId || '')) ? String(r.memorialId) : null),
      // Only `fag:` - a memorial whose photos are all saved. `fagidx:` (seen,
      // its photos still to come) is fetched, as a Capture would.
      seenIt: (it) => 'fag:' + it.memorialId,
      captureKey: (it) => 'fag:' + it.memorialId,
      urlOf: (it) => 'https://www.findagrave.com/memorial/' + it.memorialId +
        (it.slug && SLUG.test(it.slug) ? '/' + it.slug : ''),
      fields: ['memorialId', 'slug'],
    },
  };

  // ── the ledger ───────────────────────────────────────────────────────────

  function newLedger(now) {
    return {
      version: 1,
      created: new Date(now || Date.now()).toISOString(),
      items: {},
      order: [],
      pacing: null,
      log: [],
    };
  }

  // Normalize a ledger read from storage (the archive copy's, or ours): a
  // missing part is made empty, never trusted to exist.
  function repair(L) {
    if (!L || typeof L !== 'object') return null;
    if (!L.items || typeof L.items !== 'object') L.items = {};
    if (!Array.isArray(L.order)) L.order = Object.keys(L.items);
    if (!Array.isArray(L.log)) L.log = [];
    return L;
  }

  function logEvent(L, text, now) {
    L.log.push(new Date(now || Date.now()).toISOString() + '  ' + text);
    if (L.log.length > 200) L.log.splice(0, L.log.length - 200);
  }

  // Queue records ({ dbId, recordId } | { fsId } | { memorialId, slug },
  // with optional persons and title). An entry already there is never reset:
  // a done record stays done however often it is queued again.
  function addRecords(siteName, L, records, from) {
    const site = SITES[siteName];
    let added = 0;
    for (const r of records || []) {
      const key = site.keyOf(r);
      if (!key) continue;
      const prev = L.items[key];
      if (prev) {
        prev.persons = Array.isArray(prev.persons) ? prev.persons : [];
        for (const p of r.persons || []) if (p && prev.persons.indexOf(p) < 0) prev.persons.push(p);
        if (!prev.title && r.title) prev.title = String(r.title).slice(0, 200);
        continue;
      }
      const entry = { key };
      for (const f of site.fields) if (r[f] != null && r[f] !== '') entry[f] = String(r[f]);
      if (siteName === 'familysearch') entry.fsId = key;
      if (entry.slug && !SLUG.test(entry.slug)) delete entry.slug;
      entry.url = site.urlOf(entry);
      L.items[key] = Object.assign(entry, {
        persons: (r.persons || []).filter(Boolean).slice(),
        title: String(r.title || '').slice(0, 200),
        from: from || null,
        state: 'queued',
        tries: 0,
      });
      L.order.push(key);
      added++;
    }
    return added;
  }

  // Queue Newspapers.com clippings from the member's own list
  // (/api/clipping/list). Only `username`'s own; one with no usable address
  // is left out. Grouped by newspaper page, so a page's clippings are taken
  // one after another.
  function addClippings(L, clippings, username) {
    let added = 0;
    for (const c of clippings || []) {
      if (!c) continue;
      const key = SITES.newspapers.keyOf({ id: c.clipping_id });
      if (!key) continue;
      if (!username || !c.user_username ||
          String(c.user_username).toLowerCase() !== String(username).toLowerCase()) continue;
      const url = npClipUrl(c.url);
      const prev = L.items[key];
      if (prev) {
        if (!prev.url && url) prev.url = url;
        continue;
      }
      if (!url) continue;
      L.items[key] = {
        id: Number(key),
        url,
        pageId: Number(c.page_id) || null,
        title: String(c.title || '').slice(0, 200),
        pageDate: c.page_date || null,
        created: c.created || null,
        state: 'queued',
        tries: 0,
      };
      added++;
    }
    sortClippings(L);
    return added;
  }

  function sortClippings(L) {
    const items = L.order.concat(Object.keys(L.items).filter((k) => L.order.indexOf(k) < 0))
      .map((k) => L.items[k]).filter(Boolean);
    const first = {};
    for (const it of items) {
      const k = it.pageId || 'x' + it.id;
      const t = it.created || '';
      if (!(k in first) || t < first[k]) first[k] = t;
    }
    items.sort((a, b) => {
      const ka = a.pageId || 'x' + a.id;
      const kb = b.pageId || 'x' + b.id;
      if (first[ka] !== first[kb]) return first[ka] < first[kb] ? -1 : 1;
      if (ka !== kb) return String(ka) < String(kb) ? -1 : 1;
      return a.id - b.id;
    });
    L.order = items.map((it) => String(it.id));
  }

  function markDone(L, key, bundle, note, now) {
    const it = L.items[key];
    if (!it) return;
    it.state = 'done';
    it.bundle = bundle;
    it.doneAt = new Date(now || Date.now()).toISOString();
    it.lastError = null;
    if (note) it.note = note;
    else delete it.note;
    logEvent(L, 'done ' + key + ' -> ' + bundle + (note ? ' (' + note + ')' : ''), now);
  }

  // The archive already holds it (Seen It said so): done, nothing fetched.
  function markHeld(L, key, where, now) {
    markDone(L, key, '(already ' + where + ')', null, now);
  }

  // Set aside with the reason: the page shown was not a record the batch can
  // save as the one queued. Not tried again by this run or the next; the
  // reading copy of the ledger lists it.
  function markSetAside(L, key, reason, now) {
    const it = L.items[key];
    if (!it) return;
    it.tries = (it.tries || 0) + 1;
    it.state = 'failed';
    it.lastError = String(reason || '').slice(0, 400);
    logEvent(L, 'SET ASIDE ' + key + ': ' + it.lastError, now);
  }

  // Fold in what another panel window stored meanwhile (a record captured or
  // queued there): new entries are added, a done one stays done.
  function mergeDone(L, stored) {
    if (!stored || !stored.items) return 0;
    let n = 0;
    for (const key of Object.keys(stored.items)) {
      const s = stored.items[key];
      if (!s) continue;
      const mine = L.items[key];
      if (!mine) {
        L.items[key] = Object.assign({}, s);
        L.order.push(key);
        n++;
      } else if (s.state === 'done' && mine.state !== 'done') {
        L.items[key] = Object.assign({}, mine, s);
        n++;
      }
    }
    return n;
  }

  // `retry` is the archive copy's word for "failed once, try again": queued.
  function nextPending(L) {
    for (const key of L.order) {
      const it = L.items[key];
      if (it && it.url && (it.state === 'queued' || it.state === 'retry')) return Object.assign({ key }, it, { key });
    }
    return null;
  }

  function counts(L) {
    const c = { total: 0, done: 0, queued: 0, failed: 0 };
    for (const key of (L && L.order) || []) {
      const it = L.items[key];
      if (!it) continue;
      c.total++;
      if (it.state === 'done') c.done++;
      else if (it.state === 'failed') c.failed++;
      else c.queued++;
    }
    return c;
  }

  // ── pacing ───────────────────────────────────────────────────────────────

  function ensurePacing(L, rand) {
    if (!L.pacing || typeof L.pacing !== 'object') {
      L.pacing = { sinceShort: 0, shortEvery: between(PACING.shortEvery, rand), rateEvents: 0, resumeAt: 0, resumeReason: '' };
    }
    return L.pacing;
  }

  // The site said "slow down": no request before this break is over.
  // { kind: 'ratelimit' | 'long', until }.
  function planRateLimit(L, now, rand) {
    const p = ensurePacing(L, rand);
    p.rateEvents = (p.rateEvents || 0) + 1;
    let kind;
    let until;
    if (p.rateEvents >= PACING.longAfterRateEvents) {
      kind = 'long';
      until = now + between(PACING.longBreakMs, rand);
      p.rateEvents = 0;
    } else {
      kind = 'ratelimit';
      until = now + between(PACING.rateBreakMs, rand);
    }
    p.sinceShort = 0;
    p.shortEvery = between(PACING.shortEvery, rand);
    p.resumeAt = until;
    p.resumeReason = kind;
    return { kind, until };
  }

  // After one record was captured without a "slow down": the ordinary gap,
  // or the short break. A clean capture also ends a run of rate-limit stops.
  // { kind: 'between' | 'short', until }.
  function planAfterCapture(L, now, rand) {
    const p = ensurePacing(L, rand);
    p.sinceShort = (p.sinceShort || 0) + 1;
    p.rateEvents = 0;
    let kind = 'between';
    let until = now + between(PACING.betweenMs, rand);
    if (p.sinceShort >= p.shortEvery) {
      kind = 'short';
      until = now + between(PACING.shortBreakMs, rand);
      p.sinceShort = 0;
      p.shortEvery = between(PACING.shortEvery, rand);
    }
    p.resumeAt = until;
    p.resumeReason = kind;
    return { kind, until };
  }

  // What a failed capture means for the run:
  //   'slowdown' - the site said slow down or busy (429/503): stop, and wait
  //                the rate-limit break before the next request;
  //   'skip'     - this page is not a record that can be saved as the one
  //                queued (gone, merged, another record shown): set it aside
  //                and go on;
  //   'stop'     - anything else, a refusal above all (sign in, 401/403, a
  //                security check, no connection): stop with the cause, and
  //                let the owner look. Unknown means stop, never "try again".
  function classifyError(message) {
    const m = String(message || '');
    if (/slow down|HTTP 429|HTTP 503|too many requests/i.test(m)) return 'slowdown';
    if (/sign in|signed in|refused|security check|HTTP 401|HTTP 403/i.test(m)) return 'stop';
    if (/HTTP 404|HTTP 410|no memorial at this address|no record at this address|removed or merged|not the one queued|is not a record page|not one the batch can save/i.test(m)) return 'skip';
    return 'stop';
  }

  // ── what fills a queue ───────────────────────────────────────────────────

  // GEDCOM -> [{ dbId, recordId, persons }]: every source citation an
  // Ancestry tree export carries as `_APID 1,{dbId}::{recordId}`, with the
  // people it is cited for (the NAME of the INDI it sits under). A family's
  // citations (under `0 @F..@ FAM`) carry no person.
  function parseGedcom(text) {
    const out = new Map();
    let person = null;
    for (const raw of String(text || '').split(/\r?\n/)) {
      const m = /^(\d+)\s+(@[^@]+@\s+)?(\S+)\s*(.*)$/.exec(raw.trim());
      if (!m) continue;
      const level = Number(m[1]);
      const tag = m[3];
      const value = m[4] || '';
      if (level === 0) {
        person = tag === 'INDI' ? { name: null } : null;
        continue;
      }
      if (person && level === 1 && tag === 'NAME' && !person.name) {
        person.name = value.replace(/\//g, '').replace(/\s+/g, ' ').trim() || null;
        continue;
      }
      if (tag !== '_APID') continue;
      const a = /^\d+,(\d+)::(\d+)/.exec(value.trim());
      if (!a) continue;
      const key = a[1] + ':' + a[2];
      const e = out.get(key) || { dbId: a[1], recordId: a[2], persons: [] };
      if (person && person.name && e.persons.indexOf(person.name) < 0) e.persons.push(person.name);
      out.set(key, e);
    }
    return [...out.values()];
  }

  // Find a Grave memorials named in any text - a list of addresses, a
  // GEDCOM's web links, a saved search page: /memorial/{id}/{slug}, or
  // "memorial ID 900000001". [{ memorialId, slug }].
  function parseMemorialList(text) {
    const out = new Map();
    const re = /findagrave\.com\/(?:[a-z]{2}\/)?memorial\/(\d+)(?:\/([A-Za-z0-9_-]+))?|memorial\s*id\W{0,4}(\d{3,})/gi;
    let m;
    const t = String(text || '');
    while ((m = re.exec(t))) {
      const id = m[1] || m[3];
      const slug = m[2] && !/^(photo|photos|flower|flowers|edit)$/i.test(m[2]) ? m[2] : null;
      if (!out.has(id)) out.set(id, { memorialId: id, slug });
    }
    return [...out.values()];
  }

  // The reading copy written beside the bundles: counts, the entries and the
  // last 50 log lines. No login, cookie or token is ever in a ledger.
  function ledgerFileBody(siteName, L, now) {
    return {
      note: SITES[siteName].name + ' batch download ledger, written by the capture extension. The working ' +
        'copy is inside the extension; this file is for reading. A record is "done" once its bundle was ' +
        'staged, or when the archive already had it; "failed" means it was set aside, with the reason.',
      written: new Date(now || Date.now()).toISOString(),
      counts: counts(L),
      pacing: L.pacing || null,
      entries: L.order.map((k) => L.items[k]).filter(Boolean),
      log: L.log.slice(-50),
    };
  }

  const api = {
    PACING, SITES, NP_LIST_MAX_PAGES, MAX_PER_RUN,
    between, npClipUrl, newLedger, repair, logEvent, addRecords, addClippings,
    markDone, markHeld, markSetAside, mergeDone, nextPending, counts,
    ensurePacing, planRateLimit, planAfterCapture, classifyError,
    parseGedcom, parseMemorialList, ledgerFileBody,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) {
    root.FHA = root.FHA || {};
    root.FHA.captureBatch = api;
  }
})(typeof window !== 'undefined' ? window : null);
