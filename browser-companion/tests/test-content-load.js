// content.js as Chrome runs it: the whole file loaded into a sandbox page and
// driven through its own message listener. The lib tests cover the copied
// blocks; this covers what only the whole file can show - that it loads at all
// (a block reading a constant declared further down fails here, as it would
// on every page in the browser), and that its FamilySearch wiring passes the
// signed-in session only where the login rule says.
//
//   node --test browser-companion/tests/test-content-load.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SRC = fs.readFileSync(path.join(__dirname, '..', 'src', 'content.js'), 'utf8');
const FIX = JSON.parse(fs.readFileSync(path.join(__dirname, '..', '..', 'tests', 'fixtures',
  'capture-samples', 'familysearch-gedcomx.json'), 'utf8'));
const TOKEN = 'fs-session-SECRET-0000';

// A page with nothing on it but what a test hands in: its address, its
// cookie, the links it shows, and how the network answers.
function loadPage({ url, cookie = '', links = [], fetch } = {}) {
  const listeners = [];
  const el = (tag) => ({ tagName: tag, textContent: '', innerText: '', getAttribute: () => null, closest: () => null });
  const document = {
    title: 'A page',
    cookie,
    doctype: null,
    body: el('BODY'),
    documentElement: { outerHTML: '<html></html>', cloneNode: () => ({ querySelectorAll: () => [], outerHTML: '<html></html>' }) },
    scripts: [],
    images: [],
    querySelector: () => null,
    querySelectorAll: (sel) => (/3:1:/.test(sel) ? links : []),
    getElementById: () => null,
  };
  const calls = [];
  const sandbox = {
    document,
    location: new URL(url),
    navigator: { userAgent: 'test' },
    chrome: {
      runtime: { getManifest: () => ({ version: '9.9.9' }), onMessage: { addListener: (f) => listeners.push(f) } },
    },
    fetch: async (u, init) => {
      calls.push({ url: String(u), auth: !!(init && init.headers && init.headers.Authorization), init });
      return fetch(String(u), init);
    },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
    URL, Blob, TextDecoder, setTimeout, clearTimeout, console,
    FileReader: class {
      readAsDataURL(b) {
        b.arrayBuffer().then((buf) => {
          this.result = 'data:image/jpeg;base64,' + Buffer.from(buf).toString('base64');
          this.onload();
        });
      }
    },
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(SRC, sandbox, { filename: 'content.js' });
  assert.strictEqual(listeners.length, 1, 'content.js registers one listener');
  const ask = (msg) => new Promise((resolve) => {
    let sync;
    const r = listeners[0](msg, {}, (resp) => {
      sync = resp;
      resolve(JSON.parse(JSON.stringify(resp)));
    });
    if (r !== true && sync === undefined) resolve(undefined);
  });
  return { ask, calls };
}

// FamilySearch as the fixture answers it; `over` rewrites answers by address.
function familysearch(over = {}) {
  return async (url) => {
    const o = Object.keys(over).find((k) => url.indexOf(k) >= 0);
    if (o) return over[o];
    const j = (x) => ({ ok: true, status: 200, text: async () => JSON.stringify(x) });
    let m = /\/ark:\/61903\/1:1:([A-Z0-9-]+)$/.exec(url);
    if (m && FIX.records[m[1]]) return j(FIX.records[m[1]]);
    m = /\/ark:\/61903\/3:1:([A-Z0-9-]+)$/.exec(url);
    if (m && FIX.images[m[1]]) return j(FIX.images[m[1]]);
    if (/dist\.jpg$/.test(url)) {
      return { ok: true, status: 200, headers: { get: () => 'image/jpeg' },
        blob: async () => new Blob([Buffer.alloc(40 * 1024)], { type: 'image/jpeg' }) };
    }
    return { ok: false, status: 404, text: async () => '' };
  };
}

const REC = 'https://www.familysearch.org/ark:/61903/1:1:HRT1-CEN1';
const VIEWER = 'https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG1';

test('content.js loads, and answers a prefill on an ordinary page', async () => {
  const page = loadPage({ url: 'https://example.org/records/1', fetch: familysearch() });
  const r = await page.ask({ action: 'prefill' });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.prefill.familysearchRecord, false);
  assert.strictEqual(r.prefill.familysearchImageViewer, false);
});

