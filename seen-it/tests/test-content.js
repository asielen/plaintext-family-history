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
