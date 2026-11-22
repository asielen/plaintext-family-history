// test-content.js - the content scripts start up on a pretend page
// (contentharness.js), and the small rules they share (searchrun.js).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const path = require('path');
const { loadContentScript } = require('./contentharness.js');

const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const present = (files) => files.filter((f) => fs.existsSync(path.join(__dirname, '..', f)));

// PR #212 review: on a Find a Grave listing the first check found "nothing
// changed" (no memorial = null = the starting value) and never drew the pills.
test('Find a Grave: results already on a cemetery listing get their pills', async () => {
  const page = loadContentScript('content-findagrave.js',
    { pathname: '/cemetery/1001/fairview-cemetery', origin: 'https://www.findagrave.com' });
  await wait(600);
  page.stop();
  assert.ok(page.queries.includes('a[href*="/memorial/"]'), 'the memorial links were never looked at');
});

// The same first-render check for every content script (the class sweep).
for (const [label, files, pathname, origin, drawn] of [
  ['Ancestry search results', ['content-ancestry.js'], '/search/categories/usfedcen/', 'https://www.ancestry.com', /a\[href/],
  ['FamilySearch search results', ['content-familysearch.js'], '/search/record/results', 'https://www.familysearch.org', /a\[href/],
  ['Newspapers.com browse page', present(['cliplist.js', 'searchrun.js', 'content.js']), '/browse/', 'https://www.newspapers.com', /SearchResult/],
  ['Find a Grave memorial', ['content-findagrave.js'], '/memorial/5505/thomas-hartley', 'https://www.findagrave.com', /a\[href/],
]) {
  test(label + ': the first look at the page draws the pills', async () => {
    const page = loadContentScript(files, { pathname, origin });
    await wait(600);
    page.stop();
    assert.ok(page.queries.some((q) => drawn.test(q)), 'nothing was drawn: ' + JSON.stringify(page.queries));
  });
}

// PR #212 review: the same search run again 40 minutes later is a new run.
test('a search repeated in a later sitting is logged again; a re-render is not', () => {
  const { isRepeat, SITTING_MS } = require('../searchrun.js');
  const t = Date.now();
  const last = { key: '{"keyword":"Hartley"}', count: 12, at: t };
  assert.strictEqual(isRepeat(last, last.key, 12, t + 1000), true);
  assert.strictEqual(isRepeat(last, last.key, 12, t + 40 * 60 * 1000), false);
  assert.strictEqual(isRepeat(last, last.key, 13, t + 1000), false); // the count settled: log it
  assert.strictEqual(isRepeat(last, '{"keyword":"Hartley","date-start":"1920"}', 12, t + 1000), false);
  assert.strictEqual(isRepeat(null, last.key, 12, t), false);
  assert.strictEqual(SITTING_MS, 30 * 60 * 1000);
});

test('content.js uses that rule, and forgets the last search on leaving the results', () => {
  const src = fs.readFileSync(path.join(__dirname, '..', 'content.js'), 'utf8');
  assert.match(src, /SeenItSearch\.isRepeat\(searchLogged/);
  assert.match(src, /searchLogged = null; \/\/ left the results/);
  const manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'manifest.json'), 'utf8'));
  const np = manifest.content_scripts.find((c) => c.js.includes('content.js'));
  assert.deepStrictEqual(np.js, ['cliplist.js', 'searchrun.js', 'content.js']);
});

// A person's Gallery page: each tile is a link filling the tile (its picture)
// in a .mediaGalleryItem that clips what overflows it, so pills placed after
// the link were hidden below the picture. They sit on its top-left corner.
test('Ancestry gallery: the pills on a photo tile are drawn over its top-left corner, above the picture', () => {
  const css = fs.readFileSync(path.join(__dirname, '..', 'content-ancestry.css'), 'utf8');
  const rule = /\.mediaGalleryItem \.nsi-pills\s*\{([^}]*)\}/.exec(css);
  assert.ok(rule, 'no rule for pills inside a gallery tile');
  for (const want of [/position:\s*absolute/, /top:\s*\d+px/, /left:\s*\d+px/, /z-index:\s*\d+/, /max-width:\s*calc\(100% - \d+px\)/]) {
    assert.match(rule[1], want);
  }
});

// Ancestry Hints page (person/hints): opening a hint slides in Ancestry's side
// panel without changing the address. That click is the look; a photo hint is
// keyed by its media id; scrolling past hints records nothing.
function hintClick(href, { header = true, button = 0, ctrlKey = false } = {}) {
  const card = { querySelector: (sel) => (/hintCardContentHeader/.test(sel) ? { innerText: 'Photo of the family', getAttribute: () => null } : null) };
  const a = {
    getAttribute: (n) => (n === 'href' ? href : null),
    classList: { contains: (c) => header && c === 'hintCardContentHeader' },
    closest: (sel) => (sel === 'section.extHintCard' ? card : null),
  };
  return { button, ctrlKey, metaKey: false, shiftKey: false, altKey: false, target: { closest: (sel) => (sel === 'a[href]' ? a : null) } };
}

test('Ancestry hints: opening a photo hint counts as opening that photo', async () => {
  let panel = null;
  const page = loadContentScript('content-ancestry.js', {
    pathname: '/family-trees/111/people/222/hints', origin: 'https://www.ancestry.com',
    querySelector: (sel) => (sel === 'aside.sidebar:not(.noDisplay)' ? panel : null),
  });
  let look;
  try {
    await wait(50);
    assert.strictEqual(page.sent.filter((m) => m.type === 'ancLook').length, 0, 'the hints page itself is not a look');
    const click = page.listeners.click && page.listeners.click[0];
    assert.ok(click, 'no click listener on the hints page');
    panel = {};
    click(hintClick('https://www.ancestry.com/mediaui-viewer/tree/333/person/444/media/ABCDEF01-2222-4333-8444-ABCDEFABCDEF?usePUBJs=true'));
    look = page.sent.find((m) => m.type === 'ancLook');
  } finally {
    page.stop();
  }
  assert.ok(look, 'opening the hint recorded no look');
  assert.strictEqual(look.key, 'med:abcdef01-2222-4333-8444-abcdefabcdef');
  assert.strictEqual(look.info.title, 'Photo of the family');
});

