// content.js as Chrome runs it: the whole file loaded into a sandbox page and
// driven through its own message listener. The lib tests cover the copied
// blocks; this covers what only the whole file can show - that it loads at all
// (a block reading a constant declared further down fails here, as it would
// on every page in the browser), that its FamilySearch wiring passes the
// signed-in session only where the login rule says, and that Find a Grave is
// read without any login at all.
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
const { parse } = require('./mini-dom.js');
const SAMPLE = (name) => fs.readFileSync(path.join(__dirname, '..', '..', 'tests', 'fixtures',
  'capture-samples', name + '.html'), 'utf8');

// A page with nothing on it but what a test hands in: its address, its
// cookie, the links it shows, how the network answers, and (`tree`, a
// tests/mini-dom.js page) the elements the Find a Grave readers walk.
function loadPage({ url, cookie = '', links = [], fetch, canonical, tree } = {}) {
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
    querySelector: (sel) => (canonical && /canonical|og:url/.test(sel)
      ? { getAttribute: (a) => (a === 'href' ? canonical : null) } : null),
    querySelectorAll: (sel) => (/3:1:/.test(sel) ? links : []),
    getElementById: () => null,
    childNodes: tree ? tree.childNodes : [],
  };
  if (tree) document.title = tree.title;
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
    DOMParser: class { parseFromString(html) { return parse(html); } },
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

// ── PR #214 review ───────────────────────────────────────────────────────────

const oneLink = (href) => [{ getAttribute: (a) => (a === 'href' ? href : null), closest: () => null }];

test('a page elsewhere that links one FamilySearch image is not a FamilySearch viewer (Codex 4500422479)', async () => {
  // A blog, a forum, a tree site: it keeps its own file, and nothing is asked of FamilySearch.
  for (const url of ['https://blog.example.org/our-hartley-family', 'https://www.ancestry.com/boards/hartley/1']) {
    const page = loadPage({ url, links: oneLink('https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG1'), fetch: familysearch() });
    const r = await page.ask({ action: 'prefill' });
    assert.strictEqual(r.prefill.familysearchImageViewer, false, url);
    assert.strictEqual(r.prefill.familysearchImageId, null, url);
    assert.strictEqual(page.calls.length, 0, url);
  }
  // A FamilySearch page that is not a viewer (a search results page) is not one either.
  const search = loadPage({ url: 'https://www.familysearch.org/search/record/results?q.surname=Hartley',
    links: oneLink('/ark:/61903/3:1:HRT1-IMG1'), fetch: familysearch() });
  assert.strictEqual((await search.ask({ action: 'prefill' })).prefill.familysearchImageViewer, false);
  // The film viewer still finds its one image.
  const film = loadPage({ url: 'https://www.familysearch.org/search/film/000000?i=3',
    links: oneLink('/ark:/61903/3:1:HRT1-IMG1'), fetch: familysearch() });
  assert.strictEqual((await film.ask({ action: 'prefill' })).prefill.familysearchImageId, 'HRT1-IMG1');
});

test('an unparseable canonical or og:url never fails the whole prefill (audit #7)', async () => {
  const page = loadPage({ url: 'https://www.familysearch.org/search/film/000000?i=3',
    canonical: 'https://[not a host/', links: oneLink('/ark:/61903/3:1:HRT1-IMG1'), fetch: familysearch() });
  const r = await page.ask({ action: 'prefill' });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.prefill.familysearchImageId, 'HRT1-IMG1');
});

test('an image request is never sent from a tab that has left the site (#214 sweep)', async () => {
  // A capture whose tab moved off FamilySearch (or Ancestry) mid-way asks the
  // content script there for the image: it refuses, and nothing goes to that host.
  let page = loadPage({ url: 'https://blog.example.org/x', cookie: 'fssessionid=' + TOKEN, fetch: familysearch() });
  let r = await page.ask({ action: 'familysearchImage', imageId: 'HRT1-IMG1' });
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /no longer on FamilySearch/);
  assert.strictEqual(page.calls.length, 0);
  page = loadPage({ url: 'https://blog.example.org/x', fetch: familysearch() });
  r = await page.ask({ action: 'ancestryImage', dbId: '90001', imageId: '4000123_00045' });
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /no longer on Ancestry/);
  assert.strictEqual(page.calls.length, 0);
});

// ── Find a Grave: no login ──────────────────────────────────────────────────

const MEMORIAL = 'https://www.findagrave.com/memorial/900000001/calvin-george-hartley';

// Find a Grave's gallery pages, as the fixture answers them.
function findagrave() {
  return async (url) => (/\/memorial\/900000001\/calvin-george-hartley\/photo$/.test(url)
    ? { ok: true, status: 200, text: async () => SAMPLE('findagrave-memorial-photos') }
    : { ok: false, status: 404, text: async () => '' });
}

