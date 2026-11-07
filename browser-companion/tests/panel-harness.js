// A stand-in for the capture panel's page, so tests can drive panel.js itself
// without a browser: every element panel.html declares, a scripted content
// script (`respond`), recording stand-ins for the download and the archive
// connection, and the Seen It add-on's one question. The image work
// (canvases) is stubbed; only a real browser can check pixels.
//
// Used by every tests/test-panel-*.js (the batch downloads' too: `storage` holds
// their switch and queues, `navigations` the addresses a batch sent the tab to).

'use strict';

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const { webcrypto } = require('node:crypto');

const SRC = path.join(__dirname, '..', 'src');
const read = (rel) => fs.readFileSync(path.join(SRC, rel), 'utf8');

// ── a stand-in page ──────────────────────────────────────────────────────────

function makeEl(id, tag) {
  const el = {
    id, tagName: tag, value: '', checked: false, readOnly: false, disabled: false,
    placeholder: '', className: '', type: '', style: {}, dataset: {},
    children: [], listeners: {}, _text: '',
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children = []; },
    get innerHTML() { return ''; },
    set innerHTML(v) { this.children = []; },
    classList: {
      set: new Set(),
      add(...c) { c.forEach((x) => this.set.add(x)); },
      remove(...c) { c.forEach((x) => this.set.delete(x)); },
      toggle(c, on) {
        const want = on === undefined ? !this.set.has(c) : !!on;
        if (want) this.set.add(c);
        else this.set.delete(c);
        return want;
      },
      contains(c) { return this.set.has(c); },
    },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    appendChild(c) { this.children.push(c); return c; },
    insertBefore(c, ref) {
      const i = ref ? this.children.indexOf(ref) : -1;
      if (i < 0) this.children.push(c);
      else this.children.splice(i, 0, c);
      return c;
    },
    removeChild(c) {
      const i = this.children.indexOf(c);
      if (i < 0) throw new Error('removeChild: not a child');
      this.children.splice(i, 1);
      return c;
    },
    descendants() {
      return this.children.flatMap((c) => [c, ...c.descendants()]);
    },
    querySelectorAll(sel) {
      if (sel === 'input:checked') {
        return this.descendants().filter((e) => e.tagName === 'input' && e.checked);
      }
      return [];
    },
    querySelector() { return null; },
    fire(type) { return Promise.all((this.listeners[type] || []).map((f) => f({}))); },
  };
  return el;
}

function makeDocument() {
  const html = read('panel.html');
  const els = {};
  for (const m of html.matchAll(/<([a-z0-9]+)\b[^>]*\bid="([^"]+)"/g)) els[m[2]] = makeEl(m[2], m[1]);
  const radios = [...html.matchAll(/name="assetMode" value="([^"]+)"( checked)?/g)].map((m) => {
    const r = makeEl(null, 'input');
    r.value = m[1];
    r.checked = !!m[2];
    return r;
  });
  let ready = null;
  return {
    els,
    radios,
    body: makeEl('body', 'body'),
    getElementById: (id) => els[id] || null,
    createElement: (tag) => makeEl(null, tag),
    querySelector(sel) {
      if (sel === 'input[name="assetMode"]:checked') return radios.find((r) => r.checked) || null;
      const m = /^input\[name="assetMode"\]\[value="([^"]+)"\]$/.exec(sel);
      if (m) return radios.find((r) => r.value === m[1]) || null;
      return null;
    },
    querySelectorAll(sel) {
      return sel === 'input[name="assetMode"]' ? radios : [];
    },
    addEventListener(type, f) { if (type === 'DOMContentLoaded') ready = f; },
    ready: () => ready(),
  };
}

// An OffscreenCanvas that draws nothing: enough for clipCanvas/combined to run.
class FakeCanvas {
  constructor(w, h) { this.width = w; this.height = h; }
  getContext() {
    return new Proxy({}, {
      get: (t, k) => (k === 'measureText' ? (s) => ({ width: String(s).length * 10 })
        : k in t ? t[k] : () => {}),
      set: (t, k, v) => { t[k] = v; return true; },
    });
  }
  async convertToBlob() { return new Blob(['jpeg'], { type: 'image/jpeg' }); }
}

class FakeFileReader {
  readAsDataURL() {
    this.result = 'data:application/octet-stream;base64,AAAA';
    setTimeout(() => this.onload(), 0);
  }
}

const tick = () => new Promise((r) => setTimeout(r, 0));

