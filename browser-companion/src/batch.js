// batch.js - the owner's batch downloads, the half that drives the tab.
//
// What it is for: a researcher with hundreds of records already found - every
// Newspapers.com clipping she made, every Ancestry record cited in her tree,
// a tree person's FamilySearch sources, a family's Find a Grave memorials -
// can have them saved one at a time, slowly, while she does something else,
// and never the same one twice. Each record is saved by this panel's own
// Capture (panel.js runCapture), so each bundle is the bundle a Capture of
// that record makes: the same files, the same checks, the same words.
//
// ── SWITCHED OFF unless this browser is told otherwise ──────────────────────
// Batch downloads are a power tool. Nothing about them is shown in the panel
// or described in the add-on's READMEs, and with the switch off this file
// builds nothing, reads no ledger and sends nothing: panel.js does not even
// call it, and content.js refuses its questions.
//
// To switch it ON in one browser (developer-facing; there is no button):
//   1. chrome://extensions, find "Plaintext Family History (Capture)";
//   2. click its "service worker" link (or right-click inside the open capture
//      panel and choose Inspect), and in the Console type:
//          chrome.storage.local.set({ batchDownloads: true })
//   3. close the capture panel and open it again: the "Batch downloads" box
//      is at the bottom of the panel.
// To switch it OFF:  chrome.storage.local.remove('batchDownloads')
//      (or set it to false), then reopen the panel. The queues are kept.
//
// The switch lives in the add-on's own storage, not in its files, so
// `fha update-tools` (which rewrites the add-on's files in place, in the same
// folder) leaves it alone: an add-on loaded unpacked keeps its ID - and with
// it its storage - for as long as it is loaded from the same folder. It is
// lost only if the add-on is removed from Chrome, or loaded again from a
// different folder (the archive moved or renamed); then set it again.
// ─────────────────────────────────────────────────────────────────────────────
//
// How one record goes (the run loop, `run`):
//   1. ask the Seen It add-on, for this one record only, whether the archive
//      already holds it: if so it is marked done and nothing is fetched;
//   2. send the tab to the record's own address (built here from its id,
//      never taken from a page), wait for it to load and settle, and stop at
//      a sign-in page, a security check, or a page off the site;
//   3. read the page into the form (panel.js batchReadPage), then press
//      Capture with the record's key: panel.js checks the record shown is the
//      one queued, from the record's own data, before downloading anything;
//   4. pause 45-90 s (lib/capture-batch.js PACING), and on to the next.
// The first "slow down" (429/503) stops the run, and the next Start waits
// out a long break before its first request. A cause that belongs to one
// record (gone, merged, another shown, a clipping that marks no area, a
// record restricted to a FamilySearch center) sets it aside and the run goes
// on; a cause that belongs to the run (signed out, a security check, a busy
// or error page, the tab) stops it. A record refused on two Starts is set
// aside, three records in a row that cannot be saved stop the run, and "Try
// the set-aside ones again" queues the set-aside ones again. A record saved
// without all of its image or photos is "partial", tried again at the next
// Start. One run at a time, one request at a time; Start waits while a
// Capture by hand or a read of the page is under way.
//
// The ledgers (lib/capture-batch.js) live in chrome.storage.local; a copy for
// reading is written beside the bundles, `_{site}-batch-ledger.json`.
//
// Classic script; attaches FHA.batchPanel = { install(hooks) }. panel.js
// calls install only with the switch on.
//
// Code map:
//   SITE_UI                the four batches: names, addresses, buttons
//   load / save / mirror   the ledger in storage, and its reading copy
//   goTo / waitForComplete send the tab to one record, safely
//   readLease / holdLease  one batch at a time across every panel window (read before renewed)
//   halted                 paused, switched off, or taken over by another window?
//   run / one              the run loop above, and one record of it
//   countFail / setAside   one record set aside; several in a row stop the run
//   readClippingsList      Newspapers.com: queue the member's own clippings
//   queueFrom*             fill a queue from a file or a page she has open
//   recordManual           a record saved by hand joins its ledger
//   install                build the box and wire it (switch on only)

