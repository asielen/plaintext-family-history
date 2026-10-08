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
// Classic script; depends on window.FHA.{captureJson,bundle,nativeHost}.

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
    // Newspapers.com: the newspapersInfo result for the page in the tab (page
    // id, size, image pass, YOUR clippings on it) and which of them to save.
    // On Capture the panel rebuilds the page from tiles (lib/newspapers.js).
    np: null,
    npSelected: new Set(),
    // On a Newspapers.com page: why automatic saving is unavailable, if it is.
    npPage: false,
    npError: null,
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
    state.prefill = prefill;
    state.iiif = !!prefill.iiif;
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
    if (prefill.ancestryImageViewer || prefill.iiif || prefill.newspapers) {
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
  // Titles, dates and citations live in lib/newspapers.js, where they are tested.
  const npPub = (clip, np) => FHA.newspapers.publicationOf(clip, np);
  const npTitle = (clip, np) => FHA.newspapers.title(clip, np);
  const npCaption = (clip, np) => FHA.newspapers.caption(clip, np);
  const npCitations = (clip, np) =>
    FHA.newspapers.citations(clip, np, captureJson.accessedDate());
  // The machine-and-human-readable record of where the clipping sits, so the
  // combined view can be rebuilt later and the crop is never a mystery.
  function npNotes(clip, np, page) {
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
    return lines.join('\n');
  }

  function npSetText(text) {
    const el = $('np-text');
    if (el) el.textContent = text;
  }

  async function loadNewspapers(prefill) {
    state.npPage = !!(prefill && prefill.newspapers);
    state.npError = null;
    if (!state.npPage) {
      setNewspapersMode(null);
      return;
    }
    const forUrl = prefill.url;
    $('np-note').style.display = 'block';
    npSetText('Reading your clippings on this page…');
    $('np-clips').textContent = '';
    let info;
    try {
      info = await sendToTab(state.tabId, { action: 'newspapersInfo' });
    } catch (e) {
      info = { ok: false, error: e.message };
    }
    if (state.prefilledUrl !== forUrl) return; // the tab moved on meanwhile
    if (!info || !info.ok) {
      setNewspapersMode(null);
      state.npError = (info && info.error) || 'no answer from the page';
      $('np-note').style.display = 'block';
      npSetText(
        'Automatic saving is not available here: ' + ((info && info.error) || 'no answer from the page') +
          '. You can still drop in a file.'
      );
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
        urlInput.value = '';
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
      npSetText(
        np.focusOwner
          ? 'This clipping was made by ' + np.focusOwner + ', not you, so nothing is saved ' +
              'automatically. Clip the item yourself on Newspapers.com, or drop in a file.'
          : 'None of the clippings on this page are yours, so nothing is saved automatically. ' +
              'Clip the item on Newspapers.com first, then open the panel again.'
      );
      updateAssetStatus();
      return;
    }
    // Default: on a clipping page, that clipping; on the page viewer, all of
    // your clippings on the page. The checkboxes below change it.
    const focus = own.find((c) => c.id === np.focusClipId);
    if (focus && np.kind === 'article') state.npSelected.add(focus.id);
    else own.forEach((c) => state.npSelected.add(c.id));

    urlInput.value = NP_URL_LABEL;
    urlInput.readOnly = true;
    npSetText(
      (np.focusOwner
        ? 'This clipping was made by ' + np.focusOwner + '. It is saved because you opened it. '
        : '') +
        'On Capture this rebuilds the whole page at full resolution from Newspapers.com’s own ' +
        'image tiles, one at a time with pauses (about 40 seconds a page), and cuts out your ' +
        'clipping. Each clipping becomes its own source with the clipping, the whole page, ' +
        'and a combined view with the item boxed.' +
        (np.otherClipCount
          ? ' Other clippings on this page that are not yours (' + np.otherClipCount + ') are skipped.'
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
    if (!np || np.ownClips.length < 2) return;
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
    $('f-type').value = 'newspaper';
    if (chosen.length === 1) {
      const c = chosen[0];
      $('f-title').value = npTitle(c, np);
      $('f-title').placeholder = '';
      $('f-date').value = c.pageDate || np.pageDate || '';
      $('f-repo').value = npPub(c, np).name || 'Newspapers.com';
      if (c.url) $('f-url').value = c.url;
    } else if (chosen.length > 1) {
      $('f-title').value = '';
      $('f-title').placeholder = 'Each clipping gets its own title';
      $('f-date').value = np.pageDate || '';
      $('f-repo').value = (np.publication && np.publication.name) || 'Newspapers.com';
    }
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

  async function captureNewspapers(pageHtml, wantPageCopy) {
    const np = state.np;
    const chosen = np.ownClips.filter((c) => state.npSelected.has(c.id));
    if (!chosen.length) throw new Error('tick at least one of your clippings');
    const pageCopy = wantPageCopy ? await buildPageCopy() : null;
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

    const staged = [];
    let viaHost = false;
    let hostWarning = null;
    const single = chosen.length === 1;
    for (const clip of chosen) {
      const clipBlob = await FHA.newspapers.toJpeg(FHA.newspapers.clipCanvas(page, clip));
      // Dashed boxes mark YOUR other clippings on the page only (on a clipping
      // page made by someone else, theirs is offered but is not yours).
      const others = np.ownClips.filter(
        (o) => o.id !== clip.id && (!np.me || !np.me.id || o.userId === np.me.id)
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
          blob: new Blob([JSON.stringify(npClipData(clip, np, texts.get(clip.id)), null, 2) + '\n'],
            { type: 'application/json' }),
          role: 'site-data',
          mode: 'newspapers-page-data',
        },
      ];
      if (pageCopy) {
        assets.push({ filename: pageCopy.filename, blob: pageCopy.blob, role: 'webpage', mode: 'singlefile' });
      }
      const title = single ? ($('f-title').value.trim() || npTitle(clip, np)) : npTitle(clip, np);
      const fields = {
        url: clip.url || $('f-url').value.trim() || (state.prefill && state.prefill.url),
        title,
        accessed: captureJson.accessedDate(),
        sourceDate: single
          ? ($('f-date').value.trim() || clip.pageDate || np.pageDate || '')
          : (clip.pageDate || np.pageDate || ''),
        sourceType: $('f-type').value || 'newspaper',
        repository: single ? $('f-repo').value.trim() : (npPub(clip, np).name || 'Newspapers.com'),
        citation: npCitations(clip, np).genealogy,
        people: checkedPeople(),
        notes: [gatherNotes(false), npNotes(clip, np, page)].filter((t) => t && t.trim()).join('\n\n'),
        recipeHint: 'newspapers',
        assets: assets.map((a) => ({ file: a.filename, role: a.role, mode: a.mode, provisional: false })),
      };
      const cap = captureJson.build(fields);
      const st = await stageBundle(captureJson.bundleName(title), pageHtml, cap, assets);
      staged.push(st.where);
      viaHost = st.viaHost;
      if (st.hostWarning) hostWarning = st.hostWarning;
      if (!single) setStageResult('Saved ' + staged.length + ' of ' + chosen.length + ' clippings…', '');
    }
    const reducedWarn = page.reduced
      ? page.reduced + ' of ' + page.tiles + ' page tiles came back below full size, so parts of page.jpg are softer than the scan.'
      : null;
    const where = single
      ? staged[0]
      : staged.length + ' captures, one per clipping (the last: ' + staged[staged.length - 1] + ')';
    reportStaged(where, viaHost, null, [hostWarning, reducedWarn].filter(Boolean).join('\n') || null);
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
    if (state.ancestryViewer || state.iiif) return true;
    if (npActive() && !state.droppedAsset) return state.npSelected.size > 0;
    return !!(state.droppedAsset || evidenceUrl());
  }

  // A running, plain-language summary of what the capture will contain, so the
  // human can see the "both" composition before pressing Capture.
  function updateAssetStatus() {
    const parts = [];
    if (pageCopyOn()) parts.push('Whole-page copy ✓');
    if (evidenceMode() === 'yes') {
      if (state.droppedAsset) {
        const note = $('f-provisional').checked ? ' (screen capture)' : '';
        parts.push('Record file: ' + state.droppedAsset.filename + note);
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
    const ok = pageCopyOn() || evidenceMode() !== 'yes' || hasEvidence();
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

  function gatherNotes(provisional) {
    let notes = $('f-notes').value;
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
    const banner = $('recipe-banner');
    banner.textContent =
      'This page changed since the form was filled - check the title and web address before saving.';
    banner.classList.remove('recipe');
    banner.classList.add('warn');
  }

  async function capture() {
    if (state.busy) return;

    const wantPageCopy = pageCopyOn();
    const wantEvidence = evidenceMode() === 'yes';
    // Both controls off is NOT a refusal: it is the pointer-only capture
    // (§5.3's "none", TOOLING §13b case (c)) - page.html + an empty assets
    // list, which ingest files as a citation/link pointer stub
    // (asset_elsewhere: true). The status line above the button already says
    // "Citation + link only", so the human sees what they are staging.
    if (wantEvidence && !state.droppedAsset && !evidenceUrl()
        && !state.ancestryViewer && !state.iiif && !npActive()) {
      if (state.npPage) {
        setStageResult(
          'Newspapers.com automatic saving is off for this page' +
            (state.npError
              ? ': ' + state.npError
              : state.np && state.np.focusOwner
                ? ': this clipping was made by ' + state.np.focusOwner + ', not you. Clip the item yourself to save it'
                : state.np ? ': none of the clippings here are yours' : '') +
            '. Reload the page and reopen the panel, or drop a file in.',
          'warn'
        );
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

    try {
      await warnIfFormStale();

      // page.html is always saved - grab it fresh at capture time so any
      // late-settling content is in the raw DOM the recipe re-extracts from.
      const pageResp = await sendToTab(state.tabId, { action: 'pagehtml' });
      if (!pageResp || !pageResp.ok || !pageResp.html) {
        throw new Error('could not read the page, reload it and try again');
      }

      // Newspapers.com: the whole page + each chosen clipping, one bundle per
      // clipping. A dropped file still wins (the manual override).
      if (wantEvidence && npActive() && !state.droppedAsset) {
        await captureNewspapers(pageResp.html, wantPageCopy);
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
      if (wantPageCopy) {
        const pc = await buildPageCopy();
        assets.push({
          filename: pc.filename, blob: pc.blob,
          role: 'webpage', mode: 'singlefile',
        });
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
          (state.ancestryViewer || state.iiif) && !state.droppedAsset && !evidenceUrl();
        if (autoOnly && wantPageCopy) {
          try {
            const ev = await buildEvidence();
            assets.push({
              filename: ev.filename, blob: ev.blob,
              role: 'record', mode: ev.mode,
              provisional,
            });
          } catch (e) {
            evidenceWarning = e.message;
          }
        } else {
          const ev = await buildEvidence();
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
    const tab = await getTab(tabId);
    if (!tab || !/^https?:/i.test(tab.url || '')) return;
    // Same-record compare, not exact compare: in-page viewers (BookReader's
    // #page arrows) move only the #fragment without changing the record, and
    // a re-prefill on that wipes the human's typed notes/people for nothing.
    // The staleness banner (warnIfFormStale) already compares this way.
    if (sameRecordUrl(tab.url, state.prefilledUrl)) return;
    try {
      await injectContent(tabId);
      const resp = await sendToTab(tabId, { action: 'prefill' });
      if (resp && resp.ok) applyPrefill(resp.prefill);
    } catch (e) {
      // A restricted/again-loading page: don't clobber the form with an error;
      // the human can still type, and the next 'complete' may succeed.
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
