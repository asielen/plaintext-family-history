// The capture panel driven end to end on a WikiTree profile, without a
// browser (tests/panel-harness.js). The content script is played by the real
// readers in lib/wikitree-record.js, and WikiTree's file host by a stand-in
// that records every request.
//
// What it guards, in the human's terms:
//   - Capture on a profile saves its ID, last edit and family as data, the
//     page's own text (searchable, not marked as a machine reading) and a
//     citation dated the day he looked;
//   - the cited sources land in the notes as a list he can tick: linked ones
//     with where they point, then "To find:";
//   - a scan, a PDF or a transcription someone put on WikiTree as a source is
//     saved as source-N, tied to the source that cites it; a source on
//     another site (Ancestry, FamilySearch) is never fetched;
//   - at most 10 such files and 10 photos in one capture, and the notes say
//     how many were left.
//
//   node --test browser-companion/tests/test-panel-wikitree.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const { openPanel } = require('./panel-harness.js');
const wt = require('../src/lib/wikitree-record.js');
const { parse } = require('./mini-dom.js');

const SAMPLES = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'capture-samples');
const API = JSON.parse(fs.readFileSync(path.join(SAMPLES, 'wikitree-api.json'), 'utf8'));
const PAGE_HTML = fs.readFileSync(path.join(SAMPLES, 'wikitree-profile.html'), 'utf8');
const PROFILE = 'https://www.wikitree.com/wiki/Hartley-6084';
const ORIGINAL = (h, hh, name) => 'https://www.wikitree.com/photo.php/' + h + '/' + hh + '/' + name;

// The answer of content.js wikitreeInfo, composed from the real readers as
// content.js composes it. `bio` replaces the biography; `refused` makes the
// API refuse (the page is read instead); `filePages` are the held files'
// own pages ({ fileName: { caption, originalUrl } }); `photos` the getPhotos answer.
function infoFor(tabUrl, today, { bio, refused, filePages = {}, photos = API.noPhotos, photosTotal = 0, slowDown = false } = {}) {
  const parsed = wt.wtParsePage(tabUrl);
  const json = JSON.parse(JSON.stringify(API.profile));
  if (bio !== undefined) json[0].profile.bio = bio;
  const api = refused ? wt.wtRecordFromApi(API.refused, parsed) : wt.wtRecordFromApi(json, parsed);
  let record;
  let sources;
  let bioText;
  if (api.ok) {
    record = api.record;
    sources = wt.wtSources(api.bio);
    bioText = wt.wtPlainText(api.bio);
  } else {
    const page = wt.wtRecordFromPage(parse(PAGE_HTML), parsed);
    ({ record, sources, bioText } = page);
  }
  const hostedAll = wt.wtHostedFiles(sources);
  const hosted = hostedAll.slice(0, wt.WT_MAX_HOSTED).map((h) => {
    const out = Object.assign({ caption: null, originalUrl: null, text: null }, h);
    if (h.kind === 'space') {
      const got = wt.wtRecordFromApi(API.space, wt.wtParsePage(h.pageUrl));
      out.caption = got.record.name;
      out.text = wt.wtPlainText(got.bio);
    } else {
      Object.assign(out, filePages[h.fileName] || { error: 'WikiTree did not show the file’s page (HTTP 404)' });
    }
    return out;
  });
  return {
    ok: true, via: api.ok ? 'api' : 'page', apiError: api.ok ? null : api.error, key: parsed.key, record,
    bioWiki: api.ok ? api.bio : null, bioText, sources, citation: wt.wtCitation(record, today),
    title: record.name + ' (' + record.wikitreeId + ') - WikiTree profile',
    photos: wt.wtPhotosFromApi(photos), photosTotal, photosError: null, hosted, hostedTotal: hostedAll.length, slowDown,
  };
}

