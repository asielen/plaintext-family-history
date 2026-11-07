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

  function applyPrefill(prefill) {
    // A prefill for a DIFFERENT record invalidates the previous record's
    // evidence picks: without this, a file dropped for record A (and its
    // screen-capture tick) would survive the navigation and stage as record
    // B's evidence. Compare before prefilledUrl is overwritten below.
    // Fragment-only moves never reach here (refreshOnNavigation skips them),
    // so this fires only on a genuine new record - and on the first prefill,
    // where it is a harmless no-op on the fresh form.
    if (!sameRecordUrl(prefill.url, state.prefilledUrl)) clearEvidenceSelection();
    state.userEdited = new Set();
    for (const id of ['f-title', 'f-date', 'f-repo']) $(id).readOnly = false;
    state.prefill = prefill;
    state.iiif = !!prefill.iiif;
    state.ancRecord = !!prefill.ancestryRecord;
    state.fsRecord = !!prefill.familysearchRecord;
    state.fsImage = !!prefill.familysearchImageViewer;
    state.fagRecord = !!prefill.findagraveMemorial;
    state.prefilledUrl = prefill.url || null;
    $('f-title').value = prefill.title || '';
    $('f-date').value = prefill.sourceDate || '';
    $('f-repo').value = prefill.repository || '';
    $('f-url').value = prefill.url || '';
    // Reset the per-record fields the prefill doesn't otherwise set, so a batch
    // re-prefill (on navigation) never carries the previous record's notes or
    // chosen kind onto the next page. (Auto-detect = blank kind.)
    $('f-notes').value = '';
    $('f-type').value = prefill.sourceType || '';
    renderPeople(prefill.people);
    // Pre-fill the evidence url box with the best-guess image/PDF the page
    // exposed - EXCEPT where the visible image is a downsized derivative and an
    // auto path can fetch the real file: an Ancestry image-viewer page (the EX7
    // 507x600 thumbnail trap) or an open-archive IIIF page (the rendered <img>
    // is a derivative). There the URL box is left empty and the auto path
    // supplies the full record on Capture. Any other page clears a stale URL so
    // a previous record's address can't be re-fetched as this one's evidence.
    if (prefill.ancestryImageViewer || prefill.ancestryRecord || prefill.familysearchRecord ||
        prefill.familysearchImageViewer || prefill.findagraveMemorial || prefill.iiif || prefill.newspapers) {
      $('f-asset-url').value = '';
    } else if (prefill.imageUrl) {
      $('f-asset-url').value = prefill.imageUrl;
    } else if (prefill.pdfUrl) {
      $('f-asset-url').value = prefill.pdfUrl;
    } else {
      $('f-asset-url').value = '';
    }
    applyAncestryNote(!!prefill.ancestryImageViewer);
    loadNewspapers(prefill);
    checkFamilySearch();

    const banner = $('recipe-banner');
    const hintLabel = RECIPE_LABELS[prefill.recipeHint];
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

  async function loadNewspapers(prefill) {
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
    if (!info || !info.ok) {
      setNewspapersMode(null);
      state.npError = (info && info.error) || 'no answer from the page';
      $('np-note').style.display = 'block';
      npSetText(npUnavailableReason().replace(/^./, (c) => c.toUpperCase()));
      updateAssetStatus();
      return;
    }
    setNewspapersMode(info);
  }

  function setNewspapersMode(info) {
    state.np = info && info.ok ? info : null;
    state.npSelected = new Set();
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
    // your clippings on the page. The checkboxes below change it.
    const focus = own.find((c) => c.id === np.focusClipId);
    if (focus && np.kind === 'article') state.npSelected.add(focus.id);
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
      $('f-title').placeholder = 'Each clipping gets its own title';
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
  async function captureNewspapers(pageHtml, wantPageCopy, recordUrl) {
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
    // The tab's page copy is a copy of the record open in the tab; build it
    // only if some chosen clipping is that record (lib pageCopyFits).
    const copyFits = (clip) => FHA.newspapers.pageCopyFits(np, clip);
    const pageCopy = wantPageCopy && chosen.some(copyFits) ? await buildPageCopy() : null;
    // The last read of the tab: the page's tiles below are fetched by the
    // page's own id, and each clipping's text by its own address.
    await checkTabStillOn(recordUrl);
    const opened = np.ownClips.find((c) => c.id === np.focusClipId);
    const texts = await npGatherText(chosen);

    const page = await FHA.newspapers.fullPage(np, (i, n, wait) =>
      setStageResult(
        wait
          ? 'Newspapers.com asked us to slow down. Waiting ' + wait + ' seconds before tile ' + i + ' of ' + n + '…'
          : 'Rebuilding the full page from Newspapers.com, tile ' + i + ' of ' + n + ' (paced gently)…',
        ''
      )
    );
    setStageResult('Building the clipping and combined images…', '');
    const pageBlob = await FHA.newspapers.toJpeg(page.canvas);

    const results = [];
    const single = chosen.length === 1;
    for (const clip of chosen) {
      const title = single ? (form.title || npTitle(clip, np)) : npTitle(clip, np);
      try {
        const st = await stageNpClip(clip, np, page, pageBlob, {
          title, single, pageHtml, texts, form,
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
    return stageBundle(captureJson.bundleName(o.title), o.pageHtml, cap, assets);
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

  async function captureAncestryRecord(pageHtml, wantPageCopy, form, recordUrl) {
    const info = await sendToTab(state.tabId, {
      // A title she typed is hers and is used as typed (below); only the
      // form's untouched prefill goes through ancTitle's "is this just the
      // tab's own title?" test (PR #214 review).
      action: 'ancestryRecordInfo', formTitle: form.edited.has('f-title') ? '' : form.title,
    });
    if (!info || !info.ok) throw new Error((info && info.error) || 'could not read the Ancestry record');
    const rec = info.record;
    const hints = info.hints || {};
    const assets = [];
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
    if (dropped) {
      assets.push({
        filename: 'record.' + dropped.ext, blob: dropped.blob,
        role: 'record', mode: 'manual', provisional,
      });
    } else if (sameImageAs) {
      rec.same_image_as = sameImageAs;
    } else if (rec.imageId) {
      setStageResult('Fetching the record’s image from Ancestry…', '');
      const img = await sendToTab(state.tabId, { action: 'ancestryImage', dbId: rec.dbId, imageId: rec.imageId });
      if (img && img.ok) {
        assets.push({
          filename: 'record.' + (img.ext || 'jpg'),
          blob: bundle.base64ToBlob(img.base64, img.contentType),
          role: 'record', mode: 'ancestry-api',
        });
      } else {
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
    const st = await stageBundle(bundleName, pageHtml, cap, assets);
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
    state.fsCheck = r && r.ok ? r : { unknown: true, error: (r && r.error) || 'no answer from the page' };
    if (r && r.ok && r.title && !state.userEdited.has('f-title')) $('f-title').value = r.title;
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

  async function captureFamilySearchRecord(pageHtml, wantPageCopy, form, recordUrl) {
    const info = await sendToTab(state.tabId, {
      action: 'familysearchRecordInfo',
      // FamilySearch's own name for the record; a title she typed is used
      // as typed below, never second-guessed (PR #214 review).
      formTitle: '',
    });
    if (!info || !info.ok) throw new Error((info && info.error) || 'could not read the FamilySearch record');
    const rec = info.record;
    const hints = info.hints || {};
    const assets = [];
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
    if (dropped) {
      assets.push({ filename: 'record.' + dropped.ext, blob: dropped.blob, role: 'record', mode: 'manual', provisional });
    } else if (sameImageAs) {
      rec.same_image_as = sameImageAs;
    } else if (imageKey) {
      setStageResult('Fetching the record’s image from FamilySearch…', '');
      const img = await sendToTab(state.tabId, { action: 'familysearchImage', imageId: imageKey });
      if (img && img.ok) {
        assets.push({
          filename: 'record.' + (img.ext || 'jpg'),
          blob: bundle.base64ToBlob(img.base64, img.contentType),
          role: 'record', mode: 'familysearch-api',
        });
      } else {
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
    const notes = [
      gatherNotes(provisional, form.notes),
      info.citation ? 'FamilySearch citation: ' + info.citation : '',
      sameImageAs ? 'Same image (' + imageKey + ') as ' + describeSameImage(sameImageAs) + '; the image was not downloaded again.' : '',
      imageWarning ? 'Image not saved: FamilySearch image ' + imageKey + ' (' + imageArk + ') could not be downloaded - ' +
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
    const st = await stageBundle(captureJson.bundleName(title), pageHtml, cap, assets);
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

  async function captureFindAGraveMemorial(pageHtml, wantPageCopy, form, recordUrl) {
    const info = await sendToTab(state.tabId, { action: 'findagraveMemorialInfo', today: captureJson.accessedDate() });
    if (!info || !info.ok) throw new Error((info && info.error) || 'could not read the Find a Grave memorial');
    const rec = info.record;
    const hints = info.hints || {};
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
    const gallery = await sendToTab(state.tabId, { action: 'findagraveGallery', memorialId: rec.memorialId, slug: rec.slug });
    const galleryOk = !!(gallery && gallery.ok);
    const listed = galleryOk ? gallery.photos : [];
    const photos = listed.map((p) => ({
      photoId: p.photoId, url: p.url, role: p.role, type: p.type || null,
      caption: p.caption || null, addedBy: p.addedBy || null, file: null,
    }));
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
    let pageCopy = await recordPageCopy('early', wantPageCopy || (!dropped && !toFetch.length), recordUrl);

    const assets = [];
    const failed = [];
    let slowDown = false;
    for (let i = 0; i < toFetch.length; i++) {
      const p = toFetch[i];
      if (slowDown) {
        p.error = 'not tried: Find a Grave asked us to slow down';
        continue;
      }
      if (i) await sleep(FHA.findagrave.pauseMs());
      setStageResult('Downloading photo ' + (i + 1) + ' of ' + toFetch.length + ' from Find a Grave…', '');
      const got = await FHA.findagrave.fetchPhoto(p.url, (u, init) => window.fetch(u, init));
      if (got.ok) {
        p.file = p.role + '.' + got.ext;
        assets.push({ filename: p.file, blob: got.blob, role: p.role, mode: 'findagrave-photo' });
      } else {
        p.error = got.error;
        failed.push(p);
        if (got.slowDown) slowDown = true;
      }
    }
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

    const photosTotal = galleryOk ? gallery.total : null;
    const data = {
      note: 'Captured from Find a Grave by the archive capture extension. "record" is what the memorial page ' +
        'says - a volunteer’s compilation from the stone, records and family knowledge, not an original record. ' +
        '"photos" lists the memorial’s photos: "file" is the file this capture saved it as, or null - then ' +
        '"same_image_as" says where it is already saved, "error" why it could not be saved, "skipped" why it was ' +
        'not tried. "photosTotal" is how many photos the memorial shows (null when its photo list could not be read).',
      record: rec,
      citation: info.citation || null,
      title: info.title,
      photos,
      photosTotal,
    };
    assets.push({
      filename: 'record-data.json',
      blob: new Blob([JSON.stringify(data, null, 2) + '\n'], { type: 'application/json' }),
      role: 'site-data',
      mode: 'findagrave-record-data',
    });

    const saved = photos.filter((p) => p.file);
    const already = photos.filter((p) => p.same_image_as);
    const skipped = photos.filter((p) => p.skipped);
    const named = (list) => list.map((p) => p.role).join(', ');
    let warning = null;
    if (!galleryOk) {
      warning = 'The memorial’s photos were not saved: ' + ((gallery && gallery.error) || 'no answer from the page') +
        '. Its facts and citation are saved; press Capture again later for the photos.';
    } else if (failed.length) {
      warning = (failed.length === 1 ? 'One photo was' : failed.length + ' photos were') + ' not saved (' +
        failed.map((p) => p.role + ': ' + p.error).join('; ') + '). ' +
        (slowDown
          ? 'Wait a few minutes, then press Capture again: the photos already saved are not downloaded again.'
          : 'Press Capture again to try again: the photos already saved are not downloaded again.');
    }
    const photoNotes = [
      saved.length ? saved.length + (saved.length === 1 ? ' photo' : ' photos') + ' saved from the memorial (' + named(saved) + ').' : '',
      already.length ? already.map((p) => 'Photo ' + p.role + ' (Find a Grave photo ' + p.photoId + ') is already saved in ' +
        describeSameImage(p.same_image_as) + '; it was not downloaded again.').join('\n') : '',
      skipped.length ? skipped.length + ' more photos were not downloaded (' + skipped[0].skipped + '): ' + named(skipped) + '.' : '',
      galleryOk && !photos.length ? 'The memorial has no photos.' : '',
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
      assets: assets.map((a) => ({ file: a.filename, role: a.role, mode: a.mode, provisional: !!a.provisional })),
    };
    const cap = captureJson.build(fields);
    const st = await stageBundle(captureJson.bundleName(title), pageHtml, cap, assets);
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
  }

  // Stage one bundle: straight into the archive when the native host answers,
  // else the staging-folder download. Returns { viaHost, where, hostWarning }.
  async function stageBundle(bundleName, pageHtml, cap, assets) {
    let hostWarning = null;
    if (await nativeHost.isAvailable()) {
      try {
        const hostAssets = [];
        for (const a of assets) {
          hostAssets.push({ filename: a.filename, base64: await blobToBase64(a.blob) });
        }
        const resp = await nativeHost.sendBundle({
          bundleName, pageHtml, captureJson: cap, assets: hostAssets,
        });
        return { viaHost: true, where: resp.stub || 'your archive inbox', hostWarning: null };
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
    };
  }

  // ── Step 2: page-copy toggle + evidence picker ───────────────────────────────

  function pageCopyOn() {
    return $('cb-pagecopy').checked;
  }

  function evidenceMode() {
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
    if (state.ancestryViewer || state.ancRecord || state.fsRecord || state.fsImage || state.fagRecord || state.iiif) return true;
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
      const resp = await sendToTab(state.tabId, { action: 'ancestryImage' });
      if (!resp || !resp.ok) {
        // Surface Ancestry's reason (not signed in, downloads disabled, shape
        // changed, thumbnail-sized) and point at the manual fallbacks.
        throw new Error(
          ((resp && resp.error) || 'could not fetch the full record from Ancestry') +
            '. You can use Ancestry’s Download button and drop the file in, or paste an image address.'
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
      const resp = await sendToTab(state.tabId, { action: 'familysearchImage', imageId });
      if (!resp || !resp.ok) {
        throw new Error(((resp && resp.error) || 'could not fetch the full image from FamilySearch') +
          '. You can use FamilySearch’s Download button and drop the file in.');
      }
      const blob = bundle.base64ToBlob(resp.base64, resp.contentType);
      return { blob, filename: 'record.' + (resp.ext || 'jpg'), mode: 'familysearch-api' };
    }
    if (!url && state.iiif) {
      // Open-standard IIIF: fetch the full image (content.js rewrites the
      // derivative's URL to full/full then full/max). No auth, so a plain fetch.
      const resp = await sendToTab(state.tabId, { action: 'iiifImage' });
      if (resp && resp.ok) {
        const blob = bundle.base64ToBlob(resp.base64, resp.contentType);
        return { blob, filename: 'record.' + (resp.ext || 'jpg'), mode: 'iiif' };
      }
      // The full-image fetch failed (rewritten URL 404, CORS). Rather than lose
      // the record entirely, fall back to the visible derivative the page
      // exposed, if any, before giving up.
      const derivative = state.prefill && state.prefill.imageUrl;
      if (derivative) {
        const dResp = await sendToTab(state.tabId, { action: 'fetchAsset', url: derivative });
        if (dResp && dResp.ok) {
          const blob = bundle.base64ToBlob(dResp.base64, dResp.contentType);
          return { blob, filename: 'record.' + (dResp.ext || 'jpg'), mode: 'iiif-derivative' };
        }
      }
      throw new Error(
        ((resp && resp.error) || 'could not fetch the full IIIF image') +
          '. You can paste an image address or drop a file in instead.'
      );
    }
    const resp = await sendToTab(state.tabId, { action: 'fetchAsset', url });
    if (!resp || !resp.ok) {
      throw new Error((resp && resp.error) || 'could not pull that file from the page');
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

  // The batch-mode staleness tell: if the tab's address moved after the form
  // was pre-filled (a navigation the refresh missed, or one still parked behind
  // an earlier capture), the fields below may describe the previous record.
  // Warn in the top banner - never block, and never touch the fields: the human
  // may have edited them deliberately for exactly this page.
  async function warnIfFormStale() {
    if (!state.prefilledUrl) return; // no pre-fill baseline, nothing to compare
    const tab = await getTab(state.tabId);
    if (!tab || !tab.url || sameRecordUrl(tab.url, state.prefilledUrl)) return;
    staleBanner();
  }

  function staleBanner() {
    const banner = $('recipe-banner');
    banner.textContent =
      'This page changed since the form was filled - check the title and web address before saving.';
    banner.classList.remove('recipe');
    banner.classList.add('warn');
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
    };
  }

  async function capture() {
    if (state.busy) return;
    if (state.navInFlight) {
      setStageResult('The page in the tab just changed - reading it now. Press Capture again in a moment.', 'warn');
      return;
    }

    const wantPageCopy = pageCopyOn();
    const wantEvidence = evidenceMode() === 'yes';
    const form = formSnapshot();
    // Both controls off is NOT a refusal: it is the pointer-only capture
    // (§5.3's "none", TOOLING §13b case (c)) - page.html + an empty assets
    // list, which ingest files as a citation/link pointer stub
    // (asset_elsewhere: true). The status line above the button already says
    // "Citation + link only", so the human sees what they are staging.
    if (wantEvidence && !state.droppedAsset && state.npPage && state.npLoading) {
      // The clippings for the page now in the tab are still being read; the
      // previous page's are already forgotten (loadNewspapers), so wait.
      setStageResult(
        'Still reading your clippings on this Newspapers.com page - wait a moment, then press Capture again.',
        'warn'
      );
      return;
    }
    if (wantEvidence && !state.droppedAsset && !evidenceUrl()
        && !state.ancestryViewer && !state.ancRecord && !state.fsRecord && !state.fsImage
        && !state.fagRecord && !state.iiif && !npActive()) {
      if (state.npPage) {
        setStageResult('Newspapers.com: ' + npUnavailableReason(), 'warn');
        return;
      }
      setStageResult(
        'Add the file address or drop a file, or switch to "No, the page copy is the record".',
        'warn'
      );
      return;
    }

    state.busy = true;
    $('btn-capture').disabled = true;
    setStageResult('Capturing…', '');

    // The record this capture is of: the one the form was filled for.
    const recordUrl = state.prefilledUrl;
    const recordCapture = wantEvidence && !evidenceUrl() &&
      ((npActive() && !state.droppedAsset) || state.ancRecord || state.fsRecord || state.fagRecord);
    try {
      await warnIfFormStale();
      // A record capture saves the form's details with the record's own data
      // from the tab: if the tab is no longer on that record, they would not
      // match, so stop before reading anything.
      if (recordCapture) await checkTabStillOn(recordUrl);

      // page.html is always saved - grab it fresh at capture time so any
      // late-settling content is in the raw DOM the recipe re-extracts from.
      const pageResp = await sendToTab(state.tabId, { action: 'pagehtml' });
      if (!pageResp || !pageResp.ok || !pageResp.html) {
        throw new Error('could not read the page, reload it and try again');
      }
      if (recordCapture) await checkTabStillOn(recordUrl);

      // Newspapers.com: the whole page + each chosen clipping, one bundle per
      // clipping. A dropped file still wins (the manual override).
      if (wantEvidence && npActive() && !state.droppedAsset) {
        await captureNewspapers(pageResp.html, wantPageCopy, recordUrl);
        resetForNext();
        return;
      }

      // Ancestry record page: the record's own index and citation, plus its
      // linked scan - or the file she dropped in, saved with the index. A typed
      // file address still takes the ordinary path.
      if (wantEvidence && state.ancRecord && !evidenceUrl()) {
        await captureAncestryRecord(pageResp.html, wantPageCopy, form, recordUrl);
        resetForNext();
        return;
      }

      // FamilySearch record (or the viewer opened from one): FamilySearch's
      // own data and citation, plus its image - or the file she dropped in,
      // saved with the data.
      if (wantEvidence && state.fsRecord && !evidenceUrl()) {
        await captureFamilySearchRecord(pageResp.html, wantPageCopy, form, recordUrl);
        resetForNext();
        return;
      }

      // Find a Grave memorial: what the memorial says and its citation, plus
      // its photos at full size (and a file she dropped in, saved with them).
      if (wantEvidence && state.fagRecord && !evidenceUrl()) {
        await captureFindAGraveMemorial(pageResp.html, wantPageCopy, form, recordUrl);
        resetForNext();
        return;
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
      // The page these files come from, as the browser shows it now (the
      // ordinary path keeps its warn-but-save rule for a stale form).
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
          // An automatic file is read from the page itself: it must still be this page.
          if (autoOnly) await checkTabStillOn(pageUrl);
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
        notes: gatherNotes(provisional),
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
    } catch (e) {
      setStageResult('Could not capture: ' + e.message, 'error');
      // The form still describes the record she left: read the new one.
      if (e instanceof TabMoved) state.pendingNav = true;
    } finally {
      state.busy = false;
      $('btn-capture').disabled = false;
      // Replay a navigation that arrived mid-capture (parked by
      // refreshOnNavigation) so the form moves on to the record now in the
      // tab instead of silently staying on the one just staged.
      if (state.pendingNav) {
        state.pendingNav = false;
        refreshOnNavigation(state.tabId, { status: 'complete' });
      }
    }
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

  function resetForNext() {
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
      // Same-record compare, not exact compare: in-page viewers (BookReader's
      // #page arrows) move only the #fragment without changing the record, and
      // a re-prefill on that wipes the human's typed notes/people for nothing.
      // The staleness banner (warnIfFormStale) already compares this way.
      if (sameRecordUrl(tab.url, state.prefilledUrl)) return;
      try {
        await injectContent(tabId);
        const resp = await sendToTab(tabId, { action: 'prefill' });
        if (parked()) return;
        if (resp && resp.ok) applyPrefill(resp.prefill);
      } catch (e) {
        // A restricted/again-loading page: don't clobber the form with an error;
        // the human can still type, and the next 'complete' may succeed.
      }
    } finally {
      if (seq === state.navSeq) state.navInFlight = false;
    }
  }

  // ── settings (chrome.storage) ────────────────────────────────────────────────

  function loadSettings() {
    return new Promise((resolve) => {
      chrome.storage.local.get(
        {
          captureFolder: captureJson.DEFAULT_FOLDER,
          defaultEvidence: 'yes',
          pageCopyDefault: true,
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
      chrome.storage.local.set({ pageCopyDefault: pageCopyOn() });
      updateAssetStatus();
    });
    document.querySelectorAll('input[name="assetMode"]').forEach((r) =>
      r.addEventListener('change', () => {
        selectEvidenceCard(evidenceMode());
        updateAssetStatus();
      })
    );
    $('f-asset-url').addEventListener('input', updateAssetStatus);
    // Remember which fields the human typed in, so newspaper data arriving
    // later never overwrites a correction (state.userEdited, #211 review).
    for (const id of ['f-title', 'f-date', 'f-repo', 'f-url', 'f-type']) {
      $(id).addEventListener('input', () => state.userEdited.add(id));
      $(id).addEventListener('change', () => state.userEdited.add(id));
    }
    $('f-provisional').addEventListener('change', updateAssetStatus);
    $('btn-capture').addEventListener('click', capture);
    $('btn-copy-cmd').addEventListener('click', () => copyCmd($('btn-copy-cmd')));
    $('btn-add-person').addEventListener('click', () => {
      const name = $('f-add-person').value.trim();
      if (name) {
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

    try {
      await injectContent(tab.id);
      const resp = await sendToTab(tab.id, { action: 'prefill' });
      if (resp && resp.ok) applyPrefill(resp.prefill);
      else throw new Error((resp && resp.error) || 'no pre-fill');
    } catch (e) {
      // A restricted page (chrome://, the web store) or a page that hasn't
      // finished loading. Don't dead-end: explain and let the human still type.
      $('recipe-banner').textContent =
        "Couldn't read this page automatically. Type the details below, or reload and reopen.";
      $('f-url').value = tab.url || '';
    }

    // Honor the saved default evidence option as the initial selection.
    selectEvidenceMode((cfg && cfg.defaultEvidence) || 'yes');
    updateAssetStatus();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