(function () {
  const FHA = (window.FHA = window.FHA || {});
  const CB = () => FHA.captureBatch;

  class Paused extends Error {}
  class Stop extends Error {
    constructor(message, kind) {
      super(message);
      this.kind = kind || 'stop';
    }
  }

  const nap = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));

  const SIGNIN_RE = /\/(account\/signin|signin|sign-in|login|secure\/login|auth\/)/i;
  const CHECK_RE = /just a moment|attention required|cloudflare|access denied|verify you are human/i;
  // A page the site sent instead of the record: busy, an error, too many
  // requests. Never a record that is gone - so a stop, never a set-aside.
  // Error-page wording only: a clipping titled "Fire at 503 Elm Street" is
  // a record, not a busy page, so a bare number never counts - only a title
  // that is the code alone, or the code beside "HTTP" or "error".
  const BUSY_RE = /too many requests|service (temporarily )?unavailable|temporarily unavailable|bad gateway|gateway time-?out|internal server error|^\s*(429|50[0234])\s*$|\bHTTP\s*(error\s*)?(429|50[0234])\b|\berror\s*(429|50[0234])\b|\b(429|50[0234])\s*error\b/i;

  const SITE_UI = {
    newspapers: {
      short: 'np',
      title: 'Newspapers.com: every clipping you made',
      hostRe: /^https:\/\/(www\.)?newspapers\.com\//i,
      prefillFlag: 'newspapers',
      listUrl: 'https://www.newspapers.com/clippings/my-clippings/',
    },
    ancestry: {
      short: 'anc',
      title: 'Ancestry: records cited in your tree',
      hostRe: /^https:\/\/(www\.)?ancestry\.com\//i,
      prefillFlag: 'ancestryRecord',
      personAction: 'ancestryPersonSources',
      fileLabel: 'Add the records cited in a GEDCOM file from your Ancestry tree',
      parseFile: (text) => CB().parseGedcom(text),
      personLabel: 'Add the sources of the person in front of you',
      empty: 'Load a GEDCOM file, or add a person’s sources, first.',
    },
    familysearch: {
      short: 'fs',
      title: 'FamilySearch: a tree person’s sources',
      hostRe: /^https:\/\/(www\.)?familysearch\.org\//i,
      prefillFlag: 'familysearchRecord',
      personAction: 'familysearchPersonSources',
      personLabel: 'Add the sources of the person in front of you',
      empty: 'Open a person in the FamilySearch Family Tree and add their sources first.',
    },
    findagrave: {
      short: 'fag',
      title: 'Find a Grave: memorials',
      hostRe: /^https:\/\/(www\.)?findagrave\.com\//i,
      prefillFlag: 'findagraveMemorial',
      personAction: 'findagraveLinks',
      linkHostRe: /^https:\/\/(www\.)?(findagrave\.com|ancestry\.com|familysearch\.org)\//i,
      fileLabel: 'Add the memorials named in a file (a list of addresses, a GEDCOM, a saved page)',
      parseFile: (text) => CB().parseMemorialList(text),
      personLabel: 'Add the memorials the page in front of you links to',
      empty: 'Add the memorials on a page, or load a list, first.',
    },
  };

  function install(hooks) {
    const ui = {};
    let active = null; // { site, tabId, stop, off, lost, fails } while a run goes
    let switchedOff = false; // the switch was turned off while this panel was open

    // ── words ────────────────────────────────────────────────────────────────

    function say(siteName, text, cls) {
      const el = ui[siteName] && ui[siteName].status;
      if (!el) return;
      el.textContent = text;
      el.className = 'stage-result' + (cls ? ' ' + cls : '');
    }

    function fmtWhen(ts) {
      const d = new Date(ts);
      const t = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
      return d.toDateString() === new Date().toDateString()
        ? t : d.toLocaleDateString([], { weekday: 'short' }) + ' ' + t;
    }

    function countsLine(L) {
      if (!L || !L.order.length) return 'Nothing queued yet.';
      const c = CB().counts(L);
      return c.done + ' done, ' +
        (c.partial ? c.partial + ' saved in part (tried again at the next Start), ' : '') +
        c.queued + ' to go' + (c.failed ? ', ' + c.failed + ' set aside' : '') + '.';
    }

    function breakLabel(siteName, kind) {
      const name = CB().SITES[siteName].name;
      return {
        between: 'Pausing before the next one',
        short: 'Taking a short break (about half an hour)',
        ratelimit: name + ' asked us to slow down, so the batch waits about half an hour before its next request',
        long: name + ' asked us to slow down three times running, so the batch waits about four hours',
      }[kind] || 'Waiting';
    }

    function buttons() {
      for (const name of Object.keys(ui)) {
        const u = ui[name];
        u.start.disabled = !!active;
        u.pause.disabled = !active || active.site !== name;
        if (u.relist) u.relist.disabled = !!active;
        u.retry.disabled = !!active;
      }
    }

    // ── the ledger ───────────────────────────────────────────────────────────

    function loadRaw(siteName) {
      return new Promise((resolve) => {
        try {
          const k = CB().SITES[siteName].storageKey;
          chrome.storage.local.get(k, (o) => resolve(chrome.runtime.lastError ? null : CB().repair(o && o[k])));
        } catch (e) {
          resolve(null);
        }
      });
    }

    async function load(siteName) {
      const L = (await loadRaw(siteName)) || CB().newLedger();
      if (siteName === 'newspapers') {
        if (L.listCursor == null) L.listCursor = 0;
        if (L.listComplete == null) L.listComplete = false;
        if (L.user === undefined) L.user = null;
      }
      return L;
    }

    // Write the ledger, folding in what another panel window stored
    // meanwhile so this save never drops it. `mirror` also writes the reading
    // copy beside the bundles; storage is the working copy, so a failed
    // mirror is never a failed save.
    async function save(siteName, L, mirror) {
      CB().mergeDone(L, await loadRaw(siteName));
      const k = CB().SITES[siteName].storageKey;
      await new Promise((resolve) => chrome.storage.local.set({ [k]: L }, () => resolve()));
      if (mirror) writeMirror(siteName, L);
    }

    function writeMirror(siteName, L) {
      try {
        if (!chrome.downloads || !chrome.downloads.download) return;
        const body = JSON.stringify(CB().ledgerFileBody(siteName, L), null, 2) + '\n';
        const url = URL.createObjectURL(new Blob([body], { type: 'application/json' }));
        chrome.downloads.download({
          url,
          filename: String(hooks.folder() || 'fha-inbox').replace(/\/+$/, '') + '/' + CB().SITES[siteName].ledgerFile,
          conflictAction: 'overwrite',
          saveAs: false,
        }, (id) => {
          void chrome.runtime.lastError;
          setTimeout(() => URL.revokeObjectURL(url), 30000);
          // Keep the downloads list readable: forget the entry (never the file).
          if (id != null && chrome.downloads.erase) {
            setTimeout(() => chrome.downloads.erase({ id }, () => void chrome.runtime.lastError), 5000);
          }
        });
      } catch (e) {
        /* the reading copy is a convenience */
      }
    }

    // ── the tab ──────────────────────────────────────────────────────────────

    function waitForComplete(tabId, timeoutMs) {
      return new Promise((resolve, reject) => {
        const done = (fn, arg) => {
          clearTimeout(timer);
          chrome.tabs.onUpdated.removeListener(onUpd);
          fn(arg);
        };
        const onUpd = (id, info) => {
          if (id === tabId && info && info.status === 'complete') done(resolve);
        };
        const timer = setTimeout(() => done(reject, new Stop('the page did not finish loading')), timeoutMs);
        chrome.tabs.onUpdated.addListener(onUpd);
      });
    }

    // Send the batch's tab to `url` as a person would, let it settle, and
    // refuse to go on past a sign-in page, a security check or another site.
    async function goTo(siteName, url) {
      const name = CB().SITES[siteName].name;
      const tabId = active.tabId;
      if (hooks.state.tabId !== tabId) throw new Stop('the panel was moved to another tab');
      const loaded = waitForComplete(tabId, CB().PACING.loadTimeoutMs);
      await new Promise((resolve, reject) => chrome.tabs.update(tabId, { url }, () =>
        (chrome.runtime.lastError
          ? reject(new Stop('the batch’s tab could not be moved on (' + chrome.runtime.lastError.message + ')'))
          : resolve())));
      await loaded;
      await nap(CB().between(CB().PACING.settleMs));
      const tab = await hooks.getTab(tabId);
      if (!tab) throw new Stop('the batch’s tab was closed');
      if (CHECK_RE.test(tab.title || '')) throw new Stop(name + ' showed a security check page');
      if (BUSY_RE.test(tab.title || '')) {
        const e = new Stop(name + ' answered with a busy or error page ("' + String(tab.title).slice(0, 80) + '")',
          /429|too many requests|503|unavailable/i.test(tab.title) ? 'slowdown' : 'stop');
        e.atRecord = true;
        throw e;
      }
      if (!SITE_UI[siteName].hostRe.test(tab.url || '')) throw new Stop('the tab ended up outside ' + name);
      let path = '';
      try {
        path = new URL(tab.url).pathname;
      } catch (e) {
        /* checked above */
      }
      if (SIGNIN_RE.test(path)) throw new Stop(name + ' asked you to sign in');
    }

    // Between steps: has the owner paused, switched batches off, or another
    // window taken the batch over?
    function halted() {
      if (active.off) throw new Stop('batch downloads were switched off in this browser', 'halt');
      if (active.lost) throw new Stop(active.lost, 'halt');
      if (active.stop) throw new Paused();
    }

    async function sleepUntil(siteName, L, until, label) {
      while (Date.now() < until) {
        halted();
        await holdLease(siteName);
        say(siteName, label + '. Next about ' + fmtWhen(until) + '. ' + countsLine(L));
        await nap(Math.min(1000, until - Date.now()));
      }
    }

    // ── Newspapers.com: the member's own clippings ──────────────────────────

    async function readClippingsList(L) {
      if (L.listComplete) return;
      await goTo('newspapers', SITE_UI.newspapers.listUrl);
      await hooks.injectContent(active.tabId);
      let pages = 0;
      while (!L.listComplete) {
        halted();
        if (++pages > CB().NP_LIST_MAX_PAGES) {
          throw new Stop('your clippings list is longer than one Start reads (' + CB().NP_LIST_MAX_PAGES * 24 +
            ' clippings); press Start again to read on from where it stopped');
        }
        await holdLease('newspapers');
        say('newspapers', 'Reading your clippings list (' + L.order.length + ' so far)…');
        let r;
        try {
          r = await hooks.sendToTab(active.tabId, { action: 'newspapersList', cursor: L.listCursor, user: L.user });
        } catch (e) {
          r = { ok: false, error: 'the page did not answer (' + e.message + ')' };
        }
        if (!r || !r.ok) {
          const why = (r && r.error) || 'no answer from the page';
          if (CB().classifyError(why) === 'slowdown') {
            CB().planRateLimit(L, Date.now());
            await save('newspapers', L, true);
            throw new Stop('Newspapers.com asked us to slow down while your clippings list was read', 'slowdown');
          }
          throw new Stop('could not read your clippings list: ' + why);
        }
        if (!L.user) L.user = r.user;
        CB().addClippings(L, r.clippings, L.user);
        const moved = r.nextCursor != null && String(r.nextCursor) !== String(L.listCursor);
        L.listComplete = !r.more || !r.clippings.length || !moved;
        if (moved) L.listCursor = r.nextCursor;
        await save('newspapers', L, false);
        if (!L.listComplete) await nap(CB().between(CB().PACING.listPauseMs));
      }
      CB().logEvent(L, 'list read: ' + L.order.length + ' clippings by ' + L.user);
      await save('newspapers', L, true);
    }

    // ── one batch at a time, across every panel window ──────────────────────
    // The capture panel can be open in several windows; each could start a
    // batch of its own, and two would ask a site twice as often. A running
    // batch holds a lease in storage, renewed at every step; another window's
    // Start is refused while it is held. A panel closed mid-run leaves its
    // lease to run out (LEASE_MS) - never a batch that cannot be started.
    const LEASE_KEY = 'batchLease';
    const LEASE_MS = 10 * 60 * 1000;
    const me = Math.random().toString(36).slice(2) + Date.now().toString(36);

    function readLease() {
      return new Promise((resolve) => chrome.storage.local.get(LEASE_KEY, (o) =>
        resolve(!chrome.runtime.lastError && o && o[LEASE_KEY] ? o[LEASE_KEY] : null)));
    }
    // Renewed at every step and once a minute on a timer (a long capture
    // waiting out a site's slow-downs must not let it run out), written at
    // most once a minute. Before it is renewed, it is read: if another
    // window holds it now, this run stops at its next step instead of
    // writing over it.
    let leaseAt = 0;
    async function holdLease(siteName, force) {
      if (!active || (!force && Date.now() - leaseAt < 60 * 1000)) return;
      leaseAt = Date.now();
      const lease = await readLease();
      if (lease && lease.owner !== me && lease.until > Date.now()) {
        active.lost = 'another panel window started a batch while this one ran, so this one stopped';
        return;
      }
      await new Promise((resolve) => chrome.storage.local.set(
        { [LEASE_KEY]: { owner: me, site: siteName, until: Date.now() + LEASE_MS } }, () => resolve()));
    }
    async function dropLease() {
      const lease = await readLease();
      if (lease && lease.owner === me) await new Promise((resolve) => chrome.storage.local.remove(LEASE_KEY, () => resolve()));
    }

    // ── the run ──────────────────────────────────────────────────────────────

    // A capture saving or the panel reading a page: the batch would move the
    // tab out from under it.
    function panelBusy() {
      const st = hooks.state;
      if (st.busy) return 'A capture is still saving. Press Start once it says where it was saved.';
      if (st.rereading || st.navInFlight) return 'The panel is still reading the page. Press Start in a moment.';
      return null;
    }

    async function run(siteName) {
      if (active) {
        say(siteName, 'Another batch is running. Press its Pause, wait until it has stopped, then press Start here.', 'warn');
        return;
      }
      if (panelBusy()) {
        say(siteName, panelBusy(), 'warn');
        return;
      }
      if (switchedOff) {
        say(siteName, 'Batch downloads were switched off in this browser. Reopen the panel.', 'warn');
        return;
      }
      const lib = CB().SITES[siteName];
      const tabId = hooks.state.tabId;
      const tab = tabId ? await hooks.getTab(tabId) : null;
      if (!tab || !SITE_UI[siteName].hostRe.test(tab.url || '')) {
        say(siteName, 'Open this panel on a ' + lib.name + ' tab (press ↻ Re-read page there): that tab is the one ' +
          'the batch moves from record to record. Then press Start.', 'warn');
        return;
      }
      if (active) return;
      const lease = await readLease();
      if (lease && lease.owner !== me && lease.until > Date.now()) {
        say(siteName, 'A batch is running in another window (or was, until a few minutes ago). Pause it there, ' +
          'or wait until about ' + fmtWhen(lease.until) + ', then press Start here.', 'warn');
        return;
      }
      if (active) return;
      if (panelBusy()) {
        say(siteName, panelBusy(), 'warn');
        return;
      }
      active = { site: siteName, tabId, stop: false, fails: 0 };
      await holdLease(siteName, true);
      const leaseTimer = setInterval(() => holdLease(siteName, true).catch(() => {}), 60 * 1000);
      buttons();
      hooks.setDriving(true);
      let L = null;
      try {
        L = await load(siteName);
        CB().ensurePacing(L);
        // Saved in part last time: asked about and captured again now.
        const again = CB().requeuePartials(L);
        if (again) CB().logEvent(L, 'trying again: ' + again + ' saved in part');
        await save(siteName, L, false);
        // The back-off: after a "slow down", no request before the break is over.
        if (L.pacing.resumeAt > Date.now()) {
          await sleepUntil(siteName, L, L.pacing.resumeAt, breakLabel(siteName, L.pacing.resumeReason));
        }
        if (siteName === 'newspapers') await readClippingsList(L);
        if (!CB().nextPending(L)) {
          say(siteName, 'Nothing to download. ' + countsLine(L) + (SITE_UI[siteName].empty ? ' ' + SITE_UI[siteName].empty : ''), 'warn');
          return;
        }
        for (let looked = 0; ; looked++) {
          CB().mergeDone(L, await loadRaw(siteName));
          const item = CB().nextPending(L);
          if (!item) break;
          halted();
          if (looked >= CB().MAX_PER_RUN) throw new Stop('one Start looks at most ' + CB().MAX_PER_RUN + ' records; press Start again');
          await one(siteName, L, item);
        }
        L.pacing.resumeAt = 0;
        CB().logEvent(L, 'finished: ' + countsLine(L));
        await save(siteName, L, true);
        const c = CB().counts(L);
        say(siteName, 'Finished. ' + countsLine(L) +
          (c.failed ? ' The set-aside ones, with the reasons, are in ' + lib.ledgerFile + ' in the download folder.' : ''),
          c.failed ? 'warn' : 'ok');
      } catch (e) {
        if (e instanceof Paused) {
          if (L) {
            CB().logEvent(L, 'paused');
            await save(siteName, L, true);
          }
          say(siteName, 'Paused. ' + countsLine(L) + ' Press Start to carry on.');
        } else {
          const slow = e instanceof Stop && e.kind === 'slowdown';
          if (L) {
            // A halt (switched off, another window took over) keeps the pacing:
            // a break under way is still owed when batches start again.
            if (!slow && e.kind !== 'halt' && L.pacing) L.pacing.resumeAt = 0;
            // A "slow down" seen before any capture (a busy page, the list):
            // the break starts now if it has not already.
            if (slow && !(L.pacing && L.pacing.resumeAt > Date.now())) CB().planRateLimit(L, Date.now());
            CB().logEvent(L, 'STOPPED: ' + e.message);
            await save(siteName, L, true);
          }
          say(siteName, 'Stopped: ' + e.message + '. ' + (slow
            ? 'Press Start to carry on; it waits until about ' + fmtWhen(L.pacing.resumeAt) + ' before its next request. '
            : e.kind === 'halt' ? ''
              : 'Look at the tab (signed in? a security check?), then press Start to carry on. ') + countsLine(L), 'error');
        }
      } finally {
        clearInterval(leaseTimer);
        active = null;
        hooks.setDriving(false);
        buttons();
        await dropLease().catch(() => {});
      }
    }

    // One queued record: Seen It first, then the tab, then Capture.
    async function one(siteName, L, item) {
      const lib = CB().SITES[siteName];
      await holdLease(siteName, true);
      halted();
      // The address is rebuilt from the record's checked id every time, never
      // taken as stored: a queue kept by an older copy of the add-on may hold
      // any address at all.
      const url = lib.keyOf(item) ? lib.urlOf(item) : null;
      if (!url) {
        await setAside(siteName, L, item, 'its id or address is not one the batch can save as the record queued');
        return;
      }
      // A record saved in part before is held only once what was missing is
      // saved: its scan (img:/fsimg:), or for a memorial every photo (fag:,
      // which Seen It claims only then).
      const sk = item.again && item.imageKey ? item.imageKey : lib.seenIt(item);
      const held = (await hooks.seenItHeld([sk]))[sk];
      if (held) {
        CB().markHeld(L, item.key, held.where === 'archive'
          ? 'in the archive' + (held.source ? ', source ' + held.source : '')
          : 'waiting in the inbox to be filed');
        active.fails = 0;
        await save(siteName, L, true);
        return;
      }
      say(siteName, 'Saving ' + (item.title || lib.name + ' ' + item.key) + '… ' + countsLine(L));
      try {
        await goTo(siteName, url);
      } catch (e) {
        // A busy or error page at this record's address stops the run (and
        // takes the break on a "slow down"); the same page there again on a
        // later Start sets the record aside, so it never holds up the rest.
        if (e instanceof Stop && e.atRecord) {
          const it = L.items[item.key];
          it.stops = (it.stops || 0) + 1;
          if (it.stops >= CB().MAX_TRIES) CB().markSetAside(L, item.key, e.message + ' (' + it.stops + ' times)');
        }
        throw e;
      }
      const page = await hooks.readPage();
      // A page the panel cannot read at all stops the run (the next one would
      // fare no better); a page that reads but is not a record is set aside.
      if (!page.ok) throw new Stop(page.error + ' (' + url + ')');
      let out;
      if (!page.prefill[SITE_UI[siteName].prefillFlag]) {
        out = { ok: false, error: 'the page it opened is not a record page (it may have moved or been removed)' };
      } else {
        out = await hooks.capture(lib.captureKey(item));
      }
      out = out || { ok: false, error: 'nothing was saved' };
      // The site said "slow down" during this capture, saved or not: the
      // break comes before the next request, whatever else went wrong.
      const metSlowDown = !!(out.slowDown || out.rateHits > 0);
      if (out.ok) {
        const bundle = siteName === 'newspapers'
          ? ((out.results || []).find((r) => r.ok) || {}).bundle : out.bundle;
        if (out.partial) {
          CB().markPartial(L, item.key, bundle || '(staged)', out.note || null);
          if (out.imageKey && L.items[item.key]) L.items[item.key].imageKey = out.imageKey;
        } else {
          CB().markDone(L, item.key, bundle || '(staged)', out.note || null);
        }
        active.fails = 0;
        if (metSlowDown) {
          CB().planRateLimit(L, Date.now());
          await save(siteName, L, true);
          throw new Stop(lib.name + ' asked us to slow down while this one was saved', 'slowdown');
        }
        // The record page itself loaded, so its session is there: only an
        // unmistakable lost login (401, "asked you to sign in") stops the
        // run - never a download refused for one collection (an Ancestry
        // 403 says "sign in ... or the collection may not allow downloads").
        if (/asked you to sign in|HTTP 401/i.test(out.note || '')) {
          await save(siteName, L, true);
          throw new Stop(lib.name + ' asked you to sign in (the last one was saved without its image; it is tried ' +
            'again at the next Start)');
        }
      } else {
        const why = out.error || 'nothing was saved';
        const kind = metSlowDown ? 'slowdown' : CB().classifyError(why);
        if (kind === 'slowdown') {
          CB().planRateLimit(L, Date.now());
          await save(siteName, L, true);
          throw new Stop(lib.name + ' asked us to slow down (' + why + ')', 'slowdown');
        }
        if (kind === 'stop') throw new Stop(why);
        if (kind === 'refused') {
          // Stop at the first refusal, with its cause. The same record
          // refused again on a later Start is set aside instead, so one
          // record can never hold up the rest of the queue.
          const it = L.items[item.key];
          it.stops = (it.stops || 0) + 1;
          if (it.stops < CB().MAX_TRIES) throw new Stop(why);
          await setAside(siteName, L, item, why + ' (refused ' + it.stops + ' times)');
        } else if (kind === 'retry') {
          CB().markRetry(L, item.key, why);
          countFail(why);
        } else {
          await setAside(siteName, L, item, why);
        }
      }
      const plan = CB().planAfterCapture(L, Date.now());
      await save(siteName, L, true);
      if (CB().nextPending(L)) await sleepUntil(siteName, L, plan.until, breakLabel(siteName, plan.kind));
    }

    // Several records in a row that cannot be saved say more about the site
    // than about the records: the run stops, and they can be queued again.
    function countFail(why) {
      active.fails = (active.fails || 0) + 1;
      if (active.fails >= CB().MAX_FAILS_IN_A_ROW) {
        throw new Stop(active.fails + ' records in a row could not be saved (the last: ' + why + '). The site may be ' +
          'having trouble. Those set aside can be queued again with "Try the set-aside ones again"');
      }
    }

    async function setAside(siteName, L, item, why) {
      CB().markSetAside(L, item.key, why);
      await save(siteName, L, true);
      countFail(why);
    }

    // ── filling a queue ──────────────────────────────────────────────────────

    // Every button says at once that it is working, and any failure is said:
    // a click never ends in silence.
    const guarded = (siteName, label, fn) => async () => {
      say(siteName, label + '…');
      try {
        await fn();
      } catch (e) {
        say(siteName, 'Could not finish: ' + ((e && e.message) || e) + '. Reload the page and try again.', 'error');
      }
    };

    async function queueRecords(siteName, records, from, what) {
      const L = await load(siteName);
      const added = CB().addRecords(siteName, L, records, from);
      CB().logEvent(L, what + ': ' + records.length + ' found, ' + added + ' new to the queue');
      await save(siteName, L, true);
      say(siteName, what + ': ' + records.length + ' found, ' + added + ' new to the queue. ' + countsLine(L) +
        (added ? ' Press Start to download them.' : ''), added ? 'ok' : '');
    }

    async function queueFromFile(siteName, input) {
      const f = input.files && input.files[0];
      if (!f) return;
      const recs = SITE_UI[siteName].parseFile(await f.text());
      input.value = '';
      await queueRecords(siteName, recs, 'file: ' + f.name, f.name);
    }

    // Read the page she is looking at (any tab, not the batch's own while it
    // runs) for what to queue.
    async function queueFromPage(siteName) {
      const site = SITE_UI[siteName];
      const tab = await hooks.queryActiveTab();
      if (!tab || !tab.id || !(site.linkHostRe || site.hostRe).test(tab.url || '')) {
        say(siteName, site.linkHostRe
          ? 'Switch to a Find a Grave, Ancestry or FamilySearch page that links to memorials, then press again.'
          : 'Switch to a tab showing a person on ' + CB().SITES[siteName].name + ', then press again.', 'warn');
        return;
      }
      if (active && tab.id === active.tabId) {
        say(siteName, 'That tab is the one the batch is moving. Open the person in another tab and press there.', 'warn');
        return;
      }
      await hooks.injectContent(tab.id);
      const r = await hooks.sendToTab(tab.id, { action: site.personAction });
      if (!r || !r.ok) {
        say(siteName, 'Could not read this page: ' + ((r && r.error) || 'no answer') + '.', 'warn');
        return;
      }
      const who = r.person || 'This page';
      if (!r.sources.length) {
        say(siteName, r.sectionFound === false
          ? 'No sources section on this page. Open the person’s Facts page and press again.'
          : who + ' has no ' + CB().SITES[siteName].name + ' records to queue' +
            (r.other ? ' (' + r.otherWhat + ')' : '') + '.', 'warn');
        return;
      }
      await queueRecords(siteName, r.sources.map((s) => Object.assign({ persons: r.person ? [r.person] : [] }, s)),
        'page: ' + (r.person || tab.url), who + (r.other ? ' (also ' + r.otherWhat + ', not queued)' : ''));
    }

    // A record saved by hand with Capture goes in its ledger too, so a batch
    // never downloads it again.
    async function recordManual(out) {
      if (!out || !out.ok || !CB().SITES[out.site]) return;
      const L = await load(out.site);
      if (out.site === 'newspapers') {
        for (const r of out.results || []) {
          if (!r.ok) continue;
          const key = String(r.id);
          if (!L.items[key]) {
            L.items[key] = { id: Number(r.id), url: null, title: r.title || '', state: 'queued', tries: 0 };
            L.order.push(key);
          }
          CB().markDone(L, key, r.bundle, 'saved by hand');
        }
      } else {
        const key = CB().SITES[out.site].keyOf(out.record);
        if (!key) return;
        CB().addRecords(out.site, L, [Object.assign({ title: out.record.name || '' }, out.record)], 'by hand');
        if (out.partial) {
          CB().markPartial(L, key, out.bundle, 'saved by hand' + (out.note ? ', ' + out.note : ''));
          if (out.imageKey) L.items[key].imageKey = out.imageKey;
        } else {
          CB().markDone(L, key, out.bundle, 'saved by hand');
        }
      }
      await save(out.site, L, false);
    }

    // ── the box ──────────────────────────────────────────────────────────────

    function el(tag, props, parent) {
      const e = document.createElement(tag);
      Object.assign(e, props || {});
      if (parent) parent.appendChild(e);
      return e;
    }
    function tagged(e, name) {
      e.dataset.batch = name;
      return e;
    }

    const box = el('details', { className: 'batch-box' });
    tagged(box, 'box');
    el('summary', { textContent: 'Batch downloads' }, box);
    el('p', {
      className: 'mini',
      textContent: 'Saves many records, one at a time and slowly, from the tab this panel is on. Each is saved ' +
        'exactly as Capture would, into the download folder, and never twice. Records the archive already ' +
        'has (as the Seen It add-on knows) are skipped.',
    }, box);
    for (const siteName of Object.keys(SITE_UI)) {
      const site = SITE_UI[siteName];
      const u = (ui[siteName] = {});
      const sec = el('div', { className: 'batch-site' }, box);
      el('strong', { textContent: site.title }, sec);
      u.status = tagged(el('div', { className: 'stage-result', textContent: 'Reading the queue…' }, sec), site.short + '-status');
      if (site.parseFile) {
        const lab = el('label', { className: 'mini', textContent: site.fileLabel + ' ' }, sec);
        u.file = tagged(el('input', { type: 'file' }, lab), site.short + '-file');
        u.file.addEventListener('change', guarded(siteName, 'Reading the file', () => queueFromFile(siteName, u.file)));
      }
      if (site.personAction) {
        u.person = tagged(el('button', { type: 'button', textContent: site.personLabel }, sec), site.short + '-page');
        u.person.addEventListener('click', guarded(siteName, 'Reading the page', () => queueFromPage(siteName)));
      }
      if (siteName === 'newspapers') {
        u.relist = tagged(el('button', { type: 'button', textContent: 'Read my clippings list again' }, sec), 'np-relist');
        u.relist.addEventListener('click', guarded(siteName, 'Marking the list to be read again', async () => {
          const L = await load(siteName);
          L.listComplete = false;
          L.listCursor = 0;
          // Read for whoever is signed in then (content.js always asks for
          // the member signed in on the page).
          L.user = null;
          await save(siteName, L, false);
          say(siteName, 'Your list is read again at the next Start; the clippings already done stay done. ' + countsLine(L));
        }));
      }
      u.retry = tagged(el('button', { type: 'button', textContent: 'Try the set-aside ones again' }, sec), site.short + '-retry');
      u.retry.addEventListener('click', guarded(siteName, 'Queuing the set-aside ones again', async () => {
        if (active) return say(siteName, 'Pause the batch first, then press this again.', 'warn');
        const L = await load(siteName);
        const n = CB().requeueSetAside(L);
        await save(siteName, L, true);
        say(siteName, (n ? n + ' set-aside record' + (n === 1 ? ' is' : 's are') + ' queued again; press Start to try them. '
          : 'Nothing is set aside. ') + countsLine(L), n ? 'ok' : '');
      }));
      u.start = tagged(el('button', { type: 'button', textContent: 'Start' }, sec), site.short + '-start');
      u.start.addEventListener('click', () => run(siteName));
      u.pause = tagged(el('button', { type: 'button', textContent: 'Pause' }, sec), site.short + '-pause');
      u.pause.addEventListener('click', () => {
        if (!active || active.site !== siteName) return;
        active.stop = true;
        say(siteName, 'Pausing after the record being saved now…');
      });
    }
    document.body.appendChild(box);
    buttons();

    // Batch downloads switched off while this panel is open: a run going now
    // stops after the record being saved, and Start answers nothing more.
    if (chrome.storage.onChanged && chrome.storage.onChanged.addListener) {
      chrome.storage.onChanged.addListener((changes, area) => {
        if (area !== 'local' || !changes || !changes.batchDownloads) return;
        if (changes.batchDownloads.newValue === true) return;
        switchedOff = true;
        if (active) {
          active.off = true;
          say(active.site, 'Batch downloads were switched off - stopping after the record being saved now…', 'warn');
        }
      });
    }

    // Each queue's state, as it was left.
    (async () => {
      for (const siteName of Object.keys(SITE_UI)) {
        const L = await loadRaw(siteName);
        if (active && active.site === siteName) continue;
        const waiting = L && L.pacing && L.pacing.resumeAt > Date.now()
          ? ' It waits until about ' + fmtWhen(L.pacing.resumeAt) + ' before its next request.' : '';
        say(siteName, 'Not running. ' + countsLine(L) + waiting);
      }
    })().catch(() => {});

    return { recordManual, run, box };
  }

  FHA.batchPanel = { install };
})();