// What a scripted content script answers to play a page that unloaded before
// it could answer: the panel's message port closes (chrome.runtime.lastError).
const PORT_CLOSED = Symbol('port closed');
async function settle() { for (let i = 0; i < 20; i++) await tick(); }

// ── one panel, freshly loaded ────────────────────────────────────────────────

// `respond(msg, tab)` plays the content script: return a value, or a promise
// to hold the answer back. `host` makes the archive connection answer.
// `seenIt` is the Seen It add-on: { 'img:…': 'filed' | 'captured' |
// { state, sid } } it knows, or undefined when it is not installed.
// `fullPage` replaces the newspaper page download (to hold it back, or to
// say it met a "slow down": return { rateHits }).
// `fetch` is the network the panel itself reaches (Find a Grave's photos):
// every request is recorded in `fetched`; without it, any request fails.
// `waitReady: false` returns as soon as the panel starts (its first read of
// the page may still be on its way); `p.ready` settles when it is done.
// `storage` is what chrome.storage.local holds when the panel opens (the
// batch switch, its ledgers); it is kept, so `p.storage` shows what the
// panel wrote. Without it the store starts empty, as on a fresh install.
// `beforePanel(window)` runs after the libraries load, before panel.js.
// `titleFor(url)` is the title a tab shows once a batch sends it to `url`.
// `sharedStore` is a chrome.storage.local two panels (two windows) share, used
// as it is; `locks` is a navigator.locks they share (makeLocks). Without
// `locks` the page has no Web Locks, as an older browser would not.
async function openPanel({ url, respond, host, seenIt, fullPage, fetch, storage, beforePanel, titleFor,
  sharedStore, locks, waitReady = true }) {
  const getHolds = new Map(); // tab id -> promise the next tabs.get answer waits on
  const doc = makeDocument();
  const tab = { id: 7, url };
  // Every tab in the window, and the one in front (openTab / activate).
  const tabs = new Map([[tab.id, tab]]);
  let active = tab;
  const sentTo = []; // the tab id each message in `sent` went to
  const sent = [];
  const written = [];
  const filed = [];
  const writeFails = new Set();
  const sendFails = new Set();
  const onUpdated = []; // every tabs.onUpdated listener (the panel's, a batch's)
  const fireUpdated = (id, info) => Promise.all(onUpdated.slice().map((f) => f(id, info)));
  const external = [];
  // chrome.storage.local, kept as JSON (as Chrome keeps it).
  const store = sharedStore || JSON.parse(JSON.stringify(storage || {}));
  // A write the browser refuses: storageFault(key) gives the reason, or null.
  let storageFault = null;
  const storageReads = []; // the keys of every get
  const navigations = []; // { id, url } of every tabs.update
  const downloads = []; // chrome.downloads.download calls (the ledger's reading copy)
  const storageListeners = []; // chrome.storage.onChanged
  const chrome = {
    runtime: {
      lastError: undefined,
      // Another extension (Seen It): answers from `seenIt`, else "not there".
      sendMessage(extId, msg, cb) {
        external.push({ extId, msg });
        setTimeout(() => {
          if (!seenIt) {
            chrome.runtime.lastError = { message: 'Could not establish connection. Receiving end does not exist.' };
            cb(undefined);
            chrome.runtime.lastError = undefined;
            return;
          }
          const keys = {};
          const sids = {};
          for (const k of msg.keys || []) {
            if (!(k in seenIt)) continue;
            const v = seenIt[k];
            keys[k] = typeof v === 'string' ? v : v.state;
            if (v && v.sid) sids[k] = v.sid;
          }
          cb({ ok: true, keys, sids });
        }, 0);
      },
    },
    tabs: {
      query: (q, cb) => cb([active]),
      get: (id, cb) => {
        if (getHolds.has(id)) {
          const held = getHolds.get(id);
          getHolds.delete(id);
          held.then(() => cb(tabs.get(id)));
          return undefined;
        }
        if (tabs.has(id)) return cb(tabs.get(id));
        chrome.runtime.lastError = { message: 'No tab with id: ' + id + '.' };
        cb(undefined);
        chrome.runtime.lastError = undefined;
        return undefined;
      },
      sendMessage: (id, msg, cb) => {
        sent.push(msg);
        sentTo.push(id);
        Promise.resolve(respond(msg, tabs.get(id))).then((r) => setTimeout(() => {
          if (r !== PORT_CLOSED) {
            cb(r);
            return;
          }
          // The page went away before it answered, as Chrome reports it.
          chrome.runtime.lastError = { message: 'The message port closed before a response was received.' };
          cb(undefined);
          chrome.runtime.lastError = undefined;
        }, 0));
      },
      onUpdated: {
        addListener: (f) => { onUpdated.push(f); },
        removeListener: (f) => {
          const i = onUpdated.indexOf(f);
          if (i >= 0) onUpdated.splice(i, 1);
        },
      },
      // The tab loads the address it is sent to, and says so when done.
      update: (id, props, cb) => {
        navigations.push({ id, url: props.url });
        const t = tabs.get(id);
        if (!t) {
          chrome.runtime.lastError = { message: 'No tab with id: ' + id + '.' };
          if (cb) cb(undefined);
          chrome.runtime.lastError = undefined;
          return;
        }
        t.url = props.url;
        t.title = titleFor ? titleFor(props.url) : t.title;
        if (cb) cb(t);
        setTimeout(() => fireUpdated(id, { status: 'complete' }), 0);
      },
    },
    scripting: { executeScript: async () => {} },
    storage: {
      local: {
        get: (keys, cb) => {
          const out = {};
          if (typeof keys === 'string') keys = [keys];
          if (Array.isArray(keys)) {
            storageReads.push(...keys);
            for (const k of keys) if (k in store) out[k] = JSON.parse(JSON.stringify(store[k]));
          } else {
            storageReads.push(...Object.keys(keys || {}));
            for (const [k, d] of Object.entries(keys || {})) {
              out[k] = k in store ? JSON.parse(JSON.stringify(store[k])) : d;
            }
          }
          cb(out);
        },
        set: (obj, cb) => {
          const why = storageFault && Object.keys(obj).map((k) => storageFault(k)).find(Boolean);
          if (why) {
            chrome.runtime.lastError = { message: why };
            if (cb) cb();
            chrome.runtime.lastError = undefined;
            return;
          }
          const changes = {};
          for (const [k, v] of Object.entries(obj)) changes[k] = { oldValue: store[k], newValue: v };
          Object.assign(store, JSON.parse(JSON.stringify(obj)));
          if (cb) cb();
          for (const f of storageListeners.slice()) f(changes, 'local');
        },
        remove: (k, cb) => {
          const changes = {};
          for (const x of [].concat(k)) {
            changes[x] = { oldValue: store[x] };
            delete store[x];
          }
          if (cb) cb();
          for (const f of storageListeners.slice()) f(changes, 'local');
        },
      },
      onChanged: { addListener: (f) => storageListeners.push(f) },
    },
    downloads: {
      download: (opts, cb) => {
        downloads.push(opts);
        if (cb) setTimeout(() => cb(downloads.length), 0);
      },
      erase: (q, cb) => { if (cb) cb([]); },
    },
  };
  const fetched = [];
  const window = {
    FHA: {},
    fetch: async (u, init) => {
      fetched.push({ url: u, init });
      if (!fetch) throw new TypeError('Failed to fetch');
      return fetch(u, init);
    },
  };
  new Function('window', read('lib/capture-json.js'))(window);
  new Function('window', read('lib/findagrave.js'))(window);
  window.FHA.findagrave.pauseMs = () => 0;
  new Function('window', 'OffscreenCanvas', read('lib/newspapers.js'))(window, FakeCanvas);
  window.FHA.newspapers.fullPage = async (info) => {
    // `fullPage` may also say how many "slow down" answers the download
    // waited out ({ rateHits }), as lib/newspapers.js fullPage reports them.
    const said = fullPage ? await fullPage(info) : null;
    return { pageId: info.pageId, canvas: new FakeCanvas(info.pageWidth, info.pageHeight), reduced: 0, tiles: 1,
      rateHits: (said && said.rateHits) || 0 };
  };
  window.FHA.bundle = {
    base64ToBlob: (b, t) => new Blob([b], { type: t }),
    async writeBundle(spec) {
      if (writeFails.has(written.length + filed.length)) {
        writeFails.delete(written.length + filed.length);
        throw new Error('the download was blocked');
      }
      written.push(spec);
      return { dir: 'fha-inbox/' + spec.bundleName, filePath: '/dl/fha-inbox/' + spec.bundleName + '/page.html' };
    },
  };
  window.FHA.nativeHost = {
    hasPermission: async () => !!host,
    isAvailable: async () => !!host,
    async sendBundle(spec) {
      if (sendFails.has(written.length + filed.length)) {
        sendFails.delete(written.length + filed.length);
        throw new Error('Error when communicating with the native messaging host.');
      }
      filed.push(spec);
      return { stub: 'inbox/' + spec.bundleName };
    },
  };
  // The batch downloads' two scripts, loaded as panel.html loads them: they
  // do nothing until panel.js installs them (switched on only).
  new Function('window', read('lib/capture-batch.js'))(window);
  new Function('window', 'document', 'chrome', 'navigator', read('batch.js'))(window, doc, chrome,
    locks ? { locks } : {});
  if (beforePanel) beforePanel(window);
  // panel.js reads FHA as the page global that window.FHA is in a browser.
  // `crypto` is the page's Web Crypto (a global in a browser and in newer Node).
  new Function('window', 'FHA', 'document', 'chrome', 'FileReader', 'navigator', 'crypto', read('panel.js'))(
    window, window.FHA, doc, chrome, FakeFileReader, {}, webcrypto
  );
  const ready = doc.ready();
  if (waitReady) {
    await ready;
    await settle();
  }
  const $ = (id) => doc.els[id];
  let nextTabId = 8;
  return {
    doc, $, tab, sent, sentTo, written, filed, writeFails, sendFails, external, fetched, ready,
    storage: store, storageReads, navigations, downloads, window, chrome,
    setStorageFault(fn) { storageFault = fn; },
    // The batch box's element named `name` (its data-batch tag), or undefined.
    batchEl: (name) => doc.body.descendants().concat([doc.body]).find((e) => e.dataset && e.dataset.batch === name),
    // Hold the next tabs.get answer for tab `id` until the returned release().
    holdNextGet(id) {
      let release = null;
      getHolds.set(id, new Promise((r) => { release = r; }));
      return () => release();
    },
    async navigate(newUrl) {
      tab.url = newUrl;
      await fireUpdated(tab.id, { status: 'complete' });
      await tick();
    },
    // Another tab, opened in front of the panel's own (she clicked to it).
    openTab(newUrl) {
      const t = { id: nextTabId++, url: newUrl };
      tabs.set(t.id, t);
      active = t;
      return t;
    },
    activate(t) { active = t; },
    // A tab other than the panel's own loading a new page.
    async navigateTab(t, newUrl) {
      t.url = newUrl;
      await fireUpdated(t.id, { status: 'complete' });
      await tick();
    },
    // The two Re-read buttons (each holds its "reading…" a moment on purpose).
    async reread() {
      await $('btn-rescrape').fire('click');
      await settle();
    },
    async rereadFile() {
      await $('btn-rescrape-file').fire('click');
      await settle();
    },
    // The fetch log under the automatic-saving box, one string per line.
    log: () => $('auto-log').children.map((li) => (li.children.length
      ? li.children.map((c) => c.textContent).filter(Boolean).join(' ') : li.textContent)),
    autoBox: () => ({ cls: $('auto-box').className, text: $('auto-text').textContent }),
    async capture() {
      await $('btn-capture').fire('click');
      await settle();
    },
    result: () => $('stage-result').textContent,
    resultClass: () => $('stage-result').className,
    capJson: (spec) => spec.captureJson,
    files: (spec) => spec.assets.map((a) => a.filename),
    tickClip(title) {
      const label = $('np-clips').children.find(
        (c) => c.children && c.children[1] && c.children[1].textContent.startsWith(title));
      assert.ok(label, 'no checkbox for ' + title);
      label.children[0].checked = true;
      return label.children[0].fire('change');
    },
  };
}

