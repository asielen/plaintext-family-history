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
// `head` is the page's head tags by name - 'og:title', 'og:url', 'og:image',
// 'canonical', 'date' - each answering as that tag would.
// `storage` is what chrome.storage.local holds (the batch switch); without
// it the page has no storage to ask, as on an add-on that cannot read it.
// `scripts` are the page's script texts (Newspapers.com's page data).
// `clone` is what the page copy clones (a stand-in with the pictures to inline).
function loadPage({ url, cookie = '', links = [], fetch, canonical, tree, head = {}, title = 'A page', storage, scripts = [],
  clone } = {}) {
  const listeners = [];
  const el = (tag) => ({ tagName: tag, textContent: '', innerText: '', getAttribute: () => null, closest: () => null });
  const headTag = (sel) => {
    const name = Object.keys(head).find((k) =>
      (k === 'canonical' ? /^link\[rel="canonical"\]/ : new RegExp('^meta\\[(property|name)="' + k + '"\\]')).test(sel));
    if (!name) return null;
    const v = head[name];
    return { href: new URL(v, url).href, getAttribute: (a) => (a === 'href' || a === 'content' ? v : null) };
  };
  const document = {
    title,
    cookie,
    doctype: null,
    body: el('BODY'),
    documentElement: { outerHTML: '<html></html>', cloneNode: () => clone || ({ querySelectorAll: () => [], outerHTML: '<html></html>' }) },
    scripts: scripts.map((t) => ({ textContent: t })),
    images: [],
    querySelector: (sel) => headTag(sel) || (canonical && /canonical|og:url/.test(sel)
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
      storage: storage === undefined ? undefined
        : { local: { get: (d, cb) => cb(Object.assign({}, d, JSON.parse(JSON.stringify(storage)))) } },
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

// ── the page's head on a single-page site ───────────────────────────────────

test('a head left over from the previous record does not title, date or picture this one', async () => {
  // FamilySearch moved from Calvin's record to Harriet's without reloading:
  // the address is Harriet's, the head still describes Calvin.
  const CALVIN = 'https://www.familysearch.org/ark:/61903/1:1:HRT1-CEN1';
  const HARRIET = 'https://www.familysearch.org/ark:/61903/1:1:HRT1-CEN2';
  const head = {
    'og:url': CALVIN,
    'og:title': 'Calvin Hartley, United States Census, 1880',
    'og:image': 'https://www.familysearch.org/img/calvin.jpg',
    'article:published_time': '1880-06-01',
    canonical: CALVIN,
  };
  let r = await loadPage({ url: HARRIET, head, title: 'Harriet Hartley • FamilySearch', fetch: familysearch() })
    .ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.title, 'Harriet Hartley • FamilySearch');
  assert.strictEqual(r.prefill.canonical, null);
  assert.strictEqual(r.prefill.sourceDate, null);
  assert.notStrictEqual(r.prefill.imageUrl, 'https://www.familysearch.org/img/calvin.jpg');
  // Only the canonical left behind (og:url absent) is enough to tell.
  r = await loadPage({ url: HARRIET, head: { canonical: CALVIN, 'og:title': 'Calvin Hartley' }, fetch: familysearch() })
    .ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.title, 'A page');
});

test('a head that describes this page is used, whatever its language prefix, slash or query', async () => {
  const url = 'https://www.familysearch.org/en/ark:/61903/1:1:HRT1-CEN1?lang=en';
  const r = await loadPage({ url, fetch: familysearch(), head: {
    'og:url': 'https://www.familysearch.org/ark:/61903/1:1:HRT1-CEN1/',
    canonical: 'https://www.familysearch.org/ark:/61903/1:1:hrt1-cen1',
    'og:title': 'Calvin Hartley, United States Census, 1880',
    'og:image': 'https://www.familysearch.org/img/calvin.jpg',
  } }).ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.title, 'Calvin Hartley, United States Census, 1880');
  assert.strictEqual(r.prefill.canonical, 'https://www.familysearch.org/ark:/61903/1:1:hrt1-cen1');
  assert.strictEqual(r.prefill.imageUrl, 'https://www.familysearch.org/img/calvin.jpg');
  // An ordinary site with no og:url or canonical keeps its og:title.
  const plain = await loadPage({ url: 'https://example.org/records/1', head: { 'og:title': 'A will, 1901' },
    fetch: familysearch() }).ask({ action: 'prefill' });
  assert.strictEqual(plain.prefill.title, 'A will, 1901');
});

