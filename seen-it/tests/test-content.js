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
    storage: { anc: { ['med:' + MED]: { t: [Date.now() - 86400000], last: Date.now() - 86400000, mark: null } } },
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