function wikitreeSite(opts = {}) {
  return async (msg, tab) => {
    switch (msg.action) {
      case 'prefill':
        return { ok: true, prefill: { url: tab.url, title: 'Calvin Hartley (1842-1918) | WikiTree FREE Family Tree',
          repository: 'www.wikitree.com', recipeHint: 'wikitree', people: ['Calvin Hartley'],
          wikitreePage: !!wt.wtParsePage(tab.url) } };
      case 'wikitreeInfo': return infoFor(tab.url, msg.today, opts);
      case 'pagehtml': return { ok: true, html: '<html>' + tab.url + '</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy of ' + tab.url + '</html>' };
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

// WikiTree's file host: `answer(url, init)` or the file, typed by its name.
function fileHost(answer) {
  return async (url, init) => {
    if (answer) {
      const a = await answer(url, init);
      if (a) return a;
    }
    const type = /\.pdf$/i.test(url) ? 'application/pdf' : /\.png$/i.test(url) ? 'image/png' : 'image/jpeg';
    return { ok: true, status: 200, url, headers: { get: () => type }, blob: async () => new Blob(['file ' + url], { type }) };
  };
}

const open = (opts = {}) => openPanel(Object.assign({ url: PROFILE, respond: wikitreeSite(opts), fetch: fileHost(opts.answer) }, opts));
const asset = (spec, name) => spec.assets.find((a) => a.filename === name);
const roles = (cap) => cap.assets.filter((a) => a.role !== 'webpage').map((a) => [a.file, a.role]);

test('a profile is saved with its text, what WikiTree says about it, its citation and its sources to find', async () => {
  const p = await open();
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const b = p.written[0];
  const cap = p.capJson(b);
  assert.strictEqual(cap.title, 'Calvin Hartley (Hartley-6084) - WikiTree profile');
  assert.strictEqual(cap.url, PROFILE);
  assert.strictEqual(cap.recipe_hint, 'wikitree');
  assert.strictEqual(cap.repository, 'WikiTree');
  assert.strictEqual(cap.source_type, 'website');
  assert.strictEqual(cap.source_class, 'derivative');
  assert.match(cap.citation, /^WikiTree, "Calvin Hartley \(Hartley-6084\)," \(https:\/\/www\.wikitree\.com\/wiki\/Hartley-6084 : last edited 2023-05-26; accessed \d{4}-\d{2}-\d{2}\)\.$/);
  assert.deepStrictEqual(roles(cap), [['page.md', 'transcript'], ['record-data.json', 'site-data']]);
  // The page itself is the main file (its copy, kept by default), and its
  // text is the page's own words: no machine-reading marker.
  assert.deepStrictEqual(cap.assets.filter((a) => a.is_primary).map((a) => a.role), ['webpage']);
  const text = await asset(b, 'page.md').blob.text();
  assert.match(text, /^# Calvin Hartley \(WikiTree Hartley-6084\)/);
  assert.match(text, /lived at Fairview on the paternal homestead/);
  assert.ok(!/AI-DRAFT/.test(text));
  const data = JSON.parse(await asset(b, 'record-data.json').blob.text());
  assert.strictEqual(data.record.wikitreeId, 'Hartley-6084');
  assert.strictEqual(data.record.lastEdited, '2023-05-26');
  assert.deepStrictEqual(data.record.parents.map((x) => x.wikitreeId), ['Hartley-6083', 'Webb-3']);
  assert.strictEqual(data.sources.length, 4);
  assert.match(data.biographyWikiText, /== Sources ==/);
  // The notes: the citation, then the sources as a list to tick.
  assert.match(cap.notes, /WikiTree citation: WikiTree, "Calvin Hartley/);
  assert.match(cap.notes, /### Cited sources\n\nTo find:\n- \[ \] \[S362\] History of the Hartley Family/);
  assert.strictEqual((cap.notes.match(/^- \[ \] /gm) || []).length, 4);
  assert.strictEqual(p.fetched.length, 0, 'nothing to download');
});

const HELD_BIO = [
  '== Biography ==',
  'Calvin married Ethel Cole in 1866.<ref>Marriage record, [https://www.wikitree.com/photo/jpg/Hartley-6084-2 image].</ref>',
  '== Sources ==',
  '* Deed, Fairview: https://www.wikitree.com/photo/pdf/Hartley-Deed-1',
  '* Family Bible: [[Space:Hartley_Family_Bible|transcription]]',
  '* Probate packet: https://www.ancestry.com/discoveryui-content/view/123:9071',
  '* 1850 census: {{FamilySearch Record|ABCD-123}}',
  '* Family letters held by Edith Louise Hartley',
].join('\n');
const FILE_PAGES = {
  'Hartley-6084-2.jpg': { caption: 'Hartley-Cole marriage record, Fairview', originalUrl: ORIGINAL('7', '7a', 'Hartley-6084-2.jpg') },
  'Hartley-Deed-1.pdf': { caption: 'Deed, Hartley to Cole, 1871', originalUrl: ORIGINAL('3', '3f', 'Hartley-Deed-1.pdf') },
};

test('a scan, a PDF and a transcription WikiTree holds are saved as source-N, tied to the source that cites them', async () => {
  const p = await open({ bio: HELD_BIO, filePages: FILE_PAGES });
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const b = p.written[0];
  const cap = p.capJson(b);
  assert.deepStrictEqual(roles(cap), [
    ['page.md', 'transcript'],
    ['source-1.jpg', 'source-1'],
    ['source-2.pdf', 'source-2'],
    ['source-3.md', 'source-3'],
    ['record-data.json', 'site-data'],
  ]);
  // Only WikiTree's originals were downloaded: never Ancestry or FamilySearch,
  // never with cookies, a referrer or a redirect.
  assert.deepStrictEqual(p.fetched.map((f) => f.url), [FILE_PAGES['Hartley-6084-2.jpg'].originalUrl,
    FILE_PAGES['Hartley-Deed-1.pdf'].originalUrl]);
  for (const f of p.fetched) {
    assert.strictEqual(f.init.credentials, 'omit');
    assert.strictEqual(f.init.redirect, 'error');
    assert.strictEqual(f.init.referrerPolicy, 'no-referrer');
  }
  assert.strictEqual((await asset(b, 'source-2.pdf').blob.text()), 'file ' + FILE_PAGES['Hartley-Deed-1.pdf'].originalUrl);
  const bible = await asset(b, 'source-3.md').blob.text();
  assert.match(bible, /^# Hartley Family Bible\n/);
  assert.match(bible, /- Calvin Hartley born 12 March 1842/);
  const data = JSON.parse(await asset(b, 'record-data.json').blob.text());
  assert.deepStrictEqual(data.hosted.map((h) => [h.n, h.file, h.caption, h.entries]), [
    [1, 'source-1.jpg', 'Hartley-Cole marriage record, Fairview', [0]],
    [2, 'source-2.pdf', 'Deed, Hartley to Cole, 1871', [1]],
    [3, 'source-3.md', 'Hartley Family Bible', [2]],
  ]);
  // In the notes: each saved file on the line of the source that cites it.
  assert.match(cap.notes, /- \[ \] Marriage record, image\. - https:\/\/www\.wikitree\.com\/photo\/jpg\/Hartley-6084-2; saved in this source as source-1\.jpg \("Hartley-Cole marriage record, Fairview"\), from https:\/\/www\.wikitree\.com\/photo\/jpg\/Hartley-6084-2/);
  assert.match(cap.notes, /- \[ \] Deed, Fairview: https:\/\/www\.wikitree\.com\/photo\/pdf\/Hartley-Deed-1 - .*saved in this source as source-2\.pdf/);
  assert.match(cap.notes, /- \[ \] Probate packet: .* - https:\/\/www\.ancestry\.com\/discoveryui-content\/view\/123:9071\n/);
  assert.match(cap.notes, /To find:\n- \[ \] Family letters held by Edith Louise Hartley$/);
  assert.match(cap.notes, /3 cited files WikiTree holds saved \(source-1\.jpg, source-2\.pdf, source-3\.md\)/);
});

test('more than 10 files WikiTree holds: 10 are saved and the notes say how many were left', async () => {
  const bio = '== Sources ==\n' + Array.from({ length: 12 }, (_, i) =>
    '* Page ' + (i + 1) + ': https://www.wikitree.com/photo/jpg/Hartley-Ledger-' + (i + 1)).join('\n');
  const filePages = {};
  for (let i = 1; i <= 12; i++) {
    filePages['Hartley-Ledger-' + i + '.jpg'] = { caption: 'Ledger page ' + i, originalUrl: ORIGINAL('1', '1a', 'Hartley-Ledger-' + i + '.jpg') };
  }
  const p = await open({ bio, filePages });
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const cap = p.capJson(p.written[0]);
  assert.strictEqual(cap.assets.filter((a) => /^source-/.test(a.role)).length, 10);
  assert.strictEqual(p.fetched.length, 10);
  assert.match(cap.notes, /2 more cited files WikiTree holds were not saved \(at most 10 in one capture\)/);
  assert.match(p.result(), /2 more cited files/);
});

test('a held file that cannot be downloaded leaves the rest saved, and the notes say which and why', async () => {
  const p = await open({ bio: HELD_BIO, filePages: FILE_PAGES,
    answer: (url) => (/\.pdf$/.test(url) ? { ok: false, status: 404, url, headers: { get: () => 'text/html' } } : null) });
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const cap = p.capJson(p.written[0]);
  assert.ok(!cap.assets.some((a) => a.role === 'source-2'));
  assert.ok(cap.assets.some((a) => a.role === 'source-1'));
  assert.match(cap.notes, /1 cited file WikiTree holds could not be saved \(Hartley-Deed-1\.pdf: WikiTree refused the file \(HTTP 404\)\)/);
  const data = JSON.parse(await asset(p.written[0], 'record-data.json').blob.text());
  assert.strictEqual(data.hosted[1].file, null);
  assert.match(data.hosted[1].error, /HTTP 404/);
});

test('the profile\'s photos are saved as photo-N, and one that is also a cited file is saved once, as the source', async () => {
  const photos = JSON.parse(JSON.stringify(API.photos));
  photos[0].photos.push(Object.assign({}, photos[0].photos[0], { ImageName: 'Hartley-6084-2.jpg', Title: 'Marriage record',
    URL: '/photo/jpg/Hartley-6084-2', URL_300: '/photo.php/thumb/7/7a/Hartley-6084-2.jpg/300px-Hartley-6084-2.jpg' }));
  const p = await open({ bio: HELD_BIO, filePages: FILE_PAGES, photos, photosTotal: 3 });
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const cap = p.capJson(p.written[0]);
  assert.deepStrictEqual(cap.assets.filter((a) => /^photo-/.test(a.role)).map((a) => [a.file, a.role]),
    [['photo-1.jpg', 'photo-1'], ['photo-2.jpg', 'photo-2']]);
  assert.strictEqual(p.fetched.filter((f) => /Hartley-6084-2\.jpg$/.test(f.url)).length, 1);
  assert.match(cap.notes, /2 photos saved from the profile/);
});

test('with the page copy unticked, the page\'s text is the main file', async () => {
  const p = await open();
  p.$('cb-pagecopy').checked = false;
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const cap = p.capJson(p.written[0]);
  assert.ok(!cap.assets.some((a) => a.role === 'webpage'));
  assert.deepStrictEqual(cap.assets.filter((a) => a.is_primary).map((a) => a.file), ['page.md']);
});

test('when the API refuses, the page itself is read, and the notes say so', async () => {
  const p = await open({ refused: true });
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  const cap = p.capJson(p.written[0]);
  assert.match(cap.notes, /did not answer for this page \(WikiTree did not share this page.*\), so what is saved was read from the page itself/);
  assert.match(cap.notes, /To find:\n- \[ \] \[S362\]/);
  const data = JSON.parse(await asset(p.written[0], 'record-data.json').blob.text());
  assert.strictEqual(data.via, 'page');
  assert.strictEqual(data.record.from, 'page');
});

// Review of #512: when WikiTree already said "slow down" while its files'
// pages were read, the panel downloads nothing more from it in this capture.
test('after WikiTree said "slow down" while reading, no file is downloaded and the notes say why', async () => {
  const p = await open({ bio: HELD_BIO, filePages: FILE_PAGES, slowDown: true });
  await p.capture();
  assert.strictEqual(p.written.length, 1, p.result());
  assert.deepStrictEqual(p.fetched, []);
  const cap = p.capJson(p.written[0]);
  assert.match(cap.notes, /Hartley-6084-2\.jpg: not tried: WikiTree asked us to slow down/);
});