// ── the panel's retry of a refused FamilySearch address ─────────────────────

test('the retry fetches the image the refused address names, through FamilySearch only', async () => {
  const page = loadPage({ url: 'https://www.familysearch.org/records/images/image-details?rmsId=TH-0000-HRT1',
    cookie: 'fssessionid=' + TOKEN, fetch: familysearch() });
  const img = await page.ask({ action: 'familysearchImage',
    fromUrl: 'https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG1?i=3' });
  assert.strictEqual(img.ok, true);
  assert.deepStrictEqual(page.calls.map((c) => [c.url, c.auth]), [
    ['https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG1', true],
    ['https://www.familysearch.org/das/v2/HRT1-IMG1/dist.jpg', true],
  ]);
  assert.ok(!JSON.stringify(img).includes(TOKEN));
  // An address elsewhere names no FamilySearch image; this page is not a
  // viewer, so there is no image to retry with, and nothing is asked.
  const none = loadPage({ url: 'https://www.familysearch.org/tree/person/details/HRT1-PPL',
    cookie: 'fssessionid=' + TOKEN, links: oneLink('/ark:/61903/3:1:HRT1-IMG1'), fetch: familysearch() });
  const r = await none.ask({ action: 'familysearchImage', fromUrl: 'https://evil.example.com/ark:/61903/3:1:HRT1-IMG1' });
  assert.strictEqual(r.ok, false);
  assert.strictEqual(none.calls.length, 0);
  // And from a page off FamilySearch, nothing at all.
  const away = loadPage({ url: 'https://blog.example.org/x', cookie: 'fssessionid=' + TOKEN, fetch: familysearch() });
  assert.strictEqual((await away.ask({ action: 'familysearchImage',
    fromUrl: 'https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG1' })).ok, false);
  assert.strictEqual(away.calls.length, 0);
});

// ── PR #223 audit F5/F7: the second try reads FamilySearch's own addresses ──

