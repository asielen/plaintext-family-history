// The capture panel driven end to end on Newspapers.com pages, without a
// browser: panel.js runs against a small stand-in for the panel's page (every
// element panel.html declares), a scripted content script, and recording
// stand-ins for the download and archive-connection paths.
//
// What it guards, each in the human's terms:
//   - the panel opens at all (on any page, not only Newspapers.com);
//   - moving the tab to another clipping never saves the previous page's
//     clippings under the new page's details;
//   - when several clippings are saved and one fails, or they land in
//     different places, the closing message says where each one went;
//   - a second clipping ticked on a clipping page does not carry a copy of
//     the first clipping's web page;
//   - a clipping with nothing to cut out says so, instead of failing late.
//
// The image work itself (tiles, canvases) is stubbed: lib/newspapers.js's own
// tests cover the words and the tile logic, and only a real browser can cover
// the pixels (see the PR's hand-test checklist).
//
//   node --test browser-companion/tests/test-panel-newspapers.js

'use strict';

const { test } = require('node:test');
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
async function settle() { for (let i = 0; i < 20; i++) await tick(); }

// ── one panel, freshly loaded ────────────────────────────────────────────────

// `respond(msg)` plays the content script: return a value, or a promise to
// hold the answer back. `host` makes the archive connection answer.
async function openPanel({ url, respond, host }) {
  const doc = makeDocument();
  const tab = { id: 7, url };
  const sent = [];
  const written = [];
  const filed = [];
  const writeFails = new Set();
  const sendFails = new Set();
  let onUpdated = null;
  const chrome = {
    runtime: { lastError: undefined },
    tabs: {
      query: (q, cb) => cb([tab]),
      get: (id, cb) => cb(tab),
      sendMessage: (id, msg, cb) => {
        sent.push(msg);
        Promise.resolve(respond(msg, tab)).then((r) => setTimeout(() => cb(r), 0));
      },
      onUpdated: { addListener: (f) => { onUpdated = f; } },
    },
    scripting: { executeScript: async () => {} },
    storage: { local: { get: (d, cb) => cb(d), set: () => {} } },
  };
  const window = { FHA: {} };
  new Function('window', read('lib/capture-json.js'))(window);
  new Function('window', 'OffscreenCanvas', read('lib/newspapers.js'))(window, FakeCanvas);
  window.FHA.newspapers.fullPage = async (info) => ({
    pageId: info.pageId, canvas: new FakeCanvas(info.pageWidth, info.pageHeight), reduced: 0, tiles: 1,
  });
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
    doc, $, tab, sent, written, filed, writeFails, sendFails,
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

// ── fictional Newspapers.com pages (the Hartley family, invented ids) ────────

const ME = { id: 7, username: 'HartleyResearch' };

function clipObj(id, title, extra) {
  return Object.assign({
    id, pageId: 5550001, title, tags: '', userId: ME.id, username: ME.username,
    rects: [{ x: 10, y: 20, w: 300, h: 400, rotation: 0 }],
    pageDate: '1931-03-03', pageNo: '5',
    publication: { name: 'The Fairview Gazette', location: 'Fairview, Kansas' },
    url: 'https://www.newspapers.com/article/the-fairview-gazette-' + id + '/' + id + '/',
  }, extra || {});
}

function npInfo(kind, clips, extra) {
  return Object.assign({
    ok: true, kind, focusClipId: kind === 'article' && clips.length ? clips[0].id : null,
    pageId: 5550001, pageWidth: 3000, pageHeight: 4000, token: 'eyJ.test.pass', me: ME,
    publication: { name: 'The Fairview Gazette', location: 'Fairview, Kansas' },
    pageDate: '1931-03-03', pageNo: '5', ownClips: clips,
    unusable: [], openedUnusable: false, otherClipCount: 0, focusOwner: null,
  }, extra || {});
}

function prefillFor(url, title) {
  return {
    url, title, sourceDate: '', repository: '', people: [], recipeHint: 'newspapers',
    newspapers: /newspapers\.com/.test(url),
  };
}

// The content script for a set of pages: { [url]: { title, info } }, where
// info may be a function returning a promise (an answer held back).
function scripted(pages) {
  return (msg, tab) => {
    const page = pages[tab.url];
    switch (msg.action) {
      case 'prefill': return { ok: true, prefill: prefillFor(tab.url, page.title) };
      case 'newspapersInfo': return typeof page.info === 'function' ? page.info() : page.info;
      case 'pagehtml': return { ok: true, html: '<html><title>' + page.title + '</title></html>' };
      case 'singlefile': return { ok: true, html: '<html>copy of ' + page.title + '</html>' };
      case 'newspapersClipText': return { ok: true, ocr: { text: 'machine text', complete: true } };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

function openOn(url, pages, opts) {
  return openPanel(Object.assign({ url, respond: scripted(pages) }, opts || {}));
}

const URL_A = 'https://www.newspapers.com/article/the-fairview-gazette-obituary/101/';
const URL_B = 'https://www.newspapers.com/article/the-fairview-gazette-wedding/202/';
const URL_PAGE = 'https://www.newspapers.com/image/5550001/';

// ── the panel opens ──────────────────────────────────────────────────────────

test('the panel opens and describes the capture on an ordinary page', async () => {
  const p = await openPanel({
    url: 'https://example.org/records/1',
    respond: (m) => (m.action === 'prefill'
      ? { ok: true, prefill: { url: 'https://example.org/records/1', title: 'A record', people: [] } }
      : { ok: false }),
  });
  assert.match(p.$('asset-status').textContent, /Whole-page copy/);
  assert.strictEqual(p.$('np-note').style.display, 'none');
});

test('the panel opens on a Newspapers.com clipping and offers it', async () => {
  const p = await openOn(URL_A, {
    [URL_A]: { title: 'Obituary', info: npInfo('article', [clipObj(101, 'Obituary for Calvin Hartley')]) },
  });
  assert.strictEqual(p.$('f-asset-url').value, 'Newspapers.com page + clipping (saved automatically)');
  assert.match(p.$('asset-status').textContent, /whole page \+ 1 clipping/);
  assert.match(p.$('f-title').value, /^Obituary for Calvin Hartley - The Fairview Gazette/);
});

// ── moving to another record ─────────────────────────────────────────────────

test('Capture waits while the next page is read, and never saves the last page\'s clipping', async () => {
  let releaseB;
  const pages = {
    [URL_A]: { title: 'Obituary', info: npInfo('article', [clipObj(101, 'Obituary for Calvin Hartley')]) },
    [URL_B]: {
      title: 'Wedding',
      info: () => new Promise((r) => {
        releaseB = () => r(npInfo('article', [clipObj(202, 'Hartley - Webb wedding')]));
      }),
    },
  };
  const p = await openOn(URL_A, pages);
  await p.navigate(URL_B);
  // B's clippings are still being read: A's must already be gone.
  await p.capture();
  assert.strictEqual(p.written.length, 0, 'nothing may be staged while the new page is read');
  assert.match(p.result(), /Still reading your clippings/);

  releaseB();
  await settle();
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  const cap = p.capJson(p.written[0]);
  assert.match(cap.title, /^Hartley - Webb wedding/);
  assert.match(cap.url, /\/202\/$/);
});

test('a late answer for the page the tab left is dropped', async () => {
  let releaseA;
  const pages = {
    [URL_A]: {
      title: 'Obituary',
      info: () => new Promise((r) => {
        releaseA = () => r(npInfo('article', [clipObj(101, 'Obituary for Calvin Hartley')]));
      }),
    },
    [URL_B]: { title: 'Wedding', info: npInfo('article', [clipObj(202, 'Hartley - Webb wedding')]) },
  };
  const p = await openOn(URL_A, pages);
  await p.navigate(URL_B);
  await settle();
  releaseA(); // A's reply arrives after B's
  await settle();
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.match(p.capJson(p.written[0]).title, /^Hartley - Webb wedding/);
});

// ── several clippings: say where every one went ──────────────────────────────

const twoOnPage = () => ({
  [URL_PAGE]: {
    title: 'The Fairview Gazette, page 5',
    info: npInfo('image', [
      clipObj(301, 'Hartley farm sale'),
      clipObj(302, 'Ethel Hartley visits Topeka'),
    ]),
  },
});

test('one clipping failing after another was saved names both, and keeps only the failed one ticked', async () => {
  const p = await openOn(URL_PAGE, twoOnPage());
  p.writeFails.add(1); // the second bundle's download is blocked
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  const text = p.result();
  assert.match(text, /Saved 1 of 2 clippings/);
  assert.match(text, /"Hartley farm sale[^"]*": staged to \/dl\/fha-inbox\//);
  assert.match(text, /"Ethel Hartley visits Topeka[^"]*": NOT saved - the download was blocked/);
  assert.match(text, /still ticked/);
  assert.match(p.resultClass(), /warn/);
  assert.ok(p.$('handoff').classList.contains('show'));

  // Capture again: only the one that failed is staged, never a duplicate.
  await p.capture();
  assert.strictEqual(p.written.length, 2);
  assert.match(p.capJson(p.written[1]).title, /^Ethel Hartley visits Topeka/);
});

test('bundles that land in different places are each reported where they went', async () => {
  const p = await openOn(URL_PAGE, twoOnPage(), { host: true });
  p.sendFails.add(1); // the archive connection drops for the second bundle
  await p.capture();
  assert.strictEqual(p.filed.length, 1);
  assert.strictEqual(p.written.length, 1);
  const text = p.result();
  assert.match(text, /Saved all 2 clippings/);
  assert.match(text, /"Hartley farm sale[^"]*": filed straight into your archive \(inbox\//);
  assert.match(text, /"Ethel Hartley visits Topeka[^"]*": staged to \/dl\/fha-inbox\//);
  assert.match(text, /saved to Downloads instead/);
  assert.ok(p.$('handoff').classList.contains('show'), 'the Downloads one still needs the sweep');
});

// ── the page copy belongs to the clipping on screen ──────────────────────────

test('on a clipping page, a second ticked clipping gets no copy of the first one\'s page', async () => {
  const pages = {
    [URL_A]: {
      title: 'Obituary',
      info: npInfo('article', [
        clipObj(101, 'Obituary for Calvin Hartley'),
        clipObj(102, 'Card of thanks from the Hartley family'),
      ]),
    },
  };
  const p = await openOn(URL_A, pages);
  assert.ok(p.$('cb-pagecopy').checked, 'page copy is on by default');
  await p.tickClip('Card of thanks');
  await p.capture();
  assert.strictEqual(p.written.length, 2);
  assert.strictEqual(p.sent.filter((m) => m.action === 'singlefile').length, 1);
  const [opened, sibling] = p.written;
  assert.ok(p.files(opened).includes('page-snapshot.html'));
  assert.ok(!p.files(sibling).includes('page-snapshot.html'));
  assert.deepStrictEqual(p.capJson(sibling).assets.map((a) => a.role),
    ['clipping', 'page', 'annotated', 'site-data']);
  assert.match(p.capJson(sibling).notes, /No copy of the web page was kept for this clipping/);
  assert.doesNotMatch(p.capJson(opened).notes, /No copy of the web page/);
  // The closing message says so too: the status line had promised a page copy.
  assert.match(p.result(), /went only with the clipping open in the browser; the other clipping was saved without one/);
});

test('on the page viewer, the page copy is the page every clipping was cut from', async () => {
  const p = await openOn(URL_PAGE, twoOnPage());
  await p.capture();
  assert.strictEqual(p.written.length, 2);
  for (const b of p.written) assert.ok(p.files(b).includes('page-snapshot.html'));
});

// ── nothing to cut out ───────────────────────────────────────────────────────

test('a clipping with no marked area says so and saves nothing automatically', async () => {
  const pages = {
    [URL_A]: {
      title: 'Obituary',
      info: npInfo('article', [], {
        focusClipId: 101,
        unusable: [{ id: 101, title: 'Obituary for Calvin Hartley' }], openedUnusable: true,
      }),
    },
  };
  const p = await openOn(URL_A, pages);
  assert.match(p.$('np-text').textContent, /marks no area on the newspaper page/);
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /marks no area/);
});

// ── #211 review and audit ────────────────────────────────────────────────────

test('moving from a clipping to an ordinary record keeps that record\'s file address', async () => {
  // Leaving Newspapers.com cleared the address box AFTER the next record's
  // detected image had been put in it, so Capture had nothing to save.
  const OTHER = 'https://example.org/records/9';
  const respond = (msg, tab) => {
    if (tab.url === OTHER) {
      if (msg.action === 'prefill') {
        return { ok: true, prefill: { url: OTHER, title: 'A record', people: [],
          imageUrl: 'https://example.org/scans/9.jpg' } };
      }
      return { ok: false };
    }
    return scripted({ [URL_A]: { title: 'Obituary',
      info: npInfo('article', [clipObj(101, 'Obituary for Calvin Hartley')]) } })(msg, tab);
  };
  const p = await openPanel({ url: URL_A, respond });
  await p.navigate(OTHER);
  await settle();
  assert.strictEqual(p.$('f-asset-url').value, 'https://example.org/scans/9.jpg');
  assert.strictEqual(p.$('f-asset-url').readOnly, false);
});

test('a refused clipping list is reported as a refusal, not as "none are yours"', async () => {
  const p = await openOn(URL_PAGE, {
    [URL_PAGE]: { title: 'The Fairview Gazette, page 5', info: npInfo('image', [], {
      listError: 'Newspapers.com would not send the list of clippings on this page (HTTP 429)',
    }) },
  });
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /HTTP 429/);
  assert.doesNotMatch(p.result(), /none of the clippings on this page are yours/);
});

test('a title typed while the clippings load is kept', async () => {
  let release;
  const p = await openOn(URL_A, {
    [URL_A]: { title: 'Obituary', info: () => new Promise((r) => {
      release = () => r(npInfo('article', [clipObj(101, 'Obituary for Calvin Hartley')]));
    }) },
  });
  p.$('f-title').value = 'Calvin Hartley obituary (corrected)';
  await p.$('f-title').fire('input');
  release();
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Calvin Hartley obituary (corrected)');
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.capJson(p.written[0]).title, 'Calvin Hartley obituary (corrected)');
});

test('Capture waits while the tab is moving to the next clipping', async () => {
  // The panel had not yet read the new address, so Capture paired clipping
  // A's images with clipping B's page and form.
  let releasePrefill;
  const pages = {
    [URL_A]: { title: 'Obituary', info: npInfo('article', [clipObj(101, 'Obituary for Calvin Hartley')]) },
    [URL_B]: { title: 'Wedding', info: npInfo('article', [clipObj(202, 'Hartley - Webb wedding')]) },
  };
  const base = scripted(pages);
  const respond = (msg, tab) => {
    if (msg.action === 'prefill' && tab.url === URL_B && !releasePrefill) {
      return new Promise((r) => { releasePrefill = () => r(base(msg, tab)); });
    }
    return base(msg, tab);
  };
  const p = await openPanel({ url: URL_A, respond });
  p.tab.url = URL_B;
  const nav = p.navigate(URL_B);
  await settle();
  await p.capture();
  assert.strictEqual(p.written.length, 0, 'nothing may be staged while the tab moves');
  assert.match(p.result(), /page in the tab just changed/);
  releasePrefill();
  await nav;
  await settle();
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.match(p.capJson(p.written[0]).title, /^Hartley - Webb wedding/);
});

// ── #211 audit, round 2 ──────────────────────────────────────────────────────

function setTick(p, title, on) {
  const label = p.$('np-clips').children.find(
    (c) => c.children && c.children[1] && c.children[1].textContent.startsWith(title));
  assert.ok(label, 'no checkbox for ' + title);
  label.children[0].checked = on;
  return label.children[0].fire('change');
}

test('a correction typed for one clipping is not filed on the next one picked', async () => {
  const p = await openOn(URL_PAGE, twoOnPage());
  await setTick(p, 'Ethel Hartley visits Topeka', false);
  p.$('f-title').value = 'Hartley farm sale notice';
  await p.$('f-title').fire('input');
  p.$('f-date').value = '1931-03-04';
  await p.$('f-date').fire('input');
  await setTick(p, 'Hartley farm sale', false);
  await setTick(p, 'Ethel Hartley visits Topeka', true);
  assert.match(p.$('f-title').value, /^Ethel Hartley visits Topeka/);
  assert.strictEqual(p.$('f-date').value, '1931-03-03');
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.match(p.capJson(p.written[0]).title, /^Ethel Hartley visits Topeka/);
});

test('with several clippings ticked the form shows only what will be saved', async () => {
  const p = await openOn(URL_PAGE, twoOnPage());
  await setTick(p, 'Ethel Hartley visits Topeka', false);
  p.$('f-date').value = '1931-03-04';
  await p.$('f-date').fire('input');
  await setTick(p, 'Ethel Hartley visits Topeka', true);
  assert.strictEqual(p.$('f-date').value, '1931-03-03');
  assert.strictEqual(p.$('f-date').readOnly, true);
  assert.strictEqual(p.$('f-title').readOnly, true);
  await setTick(p, 'Ethel Hartley visits Topeka', false);
  assert.strictEqual(p.$('f-date').readOnly, false);
});

test('an opened clipping that cannot be cut out does not quietly save another one', async () => {
  const pages = {
    [URL_A]: {
      title: 'Obituary',
      info: npInfo('article', [clipObj(102, 'Card of thanks')], {
        focusClipId: 101,
        unusable: [{ id: 101, title: 'Obituary for Calvin Hartley' }], openedUnusable: true,
      }),
    },
  };
  const p = await openOn(URL_A, pages);
  await p.capture();
  assert.strictEqual(p.written.length, 0, 'nothing is saved that she did not ask for');
  await setTick(p, 'Card of thanks', true);
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.match(p.capJson(p.written[0]).title, /^Card of thanks/);
});

test('a page with more clippings than the panel reads is not "none are yours"', async () => {
  const p = await openOn(URL_PAGE, {
    [URL_PAGE]: { title: 'The Fairview Gazette, page 1', info: npInfo('image', [], {
      listTruncated: true,
      listError: 'this page has more than 200 clippings, more than the panel reads',
    }) },
  });
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /more than 200 clippings/);
  assert.match(p.result(), /its own page/);
  assert.doesNotMatch(p.result(), /none of the clippings on this page are yours/);
});