test('Ancestry hints: a record hint opened in the panel counts; a new-tab click is left to the new tab', async () => {
  const page = loadContentScript('content-ancestry.js', { pathname: '/family-trees/111/people/222/hints', origin: 'https://www.ancestry.com' });
  try {
    await wait(50);
    const click = page.listeners.click && page.listeners.click[0];
    assert.ok(click, 'no click listener on the hints page');
    click(hintClick('https://www.ancestry.com/search/collections/6742/records/4321?tid=1', { ctrlKey: true }));
    click(hintClick('https://www.ancestry.com/search/collections/6742/records/4321?tid=1', { button: 1 }));
    assert.strictEqual(page.sent.filter((m) => m.type === 'ancLook').length, 0, 'a new-tab click was counted here too');
    click(hintClick('https://www.ancestry.com/search/collections/6742/records/4321?tid=1'));
  } finally {
    page.stop();
  }
  assert.deepStrictEqual(page.sent.filter((m) => m.type === 'ancLook').map((m) => m.key), ['rec:6742:4321']);
});

// PR #256 review: inside a hint card the title claims its item first, so a
// bare thumbnail ahead of the title cannot leave the hint without pills; a
// click on a record hint's scan counts the hint's record; a slow side panel
// is waited for.
const MED = 'abcdef01-2222-4333-8444-abcdefabcdef';
function fakeLink(href, { text = '', cls = '', card = null } = {}) {
  const a = {
    className: cls, textContent: text, innerText: text, nextElementSibling: null, inserted: [],
    getAttribute: (n) => (n === 'href' ? href : null),
    classList: { contains: (c) => cls.split(' ').includes(c) },
    closest: (sel) => (sel === 'section.extHintCard' ? card : null),
    insertAdjacentElement(pos, el) { a.inserted.push(el); a.nextElementSibling = el; },
    querySelector: () => null,
  };
  return a;
}
function fakeCard(titleCls) {
  const card = { title: null };
  card.querySelector = (sel) => (sel.includes(titleCls) ? card.title : null);
  return card;
}

test('Ancestry hints: a thumbnail ahead of the title still leaves the title its pills', async () => {
  const card = fakeCard('hintCardContentHeader');
  const href = 'https://www.ancestry.com/mediaui-viewer/tree/333/person/444/media/' + MED;
  const thumb = fakeLink(href, { cls: 'photo photoCenter', card });
  const title = fakeLink(href, { text: 'Photo of the family', cls: 'link hintCardContentHeader', card });
  card.title = title;
  const page = loadContentScript('content-ancestry.js', {
    pathname: '/family-trees/111/people/222/hints', origin: 'https://www.ancestry.com',
    querySelectorAll: (sel) => (sel.includes('mediaui-viewer') ? [thumb, title] : []),
    storage: { anc: { ['med:' + MED]: { t: [Date.now() - 24 * 3600 * 1000], last: Date.now() - 24 * 3600 * 1000, mark: null } } },
  });
  try {
    await wait(700);
  } finally {
    page.stop();
  }
  assert.strictEqual(title.inserted.length, 1, 'the title got no pills');
  assert.strictEqual(thumb.inserted.length, 0);
});

test('Ancestry hints: clicking a record hint\'s scan counts the hint\'s record', async () => {
  const card = fakeCard('recordCardV2CollectionTitle');
  card.title = fakeLink('https://www.ancestry.com/search/collections/6742/records/4321', { text: '1880 census', cls: 'link recordCardV2CollectionTitle', card });
  const scan = fakeLink('https://www.ancestry.com/imageviewer/collections/6742/images/4321_0001', { cls: 'photoContainer photo', card });
  const page = loadContentScript('content-ancestry.js', { pathname: '/family-trees/111/people/222/hints', origin: 'https://www.ancestry.com' });
  try {
    await wait(50);
    page.listeners.click[0]({ button: 0, target: { closest: (sel) => (sel === 'a[href]' ? scan : null) } });
  } finally {
    page.stop();
  }
  assert.deepStrictEqual(page.sent.filter((m) => m.type === 'ancLook').map((m) => m.key), ['rec:6742:4321']);
});

test('Ancestry hints: a side panel slow to appear is waited for, not taken as closed', async () => {
  let panel = null;
  const page = loadContentScript('content-ancestry.js', {
    pathname: '/family-trees/111/people/222/hints', origin: 'https://www.ancestry.com',
    querySelector: (sel) => (sel === 'aside.sidebar:not(.noDisplay)' ? panel : null),
  });
  try {
    await wait(50);
    page.listeners.click[0](hintClick('https://www.ancestry.com/mediaui-viewer/tree/333/person/444/media/' + MED));
    await wait(2600);
    assert.strictEqual(page.sent.filter((m) => m.type === 'ancDwell').length, 0, 'the reading time was stopped before the panel appeared');
    panel = {};
  } finally {
    page.stop();
  }
});