test('F5: the second try finds the image in FamilySearch\'s tile and dist.jpg addresses', async () => {
  const pagesTried = ['https://www.familysearch.org/records/images/image-details?rmsId=TH-0000-HRT1&imageIndex=3',
    'https://www.familysearch.org/search/film/000000?cat=1&i=3'];
  const addresses = [
    'https://sg30p0.familysearch.org/service/records/storage/deepzoomcloud/dz/v1/3:1:HRT1-IMG1/image_files/12/0_0.jpg',
    'https://sg30p0.familysearch.org/service/records/storage/das/v2/3:1:HRT1-IMG1/dist.jpg',
    'https://sg30p0.familysearch.org/service/records/storage/das/v2/3%3A1%3AHRT1-IMG1/dist.jpg',
    'https://www.familysearch.org/das/v2/HRT1-IMG1/dist.jpg',
  ];
  for (const url of pagesTried) {
    for (const fromUrl of addresses) {
      const page = loadPage({ url, cookie: 'fssessionid=' + TOKEN, fetch: familysearch() });
      const img = await page.ask({ action: 'familysearchImage', fromUrl });
      assert.strictEqual(img.ok, true, url + ' <- ' + fromUrl);
      assert.strictEqual(page.calls[0].url, 'https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG1', fromUrl);
      assert.ok(page.calls.every((c) => /^https:\/\/[^/]*familysearch\.org\//.test(c.url)));
    }
  }
});

test('F7: an address elsewhere, or not of an image, is not swapped for the image the viewer shows', async () => {
  const film = 'https://www.familysearch.org/search/film/000000?cat=1&i=3';
  for (const fromUrl of ['https://county.example.gov/records/hartley.pdf',
    'https://www.familysearch.org/tree/person/details/HRT1-PPL',
    'http://sg30p0.familysearch.org/service/records/storage/das/v2/3:1:HRT1-IMG1/dist.jpg']) {
    const page = loadPage({ url: film, cookie: 'fssessionid=' + TOKEN, links: oneLink('/ark:/61903/3:1:HRT1-IMG1'),
      fetch: familysearch() });
    const r = await page.ask({ action: 'familysearchImage', fromUrl });
    assert.strictEqual(r.ok, false, fromUrl);
    assert.match(r.error, /not of a FamilySearch image, so FamilySearch was not asked for it/);
    assert.strictEqual(page.calls.length, 0, fromUrl);
  }
  // Without an address, the viewer's own image is still the one (unchanged).
  const page = loadPage({ url: film, cookie: 'fssessionid=' + TOKEN, links: oneLink('/ark:/61903/3:1:HRT1-IMG1'),
    fetch: familysearch() });
  assert.strictEqual((await page.ask({ action: 'familysearchImage' })).ok, true);
});

// ── PR #223 audit F8: the stale-head comparison ─────────────────────────────

test('F8: a head for this page is used however its address is written', async () => {
  const ok = [
    // percent-encoded colon in the canonical, a plain one in the bar
    ['https://records.example.org/view/12:7884', { canonical: 'https://records.example.org/view/12%3A7884' }],
    // the bar carries extra query values (tracking) the tag does not
    ['https://records.example.org/item?itemId=5&utm_source=mail', { 'og:url': 'https://records.example.org/item?itemId=5' }],
    // the tag names a query value the bar leaves out
    ['https://records.example.org/item/5', { canonical: 'https://records.example.org/item/5?lang=en' }],
    // the same hash route
    ['https://records.example.org/app#/record/5', { 'og:url': 'https://records.example.org/app#/record/5' }],
  ];
  for (const [url, head] of ok) {
    const r = await loadPage({ url, head: Object.assign({ 'og:title': 'The right record' }, head), fetch: familysearch() })
      .ask({ action: 'prefill' });
    assert.strictEqual(r.prefill.title, 'The right record', url);
  }
});

test('F8: a head left from another record keyed in the query or a hash route is stale', async () => {
  const stale = [
    ['https://records.example.org/item?itemId=6', { 'og:url': 'https://records.example.org/item?itemId=5' }],
    ['https://records.example.org/app#/record/6', { canonical: 'https://records.example.org/app#/record/5' }],
  ];
  for (const [url, head] of stale) {
    const r = await loadPage({ url, head: Object.assign({ 'og:title': 'The previous record',
      'og:image': 'https://records.example.org/img/previous.jpg' }, head), fetch: familysearch() })
      .ask({ action: 'prefill' });
    assert.strictEqual(r.prefill.title, 'A page', url);
    assert.notStrictEqual(r.prefill.imageUrl, 'https://records.example.org/img/previous.jpg', url);
  }
});

// ── the owner's batch downloads: switched off unless told ───────────────────

const FS_PERSON = 'https://www.familysearch.org/tree/person/details/KWHR-T01';
const NP_LIST_PAGE = 'https://www.newspapers.com/clippings/my-clippings/';
const ANC_PERSON = 'https://www.ancestry.com/family-tree/person/tree/1/person/2/facts';
const BATCH_ON = { batchDownloads: true };

// Every network answer a batch question could want; each request recorded.
function anything() {
  return async (url) => {
    if (/\/platform\/tree\/persons\/KWHR-T01\/sources$/.test(url)) {
      return { ok: true, status: 200, text: async () => JSON.stringify({ sourceDescriptions: [
        { about: 'https://www.familysearch.org/ark:/61903/1:1:HRT1-CEN1', titles: [{ value: 'Calvin Hartley, 1880' }] },
        { about: 'https://www.findagrave.com/memorial/900000001/calvin-george-hartley' },
      ] }) };
    }
    if (/\/platform\/tree\/persons\/KWHR-T01$/.test(url)) {
      return { ok: true, status: 200, text: async () => JSON.stringify({ persons: [{ display: { name: 'Calvin George Hartley' } }] }) };
    }
    if (/\/api\/clipping\/list\?/.test(url)) {
      return { ok: true, status: 200, json: async () => ({ clippings: [], more_results: false }) };
    }
    return { ok: false, status: 404, text: async () => '' };
  };
}

test('switched off, every batch question is answered "switched off" and nothing is read or sent', async () => {
  const asks = [
    [FS_PERSON, { action: 'familysearchPersonSources' }],
    [FS_PERSON, { action: 'findagraveLinks' }],
    [MEMORIAL, { action: 'findagraveLinks' }],
    [ANC_PERSON, { action: 'ancestryPersonSources' }],
    [NP_LIST_PAGE, { action: 'newspapersList', cursor: 0, user: 'HartleyResearch' }],
  ];
  for (const storage of [undefined, {}, { batchDownloads: false }, { batchDownloads: 'true' }]) {
    for (const [url, msg] of asks) {
      const page = loadPage({ url, cookie: 'fssessionid=' + TOKEN, fetch: anything(), storage,
        tree: parse(SAMPLE('findagrave-memorial')) });
      const r = await page.ask(msg);
      assert.deepStrictEqual(r, { ok: false, off: true, error: 'batch downloads are switched off in this browser' },
        msg.action + ' with ' + JSON.stringify(storage));
      assert.strictEqual(page.calls.length, 0, msg.action + ' sent a request');
    }
  }
});

test('switched on: a tree person\'s FamilySearch sources, the session sent to FamilySearch only and returned nowhere', async () => {
  const page = loadPage({ url: FS_PERSON, cookie: 'fssessionid=' + encodeURIComponent(TOKEN), fetch: anything(), storage: BATCH_ON });
  const r = await page.ask({ action: 'familysearchPersonSources' });
  assert.strictEqual(r.ok, true, r.error);
  assert.strictEqual(r.person, 'Calvin George Hartley');
  assert.deepStrictEqual(r.sources, [{ fsId: 'HRT1-CEN1', title: 'Calvin Hartley, 1880' }]);
  assert.strictEqual(page.calls.length, 2);
  for (const c of page.calls) {
    assert.match(c.url, /^https:\/\/www\.familysearch\.org\//);
    assert.strictEqual(c.auth, true);
  }
  assert.ok(!JSON.stringify(r).includes(TOKEN));
  // ...and the memorials among them, the same way.
  const f = loadPage({ url: FS_PERSON, cookie: 'fssessionid=' + TOKEN, fetch: anything(), storage: BATCH_ON,
    tree: parse('<html><h1>Calvin George Hartley</h1></html>') });
  const links = await f.ask({ action: 'findagraveLinks' });
  assert.deepStrictEqual(links.sources.map((s) => s.memorialId), ['900000001']);
  assert.ok(f.calls.every((c) => /^https:\/\/www\.familysearch\.org\//.test(c.url)));
  // Asked from a tab that is not on FamilySearch: no request at all.
  const away = loadPage({ url: 'https://blog.example.org/tree/person/details/KWHR-T01', cookie: 'fssessionid=' + TOKEN,
    fetch: anything(), storage: BATCH_ON });
  assert.strictEqual((await away.ask({ action: 'familysearchPersonSources' })).ok, false);
  assert.strictEqual(away.calls.length, 0);
});

test('switched on: Find a Grave and Ancestry pages are read for what to queue without any request', async () => {
  const page = loadPage({ url: MEMORIAL, cookie: 'session=SECRET-FAG-COOKIE', fetch: anything(), storage: BATCH_ON,
    tree: parse(SAMPLE('findagrave-memorial')) });
  const r = await page.ask({ action: 'findagraveLinks' });
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.sources[0].memorialId, '900000001');
  assert.ok(r.sources.length > 1);
  assert.strictEqual(page.calls.length, 0);
  const anc = loadPage({ url: ANC_PERSON, fetch: anything(), storage: BATCH_ON, tree: parse('<html><h1>Calvin Hartley</h1>' +
    '<div id="sourcesSection"><a href="/search/collections/90001/records/1000001">1880 census</a></div></html>') });
  const a = await anc.ask({ action: 'ancestryPersonSources' });
  assert.deepStrictEqual(a.sources.map((s) => s.dbId + ':' + s.recordId), ['90001:1000001']);
  assert.strictEqual(anc.calls.length, 0);
  // Ancestry's reader answers on Ancestry only.
  const off = loadPage({ url: MEMORIAL, fetch: anything(), storage: BATCH_ON, tree: parse(SAMPLE('findagrave-memorial')) });
  assert.strictEqual((await off.ask({ action: 'ancestryPersonSources' })).ok, false);
});

// The page data that names who is signed in, as Newspapers.com sends it.
const npSignedIn = (name) => 'self.__next_f.push([1,' +
  JSON.stringify('1:{"user":{"id":7,"username":"' + name + '"}}\n') + '])';

test('switched on: her clippings list is asked of Newspapers.com only, for whoever is signed in there', async () => {
  const page = loadPage({ url: NP_LIST_PAGE, fetch: anything(), storage: BATCH_ON, scripts: [npSignedIn('HartleyResearch')] });
  const r = await page.ask({ action: 'newspapersList', cursor: 0, user: null });
  assert.strictEqual(r.ok, true, r.error);
  assert.strictEqual(r.user, 'HartleyResearch');
  assert.strictEqual(page.calls.length, 1);
  assert.match(page.calls[0].url, /^https:\/\/www\.newspapers\.com\/api\/clipping\/list\?user=HartleyResearch&/);
  assert.strictEqual(page.calls[0].auth, false);
  // Another tab's site, nobody signed in, or another member signed in than
  // the list was started for: nothing is asked.
  for (const [url, scripts, user] of [
    ['https://blog.example.org/clippings/', [npSignedIn('HartleyResearch')], 'HartleyResearch'],
    [NP_LIST_PAGE, [], 'HartleyResearch'],
    [NP_LIST_PAGE, [npSignedIn('SomeoneElse')], 'HartleyResearch'],
  ]) {
    const away = loadPage({ url, fetch: anything(), storage: BATCH_ON, scripts });
    assert.strictEqual((await away.ask({ action: 'newspapersList', cursor: 0, user })).ok, false, url + ' ' + scripts);
    assert.strictEqual(away.calls.length, 0);
  }
});

test('Codex 4504508005: FamilySearch\'s image route says a body that broke off passes, never only the browser\'s words', async () => {
  const page = loadPage({ url: REC, cookie: 'fssessionid=' + TOKEN, fetch: familysearch({
    'dist.jpg': { ok: true, status: 200, headers: { get: () => 'image/jpeg' }, blob: async () => { throw new TypeError('Failed to fetch'); } },
  }) });
  const r = await page.ask({ action: 'familysearchImage', imageId: 'HRT1-IMG1' });
  assert.deepStrictEqual([r.ok, r.transient], [false, true]);
  assert.match(r.error, /broke off/);
});

test('second audit N3: a fault in the add-on while it reads is said as one, never as a connection that broke off', async () => {
  // A page element the reader trips over (a fault in the reader, not the site).
  const bad = { nodeType: 1, tagName: 'A', id: '', className: '', childNodes: [],
    getAttribute: () => { throw new TypeError('cannot read the link of this element'); } };
  const page = loadPage({ url: MEMORIAL, storage: BATCH_ON, tree: { title: 'A memorial', childNodes: [bad] } });
  const r = await page.ask({ action: 'findagraveLinks' });
  assert.strictEqual(r.ok, false);
  assert.ok(!r.transient, 'a code fault was called a passing cause');
  assert.match(r.error, /could not read this record \(TypeError: cannot read the link/);
  // A real network failure still is a cause that passes.
  const net = loadPage({ url: REC, cookie: 'fssessionid=' + TOKEN, fetch: familysearch({
    'dist.jpg': { ok: true, status: 200, headers: { get: () => 'image/jpeg' }, blob: async () => { throw new TypeError('Failed to fetch'); } },
  }) });
  const n = await net.ask({ action: 'familysearchImage', imageId: 'HRT1-IMG1' });
  assert.strictEqual(n.transient, true);
});

// ── PR #224 Codex 4504665602: secondary requests still say "slow down" ─────

const NP_CLIP = 'https://www.newspapers.com/article/the-fairview-gazette-obituary/101/';

test('Codex 4504665602: a clipping-text request refused with 429 says so, with its status', async () => {
  const page = loadPage({ url: NP_CLIP, fetch: async () => ({ ok: false, status: 429, text: async () => '' }) });
  const r = await page.ask({ action: 'newspapersClipText', url: 'https://www.newspapers.com/article/x/202/', clippingId: 202 });
  assert.deepStrictEqual([r.ok, r.status, r.slowDown], [false, 429, true]);
});

test('Codex 4504665602 sweep: a page re-read refused with 503 says "slow down", not only "could not re-read"', async () => {
  // The page's own data describes another clipping, so the page is read again.
  const other = 'self.__next_f.push([1,' + JSON.stringify('2:{"clipping":{"clipping_id":999}}\n') + '])';
  const page = loadPage({ url: NP_CLIP, scripts: [other], fetch: async () => ({ ok: false, status: 503, text: async () => '' }) });
  const r = await page.ask({ action: 'newspapersInfo' });
  assert.deepStrictEqual([r.ok, r.slowDown], [false, true]);
  assert.match(r.error, /slow down \(HTTP 503\)/);
});

test('Codex 4504665602 sweep: the page copy names the hosts that said slow down to its pictures', async () => {
  const img = { getAttribute: (a) => (a === 'src' ? 'https://images.findagrave.com/photos/2020/45/900000001_aaaa1111.jpeg' : null),
    getAttributeNames: () => ['src'], hasAttribute: (a) => a === 'src', setAttribute() {}, removeAttribute() {},
    parentElement: null, tagName: 'IMG' };
  const clone = { querySelectorAll: (sel) => (sel === 'img' ? [img] : []), querySelector: () => null, outerHTML: '<html></html>',
    getAttributeNames: () => [], getAttribute: () => null, setAttribute() {}, removeAttribute() {}, tagName: 'HTML' };
  const page = loadPage({ url: MEMORIAL, clone, fetch: async () => ({ ok: false, status: 429, text: async () => '' }) });
  const r = await page.ask({ action: 'singlefile' });
  assert.strictEqual(r.ok, true, r.error);
  assert.deepStrictEqual(r.slowHosts, ['images.findagrave.com']);
});

// ── a photo someone shared on Ancestry ───────────────────────────────────────

const MEDIA = '7c0ffee0-1234-4abc-8def-0123456789ab';
const SHARED = 'https://www.ancestry.com/mediaui-viewer/tree/90000001/person/90000002/media/' + MEDIA;
const SHARED_IMG = 'https://www.ancestry.com/api/media/retrieval/v2/image/namespaces/4242/media/' + MEDIA +
  '?client=trees-mediaservice&imageQuality=hq';
const SHARED_BYTES = Buffer.from('\xff\xd8\xff\xe0\x00\x10JFIF fictional shared photo\xff\xd9', 'latin1');

// Ancestry's media service as it answers a signed-in page; `over` by address.
function ancestryMedia(over = {}) {
  return async (url) => {
    if (over[url]) return over[url];
    if (url === SHARED_IMG) {
      return { ok: true, status: 200, headers: { get: () => 'image/jpeg' },
        blob: async () => new Blob([SHARED_BYTES], { type: 'image/jpeg' }) };
    }
    return { ok: false, status: 404, headers: { get: () => 'text/html' } };
  };
}

test('prefill knows a shared photo\'s viewer page, and only that page', async () => {
  const tree = parse(SAMPLE('ancestry-media-photo'));
  let r = await loadPage({ url: SHARED + '?destTreeId=90000003&hid=h1', tree, fetch: ancestryMedia() })
    .ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.ancestryPhoto, true);
  assert.strictEqual(r.prefill.ancestryMediaId, MEDIA);
  assert.strictEqual(r.prefill.sourceType, 'photo');
  assert.strictEqual(r.prefill.ancestryRecord, false);
  r = await loadPage({ url: 'https://www.ancestry.com/family-trees/90000001/people/90000002/hints', tree,
    fetch: ancestryMedia() }).ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.ancestryPhoto, false);
  assert.strictEqual(r.prefill.sourceType, undefined);
});

test('the shared photo is read from the page and fetched from Ancestry\'s own site, in her session, as sent', async () => {
  const page = loadPage({ url: SHARED + '?hid=h1', tree: parse(SAMPLE('ancestry-media-photo')), fetch: ancestryMedia() });
  const info = await page.ask({ action: 'ancestryPhotoInfo' });
  assert.strictEqual(info.ok, true);
  assert.strictEqual(info.media.url, SHARED);
  assert.strictEqual(info.media.imageUrl, SHARED_IMG);
  assert.deepStrictEqual(info.hints, { sourceType: 'photo', sourceDate: '1925~', sourceClass: null });
  assert.ok(!JSON.stringify(info).includes('cousin_member'), 'the "Saved by" list was read');
  assert.strictEqual(page.calls.length, 0, 'reading the photo asks nothing of the network');
  const got = await page.ask({ action: 'ancestryPhoto', mediaId: MEDIA });
  assert.strictEqual(got.ok, true, got.error);
  assert.ok(Buffer.from(got.base64, 'base64').equals(SHARED_BYTES), 'the photo is kept byte for byte');
  assert.strictEqual(got.ext, 'jpg');
  assert.deepStrictEqual(page.calls.map((c) => [c.url, c.init.credentials]), [[SHARED_IMG, 'include']]);
});

test('the photo is never fetched for another photo, from another site, or as a page instead of an image', async () => {
  const tree = parse(SAMPLE('ancestry-media-photo'));
  let page = loadPage({ url: SHARED, tree, fetch: ancestryMedia() });
  let r = await page.ask({ action: 'ancestryPhoto', mediaId: '1badf00d-5678-4def-9abc-ba9876543210' });
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /no longer shows this photo/);
  page = loadPage({ url: 'https://blog.example.org/mediaui-viewer/tree/1/person/2/media/' + MEDIA, tree,
    fetch: ancestryMedia() });
  r = await page.ask({ action: 'ancestryPhoto', mediaId: MEDIA });
  assert.match(r.error, /no longer on Ancestry/);
  assert.strictEqual(page.calls.length, 0);
  page = loadPage({ url: SHARED, tree, fetch: ancestryMedia({ [SHARED_IMG]: { ok: true, status: 200,
    headers: { get: () => 'text/html' }, blob: async () => new Blob(['<html>sign in</html>'], { type: 'text/html' }) } }) });
  r = await page.ask({ action: 'ancestryPhoto', mediaId: MEDIA });
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /sent a page instead of the photo/);
  page = loadPage({ url: SHARED, tree, fetch: ancestryMedia({ [SHARED_IMG]: { ok: false, status: 403 } }) });
  r = await page.ask({ action: 'ancestryPhoto', mediaId: MEDIA });
  assert.match(r.error, /refused the photo \(HTTP 403\) - sign in/);
  // No picture of it on the page yet: nothing is fetched.
  page = loadPage({ url: SHARED, tree: parse('<p>loading</p>'), fetch: ancestryMedia() });
  r = await page.ask({ action: 'ancestryPhoto', mediaId: MEDIA });
  assert.match(r.error, /has not appeared on the page yet/);
  assert.strictEqual(page.calls.length, 0);
});

test('PR #258 review: on a shared photo, the form starts from its Details - linked people as names to tick', async () => {
  const r = await loadPage({ url: SHARED + '?hid=h1', tree: parse(SAMPLE('ancestry-media-photo')), fetch: ancestryMedia() })
    .ask({ action: 'prefill' });
  assert.strictEqual(r.prefill.title, 'Thomas and Margaret Hartley at the Fairview farm, about 1925');
  assert.strictEqual(r.prefill.sourceDate, '1925~');
  assert.deepStrictEqual(r.prefill.people, ['Thomas Hartley', 'Margaret Hartley']);
});

test('PR #258 review: the "Saved by" list of other members is left out of page.html and the page copy', async () => {
  const { serialize } = require('./mini-dom.js');
  const copyOf = () => {
    const html = parse(SAMPLE('ancestry-media-photo')).childNodes.find((c) => c.tagName === 'HTML');
    html.querySelectorAll = () => [];
    Object.defineProperty(html, 'outerHTML', { get() { return serialize(html); } });
    return html;
  };
  let r = await loadPage({ url: SHARED + '?hid=h1', tree: parse(SAMPLE('ancestry-media-photo')), clone: copyOf(),
    fetch: ancestryMedia() }).ask({ action: 'pagehtml' });
  assert.strictEqual(r.ok, true);
  assert.ok(!/cousin_member|Saved by/.test(r.html), 'other members are in page.html');
  assert.ok(r.html.includes('Linked to') && r.html.includes('The dog is Shep.'), 'the rest of the page is kept');
  // Off a photo page, nothing is taken out.
  r = await loadPage({ url: 'https://www.ancestry.com/family-trees/90000001/people/90000002/hints', clone: copyOf(),
    fetch: ancestryMedia() }).ask({ action: 'pagehtml' });
  assert.ok(r.html.includes('cousin_member_one'));
  // The page copy is built from the same kind of clone, through the same step.
  assert.match(SRC, /const clone = document\.documentElement\.cloneNode\(true\);\n    stripSeenIt\(clone\);\n    stripOtherMembers\(clone\);/);
});
