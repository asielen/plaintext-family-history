// bundle.js - write the staged bundle into Downloads (TOOLING_INGESTION §5.1/§5.2).
//
// The transport problem and its honest answer (§5.1): an MV3 extension cannot
// write to an arbitrary path - its only file-writing affordance is
// chrome.downloads.download(), which writes UNDER the browser's Downloads
// directory. So the default path stages the bundle to
// `Downloads/<folder>/<slug>-<timestamp>-<token>/` and the human later runs
// `fha capture --ingest` to sweep it into the real `inbox/` (the one sanctioned
// move). This module never pretends it reached the archive; it returns exactly
// where the files landed so the panel can say so.
//
// Loaded as a classic script in panel.html; attaches to the global `FHA`.

(function () {
  const FHA = (window.FHA = window.FHA || {});

  // Decode the base64 a content-script asset fetch returned into a Blob the
  // downloads API can write. (Drag-dropped files are already Blobs and skip this.)
  function base64ToBlob(base64, contentType) {
    const binary = atob(base64);
    const len = binary.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) bytes[i] = binary.charCodeAt(i);
    return new Blob([bytes], { type: contentType || 'application/octet-stream' });
  }

  // Resolve only once the download truly finishes, so the panel never reports
  // "staged" before the bytes are on disk. An interrupted download rejects with
  // its reason so a failed write surfaces instead of looking like success.
  //
  // The tiny page.html / capture.json blobs can finish BEFORE this listener
  // attaches (the download() callback returns the id, and only then do we get
  // here) - so after registering we also poll the current state once, resolving
  // immediately if it already completed. Without this a fast small write hangs
  // the panel on "Capturing…" forever.
  function waitForComplete(downloadId) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (fn, arg) => {
        if (settled) return;
        settled = true;
        chrome.downloads.onChanged.removeListener(onChanged);
        fn(arg);
      };
      const onChanged = (delta) => {
        if (delta.id !== downloadId || !delta.state) return;
        if (delta.state.current === 'complete') {
          finish(resolve, downloadId);
        } else if (delta.state.current === 'interrupted') {
          const why = delta.error ? delta.error.current : 'unknown error';
          finish(reject, new Error('download interrupted: ' + why));
        }
      };
      chrome.downloads.onChanged.addListener(onChanged);
      // Catch a download that already finished before the listener was added.
      chrome.downloads.search({ id: downloadId }, (items) => {
        const item = items && items[0];
        if (!item) return; // still in flight; the listener will catch it
        if (item.state === 'complete') {
          finish(resolve, downloadId);
        } else if (item.state === 'interrupted') {
          finish(reject, new Error('download interrupted: ' + (item.error || 'unknown error')));
        }
      });
    });
  }

  function downloadBlob(blob, path) {
    const url = URL.createObjectURL(blob);
    return new Promise((resolve, reject) => {
      chrome.downloads.download(
        { url, filename: path, conflictAction: 'uniquify', saveAs: false },
        (downloadId) => {
          if (chrome.runtime.lastError || downloadId == null) {
            URL.revokeObjectURL(url);
            reject(
              new Error(
                (chrome.runtime.lastError && chrome.runtime.lastError.message) ||
                  'the browser refused to write ' + path
              )
            );
            return;
          }
          waitForComplete(downloadId)
            .then(() => resolve(downloadId))
            .catch(reject)
            .finally(() => URL.revokeObjectURL(url));
        }
      );
    });
  }

  // The absolute path the browser actually wrote a completed download to, or
  // '' when it will not say.
  //
  // DownloadItem.filename is the browser's OWN answer to "where did this go",
  // and it is the only honest one available here: the download directory is a
  // browser setting (moved to OneDrive, to another volume, or carrying a
  // localized name on plenty of machines), so a home-relative Downloads path
  // is a guess, and the handoff command must not be built out of one. An
  // empty string means
  // "unknown" - never a fallback path - and the panel degrades to the bare
  // ingest command plus a plain hint rather than sending the sweep somewhere
  // the bundle is not.
  function downloadedFilePath(downloadId) {
    return new Promise((resolve) => {
      try {
        chrome.downloads.search({ id: downloadId }, (items) => {
          const item = items && items[0];
          resolve((item && item.filename) || '');
        });
      } catch (e) {
        // A staged bundle is on disk either way; not knowing its absolute path
        // is a smaller thing than failing the capture over it.
        resolve('');
      }
    });
  }

  // Write the files of one bundle: page.html (always) + zero-or-more asset files
  // + capture.json.
  //
  //   spec = {
  //     folder,        // Downloads subfolder, e.g. 'fha-inbox'
  //     bundleName,    // '<slug>-<timestamp>-<token>' - unique per capture, so
  //                    //   `conflictAction: 'uniquify'` below never has to
  //                    //   rename a member of this bundle (see capture-json.js
  //                    //   randomToken for what that would cost)
  //     pageHtml,      // string - ALWAYS written (§5.2: page.html is mandatory)
  //     assets,        // [{ filename, blob }]  - the "both" case may carry two
  //                    //   (a `webpage` page copy and a `record` evidence file);
  //                    //   empty/absent for the pointer-only case (c)
  //     captureJson,   // the object from FHA.captureJson.build()
  //   }
  //
  // page.html is written first and capture.json last, so a bundle that exists on
  // disk with a capture.json is known-complete - the same "metadata closes the
  // record" ordering the engine relies on when it reads a bundle back.
  async function writeBundle(spec) {
    const dir = spec.folder.replace(/\/+$/, '') + '/' + spec.bundleName;
    const written = [];

    const pageId = await downloadBlob(
      new Blob([spec.pageHtml], { type: 'text/html' }),
      dir + '/page.html'
    );
    written.push('page.html');
    // Read the real location off the first completed file. Every file of this
    // bundle goes to the same folder, so one lookup answers for all of them,
    // and page.html is the one file always written.
    const filePath = await downloadedFilePath(pageId);

    for (const asset of spec.assets || []) {
      if (!asset || !asset.blob || !asset.filename) continue;
      await downloadBlob(asset.blob, dir + '/' + asset.filename);
      written.push(asset.filename);
    }

    const json = JSON.stringify(spec.captureJson, null, 2) + '\n';
    await downloadBlob(
      new Blob([json], { type: 'application/json' }),
      dir + '/capture.json'
    );
    written.push('capture.json');

    // `dir` is the Downloads-RELATIVE path we asked for; `filePath` is where
    // the browser put it. The panel needs the second to tell the human
    // anything true about the location.
    return { dir, files: written, filePath };
  }

  FHA.bundle = { base64ToBlob, writeBundle };
})();