// PR #256 second review: a link Ancestry redraws picks its pills back up (no
// pile-up), and a click on a link that names no record, scan or photo (Ignore,
// a help page) does not open the hint.
test('Ancestry hints: a redrawn title link reuses its pills instead of adding more', async () => {
  const card = fakeCard('hintCardContentHeader');
  const href = 'https://www.ancestry.com/mediaui-viewer/tree/333/person/444/media/' + MED;
  let title = fakeLink(href, { text: 'Photo of the family', cls: 'link hintCardContentHeader', card });
  card.title = title;
  let links = [title];
  const storage = { anc: { ['med:' + MED]: { t: [Date.now() - 24 * 3600 * 1000], last: Date.now() - 24 * 3600 * 1000, mark: null } } };
  const page = loadContentScript('content-ancestry.js', {
    pathname: '/family-trees/111/people/222/hints', origin: 'https://www.ancestry.com',
    querySelectorAll: (sel) => (sel.includes('mediaui-viewer') ? links : []),
    storage,
  });
  try {
    await wait(700);
    assert.strictEqual(title.inserted.length, 1);
    // Ancestry redraws the title: a new link element, our pills still beside it.
    const pills = title.nextElementSibling;
    const redrawn = fakeLink(href, { text: 'Photo of the family', cls: 'link hintCardContentHeader', card });
    redrawn.nextElementSibling = pills;
    card.title = redrawn;
    links = [redrawn];
    // Opening the hint schedules the next render.
    page.listeners.click[0]({ button: 0, target: { closest: (sel) => (sel === 'a[href]' ? redrawn : null) } });
    await wait(700);
  } finally {
    page.stop();
  }
  assert.strictEqual(links[0].inserted.length, 0, 'a second set of pills was added beside the redrawn title');
});

test('Ancestry hints: Ignore or a help link in a card does not count as opening the hint', async () => {
  const card = fakeCard('recordCardV2CollectionTitle');
  card.title = fakeLink('https://www.ancestry.com/search/collections/6742/records/4321', { text: '1880 census', cls: 'link recordCardV2CollectionTitle', card });
  const page = loadContentScript('content-ancestry.js', { pathname: '/family-trees/111/people/222/hints', origin: 'https://www.ancestry.com' });
  try {
    await wait(50);
    for (const href of ['#', 'https://support.ancestry.com/s/article/hints', 'https://www.ancestry.com/family-tree/person/tree/333/person/444/facts']) {
      const a = fakeLink(href, { text: 'x', card });
      page.listeners.click[0]({ button: 0, target: { closest: (sel) => (sel === 'a[href]' ? a : null) } });
    }
  } finally {
    page.stop();
  }
  assert.deepStrictEqual(page.sent.filter((m) => m.type === 'ancLook').map((m) => m.key), []);
});

test('Ancestry hints: a bare scan link beside the title box finds its own pills, not the title\'s', async () => {
  const card = fakeCard('recordCardV2CollectionTitle');
  const box = { textContent: '1880 census Thomas Hartley', children: [] };
  box.appendChild = (el) => { box.children.push(el); return el; };
  box.querySelector = (sel) => {
    const m = /data-nsi-owner="([^"]+)"/.exec(sel);
    return box.children.find((c) => (m ? c.dataset.nsiOwner === m[1] : !!c.dataset.nsiFor)) || null;
  };
  const scan = fakeLink('https://www.ancestry.com/imageviewer/collections/6742/images/4321_0001', { cls: 'photoContainer photo', card });
  scan.nextElementSibling = box;
  const title = fakeLink('https://www.ancestry.com/search/collections/6742/records/4321', { text: '1880 census', cls: 'link recordCardV2CollectionTitle', card });
  // The title sits first in the box, so its pills land ahead of the scan's.
  title.insertAdjacentElement = (pos, el) => { box.children.unshift(el); title.nextElementSibling = el; };
  card.title = title;
  const day = Date.now() - 24 * 3600 * 1000;
  const page = loadContentScript('content-ancestry.js', {
    pathname: '/family-trees/111/people/222/hints', origin: 'https://www.ancestry.com',
    querySelectorAll: (sel) => (sel.includes('imageviewer') ? [scan, title] : []),
    storage: { anc: { 'rec:6742:4321': { t: [day], last: day, mark: null }, 'img:6742:4321_0001': { t: [day], last: day, mark: null } } },
  });
  try {
    await wait(700);
    const first = box.children.length;
    page.listeners.click[0]({ button: 0, target: { closest: (sel) => (sel === 'a[href]' ? title : null) } });
    await wait(700);
    assert.strictEqual(first, 2, 'the record and its scan each get one set');
    assert.strictEqual(box.children.length, 2, 'a second render added more pills');
  } finally {
    page.stop();
  }
});

// A tree person's gallery is counted on its own, beside their sources:
// "1 of 2 sources saved · 1 of 3 gallery items saved". Made-up ids only.
const GAL = (n) => 'abcdef0' + n + '-2222-4333-8444-abcdefabcdef';
const tile = (href) => ({ getAttribute: (n) => (n === 'href' ? href : null), textContent: '', innerText: '' });
const sourceLink = (dbId, recId) => tile('https://www.ancestry.com/search/collections/' + dbId + '/records/' + recId);
const FILED = { state: 'filed', sid: 'S-0a1b2c3d4e' };
const DAY_MS = 24 * 3600 * 1000;

async function personPage({ pathname, search = '', tiles = [], sources = null, storage = {}, nodes = [] }) {
  const page = loadContentScript('content-ancestry.js', {
    pathname, search, origin: 'https://www.ancestry.com', storage,
    getElementById: (id) => (id === 'sourcesSection' && sources ? { querySelectorAll: () => sources } : null),
    querySelector: (sel) => (sel === 'h1' ? { innerText: 'Thomas Hartley' } : null),
    querySelectorAll: (sel) => (sel.includes('mediaGalleryItem') ? tiles : sel === 'div.node[id]' ? nodes : []),
  });
  try {
    await wait(700);
  } finally {
    page.stop();
  }
  const badge = page.document.body.children.find((c) => c.id === 'nsi-anc-person');
  const pills = badge ? badge.children[0].children : [];
  return { page, badge, texts: pills.map((p) => p.textContent), titles: pills.map((p) => p.title || '') };
}