test('on a clipping page, a corrected title does not follow to a newly ticked clipping', async () => {
  const p = await openOn(URL_A, {
    [URL_A]: { title: 'Obituary', info: npInfo('article', [
      clipObj(101, 'Obituary for Calvin Hartley'), clipObj(202, 'Hartley - Webb wedding')]) },
  });
  p.$('f-title').value = 'Corrected obituary';
  await p.$('f-title').fire('input');
  await setTick(p, 'Obituary for Calvin Hartley', false);
  await setTick(p, 'Hartley - Webb wedding', true);
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.match(p.capJson(p.written[0]).title, /^Hartley - Webb wedding/);
});

test('a corrected web address is kept for a single clipping', async () => {
  const p = await openOn(URL_A, {
    [URL_A]: { title: 'Obituary', info: npInfo('article', [clipObj(101, 'Obituary for Calvin Hartley')]) },
  });
  p.$('f-url').value = 'https://www.newspapers.com/clip/101/obituary-for-calvin-hartley/';
  await p.$('f-url').fire('input');
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.capJson(p.written[0]).url,
    'https://www.newspapers.com/clip/101/obituary-for-calvin-hartley/');
});

test('a partly read clipping list says some clippings may be missing', async () => {
  for (const extra of [
    { listError: 'Newspapers.com would not send the list of clippings on this page (HTTP 503)' },
    { listTruncated: true, listError: 'this page has more than 200 clippings, more than the panel reads' },
  ]) {
    const p = await openOn(URL_PAGE, {
      [URL_PAGE]: { title: 'The Fairview Gazette, page 5',
        info: npInfo('image', [clipObj(301, 'Hartley farm sale')], extra) },
    });
    assert.match(p.$('np-text').textContent, /some of your clippings on this page may not be listed/);
    await p.capture();
    assert.strictEqual(p.written.length, 1, 'the clipping it does know is still saved');
  }
});
