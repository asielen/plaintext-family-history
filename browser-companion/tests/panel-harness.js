// A stand-in for the capture panel's page, so tests can drive panel.js itself
// without a browser: every element panel.html declares, a scripted content
// script (`respond`), recording stand-ins for the download and the archive
// connection, and the Seen It add-on's one question. The image work
// (canvases) is stubbed; only a real browser can check pixels.
//
// Used by tests/test-panel-newspapers.js and tests/test-panel-ancestry.js.

'use strict';

const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

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
// `fullPage` replaces the newspaper page download (to hold it back).
// `fetch` is the network the panel itself reaches (Find a Grave's photos):
// every request is recorded in `fetched`; without it, any request fails.
async function openPanel({ url, respond, host, seenIt, fullPage, fetch }) {
  const doc = makeDocument();
  const tab = { id: 7, url };
  const sent = [];
  const written = [];
  const filed = [];
  const writeFails = new Set();
  const sendFails = new Set();
  let onUpdated = null;
  const external = [];
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
      query: (q, cb) => cb([tab]),
      get: (id, cb) => cb(tab),
      sendMessage: (id, msg, cb) => {
        sent.push(msg);
        Promise.resolve(respond(msg, tab)).then((r) => setTimeout(() => {
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
      onUpdated: { addListener: (f) => { onUpdated = f; } },
    },
    scripting: { executeScript: async () => {} },
    storage: { local: { get: (d, cb) => cb(d), set: () => {} } },
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
    if (fullPage) await fullPage(info);
    return { pageId: info.pageId, canvas: new FakeCanvas(info.pageWidth, info.pageHeight), reduced: 0, tiles: 1 };
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
  // panel.js reads FHA as the page global that window.FHA is in a browser.
  new Function('window', 'FHA', 'document', 'chrome', 'FileReader', 'navigator', read('panel.js'))(
    window, window.FHA, doc, chrome, FakeFileReader, {}
  );
  await doc.ready();
  await settle();
  const $ = (id) => doc.els[id];
  return {
    doc, $, tab, sent, written, filed, writeFails, sendFails, external, fetched,
    async navigate(newUrl) {
      tab.url = newUrl;
      await onUpdated(tab.id, { status: 'complete' });
      await tick();
    },
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

module.exports = { openPanel, settle, tick, read, holdFirst, PORT_CLOSED };