test('Ancestry gallery: the items on a gallery page are remembered for that person and page', async () => {
  const { page } = await personPage({
    pathname: '/family-tree/person/tree/111/person/222/gallery', search: '?galleryPage=2&tab=gallery',
    tiles: [
      tile('/mediaui-viewer/collection/1030/tree/111/person/222/media/' + GAL(1).toUpperCase() + '?galleryPage=2'),
      tile('/mediaui-viewer/tree/111/person/222/media/' + GAL(1)),
      tile('/mediaui-viewer/tree/111/person/222/media/' + GAL(2)),
    ],
  });
  const msg = page.sent.find((m) => m.type === 'ancPerson');
  assert.ok(msg, 'the gallery page was not remembered');
  assert.strictEqual(msg.key, '111:222');
  // (copied out of the page's own world, as the message would be)
  assert.deepStrictEqual(JSON.parse(JSON.stringify(msg.gallery)), { page: 2, keys: ['med:' + GAL(1), 'med:' + GAL(2)] });
  assert.strictEqual(msg.keys, undefined, 'a gallery page claimed the person has no sources');
});

// PR #271 audit: moving from one person's gallery to another's inside the
// page, Seen It can read before Ancestry redraws the tiles. Tiles belonging to
// someone else are never filed under the person in the address bar.
test('Ancestry gallery: tiles still showing another person\'s gallery are not counted for this person', async () => {
  const stale = await personPage({
    pathname: '/family-tree/person/tree/111/person/333/gallery', search: '?galleryPage=1',
    tiles: [tile('/mediaui-viewer/tree/111/person/222/media/' + GAL(1)), tile('/mediaui-viewer/collection/1030/tree/111/person/222/media/' + GAL(2))],
  });
  assert.deepStrictEqual(stale.page.sent.filter((m) => m.type === 'ancPerson'), [], 'another person\'s tiles were filed under this one');
  // once redrawn, her own tiles are counted (and a stray tile of someone else's is not)
  const own = await personPage({
    pathname: '/family-tree/person/tree/111/person/333/gallery', search: '?galleryPage=1',
    tiles: [tile('/mediaui-viewer/tree/111/person/333/media/' + GAL(3)), tile('/mediaui-viewer/tree/111/person/222/media/' + GAL(1))],
  });
  const msg = own.page.sent.find((m) => m.type === 'ancPerson');
  assert.ok(msg, 'her own gallery page was not remembered');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(msg.gallery)), { page: 1, keys: ['med:' + GAL(3)] });
});

test('Ancestry Facts page: a gallery page stored as a bare list still counts', async () => {
  const p = hartley({ 1: ['med:' + GAL(1)] });
  p.gallery.pages[2] = ['med:' + GAL(2)];
  const { texts } = await personPage({ ...FACTS, storage: {
    settings: { advanced: true }, known: { ['med:' + GAL(1)]: FILED }, ancPeople: { '111:222': p },
  } });
  assert.ok(texts.includes('1 of 2 gallery items saved'), JSON.stringify(texts));
});

// Each gallery page is stored with the time it was read: { keys, at }.
const hartley = (gallery) => ({ name: 'Thomas Hartley', keys: ['rec:1001:2002', 'rec:3003:4004'], at: Date.now() - DAY_MS,
  ...(gallery ? { gallery: { at: Date.now() - DAY_MS,
    pages: Object.fromEntries(Object.entries(gallery).map(([n, keys]) => [n, { keys, at: Date.now() - DAY_MS }])) } } : {}) });
const FACTS = { pathname: '/family-tree/person/tree/111/person/222/facts', sources: [sourceLink(1001, 2002), sourceLink(3003, 4004)] };

test('Ancestry Facts page: the gallery gets its own count beside the sources', async () => {
  const { texts, titles } = await personPage({ ...FACTS, storage: {
    settings: { advanced: true },
    known: { 'rec:1001:2002': FILED, ['med:' + GAL(1)]: FILED },
    ancPeople: { '111:222': hartley({ 1: ['med:' + GAL(1), 'med:' + GAL(2)], 2: ['med:' + GAL(3)] }) },
  } });
  assert.strictEqual(texts[0], '1 of 2 sources saved');
  const g = texts.indexOf('1 of 3 gallery items saved');
  assert.ok(g > 0, 'no gallery count: ' + JSON.stringify(texts));
  assert.match(titles[g], /pages 1–2/);
});

test('Ancestry Facts page: a person whose gallery was never opened shows only the sources, as before', async () => {
  const { texts } = await personPage({ ...FACTS, storage: {
    settings: { advanced: true },
    known: { 'rec:1001:2002': FILED },
    ancPeople: { '111:222': hartley(null) },
  } });
  assert.deepStrictEqual(texts, ['1 of 2 sources saved', '📚 1 in archive', '○ 1 not saved']);
});

test('Ancestry Facts page: outside advanced mode nothing about the archive is shown', async () => {
  const { badge } = await personPage({ ...FACTS, storage: {
    known: { 'rec:1001:2002': FILED, ['med:' + GAL(1)]: FILED },
    ancPeople: { '111:222': hartley({ 1: ['med:' + GAL(1)] }) },
  } });
  assert.strictEqual(badge, undefined);
});

