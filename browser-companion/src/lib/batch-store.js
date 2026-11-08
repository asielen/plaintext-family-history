// batch-store.js - the ONE writer of the batch downloads' ledgers and their
// run lease. It runs in the add-on's service worker (src/background.js);
// every panel window asks it, over chrome.runtime messaging, to change a
// ledger, and never writes one itself (src/batch.js).
//
// Why: with several panel windows open, each used to read a ledger, change
// its own copy and write it back, so one window could write over another's
// change - a GEDCOM queued in one window lost to a record saved by hand in
// another, or a "slow down" one window met erased by another's next save.
// Locks, stamps and merge rules held that off, case by case. Now there is
// one writer, and each change ("intent") is applied to the ledger as it is
// stored, one at a time, so no change can be lost to another.
//
// An intent: { type: 'fha-batch', id, site, ops: [[name, ...args], ...] },
// answered { ok, L, results } (the ledger as now stored, and each op's
// answer). Its `id` makes it safe to send again: the service worker can be
// stopped by the browser between applying an intent and answering it, and a
// panel that got no answer sends the same intent again. An id already
// applied is answered from what was stored with the ledger, never applied
// twice. The ids live in the ledger itself (`applied`), written in the same
// storage write as the change they record.
//
// The run lease ({ type: 'fha-batch-lease', action, owner, site }) is kept
// here too: one batch at a time across every panel window.
//
// Classic script (self.FHA.batchStore in the service worker) and CommonJS
// module (tests/test-capture-batch.js).