test('prefill knows a FamilySearch record, the viewer opened from one, and a bare viewer', async () => {
  let r = await loadPage({ url: REC, fetch: familysearch() }).ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.familysearchRecord, true);
  assert.strictEqual(r.prefill.familysearchImageViewer, false);
  r = await loadPage({ url: VIEWER + '?view=index&personArk=%2Fark%3A%2F61903%2F1%3A1%3AHRT1-CEN1', fetch: familysearch() })
    .ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.familysearchRecord, true, 'opened from a record: captured as that record');
  r = await loadPage({ url: VIEWER, fetch: familysearch() }).ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.familysearchRecord, false);
  assert.strictEqual(r.prefill.familysearchImageViewer, true);
  assert.strictEqual(r.prefill.familysearchImageId, 'HRT1-IMG1');
});

test('a film viewer\'s image is the ONE image it links to; Seen It\'s badges are not its links', async () => {
  const link = (href, inSeenIt) => ({ getAttribute: (a) => (a === 'href' ? href : null), closest: () => (inSeenIt ? {} : null) });
  const film = 'https://www.familysearch.org/search/film/000000?cat=1&i=3';
  let r = await loadPage({ url: film, links: [link('/ark:/61903/3:1:HRT1-IMG1?i=3')], fetch: familysearch() })
    .ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.familysearchImageId, 'HRT1-IMG1');
  r = await loadPage({ url: film, links: [link('/ark:/61903/3:1:HRT1-IMG1'), link('/ark:/61903/3:1:HRT1-IMG3')], fetch: familysearch() })
    .ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.familysearchImageViewer, false, 'two images is not an identification');
  r = await loadPage({ url: film, links: [link('/ark:/61903/3:1:HRT1-IMG1'), link('/ark:/61903/3:1:HRT1-IMG3', true)], fetch: familysearch() })
    .ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.familysearchImageId, 'HRT1-IMG1');
});

test('the record is read with the signed-in session, and the session is in nothing it returns', async () => {
  const page = loadPage({ url: REC, cookie: 'other=1; fssessionid=' + encodeURIComponent(TOKEN), fetch: familysearch() });
  const info = await page.ask({ action: 'familysearchRecordInfo' });
  assert.strictEqual(info.ok, true);
  assert.strictEqual(info.record.name, 'Calvin Hartley');
  assert.deepStrictEqual(page.calls.map((c) => [c.url, c.auth]), [[REC, true]]);
  assert.strictEqual(page.calls[0].init.headers.Authorization, 'Bearer ' + TOKEN);
  assert.ok(!JSON.stringify(info).includes(TOKEN));
  const check = await page.ask({ action: 'familysearchImageCheck' });
  assert.deepStrictEqual([check.ok, check.available, check.imageId, check.title],
    [true, true, 'HRT1-IMG1', 'Calvin Hartley - United States, Census, 1880']);
  const img = await page.ask({ action: 'familysearchImage', imageId: 'HRT1-IMG1' });
  assert.strictEqual(img.ok, true);
  assert.ok(!JSON.stringify([check, img]).includes(TOKEN));
});

test('an image FamilySearch points elsewhere is fetched without the session', async () => {
  const page = loadPage({ url: REC, cookie: 'fssessionid=' + TOKEN, fetch: familysearch() });
  await page.ask({ action: 'familysearchImage', imageId: 'HRT1-EVIL' });
  const away = page.calls.filter((c) => /evil\.com/.test(c.url));
  assert.strictEqual(away.length, 1);
  assert.strictEqual(away[0].auth, false);
  assert.ok(!JSON.stringify(away[0].init || {}).includes(TOKEN));
});

test('signed out: no session is sent, and the refusal says to sign in', async () => {
  const page = loadPage({ url: REC, cookie: '', fetch: familysearch({ '1:1:': { ok: false, status: 403, text: async () => '' } }) });
  const info = await page.ask({ action: 'familysearchRecordInfo' });
  assert.strictEqual(page.calls[0].auth, false);
  assert.match(info.error, /asked you to sign in \(HTTP 403\)/);
});