test('Ancestry gallery: the badge counts the page in front of her, with the sources from the last Facts visit', async () => {
  const { texts, titles } = await personPage({
    pathname: '/family-tree/person/tree/111/person/222/gallery', search: '?galleryPage=1&tab=gallery',
    tiles: [tile('/mediaui-viewer/tree/111/person/222/media/' + GAL(1))],
    storage: {
      settings: { advanced: true },
      known: { 'rec:1001:2002': FILED, ['med:' + GAL(1)]: FILED },
      // GAL(2) was on page 1 last time; it has since been taken off Ancestry
      ancPeople: { '111:222': hartley({ 1: ['med:' + GAL(1), 'med:' + GAL(2)], 2: ['med:' + GAL(3)] }) },
    },
  });
  assert.strictEqual(texts[0], '1 of 2 sources saved');
  assert.ok(texts.includes('1 of 2 gallery items saved'), JSON.stringify(texts));
  assert.match(titles[0], /Facts page/);
});

test('Ancestry gallery: before her Facts page is opened, only the gallery is counted', async () => {
  const { texts } = await personPage({
    pathname: '/family-tree/person/tree/111/person/222/gallery',
    tiles: [tile('/mediaui-viewer/tree/111/person/222/media/' + GAL(1))],
    storage: { settings: { advanced: true }, known: { ['med:' + GAL(1)]: FILED } },
  });
  assert.deepStrictEqual(texts, ['✓ 1 gallery item saved']);
});

test('Ancestry tree view: the chip still counts sources; its tooltip adds the gallery', async () => {
  const host = { children: [] };
  host.appendChild = (c) => { host.children.push(c); return c; };
  host.querySelector = () => host.children[0] || null;
  const node = { id: '222:0', querySelector: (sel) => (sel === '.vAlignContent' ? host : null) };
  await personPage({
    pathname: '/family-tree/tree/111/family', nodes: [node],
    storage: {
      settings: { advanced: true },
      known: { 'rec:1001:2002': FILED, ['med:' + GAL(1)]: FILED },
      ancPeople: { '111:222': hartley({ 1: ['med:' + GAL(1), 'med:' + GAL(2), 'med:' + GAL(3)] }) },
    },
  });
  const chip = host.children[0];
  assert.ok(chip, 'no chip on the person');
  assert.strictEqual(chip.textContent, '1/2');
  assert.match(chip.title, /Gallery items: 1 of 3 saved/);
});


// A Newspapers.com clipping saved to an Ancestry gallery is an Ancestry photo
// with its own media id. Its viewer page links the clipping back; once Seen
// It has read that link, the photo counts as saved whenever the archive holds
// the clipping. Made-up ids only.
const CLIP = 'clip:4321';
const npLink = (id) => tile('https://www.newspapers.com/clip/' + id + '/hartley-farm-sale/');

// A photo's viewer page. `card` are the links in the sharer's card ("originally
// shared this article ... from Newspapers.com"), `other` the links anywhere
// else (a comment, the description). `pictures` are the viewer's <img>s, each
// with the media id it carries as its id and its alt text (the carousel keeps
// the neighbouring photos' pictures loaded); `panelTitle` is the title in the
// Details panel.
const TITLE = 'Hartley farm sale';
async function viewerPage({ card = [], other = [], pictures = [{ id: GAL(1), alt: TITLE }], panelTitle = TITLE, path = GAL(1) } = {}) {
  const page = loadContentScript('content-ancestry.js', {
    pathname: '/mediaui-viewer/collection/1030/tree/111/person/222/media/' + path, origin: 'https://www.ancestry.com',
    ...viewerDom(() => ({ card, other, pictures, panelTitle })),
  });
  try {
    await wait(700);
  } finally {
    page.stop();
  }
  return page.sent.filter((m) => m.type === 'ancMedClip');
}

// querySelector / querySelectorAll for that page, read afresh on each call
// so a test can swap what it shows. img[id="..."] matches as a browser does:
// with the ` i` flag in any letter case.
function viewerDom(now) {
  return {
    querySelectorAll: (sel) => {
      const { card, other = [], pictures } = now();
      const img = /^img\[id="([^"]+)"( i)?\]$/.exec(sel);
      if (img) {
        return pictures.filter((p) => (img[2] ? p.id.toLowerCase() === img[1].toLowerCase() : p.id === img[1]))
          .map((p) => ({ id: p.id, getAttribute: (n) => (n === 'alt' ? p.alt : n === 'id' ? p.id : null) }));
      }
      if (!sel.includes('newspapers.com/clip/') || sel.includes('/records/')) return [];
      return sel.includes('.mediaOriginallyShared') ? card : card.concat(other);
    },
    querySelector: (sel) => {
      const { panelTitle, pictures } = now();
      const byId = /^\[id="([^"]+)" i\]$/.exec(sel); // any element with that id: the pictures are the page's only ones
      if (byId) return pictures.find((p) => p.id.toLowerCase() === byId[1].toLowerCase()) || null;
      return sel === '#mediaViewerDetailsPanel .mediaTitle .mediaDataFieldValue' && panelTitle ? { textContent: panelTitle } : null;
    },
  };
}

test('Ancestry photo viewer: a photo that links its Newspapers.com clipping is remembered as that clipping', async () => {
  const sent = await viewerPage({ card: [npLink(4321), npLink(4321)] });
  assert.ok(sent.length, 'the clipping link was not remembered');
  assert.deepStrictEqual({ key: sent[0].key, clip: sent[0].clip }, { key: 'med:' + GAL(1), clip: CLIP });
});

test('Ancestry photo viewer: two different clippings on one page leave the photo as it was', async () => {
  assert.deepStrictEqual(await viewerPage({ card: [npLink(4321), npLink(4322)] }), []);
});

// Audit of #412: a headstone photo whose comment links the obituary clipping
// was remembered as that clipping, and showed "In archive" for good.
test('Ancestry photo viewer: a clipping linked only in a comment does not make a plain photo that clipping', async () => {
  assert.deepStrictEqual(await viewerPage({ other: [npLink(4321)] }), []);
});

test('Ancestry photo viewer: a comment linking another clipping does not hide the sharer\'s own', async () => {
  const sent = await viewerPage({ card: [npLink(4321)], other: [npLink(4322)] });
  assert.deepStrictEqual(sent.map((m) => m.clip), [CLIP]);
});

