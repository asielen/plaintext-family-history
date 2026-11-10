// panel.js - the capture panel controller (TOOLING_INGESTION §5.3, the 4 phases).
//
// This wires the side-panel DOM to the in-page content script and the bundle
// writer. It holds NO authoritative knowledge of any site - it gathers a generic
// pre-fill, lets the human nudge it, captures the evidence, and stages a bundle
// (§5.5: the browser captures; Python extracts). The panel's only insistence is
// on the asset (Step 2), the one thing that can't be redone once the page is
// closed; everything else is optional and a hurried human clicks straight to
// Capture.
//
// The design (the capture-panel mockup in the workshop repo's design notes;
// not vendored into archives) composes two independent choices in Step 2:
//   • a "keep a copy of the whole page" CHECKBOX (its own toggle, default on)
//     that produces a self-contained single-file snapshot (role `webpage`), AND
//   • a two-option evidence picker: "Yes, save the actual file" (a pre-filled
//     url box from the detected image plus a drop box; no separate fetch button,
//     Capture pulls it) or "No, the page copy is the record".
// A capture can therefore carry BOTH a page copy and a separate evidence file
// (the "both" case): the bundle records them in capture.json's `assets:` list
// (schema 2) and the always-saved raw page.html stays the scrape source.
//
// Flow:
//   init     → find the active tab, inject content.js, pull the pre-fill (P1→P2)
//   step 2   → page-copy toggle + evidence picker (url or drop), provisional flag
//   capture  → grab fresh page.html, build the page copy + evidence, stage bundle
//
// Classic script; depends on window.FHA.{captureJson,bundle,nativeHost,newspapers,findagrave}.
// The owner's batch downloads (window.FHA.batchPanel, src/batch.js) are handed a
// few hooks only when this browser has them switched on (init, batchHooks);
// switched off, the panel never calls them.