test('prefill on a memorial starts from what the memorial says; its gallery page is not a memorial', async () => {
  const tree = parse(SAMPLE('findagrave-memorial'));
  let r = await loadPage({ url: MEMORIAL, tree, fetch: findagrave() }).ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.findagraveMemorial, true);
  assert.strictEqual(r.prefill.recipeHint, 'findagrave');
  assert.strictEqual(r.prefill.title, 'Calvin George Hartley (1842-1918) - Find a Grave memorial');
  assert.strictEqual(r.prefill.sourceDate, '1918-01-04');
  assert.strictEqual(r.prefill.sourceType, 'website');
  assert.strictEqual(r.prefill.repository, 'Find a Grave');
  assert.deepStrictEqual(r.prefill.people,
    ['Calvin George Hartley', 'Thomas Hartley', 'Harriet Frances Hartley', 'Warren Calvin Hartley']);
  for (const url of [MEMORIAL + '/photo', MEMORIAL + '/flower', 'https://www.findagrave.com/memorial/900000001/photo',
    'https://www.findagrave.com/cemetery/9990001/fairview-cemetery']) {
    r = await loadPage({ url, tree, fetch: findagrave() }).ask({ action: 'prefill' });
    assert.strictEqual(r.prefill.findagraveMemorial, false, url);
  }
  // a memorial page that will not read is still a memorial: Capture says why
  r = await loadPage({ url: MEMORIAL, fetch: findagrave() }).ask({ action: 'prefill' });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.prefill.findagraveMemorial, true);
  assert.strictEqual(r.prefill.title, 'A page');
});

test('the memorial is read from the page, with its hints and title', async () => {
  const page = loadPage({ url: MEMORIAL, tree: parse(SAMPLE('findagrave-memorial')), fetch: findagrave() });
  const info = await page.ask({ action: 'findagraveMemorialInfo', today: '2026-11-06' });
  assert.strictEqual(info.ok, true);
  assert.strictEqual(info.record.name, 'Calvin George Hartley');
  assert.deepStrictEqual(info.hints, { sourceType: 'website', sourceDate: '1918-01-04', sourceClass: 'derivative' });
  assert.strictEqual(info.title, 'Calvin George Hartley (1842-1918) - Find a Grave memorial');
  assert.match(info.citation, /accessed 2026-11-06\)/);
  assert.strictEqual(page.calls.length, 0, 'reading the memorial asks nothing of the network');
});

test('the photo list is read from Find a Grave without cookies or a login, each photo with its role', async () => {
  const page = loadPage({ url: MEMORIAL, cookie: 'session=SECRET-FAG-COOKIE', fetch: findagrave() });
  const got = await page.ask({ action: 'findagraveGallery', memorialId: '900000001', slug: 'calvin-george-hartley' });
  assert.strictEqual(got.ok, true);
  assert.deepStrictEqual(got.photos.map((p) => [p.photoId, p.role]),
    [['990000101', 'grave-1'], ['990000102', 'person-1'], ['990000103', 'family-1']]);
  assert.deepStrictEqual(page.calls.map((c) => c.url), [MEMORIAL + '/photo']);
  for (const c of page.calls) {
    assert.strictEqual(JSON.stringify(c.init), JSON.stringify({ credentials: 'omit' })); // made in the page's realm
    assert.strictEqual(c.auth, false);
  }
});

test('the photo list is never asked for from a tab that has left Find a Grave, or for no memorial', async () => {
  let page = loadPage({ url: 'https://blog.example.org/x', fetch: findagrave() });
  let r = await page.ask({ action: 'findagraveGallery', memorialId: '900000001', slug: 'calvin-george-hartley' });
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /no longer on Find a Grave/);
  assert.strictEqual(page.calls.length, 0);
  page = loadPage({ url: MEMORIAL, fetch: findagrave() });
  for (const msg of [{ memorialId: '../../account' }, { memorialId: '' }, { memorialId: '9/../1' }]) {
    r = await page.ask(Object.assign({ action: 'findagraveGallery' }, msg));
    assert.strictEqual(r.ok, false, JSON.stringify(msg));
  }
  assert.strictEqual(page.calls.length, 0);
  // an odd slug is made one path segment: the request stays under the memorial
  await page.ask({ action: 'findagraveGallery', memorialId: '900000001', slug: '../x?y' });
  assert.deepStrictEqual(page.calls.map((c) => c.url), ['https://www.findagrave.com/memorial/900000001/..%2Fx%3Fy/photo']);
});

test('F2/F4: a memorial whose slug has an accented letter or an apostrophe still gets its photo list', async () => {
  for (const [slug, path] of [
    ['j%C3%B3zefa-hartley', '/memorial/900000005/j%C3%B3zefa-hartley/photo'],
    ["mary-o'hartley", "/memorial/900000005/mary-o'hartley/photo"],
    ['mary-o%27hartley', "/memorial/900000005/mary-o'hartley/photo"],
  ]) {
    const page = loadPage({
      url: 'https://www.findagrave.com/memorial/900000005/' + slug,
      fetch: async () => ({ ok: true, status: 200, text: async () => SAMPLE('findagrave-memorial-photos') }),
    });
    const r = await page.ask({ action: 'findagraveGallery', memorialId: '900000005', slug });
    assert.strictEqual(r.ok, true, slug + ': ' + r.error);
    assert.strictEqual(r.photos.length, 3, slug);
    assert.deepStrictEqual(page.calls.map((c) => new URL(c.url).pathname), [path], slug);
  }
});

test('PR #221 sweep: a FamilySearch image id that is not one is refused before any request', async () => {
  const page = loadPage({ url: REC, cookie: 'fssessionid=' + TOKEN, fetch: familysearch() });
  for (const imageId of ['../../platform/users/current', 'HRT1-IMG1?x=1', 'a/b']) {
    const r = await page.ask({ action: 'familysearchImage', imageId });
    assert.strictEqual(r.ok, false, imageId);
    assert.match(r.error, /not a FamilySearch image/);
  }
  assert.strictEqual(page.calls.length, 0);
  const ok = await page.ask({ action: 'familysearchImage', imageId: 'HRT1-IMG1' });
  assert.strictEqual(ok.ok, true);
});