// Audit of #412: the arrow to the next photo changes the address before the
// picture and its details swap. A look in that gap filed the last photo's
// clipping under the next one, a plain photo, which kept it for good.
test('Ancestry photo viewer: while the last photo is still showing, its clipping is not filed under the next one', async () => {
  assert.deepStrictEqual(await viewerPage({ card: [npLink(4321)], path: GAL(2) }), []);
});

// Audit of #412: the viewer's carousel keeps the next photo's picture loaded,
// so on the arrow to it the picture was already there while the Details
// panel and sharer's card still showed the last photo, and its clipping was
// filed under the next one.
test('Ancestry photo viewer: a picture of the next photo in the carousel is not enough while the panel shows the last one', async () => {
  const pictures = [{ id: GAL(1), alt: TITLE }, { id: GAL(2), alt: 'Hartley headstone' }];
  assert.deepStrictEqual(await viewerPage({ card: [npLink(4321)], pictures, path: GAL(2) }), []);
  const sent = await viewerPage({ card: [npLink(4321)], pictures, path: GAL(1) });
  assert.deepStrictEqual(sent.map((m) => [m.key, m.clip]), [['med:' + GAL(1), CLIP]]);
});

test('Ancestry photo viewer: letter case and spacing in the title do not matter; a panel with no title is not checked', async () => {
  const pictures = [{ id: GAL(1), alt: 'Hartley  Farm Sale' }];
  assert.strictEqual((await viewerPage({ card: [npLink(4321)], pictures })).length, 1);
  assert.strictEqual((await viewerPage({ card: [npLink(4321)], pictures, panelTitle: null })).length, 1);
});

// The same on the viewer page saved from Ancestry (tests/fixtures): its
// carousel holds the clipping's picture and a plain photo's.
test('Ancestry photo viewer, saved page: the plain photo beside a clipping is not filed as that clipping', async () => {
  const html = fs.readFileSync(path.join(__dirname, '..', '..', 'tests', 'fixtures', 'capture-samples', 'ancestry-media-clipping.html'), 'utf8');
  const pictures = [...html.matchAll(/<img id="([^"]+)"[^>]*\balt="([^"]*)"/g)].map((m) => ({ id: m[1], alt: m[2] }));
  const panelTitle = /class="[^"]*\bmediaTitle\b[^"]*"[^>]*><span class="mediaDataFieldValue[^"]*">([^<]*)/.exec(html)[1];
  const card = [.../<span class="mediaOriginallyShared">([\s\S]*?)<\/span><\/p>/.exec(html)[1].matchAll(/<a href="([^"]+)"/g)]
    .map((m) => tile(m[1])).filter((a) => /newspapers\.com\/(clip|article)\//.test(a.getAttribute('href')));
  const ids = [...new Set(pictures.map((p) => p.id))];
  assert.strictEqual(ids.length, 2, 'the saved page no longer has two photos in its carousel');
  const [clipping, plain] = ids;
  assert.deepStrictEqual(await viewerPage({ card, pictures, panelTitle, path: plain }), []);
  assert.deepStrictEqual((await viewerPage({ card, pictures, panelTitle, path: clipping })).map((m) => m.key), ['med:' + clipping]);
});

test('Ancestry photo viewer: the photo is recognised by its picture whatever the letter case of its id', async () => {
  const sent = await viewerPage({ card: [npLink(4321)], pictures: [{ id: GAL(1).toUpperCase(), alt: TITLE }], path: GAL(1) });
  assert.deepStrictEqual(sent.map((m) => m.key), ['med:' + GAL(1)]);
});

test('Ancestry photo viewer: moving to the next photo learns nothing until that photo shows', async () => {
  let shown = { card: [npLink(4321)], pictures: [{ id: GAL(1), alt: TITLE }], panelTitle: TITLE };
  const page = loadContentScript('content-ancestry.js', {
    pathname: '/mediaui-viewer/collection/1030/tree/111/person/222/media/' + GAL(1), origin: 'https://www.ancestry.com',
    ...viewerDom(() => shown),
  });
  try {
    await wait(700);
    page.location.pathname = '/mediaui-viewer/collection/1030/tree/111/person/222/media/' + GAL(2);
    page.location.href = page.location.origin + page.location.pathname;
    await wait(1400); // the address moved; photo 1 is still on the page
    shown = { card: [], pictures: [{ id: GAL(2), alt: 'Hartley headstone' }], panelTitle: 'Hartley headstone' }; // photo 2: a plain photo
    await wait(1400);
  } finally {
    page.stop();
  }
  const sent = page.sent.filter((m) => m.type === 'ancMedClip').map((m) => [m.key, m.clip]);
  assert.deepStrictEqual(sent, [['med:' + GAL(1), CLIP]]);
});

// A gallery tile: a link inside .mediaGalleryItem, pills placed after it.
function galleryTile(href) {
  const a = tile(href);
  a.textContent = a.innerText = 'Hartley farm sale';
  a.inserted = [];
  a.nextElementSibling = null;
  a.classList = { contains: () => false };
  a.closest = (sel) => (sel === '.mediaGalleryItem' ? {} : null);
  a.insertAdjacentElement = (pos, el) => { a.inserted.push(el); a.nextElementSibling = el; };
  return a;
}

async function galleryWith(storage, tiles) {
  const page = loadContentScript('content-ancestry.js', {
    pathname: '/family-tree/person/tree/111/person/222/gallery', search: '?tab=gallery', origin: 'https://www.ancestry.com', storage,
    querySelector: (sel) => (sel === 'h1' ? { innerText: 'Thomas Hartley' } : null),
    querySelectorAll: (sel) => (sel.includes('mediaGalleryItem') || sel.includes('mediaui-viewer') ? tiles : []),
  });
  try {
    await wait(700);
  } finally {
    page.stop();
  }
  const badge = page.document.body.children.find((c) => c.id === 'nsi-anc-person');
  return { texts: badge ? badge.children[0].children.map((p) => p.textContent) : [] };
}

