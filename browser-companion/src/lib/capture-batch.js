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
// One ledger per site, in chrome.storage.local (the authoritative copy,
// written only by the service worker - lib/batch-store.js - at the panels'
// asking; src/batch.js reads it), mirrored for reading to
// `<staging>/_{site}-batch-ledger.json`: a FILE at the staging root, which
// `fha capture --ingest` never sweeps (it files sub-FOLDERS only). The
// storage keys and the entry shapes are the ones the owner's archive copy of
// the add-on (0.6.x) used, so a queue built there carries over.
//
// An entry is `done` only once its bundle finished staging with everything
// it should hold, or when the Seen It add-on says the archive already holds
// the record; `partial` when it was staged without all of its image or
// photos (tried again at the next Start, after asking Seen It about what was
// missing); `retry` when its page may not have shown it (once more, at the
// end of the queue); `failed` ("set aside") when it cannot be saved as the
// record queued, with the reason - until the owner presses "Try the
// set-aside ones again". The ledger never records that an IMAGE is saved:
// only a file that exists can say that, and Seen It reads the files
// (seen-it/host/seenit_host.py).
//
// Pacing (owner, 2026-10-08), all randomized:
//   • 45-90 s of quiet after each record's capture finishes;
//   • a short break of 20-40 min every 30-60 records.
// Rate limiting: the first "slow down" (HTTP 429/503) stops the run at once.
// Before the next request the batch waits 30-35 min (the third such stop in
// a row: about 4 h, then the count starts again). A cause that belongs to one
// record sets that record aside and the run goes on; a cause that belongs to
// the run (signed out, a security check, no connection, the tab) stops it,
// with its real cause (classifyError). Nothing is retried on a timer.
//
// Works as a classic script (window.FHA.captureBatch) and as a CommonJS
// module (tests/test-capture-batch.js).
//
// Code map:
//   PACING, between           the rhythm, and one random draw from a range
//   ancMediaUrl               an Ancestry gallery item's viewer address, checked
//   SITES                     each site's ledger name, keys and record address
//                             (Ancestry's: records, and a person's gallery items)
//   newLedger                 an empty ledger (the archive copy's shape)
//   addRecords                queue records; a done one stays done
//   addClippings              queue the member's own Newspapers.com clippings
//   markHeld / markDone / markPartial / markRetry / markSetAside   what became of one entry
//   requeuePartials / requeueSetAside    a later Start; "Try the set-aside ones again"
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
    // A panel whose service worker did not answer (stopped by the browser
    // between two messages) asks again after this long, a few times.
    askRetryMs: 250,
  };

  // At most this many list pages are read in one go (24 clippings each):
  // a list that never says "no more" must not be read forever.
  const NP_LIST_MAX_PAGES = 250;
  // At most this many entries are looked at in one Start.
  const MAX_PER_RUN = 5000;
  // A record whose page might not have shown it (a busy page) is tried this
  // many times before it is set aside; one saved in part this many times
  // before it counts as done; and this many records in a row that cannot be
  // saved stop the run (the site, not the records, is the likely cause).
  const MAX_TRIES = 2;
  const MAX_PARTIAL = 3;
  const MAX_FAILS_IN_A_ROW = 3;
  // A site reporting trouble (HTTP 5xx) at one record, Start after Start: set
  // aside after this many, one more than other stops, so a short outage over
  // a couple of Starts sets nothing aside.
  const MAX_TROUBLE_TRIES = 3;

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

  // A photo, clipping or document in a tree person's Ancestry gallery is
  // queued in the Ancestry batch beside her cited records, as the media
  // viewer's address the gallery's tile opens
  // (/mediaui-viewer/[collection/{n}/]tree/{t}/person/{p}/media/{mediaId}).
  // That address, rebuilt on Ancestry from its checked parts, is the only
  // one the batch sends the tab to for it; one naming another item (`mediaId`)
  // or any other place is none.
  const ANC_GUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const ANC_VIEWER_RE = /^\/mediaui-viewer\/((?:collection\/\d+\/)?tree\/\d+\/person\/\d+\/)media\/([0-9a-f-]{36})\/?$/i;

  function ancMediaUrl(raw, mediaId) {
    let u;
    try {
      u = new URL(String(raw || ''));
    } catch (e) {
      return null;
    }
    if (u.protocol !== 'https:' || !/^(www\.)?ancestry\.com$/i.test(u.hostname) || u.port ||
        u.username || u.password) return null;
    const m = ANC_VIEWER_RE.exec(u.pathname);
    if (!m || !ANC_GUID_RE.test(m[2]) || m[2].toLowerCase() !== String(mediaId || '').toLowerCase()) return null;
    return 'https://www.ancestry.com/mediaui-viewer/' + m[1].toLowerCase() + 'media/' + m[2].toLowerCase();
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
    // A record ({ dbId, recordId }, keyed "{dbId}:{recordId}") or an item
    // from a person's gallery ({ mediaId, url }, keyed as Seen It knows it,
    // "med:{mediaId}"): the two kinds of key never meet.
    ancestry: {
      name: 'Ancestry',
      storageKey: 'ancBatchLedger',
      ledgerFile: '_ancestry-batch-ledger.json',
      keyOf: (r) => (r && r.mediaId
        ? (ancMediaUrl(r.url, r.mediaId) ? 'med:' + String(r.mediaId).toLowerCase() : null)
        : r && ANC_ID.test(String(r.dbId)) && ANC_ID.test(String(r.recordId)) ? r.dbId + ':' + r.recordId : null),
      seenIt: (it) => (it.mediaId ? 'med:' + String(it.mediaId).toLowerCase() : 'rec:' + it.dbId + ':' + it.recordId),
      captureKey: (it) => (it.mediaId ? 'med:' + String(it.mediaId).toLowerCase() : 'rec:' + it.dbId + ':' + it.recordId),
      urlOf: (it) => (it.mediaId ? ancMediaUrl(it.url, it.mediaId)
        : 'https://www.ancestry.com/search/collections/' + it.dbId + '/records/' + it.recordId),
      fields: ['dbId', 'recordId', 'mediaId', 'url'],
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
  // The archive copy kept a Newspapers.com queue's order as numbers
  // ([5, 7]) while its items are keyed by text ("5", "7"): the order is
  // made text, said once each, and only of entries that exist, and an entry
  // the order left out goes at its end - so no entry is ever counted or
  // queued twice.
  function repair(L) {
    if (!L || typeof L !== 'object') return null;
    if (!L.items || typeof L.items !== 'object') L.items = {};
    const seen = new Set();
    const order = [];
    for (const k of (Array.isArray(L.order) ? L.order : []).map(String).concat(Object.keys(L.items))) {
      if (seen.has(k) || !L.items[k]) continue;
      seen.add(k);
      order.push(k);
    }
    L.order = order;
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
      if (entry.mediaId) entry.mediaId = entry.mediaId.toLowerCase();
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
    repair(L);
    const page = (k) => L.items[k].pageId || 'x' + k;
    const first = {};
    for (const k of L.order) {
      const t = L.items[k].created || '';
      if (!(page(k) in first) || t < first[page(k)]) first[page(k)] = t;
    }
    L.order.sort((a, b) => {
      const pa = page(a);
      const pb = page(b);
      if (first[pa] !== first[pb]) return first[pa] < first[pb] ? -1 : 1;
      if (pa !== pb) return String(pa) < String(pb) ? -1 : 1;
      return Number(a) - Number(b);
    });
  }

  function markDone(L, key, bundle, note, now) {
    const it = L.items[key];
    if (!it) return;
    it.state = 'done';
    it.bundle = bundle;
    it.doneAt = new Date(now || Date.now()).toISOString();
    it.lastError = null;
    it.stops = 0;
    delete it.ownRefusal;
    delete it.again;
    if (note) it.note = note;
    else delete it.note;
    logEvent(L, 'done ' + key + ' -> ' + bundle + (note ? ' (' + note + ')' : ''), now);
  }

  // The archive already holds it (Seen It said so): done, nothing fetched.
  function markHeld(L, key, where, now) {
    markDone(L, key, '(already ' + where + ')', null, now);
  }

  // Set aside with the reason: the page shown was not a record the batch can
  // save as the one queued. Not tried again until the owner presses "Try the
  // set-aside ones again" (requeueSetAside); the reading copy lists it.
  // A record done stays done: a result that arrives after it was saved (a
  // batch capture still on its way when she saved it by hand, in another
  // window) never sets it aside, tries it again, counts a stop against it or
  // marks it saved in part. Nothing re-opens a done record; Seen It is what
  // says whether the archive holds it.
  const isDone = (it) => !!it && it.state === 'done';

  function markSetAside(L, key, reason, now) {
    const it = L.items[key];
    if (!it || isDone(it)) return;
    it.tries = (it.tries || 0) + 1;
    it.state = 'failed';
    it.lastError = String(reason || '').slice(0, 400);
    logEvent(L, 'SET ASIDE ' + key + ': ' + it.lastError, now);
  }

  // This page could not be read as the record, for a reason that may pass
  // (a page that showed no memorial): tried once more at the end of the
  // queue, then set aside.
  function markRetry(L, key, reason, now) {
    const it = L.items[key];
    if (!it || isDone(it)) return;
    if ((it.tries || 0) + 1 >= MAX_TRIES) {
      markSetAside(L, key, reason, now);
      return;
    }
    it.tries = (it.tries || 0) + 1;
    it.state = 'retry';
    it.lastError = String(reason || '').slice(0, 400);
    L.order = L.order.filter((k) => k !== key).concat([key]);
    logEvent(L, 'try again later ' + key + ': ' + it.lastError, now);
  }

  // Saved, but without all of its image or photos (a "slow down" part way,
  // more photos than one capture takes, a scan that would not come): not
  // done. The next Start asks Seen It again and captures it again; after
  // MAX_PARTIAL tries it counts as done, with the note saying what is missing.
  function markPartial(L, key, bundle, note, now) {
    const it = L.items[key];
    if (!it || isDone(it)) return;
    it.partialTries = (it.partialTries || 0) + 1;
    if (it.partialTries >= MAX_PARTIAL) {
      markDone(L, key, bundle, (note ? note + '; ' : '') + 'still incomplete after ' + MAX_PARTIAL + ' tries', now);
      return;
    }
    it.state = 'partial';
    it.bundle = bundle;
    it.note = note || 'saved without all of its image or photos';
    it.lastError = null;
    logEvent(L, 'saved in part ' + key + ' -> ' + bundle + ' (' + it.note + ')', now);
  }

  // At Start: the records saved in part are tried again.
  function requeuePartials(L, now) {
    let n = 0;
    for (const key of L.order) {
      const it = L.items[key];
      if (it && it.state === 'partial') {
        it.state = 'queued';
        it.again = true;
        n++;
      }
    }
    return n;
  }

  // "Try the set-aside ones again": every set-aside record is queued again,
  // its tries counted from nothing. Its last reason is kept for reading.
  function requeueSetAside(L, now) {
    let n = 0;
    for (const key of L.order) {
      const it = L.items[key];
      if (!it || it.state !== 'failed') continue;
      it.state = 'queued';
      it.tries = 0;
      it.stops = 0;
      delete it.ownRefusal;
      it.setAsideBefore = it.lastError || null;
      it.lastError = null;
      n++;
    }
    if (n) logEvent(L, 'queued again: ' + n + ' set-aside record' + (n === 1 ? '' : 's'), now);
    return n;
  }

  // A stop counted against one record (src/batch.js stopAtRecord).
  function markRefused(L, key, now) {
    const it = L.items[key];
    if (!it || isDone(it)) return 0;
    it.stops = (it.stops || 0) + 1;
    return it.stops;
  }

  // A "slow down" break (as opposed to the ordinary gap between records).
  const isCooldown = (p) => !!p && (p.resumeReason === 'ratelimit' || p.resumeReason === 'long');

  // `retry` is the archive copy's word for "failed once, try again": queued.
  // `skip`: keys not to give (a Set), if any.
  function nextPending(L, skip) {
    for (const key of L.order) {
      const it = L.items[key];
      if (skip && skip.has(key)) continue;
      if (it && it.url && (it.state === 'queued' || it.state === 'retry')) return Object.assign({ key }, it, { key });
    }
    return null;
  }

  function counts(L) {
    const c = { total: 0, done: 0, queued: 0, failed: 0, partial: 0 };
    for (const key of (L && L.order) || []) {
      const it = L.items[key];
      if (!it) continue;
      c.total++;
      if (it.state === 'done') c.done++;
      else if (it.state === 'failed') c.failed++;
      else if (it.state === 'partial') c.partial++;
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
      // The count starts again; the long break it earned is the deadline
      // (never shortened by the reset).
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

  // What a failed capture means for the run. The rule: a cause that belongs
  // to ONE record sets that record aside and the run goes on; a cause that
  // belongs to the run (the site, the session, the tab) stops it.
  //   'slowdown' - the site said slow down or busy (429/503): stop, and wait
  //                the rate-limit break before the next request;
  //   'skip'     - this record cannot be saved as the one queued (gone,
  //                merged, another record shown, a clipping that marks no
  //                area, a record restricted to a FamilySearch center): set
  //                it aside and go on;
  //   'retry'    - the page did not show the record, for a reason that may
  //                pass (a memorial page with no memorial on it): once more
  //                at the end of the queue, then set aside;
  //   'stop'     - the run's own cause (signed out, a security check, no
  //                connection, a download that would not write, the tab
  //                moved, a capture already saving, a site having trouble -
  //                any HTTP 5xx): stop with the cause;
  //   'refused'  - anything else (a 403, a message nobody expected):
  //                stop with the cause, and if the same record is refused
  //                again on a later Start, set it aside so it can never hold
  //                up the rest of the queue.
  const SLOWDOWN_RE = /slow down|HTTP 429|HTTP 503|too many requests/i;
  const RECORD_RE = new RegExp([
    'HTTP 404', 'HTTP 410', 'no memorial at this address', 'no record at this address', 'not the one queued',
    'is not a record page', 'is not a photo or document page', 'not one the batch can save', 'marks no area',
    'tick at least one of your clippings', 'none of the clippings on this page are yours', 'restricted to a FamilySearch center',
  ].join('|'), 'i');
  const RUN_RE = new RegExp([
    'sign in', 'signed in', 'security check', 'HTTP 401', 'could not reach', 'internet connection', 'broke off',
    'download was blocked', 'download interrupted', 'the page in the tab', 'the tab ', 'the batch.s tab', 'the panel',
    'a capture is already saving', 'different member', 'switched off', 'did not finish loading', 'took over',
    'having trouble', 'HTTP 5\\d\\d', 'busy or error page',
    // The browser's own words for a request that did not come through.
    'failed to fetch', 'networkerror', 'network error', 'load failed', 'connection'
  ].join('|'), 'i');
  // A gallery item's page that had not finished showing it: Ancestry's
  // Details still on the item shown before (never saved without them - the
  // batch has no "press again to save it without its details"), more of its
  // Details appearing after the panel read the page, its Details not drawn
  // yet (never saved as a bare photo), or its picture not there yet.
  const RETRY_RE = new RegExp(['shows no memorial',
    'could not read the (Ancestry record|FamilySearch record|Find a Grave memorial)',
    'have not caught up', 'more details on the page', 'details have not appeared', 'has not appeared on the page',
  ].join('|'), 'i');
  // The cause in a message, without the advice the panel or a site puts after
  // it ("Reload the page and open the panel again", "- sign in and reload the
  // page", "Wait a few minutes, then press Capture again"). Advice names the
  // panel, the tab and signing in whatever the cause was, so only the cause is
  // sorted (PR #224 X1: a fault reading one clipping, followed by "...open the
  // panel again", read as "the panel is busy" and stopped every Start there).
  const ADVICE_RE = new RegExp('(?:[.;,]\\s+|\\s+-\\s+)(?:then\\s+)?(?:' + [
    'reload', 'sign in', 'press capture', 'wait a (?:few minutes|moment)', 'try again', 'check (?:your|the page|you are)',
    'you can use', 'use [^.]{0,30}.s download', 'open (?:the panel|your clipping)', 'clip the item',
    'search find a grave', 'go back to', 'it can be saved', 'or untick', 'or drop',
  ].join('|') + ')\\b[\\s\\S]*$', 'i');
  function causeOf(message) {
    return String(message || '').replace(ADVICE_RE, '');
  }

  // Whether a cause that stopped the run at a record counts against THAT
  // record - the one place that decides it (PR #224 third audit M1, X1).
  // Sorted on the cause alone (causeOf), in this order:
  //   1. The session's own causes never count: a slow-down, a lost login (the
  //      site asking her to sign in, HTTP 401), a different member signed in.
  //      They come first because the site words them with a status a record
  //      cause also uses (a 503 "slow down", a 403 "asked you to sign in").
  //   2. RECORD causes count, and set the record aside after so many Starts
  //      that stopped on it: the site reporting trouble with it (HTTP 5xx, a
  //      busy or error page) or its page not answering (the message port
  //      closed) - MAX_TROUBLE_TRIES (3); a fault in the add-on while it read
  //      this record, or the site refusing this record - MAX_TRIES (2).
  //   3. RUN causes never count: no connection, a connection that broke off, a
  //      download the browser blocked or cut off, the tab changed or closed,
  //      the panel busy. They are the computer's, not the record's.
  //   4. Anything else - a cause nobody foresaw - counts, after
  //      MAX_TROUBLE_TRIES: if it keeps happening at one record, that record
  //      is set aside rather than stopping every Start ("Try the set-aside
  //      ones again" brings it back).
  //   Before all of these, and on the whole message, advice included: a 401
  //   or 403 for HER OWN clippings list (never a record) never counts; one
  //   whose words point at signing in or subscribing does not count either,
  //   but says `signIn`: a lapsed session answers EVERY record so, while a
  //   paper her subscription does not cover answers for its clippings only.
  //   The batch tells the two apart by asking the next record (batch.js
  //   probe; PR #224 X1 correction, fifth audit Y1). A bare 403 for one
  //   record, with no such words, is the site refusing it.
  // { counts, max, trouble, signIn }.
  const SIGN_IN_403_RE = /HTTP 40[13]\b/;
  const OWN_LIST_RE = /your clippings list/i;
  const SIGN_IN_WORDS_RE = /sign(?:ed)? in|log(?:ged)? in|subscri/i;
  const SESSION_STOP_RE = /slow down|HTTP 429|too many requests|asked you to sign in|not signed in|HTTP 401|different member/i;
  const TROUBLE_STOP_RE = /having trouble|HTTP 5\d\d|busy or error page|bad gateway|service (temporarily )?unavailable|internal server error|did not answer|message port closed/i;
  const FAULT_STOP_RE = /could not read this record|\brefused\b|HTTP 40[02-9]\b|HTTP 410/i;
  const RUN_STOP_RE = new RegExp([
    'sign in', 'signed in', 'could not reach', 'internet connection', 'broke off', 'failed to fetch', 'networkerror',
    'network error', 'load failed', 'download was blocked', 'download interrupted', 'the page in the tab', 'the tab ',
    'tab was closed', 'the batch.s tab', 'the panel', 'a capture is already saving', 'still reading',
    'switched off', 'took over', 'did not finish loading', 'could not establish connection',
  ].join('|'), 'i');
  function stopRule(message) {
    const whole = String(message || '');
    if (SIGN_IN_403_RE.test(whole) && OWN_LIST_RE.test(whole)) return { counts: false };
    const m = causeOf(whole);
    // The session's own words first - "asked you to sign in", a 401, a slow-
    // down - so FamilySearch's signed-out 403 stops the run and is never
    // asked about (sixth audit Z1).
    if (SESSION_STOP_RE.test(m)) return { counts: false };
    if (/HTTP 403\b/.test(whole) && SIGN_IN_WORDS_RE.test(whole)) return { counts: false, signIn: true };
    if (TROUBLE_STOP_RE.test(m)) return { counts: true, max: MAX_TROUBLE_TRIES, trouble: true };
    if (FAULT_STOP_RE.test(m)) return { counts: true, max: MAX_TRIES, trouble: false };
    if (RUN_STOP_RE.test(m)) return { counts: false };
    return { counts: true, max: MAX_TROUBLE_TRIES, trouble: false };
  }

  function classifyError(message) {
    const m = causeOf(message);
    if (SLOWDOWN_RE.test(m)) return 'slowdown';
    if (RECORD_RE.test(m)) return 'skip';
    if (RUN_RE.test(m)) return 'stop';
    if (RETRY_RE.test(m)) return 'retry';
    return 'refused';
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
    PACING, SITES, NP_LIST_MAX_PAGES, MAX_PER_RUN, MAX_TRIES, MAX_PARTIAL, MAX_FAILS_IN_A_ROW, MAX_TROUBLE_TRIES,
    between, npClipUrl, ancMediaUrl, newLedger, repair, logEvent, addRecords, addClippings,
    isDone, markDone, markHeld, markSetAside, markRetry, markPartial, markRefused, requeuePartials, requeueSetAside,
    isCooldown, nextPending, counts, stopRule, causeOf,
    ensurePacing, planRateLimit, planAfterCapture, classifyError,
    parseGedcom, parseMemorialList, ledgerFileBody,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) {
    root.FHA = root.FHA || {};
    root.FHA.captureBatch = api;
  }
})(typeof window !== 'undefined' ? window : (typeof self !== 'undefined' ? self : null));