(function () {
  const { captureJson, bundle, nativeHost } = window.FHA;

  // Friendly labels over the controlled source_type vocabulary (_lib.SOURCE_TYPES).
  // "Auto-detect" (blank) is the safe default - the Python recipe sets the real
  // type from page.html; a stored value here only pre-empts that when the human
  // chooses one. Every value is in-vocabulary so ingest never refuses the stub.
  const SOURCE_TYPES = [
    ['census', 'Census'],
    ['vital-record', 'Vital record (birth/marriage/death)'],
    ['newspaper', 'Newspaper'],
    ['photo', 'Photo'],
    ['interview', 'Interview'],
    ['letter', 'Letter'],
    ['military-record', 'Military record'],
    ['land-record', 'Land record'],
    ['probate', 'Probate / will'],
    ['directory', 'Directory'],
    ['dna', 'DNA'],
    ['book', 'Book'],
    ['website', 'Website'],
    ['artifact', 'Artifact'],
    ['proof-argument', 'Proof argument'],
    ['ephemera', 'Ephemera'],
    ['other', 'Other'],
  ];

  const RECIPE_LABELS = {
    ancestry: 'an Ancestry record',
    familysearch: 'a FamilySearch record',
    newspapers: 'a Newspapers.com clipping',
    findagrave: 'a Find a Grave memorial',
  };

  // ── panel state ───────────────────────────────────────────────────────────
  const state = {
    tabId: null,
    prefill: null,
    // The dropped/chosen evidence file, when the human supplies one directly
    // (the "or drop a file" path). A url in the box is pulled at Capture time.
    droppedAsset: null, // { blob, ext, filename } | null
    folder: 'fha-inbox',
    busy: false,
    // True on an Ancestry image-viewer page: the "Yes, save the actual file"
    // flow then fetches the full-res record in-session (the seamless auto path)
    // instead of pulling the URL box (which would be the thumbnail). Set from
    // the prefill; drives buildEvidence() and the Ancestry note.
    ancestryViewer: false,
    // True on an open-archive IIIF page: the visible <img> is a downsized
    // derivative, so the "Yes" flow auto-fetches the FULL IIIF image instead of
    // pulling the URL box. Set from the prefill; drives buildEvidence().
    iiif: false,
    // True on an Ancestry record page (/search/collections/{dbId}/records/…):
    // Capture saves the record's own index (record-data.json) and its linked
    // scan, if it has one. Set from the prefill; drives captureAncestryRecord.
    ancRecord: false,
    // True on a photo someone shared on Ancestry (/mediaui-viewer/…/media/{id}):
    // Capture saves the photo the page shows, with its Details, filed as a
    // photo. Set from the prefill; drives captureAncestryPhoto.
    ancPhoto: false,
    // On a shared photo: { keys, people } - the Details the form was filled
    // from, and the names it lists (lowercased). Capture stages nothing new
    // to her (captureAncestryPhoto).
    photoShown: null,
    // FamilySearch: a record (ark 1:1, or the viewer opened from one) and a
    // bare image viewer (ark 3:1). fsCheck is the answer to "can this page's
    // image be saved from here?", asked when the page is read (null until it
    // is; fsCheckSeq drops an answer for a page the tab has left). fsImages is
    // ancImages for FamilySearch: "{imageId}" -> the record that holds it.
    fsRecord: false,
    fsImage: false,
    fsCheck: null,
    fsCheckSeq: 0,
    fsImages: {},
    // Census pages saved in this sitting, "{dbId}:{imageId}" -> the bundle that
    // holds the page. Everyone in a household has their own record but they
    // share one page image, so the next member's capture does not download it
    // again. Lives as long as the panel, like the form it serves.
    ancImages: {},
    // Find a Grave: a memorial page (Capture saves its facts and citation and
    // its photos), and the photos saved in this sitting, "{photoId}" -> the
    // memorial capture that holds it, so capturing the memorial again (after
    // Find a Grave asked us to slow down, say) fetches only the rest.
    fagRecord: false,
    fagPhotos: {},
    // Newspapers.com: the newspapersInfo result for the page in the tab (page
    // id, size, image pass, YOUR clippings on it) and which of them to save.
    // On Capture the panel rebuilds the page from tiles (lib/newspapers.js).
    np: null,
    npSelected: new Set(),
    // On a Newspapers.com page: why automatic saving is unavailable, if it is.
    npPage: false,
    npError: null,
    // True while the clippings for the page in the tab are still being read.
    // Capture waits for them rather than staging the previous page's clippings
    // under this page's form. npSeq numbers each read, so an answer that comes
    // back after the tab has moved on is dropped, never installed.
    npLoading: false,
    // Form fields the human has typed in since this record's prefill. The
    // newspaper data arriving later must not overwrite them (#211 review).
    userEdited: new Set(),
    // True from the moment the tab moves to a new address until the panel
    // has read the new page. Capture refuses meanwhile, or it would pair the
    // old clipping with the new page and form (#211 audit).
    navInFlight: false,
    npSeq: 0,
    // The URL the form was last pre-filled for, so the batch-capture refresh
    // (re-running prefill when the tab navigates to a new record) can tell a
    // genuine navigation from a same-page update.
    prefilledUrl: null,
    // True when the tab navigated while a capture was mid-flight (state.busy):
    // refreshing then would swap the form out from under the bundle being
    // staged, so refreshOnNavigation parks the event here and capture()'s
    // finally block replays it once. A boolean, not a URL: the live tab is
    // queried at replay time, so a double navigation during one capture still
    // lands on the record actually in the tab. It lives in the panel page's
    // state (not the service worker's), so it exists exactly as long as the
    // form it protects; if the panel closes mid-capture, both die together.
    pendingNav: false,
    // The folder the browser really staged the last bundle into, absolute, as
    // the downloads API reported it - '' until a capture in this sitting says
    // so. The handoff command names a location only from this: the download
    // directory is a browser setting, so it cannot be derived from `folder`.
    stagedDir: '',
    // The fetch log's tally for the last Capture ({ ok, failed }), or null
    // before one: the automatic-saving box turns it into a tick or a cross.
    autoOutcome: null,
    // A fingerprint (SHA-256) of every file the automatic fetches brought back
    // in this Capture, and in the last Capture that was saved. A file that
    // matches the last one says so in the log: on a new record that is the
    // tell of a tab still showing the previous picture. Memory only.
    thisCaptureSigs: new Set(),
    lastCaptureSigs: new Set(),
    // The line the panel itself put in the Notes box for a FamilySearch image
    // it cannot save, so it can be taken out again if the image turns out to
    // be saveable after all. Never anything she typed.
    fsNoteLine: null,
    // True while a Re-read button reads the page: Capture waits for it.
    rereading: false,
    // Whether the last Capture staged its bundle: null while none has
    // finished, true once saved, false when it failed after fetching. The
    // automatic-saving box says "saved" only on true.
    autoSaved: null,
    // Log lines trimmed past AUTO_LOG_MAX, and the summary line saying so.
    autoHidden: { ok: 0, failed: 0 },
    autoMore: null,
    // An automatic file on a viewer did not come: the address box shows
    // again (updateAddressRow).
    autoMissed: false,
    // The newest read of the page that may fill the form (beginRead).
    readSeq: 0,
    // The last read of prefilledUrl could not read the page: the form is
    // empty for that address, and its next navigation event reads it again.
    prefillFailed: false,
    // What she has touched since the last read that filled the form: the
    // fields she typed in, the file address, the people, a dropped file, the
    // clipping ticks. A retry of a failed read fills only the rest.
    touched: new Set(),
    // The answers still on their way for the page just read: its newspaper
    // clippings (loadNewspapers) and its FamilySearch image check. A batch
    // download waits for both before it presses Capture.
    npReading: null,
    fsChecking: null,
    // The owner's batch downloads (src/batch.js), when this browser has them
    // switched on - null otherwise, and then nothing below ever calls them.
    // `batchDriving` is true while a batch moves this tab from record to
    // record: the batch reads each page itself, and Capture waits.
    batch: null,
    batchDriving: false,
    // A "slow down" was met somewhere in the capture under way (noteSlowDown).
    slowDownSeen: false,
    // A "slow down" (429/503) met while the page in the form was read -
    // its FamilySearch image check, its Newspapers.com clippings - before
    // any Capture. A batch takes its break on it before another request,
    // and a capture of this page starts out knowing it (PR #224 Codex 4504294846).
    preflightSlowDown: false,
    preflightSlowDownSite: null,
    // The batch sites whose hosts said "slow down" in the capture under way.
    slowDownSites: new Set(),
  };

  const $ = (id) => document.getElementById(id);

  // ── chrome plumbing (promise wrappers) ──────────────────────────────────────

  function queryActiveTab() {
    return new Promise((resolve) => {
      chrome.tabs.query({ active: true, currentWindow: true }, (tabs) =>
        resolve(tabs && tabs[0] ? tabs[0] : null)
      );
    });
  }

  // Look up one tab by id; resolves null (never rejects) for a gone tab, so
  // callers can treat "tab vanished" the same as "nothing to compare against".
  function getTab(tabId) {
    return new Promise((resolve) =>
      chrome.tabs.get(tabId, (t) => resolve(chrome.runtime.lastError ? null : t))
    );
  }

  function sendToTab(tabId, msg) {
    return new Promise((resolve, reject) => {
      chrome.tabs.sendMessage(tabId, msg, (resp) => {
        if (chrome.runtime.lastError) {
          reject(new Error(chrome.runtime.lastError.message));
        } else {
          resolve(resp);
        }
      });
    });
  }

  // ── automatic saving: the box and its fetch log ─────────────────────────────
  // She sees, before Capture, whether this page's file will be fetched on its
  // own; and after, one line per fetch ("…" while trying, a tick with its size,
  // a cross with the site's own reason), so a miss is plain at a glance rather
  // than buried in a sentence. Cleared at each Capture, on a new record and on
  // a Re-read. Every line is built with textContent: the reasons come from the
  // page, never as markup.

  const AUTO_LOG_MAX = 8;

  function clearAutoLog() {
    state.autoOutcome = null;
    state.autoSaved = null;
    state.autoHidden = { ok: 0, failed: 0 };
    state.autoMore = null;
    const ul = $('auto-log');
    if (ul) {
      ul.textContent = '';
      ul.hidden = true;
    }
    renderAutoBox();
  }

  // What this page's automatic path will fetch, in plain words; null when the
  // page has none (then the file must be dropped in, or its address given).
  function autoPathLabel() {
    if (state.ancestryViewer) return 'the full record image (Ancestry)';
    if (state.ancRecord) return 'the record’s index and citation, and its scan if it has one (Ancestry)';
    if (state.ancPhoto) return 'the photo, as Ancestry’s viewer shows it, and its details (Ancestry)';
    if (state.fsRecord) {
      const fc = state.fsCheck;
      return fc && fc.available && fc.imageId
        ? 'the record’s data and citation, and its image ' + fc.imageId + ' (FamilySearch)'
        : 'the record’s data and citation, and its image if FamilySearch hands it over (FamilySearch)';
    }
    if (state.fsImage) {
      const id = (state.prefill && state.prefill.familysearchImageId) || (state.fsCheck && state.fsCheck.imageId);
      return 'the full image' + (id ? ' ' + id : '') + ' (FamilySearch)';
    }
    if (state.fagRecord) return 'the memorial’s facts and citation, and its photos (Find a Grave)';
    if (state.iiif) return 'the full image (IIIF archive)';
    if (npActive()) return 'the whole page and your clippings (Newspapers.com)';
    return null;
  }

  // Whether the "File address on the page" box is showing (it is hidden where
  // the file is fetched automatically), so the box's advice names only what
  // she can see.
  function addressBoxShown() {
    const row = $('asset-url-row');
    return !row || row.style.display !== 'none';
  }

  // The box at the top of "Yes, save the actual file".
  function renderAutoBox() {
    const box = $('auto-box');
    if (!box) return;
    const label = autoPathLabel();
    const out = state.autoOutcome;
    const fc = state.fsCheck;
    const drop = addressBoxShown() ? 'Drop the file in, or paste its address.' : 'Drop the file in below.';
    let cls;
    let ico;
    let text;
    if (state.rereading) {
      [cls, ico, text] = ['none', '…', 'Reading the page again…'];
    } else if (state.droppedAsset) {
      [cls, ico, text] = ['ready', '●', 'Using the file you dropped in.'];
    } else if (out && out.failed && !out.ok) {
      [cls, ico, text] = ['failed', '✗', 'Automatic saving did not work - the lines below say why. ' + drop];
    } else if (out && out.ok && state.autoSaved !== true) {
      // Fetched is not saved: "saved" waits for the bundle to be staged, and
      // a capture that then failed says so (PR #223 audit F6/F11).
      [cls, ico, text] = state.autoSaved === false
        ? ['failed', '✗', 'The file was fetched, but nothing was saved - the message under Capture says why. ' +
          'Press Capture again.']
        : ['none', '…', 'Fetched - saving the capture…'];
    } else if (out && out.ok) {
      // A miss and then a save is usually a retry that worked; the lines say which.
      [cls, ico, text] = ['done', '✓', out.failed
        ? 'Saved automatically, but not every try worked - see below.'
        : 'Saved automatically.'];
    } else if (evidenceUrl()) {
      [cls, ico, text] = ['ready', '●', 'Using the file at the address below.'];
    } else if (state.npPage && state.npLoading) {
      [cls, ico, text] = ['none', '…', 'Reading your clippings on this page…'];
    } else if ((state.fsRecord || state.fsImage) && fc && fc.ok && !fc.available) {
      // FamilySearch said no before Capture: say what WILL be saved.
      if (state.fsRecord && !fc.imageId) {
        [cls, ico, text] = ['ready', '⚡', 'Saved automatically when you press Capture: the record’s data and ' +
          'citation (FamilySearch). It has no image: ' + fc.reason + '.'];
      } else if (state.fsRecord) {
        [cls, ico, text] = ['partial', '⚠', 'Saved automatically: the record’s data only (FamilySearch). Its image ' +
          fc.imageId + ' can’t be saved from here - ' + fc.reason + '. Drop the image in if you have it.'];
      } else {
        [cls, ico, text] = ['failed', '✗', 'This image can’t be saved from here - ' + fc.reason + '. ' +
          'Drop the file in if you have it.'];
      }
    } else if (label) {
      [cls, ico, text] = ['ready', '⚡', 'Saved automatically when you press Capture: ' + label + '.' +
        (fc && fc.checking ? ' (Checking the image…)' : '')];
    } else if (state.npPage) {
      [cls, ico, text] = ['none', '○', 'Nothing on this page is saved automatically - the Newspapers.com note ' +
        'below says why. ' + drop];
    } else {
      [cls, ico, text] = ['none', '○', 'No automatic file on this page. ' + drop];
    }
    box.className = 'auto-box ' + cls;
    $('auto-ico').textContent = ico;
    $('auto-text').textContent = text;
  }

  // One line in the log; returns done(ok, detail) to finish it.
  function autoRow(label) {
    const ul = $('auto-log');
    if (!ul) return () => {};
    const li = document.createElement('li');
    li.className = 'try';
    const ico = document.createElement('span');
    ico.className = 'ico';
    ico.textContent = '…';
    const text = document.createElement('span');
    text.textContent = label;
    const why = document.createElement('span');
    why.className = 'why';
    li.appendChild(ico);
    li.appendChild(text);
    li.appendChild(why);
    ul.appendChild(li);
    trimAutoLog(ul);
    ul.hidden = false;
    return (ok, detail) => {
      li.className = ok ? 'ok' : 'fail';
      ico.textContent = ok ? '✓' : '✗';
      why.textContent = detail ? (ok ? '(' + detail + ')' : '- ' + detail) : '';
      const o = state.autoOutcome || (state.autoOutcome = { ok: 0, failed: 0 });
      if (ok) o.ok++;
      else o.failed++;
      renderAutoBox();
    };
  }

  // Keep the log short without losing what went wrong (PR #223 audit F3/F14):
  // past AUTO_LOG_MAX lines the oldest line that worked goes first, a failed
  // one only when nothing else is left, and a line at the top says how many
  // are not shown and whether any of them failed.
  function trimAutoLog(ul) {
    let rows = Array.from(ul.children).filter((li) => li !== state.autoMore);
    if (rows.length <= AUTO_LOG_MAX) return;
    while (rows.length > AUTO_LOG_MAX) {
      const older = rows.slice(0, -1);
      const drop = older.find((li) => li.className === 'ok') || older[0];
      if (drop.className === 'fail') state.autoHidden.failed++;
      else state.autoHidden.ok++;
      ul.removeChild(drop);
      rows = rows.filter((li) => li !== drop);
    }
    const n = state.autoHidden.ok + state.autoHidden.failed;
    if (!state.autoMore) {
      state.autoMore = document.createElement('li');
      state.autoMore.className = 'more';
      ul.insertBefore(state.autoMore, ul.children[0] || null);
    }
    state.autoMore.textContent = n + ' earlier ' + (n === 1 ? 'line' : 'lines') + ' not shown' +
      (state.autoHidden.failed
        ? ' (' + state.autoHidden.failed + ' of them did not work)'
        : ' - ' + (n === 1 ? 'it' : 'all of them') + ' worked');
  }

  // A file that is byte for byte the one the last saved Capture fetched. Often
  // that is right (a second capture of one record), but on a new record it is
  // the tell of a tab still showing the previous picture, so it is said
  // rather than hidden. The fingerprint is kept for this Capture either way.
  async function sameAsLastCapture(base64) {
    const subtle = typeof crypto !== 'undefined' && crypto && crypto.subtle;
    if (!base64 || !subtle) return false;
    let sig;
    try {
      const digest = await subtle.digest('SHA-256', new TextEncoder().encode(base64));
      sig = Array.from(new Uint8Array(digest)).map((b) => b.toString(16).padStart(2, '0')).join('');
    } catch (e) {
      return false;
    }
    state.thisCaptureSigs.add(sig);
    return state.lastCaptureSigs.has(sig);
  }

  // sendToTab for an automatic fetch, logged. A page that does not answer
  // becomes { ok: false, error } like any other miss, so every caller handles
  // one shape. A record capture passes `rec` ({ recordUrl, ids }): its
  // question goes through askTab, and a tab that has moved to another record
  // still stops the capture (TabMoved), the line saying so.
  // A "slow down" (429/503) a site gave anywhere in this capture: in an
  // answer's own flag, its words, or a photo list's status. Remembered for
  // the whole capture, so that one which then fails still tells the owner's
  // batch downloads to take their break (PR #224 Codex 4503849177).
  // `site` is the batch site whose host gave the answer ('ancestry',
  // 'familysearch', 'findagrave', 'newspapers'), or null for any other host:
  // only that site's batch is held back by it, and another host's "slow
  // down" counts for nothing - not for the capture's outcome either, or a
  // running batch would stop on it (PR #224 second audit N4).
  function noteSlowDown(resp, site) {
    if (!resp || !site) return;
    const st = (x) => x === 429 || x === 503;
    if (resp.slowDown || (resp.rateHits > 0) || st(resp.partialStatus) || (!resp.ok && st(resp.status)) ||
        (!resp.ok && /slow down|HTTP 429|HTTP 503|too many requests/i.test(String(resp.error || '')))) {
      state.slowDownSeen = true;
      if (site) state.slowDownSites.add(site);
    }
  }

  // The batch site an address is on, or null for any other host.
  function siteOfHost(href) {
    let h = '';
    try {
      h = new URL(href).hostname;
    } catch (e) {
      return null;
    }
    if (/(^|\.)ancestry\.[a-z.]+$/i.test(h) || /(^|\.)ancestrycdn\.com$/i.test(h)) return 'ancestry';
    if (/(^|\.)familysearch\.org$/i.test(h)) return 'familysearch';
    if (/(^|\.)findagrave\.com$/i.test(h)) return 'findagrave';
    if (/(^|\.)newspapers\.com$/i.test(h)) return 'newspapers';
    return null;
  }

  // The batch site whose host a question to the page reaches: its own
  // site's for the site readers, the address's host for a typed address.
  function siteOfAsk(msg) {
    const a = String((msg && msg.action) || '');
    if (a === 'fetchAsset') return siteOfHost(String(msg.url || ''));
    if (/^ancestry/.test(a)) return 'ancestry';
    if (/^familysearch/.test(a)) return 'familysearch';
    if (/^findagrave/.test(a)) return 'findagrave';
    if (/^newspapers/.test(a)) return 'newspapers';
    return null;
  }

  async function autoFetch(label, msg, rec) {
    const done = autoRow(label);
    let resp;
    try {
      resp = rec ? await askTab(msg, rec.recordUrl, rec.ids) : await sendToTab(state.tabId, msg);
    } catch (e) {
      if (e instanceof TabMoved) {
        done(false, 'the page in the tab changed');
        throw e;
      }
      resp = { ok: false, error: String((e && e.message) || e) };
    }
    if (!resp) resp = { ok: false, error: 'no answer from the page - reload it and try again' };
    noteSlowDown(resp, siteOfAsk(msg));
    if (resp.ok) {
      const kb = resp.base64 ? Math.max(1, Math.round((resp.base64.length * 3) / 4 / 1024)) : 0;
      const same = await sameAsLastCapture(resp.base64);
      done(true, [kb ? kb + ' KB' : '',
        same ? 'the same picture as your last capture: check the tab shows the record you mean' : '']
        .filter(Boolean).join(' - '));
    } else {
      done(false, resp.error || 'it did not come through');
    }
    return resp;
  }

  async function injectContent(tabId) {
    // activeTab + scripting: the content script is injected only here, on the
    // human's invocation - never ambient (§5.4). content.js self-guards against a
    // second injection, so calling this again on the same page is harmless.
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ['src/content.js'],
    });
  }

  // ── Step 1: confirm ──────────────────────────────────────────────────────────

  function populateTypeSelect() {
    const sel = $('f-type');
    for (const [value, label] of SOURCE_TYPES) {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = label;
      sel.appendChild(opt);
    }
  }

  function renderPeople(names) {
    const list = $('people-list');
    list.innerHTML = '';
    (names || []).forEach((name) => addPersonRow(name, true));
  }

  function addPersonRow(name, checked) {
    const list = $('people-list');
    const label = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = checked !== false;
    cb.dataset.name = name;
    // Ticking or unticking someone is her choice for this record (touched).
    cb.addEventListener('change', () => state.touched.add('people'));
    const span = document.createElement('span');
    span.textContent = name;
    label.appendChild(cb);
    label.appendChild(span);
    list.appendChild(label);
  }

  function checkedPeople() {
    return Array.from($('people-list').querySelectorAll('input:checked')).map(
      (cb) => cb.dataset.name
    );
  }

  // `retry`: a second read of the address whose last read failed, after she
  // may have started filling the empty form by hand. Then only what she has
  // not touched since (state.touched) is filled from the page; everything
  // she typed, added, ticked or dropped stays exactly as it is, read from
  // the form now rather than from a copy (PR #223 delta D1-D3).
  function applyPrefill(prefill, retry) {
    const keep = retry ? state.touched : new Set();
    const fill = (id, value) => {
      if (!keep.has(id)) $(id).value = value;
    };
    // A prefill for a DIFFERENT record invalidates the previous record's
    // evidence picks: without this, a file dropped for record A (and its
    // screen-capture tick) would survive the navigation and stage as record
    // B's evidence. Compare before prefilledUrl is overwritten below.
    // Fragment-only moves never reach here (refreshOnNavigation skips them),
    // so this fires only on a genuine new record - and on the first prefill,
    // where it is a harmless no-op on the fresh form.
    if (!sameRecordUrl(prefill.url, state.prefilledUrl)) {
      clearEvidenceSelection();
      clearAutoLog();
    }
    // Her corrections on a retry stay hers (later data never overwrites
    // them); a new record starts with none.
    if (!retry) state.userEdited = new Set();
    for (const id of ['f-title', 'f-date', 'f-repo']) $(id).readOnly = false;
    state.prefill = prefill;
    state.prefilledUrl = prefill.url || null;
    state.prefillFailed = false;
    fill('f-title', prefill.title || '');
    fill('f-date', prefill.sourceDate || '');
    fill('f-repo', prefill.repository || '');
    // The address the form was READ for is prefilledUrl (above), and that is
    // what Capture checks the tab against (checkFormIsForTab). The Page
    // address box is an ordinary field: one she corrected stays on a retry
    // and is the address the capture records (Codex 4503614707).
    fill('f-url', prefill.url || '');
    // Reset the per-record fields the prefill doesn't otherwise set, so a batch
    // re-prefill (on navigation) never carries the previous record's notes or
    // chosen kind onto the next page. (Auto-detect = blank kind.)
    if (!keep.has('f-notes')) {
      $('f-notes').value = '';
      state.fsNoteLine = null;
    }
    fill('f-type', prefill.sourceType || '');
    if (!keep.has('people')) renderPeople(prefill.people);
    // On a shared photo: what of its Details the form now shows - the names
    // listed, the Details it was filled from - for Capture to compare with
    // the page as it is then (captureAncestryPhoto).
    state.photoShown = prefill.ancestryPhoto
      // `keys` null: the page did not say which Details it read (an older
      // page script): then only the names are compared.
      ? { keys: Array.isArray(prefill.ancestryPhotoShown) ? prefill.ancestryPhotoShown.slice() : null,
        people: (prefill.people || []).map((n) => String(n).toLowerCase()) }
      : null;
    // Her clipping ticks, too, when she changed them since (and a dropped
    // file is never let go on the same record: clearEvidenceSelection above).
    const keepNp = keep.has('npTicks') && state.np
      ? { pageId: state.np.pageId, selected: new Set(state.npSelected) } : null;
    applyFilePrefill(prefill, keepNp, keep.has('f-asset-url'));
    // From here, "touched" counts from this read.
    state.touched = new Set();

    const banner = $('recipe-banner');
    const hintLabel = prefill.ancestryPhoto ? 'a photo shared on Ancestry' : RECIPE_LABELS[prefill.recipeHint];
    if (prefill.warning) {
      // The capture-timing guard (§08-A): the record detail looks empty/unloaded,
      // so a capture now would stage a page.html the recipe can't extract from.
      // Surface it prominently - this matters more than the recipe hint.
      banner.textContent = prefill.warning;
      banner.classList.remove('recipe');
      banner.classList.add('warn');
    } else if (hintLabel) {
      banner.textContent = 'Looks like ' + hintLabel + '. Filing will confirm it.';
      banner.classList.remove('warn');
      banner.classList.add('recipe');
    } else {
      banner.textContent = 'Generic capture, filing will read the page itself.';
      banner.classList.remove('warn', 'recipe');
    }
    updateAssetStatus();
  }

  // The prefill's "Save the record" half: which automatic path applies, and
  // the best-guess file address. A new record (applyPrefill) sets it with the
  // rest; the "Re-read file" button sets it alone, so the title, date, people
  // and notes she has stay as they are.
  const FILE_PREFILL_KEYS = ['imageUrl', 'pdfUrl', 'ancestryImageViewer', 'ancestryRecord', 'ancestryPhoto',
    'ancestryMediaId', 'ancestryPhotoShown',
    'familysearchRecord', 'familysearchImageViewer', 'familysearchImageId', 'findagraveMemorial',
    'newspapers', 'iiif'];

  // Pages whose record file is fetched without her: there the "File address
  // on the page" box has no job, and is hidden (the drop zone stays).
  function autoFilePage(prefill) {
    return !!(prefill.ancestryImageViewer || prefill.ancestryRecord || prefill.ancestryPhoto || prefill.familysearchRecord ||
      prefill.familysearchImageViewer || prefill.findagraveMemorial || prefill.iiif || prefill.newspapers);
  }

  // `keepNp`: { pageId, selected } - Re-read file on the same Newspapers.com
  // page keeps the clippings she ticked (PR #223 audit F2), rather than the
  // page's defaults; a new record never passes it.
  // `keepAddress`: the file address box holds one she typed (a retry), so
  // the page's guess does not replace it.
  function applyFilePrefill(prefill, keepNp, keepAddress) {
    state.autoMissed = false;
    state.iiif = !!prefill.iiif;
    state.ancRecord = !!prefill.ancestryRecord;
    state.ancPhoto = !!prefill.ancestryPhoto;
    // On a shared photo the button says what it saves, and the page copy and
    // "the page copy is the record" are not offered (pageCopyOn): one plain
    // line says why.
    $('btn-capture').textContent = state.ancPhoto ? 'Capture this photo' : 'Capture & save';
    $('page-copy-card').style.display = state.ancPhoto ? 'none' : '';
    $('opt-none').style.display = state.ancPhoto ? 'none' : '';
    $('pagecopy-note').style.display = state.ancPhoto ? 'block' : 'none';
    state.fsRecord = !!prefill.familysearchRecord;
    state.fsImage = !!prefill.familysearchImageViewer;
    state.fagRecord = !!prefill.findagraveMemorial;
    // Pre-fill the evidence url box with the best-guess image/PDF the page
    // exposed - EXCEPT where the visible image is a downsized derivative and an
    // auto path can fetch the real file: an Ancestry image-viewer page (the EX7
    // 507x600 thumbnail trap) or an open-archive IIIF page (the rendered <img>
    // is a derivative). There the URL box is left empty and the auto path
    // supplies the full record on Capture. Any other page clears a stale URL so
    // a previous record's address can't be re-fetched as this one's evidence.
    const auto = autoFilePage(prefill);
    if (keepAddress) {
      /* hers: left as typed (updateAddressRow keeps it in view) */
    } else if (auto) {
      $('f-asset-url').value = '';
    } else if (prefill.imageUrl) {
      $('f-asset-url').value = prefill.imageUrl;
    } else if (prefill.pdfUrl) {
      $('f-asset-url').value = prefill.pdfUrl;
    } else {
      $('f-asset-url').value = '';
    }
    applyAncestryNote(!!prefill.ancestryImageViewer);
    state.preflightSlowDown = false;
    state.npReading = loadNewspapers(prefill, keepNp);
    state.fsChecking = checkFamilySearch();
    updateAddressRow();
    updateAssetStatus();
  }

  // The "File address on the page" box is hidden where the file comes by
  // itself, with two exceptions so she can still name the right file (PR
  // #223 audit F9): on Newspapers.com only while there are clippings of hers
  // to save automatically, and on a viewer that takes a typed address over
  // its own guess (an Ancestry or FamilySearch image viewer, an IIIF page)
  // once that guess has failed, or while an address is in the box. On a
  // record page (Ancestry, FamilySearch, Find a Grave) a typed address would
  // file the page without the record's own data, so the drop zone - whose
  // file is saved WITH that data - stays the way to add a missing image.
  function updateAddressRow() {
    const p = state.prefill || {};
    let hide = autoFilePage(p);
    if (hide && state.npPage && !state.npLoading && !npActive()) hide = false;
    if (hide && (state.ancestryViewer || state.fsImage || state.iiif) && state.autoMissed) hide = false;
    // An address in the box is never hidden: she sees what Capture would use
    // (and can clear it). Newspapers.com's own label is not an address.
    if (hide && ($('f-asset-url').value || '').trim() && !$('f-asset-url').readOnly) hide = false;
    $('asset-url-row').style.display = hide ? 'none' : '';
  }

  // Forget every file setting the page left behind - the automatic path, the
  // best-guess address, a dropped file - and the answers still on their way
  // for them (the newspaper clippings, the FamilySearch image check). A
  // Re-read starts here, so a page that then cannot be read leaves the panel
  // holding nothing of the previous page's file: Capture saves no file rather
  // than the old one.
  function forgetFileState() {
    clearEvidenceSelection();
    if (state.prefill) {
      const off = {};
      for (const k of FILE_PREFILL_KEYS) off[k] = null;
      state.prefill = Object.assign({}, state.prefill, off);
    }
    applyFilePrefill(state.prefill || {});
  }

  // The Notes box carries the reason a FamilySearch image cannot be saved as
  // soon as the panel knows it, so she sees it and can change it before
  // Capture; it then travels into the source's notes. Only the panel's own
  // line is ever added or taken away - never what she typed.
  function restrictedImageNote(check) {
    if (!check || !check.ok) return; // no answer is no news: leave the box as it is
    const want = check.imageId && !check.available
      ? 'Image not saved: FamilySearch image ' + check.imageId + ' (https://www.familysearch.org/ark:/61903/3:1:' +
        check.imageId + ') could not be downloaded - ' + check.reason + '. Checked ' + captureJson.accessedDate() + '.'
      : null;
    const box = $('f-notes');
    if (state.fsNoteLine && state.fsNoteLine !== want) box.value = withoutLine(box.value, state.fsNoteLine);
    state.fsNoteLine = null;
    if (!want) return;
    // Already said (by an earlier check of this image, or by her): not twice.
    if (box.value.indexOf(fsNoteMark(check.imageId)) === -1) {
      box.value = box.value.trim() ? box.value.replace(/\s+$/, '') + '\n' + want : want;
    }
    if (box.value.indexOf(want) !== -1) state.fsNoteLine = want;
  }

  // How every "this image was not saved" note begins, for one image.
  function fsNoteMark(imageId) {
    return 'Image not saved: FamilySearch image ' + imageId + ' ';
  }

  // `text` without the one line `line` (when it is there, word for word).
  function withoutLine(text, line) {
    if (!line || !text || text.indexOf(line) === -1) return text;
    return text.split('\n').filter((l) => l !== line).join('\n').replace(/\n{3,}/g, '\n\n').trim();
  }

  // Show/hide the Ancestry auto-fetch affordance. On an image-viewer page the
  // "Yes, save the actual file" choice gets the full-res record automatically on
  // Capture (the seamless path); the note explains it pulls the SAME image
  // Ancestry's own Download button would, in your session, one at a time. Off
  // any other page this is a no-op and every existing behavior is untouched.
  function applyAncestryNote(on) {
    state.ancestryViewer = on;
    const note = $('ancestry-note');
    if (note) note.style.display = on ? 'block' : 'none';
    const urlInput = $('f-asset-url');
    if (urlInput) {
      // The URL box still works as a manual override/fallback, but on the viewer
      // its placeholder makes clear it is optional - the auto path is primary.
      urlInput.placeholder = on
        ? 'leave blank to auto-save the full record (or paste an address)'
        : 'image or PDF address';
    }
    updateAssetStatus();
  }

  // ── Newspapers.com: whole page + your clippings ──────────────────────────────
  // Works from a clipping page (/article/…) or the page viewer (/image/…). The
  // content script reads the page's own data; this side shows which of YOUR
  // clippings will be saved, and on Capture rebuilds the page at full
  // resolution and stages one bundle per clipping (each its own source).

  const NP_URL_LABEL = 'Newspapers.com page + clipping (saved automatically)';

  // True when the page in the tab has clippings Capture can save automatically.
  function npActive() {
    return !!(state.np && state.np.ownClips && state.np.ownClips.length);
  }

  // Why Capture cannot save this Newspapers.com page automatically, in words
  // that end in what to do instead. Shared by the panel note and Capture.
  // The cause alone, for a batch to sort (PR #224 X1): the reason above
  // wraps it in advice that names the panel ("open the panel again"), and a
  // batch must judge the cause, never the advice. null when the reason is
  // its own cause.
  function npUnavailableCause() {
    const np = state.np;
    if (state.npError) return state.npError;
    if (np && np.listError && !np.ownClips.length) return np.listError;
    return null;
  }

  function npUnavailableReason() {
    const np = state.np;
    if (state.npError) {
      return 'automatic saving is not available here: ' + state.npError +
        '. Reload the page and open the panel again, or drop in a file.';
    }
    if (np && np.listTruncated && !np.ownClips.length) {
      return 'automatic saving is not available here: ' + np.listError +
        ', so yours may be among the ones it did not check. Open your clipping on ' +
        'Newspapers.com (its own page) and capture it there, or drop in a file.';
    }
    if (np && np.listError && !np.ownClips.length) {
      return 'automatic saving is not available here: ' + np.listError +
        ', so the panel cannot tell which clippings are yours. Reload the page and open ' +
        'the panel again; if it keeps happening, wait a few minutes, or drop in a file.';
    }
    if (np && (np.openedUnusable || (np.unusable && np.unusable.length))) {
      return (np.openedUnusable ? 'this clipping marks' : 'your clippings on this page mark') +
        ' no area on the newspaper page that can be cut out, so nothing is saved ' +
        'automatically. Clip the item again on Newspapers.com, or use the site’s ' +
        'own download and drop the file in.';
    }
    return 'none of the clippings on this page are yours, so nothing is saved automatically. ' +
      'Clip the item on Newspapers.com first, then open the panel again, or drop in a file.';
  }

  // Titles, dates and citations live in lib/newspapers.js, where they are tested.
  const npPub = (clip, np) => FHA.newspapers.publicationOf(clip, np);
  const npTitle = (clip, np) => FHA.newspapers.title(clip, np);
  const npCaption = (clip, np) => FHA.newspapers.caption(clip, np);
  const npCitations = (clip, np) =>
    FHA.newspapers.citations(clip, np, captureJson.accessedDate());
  // The machine-and-human-readable record of where the clipping sits, so the
  // combined view can be rebuilt later and the crop is never a mystery.
  // `copyNote` explains a page copy left out of this bundle (a sibling clipping).
  function npNotes(clip, np, page, copyNote) {
    const pub = npPub(clip, np);
    const lines = [];
    lines.push(
      'Newspapers.com clipping ' + clip.id + (clip.title ? ' ("' + clip.title + '")' : '') +
        (clip.username ? ', clipped by ' + clip.username : '') +
        (clip.created ? ' on ' + String(clip.created).slice(0, 10) : '') + '.'
    );
    lines.push(
      [pub.name, pub.location, clip.pageDate || np.pageDate, (clip.pageNo || np.pageNo) ? 'page ' + (clip.pageNo || np.pageNo) : null]
        .filter(Boolean).join(', ') +
        '. Newspapers.com page ' + np.pageId + ', ' + np.pageWidth + ' x ' + np.pageHeight + ' pixels.'
    );
    if (clip.tags) lines.push('Tags on Newspapers.com: ' + clip.tags + '.');
    lines.push('Citation (Newspapers.com, Genealogy style): ' + npCitations(clip, np).genealogy);
    lines.push(
      'Clipping area on the page (x, y, width, height in page pixels): ' +
        clip.rects.map((r) => [r.x, r.y, r.w, r.h].join(',')).join('; ')
    );
    lines.push(
      'Files: clipping.jpg is the item at full resolution, cut from page.jpg, the whole page ' +
        'rebuilt at full resolution from Newspapers.com image tiles. annotated.jpg shows the ' +
        'clipping above the page with the item boxed in red; it was made at capture and is ' +
        'not part of the newspaper. clipping-data.json holds the clipping details, all four ' +
        'Newspapers.com citation styles, and the site\'s machine OCR of the item - unverified, ' +
        'for locating words only, never a transcript.'
    );
    if (page && page.reduced) {
      lines.push(page.reduced + ' of ' + page.tiles + ' page tiles came back below full size.');
    }
    if (copyNote) lines.push(copyNote);
    return lines.join('\n');
  }

  function npSetText(text) {
    const el = $('np-text');
    if (el) el.textContent = text;
  }

  async function loadNewspapers(prefill, keepNp) {
    // Forget the previous record's newspaper page NOW, before waiting on the
    // tab: until the new answer is installed, Capture must not pair the old
    // page's image pass and clippings with this record's form and page.html.
    const seq = ++state.npSeq;
    setNewspapersMode(null);
    state.npPage = !!(prefill && prefill.newspapers);
    state.npError = null;
    state.npLoading = state.npPage;
    if (!state.npPage) return;
    $('np-note').style.display = 'block';
    npSetText('Reading your clippings on this page…');
    updateAssetStatus();
    let info;
    try {
      info = await sendToTab(state.tabId, { action: 'newspapersInfo' });
    } catch (e) {
      info = { ok: false, error: e.message };
    }
    if (seq !== state.npSeq) return; // the tab moved on meanwhile; a newer read owns the state
    state.npLoading = false;
    if (info && (info.slowDown || SLOW_DOWN_RE.test(String(info.error || info.listError || '')))) {
      state.preflightSlowDown = true;
      state.preflightSlowDownSite = 'newspapers';
    }
    if (!info || !info.ok) {
      setNewspapersMode(null);
      state.npError = (info && info.error) || 'no answer from the page';
      $('np-note').style.display = 'block';
      npSetText(npUnavailableReason().replace(/^./, (c) => c.toUpperCase()));
      updateAddressRow();
      updateAssetStatus();
      return;
    }
    setNewspapersMode(info, keepNp);
  }

  // The title, date and newspaper boxes step aside (read-only) only while
  // several of her clippings are ticked (fillNpFields). Every change of
  // newspaper mode starts by giving them back - a clipping list that could
  // not be read again, a page with none of hers, a page that is not a
  // newspaper - so she can always name a file she supplies by hand (Codex
  // 4503601729). A new answer with several ticked locks them again.
  const NP_MANY_PLACEHOLDER = 'Each clipping gets its own title';
  function unlockNpFields() {
    for (const id of ['f-title', 'f-date', 'f-repo']) $(id).readOnly = false;
    if ($('f-title').placeholder === NP_MANY_PLACEHOLDER) $('f-title').placeholder = '';
  }

  function setNewspapersMode(info, keepNp) {
    state.np = info && info.ok ? info : null;
    state.npSelected = new Set();
    unlockNpFields();
    const note = $('np-note');
    const urlInput = $('f-asset-url');
    $('np-clips').textContent = '';
    if (!state.np) {
      note.style.display = 'none';
      if (urlInput.readOnly) {
        urlInput.readOnly = false;
        // Only our own label: by now applyPrefill may have put the next
        // record's detected file address here (#211 review).
        if (urlInput.value === NP_URL_LABEL) urlInput.value = '';
      }
      updateAssetStatus();
      return;
    }
    const np = state.np;
    const own = np.ownClips;
    note.style.display = 'block';
    if (!own.length) {
      urlInput.readOnly = false;
      if (urlInput.value === NP_URL_LABEL) urlInput.value = '';
      npSetText(npUnavailableReason().replace(/^./, (c) => c.toUpperCase()));
      updateAssetStatus();
      return;
    }
    // Default: on a clipping page, that clipping; on the page viewer, all of
    // your clippings on the page. The checkboxes below change it. Re-read
    // file on the same page keeps what she ticked, of the clippings still
    // listed (keepNp).
    const focus = own.find((c) => c.id === np.focusClipId);
    const kept = keepNp && keepNp.pageId === np.pageId
      ? own.filter((c) => keepNp.selected.has(c.id)).map((c) => c.id) : [];
    if (kept.length) kept.forEach((id) => state.npSelected.add(id));
    else if (focus && np.kind === 'article') state.npSelected.add(focus.id);
    // The clipping she opened cannot be cut out: say so (np-text below), and
    // let her tick another one herself rather than saving it unasked (#211
    // audit).
    else if (np.kind === 'article' && np.openedUnusable) { /* nothing ticked */ }
    else own.forEach((c) => state.npSelected.add(c.id));

    urlInput.value = NP_URL_LABEL;
    urlInput.readOnly = true;
    npSetText(
      (np.focusOwner
        ? 'This clipping was made by ' + np.focusOwner + '. It is saved because you opened it. '
        : '') +
        'On Capture this rebuilds the whole page at full resolution from Newspapers.com’s own ' +
        'image tiles, one at a time (about 20 seconds a page), and cuts out your ' +
        'clipping. Each clipping becomes its own source with the clipping, the whole page, ' +
        'and a combined view with the item boxed.' +
        (np.otherClipCount
          ? ' Other clippings on this page that are not yours (' + np.otherClipCount + ') are skipped.'
          : '') +
        (np.unusable && np.unusable.length
          ? ' Left out because they mark no area on the page that can be cut out: ' +
            np.unusable.map((c) => '“' + (c.title || 'untitled clipping') + '”').join(', ') + '.'
          : '') +
        // Part of the list could not be read: the clippings shown are saved,
        // but others of hers may be missing, and she must be told (#211 review).
        (np.listError
          ? ' Note: ' + np.listError + ', so some of your clippings on this page may not be ' +
            'listed here. To get one that is missing, open that clipping on Newspapers.com ' +
            '(its own page) and capture it there.'
          : '')
    );
    renderNpClips();
    fillNpFields();
    updateAssetStatus();
  }

  function renderNpClips() {
    const box = $('np-clips');
    box.textContent = '';
    const np = state.np;
    if (!np || (np.ownClips.length < 2 && !np.openedUnusable)) return;
    const head = document.createElement('span');
    head.className = 'mini';
    head.textContent = 'Clippings on this page to save (each becomes its own source):';
    box.appendChild(head);
    for (const c of np.ownClips) {
      const label = document.createElement('label');
      label.className = 'check';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = state.npSelected.has(c.id);
      cb.addEventListener('change', () => {
        state.touched.add('npTicks');
        if (cb.checked) state.npSelected.add(c.id);
        else state.npSelected.delete(c.id);
        // A correction typed for one clipping belongs to that clipping: once
        // the ticks change, the form describes a different one (#211 audit).
        state.userEdited = new Set();
        fillNpFields();
        updateAssetStatus();
      });
      const text = document.createElement('span');
      text.textContent = (c.title || 'Untitled clipping') + (c.tags ? ' (' + c.tags + ')' : '');
      label.appendChild(cb);
      label.appendChild(text);
      box.appendChild(label);
    }
  }

  // One clipping chosen: the form describes it and stays editable. Several: each
  // bundle gets its own title and date, so the form's title box steps aside.
  function fillNpFields() {
    const np = state.np;
    if (!np) return;
    const chosen = np.ownClips.filter((c) => state.npSelected.has(c.id));
    // A field the human typed in keeps what they typed: the clippings can
    // arrive seconds after the panel opens, and a correction made meanwhile
    // must not be silently replaced by the site's own value (#211 review).
    const put = (id, value) => {
      if (!state.userEdited.has(id)) $(id).value = value;
    };
    put('f-type', 'newspaper');
    if (chosen.length === 1) {
      const c = chosen[0];
      put('f-title', npTitle(c, np));
      $('f-title').placeholder = '';
      put('f-date', c.pageDate || np.pageDate || '');
      put('f-repo', npPub(c, np).name || 'Newspapers.com');
      if (c.url) put('f-url', c.url);
    } else if (chosen.length > 1) {
      // Each clipping is saved with its own title, date and newspaper from
      // the site, so the form shows those and nothing a person could type
      // and see ignored (#211 audit).
      for (const id of ['f-title', 'f-date', 'f-repo']) state.userEdited.delete(id);
      $('f-title').value = '';
      $('f-title').placeholder = NP_MANY_PLACEHOLDER;
      $('f-date').value = np.pageDate || '';
      $('f-repo').value = (np.publication && np.publication.name) || 'Newspapers.com';
    }
    const many = chosen.length > 1;
    for (const id of ['f-title', 'f-date', 'f-repo']) $(id).readOnly = many;
  }

  function npClipData(clip, np, ocr) {
    const pub = npPub(clip, np);
    return {
      note: 'Captured from Newspapers.com by the archive capture extension. site_ocr is the ' +
        'site\'s machine reading of the clipping: unverified, for locating words only, never a transcript.',
      clipping: {
        id: clip.id,
        title: clip.title || null,
        tags: clip.tags || null,
        clipped_by: clip.username || null,
        created: clip.created || null,
        url: clip.url || null,
        rectangles: clip.rects.map((r) => ({ x: r.x, y: r.y, width: r.w, height: r.h, rotation: r.rotation })),
      },
      page: {
        id: np.pageId,
        width: np.pageWidth,
        height: np.pageHeight,
        date: clip.pageDate || np.pageDate || null,
        page_number: clip.pageNo || np.pageNo || null,
        publication: pub.name || null,
        location: pub.location || null,
      },
      citations: npCitations(clip, np),
      site_ocr: ocr
        ? { text: ocr.text, complete: !!ocr.complete }
        : null,
    };
  }

  // The site's OCR for each chosen clipping: already read for the clipping whose
  // page is open; otherwise one paced request to that clipping's own page.
  async function npGatherText(chosen) {
    const out = new Map();
    let fetched = 0;
    for (const clip of chosen) {
      if (clip.ocrText) {
        out.set(clip.id, clip.ocrText);
        continue;
      }
      if (!clip.url) continue;
      if (fetched++) await new Promise((r) => setTimeout(r, 1500 + Math.random() * 2000));
      setStageResult('Reading the site’s text for “' + (clip.title || 'clipping') + '”…', '');
      try {
        const resp = await sendToTab(state.tabId, {
          action: 'newspapersClipText', url: clip.url, clippingId: clip.id,
        });
        // Optional - but a "slow down" here still holds the batch back
        // (PR #224 Codex 4504665602).
        noteSlowDown(resp, 'newspapers');
        if (resp && resp.ok && resp.ocr) out.set(clip.id, resp.ocr);
      } catch (e) {
        /* the text is a convenience; the images are the record */
      }
    }
    return out;
  }

  // Stage one bundle per chosen clipping. The page is fetched once, before any
  // bundle, so a failure there saves nothing and says so (the caller reports
  // the throw). After that each clipping succeeds or fails on its own: the
  // report names where every bundle went and which, if any, were not saved,
  // and only the unsaved ones stay ticked for another try.
  // `opts` (a batch download only): `expectKey` is the one clipping it
  // queued ("clip:{id}") - anything else ticked saves nothing - and
  // `forceDownload` stages to the download folder (stageBundle). Returns
  // { saved, results, rateHits }: `rateHits` is how many "slow down"
  // answers the page download waited out.
  async function captureNewspapers(pageHtml, wantPageCopy, recordUrl, opts = {}) {
    const np = state.np;
    // The form as it is NOW. The page download below takes about 20 seconds,
    // and nothing that happens to the form meanwhile may reach these bundles
    // (#211 audit).
    const form = {
      title: $('f-title').value.trim(),
      date: $('f-date').value.trim(),
      repo: $('f-repo').value.trim(),
      url: $('f-url').value.trim(),
      type: $('f-type').value,
      people: checkedPeople(),
      notes: gatherNotes(false),
    };
    const chosen = np.ownClips.filter((c) => state.npSelected.has(c.id));
    if (!chosen.length) throw new Error('tick at least one of your clippings');
    if (opts.expectKey && (chosen.length !== 1 || 'clip:' + chosen[0].id !== opts.expectKey ||
        np.focusClipId !== chosen[0].id)) {
      throw new Error('the clipping shown is not the one queued (' + opts.expectKey.replace(/^clip:/, '') +
        '), so nothing was saved');
    }
    // The tab's page copy is a copy of the record open in the tab; build it
    // only if some chosen clipping is that record (lib pageCopyFits).
    const copyFits = (clip) => FHA.newspapers.pageCopyFits(np, clip);
    const pageCopy = wantPageCopy && chosen.some(copyFits) ? await buildPageCopy() : null;
    // The last read of the tab: the page's tiles below are fetched by the
    // page's own id, and each clipping's text by its own address.
    await checkTabStillOn(recordUrl);
    const opened = np.ownClips.find((c) => c.id === np.focusClipId);
    const texts = await npGatherText(chosen);

    const pageRow = autoRow('Newspapers.com whole page');
    let page;
    try {
      page = await FHA.newspapers.fullPage(np, (i, n, wait) =>
        setStageResult(
          wait
            ? 'Newspapers.com asked us to slow down. Waiting ' + wait + ' seconds before tile ' + i + ' of ' + n + '…'
            : 'Rebuilding the full page from Newspapers.com, tile ' + i + ' of ' + n + ' (paced gently)…',
          ''
        )
      );
    } catch (e) {
      pageRow(false, (e && e.message) || String(e));
      noteSlowDown(e, 'newspapers');
      throw e;
    }
    noteSlowDown(page, 'newspapers');
    pageRow(true, page.tiles + (page.tiles === 1 ? ' piece' : ' pieces') +
      (page.reduced ? ', ' + page.reduced + ' below full size' : ''));
    setStageResult('Building the clipping and combined images…', '');
    const pageBlob = await FHA.newspapers.toJpeg(page.canvas);

    const results = [];
    const single = chosen.length === 1;
    for (const clip of chosen) {
      const title = single ? (form.title || npTitle(clip, np)) : npTitle(clip, np);
      try {
        const st = await stageNpClip(clip, np, page, pageBlob, {
          title, single, pageHtml, texts, form, forceDownload: !!opts.forceDownload,
          pageCopy: pageCopy && copyFits(clip) ? pageCopy : null,
          copyNote: wantPageCopy && !copyFits(clip)
            ? 'No copy of the web page was kept for this clipping: the page open in the browser ' +
              'was another clipping' + (opened && opened.title ? ' ("' + opened.title + '")' : '') +
              '. This clipping\'s own page is ' + (clip.url || 'on Newspapers.com') + '.'
            : null,
        });
        results.push({ id: clip.id, title, ok: true, ...st });
      } catch (e) {
        results.push({ id: clip.id, title, ok: false, error: e.message || String(e) });
      }
      if (!single) {
        setStageResult('Saved ' + results.filter((r) => r.ok).length + ' of ' + chosen.length + ' clippings…', '');
      }
    }
    const reducedWarn = page.reduced
      ? page.reduced + ' of ' + page.tiles + ' page tiles came back below full size, so parts of page.jpg are softer than the scan.'
      : null;
    // The status line promised a page copy; say plainly which bundles lack one.
    const noCopy = wantPageCopy ? chosen.filter((c) => !copyFits(c)).length : 0;
    const copyWarn = noCopy
      ? 'The copy of the web page went only with the clipping open in the browser; ' +
        (noCopy === 1 ? 'the other clipping was' : 'the other ' + noCopy + ' clippings were') +
        ' saved without one, since that page shows a different item.'
      : null;
    const summary = FHA.newspapers.summarizeStaged(results, [reducedWarn, copyWarn]);
    setStageResult(summary.text, summary.cls);
    $('handoff').classList.toggle('show', summary.showHandoff);
    // Leave only the clippings that were not saved ticked, so pressing Capture
    // again retries exactly those and never stages a saved one twice.
    const failed = results.filter((r) => !r.ok);
    if (failed.length) {
      const next = new Set(failed.map((r) => r.id));
      // Fewer clippings ticked now: the form describes a different set, so a
      // correction typed for the old set does not follow (the #211 rule).
      if (next.size !== state.npSelected.size) state.userEdited = new Set();
      state.npSelected = next;
      renderNpClips();
      fillNpFields();
    }
    return { saved: results.length - failed.length, results, rateHits: page.rateHits || 0 };
  }

  // Build and stage one clipping's bundle. Returns stageBundle's
  // { viaHost, where, hostWarning }; throws with a plain reason.
  async function stageNpClip(clip, np, page, pageBlob, o) {
    const clipBlob = await FHA.newspapers.toJpeg(FHA.newspapers.clipCanvas(page, clip));
    // Dashed boxes mark YOUR other clippings on the page only (on a clipping
    // page made by someone else, theirs is offered but is not yours).
    const others = np.ownClips.filter(
      (c) => c.id !== clip.id && (!np.me || !np.me.id || c.userId === np.me.id)
    );
    const combinedBlob = await FHA.newspapers.toJpeg(
      FHA.newspapers.combined(page, clip, np, others, npCaption(clip, np))
    );
    const assets = [
      { filename: 'clipping.jpg', blob: clipBlob, role: 'clipping', mode: 'newspapers-tiles' },
      { filename: 'page.jpg', blob: pageBlob, role: 'page', mode: 'newspapers-tiles' },
      { filename: 'annotated.jpg', blob: combinedBlob, role: 'annotated', mode: 'newspapers-derived' },
      {
        filename: 'clipping-data.json',
        blob: new Blob([JSON.stringify(npClipData(clip, np, o.texts.get(clip.id)), null, 2) + '\n'],
          { type: 'application/json' }),
        role: 'site-data',
        mode: 'newspapers-page-data',
      },
    ];
    if (o.pageCopy) {
      assets.push({ filename: o.pageCopy.filename, blob: o.pageCopy.blob, role: 'webpage', mode: 'singlefile' });
    }
    const fields = {
      // One clipping: the address in the form, which she may have corrected
      // (a /clip/ permalink, say). Several: each clipping's own (#211 review).
      url: (o.single && o.form.url) || clip.url || o.form.url || (state.prefill && state.prefill.url),
      title: o.title,
      accessed: captureJson.accessedDate(),
      sourceDate: o.single
        ? (o.form.date || clip.pageDate || np.pageDate || '')
        : (clip.pageDate || np.pageDate || ''),
      sourceType: o.form.type || 'newspaper',
      repository: o.single ? o.form.repo : (npPub(clip, np).name || 'Newspapers.com'),
      citation: npCitations(clip, np).genealogy,
      people: o.form.people,
      notes: [o.form.notes, npNotes(clip, np, page, o.copyNote)]
        .filter((t) => t && t.trim()).join('\n\n'),
      recipeHint: 'newspapers',
      assets: assets.map((a) => ({ file: a.filename, role: a.role, mode: a.mode, provisional: false })),
    };
    const cap = captureJson.build(fields);
    return stageBundle(captureJson.bundleName(o.title), o.pageHtml, cap, assets, { forceDownload: o.forceDownload });
  }

  // ── Ancestry record pages ─────────────────────────────────────────────────────
  // One bundle per record: page.html, the page copy, record-data.json (the
  // record's labelled fields, household and citation as the site shows them -
  // role site-data, never a transcript) and record.jpg (the linked scan, through
  // the same token call Ancestry's own Download button uses). A record with no
  // scan, or one Ancestry will not hand over, is still saved - the fields and
  // the citation are the index entry - with a note saying the image is missing.
  // The record also says what kind of source it is and when (its collection
  // and its own event date, lib/ancestry-record.js ancHints): sent in
  // capture.json so filing does not have to guess from the page (TI-066),
  // and only where the human left the form's type and date blank.

  // Seen It (seen-it/ in this repo) can say whether the archive already holds a
  // page image. Its ID is fixed by the "key" in its manifest, the same on every
  // computer (tests/test-panel-ancestry.js derives it from that key). It answers
  // only in its advanced mode; no answer just means "download it".
  const SEEN_IT_ID = 'cennkaddolfbhiabacldomegncpdphed';
  const SEEN_IT_WAIT_MS = 3000;

  // Where the archive already holds this page image, or null when Seen It is
  // absent, off, slow or does not know it: { where: 'archive' | 'inbox',
  // source?: 'S-…' }. Seen It counts a page only once a saved image backs it
  // (its helper's _holds_saved_image), never from an image id alone.
  async function seenItImage(key) {
    return (await seenItHeld([key]))[key] || null;
  }

  // seenItImage for several keys in one question (a memorial's photos):
  // { key: { where, source? } } for each one the archive holds.
  function seenItHeld(keys) {
    return new Promise((resolve) => {
      const timer = setTimeout(() => resolve({}), SEEN_IT_WAIT_MS);
      const done = (v) => {
        clearTimeout(timer);
        resolve(v);
      };
      if (!keys.length) {
        done({});
        return;
      }
      try {
        chrome.runtime.sendMessage(SEEN_IT_ID, { type: 'knownKeys', keys }, (r) => {
          const ok = !chrome.runtime.lastError && r && r.ok && r.keys;
          const out = {};
          for (const key of ok ? keys : []) {
            const st = r.keys[key];
            const sid = r.sids && /^S-[0-9a-z]{10}$/i.test(String(r.sids[key] || '')) ? r.sids[key] : null;
            if (st === 'captured') out[key] = { where: 'inbox' };
            else if (st === 'filed' || st === 'processed') out[key] = sid ? { where: 'archive', source: sid } : { where: 'archive' };
          }
          done(out);
        });
      } catch (e) {
        done({});
      }
    });
  }

  // Where a shared census page is, in words: the record of the household
  // member it was saved with (in this sitting), or the archive source.
  function describeSameImage(same) {
    if (same.record) return 'the capture of ' + (same.name || 'the record') + ' (' + same.url + ')';
    if (same.where === 'archive') return same.source ? 'the archive, source ' + same.source : 'the archive';
    return 'a capture waiting in the inbox to be filed';
  }

  // A site's "slow down" or "busy" answer, in any capture's words (the same
  // words lib/capture-batch.js classifyError reads): a batch download stops
  // after a capture that met one, even when the capture itself got through.
  const SLOW_DOWN_RE = /slow down|HTTP 429|HTTP 503|too many requests/i;
  // A missing image or photo whose cause passes - a "slow down", a site
  // having trouble, no connection, a page that did not answer, a lost login
  // - makes the save partial, and a batch comes back for it on a later Start
  // (lib/capture-batch.js markPartial). Every other cause lasts (a refused
  // download, a picture too small to be the record, no image at the
  // address, center-only): asking again would only save the same record
  // again, so the record is done, its note saying what is missing.
  // The fetchers say so themselves (`transient` on their answer: lib/
  // findagrave.js fetchPhoto, content.js's Ancestry scan, FamilySearch's
  // fsImage); these words are the fallback for an answer that does not, in
  // every wording the capture paths produce for a passing cause.
  const PASSES_RE = /slow down|HTTP 429|HTTP 5\d\d|having trouble|could not reach|internet connection|broke off|connection dropped|would not come through|did not answer|no answer from the page|HTTP 401|asked you to sign in/i;
  const passes = (why) => PASSES_RE.test(String(why || ''));
  // The causes known to last: refused, gone, restricted, too small, not the
  // file. Only these make a record saved without its file "done"; anything
  // the fetcher did not flag and no rule recognises keeps it partial, to be
  // tried again (at most MAX_PARTIAL times) - an unclassified failure of a
  // file the capture wanted is never success (PR #224 Codex 4504508005).
  const LASTS_RE = /restricted|offers no download|did not offer a downloadable|downloads may be disabled|too small|HTTP 40[0-9]\b|HTTP 410|no longer has this photo|refused the (image|photo)|not on Find a Grave|led away|sent a page instead|no image at this address|has no (image|gallery|memorial)/i;
  const missingPasses = (why, flagged) => !!flagged || passes(why) || !LASTS_RE.test(String(why || ''));
  // A Find a Grave answer that means "slow down" or "busy": 429 or 503.
  const busyStatus = (st) => st === 429 || st === 503;
  // A status whose cause passes: no answer at all, a "slow down", a server error.
  const passingStatus = (st) => st == null || st === 429 || st >= 500;

  // A batch download names the record it queued (`opts.expectKey`, the Seen
  // It key "rec:…", "fsrec:…", "fag:…"); the record the page shows must be
  // that one, checked from the record's own data before anything is
  // downloaded, or nothing is saved. An ordinary Capture names none.
  function notQueued(opts, key, label) {
    if (!opts || !opts.expectKey || opts.expectKey === key) return;
    throw new Error('the page shows ' + label + ', not the one queued, so nothing was saved');
  }

  // `opts` (a batch download only): see captureNewspapers. Returns what was
  // staged: { key, record, bundle, where, slowDown, note }.
  async function captureAncestryRecord(pageHtml, wantPageCopy, form, recordUrl, opts = {}) {
    const info = await autoFetch('Ancestry record details', {
      // A title she typed is hers and is used as typed (below); only the
      // form's untouched prefill goes through ancTitle's "is this just the
      // tab's own title?" test (PR #214 review).
      action: 'ancestryRecordInfo', formTitle: form.edited.has('f-title') ? '' : form.title,
    }, { recordUrl });
    if (!info || !info.ok) throw new Error((info && info.error) || 'could not read the Ancestry record');
    const rec = info.record;
    const hints = info.hints || {};
    const assets = [];
    notQueued(opts, 'rec:' + rec.dbId + ':' + rec.recordId, 'Ancestry record ' + rec.dbId + ':' + rec.recordId);
    // The record was read from the tab: it must still be on this record.
    await checkTabStillOn(recordUrl, [rec.recordId]);

    // A file the human dropped in is the record's image (her own download,
    // after Ancestry would not hand it over), saved WITH the index rather than
    // instead of it. Her screen-capture tick rides with it, as anywhere else.
    const dropped = form.dropped;
    const provisional = !!dropped && form.provisional;

    // Everyone in a household has their own record, but they share one census
    // page. A page already saved - earlier in this sitting, or in the archive
    // as Seen It knows it - is not fetched again: this bundle keeps the
    // person's own index entry and says where the page is.
    const imageKey = rec.imageId ? rec.dbId + ':' + rec.imageId : null;
    let sameImageAs = !dropped && imageKey ? state.ancImages[imageKey] || null : null;
    if (!dropped && imageKey && !sameImageAs) sameImageAs = await seenItImage('img:' + imageKey);
    // Read the page copy from the tab now, before the long scan download,
    // when it is sure to be kept (recordPageCopy).
    const willHaveScan = !!dropped || (!sameImageAs && !!rec.imageId);
    let pageCopy = await recordPageCopy('early', wantPageCopy || !willHaveScan, recordUrl, [rec.recordId]);
    let imageWarning = null;
    let imageFlags = { transient: false, slowDown: false };
    if (dropped) {
      assets.push({
        filename: 'record.' + dropped.ext, blob: dropped.blob,
        role: 'record', mode: 'manual', provisional,
      });
    } else if (sameImageAs) {
      rec.same_image_as = sameImageAs;
    } else if (rec.imageId) {
      setStageResult('Fetching the record’s image from Ancestry…', '');
      const img = await autoFetch('Ancestry record image', { action: 'ancestryImage', dbId: rec.dbId, imageId: rec.imageId },
        { recordUrl, ids: [rec.recordId] });
      if (img && img.ok) {
        assets.push({
          filename: 'record.' + (img.ext || 'jpg'),
          blob: bundle.base64ToBlob(img.base64, img.contentType),
          role: 'record', mode: 'ancestry-api',
        });
      } else {
        imageFlags = { transient: !img || !!img.transient, slowDown: !!(img && img.slowDown) };
        imageWarning = ((img && img.error) || 'no answer from the page') +
          '. The record’s index and citation are saved. To add the image, use Ancestry’s Download ' +
          'button, drop the file into the panel and press Capture again: that capture holds both.';
      }
    }
    // Whether THIS bundle holds the record's scan - not a link to it, not a
    // screen capture standing in for it. Seen It's helper counts the page as
    // saved only on this (PR #213 review): `imageId` alone is only the link.
    const imageSaved = !!rec.imageId && !provisional && assets.some((a) => a.role === 'record');
    rec.imageSaved = imageSaved;

    // A bundle that would hold nothing but record-data.json (no scan, the page
    // copy unticked) keeps the page copy anyway: filed alone, the index file
    // would become the source's main file under a new name, and the record
    // would lose both its role and its page (PR #213 audit #4).
    const hasScan = assets.some((a) => a.role === 'record');
    if (!pageCopy && !hasScan) pageCopy = await recordPageCopy('late', true, recordUrl, [rec.recordId]);
    let forcedCopy = false;
    if (pageCopy && (wantPageCopy || !hasScan)) {
      assets.unshift({ filename: pageCopy.filename, blob: pageCopy.blob, role: 'webpage', mode: 'singlefile' });
      forcedCopy = !wantPageCopy;
    }

    const data = {
      note: 'Captured from Ancestry by the archive capture extension. "fields" and "household" are ' +
        'Ancestry’s index of the record, as the site shows it: an index, not a transcript of the image. ' +
        '"imageSaved" says whether this capture holds the record’s image; "imageId" is the image the record links to.',
      record: rec,
      fields: info.fields,
      household: info.household,
      citation: info.citation,
    };
    assets.push({
      filename: 'record-data.json',
      blob: new Blob([JSON.stringify(data, null, 2) + '\n'], { type: 'application/json' }),
      role: 'site-data',
      mode: 'ancestry-page-data',
    });
    const title = (form.edited.has('f-title') && form.title) || info.title ||
      ('Ancestry record ' + rec.dbId + ':' + rec.recordId);
    const cite = info.citation || {};
    const sameNote = sameImageAs
      ? 'Same page (image ' + rec.imageId + ') as ' + describeSameImage(sameImageAs) +
        '; the image was not downloaded again.'
      : '';
    const notes = [
      gatherNotes(provisional, form.notes),
      cite.Citation ? 'Ancestry source citation: ' + cite.Citation : '',
      sameNote,
      imageWarning ? 'The record’s image was not saved: ' + imageWarning : '',
    ].filter((t) => t && t.trim()).join('\n\n');
    const fields = {
      url: rec.url,
      title,
      accessed: captureJson.accessedDate(),
      // What the human typed wins; else what the record says about itself.
      sourceDate: form.date || hints.sourceDate || '',
      sourceType: form.type || hints.sourceType || '',
      sourceClass: hints.sourceClass || '',
      repository: form.repo || 'Ancestry.com',
      people: form.people,
      notes,
      recipeHint: 'ancestry',
      assets: assets.map((a) => ({ file: a.filename, role: a.role, mode: a.mode, provisional: !!a.provisional })),
    };
    const cap = captureJson.build(fields);
    const bundleName = captureJson.bundleName(title);
    const st = await stageBundle(bundleName, pageHtml, cap, assets, opts);
    // The bundle now holds this page: the next household member's capture
    // points at THIS record - its key and address, which outlive the staging
    // folder (ingest re-slugs it, filing renames it) - instead of fetching it.
    if (imageKey && imageSaved) {
      state.ancImages[imageKey] = {
        record: 'rec:' + rec.dbId + ':' + rec.recordId, url: rec.url, name: rec.name || null,
      };
    }
    const pageNote = sameImageAs
      ? 'The census page was not downloaded again: it is already saved in ' + describeSameImage(sameImageAs) + '.'
      : null;
    const copyNote = forcedCopy
      ? 'A copy of the web page was kept too: with no image, it is what this record’s index is filed with.'
      : null;
    reportStaged(st.where, st.viaHost, imageWarning,
      [st.hostWarning, pageNote, copyNote].filter(Boolean).join('\n') || null);
    return {
      key: 'rec:' + rec.dbId + ':' + rec.recordId,
      record: { dbId: rec.dbId, recordId: rec.recordId, name: rec.name || null },
      bundle: st.bundle, where: st.where,
      // The Seen It key that says this record's scan is saved (asked before
      // a partial save is captured again).
      imageKey: imageKey ? 'img:' + imageKey : null,
      partial: !!imageWarning && missingPasses(imageWarning, imageFlags.transient),
      slowDown: imageFlags.slowDown || SLOW_DOWN_RE.test(imageWarning || ''),
      note: imageWarning ? 'image not saved: ' + imageWarning.split('. The record')[0]
        : sameImageAs ? 'page already saved in ' + describeSameImage(sameImageAs) : null,
    };
  }

  // ── Photos shared on Ancestry ─────────────────────────────────────────────────
  // A photo another member shared to their tree, open in Ancestry's media
  // viewer (/mediaui-viewer/…/media/{mediaId}), is one bundle that files as a
  // PHOTO source: the photo itself (role `front`, the source's main image -
  // SPEC §13's name for a photo's picture; it is not a record scan, so not
  // `record`) and photo-data.json (the photo's Details as the page shows them
  // - title, date, place, description, who added it, who it is linked to -
  // role site-data), plus page.html, the scrape source, which is never filed.
  // The photo is the image the viewer shows, fetched from Ancestry's own site
  // in her session and kept byte for byte. capture.json's `url` is the
  // viewer's address without its query, so Seen It knows the photo again
  // (med:{mediaId}) once it is filed.
  //
  // No other member's name reaches the archive: no copy of the page is kept
  // for a shared photo (pageCopyOn), the Details reader stops at member lists
  // (lib/ancestry-record.js ancMediaLabel), photo-data.json holds only what
  // that reader extracts, and the ingestion recipe reads no people or text
  // from a shared photo's page. No photo, no capture: when the photo will not
  // come and she has not dropped her own file in, nothing is staged and the
  // panel says why.
  //
  // The photo's file keeps its name for good: a photo source's picture goes
  // into the photo library, where the archive never renames a file. So it is
  // named for the photo (ancestry-{mediaId}-photo.jpg, lib/ancestry-record.js
  // ancPhotoFileStem) rather than `record.jpg`, which the next shared photo
  // would collide with. The name comes from the media id alone - checked to be
  // one - never from the title, so nothing the page writes reaches a filename.

  // `opts` (a batch download only): see captureNewspapers. Returns what was
  // staged: { key, bundle, where, note }.
  async function captureAncestryPhoto(pageHtml, form, recordUrl, opts = {}) {
    const info = await autoFetch('Ancestry photo details', { action: 'ancestryPhotoInfo' }, { recordUrl });
    if (!info || !info.ok) throw new Error((info && info.error) || 'could not read the photo on Ancestry');
    const media = info.media;
    const details = info.details || {};
    const hints = info.hints || {};
    if (!media.fileStem) throw new Error('the page does not name the photo it shows, so nothing was saved');
    notQueued(opts, 'med:' + media.mediaId, 'Ancestry photo ' + media.mediaId);
    // The photo was read from the tab: it must still be on this photo.
    await checkTabStillOn(recordUrl, [media.mediaId]);

    // The page read again, before anything is saved: what it shows now that
    // the form does not - the people it is linked to, Details she opened
    // after the panel read the page - is put in front of her first, and
    // nothing is staged. A photo is captured once; there is no "capture it
    // again for the rest" (a second capture of a filed photo cannot be filed,
    // PR #258 review).
    const shown = state.photoShown || { keys: [], people: [] };
    const newKeys = shown.keys ? Object.keys(details).filter((k) => k !== 'title' && shown.keys.indexOf(k) < 0) : [];
    const newPeople = (details.linkedTo || []).filter((n) => shown.people.indexOf(n.toLowerCase()) < 0);
    if (newKeys.length || newPeople.length) {
      for (const n of newPeople) addPersonRow(n, true);
      if (!form.edited.has('f-date') && !$('f-date').value.trim() && hints.sourceDate) $('f-date').value = hints.sourceDate;
      state.photoShown = {
        keys: shown.keys ? shown.keys.concat(newKeys) : null,
        people: shown.people.concat(newPeople.map((n) => n.toLowerCase())),
      };
      clearAutoLog(); // nothing was saved: the box goes back to what Capture will save
      setStageResult('I found more on the page — check the people below, then press Capture this photo.', 'warn');
      return { staged: false };
    }

    const dropped = form.dropped;
    const provisional = !!dropped && form.provisional;
    // No copy of the page, ever (pageCopyOn): the photo and its details only.
    const assets = [];
    if (dropped) {
      // Her own file (one she downloaded, or a scan she has) is the photo.
      assets.push({ filename: media.fileStem + '.' + dropped.ext, blob: dropped.blob, role: 'front', mode: 'manual',
        provisional, primary: true });
    } else {
      setStageResult('Fetching the photo from Ancestry…', '');
      const img = await autoFetch('Ancestry photo', { action: 'ancestryPhoto', mediaId: media.mediaId },
        { recordUrl, ids: [media.mediaId] });
      if (!img || !img.ok) {
        // No photo, no capture (PR #258 review): a photo source without its
        // picture would file as the photo - Seen It would show it as saved -
        // and the retry with her own file would make a second source.
        throw new Error('the photo could not be fetched from Ancestry: ' +
          String((img && img.error) || 'no answer from the page').replace(/[.\s]+$/, '') +
          '. Nothing was saved. If you can download the photo yourself (right-click it, Save image as), ' +
          'drop that file into the panel and press Capture this photo');
      }
      assets.push({
        filename: media.fileStem + '.' + (/^[a-z0-9]{1,5}$/i.test(String(img.ext || '')) ? img.ext : 'jpg'),
        blob: bundle.base64ToBlob(img.base64, img.contentType),
        role: 'front', mode: 'ancestry-media', primary: true,
      });
    }

    const data = {
      note: 'Captured from Ancestry by the archive capture extension: a photo another member shared to their ' +
        'tree. "details" is what the photo’s Details panel showed - written by whoever shared it, not checked. ' +
        '"photoSaved" is false when the photo in this capture is a screen capture; "imageUrl" is where the ' +
        'viewer shows it.',
      media: {
        mediaId: media.mediaId,
        treeId: media.treeId,
        personId: media.personId,
        url: media.url,
        title: media.title,
        imageUrl: media.imageUrl,
        photoSaved: !provisional,
      },
      details,
    };
    assets.push({
      filename: 'photo-data.json',
      blob: new Blob([JSON.stringify(data, null, 2) + '\n'], { type: 'application/json' }),
      role: 'site-data',
      mode: 'ancestry-media-data',
    });

    const title = (form.edited.has('f-title') && form.title) || media.title || form.title ||
      ('Photo shared on Ancestry ' + media.mediaId);
    // Who first added the photo is another member's name: it stays in
    // photo-data.json, never in the record's notes, which a packet or a
    // shared site carries (owner-away decision, PR #258 review).
    const said = [
      details.date ? 'Date: ' + details.date : '',
      details.place ? 'Place: ' + details.place : '',
      details.linkedTo ? 'Linked to: ' + details.linkedTo.join('; ') : '',
    ].filter(Boolean);
    const notes = [
      gatherNotes(provisional, form.notes),
      'A photo shared on Ancestry by another member' +
        (said.length ? '. Its details on Ancestry:\n' + said.join('\n') : '.'),
      details.description ? 'Description on Ancestry:\n' + details.description : '',
    ].filter((t) => t && t.trim()).join('\n\n');
    const fields = {
      url: media.url,
      title,
      accessed: captureJson.accessedDate(),
      // What the human typed wins; else what the photo says about itself.
      sourceDate: form.date || hints.sourceDate || '',
      sourceType: form.type || hints.sourceType || 'photo',
      repository: form.repo || 'Ancestry.com',
      // Only the people she was shown and left ticked (the form lists those
      // the photo is linked to when the panel reads the page).
      people: form.people,
      notes,
      recipeHint: 'ancestry',
      assets: assets.map((a) => ({ file: a.filename, role: a.role, mode: a.mode, provisional: !!a.provisional,
        primary: !!a.primary })),
    };
    const cap = captureJson.build(fields);
    const st = await stageBundle(captureJson.bundleName(title), pageHtml, cap, assets, opts);
    reportStaged(st.where, st.viaHost, null, st.hostWarning || null);
    return { key: 'med:' + media.mediaId, bundle: st.bundle, where: st.where, note: null };
  }

  // ── FamilySearch records and images ───────────────────────────────────────────
  // A record (ark:/61903/1:1:{id}, or the image viewer opened from one) is one
  // bundle: page.html, the page copy, record-data.json (FamilySearch's own
  // GEDCOM X data for the record with its citation - role site-data, the
  // site's index, never a transcript) and record.jpg (the full image behind
  // ark:/61903/3:1:{image}). A bare image viewer saves its full image the
  // ordinary way (buildEvidence). All of it through FamilySearch's data
  // service in the researcher's signed-in session; the login itself stays in
  // the page (content.js) and never reaches this panel.

  // Ask the page, as soon as it is read, whether its image can be saved from
  // here - so a restricted (center-only) image is said up front ("the
  // record's data only") rather than as a failure after Capture - and, on a
  // record, FamilySearch's own name for it, which becomes the title unless
  // the human has typed one (state.userEdited).
  async function checkFamilySearch() {
    const seq = ++state.fsCheckSeq;
    state.fsCheck = null;
    if (!(state.fsRecord || state.fsImage) || !state.tabId) return;
    state.fsCheck = { checking: true };
    updateAssetStatus();
    let r = null;
    try {
      r = await sendToTab(state.tabId, { action: 'familysearchImageCheck' });
    } catch (e) {
      r = { ok: false, error: e.message };
    }
    if (seq !== state.fsCheckSeq) return; // the tab moved on; a newer read owns the state
    if (r && (r.slowDown || SLOW_DOWN_RE.test(String(r.error || r.reason || '')))) {
      state.preflightSlowDown = true;
      state.preflightSlowDownSite = 'familysearch';
    }
    state.fsCheck = r && r.ok ? r : { unknown: true, error: (r && r.error) || 'no answer from the page' };
    if (r && r.ok && r.title && !state.userEdited.has('f-title')) $('f-title').value = r.title;
    restrictedImageNote(r);
    updateAssetStatus();
  }

  // What the status line says about a FamilySearch page's files, and whether
  // that is a warning (amber): { text, warn }.
  function fsStatus() {
    const fc = state.fsCheck || {};
    if (state.fsRecord) {
      if (fc.checking || !state.fsCheck) {
        return { text: 'Record files: the record’s FamilySearch data and citation + its image (checking that FamilySearch lets it be saved from here…)' };
      }
      if (fc.unknown) {
        return { text: 'Record files: the record’s FamilySearch data and citation + its image, if FamilySearch hands it over (the check did not answer: ' + fc.error + ')' };
      }
      if (fc.available) return { text: 'Record files: the record’s FamilySearch data and citation + its image ' + fc.imageId + ' (FamilySearch, auto)' };
      if (!fc.imageId) return { text: 'Record files: the record’s FamilySearch data and citation (FamilySearch, auto) - no image: ' + fc.reason };
      return {
        text: 'Record files: the record’s data only (FamilySearch). Its image ' + fc.imageId +
          ' can’t be saved from here - ' + fc.reason + '. Drop the image in if you have it.',
        warn: true,
      };
    }
    const id = (state.prefill && state.prefill.familysearchImageId) || fc.imageId || '';
    if (fc.checking || !state.fsCheck) return { text: 'Record file: full image ' + id + ' (FamilySearch, checking…)' };
    if (fc.ok && !fc.available) {
      return { text: 'This image can’t be saved from here - ' + fc.reason + '. Drop the file in if you have it.', warn: true };
    }
    return { text: 'Record file: full image ' + id + ' (FamilySearch, auto)' };
  }

  // `opts` (a batch download only): see captureNewspapers. Returns what was
  // staged: { key, record, bundle, where, slowDown, note }.
  async function captureFamilySearchRecord(pageHtml, wantPageCopy, form, recordUrl, opts = {}) {
    const info = await autoFetch('FamilySearch record details', {
      action: 'familysearchRecordInfo',
      // FamilySearch's own name for the record; a title she typed is used
      // as typed below, never second-guessed (PR #214 review).
      formTitle: '',
    }, { recordUrl });
    if (!info || !info.ok) throw new Error((info && info.error) || 'could not read the FamilySearch record');
    const rec = info.record;
    const hints = info.hints || {};
    const assets = [];
    notQueued(opts, 'fsrec:' + String(rec.fsId).toUpperCase(), 'FamilySearch record ' + rec.fsId);
    // The record was read from the tab: it must still be on this record.
    await checkTabStillOn(recordUrl, ['1:1:' + rec.fsId]);
    const dropped = form.dropped;
    const provisional = !!dropped && form.provisional;

    // Household members are each their own record on one image: an image
    // saved earlier in this sitting, or one Seen It says the archive holds,
    // is not fetched again (as for Ancestry).
    const imageKey = rec.imageId || null;
    let sameImageAs = !dropped && imageKey ? state.fsImages[imageKey] || null : null;
    if (!dropped && imageKey && !sameImageAs) sameImageAs = await seenItImage('fsimg:' + imageKey);
    // The page copy, from the tab before the long image download, when it is
    // sure to be kept (recordPageCopy).
    const willHaveImage = !!dropped || (!sameImageAs && !!imageKey);
    let pageCopy = await recordPageCopy('early', wantPageCopy || !willHaveImage, recordUrl, ['1:1:' + rec.fsId]);
    let imageWarning = null;
    let imageFlags = { transient: false, slowDown: false };
    if (dropped) {
      assets.push({ filename: 'record.' + dropped.ext, blob: dropped.blob, role: 'record', mode: 'manual', provisional });
    } else if (sameImageAs) {
      rec.same_image_as = sameImageAs;
    } else if (imageKey) {
      setStageResult('Fetching the record’s image from FamilySearch…', '');
      const img = await autoFetch('FamilySearch image ' + imageKey, { action: 'familysearchImage', imageId: imageKey },
        { recordUrl, ids: ['1:1:' + rec.fsId] });
      if (img && img.ok) {
        assets.push({
          filename: 'record.' + (img.ext || 'jpg'),
          blob: bundle.base64ToBlob(img.base64, img.contentType),
          role: 'record', mode: 'familysearch-api',
        });
      } else {
        imageFlags = { transient: !img || !!img.transient, slowDown: !!(img && img.slowDown) };
        imageWarning = ((img && img.error) || 'no answer from the page') +
          '. The record’s data and citation are saved. To add the image, use FamilySearch’s ' +
          'Download button, drop the file into the panel and press Capture again: that capture holds both.';
      }
    }
    // Whether THIS bundle holds the image - not a link to it, not a screen
    // capture standing in for it. Seen It counts the image as saved only on
    // this; `imageId` is only the image the record links to.
    const imageSaved = !!imageKey && !provisional && assets.some((a) => a.role === 'record');
    rec.imageSaved = imageSaved;

    // No image in the bundle: the page copy comes too, ticked or not, so the
    // record's data is never filed as the source's main file.
    const hasImage = assets.some((a) => a.role === 'record');
    if (!pageCopy && !hasImage) pageCopy = await recordPageCopy('late', true, recordUrl, ['1:1:' + rec.fsId]);
    let forcedCopy = false;
    if (pageCopy && (wantPageCopy || !hasImage)) {
      assets.unshift({ filename: pageCopy.filename, blob: pageCopy.blob, role: 'webpage', mode: 'singlefile' });
      forcedCopy = !wantPageCopy;
    }
    const data = {
      note: 'Captured from FamilySearch by the archive capture extension. "gedcomx" is FamilySearch’s own ' +
        'data for this record (its index of the record, as the site holds it): an index, not a transcript of ' +
        'the image. "imageSaved" says whether this capture holds the record’s image; "imageId" is the image ' +
        'the record links to.',
      record: rec,
      citation: info.citation,
      title: info.title,
      gedcomx: info.gedcomx,
    };
    assets.push({
      filename: 'record-data.json',
      blob: new Blob([JSON.stringify(data, null, 2) + '\n'], { type: 'application/json' }),
      role: 'site-data',
      mode: 'familysearch-record-data',
    });
    const title = (form.edited.has('f-title') && form.title) || info.bundleTitle ||
      ('FamilySearch record ' + rec.fsId);
    const imageArk = imageKey ? 'https://www.familysearch.org/ark:/61903/3:1:' + imageKey : '';
    // The Notes box may already say why the image is not saved (the check
    // before Capture put it there, restrictedImageNote): then it is not said
    // twice. If the image came through after all, the panel's own line goes.
    const ownNotes = hasImage || sameImageAs ? withoutLine(form.notes, form.fsNoteLine) : form.notes;
    const saidAlready = !!imageKey && ownNotes.indexOf(fsNoteMark(imageKey)) !== -1;
    const notes = [
      gatherNotes(provisional, ownNotes),
      info.citation ? 'FamilySearch citation: ' + info.citation : '',
      sameImageAs ? 'Same image (' + imageKey + ') as ' + describeSameImage(sameImageAs) + '; the image was not downloaded again.' : '',
      imageWarning && !saidAlready ? fsNoteMark(imageKey) + '(' + imageArk + ') could not be downloaded - ' +
        imageWarning.split('. The record')[0] + '. Checked ' + captureJson.accessedDate() + '.' : '',
    ].filter((t) => t && t.trim()).join('\n\n');
    const fields = {
      url: rec.url,
      title,
      accessed: captureJson.accessedDate(),
      // What she typed wins; else what the record says about itself.
      sourceDate: form.date || hints.sourceDate || '',
      sourceType: form.type || hints.sourceType || '',
      sourceClass: hints.sourceClass || '',
      repository: form.edited.has('f-repo') && form.repo ? form.repo : 'FamilySearch',
      people: form.people,
      notes,
      recipeHint: 'familysearch',
      assets: assets.map((a) => ({ file: a.filename, role: a.role, mode: a.mode, provisional: !!a.provisional })),
    };
    const cap = captureJson.build(fields);
    const st = await stageBundle(captureJson.bundleName(title), pageHtml, cap, assets, opts);
    if (imageKey && imageSaved) {
      state.fsImages[imageKey] = { record: 'fsrec:' + rec.fsId, url: rec.url, name: rec.name || null };
    }
    const pageNote = sameImageAs
      ? 'The image was not downloaded again: it is already saved in ' + describeSameImage(sameImageAs) + '.'
      : null;
    const copyNote = forcedCopy
      ? 'A copy of the web page was kept too: with no image, it is what this record’s data is filed with.'
      : null;
    reportStaged(st.where, st.viaHost, imageWarning,
      [st.hostWarning, pageNote, copyNote].filter(Boolean).join('\n') || null);
    return {
      key: 'fsrec:' + String(rec.fsId).toUpperCase(),
      record: { fsId: rec.fsId, name: rec.name || null },
      bundle: st.bundle, where: st.where,
      imageKey: imageKey ? 'fsimg:' + String(imageKey).toUpperCase() : null,
      partial: !!imageWarning && missingPasses(imageWarning, imageFlags.transient),
      slowDown: imageFlags.slowDown || SLOW_DOWN_RE.test(imageWarning || ''),
      note: imageWarning ? 'image not saved: ' + imageWarning.split('. The record')[0]
        : sameImageAs ? 'image already saved in ' + describeSameImage(sameImageAs) : null,
    };
  }

  // ── Find a Grave memorials ──────────────────────────────────────────────────
  // A memorial (/memorial/{id}/{slug}) is one bundle: page.html, the page
  // copy, record-data.json (what the memorial says, with Find a Grave's own
  // citation and the list of its photos - role site-data) and each photo at
  // full size, its role from Find a Grave's photo type (grave-1, person-1,
  // family-1, document-1, …). No login: the content script reads the memorial
  // and its gallery on Find a Grave without cookies, and the photos come from
  // Find a Grave's own image host only (lib/findagrave.js).

  // At most this many photos in one capture: a memorial with hundreds (a
  // famous grave) is a different job from a family memorial.
  const FAG_MAX_PHOTOS = 40;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // `opts` (a batch download only): see captureNewspapers. Returns what was
  // staged: { key, record, bundle, where, slowDown, note }.
  async function captureFindAGraveMemorial(pageHtml, wantPageCopy, form, recordUrl, opts = {}) {
    const info = await autoFetch('Find a Grave memorial details',
      { action: 'findagraveMemorialInfo', today: captureJson.accessedDate() }, { recordUrl });
    if (!info || !info.ok) throw new Error((info && info.error) || 'could not read the Find a Grave memorial');
    const rec = info.record;
    const hints = info.hints || {};
    notQueued(opts, 'fag:' + rec.memorialId, 'Find a Grave memorial ' + rec.memorialId);
    // The memorial was read from the tab: it must still be this memorial.
    const wantId = (/\/memorial\/(\d+)/.exec(String(recordUrl || '')) || [])[1];
    if (wantId !== rec.memorialId) {
      throw new TabMoved('the page in the tab changed while this memorial was being saved, so nothing was ' +
        'saved. The panel is reading the new page now - check it is the memorial you mean, then press Capture again');
    }
    await checkTabStillOn(recordUrl);
    const dropped = form.dropped;
    const provisional = !!dropped && form.provisional;

    // Its photos, from its gallery (by memorial id, not "whatever the tab
    // shows"). A gallery that cannot be read leaves the memorial's facts.
    setStageResult('Reading the memorial’s photos on Find a Grave…', '');
    const gallery = await autoFetch('Find a Grave list of photos',
      { action: 'findagraveGallery', memorialId: rec.memorialId, slug: rec.slug }, { recordUrl });
    const galleryOk = !!(gallery && gallery.ok);
    const listed = galleryOk ? gallery.photos : [];
    const photos = listed.map((p) => ({
      photoId: p.photoId, url: p.url, role: p.role, type: p.type || null,
      caption: p.caption || null, addedBy: p.addedBy || null, file: null,
    }));
    // How many photos Find a Grave says the memorial has (null when that is
    // not known), and whether the list was read only in part: a later page
    // failed, or it listed fewer than the count (PR #221 audit).
    const shown = galleryOk && typeof gallery.total === 'number' ? gallery.total : null;
    const partList = galleryOk && (!!gallery.partial || shown == null || shown > photos.length);
    // Find a Grave said "slow down" while the list was read: no photo is
    // asked for now either.
    let slowDown = galleryOk && busyStatus(gallery.partialStatus);

    // A photo already saved - in this sitting, or in the archive as Seen It
    // knows it (only from a saved file, never from the memorial alone) - is
    // not downloaded again: the list says where it is.
    const held = await seenItHeld(photos.filter((p) => !state.fagPhotos[p.photoId]).map((p) => 'fagphoto:' + p.photoId));
    let wanted = 0;
    for (const p of photos) {
      const same = state.fagPhotos[p.photoId] || held['fagphoto:' + p.photoId];
      if (same) p.same_image_as = same;
      else if (wanted >= FAG_MAX_PHOTOS) p.skipped = 'over the ' + FAG_MAX_PHOTOS + '-photo limit of one capture';
      else wanted++;
    }
    const toFetch = photos.filter((p) => !p.same_image_as && !p.skipped);

    // The page copy, from the tab before the long downloads, when it is sure
    // to be kept (recordPageCopy): asked for, or no photo will be saved.
    let pageCopy = await recordPageCopy('early', wantPageCopy || (!dropped && (!toFetch.length || slowDown)), recordUrl);

    const assets = [];
    const failed = [];
    const notTried = [];
    // The photos whose failure may pass (the fetcher's own word), kept apart
    // so nothing extra goes into record-data.json.
    const transientPhotos = new Set();
    for (let i = 0; i < toFetch.length; i++) {
      const p = toFetch[i];
      if (slowDown) {
        p.error = 'not tried: Find a Grave asked us to slow down';
        notTried.push(p);
        continue;
      }
      if (i) await sleep(FHA.findagrave.pauseMs());
      setStageResult('Downloading photo ' + (i + 1) + ' of ' + toFetch.length + ' from Find a Grave…', '');
      const row = autoRow('Find a Grave photo ' + (i + 1) + ' of ' + toFetch.length + ' (' + p.role + ')');
      const got = await FHA.findagrave.fetchPhoto(p.url, (u, init) => window.fetch(u, init));
      if (got.ok) {
        p.file = p.role + '.' + got.ext;
        assets.push({ filename: p.file, blob: got.blob, role: p.role, mode: 'findagrave-photo' });
        row(true, got.blob && got.blob.size ? Math.max(1, Math.round(got.blob.size / 1024)) + ' KB' : '');
      } else {
        p.error = got.error;
        if (got.transient) transientPhotos.add(p.photoId);
        failed.push(p);
        row(false, got.error);
        if (got.slowDown) slowDown = true;
        noteSlowDown(got, 'findagrave');
      }
    }
    // The gravestone is the source's main picture when it has one, else the
    // person, else the first photo saved (`is_primary`, which filing honours;
    // by name alone "family-1" would come before "grave-1" - PR #221 audit).
    const lead = ['grave', 'person', ''].map((k) => assets.find((a) => a.role.indexOf(k) === 0)).find(Boolean);
    if (lead) lead.primary = true;
    if (dropped) {
      assets.push({ filename: 'record.' + dropped.ext, blob: dropped.blob, role: 'record', mode: 'manual', provisional });
    }

    // No photo and no file of hers: the page copy comes too, ticked or not,
    // so the memorial's facts are never filed as the source's main file.
    const hasFile = assets.length > 0;
    if (!pageCopy && !hasFile) pageCopy = await recordPageCopy('late', true, recordUrl);
    let forcedCopy = false;
    if (pageCopy && (wantPageCopy || !hasFile)) {
      assets.unshift({ filename: pageCopy.filename, blob: pageCopy.blob, role: 'webpage', mode: 'singlefile' });
      forcedCopy = !wantPageCopy;
    }

    const data = {
      note: 'Captured from Find a Grave by the archive capture extension. "record" is what the memorial page ' +
        'says - a volunteer’s compilation from the stone, records and family knowledge, not an original record. ' +
        '"photos" lists the memorial’s photos: "file" is the file this capture saved it as, or null - then ' +
        '"same_image_as" says where it is already saved, "error" why it could not be saved, "skipped" why it was ' +
        'not tried. "photosTotal" is how many photos the memorial shows (null when that is not known); ' +
        '"photosPartial" is true when its photo list was read only in part.',
      record: rec,
      citation: info.citation || null,
      title: info.title,
      photos,
      photosTotal: shown,
    };
    if (!galleryOk || partList) data.photosPartial = true;
    assets.push({
      filename: 'record-data.json',
      blob: new Blob([JSON.stringify(data, null, 2) + '\n'], { type: 'application/json' }),
      role: 'site-data',
      mode: 'findagrave-record-data',
    });

    const saved = photos.filter((p) => p.file);
    const already = photos.filter((p) => p.same_image_as);
    const skipped = photos.filter((p) => p.skipped);
    const kept = saved.length + already.length;
    const named = (list) => list.map((p) => p.role).join(', ');
    const plural = (n, one) => n + ' ' + one + (n === 1 ? '' : 's');
    // The next capture is a capture of its own (it is not added to this one):
    // say so, so the rest is filed with this memorial by hand (PR #221 audit).
    const again = 'that capture is one of its own, so when you file it, add its photos to this memorial’s source.';
    const lines = [];
    if (!galleryOk) {
      lines.push('The memorial’s photos were not saved: ' + ((gallery && gallery.error) || 'no answer from the page') +
        '. Its facts and citation are saved; press Capture again later for the photos - ' + again);
    } else if (partList) {
      lines.push((shown != null
        ? 'Find a Grave shows ' + plural(shown, 'photo') + '; ' + kept + ' are saved'
        : 'Find a Grave’s photo list could not be read in full; ' + kept + (kept === 1 ? ' photo is' : ' photos are') + ' saved') +
        (gallery.partialError ? ' (' + gallery.partialError.replace(/\.[^.]*$/, '') + ')' : '') +
        ' - press Capture again later for the rest; ' + again);
    }
    if (failed.length || notTried.length) {
      lines.push(plural(failed.length + notTried.length, 'photo') + (failed.length + notTried.length === 1 ? ' was' : ' were') +
        ' not saved (' + failed.map((p) => p.role + ': ' + p.error).concat(notTried.length
          ? [named(notTried) + ': not tried after Find a Grave asked us to slow down'] : []).join('; ') + '). ' +
        (slowDown ? 'Wait a few minutes, then press Capture again' : 'Press Capture again to try again') +
        (kept ? ': the photos already saved are not downloaded again; ' : ': ') + again);
    }
    if (skipped.length) {
      lines.push(plural(skipped.length, 'more photo') + (skipped.length === 1 ? ' was' : ' were') + ' not saved in this ' +
        'capture (at most ' + FAG_MAX_PHOTOS + ' at a time): press Capture again to save the next ones; ' + again);
    }
    const warning = lines.join('\n') || null;
    const photoNotes = [
      saved.length ? plural(saved.length, 'photo') + ' saved from the memorial (' + named(saved) + ').' : '',
      already.length ? already.map((p) => 'Photo ' + p.role + ' (Find a Grave photo ' + p.photoId + ') is already saved in ' +
        describeSameImage(p.same_image_as) + '; it was not downloaded again.').join('\n') : '',
      skipped.length ? named(skipped) + ': not downloaded (' + skipped[0].skipped + ').' : '',
      galleryOk && !partList && shown === 0 ? 'The memorial has no photos.' : '',
    ];
    const title = (form.edited.has('f-title') && form.title) || info.title ||
      ('Find a Grave memorial ' + rec.memorialId);
    const notes = [
      gatherNotes(provisional, form.notes),
      info.citation ? 'Find a Grave citation: ' + info.citation : '',
    ].concat(photoNotes, [warning ? 'Not saved: ' + warning : ''])
      .filter((t) => t && t.trim()).join('\n\n');
    const fields = {
      url: rec.url,
      title,
      accessed: captureJson.accessedDate(),
      // What she typed wins; else what the memorial says about itself.
      sourceDate: form.date || hints.sourceDate || '',
      sourceType: form.type || hints.sourceType || '',
      sourceClass: hints.sourceClass || '',
      repository: form.edited.has('f-repo') && form.repo ? form.repo : 'Find a Grave',
      people: form.people,
      citation: info.citation || '',
      notes,
      recipeHint: 'findagrave',
      assets: assets.map((a) => ({
        file: a.filename, role: a.role, mode: a.mode, provisional: !!a.provisional, primary: !!a.primary,
      })),
    };
    const cap = captureJson.build(fields);
    const st = await stageBundle(captureJson.bundleName(title), pageHtml, cap, assets, opts);
    // These photos are saved now: capturing the memorial again points at
    // this capture - its memorial and address, which outlive the staging
    // folder - instead of downloading them again.
    for (const p of saved) {
      state.fagPhotos[p.photoId] = { record: 'fag:' + rec.memorialId, url: rec.url, name: rec.name || null };
    }
    const copyNote = forcedCopy
      ? 'A copy of the web page was kept too: with no photo saved, it is what this memorial’s facts are filed with.'
      : null;
    const heldNote = already.length
      ? (already.length === 1 ? 'One photo was' : already.length + ' photos were') + ' already saved, so not downloaded again.'
      : null;
    // Its own words for a photo that was not saved (reportStaged's first slot
    // speaks of "the record image"); any of these lines turns the result amber.
    reportStaged(st.where, st.viaHost, null,
      [warning, st.hostWarning, heldNote, copyNote].filter(Boolean).join('\n') || null);
    const missing = failed.length + notTried.length + skipped.length;
    return {
      key: 'fag:' + rec.memorialId,
      record: { memorialId: rec.memorialId, slug: rec.slug || null, name: rec.name || null },
      bundle: st.bundle, where: st.where,
      // Photos still to come make it partial: some not tried (a "slow down",
      // over the limit of one capture), or the list or a photo failed for a
      // cause that passes. A photo refused or gone, or a list that is short
      // every time, would only be saved the same way again.
      partial: notTried.length > 0 || skipped.length > 0 ||
        (!galleryOk && (passingStatus(gallery && gallery.status) || missingPasses(gallery && gallery.error))) ||
        (galleryOk && partList && !!gallery.partialError &&
          (passingStatus(gallery.partialStatus) || missingPasses(gallery.partialError))) ||
        failed.some((ph) => missingPasses(ph.error, transientPhotos.has(ph.photoId))),
      // A "slow down" while its photos were listed or fetched (429): the
      // memorial is saved with the photos that came, and a batch stops.
      slowDown: slowDown || (!galleryOk && (busyStatus(gallery && gallery.status) ||
        SLOW_DOWN_RE.test((gallery && gallery.error) || ''))),
      note: plural(saved.length, 'photo') + ' saved' + (already.length ? ', ' + already.length + ' already saved' : '') +
        (missing ? ', ' + missing + ' not saved' : '') + (!galleryOk || partList ? ', photo list read only in part' : ''),
    };
  }

  // Stage one bundle: straight into the archive when the native host answers,
  // else the staging-folder download. Returns { viaHost, where, hostWarning,
  // bundle }. `opts.forceDownload` (the owner's batch downloads) always takes
  // the download: a batch of hundreds lands in the staging folder, beside its
  // ledger, to be swept in by `fha capture --ingest` in chunks the owner
  // chooses, rather than filing hundreds of stubs into the inbox unasked. The
  // files are the same either way.
  async function stageBundle(bundleName, pageHtml, cap, assets, opts = {}) {
    let hostWarning = null;
    if (!opts.forceDownload && (await nativeHost.isAvailable())) {
      try {
        const hostAssets = [];
        for (const a of assets) {
          hostAssets.push({ filename: a.filename, base64: await blobToBase64(a.blob) });
        }
        const resp = await nativeHost.sendBundle({
          bundleName, pageHtml, captureJson: cap, assets: hostAssets,
        });
        return { viaHost: true, where: resp.stub || 'your archive inbox', hostWarning: null, bundle: bundleName };
      } catch (e) {
        // Fall back to the download path rather than failing the capture -
        // but say so. The human opted into the host (isAvailable was true
        // just above), so a silent downgrade would misreport where captures
        // are going for the rest of the sitting.
        hostWarning =
          "Your archive connection didn't answer (" + shortHostReason(e) +
          ') - this capture was saved to Downloads instead. Run `' +
          fhaCmd('capture --ingest') + '` to sweep it in, and `' +
          fhaCmd('capture --install-host') + '` if this keeps happening.';
      }
    }
    const result = await bundle.writeBundle({
      folder: state.folder,
      bundleName,
      pageHtml,
      assets: assets.map((a) => ({ filename: a.filename, blob: a.blob })),
      captureJson: cap,
    });
    // Where it really went, from the browser itself. Both the "staged to"
    // line and the ingest command are built from this, never from a
    // home-directory path we assumed.
    const paths = captureJson.stagedPaths(result.filePath);
    state.stagedDir = paths.staging;
    updateIngestCmd();
    return {
      viaHost: false,
      where: paths.bundle || result.dir + " (inside your browser's download folder)",
      hostWarning,
      bundle: bundleName,
    };
  }

  // ── Step 2: page-copy toggle + evidence picker ───────────────────────────────

  // A shared photo never keeps a copy of Ancestry's page: the page lists
  // the other members who saved the photo, and the archive keeps the photo
  // and its details instead (PR #258 round 4 - the owner's call, reversible).
  // Its file is always the photo, so "the page copy is the record" does not
  // apply there either.
  function pageCopyOn() {
    return !state.ancPhoto && $('cb-pagecopy').checked;
  }

  function evidenceMode() {
    if (state.ancPhoto) return 'yes';
    const r = document.querySelector('input[name="assetMode"]:checked');
    return r ? r.value : 'yes';
  }

  function evidenceUrl() {
    // On Newspapers.com the box holds a label, not an address.
    if (npActive() && $('f-asset-url').readOnly) return '';
    return ($('f-asset-url').value || '').trim();
  }

  function hasEvidence() {
    if (evidenceMode() !== 'yes') return false;
    // On an Ancestry image-viewer or an IIIF page the auto path supplies the
    // full record on Capture, so "Yes" is satisfied with no dropped file or URL.
    // A dropped file or a typed URL still takes precedence (manual override).
    if (state.ancestryViewer || state.ancRecord || state.ancPhoto || state.fsRecord || state.fsImage || state.fagRecord ||
        state.iiif) return true;
    if (npActive() && !state.droppedAsset) return state.npSelected.size > 0;
    return !!(state.droppedAsset || evidenceUrl());
  }

  // A running, plain-language summary of what the capture will contain, so the
  // human can see the "both" composition before pressing Capture.
  function updateAssetStatus() {
    const parts = [];
    // A file the page says it cannot hand over (a restricted FamilySearch
    // image): the line turns amber though Capture will still save the rest.
    let warnFile = false;
    if (pageCopyOn()) parts.push('Whole-page copy ✓');
    if (evidenceMode() === 'yes') {
      if (state.droppedAsset) {
        const note = $('f-provisional').checked ? ' (screen capture)' : '';
        parts.push('Record file: ' + state.droppedAsset.filename + note +
          (state.ancRecord ? ', with the record’s index and citation (Ancestry)'
            : state.ancPhoto ? ', as the photo, with its details (Ancestry)'
            : state.fsRecord ? ', with the record’s data and citation (FamilySearch)'
              : state.fagRecord ? ', with the memorial’s facts, citation and photos (Find a Grave)' : ''));
      } else if (state.npPage && state.npLoading) {
        parts.push('Record files: reading your Newspapers.com clippings…');
      } else if (npActive()) {
        const n = state.npSelected.size;
        parts.push(
          n
            ? 'Record files: whole page + ' + n + (n > 1 ? ' clippings, each its own source' : ' clipping') + ' (Newspapers.com, auto)'
            : 'Record files: tick at least one of your clippings'
        );
      } else if (evidenceUrl()) {
        parts.push('Record file: from page address');
      } else if (state.ancestryViewer) {
        // Auto path: the full-res record is fetched on Capture.
        parts.push('Record file: full record (Ancestry, auto)');
      } else if (state.ancRecord) {
        parts.push('Record files: the record’s index and citation + its scan, if it has one (Ancestry, auto)');
      } else if (state.ancPhoto) {
        const open = !state.photoShown || !state.photoShown.keys || state.photoShown.keys.some((k) => k !== 'title');
        parts.push('Record files: the photo + its details (Ancestry, auto)' + (open ? ''
          // Not only "closed": the Details may be open in a layout the add-on
          // does not recognise yet, where opening them again changes nothing
          // (PR #258 round 10).
          : '. The add-on couldn’t find this photo’s Details on the page (they may be closed, or Ancestry shows ' +
            'them in a way it doesn’t recognise yet). The photo and its address are saved; its date, place and ' +
            'description can be read on Ancestry from that address'));
      } else if (state.fagRecord) {
        parts.push('Record files: the memorial’s facts and citation + its photos at full size (Find a Grave, auto)');
      } else if (state.fsRecord || state.fsImage) {
        const fst = fsStatus();
        parts.push(fst.text);
        if (fst.warn) warnFile = true;
      } else if (state.iiif) {
        parts.push('Record file: full image (IIIF, auto)');
      } else {
        parts.push('Record file: add an address or drop a file');
      }
    } else if (pageCopyOn()) {
      parts.push('the page copy is the record');
    } else {
      // Pointer-only (§5.3's "none", TOOLING §13b case (c)): both controls
      // off is a deliberate, legitimate capture - citation + link, no file.
      // Ingest files it as a pointer stub flagged asset_elsewhere: true.
      parts.push('Citation + link only - no file saved (a pointer for later retrieval)');
    }
    updateAddressRow();
    renderAutoBox();
    const ok = (pageCopyOn() || evidenceMode() !== 'yes' || hasEvidence()) && !warnFile;
    setAssetStatus(
      parts.join('   ·   '),
      ok ? 'ok' : 'warn'
    );
  }

  function setAssetStatus(text, cls) {
    const el = $('asset-status');
    el.textContent = text;
    el.className = 'asset-status' + (cls ? ' ' + cls : '');
  }

  function selectEvidenceCard(mode) {
    $('asset-cards').querySelectorAll('.opt').forEach((o) =>
      o.classList.toggle('is-selected', o.dataset.mode === mode)
    );
  }

  // ── dropped-file handling ────────────────────────────────────────────────────

  function extOfFile(file) {
    const m = (file.name || '').match(/\.([a-z0-9]{1,6})$/i);
    return m ? m[1].toLowerCase() : 'bin';
  }

  function acceptDroppedFile(file) {
    state.touched.add('dropped');
    const ext = extOfFile(file);
    state.droppedAsset = { blob: file, ext, filename: file.name };
    const dz = $('dropzone');
    dz.classList.add('has-file');
    dz.textContent = file.name;
    updateAssetStatus();
  }

  function wireDropzone(el) {
    if (!el) return;
    const picker = document.createElement('input');
    picker.type = 'file';
    picker.style.display = 'none';
    document.body.appendChild(picker);

    const open = () => picker.click();
    el.addEventListener('click', open);
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        open();
      }
    });
    picker.addEventListener('change', () => {
      if (picker.files && picker.files[0]) acceptDroppedFile(picker.files[0]);
    });

    el.addEventListener('dragover', (e) => {
      e.preventDefault();
      el.classList.add('dragover');
    });
    el.addEventListener('dragleave', () => el.classList.remove('dragover'));
    el.addEventListener('drop', (e) => {
      e.preventDefault();
      el.classList.remove('dragover');
      const file = e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) acceptDroppedFile(file);
    });
  }

  function describeSize(bytes) {
    if (bytes < 1024) return bytes + ' B';
    if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' KB';
    return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  }

  // ── Step 3: assemble + stage the bundle ──────────────────────────────────────

  // `text`: the notes as they stood when Capture was pressed (formSnapshot);
  // the box itself otherwise.
  function gatherNotes(provisional, text) {
    let notes = text === undefined ? $('f-notes').value : text;
    if (!notes.trim()) notes = '';
    // schema-2 capture.json carries the provisional flag structurally (on the
    // record asset), but we ALSO surface it in the notes body - the one place
    // review always reads (§5.6 "review sees every flag") - so a flagged screen
    // capture is visible whether or not a tool honors the structured flag yet.
    if (provisional) {
      const flag = '[provisional image: a cleaner original may exist behind the paywall]';
      notes = notes ? flag + '\n\n' + notes : flag;
    }
    return notes;
  }

  function blobToBase64(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result);
        const comma = result.indexOf(',');
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = () => reject(reader.error || new Error('read failed'));
      reader.readAsDataURL(blob);
    });
  }

  // Build the page-copy asset (role `webpage`): a self-contained single-file
  // snapshot from the content script. Returns { blob, filename } or throws.
  async function buildPageCopy() {
    const resp = await sendToTab(state.tabId, { action: 'singlefile' });
    // The copy's pictures and styles are best effort, but a batch site's host
    // that said "slow down" to them still holds that site's batch back.
    for (const host of (resp && Array.isArray(resp.slowHosts) ? resp.slowHosts : [])) {
      noteSlowDown({ slowDown: true }, siteOfHost('https://' + host + '/'));
    }
    if (!resp || !resp.ok || !resp.html) {
      throw new Error((resp && resp.error) || 'could not build the page copy');
    }
    return {
      blob: new Blob([resp.html], { type: 'text/html' }),
      filename: 'page-snapshot.html',
    };
  }

  // Resolve the evidence asset (role `record`). Precedence:
  //   1. a dropped/chosen file (as-is)                          -> mode 'manual'
  //   2. a URL typed into the box (pulled in-session)            -> mode 'fetch'
  //   3. on an Ancestry image-viewer page with neither of the   -> mode 'ancestry-api'
  //      above: the SEAMLESS full-res auto path (token -> assembled JPEG, in the
  //      human's session, one image - the same file Ancestry's Download button
  //      hands back). NEVER the thumbnail.
  // Returns { blob, filename, mode } or throws (caller turns the throw into a
  // panel message and the manual paths stay available - Capture never hard-fails
  // because the auto-fetch failed).
  // Whether the page in the tab is on FamilySearch (the record the form was
  // filled for), where a refused file address can be retried through
  // FamilySearch's own image service.
  function isFamilySearchPage() {
    try {
      return /(^|\.)familysearch\.org$/i.test(new URL(state.prefilledUrl || (state.prefill && state.prefill.url) || '').hostname);
    } catch (e) {
      return false;
    }
  }

  // Whether a file address is on FamilySearch itself (https): only such an
  // address can name a FamilySearch image to try again (PR #223 audit F7).
  function onFamilySearch(href) {
    try {
      const u = new URL(href, state.prefilledUrl || undefined);
      return u.protocol === 'https:' && /(^|\.)familysearch\.org$/i.test(u.hostname);
    } catch (e) {
      return false;
    }
  }

  async function buildEvidence() {
    if (state.droppedAsset) {
      return {
        blob: state.droppedAsset.blob,
        filename: 'record.' + state.droppedAsset.ext,
        mode: 'manual',
      };
    }
    const url = evidenceUrl();
    if (!url && state.ancestryViewer) {
      const resp = await autoFetch('Ancestry full record image', { action: 'ancestryImage' });
      if (!resp.ok) {
        // Surface Ancestry's reason (not signed in, downloads disabled, shape
        // changed, thumbnail-sized) and point at the manual fallback.
        throw new Error(
          (resp.error || 'could not fetch the full record from Ancestry') +
            '. You can use Ancestry’s Download button and drop the file in.'
        );
      }
      const blob = bundle.base64ToBlob(resp.base64, resp.contentType);
      return { blob, filename: 'record.' + (resp.ext || 'jpg'), mode: 'ancestry-api' };
    }
    if (!url && state.fsImage) {
      // FamilySearch image viewer: the page draws deep-zoom tiles, so the full
      // image (dist.jpg) comes from FamilySearch's own API in her session. No
      // id is sent: the page reads the image on screen now (a single-page app).
      const imageId = (state.prefill && state.prefill.familysearchImageId) ||
        (state.fsCheck && state.fsCheck.imageId) || undefined;
      const resp = await autoFetch('FamilySearch full image' + (imageId ? ' ' + imageId : ''),
        { action: 'familysearchImage', imageId });
      if (!resp.ok) {
        throw new Error((resp.error || 'could not fetch the full image from FamilySearch') +
          '. You can use FamilySearch’s Download button and drop the file in.');
      }
      const blob = bundle.base64ToBlob(resp.base64, resp.contentType);
      return { blob, filename: 'record.' + (resp.ext || 'jpg'), mode: 'familysearch-api' };
    }
    if (!url && state.iiif) {
      // Open-standard IIIF: fetch the full image (content.js rewrites the
      // derivative's URL to full/full then full/max). No auth, so a plain fetch.
      const resp = await autoFetch('Full image (IIIF archive)', { action: 'iiifImage' });
      if (resp.ok) {
        const blob = bundle.base64ToBlob(resp.base64, resp.contentType);
        return { blob, filename: 'record.' + (resp.ext || 'jpg'), mode: 'iiif' };
      }
      // The full-image fetch failed (rewritten URL 404, CORS). Rather than lose
      // the record entirely, fall back to the visible derivative the page
      // exposed, if any, before giving up.
      const derivative = state.prefill && state.prefill.imageUrl;
      if (derivative) {
        const dResp = await autoFetch('The picture on the page (a smaller copy)',
          { action: 'fetchAsset', url: derivative });
        if (dResp.ok) {
          const blob = bundle.base64ToBlob(dResp.base64, dResp.contentType);
          return { blob, filename: 'record.' + (dResp.ext || 'jpg'), mode: 'iiif-derivative' };
        }
      }
      throw new Error(
        (resp.error || 'could not fetch the full IIIF image') + '. You can drop a file in instead.'
      );
    }
    const resp = await autoFetch('File from the page address', { action: 'fetchAsset', url });
    if (!resp.ok) {
      // On FamilySearch a file address usually refuses (the viewer's tiles do
      // not let themselves be read), but the same image comes whole through
      // FamilySearch's own image service: the one this address names, or the
      // one image the viewer shows. The login goes only to FamilySearch
      // (content.js fsAuthHeaders). The image is read from the page, so the
      // caller checks the tab is still on it (`fromPage`).
      if (isFamilySearchPage() && onFamilySearch(url)) {
        const fs = await autoFetch('The same image through FamilySearch (a second try)',
          { action: 'familysearchImage', fromUrl: url });
        if (fs.ok) {
          const blob = bundle.base64ToBlob(fs.base64, fs.contentType);
          return { blob, filename: 'record.' + (fs.ext || 'jpg'), mode: 'familysearch-api', fromPage: true };
        }
      }
      throw new Error(resp.error || 'could not pull that file from the page');
    }
    const blob = bundle.base64ToBlob(resp.base64, resp.contentType);
    return { blob, filename: 'record.' + (resp.ext || 'bin'), mode: 'fetch' };
  }

  // Compare two page addresses ignoring any #fragment: pages move the fragment
  // for in-page position without changing the record, and a staleness warning
  // on a fragment-only difference would cry wolf on a form that is fine.
  function sameRecordUrl(a, b) {
    return String(a || '').split('#')[0] === String(b || '').split('#')[0];
  }

  // Before anything is read: the details in the form must be the ones filled
  // in for the page now in the tab. If the tab has moved on (a navigation the
  // refresh missed, or one still parked behind an earlier capture), saving
  // would file one record's title, people and file with another page - so
  // nothing is saved, on every path, and the panel reads the page now in the
  // tab (capture()'s finally). Was a warning only, for the ordinary path
  // (PR #223 audit F4/F10).
  async function checkFormIsForTab(recordUrl) {
    if (!recordUrl) return; // nothing was ever read: the form is hers alone
    const tab = await getTab(state.tabId);
    if (tab && tab.url && sameRecordUrl(tab.url, recordUrl)) return;
    throw new TabMoved('the page in the tab is not the one these details were filled in for, so nothing ' +
      'was saved. The panel is reading the page in the tab now - check the details, then press Capture again');
  }

  // A capture reads the tab several times - its page, the record, the page
  // copy - while she may already be clicking on to the next record. Every
  // file in one bundle must come from the one record Capture was pressed on
  // (PR #214 review): this is asked right after the LAST read of the tab, and
  // anything that comes later (an image, the newspaper page's tiles) is
  // fetched by its own id, not by "whatever the tab shows now". `expected` is
  // the record's address; `ids` are parts its address must name (a record id
  // the page's data gave). Throws, so nothing is staged, when the tab moved.
  class TabMoved extends Error {}

  async function checkTabStillOn(expected, ids) {
    const tab = await getTab(state.tabId);
    let here = tab && tab.url ? tab.url : '';
    let want = String(expected || '');
    try {
      here = decodeURIComponent(here);
      want = decodeURIComponent(want);
    } catch (e) {
      /* compare them as they are */
    }
    const sameRecord = !!expected && !!here && sameRecordUrl(here, want);
    const named = (ids || []).every((id) => !id || want.toUpperCase().indexOf(String(id).toUpperCase()) >= 0);
    if (!sameRecord || !named) {
      throw new TabMoved('the page in the tab changed while this record was being saved, so nothing was ' +
        'saved. The panel is reading the new page now - check it is the record you mean, then press Capture again');
    }
  }

  // A record capture's question to the page in the tab. When the page goes
  // away mid-answer (she clicked on to the next record), Chrome closes the
  // message port and says so in its own words; the tab check turns that into
  // the plain "the page in the tab changed" message, nothing saved (PR #221
  // audit). A page that is still there but did not answer is { ok:false }.
  async function askTab(msg, recordUrl, ids) {
    try {
      return await sendToTab(state.tabId, msg);
    } catch (e) {
      await checkTabStillOn(recordUrl, ids);
      return { ok: false, error: 'the page did not answer (' + e.message + '). Reload it and press Capture again' };
    }
  }

  // The page copy for a record capture (PR #214 reviews). It is read from the
  // tab, so it is built before any long download whenever it is sure to be
  // kept - she asked for it, or the bundle is sure to hold no image (an index
  // entry, a household member pointing at another capture), when it is what
  // the record's data is filed with. `early` is that first chance: a copy
  // that cannot be made then fails the capture only if it is one of those.
  // `late` is the second, after the image download failed and the bundle
  // would otherwise hold the data alone: then the copy is required, and the
  // tab must still be on the record. Returns the copy, or null.
  async function recordPageCopy(when, needed, recordUrl, ids) {
    if (!needed) return null;
    let pc;
    try {
      pc = await buildPageCopy();
    } catch (e) {
      throw new Error((when === 'late'
        ? 'the record has no image, and the copy of the web page its data is filed with could not be made ('
        : 'the copy of the web page could not be made (') +
        e.message + '). Reload the page and press Capture again, or untick the page copy');
    }
    await checkTabStillOn(recordUrl, ids);
    return pc;
  }

  // The form as it stands the moment Capture is pressed. A record capture
  // waits on the page and the network for seconds; what she typed meanwhile,
  // or the next record's details arriving, must not change what this capture
  // files (#211/#213 reviews).
  function formSnapshot() {
    return {
      title: $('f-title').value.trim(),
      date: $('f-date').value.trim(),
      type: $('f-type').value,
      repo: $('f-repo').value.trim(),
      notes: $('f-notes').value,
      people: checkedPeople(),
      provisional: $('f-provisional').checked,
      dropped: state.droppedAsset,
      edited: new Set(state.userEdited),
      fsNoteLine: state.fsNoteLine,
    };
  }

  // The Capture button.
  function capture() {
    return runCapture();
  }

  // One capture of the page the form was filled in for. The Capture button
  // runs it as she set the panel; the owner's batch downloads (src/batch.js)
  // run it once per queued record with `opts.batch` = { expectKey }: the
  // page copy kept and the record's own file saved (the panel's defaults),
  // the record shown checked against the one queued before anything is
  // downloaded, and the bundle staged to the download folder. Everything
  // else - every check, every file, every word in capture.json - is this
  // one path, so a batch bundle is the bundle Capture makes.
  // Returns { ok, error?, cause?, site?, key?, bundle?, note?, slowDown?, rateHits?,
  // results? } for the batch; the button ignores it.
  async function runCapture(opts = {}) {
    // `cause`: the cause without its advice, when the text wraps one.
    const refuse = (text, cls, cause) => {
      setStageResult(text, cls || 'warn');
      return cause ? { ok: false, error: text, cause } : { ok: false, error: text };
    };
    if (!opts.batch && state.batchDriving) {
      return refuse('A batch download is moving this tab from record to record. Press Pause in the batch box, ' +
        'wait until it says it has stopped, then press Capture.');
    }
    if (state.busy) return { ok: false, error: 'a capture is already saving' };
    if (state.rereading) {
      return refuse('The panel is reading the page again - press Capture once it has finished.');
    }
    if (state.navInFlight) {
      return refuse('The page in the tab just changed - reading it now. Press Capture again in a moment.');
    }

    const wantPageCopy = opts.batch && !state.ancPhoto ? true : pageCopyOn();
    const wantEvidence = opts.batch ? true : evidenceMode() === 'yes';
    const form = formSnapshot();
    // Both controls off is NOT a refusal: it is the pointer-only capture
    // (§5.3's "none", TOOLING §13b case (c)) - page.html + an empty assets
    // list, which ingest files as a citation/link pointer stub
    // (asset_elsewhere: true). The status line above the button already says
    // "Citation + link only", so the human sees what they are staging.
    if (wantEvidence && !state.droppedAsset && state.npPage && state.npLoading) {
      // The clippings for the page now in the tab are still being read; the
      // previous page's are already forgotten (loadNewspapers), so wait.
      return refuse('Still reading your clippings on this Newspapers.com page - wait a moment, then press Capture again.');
    }
    if (wantEvidence && !state.droppedAsset && !evidenceUrl()
        && !state.ancestryViewer && !state.ancRecord && !state.ancPhoto && !state.fsRecord && !state.fsImage
        && !state.fagRecord && !state.iiif && !npActive()) {
      if (state.npPage) {
        return refuse('Newspapers.com: ' + npUnavailableReason(), null, npUnavailableCause());
      }
      return refuse('Add the file address or drop a file, or switch to "No, the page copy is the record".');
    }

    // The record this capture is of: the one the form was filled for.
    const recordUrl = state.prefilledUrl;
    const recordCapture = wantEvidence && !evidenceUrl() &&
      ((npActive() && !state.droppedAsset) || state.ancRecord || state.ancPhoto || state.fsRecord || state.fagRecord);
    // A batch saves a record page's own files only, never a file dropped
    // into the panel meanwhile or a page that is not a record.
    if (opts.batch && state.droppedAsset) {
      return refuse('a file was dropped into the panel while the batch ran, so nothing was saved. Press ↻ Re-read ' +
        'page to clear it, then press Start again', 'error');
    }
    if (opts.batch && !recordCapture) {
      return refuse('this page is not one the batch can save as the record queued, so nothing was saved', 'error');
    }
    const siteOpts = opts.batch ? { forceDownload: true, expectKey: opts.batch.expectKey } : {};

    state.busy = true;
    $('btn-capture').disabled = true;
    setRereadButtons(true); // not while this capture saves (PR #223 audit F13)
    setStageResult('Capturing…', '');
    // A fresh log for this Capture; the last saved Capture's fingerprints stay
    // to compare against (sameAsLastCapture).
    clearAutoLog();
    state.thisCaptureSigs = new Set();

    let outcome = null;
    // A slow-down already met reading this page is not forgotten here - and
    // it is counted once: this capture takes it over, so the next Capture
    // on the same page does not count it again (PR #224 second audit N1).
    state.slowDownSeen = !!state.preflightSlowDown;
    state.slowDownSites = new Set(state.preflightSlowDown && state.preflightSlowDownSite ? [state.preflightSlowDownSite] : []);
    state.preflightSlowDown = false;
    try {
      // The form's details are saved with what is read from the tab: if the
      // tab is no longer on the page they were filled in for, they would not
      // match, so stop before reading anything - on every path.
      await checkFormIsForTab(recordUrl);
      if (recordCapture) await checkTabStillOn(recordUrl);

      // page.html is always saved - grab it fresh at capture time so any
      // late-settling content is in the raw DOM the recipe re-extracts from.
      const pageResp = await sendToTab(state.tabId, { action: 'pagehtml' });
      if (!pageResp || !pageResp.ok || !pageResp.html) {
        throw new Error('could not read the page, reload it and try again');
      }
      if (recordCapture) await checkTabStillOn(recordUrl);
      else await checkFormIsForTab(recordUrl);

      // Newspapers.com: the whole page + each chosen clipping, one bundle per
      // clipping. A dropped file still wins (the manual override).
      if (wantEvidence && npActive() && !state.droppedAsset) {
        const np = await captureNewspapers(pageResp.html, wantPageCopy, recordUrl, siteOpts);
        resetForNext(np.saved > 0);
        const miss = np.results.find((r) => !r.ok);
        outcome = {
          ok: np.saved > 0, site: 'newspapers', results: np.results, rateHits: np.rateHits,
          slowDown: np.rateHits > 0, error: np.saved > 0 ? null : (miss && miss.error) || 'nothing was saved',
        };
        return outcome;
      }

      // Ancestry record page: the record's own index and citation, plus its
      // linked scan - or the file she dropped in, saved with the index. A typed
      // file address still takes the ordinary path.
      if (wantEvidence && state.ancRecord && !evidenceUrl()) {
        const got = await captureAncestryRecord(pageResp.html, wantPageCopy, form, recordUrl, siteOpts);
        resetForNext();
        outcome = Object.assign({ ok: true, site: 'ancestry' }, got);
        return outcome;
      }

      // A photo someone shared on Ancestry: the photo and its Details, filed
      // as a photo - or the file she dropped in, saved as the photo.
      if (wantEvidence && state.ancPhoto && !evidenceUrl()) {
        const got = await captureAncestryPhoto(pageResp.html, form, recordUrl, siteOpts);
        if (got.staged === false) {
          // More on the page than the form showed: nothing staged, her
          // dropped file and form kept for the Capture she presses next.
          outcome = { ok: false, error: 'more details on the page to check first' };
          return outcome;
        }
        resetForNext();
        outcome = Object.assign({ ok: true, site: null }, got);
        return outcome;
      }

      // FamilySearch record (or the viewer opened from one): FamilySearch's
      // own data and citation, plus its image - or the file she dropped in,
      // saved with the data.
      if (wantEvidence && state.fsRecord && !evidenceUrl()) {
        const got = await captureFamilySearchRecord(pageResp.html, wantPageCopy, form, recordUrl, siteOpts);
        resetForNext();
        outcome = Object.assign({ ok: true, site: 'familysearch' }, got);
        return outcome;
      }

      // Find a Grave memorial: what the memorial says and its citation, plus
      // its photos at full size (and a file she dropped in, saved with them).
      if (wantEvidence && state.fagRecord && !evidenceUrl()) {
        const got = await captureFindAGraveMemorial(pageResp.html, wantPageCopy, form, recordUrl, siteOpts);
        resetForNext();
        outcome = Object.assign({ ok: true, site: 'findagrave' }, got);
        return outcome;
      }

      // The human's screen-capture flag rides with a file they provided by
      // hand (the drop path, mode 'manual'). The url and auto paths (fetch /
      // ancestry-api / iiif) pull the page's own original, never a screenshot,
      // so gating on the dropped file keeps a stray tick from mislabeling a
      // pristine fetched record as provisional.
      const provisional =
        wantEvidence && !!state.droppedAsset && $('f-provisional').checked;

      // Compose the asset list (the "both" case): the page copy and/or the
      // record evidence. Each entry carries its role so ingest files it right.
      const assets = []; // { filename, blob, role, mode, provisional }
      // The page these files come from, as the browser shows it now (the one
      // the form was filled in for: checkFormIsForTab above).
      const startTab = await getTab(state.tabId);
      const pageUrl = startTab && startTab.url;
      if (wantPageCopy) {
        const pc = await buildPageCopy();
        assets.push({
          filename: pc.filename, blob: pc.blob,
          role: 'webpage', mode: 'singlefile',
        });
        await checkTabStillOn(pageUrl);
      }
      let evidenceWarning = null;
      if (wantEvidence) {
        // An auto fetch path (no dropped file, no typed URL, on an Ancestry
        // viewer or an IIIF page) may legitimately fail - not signed in,
        // downloads disabled, API/IIIF shape changed. That must NEVER hard-fail
        // the capture: if a page copy was made, stage it and report the miss so
        // the human can grab the file by hand. The explicit manual paths
        // (dropped file / typed URL) still fail loudly - those are user choices.
        const autoOnly =
          (state.ancestryViewer || state.fsImage || state.iiif) && !state.droppedAsset && !evidenceUrl();
        if (autoOnly && wantPageCopy) {
          try {
            const ev = await buildEvidence();
            await checkTabStillOn(pageUrl);
            assets.push({
              filename: ev.filename, blob: ev.blob,
              role: 'record', mode: ev.mode,
              provisional,
            });
          } catch (e) {
            if (e instanceof TabMoved) throw e; // never another page's file
            evidenceWarning = e.message;
          }
        } else {
          const ev = await buildEvidence();
          // An automatic file is read from the page itself (the FamilySearch
          // retry of a refused address too): it must still be this page.
          if (autoOnly || ev.fromPage) await checkTabStillOn(pageUrl);
          assets.push({
            filename: ev.filename, blob: ev.blob,
            role: 'record', mode: ev.mode || (state.droppedAsset ? 'manual' : 'fetch'),
            provisional,
          });
        }
      }

      const title = $('f-title').value.trim();
      const fields = {
        url: $('f-url').value.trim() || (state.prefill && state.prefill.url),
        title,
        accessed: captureJson.accessedDate(),
        sourceDate: $('f-date').value.trim(),
        sourceType: $('f-type').value,
        repository: $('f-repo').value.trim(),
        people: checkedPeople(),
        // The panel's own "image not saved" line goes when the image came
        // through after all (restrictedImageNote).
        notes: gatherNotes(provisional, assets.some((a) => a.role === 'record')
          ? withoutLine($('f-notes').value, state.fsNoteLine) : undefined),
        recipeHint: state.prefill && state.prefill.recipeHint,
        assets: assets.map((a) => ({
          file: a.filename, role: a.role, mode: a.mode,
          provisional: !!a.provisional,
        })),
      };
      const cap = captureJson.build(fields);
      const bundleName = captureJson.bundleName(title || (cap.url || 'capture'));

      // Seamless path (§5.7) when the human opted in and a host answers; else the
      // honest staging-folder download (§5.1). The extension never claims it
      // reached the archive when it only reached Downloads.
      const st = await stageBundle(bundleName, pageResp.html, cap, assets);
      reportStaged(st.where, st.viaHost, evidenceWarning, st.hostWarning);

      resetForNext();
      outcome = { ok: true, site: null, bundle: st.bundle, where: st.where };
    } catch (e) {
      setStageResult('Could not capture: ' + e.message, 'error');
      // Whatever was fetched, nothing was saved: the box must not say so.
      state.autoSaved = false;
      // The form still describes the record she left: read the new one.
      if (e instanceof TabMoved) state.pendingNav = true;
      noteSlowDown(e, null);
      outcome = { ok: false, error: e.message || String(e), tabMoved: e instanceof TabMoved,
        rateHits: (e && e.rateHits) || 0 };
    } finally {
      // Saved or not, a capture that met a "slow down" says so (the batch's
      // break before its next request).
      if (outcome && state.slowDownSeen) outcome.slowDown = true;
      if (outcome) outcome.slowDownSites = [...state.slowDownSites];
      // A record saved by hand goes in the batch ledger too (switched on
      // only), so a batch never downloads it again.
      if (!opts.batch && state.batch && outcome && outcome.ok && outcome.site) {
        state.batch.recordManual(outcome).catch(() => {});
      }
      // A Capture by hand that met a "slow down", saved or not, holds back
      // the batch of the site whose host said it - and no other (PR #224
      // audit F3, second audit N4).
      if (!opts.batch && state.batch && outcome) {
        for (const site of state.slowDownSites) state.batch.recordSlowDown(site).catch(() => {});
      }
      state.busy = false;
      $('btn-capture').disabled = state.batchDriving;
      setRereadButtons(state.batchDriving);
      // An automatic file that did not come: from now on she may also give
      // its address (updateAddressRow), as well as drop it in.
      state.autoMissed = !!(state.autoOutcome && state.autoOutcome.failed &&
        (state.ancestryViewer || state.fsImage || state.iiif));
      updateAddressRow();
      renderAutoBox();
      // Replay a navigation that arrived mid-capture (parked by
      // refreshOnNavigation) so the form moves on to the record now in the
      // tab instead of silently staying on the one just staged.
      if (state.pendingNav) {
        state.pendingNav = false;
        refreshOnNavigation(state.tabId, { status: 'complete' });
      }
    }
    return outcome;
  }

  // Boil a native-messaging failure down to one plain phrase for the fallback
  // warn line. Chrome's raw errors are developer-speak ("Specified native
  // messaging host not found.", "Error when communicating with the native
  // messaging host."); match the known shapes conservatively and fall back to
  // the raw text, shortened, so an unmapped reason is still visible.
  function shortHostReason(err) {
    const raw = err && err.message ? String(err.message) : '';
    const m = raw.toLowerCase();
    if (m.includes('not found') || m.includes('not registered') || m.includes('forbidden')) {
      return 'host not found';
    }
    if (m.includes('communicating') || m.includes('disconnected')
        || m.includes('exited') || m.includes('no response')) {
      return 'no reply';
    }
    if (m.includes('message length') || m.includes('exceed') || m.includes('too large')) {
      return 'bundle too large';
    }
    if (!raw) return 'no reply';
    return raw.length > 80 ? raw.slice(0, 77) + '…' : raw;
  }

  function reportStaged(where, viaHost, evidenceWarning, hostWarning) {
    // When the Ancestry auto-fetch missed but the page copy still staged, append
    // the reason so the human knows to grab the file manually - the capture
    // succeeded (it is not lost), it just doesn't yet carry the record image.
    // A native-host fallback (hostWarning) rides the same slot: the capture is
    // safe in Downloads, and the line says why it is not in the archive inbox.
    let suffix = evidenceWarning
      ? '\nThe full record image was not saved: ' + evidenceWarning
      : '';
    if (hostWarning) suffix += '\n' + hostWarning;
    const cls = evidenceWarning || hostWarning ? 'warn' : 'ok';
    if (viaHost) {
      setStageResult('Filed straight into your archive: ' + where + suffix, cls);
      $('handoff').classList.remove('show');
      return;
    }
    // Be exact about where it went, and reveal the handoff card with the
    // copyable ingest command (§5.1): the panel never pretends Downloads is the
    // archive. `where` is the browser's own absolute path when it reported one,
    // and the Downloads-relative folder named as such when it did not - the
    // panel does not fill the gap with a guessed home directory.
    setStageResult('Staged to ' + where + suffix, cls);
    $('handoff').classList.add('show');
  }

  function setStageResult(text, cls) {
    const el = $('stage-result');
    el.textContent = text;
    el.className = 'stage-result' + (cls ? ' ' + cls : '');
  }

  // Clear the evidence picks that belong to ONE record: the dropped file, the
  // dropzone's visual, and the screen-capture flag riding on that file. Runs
  // when a bundle stages (resetForNext) AND when the form re-prefills for a
  // different record (applyPrefill), so record A's dropped file - or its
  // provisional tick - can never carry over and stage as record B's evidence.
  function clearEvidenceSelection() {
    state.droppedAsset = null;
    const drop = $('dropzone');
    if (drop) {
      drop.classList.remove('has-file');
      drop.textContent = 'Drop a file here, or click to choose';
    }
    // The screen-capture flag describes the current record's dropped file
    // only, never any other record's - it must not stick across records (and
    // it is never persisted).
    $('f-provisional').checked = false;
  }

  // `saved`: whether this capture staged anything (a Newspapers.com run can
  // fail for every clipping); the automatic-saving box says "saved" only then.
  function resetForNext(saved = true) {
    state.autoSaved = saved;
    // Batch capture is the natural mode (§5.3): a research sitting yields a dozen
    // bundles. Clear the evidence so the next page starts fresh, but leave the
    // panel open and the settings intact. The form metadata (title/date/people/
    // repo) is refreshed when the human navigates to the next record - see
    // refreshOnNavigation - so it never carries one record's details onto the next.
    clearEvidenceSelection();
    // Her corrections stay: they belong to the RECORD, not to one capture of
    // it, and the panel itself may send her back ("drop the image in and press
    // Capture again"). They are cleared when the record changes (applyPrefill)
    // or the ticked clippings change (renderNpClips, the retry below) - PR
    // #214 review, keeping #211's clipping rule.
    // The files this Capture saved are now the ones the next is compared to.
    if (state.thisCaptureSigs.size) state.lastCaptureSigs = state.thisCaptureSigs;
    state.thisCaptureSigs = new Set();
    updateAssetStatus();
  }

  // Re-pull the pre-fill when the panel's tab navigates to a NEW record, so a
  // batch session never files the next page under the previous record's
  // title/date/people/repo. Fires on a finished load ('complete') AND on a bare
  // URL change: single-page viewers (Ancestry's next-image arrows) navigate
  // with history.pushState and never reach 'complete', so without the url
  // trigger the form silently goes stale mid-batch. A url change that is part
  // of a full page load (status 'loading') is skipped - its own 'complete'
  // follows once the DOM has settled. A navigation during a capture is parked,
  // not dropped: capture()'s finally block replays it, so the form catches up
  // the moment the capture lands. Skips same-record updates - including
  // fragment-only moves, which change the #position, not the record; an
  // unreadable new page is left for the human to fill, not dead-ended.
  async function refreshOnNavigation(tabId, changeInfo) {
    if (tabId !== state.tabId) return;
    // A batch download is moving this tab: it reads each page itself
    // (batchReadPage), once the page has loaded and settled.
    if (state.batchDriving) return;
    const navigated =
      changeInfo.status === 'complete' ||
      (!!changeInfo.url && changeInfo.status !== 'loading');
    if (!navigated) return;
    if (state.busy) {
      // Refreshing now would swap the form out from under the bundle being
      // staged; park the event for the replay in capture()'s finally block.
      state.pendingNav = true;
      return;
    }
    // From here until the new page is read, Capture waits (capture() checks
    // navInFlight). And after every wait below, a capture that started
    // anyway parks this refresh instead of rewriting its form (#211 audit).
    const seq = (state.navSeq = (state.navSeq || 0) + 1);
    state.navInFlight = true;
    const parked = () => {
      if (!state.busy) return false;
      state.pendingNav = true;
      return true;
    };
    try {
      const tab = await getTab(tabId);
      if (!tab || !/^https?:/i.test(tab.url || '') || parked()) return;
      // A Re-read may have moved the panel to another tab while this waited:
      // then this tab's navigation is no longer the form's business, and must
      // not take the form over from the Re-read (Codex 4502593873).
      if (tabId !== state.tabId) return;
      // Same-record compare, not exact compare: in-page viewers (BookReader's
      // #page arrows) move only the #fragment without changing the record, and
      // a re-prefill on that wipes the human's typed notes/people for nothing.
      // Capture's check of the form against the tab compares this way too.
      // ...unless the last read of this address failed (a page still loading
      // when it was asked): then its next event reads it again, keeping what
      // she has typed into the empty form meanwhile (Codex 4502752672).
      const retry = sameRecordUrl(tab.url, state.prefilledUrl) && state.prefillFailed;
      if (sameRecordUrl(tab.url, state.prefilledUrl) && !retry) return;
      // From here this read owns the form, until a newer read (a later
      // navigation, or a Re-read button) takes it over.
      const read = beginRead();
      let resp = null;
      try {
        await injectContent(tabId);
        resp = await sendToTab(tabId, { action: 'prefill' });
      } catch (e) {
        resp = null; // a restricted or still-loading page: said below
      }
      if (parked()) return;
      // A newer read owns the form (a later navigation, or a Re-read that
      // may have moved the panel to another tab): this page's answer is old.
      if (!ownsForm(read, tabId)) return;
      // The tab is on a new record: what the form held was the old one's, so
      // a page that cannot be read gets an empty form for its own address,
      // never the last record's details (the next 'complete' may still read it).
      finishRead(resp, tab.url, retry);
    } finally {
      if (seq === state.navSeq) state.navInFlight = false;
    }
  }

  // ── the Re-read buttons ──────────────────────────────────────────────────────
  // The panel stays on the tab it was opened on, whatever tab she looks at
  // next, so nothing changes under her while she types: it moves to another
  // tab only when she presses Re-read page. (The owner's rule since 0.6.6,
  // after one record was captured three times while other tabs were in
  // front.) The refresh on navigation above still follows the panel's own
  // tab to its next record.

  const REREAD_HOLD_MS = 500;

  // Reads of the page that will refill the form are numbered; only the newest
  // may fill it, and only while the panel is still on the tab it read. (Kept
  // apart from navSeq, which says whether a navigation is still being read:
  // a "page finished loading" event for the address already in the form
  // reads nothing, and must not cancel a Re-read under way.)
  function beginRead() {
    state.readSeq = (state.readSeq || 0) + 1;
    return state.readSeq;
  }
  function ownsForm(read, tabId) {
    return read === state.readSeq && tabId === state.tabId;
  }

  // Keep "Reading the page again…" up for a moment, so a fast re-read is
  // still seen to happen.
  function holdRereading(t0) {
    const left = REREAD_HOLD_MS - (Date.now() - t0);
    return left > 0 ? new Promise((r) => setTimeout(r, left)) : Promise.resolve();
  }

  function setRereadButtons(off) {
    for (const id of ['btn-rescrape', 'btn-rescrape-file']) $(id).disabled = off;
  }

  function setRereading(on) {
    state.rereading = on;
    setRereadButtons(on);
    renderAutoBox();
  }

  // Every read that owns the form ends here, whoever started it (the panel
  // opening, a navigation, Re-read page), so the screen is always left
  // usable: filled from the page, or emptied for it with the reason said,
  // and Capture ready (PR #223 audit F1).
  function finishRead(resp, url, retry) {
    if (resp && resp.ok) {
      applyPrefill(resp.prefill, retry && sameRecordUrl(resp.prefill.url, state.prefilledUrl));
    } else {
      // A new record: the previous one's details are gone at once. The same
      // page again (a retry while it loads, the panel's first read, a Re-read
      // that already started clean): what she has typed for it stays, never
      // erased by a read that did not work (Codex 4502845504).
      if (state.prefilledUrl && !sameRecordUrl(url, state.prefilledUrl)) {
        clearDetails(url);
        forgetFileState();
        clearAutoLog();
      } else {
        state.prefilledUrl = url || null;
        if (!state.touched.has('f-url')) $('f-url').value = url || '';
      }
      // This address is read again on its next event (refreshOnNavigation).
      state.prefillFailed = true;
      const banner = $('recipe-banner');
      banner.textContent = 'Couldn’t read this page automatically. Type the details below, or reload the page ' +
        'and press ↻ Re-read page.';
      banner.classList.remove('recipe');
      banner.classList.add('warn');
    }
    if (!state.busy) $('btn-capture').disabled = state.batchDriving;
    updateAssetStatus();
  }


  // A Re-read button pressed while a capture saves: say why nothing happens.
  function rereadWhileBusy() {
    // A batch download is moving this tab: a Re-read would move the panel
    // out from under it.
    if (state.batchDriving) {
      setStageResult('A batch download is moving this tab - press Pause in the batch box first.', 'warn');
      return true;
    }
    if (!state.busy) return false;
    setStageResult('Saving the capture - press Re-read once it has finished.', 'warn');
    return true;
  }

  // A Re-read takes the form over from a navigation still being read: that
  // read is dropped when it lands (ownsForm), so it must not keep Capture
  // waiting for it either (PR #223 audit F12).
  function releaseNavigation() {
    state.navSeq = (state.navSeq || 0) + 1;
    state.navInFlight = false;
  }

  // Empty the record's details - what she typed with them - before a fresh
  // read, so that if the page then cannot be read, no detail of the previous
  // record is left to be filed with this one. The page address stays: it is
  // the tab's own, and Capture needs it.
  function clearDetails(url) {
    state.userEdited = new Set();
    state.prefill = null;
    state.prefilledUrl = url || null;
    state.prefillFailed = false; // finishRead marks a failed read after this
    state.touched = new Set();
    for (const id of ['f-title', 'f-date', 'f-repo']) {
      $(id).value = '';
      $(id).readOnly = false;
    }
    $('f-type').value = '';
    $('f-notes').value = '';
    state.fsNoteLine = null;
    $('f-url').value = url || '';
    renderPeople([]);
  }

  // "Re-read page": start again from the page in front of her. Everything
  // goes - the details, what she typed, the record file, the last Capture's
  // log - and is filled in from that page, exactly as if she had closed the
  // panel and opened it there. It is the one way the panel moves to another
  // tab, and the way to start clean on a viewer that changes record without
  // changing its address. Not while a capture is saving.
  async function rescrape() {
    if (rereadWhileBusy() || state.rereading) return;
    const active = await queryActiveTab();
    if (rereadWhileBusy() || state.rereading) return;
    const banner = $('recipe-banner');
    if (!active || !active.id || !/^https?:/i.test(active.url || '')) {
      // Nothing there to read: stay on the page the form describes.
      banner.textContent = 'The tab in front of you is not a web page the panel can read, so the panel stays on ' +
        'the page it was reading. Click the record’s tab, then press ↻ Re-read page.';
      banner.classList.remove('recipe');
      banner.classList.add('warn');
      return;
    }
    // This read owns the form from now: a navigation read still on its way
    // for the old tab (or this one) is dropped when it lands.
    const read = beginRead();
    releaseNavigation();
    const tabId = active.id;
    state.tabId = tabId;
    state.pendingNav = false;
    setRereading(true);
    clearDetails(active.url);
    forgetFileState();
    clearAutoLog();
    setStageResult('', '');
    $('handoff').classList.remove('show');
    selectEvidenceMode($('f-default-evidence').value || 'yes');
    banner.textContent = 'Reading the page…';
    banner.classList.remove('warn', 'recipe');
    const t0 = Date.now();
    let resp = null;
    try {
      await injectContent(tabId);
      resp = await sendToTab(tabId, { action: 'prefill' });
    } catch (e) {
      resp = null;
    }
    await holdRereading(t0);
    setRereading(false);
    // A newer read took over (a navigation of this tab): it fills the form
    // and leaves the screen usable (finishRead).
    if (!ownsForm(read, tabId)) return;
    finishRead(resp, active.url);
  }

  // "Re-read file": look at the page again for its record file only - the
  // automatic path and the best-guess address - for a viewer that pages
  // through images while the record stays the same, or a page whose image
  // arrived late. A file dropped in for the previous image is let go. The
  // title, date, kind, people and notes stay exactly as she has them, so it
  // works only on the page the form describes: on any other tab it points
  // her to Re-read page instead of pairing this record's details with
  // another page's file.
  async function rescrapeFile() {
    if (rereadWhileBusy() || state.rereading) return;
    const active = await queryActiveTab();
    if (rereadWhileBusy() || state.rereading) return;
    const tabId = state.tabId;
    if (!tabId || !active || active.id !== tabId || !state.prefilledUrl ||
        !sameRecordUrl(active.url, state.prefilledUrl)) {
      setStageResult('The page in front of you is not the one these details describe. Press ↻ Re-read page ' +
        'to start on it.', 'warn');
      return;
    }
    const read = beginRead();
    releaseNavigation();
    const recordUrl = state.prefilledUrl;
    // Her ticks on this Newspapers.com page survive the re-read (F2).
    const keepNp = state.np ? { pageId: state.np.pageId, selected: new Set(state.npSelected) } : null;
    setRereading(true);
    forgetFileState();
    clearAutoLog();
    setStageResult('', '');
    const t0 = Date.now();
    let resp = null;
    try {
      await injectContent(tabId);
      resp = await sendToTab(tabId, { action: 'prefill' });
    } catch (e) {
      resp = null;
    }
    await holdRereading(t0);
    if (!ownsForm(read, tabId)) {
      setRereading(false); // a newer read took over
      return;
    }
    setRereading(false);
    // Read from a different address: the tab has moved on to another record,
    // and its own refresh fills the whole form; this file is not this record's.
    if (resp && resp.ok && sameRecordUrl(resp.prefill.url, recordUrl)) {
      // Only the file half of the prefill moves on; the record half (and
      // prefilledUrl, the record the form is for) stays with the form.
      const half = {};
      for (const k of FILE_PREFILL_KEYS) half[k] = resp.prefill[k];
      state.prefill = Object.assign({}, state.prefill || { url: recordUrl }, half);
      applyFilePrefill(state.prefill, keepNp);
      // The page was read: a later event for it is no retry (PR #223 delta D4).
      state.prefillFailed = false;
    } else {
      setStageResult('Couldn’t read the page’s file. Drop the file in, or reload the page and press ↻ Re-read page.',
        'warn');
    }
    updateAssetStatus();
  }

  // ── the owner's batch downloads (switched off unless told) ───────────────────
  // src/batch.js drives this tab from record to record and presses this
  // panel's own Capture for each (runCapture). It is handed these few hooks,
  // and only when this browser has batch downloads switched on (init).

  // Read the page now in the tab into the form, exactly as a navigation does
  // (beginRead → prefill → finishRead), then wait for the answers still on
  // their way (its newspaper clippings, its FamilySearch image check).
  async function batchReadPage() {
    const tabId = state.tabId;
    const read = beginRead();
    releaseNavigation();
    const tab = await getTab(tabId);
    if (!tab) return { ok: false, error: 'the batch’s tab was closed' };
    let resp = null;
    try {
      await injectContent(tabId);
      resp = await sendToTab(tabId, { action: 'prefill' });
    } catch (e) {
      resp = null;
    }
    if (!ownsForm(read, tabId)) return { ok: false, error: 'another read of the page took over the panel' };
    setStageResult('', '');
    finishRead(resp, tab.url);
    for (const pending of [state.npReading, state.fsChecking]) {
      try {
        await pending;
      } catch (e) {
        /* said in the panel; Capture then refuses or saves what it can */
      }
    }
    // The batch takes its break on a page-read slow-down, and so counts it:
    // it is not counted again by this page's Capture (second audit N1).
    const slowDown = !!state.preflightSlowDown;
    state.preflightSlowDown = false;
    return resp && resp.ok
      ? { ok: true, prefill: resp.prefill, url: tab.url, slowDown }
      : { ok: false, error: 'could not read the page' };
  }

  function batchHooks() {
    return {
      state, getTab, sendToTab, injectContent, queryActiveTab, seenItHeld, describeSameImage,
      readPage: batchReadPage,
      capture: (expectKey) => runCapture({ batch: { expectKey } }),
      folder: () => state.folder,
      setDriving(on) {
        state.batchDriving = !!on;
        $('btn-capture').disabled = !!on || state.busy;
        setRereadButtons(!!on || state.busy);
      },
    };
  }

  // ── settings (chrome.storage) ────────────────────────────────────────────────

  function loadSettings() {
    return new Promise((resolve) => {
      chrome.storage.local.get(
        {
          captureFolder: captureJson.DEFAULT_FOLDER,
          defaultEvidence: 'yes',
          pageCopyDefault: true,
          // The owner's batch downloads: shown only when this is exactly
          // true (src/batch.js says how it is set). There is no control for
          // it in the panel.
          batchDownloads: false,
        },
        (cfg) => {
          // Sanitize on the way in too: a value stored by an older build (or
          // edited by hand) must not carry an escape the API rejects later.
          state.folder = captureJson.sanitizeFolder(cfg.captureFolder);
          $('f-folder').value = state.folder;
          $('f-default-evidence').value = cfg.defaultEvidence || 'yes';
          $('cb-pagecopy').checked = cfg.pageCopyDefault !== false;
          updateIngestCmd();
          resolve(cfg);
        }
      );
    });
  }

  // Keep the handoff card's copyable command pointing where captures actually
  // stage. The DIR argument is filled in only once a capture has told us the
  // real folder (the download directory is the browser's own setting, which no
  // home-relative path can stand in for); until then the bare command stands
  // and the hint line says what the human can check. Called on settings load,
  // on a folder change, and after every staged bundle.
  function updateIngestCmd() {
    $('cmd-text').textContent = captureJson.ingestCommand(state.stagedDir);
    const hint = $('cmd-hint');
    if (hint) hint.textContent = captureJson.ingestHint(state.folder, state.stagedDir);
  }

  // Every `fha` command this panel shows carries the launcher prefix the
  // human's machine needs. `fha` is a launcher file at the archive root, not a
  // program on PATH, so a bare `fha capture --ingest` is a command-not-found
  // for everyone outside the Windows Command Prompt - see capture-json.js
  // `launcher`. This is the one place the panel builds such a string.
  function fhaCmd(rest) {
    return captureJson.launcher() + ' ' + rest;
  }

  // Fill the settings drawer's two static command samples. They are markup
  // rather than generated text, so they need one pass at init; the hint that
  // later replaces `seamless-hint` wholesale builds its own via fhaCmd.
  function fillStaticCmds() {
    const host = $('cmd-install-host');
    if (host) host.textContent = fhaCmd('capture --install-host');
    const ingest = $('cmd-ingest-plain');
    if (ingest) ingest.textContent = fhaCmd('capture --ingest');
  }

  function wireSettings() {
    $('f-folder').addEventListener('change', () => {
      state.folder = captureJson.sanitizeFolder($('f-folder').value);
      $('f-folder').value = state.folder; // reflect the sanitized form back
      chrome.storage.local.set({ captureFolder: state.folder });
      // A path learned from an earlier capture describes the OLD folder, so it
      // stops being the truth the moment the setting changes.
      state.stagedDir = '';
      updateIngestCmd();
    });
    $('f-default-evidence').addEventListener('change', () => {
      chrome.storage.local.set({ defaultEvidence: $('f-default-evidence').value });
    });
    // The seamless "file straight into my archive" opt-in. Ticking it requests
    // the optional nativeMessaging permission from this click (a user gesture, as
    // Chrome requires); declining or an absent host snaps it back off.
    const seamless = $('cb-seamless');
    if (seamless) {
      seamless.addEventListener('change', async () => {
        if (!seamless.checked) {
          await nativeHost.removePermission();
          setSeamlessHint('Off - captures stage to Downloads for `'
            + fhaCmd('capture --ingest') + '`.');
          return;
        }
        const granted = await nativeHost.requestPermission();
        if (!granted) {
          seamless.checked = false;
          setSeamlessHint('Permission declined - captures stage to Downloads.');
          return;
        }
        const ready = await nativeHost.isAvailable();
        setSeamlessHint(ready
          ? 'On - captures file straight into your archive inbox.'
          : 'Permission granted, but no capture host answered. Run `'
            + fhaCmd('capture --install-host')
            + '` in your archive, then reopen this panel.');
      });
    }
  }

  function setSeamlessHint(text) {
    const el = $('seamless-hint');
    if (el) el.textContent = text;
  }

  function selectEvidenceMode(mode) {
    const radio = document.querySelector(
      'input[name="assetMode"][value="' + mode + '"]'
    );
    if (radio) radio.checked = true;
    selectEvidenceCard(mode);
  }

  function copyCmd(btn) {
    const text = $('cmd-text').textContent;
    if (navigator.clipboard) navigator.clipboard.writeText(text);
    const old = btn.textContent;
    btn.textContent = 'Copied!';
    setTimeout(() => (btn.textContent = old), 1400);
  }

  // ── init ─────────────────────────────────────────────────────────────────────

  function wireEvents() {
    $('cb-pagecopy').addEventListener('change', () => {
      $('page-copy-card').classList.toggle('unchecked', !pageCopyOn());
      chrome.storage.local.set({ pageCopyDefault: $('cb-pagecopy').checked });
      updateAssetStatus();
    });
    document.querySelectorAll('input[name="assetMode"]').forEach((r) =>
      r.addEventListener('change', () => {
        selectEvidenceCard(evidenceMode());
        updateAssetStatus();
      })
    );
    $('f-asset-url').addEventListener('input', () => {
      state.touched.add('f-asset-url');
      updateAssetStatus();
    });
    $('f-notes').addEventListener('input', () => state.touched.add('f-notes'));
    // Remember which fields the human typed in, so newspaper data arriving
    // later never overwrites a correction (state.userEdited, #211 review).
    for (const id of ['f-title', 'f-date', 'f-repo', 'f-url', 'f-type']) {
      $(id).addEventListener('input', () => { state.userEdited.add(id); state.touched.add(id); });
      $(id).addEventListener('change', () => { state.userEdited.add(id); state.touched.add(id); });
    }
    $('f-provisional').addEventListener('change', updateAssetStatus);
    $('btn-capture').addEventListener('click', capture);
    $('btn-rescrape').addEventListener('click', rescrape);
    $('btn-rescrape-file').addEventListener('click', rescrapeFile);
    $('btn-copy-cmd').addEventListener('click', () => copyCmd($('btn-copy-cmd')));
    $('btn-add-person').addEventListener('click', () => {
      const name = $('f-add-person').value.trim();
      if (name) {
        state.touched.add('people');
        addPersonRow(name, true);
        $('f-add-person').value = '';
      }
    });
    $('f-add-person').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') $('btn-add-person').click();
    });
    wireDropzone($('dropzone'));
  }

  async function init() {
    populateTypeSelect();
    fillStaticCmds();
    wireEvents();
    wireSettings();
    const cfg = await loadSettings();
    $('page-copy-card').classList.toggle('unchecked', !pageCopyOn());
    // Switched off (the default, and every ordinary install): no batch box,
    // no batch ledger read, nothing of src/batch.js runs.
    if (cfg && cfg.batchDownloads === true && FHA.batchPanel) {
      state.batch = FHA.batchPanel.install(batchHooks());
    }

    // Reflect whether the seamless permission is already granted, and refresh the
    // pre-fill whenever the panel's tab navigates to the next record (batch mode).
    if ($('cb-seamless') && nativeHost.hasPermission) {
      $('cb-seamless').checked = await nativeHost.hasPermission();
    }
    if (chrome.tabs && chrome.tabs.onUpdated) {
      chrome.tabs.onUpdated.addListener(refreshOnNavigation);
    }

    const tab = await queryActiveTab();
    if (!tab || !tab.id || !/^https?:/i.test(tab.url || '')) {
      $('recipe-banner').textContent =
        'Open a record page (an http/https site) to capture it.';
      $('btn-capture').disabled = true;
      return;
    }
    state.tabId = tab.id;

    // The panel's first read owns the form like any other: if she presses
    // Re-read page before this slow page answers, the Re-read's page wins and
    // this answer is dropped (PR #223 audit F4/F10).
    const read = beginRead();
    let resp = null;
    try {
      await injectContent(tab.id);
      resp = await sendToTab(tab.id, { action: 'prefill' });
    } catch (e) {
      // A restricted page (chrome://, the web store) or a page that hasn't
      // finished loading. Don't dead-end: explain and let the human still type.
      resp = null;
    }
    if (!ownsForm(read, tab.id)) return; // a Re-read took over, choices and all
    finishRead(resp, tab.url);
    // Honor the saved default evidence option as the initial selection.
    selectEvidenceMode((cfg && cfg.defaultEvidence) || 'yes');
    updateAssetStatus();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