const pillTexts = (a) => (a.inserted[0] ? a.inserted[0].children.map((p) => p.textContent) : []);

test('Ancestry gallery: a photo that is a clipping the archive holds counts as saved, on its tile and in the count', async () => {
  const a = galleryTile('/mediaui-viewer/collection/1030/tree/111/person/222/media/' + GAL(1));
  const b = galleryTile('/mediaui-viewer/collection/1030/tree/111/person/222/media/' + GAL(2));
  const { texts } = await galleryWith({
    settings: { advanced: true },
    known: { [CLIP]: FILED },
    anc: { ['med:' + GAL(1)]: { t: [], last: 0, mark: null, clip: CLIP } },
  }, [a, b]);
  assert.ok(texts.includes('1 of 2 gallery items saved'), JSON.stringify(texts));
  assert.deepStrictEqual(pillTexts(a), ['📚 In archive · ' + FILED.sid]);
  assert.deepStrictEqual(pillTexts(b), ['○ Not in archive'], 'an unsaved tile was left blank');
});

test('Ancestry gallery: outside advanced mode an unsaved tile stays blank', async () => {
  const a = galleryTile('/mediaui-viewer/collection/1030/tree/111/person/222/media/' + GAL(2));
  await galleryWith({ known: { [CLIP]: FILED } }, [a]);
  assert.deepStrictEqual(a.inserted, []);
});

// Audit of #412: switching advanced mode off while a page is open left the
// archive pills up ("Not in archive" on every unsaved gallery tile), though
// outside advanced mode nothing about the archive is shown.
test('Ancestry gallery: switching advanced mode off takes "Not in archive" off the tiles', async () => {
  const a = galleryTile('/mediaui-viewer/collection/1030/tree/111/person/222/media/' + GAL(2));
  a.insertAdjacentElement = (pos, el) => {
    a.inserted.push(el);
    a.nextElementSibling = el;
    el.remove = () => { a.nextElementSibling = null; };
  };
  const page = loadContentScript('content-ancestry.js', {
    pathname: '/family-tree/person/tree/111/person/222/gallery', search: '?tab=gallery', origin: 'https://www.ancestry.com',
    storage: { settings: { advanced: true }, known: { [CLIP]: FILED } },
    querySelector: (sel) => (sel === 'h1' ? { innerText: 'Thomas Hartley' } : null),
    querySelectorAll: (sel) => (sel.includes('mediaGalleryItem') || sel.includes('mediaui-viewer') ? [a] : []),
  });
  try {
    await wait(700);
    assert.deepStrictEqual(pillTexts(a), ['○ Not in archive']);
    page.fireChange({ settings: { newValue: { advanced: false } } });
    await wait(700);
  } finally {
    page.stop();
  }
  assert.strictEqual(a.nextElementSibling, null, 'the tile still shows ' + JSON.stringify(pillTexts(a)));
});

// The same for the badge on a record's own page, on every site.
for (const [label, file, pathname, origin] of [
  ['Ancestry record', 'content-ancestry.js', '/search/collections/1001/records/2002', 'https://www.ancestry.com'],
  ['FamilySearch record', 'content-familysearch.js', '/ark:/61903/1:1:ABCD-123', 'https://www.familysearch.org'],
  ['Find a Grave memorial', 'content-findagrave.js', '/memorial/5505/thomas-hartley', 'https://www.findagrave.com'],
]) {
  test(label + ': switching advanced mode off takes "Not in archive" off the badge', async () => {
    const page = loadContentScript(file, { pathname, origin, storage: { settings: { advanced: true }, known: { [CLIP]: FILED } } });
    const badgeTexts = () => {
      const box = page.document.body.children.find((c) => c.id === 'nsi-anc-badge');
      return box ? box.children[0].children.map((p) => p.textContent) : [];
    };
    try {
      await wait(700);
      assert.ok(badgeTexts().includes('○ Not in archive'), JSON.stringify(badgeTexts()));
      page.fireChange({ settings: { newValue: { advanced: false } } });
      await wait(700);
    } finally {
      page.stop();
    }
    assert.deepStrictEqual(badgeTexts(), []);
  });
}

// On Newspapers.com: a page with your clippings, none in the archive.
test('Newspapers.com page: switching advanced mode off takes "not in archive" off the page badge', async () => {
  const page = loadContentScript(['cliplist.js', 'searchrun.js', 'content.js'], {
    pathname: '/image/5150/', origin: 'https://www.newspapers.com',
    storage: { settings: { advanced: true }, known: { [CLIP]: FILED }, clips: { 5150: [{ id: '4399', title: 'Hartley farm sale' }] } },
  });
  const texts = () => {
    const box = page.document.body.children.find((c) => c.id === 'nsi-page-badge');
    return box ? box.children[0].children.map((p) => p.textContent) : [];
  };
  try {
    await wait(700);
    assert.ok(texts().some((t) => /not in archive/.test(t)), JSON.stringify(texts()));
    page.fireChange({ settings: { newValue: { advanced: false } } });
    await wait(700);
  } finally {
    page.stop();
  }
  assert.ok(!texts().some((t) => /in archive/.test(t)), JSON.stringify(texts()));
});

