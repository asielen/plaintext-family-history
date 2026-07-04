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
// The design (private/capture-panel-mockup.html) composes two independent
// choices in Step 2:
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
    if (prefill.ancestryImageViewer || prefill.iiif) {
      $('f-asset-url').value = '';
    } else if (prefill.imageUrl) {
      $('f-asset-url').value = prefill.imageUrl;
    } else if (prefill.pdfUrl) {
      $('f-asset-url').value = prefill.pdfUrl;
    } else {
      $('f-asset-url').value = '';
    }
    applyAncestryNote(!!prefill.ancestryImageViewer);

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

  // ── Step 2: page-copy toggle + evidence picker ───────────────────────────────

  function pageCopyOn() {
    return $('cb-pagecopy').checked;
  }

  function evidenceMode() {
    const r = document.querySelector('input[name="assetMode"]:checked');
    return r ? r.value : 'yes';
  }

  function evidenceUrl() {
    return ($('f-asset-url').value || '').trim();
  }

  function hasEvidence() {
    if (evidenceMode() !== 'yes') return false;
    // On an Ancestry image-viewer or an IIIF page the auto path supplies the
    // full record on Capture, so "Yes" is satisfied with no dropped file or URL.
    // A dropped file or a typed URL still takes precedence (manual override).
    if (state.ancestryViewer || state.iiif) return true;
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
    } else {
      parts.push('the page copy is the record');
    }
    const ok = pageCopyOn() || hasEvidence();
    setAssetStatus(
      parts.join('   ·   ') || 'Nothing yet. Tick the page copy or pick a file.',
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
    if (!wantPageCopy && !wantEvidence) {
      setStageResult(
        'Tick "Keep a copy of the whole page", or choose to save the actual file.',
        'warn'
      );
      return;
    }
    if (wantEvidence && !state.droppedAsset && !evidenceUrl()
        && !state.ancestryViewer && !state.iiif) {
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

      const spec = {
        folder: state.folder,
        bundleName,
        pageHtml: pageResp.html,
        assets: assets.map((a) => ({ filename: a.filename, blob: a.blob })),
        captureJson: cap,
      };

      // Seamless path (§5.7) when the human opted in and a host answers; else the
      // honest staging-folder download (§5.1). The extension never claims it
      // reached the archive when it only reached Downloads.
      let viaHost = false;
      let hostWarning = null;
      if (await nativeHost.isAvailable()) {
        try {
          const hostAssets = [];
          for (const a of assets) {
            hostAssets.push({
              filename: a.filename,
              base64: await blobToBase64(a.blob),
            });
          }
          const resp = await nativeHost.sendBundle({
            bundleName,
            pageHtml: pageResp.html,
            captureJson: cap,
            assets: hostAssets,
          });
          viaHost = true;
          reportStaged(resp.stub || 'your archive inbox', true, evidenceWarning);
        } catch (e) {
          // Fall back to the download path rather than failing the capture -
          // but say so. The human opted into the host (isAvailable was true
          // just above), so a silent downgrade would misreport where captures
          // are going for the rest of the sitting.
          viaHost = false;
          hostWarning =
            "Your archive connection didn't answer (" + shortHostReason(e) +
            ') - this capture was saved to Downloads instead. Run' +
            ' `fha capture --ingest` to sweep it in, and `fha capture --install-host`' +
            ' if this keeps happening.';
        }
      }
      if (!viaHost) {
        const result = await bundle.writeBundle(spec);
        reportStaged(result.dir, false, evidenceWarning, hostWarning);
      }

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
    // archive.
    setStageResult('Staged to Downloads/' + where + suffix, cls);
    $('handoff').classList.add('show');
  }

  function setStageResult(text, cls) {
    const el = $('stage-result');
    el.textContent = text;
    el.className = 'stage-result' + (cls ? ' ' + cls : '');
  }

  function resetForNext() {
    // Batch capture is the natural mode (§5.3): a research sitting yields a dozen
    // bundles. Clear the evidence so the next page starts fresh, but leave the
    // panel open and the settings intact. The form metadata (title/date/people/
    // repo) is refreshed when the human navigates to the next record - see
    // refreshOnNavigation - so it never carries one record's details onto the next.
    state.droppedAsset = null;
    const drop = $('dropzone');
    if (drop) {
      drop.classList.remove('has-file');
      drop.textContent = 'Drop a file here, or click to choose';
    }
    // The screen-capture flag describes the file just staged, never the next
    // one - it must not stick across records (and it is never persisted).
    $('f-provisional').checked = false;
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
  // the moment the capture lands. Skips same-page updates; an unreadable new
  // page is left for the human to fill, not dead-ended.
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
    if (!tab || !/^https?:/i.test(tab.url || '') || tab.url === state.prefilledUrl) return;
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
        { captureFolder: 'fha-inbox', defaultEvidence: 'yes', pageCopyDefault: true },
        (cfg) => {
          state.folder = cfg.captureFolder || 'fha-inbox';
          $('f-folder').value = state.folder;
          $('f-default-evidence').value = cfg.defaultEvidence || 'yes';
          $('cb-pagecopy').checked = cfg.pageCopyDefault !== false;
          resolve(cfg);
        }
      );
    });
  }

  function wireSettings() {
    $('f-folder').addEventListener('change', () => {
      state.folder = $('f-folder').value.trim() || 'fha-inbox';
      chrome.storage.local.set({ captureFolder: state.folder });
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
          setSeamlessHint('Off - captures stage to Downloads for `fha capture --ingest`.');
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
          : "Permission granted, but no capture host answered. Run "
            + '`fha capture --install-host` in your archive, then reopen this panel.');
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