// Wrap a scripted content script so the FIRST `action` message waits until
// release() - the moment to move the tab while a capture is in flight. Later
// messages answer at once. The answer is computed when released, from the tab
// as it is THEN (a content script answers from the page it is on).
function holdFirst(respond, action) {
  const h = { held: false, release: null, used: false };
  h.respond = (msg, tab) => {
    if (msg.action !== action || h.used) return respond(msg, tab);
    h.used = true;
    h.held = true;
    return new Promise((resolve) => {
      h.release = () => {
        h.held = false;
        resolve(respond(msg, tab));
      };
    });
  };
  return h;
}

// navigator.locks as the panel windows of one browser share it: `request`
// with { ifAvailable: true } answers null while another holds the name, and
// the lock is let go when the callback's promise settles.
function makeLocks() {
  const held = new Set();
  return {
    held,
    async request(name, opts, cb) {
      if (typeof opts === 'function') {
        cb = opts;
        opts = {};
      }
      if (held.has(name)) {
        if (opts && opts.ifAvailable) return cb(null);
        throw new Error('this model only answers ifAvailable requests');
      }
      held.add(name);
      try {
        return await cb({ name, mode: 'exclusive' });
      } finally {
        held.delete(name);
      }
    },
  };
}

module.exports = { openPanel, settle, tick, read, holdFirst, PORT_CLOSED, makeLocks };