test('Ancestry tree view: a person whose Facts page was never opened gets a "?" chip that says how to fill it', async () => {
  const host = { children: [] };
  host.appendChild = (c) => { host.children.push(c); return c; };
  host.querySelector = () => host.children[0] || null;
  const node = { id: '333:0', querySelector: (sel) => (sel === '.vAlignContent' ? host : null) };
  await personPage({
    pathname: '/family-tree/tree/111/family', nodes: [node],
    storage: { settings: { advanced: true }, ancPeople: { '111:222': hartley(null) } },
  });
  const chip = host.children[0];
  assert.ok(chip, 'no chip on a person Seen It has not read');
  assert.strictEqual(chip.textContent, '?');
  assert.match(chip.title, /Open their Facts page/);
});

test('Ancestry tree view: an "Add mother" box gets no chip', async () => {
  const host = { children: [] };
  host.appendChild = (c) => { host.children.push(c); return c; };
  host.querySelector = () => host.children[0] || null;
  const node = { id: '1:4', classList: { contains: (c) => c === 'add' }, querySelector: (sel) => (sel === '.vAlignContent' ? host : null) };
  await personPage({ pathname: '/family-tree/tree/111/family', nodes: [node], storage: { settings: { advanced: true } } });
  assert.deepStrictEqual(host.children, []);
});

// An entry in a Find a Grave Index (Ancestry's collection 60525,
// FamilySearch's 2221801) indexes one memorial: once read, it counts as
// saved when the archive holds that memorial. Made-up ids only.
const MEMORIAL = 'https://www.findagrave.com/memorial/5505/thomas-hartley';
function recordFields(links) {
  return { querySelectorAll: (sel) => (sel.includes('findagrave.com/memorial/') ? links : []) };
}

async function ancRecord(pathname, fields, storage) {
  const page = loadContentScript('content-ancestry.js', {
    pathname, origin: 'https://www.ancestry.com', storage,
    getElementById: (id) => (id === 'recordFieldsContainer' ? fields : null),
  });
  try {
    await wait(700);
  } finally {
    page.stop();
  }
  return page;
}

test('Ancestry Find a Grave Index entry: the memorial in its URL field is remembered for it', async () => {
  const page = await ancRecord('/search/collections/60525/records/4321', recordFields([tile(MEMORIAL), tile(MEMORIAL + '?x=1')]));
  const sent = page.sent.filter((m) => m.type === 'ancRecFag');
  assert.ok(sent.length, 'the memorial was not remembered');
  assert.deepStrictEqual({ key: sent[0].key, fag: sent[0].fag }, { key: 'rec:60525:4321', fag: 'fag:5505' });
});

test('Ancestry: another collection, or two memorials in the fields, remembers nothing', async () => {
  let page = await ancRecord('/search/collections/90001/records/4321', recordFields([tile(MEMORIAL)]));
  assert.deepStrictEqual(page.sent.filter((m) => m.type === 'ancRecFag'), []);
  page = await ancRecord('/search/collections/60525/records/4321',
    recordFields([tile(MEMORIAL), tile('https://www.findagrave.com/memorial/5506/margaret-hartley')]));
  assert.deepStrictEqual(page.sent.filter((m) => m.type === 'ancRecFag'), []);
});

test('Ancestry Find a Grave Index entry: in the archive when the memorial is, with or without its photos', async () => {
  for (const known of [{ 'fag:5505': FILED }, { 'fagidx:5505': FILED }]) {
    const page = await ancRecord('/search/collections/60525/records/4321', null, {
      settings: { advanced: true }, known,
      anc: { 'rec:60525:4321': { t: [], last: 0, mark: null, fag: 'fag:5505' } },
    });
    const badge = page.document.body.children.find((c) => c.id === 'nsi-anc-badge');
    assert.ok(badge, 'no badge');
    assert.deepStrictEqual(badge.children[0].children.map((p) => p.textContent), ['📚 In archive · ' + FILED.sid]);
    assert.match(badge.children[0].children[0].title, /Find a Grave memorial/);
  }
});

const FS_ENTRY = (extra) => Object.assign({
  sourceDescriptions: [{ about: 'https://www.familysearch.org/platform/records/collections/2221801' }],
  fields: [{ type: 'http://familysearch.org/types/fields/ExtRecordId', values: [{ labelId: 'EXT_RECORD_ID_ORIG', text: '5505' }] }],
}, extra || {});

async function fsRecord(title, answer) {
  const calls = [];
  const page = loadContentScript('content-familysearch.js', {
    pathname: '/ark:/61903/1:1:ABCD-123', origin: 'https://www.familysearch.org', title,
    fetch: async (url, init) => {
      calls.push({ url, accept: init.headers.Accept });
      return { ok: true, status: 200, text: async () => JSON.stringify(answer) };
    },
  });
  try {
    await wait(1900);
  } finally {
    page.stop();
  }
  return { sent: page.sent.filter((m) => m.type === 'ancRecFag'), calls };
}

test('FamilySearch Find a Grave Index entry: the memorial is read from the entry\'s own data, once', async () => {
  const { sent, calls } = await fsRecord('Thomas Hartley, "Find a Grave Index" • FamilySearch', FS_ENTRY());
  assert.deepStrictEqual(calls, [{ url: '/ark:/61903/1:1:ABCD-123', accept: 'application/x-gedcomx-v1+json' }]);
  assert.deepStrictEqual(sent.map((m) => [m.key, m.fag]), [['fsrec:ABCD-123', 'fag:5505']]);
});

test('FamilySearch: another collection is never asked; an entry of another collection remembers nothing', async () => {
  let r = await fsRecord('Thomas Hartley, "California Death Index, 1940-1997" • FamilySearch', FS_ENTRY());
  assert.deepStrictEqual(r.calls, []);
  r = await fsRecord('Thomas Hartley, "Find a Grave Index" • FamilySearch',
    FS_ENTRY({ sourceDescriptions: [{ about: 'https://www.familysearch.org/platform/records/collections/2015582' }] }));
  assert.deepStrictEqual(r.sent, []);
});