(function (root) {
  const LEASE_KEY = 'batchLease';
  const LEASE_MS = 10 * 60 * 1000;
  // How many applied intent ids each ledger remembers. A panel resends an
  // intent within seconds, so a few dozen is plenty.
  const APPLIED_MAX = 50;

  // `deps`: { captureBatch, get(key) -> value, set({key: value}), remove(key),
  // now() }. get/set/remove answer promises and reject with the browser's
  // own reason when it refuses.
  function create(deps) {
    const CB = deps.captureBatch;
    const now = () => (deps.now ? deps.now() : Date.now());

    // One change at a time, in the order asked: the service worker is one
    // process, so a promise chain is the whole of the serializing.
    let chain = Promise.resolve();
    function serial(fn) {
      const run = chain.then(fn);
      chain = run.catch(() => {});
      return run;
    }

    function storageError(what, err) {
      return { ok: false, storage: true, what, error: String((err && err.message) || err || 'no reason given') };
    }

    async function load(siteName) {
      const key = CB.SITES[siteName].storageKey;
      const L = CB.repair(await deps.get(key)) || CB.newLedger(now());
      if (siteName === 'newspapers') {
        if (L.listCursor == null) L.listCursor = 0;
        if (L.listComplete == null) L.listComplete = false;
        if (L.user === undefined) L.user = null;
      }
      return L;
    }

    // A cooldown the site asked for that is still ahead: nothing may shorten
    // or erase it (a run's ordinary gap after a capture, a run stopping).
    const cooldownAhead = (L) => !!(L.pacing && CB.isCooldown(L.pacing) && L.pacing.resumeAt > now());

    const OPS = {
      // Nothing changes: the ledger as stored.
      get: () => null,
      // A Start: pacing made ready, the records saved in part queued again.
      start(L) {
        CB.ensurePacing(L);
        const n = CB.requeuePartials(L);
        if (n) CB.logEvent(L, 'trying again: ' + n + ' saved in part');
        return n;
      },
      log(L, text) { CB.logEvent(L, String(text || '')); },
      markHeld(L, key, where) { CB.markHeld(L, key, where); },
      markDone(L, key, bundle, note) { CB.markDone(L, key, bundle, note || null); },
      markPartial(L, key, bundle, note, imageKey) {
        CB.markPartial(L, key, bundle, note || null);
        if (imageKey && L.items[key]) L.items[key].imageKey = imageKey;
      },
      markRetry(L, key, why) { CB.markRetry(L, key, why); },
      markSetAside(L, key, why) { CB.markSetAside(L, key, why); },
      // A stop counted against one record; `own` marks a "sign in" refusal
      // shown to be that record's alone. Answers the count.
      markRefused(L, key, own) {
        const stops = CB.markRefused(L, key);
        if (own && L.items[key]) L.items[key].ownRefusal = true;
        return stops;
      },
      lastError(L, key, why) { if (L.items[key]) L.items[key].lastError = String(why || '').slice(0, 400); },
      // The site said "slow down": the break. `unlessAhead`: only if no
      // break is ahead already (a run stopping on a slow-down it already
      // took the break for). Answers the plan, or null.
      rateLimit(L, opts) {
        if (opts && opts.unlessAhead && L.pacing && L.pacing.resumeAt > now()) return null;
        return CB.planRateLimit(L, now());
      },
      // The ordinary gap after a capture - unless a "slow down" break another
      // window recorded is still ahead: that break stands, whole, and its
      // count of slow-downs in a row is not reset by this capture.
      afterCapture(L) {
        if (cooldownAhead(L)) return { kind: L.pacing.resumeReason, until: L.pacing.resumeAt };
        return CB.planAfterCapture(L, now());
      },
      // A run ending: the gap it was waiting out is dropped, never a
      // "slow down" break still ahead.
      clearGap(L) { if (L.pacing && !cooldownAhead(L)) L.pacing.resumeAt = 0; },
      addRecords(L, records, from, what) {
        const list = Array.isArray(records) ? records : [];
        const n = CB.addRecords(currentSite, L, list, from);
        if (what) CB.logEvent(L, what + ': ' + list.length + ' found, ' + n + ' new to the queue');
        return n;
      },
      // One page of her Newspapers.com clippings list. `epoch` is the list's
      // as it was when the page was asked for: "Read my clippings list
      // again" pressed meanwhile (a new epoch) makes the page stale, and it
      // is dropped - the list is read again from its start.
      listPage(L, page) {
        if ((L.listEpoch || 0) !== (page.epoch || 0)) return { stale: true };
        if (!L.user) L.user = page.user || null;
        const added = CB.addClippings(L, page.clippings, L.user);
        const moved = page.nextCursor != null && String(page.nextCursor) !== String(L.listCursor);
        L.listComplete = !page.more || !(page.clippings || []).length || !moved;
        if (moved) L.listCursor = page.nextCursor;
        return { added, complete: L.listComplete };
      },
      relist(L) {
        L.listComplete = false;
        L.listCursor = 0;
        L.listEpoch = (L.listEpoch || 0) + 1;
        // Read for whoever is signed in then (content.js always asks for
        // the member signed in on the page).
        L.user = null;
      },
      requeueSetAside(L) { return CB.requeueSetAside(L); },
      // A record saved by hand with Capture: in its ledger too, so a batch
      // never downloads it again.
      manual(L, out) { noteManual(L, out); },
    };

    // The site of the intent being applied (addRecords needs it).
    let currentSite = null;

    function noteManual(L, out) {
      if (!out || !out.ok) return;
      if (out.site === 'newspapers') {
        for (const r of out.results || []) {
          if (!r || !r.ok) continue;
          const key = CB.SITES.newspapers.keyOf({ id: r.id });
          if (!key) continue;
          if (!L.items[key]) {
            L.items[key] = { id: Number(key), url: null, title: r.title || '', state: 'queued', tries: 0 };
            L.order.push(key);
          }
          CB.markDone(L, key, r.bundle, 'saved by hand');
        }
        return;
      }
      const site = CB.SITES[out.site];
      const key = site && site.keyOf(out.record);
      if (!key) return;
      CB.addRecords(out.site, L, [Object.assign({ title: out.record.name || '' }, out.record)], 'by hand');
      if (out.partial) {
        CB.markPartial(L, key, out.bundle, 'saved by hand' + (out.note ? ', ' + out.note : ''));
        if (out.imageKey && L.items[key]) L.items[key].imageKey = out.imageKey;
      } else {
        CB.markDone(L, key, out.bundle, 'saved by hand');
      }
    }

    async function applyIntent(msg) {
      const siteName = msg.site;
      if (!CB.SITES[siteName]) return { ok: false, error: 'no batch for "' + siteName + '"' };
      const ops = Array.isArray(msg.ops) ? msg.ops : [];
      for (const o of ops) {
        if (!Array.isArray(o) || !Object.prototype.hasOwnProperty.call(OPS, o[0])) {
          return { ok: false, error: 'the batch does not know the change "' + (Array.isArray(o) ? o[0] : o) + '"' };
        }
      }
      let L;
      try {
        L = await load(siteName);
      } catch (e) {
        return storageError('read', e);
      }
      const prior = msg.id && (L.applied || []).find((a) => a.id === msg.id);
      if (prior) return { ok: true, L: view(L), results: prior.results, again: true };
      if (ops.every((o) => o[0] === 'get')) return { ok: true, L: view(L), results: ops.map(() => null) };
      currentSite = siteName;
      let results;
      try {
        results = ops.map((o) => {
          const r = OPS[o[0]](L, ...o.slice(1));
          return r === undefined ? null : r;
        });
      } finally {
        currentSite = null;
      }
      if (msg.id) L.applied = (L.applied || []).concat([{ id: msg.id, results }]).slice(-APPLIED_MAX);
      try {
        await deps.set({ [CB.SITES[siteName].storageKey]: L });
      } catch (e) {
        return storageError('save', e);
      }
      return { ok: true, L: view(L), results };
    }

    // The ledger a panel is given: everything but the ids of applied intents.
    function view(L) {
      const out = Object.assign({}, L);
      delete out.applied;
      return out;
    }

    // ── the run lease ──────────────────────────────────────────────────────
    // A running batch holds it, renewed at every step; another window's Start
    // is refused while it is held. A panel closed mid-run lets it go at once
    // (its port to the service worker closes: src/background.js); one whose
    // service worker was stopped meanwhile leaves it to run out (LEASE_MS).
    async function leaseIntent(msg) {
      let cur;
      try {
        cur = (await deps.get(LEASE_KEY)) || null;
      } catch (e) {
        return storageError('read', e);
      }
      const t = now();
      const other = !!cur && cur.owner !== msg.owner && cur.until > t;
      try {
        if (msg.action === 'peek') return { ok: true, lease: cur, held: other };
        if (msg.action === 'claim' || msg.action === 'renew') {
          if (other) return { ok: false, lease: cur, lost: msg.action === 'renew' };
          await deps.set({ [LEASE_KEY]: { owner: msg.owner, site: msg.site, until: t + LEASE_MS } });
          return { ok: true };
        }
        if (msg.action === 'release') {
          if (cur && cur.owner === msg.owner) await deps.remove(LEASE_KEY);
          return { ok: true };
        }
      } catch (e) {
        return storageError('save', e);
      }
      return { ok: false, error: 'the batch does not know the lease action "' + msg.action + '"' };
    }

    function handle(msg) {
      if (!msg || typeof msg !== 'object') return Promise.resolve({ ok: false, error: 'no intent' });
      if (msg.type === 'fha-batch') return serial(() => applyIntent(msg));
      if (msg.type === 'fha-batch-lease') return serial(() => leaseIntent(msg));
      return Promise.resolve({ ok: false, error: 'not a batch intent' });
    }

    return { handle };
  }

  const api = { create, LEASE_KEY, LEASE_MS, APPLIED_MAX };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) {
    root.FHA = root.FHA || {};
    root.FHA.batchStore = api;
  }
})(typeof self !== 'undefined' ? self : (typeof window !== 'undefined' ? window : null));
