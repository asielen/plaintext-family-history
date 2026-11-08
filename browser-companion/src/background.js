// background.js - the service worker that opens the capture panel, and the
// one writer of the batch downloads' ledgers.
//
// The browser companion (TOOLING_INGESTION §5) is a *dumb, replaceable front-end*:
// it stages raw material into Downloads and the durable Python tooling
// (`fha capture --ingest`) decides what that material means. This worker wires
// the toolbar button to the side panel and otherwise stays out of the way. All
// the real work (reading the page, fetching the asset, writing the bundle)
// happens in the content script and the panel; there is deliberately no ambient
// page access and no background scraping here (§2.4, §7).
//
// The owner's batch downloads (switched off unless told: src/batch.js) keep
// their ledgers in chrome.storage.local, and only this worker writes them
// (lib/batch-store.js): every panel window asks it for each change, so no
// window's change is lost to another's. With batch downloads switched off no
// panel asks, and nothing here reads or writes a ledger.

importScripts('lib/capture-batch.js', 'lib/batch-store.js');

// Open the side panel when the human clicks the toolbar button (Phase 1, §5.3).
// setPanelBehavior is the modern one-call wiring; the onClicked fallback below
// covers a Chromium build where that call is unavailable, so the button always
// does something rather than silently failing.
chrome.runtime.onInstalled.addListener(() => {
  if (chrome.sidePanel && chrome.sidePanel.setPanelBehavior) {
    chrome.sidePanel
      .setPanelBehavior({ openPanelOnActionClick: true })
      .catch((err) => console.warn('fha-capture: setPanelBehavior failed', err));
  }
});

chrome.action.onClicked.addListener(async (tab) => {
  // onClicked only fires when openPanelOnActionClick did NOT already handle the
  // click (older builds, or if the call above failed), so opening here is safe
  // and never double-opens.
  if (!chrome.sidePanel || !chrome.sidePanel.open) return;
  try {
    if (tab && tab.windowId != null) {
      await chrome.sidePanel.open({ windowId: tab.windowId });
    }
  } catch (err) {
    console.warn('fha-capture: sidePanel.open failed', err);
  }
});

// ── the batch ledgers' one writer ────────────────────────────────────────────

function storageCall(fn) {
  return new Promise((resolve, reject) => {
    try {
      fn((value) => {
        const err = chrome.runtime.lastError;
        if (err) reject(new Error(err.message));
        else resolve(value);
      });
    } catch (e) {
      reject(e);
    }
  });
}

const batchStore = self.FHA.batchStore.create({
  captureBatch: self.FHA.captureBatch,
  get: (key) => storageCall((done) => chrome.storage.local.get(key, (o) => done(o ? o[key] : undefined))),
  set: (obj) => storageCall((done) => chrome.storage.local.set(obj, () => done())),
  remove: (key) => storageCall((done) => chrome.storage.local.remove(key, () => done())),
});

// An intent from one of this add-on's own panels only.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || (msg.type !== 'fha-batch' && msg.type !== 'fha-batch-lease')) return false;
  if (!sender || sender.id !== chrome.runtime.id) return false;
  batchStore.handle(msg).then(sendResponse,
    (err) => sendResponse({ ok: false, error: 'the batch could not make that change (' + ((err && err.message) || err) + ')' }));
  return true; // the answer comes later
});

// A panel running a batch keeps a port open: when it closes (the panel was
// closed mid-run), the lease is let go at once, so another window can start.
chrome.runtime.onConnect.addListener((port) => {
  const m = /^fha-batch-run:(.+)$/.exec((port && port.name) || '');
  if (!m || !port.sender || port.sender.id !== chrome.runtime.id) return;
  port.onDisconnect.addListener(() => {
    batchStore.handle({ type: 'fha-batch-lease', action: 'release', owner: m[1] }).catch(() => {});
  });
});
