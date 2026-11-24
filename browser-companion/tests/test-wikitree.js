// lib/wikitree-record.js: which WikiTree page an address is, what the
// public API and the page itself say, and the cited sources as a list he can
// tick through, including the scans and transcriptions WikiTree itself holds.
//
// Fixtures: tests/fixtures/capture-samples/wikitree-*.{json,html}, recorded
// from the live site on 2026-11-24 with the example-archive's names swapped in.
//
//   node --test browser-companion/tests/test-wikitree.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const wt = require('../src/lib/wikitree-record.js');
const { parse } = require('./mini-dom.js');

const SAMPLES = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'capture-samples');
const API = JSON.parse(fs.readFileSync(path.join(SAMPLES, 'wikitree-api.json'), 'utf8'));
const PAGE = fs.readFileSync(path.join(SAMPLES, 'wikitree-profile.html'), 'utf8');
const PHOTO_PAGE = fs.readFileSync(path.join(SAMPLES, 'wikitree-photo-page.html'), 'utf8');
const PROFILE = 'https://www.wikitree.com/wiki/Hartley-6084';

test('a profile address and a free-space page address are told apart; anything else is not a WikiTree page', () => {
  assert.deepStrictEqual(wt.wtParsePage(PROFILE),
    { kind: 'profile', wikitreeId: 'Hartley-6084', key: 'Hartley-6084', url: PROFILE });
  assert.strictEqual(wt.wtParsePage('https://wikitree.com/wiki/Hartley-6084#Sources').wikitreeId, 'Hartley-6084');
  assert.strictEqual(wt.wtParsePage('https://www.wikitree.com/wiki/Profile:Hartley-6084').wikitreeId, 'Hartley-6084');
  assert.strictEqual(wt.wtParsePage('https://www.wikitree.com/wiki/Van_Buren-3').wikitreeId, 'Van_Buren-3');
  assert.deepStrictEqual(wt.wtParsePage('https://www.wikitree.com/wiki/Space:Hartley_Family_Bible'),
    { kind: 'space', spaceName: 'Hartley_Family_Bible', key: 'Space:Hartley_Family_Bible',
      url: 'https://www.wikitree.com/wiki/Space:Hartley_Family_Bible' });
  assert.strictEqual(wt.wtParsePage('https://www.wikitree.com/wiki/Space:Hartley%20Family%20Bible').spaceName,
    'Hartley_Family_Bible');
  for (const u of [
    'https://www.wikitree.com/', 'https://www.wikitree.com/wiki/Category:Fairview',
    'https://www.wikitree.com/wiki/Help:Sources', 'https://www.wikitree.com/genealogy/HARTLEY',
    'https://www.wikitree.com/photo/jpg/Hartley-6084-2', 'https://www.wikitree.com/wiki/Hartley',
    'https://www.wikitree.com.evil.example/wiki/Hartley-6084', 'https://evil.example/wiki/Hartley-6084',
    'https://www.wikitree.com:8443/wiki/Hartley-6084', 'ftp://www.wikitree.com/wiki/Hartley-6084', 'not a url',
  ]) {
    assert.strictEqual(wt.wtParsePage(u), null, u);
  }
});

test('the API is asked once for the page, with the biography as wiki text and no login', () => {
  const u = new URL(wt.wtApiUrl('Hartley-6084'));
  assert.strictEqual(u.origin + u.pathname, 'https://api.wikitree.com/api.php');
  assert.strictEqual(u.searchParams.get('action'), 'getProfile');
  assert.strictEqual(u.searchParams.get('key'), 'Hartley-6084');
  assert.strictEqual(u.searchParams.get('bioFormat'), 'wiki');
  for (const f of ['Touched', 'Parents', 'Spouses', 'Children', 'Bio', 'BirthDate', 'DeathLocation']) {
    assert.ok(u.searchParams.get('fields').split(',').includes(f), f);
  }
  assert.strictEqual(new URL(wt.wtApiUrl('Space:Hartley_Family_Bible')).searchParams.get('key'), 'Space:Hartley_Family_Bible');
  assert.strictEqual(new URL(wt.wtApiUrl('Hartley-6084', 'photos')).searchParams.get('action'), 'getPhotos');
});

test('a profile from the API: its ID, last edit, names, dates, places and family as WikiTree IDs', () => {
  const got = wt.wtRecordFromApi(API.profile, wt.wtParsePage(PROFILE));
  assert.ok(got.ok);
  const r = got.record;
  assert.strictEqual(r.wikitreeId, 'Hartley-6084');
  assert.strictEqual(r.name, 'Calvin Hartley');
  assert.strictEqual(r.lastEdited, '2023-05-26');
  assert.strictEqual(r.created, '2009-09-21');
  assert.deepStrictEqual(r.birth, { date: '1842-03-12', place: 'Fairview' });
  assert.deepStrictEqual(r.death, { date: '1918-01-04', place: 'Fairview' });
  assert.deepStrictEqual(r.parents.map((p) => [p.relation, p.wikitreeId, p.name, p.birthDate]), [
    ['father', 'Hartley-6083', 'Caleb Hartley', '1815'],
    ['mother', 'Webb-3', 'Harriet (Webb) Hartley', '1819-07-14'],
  ]);
  assert.deepStrictEqual(r.spouses.map((p) => [p.wikitreeId, p.name, p.marriageDate, p.marriagePlace]), [
    ['Cole-1234', 'Ethel (Cole) Hartley', '1866-06-10', 'Fairview'],
    ['Reed-12', 'Frances (Reed) Hartley', '1874', null],
  ]);
  assert.deepStrictEqual(r.children.map((p) => [p.wikitreeId, p.deathDate]), [['Hartley-6090', null]]);
  assert.strictEqual(r.from, 'api');
  assert.match(got.bio, /== Sources ==/);
});

test('a free-space page from the API: its title stands in for the person', () => {
  const parsed = wt.wtParsePage('https://www.wikitree.com/wiki/Space:Hartley_Family_Bible');
  const got = wt.wtRecordFromApi(API.space, parsed);
  assert.ok(got.ok);
  assert.strictEqual(got.record.kind, 'space');
  assert.strictEqual(got.record.name, 'Hartley Family Bible');
  assert.strictEqual(got.record.lastEdited, '2024-11-17');
  assert.strictEqual(wt.wtCitation(got.record, '2026-11-24'),
    'WikiTree, "Space:Hartley Family Bible," (https://www.wikitree.com/wiki/Space:Hartley_Family_Bible : last edited ' +
    '2024-11-17; accessed 2026-11-24).');
});

test('a page WikiTree will not share through the API is refused, never read as empty', () => {
  const parsed = wt.wtParsePage('https://www.wikitree.com/wiki/Hartley-9999');
  const got = wt.wtRecordFromApi(API.refused, parsed);
  assert.strictEqual(got.ok, false);
  assert.match(got.error, /did not share this page.*Permission denied/);
  assert.strictEqual(wt.wtRecordFromApi(null, parsed).ok, false);
  assert.strictEqual(wt.wtRecordFromApi([{ status: 0, profile: [] }], parsed).ok, false);
});

test('the citation names the person, the ID, the address, the last edit and the day he looked', () => {
  const r = wt.wtRecordFromApi(API.profile, wt.wtParsePage(PROFILE)).record;
  assert.strictEqual(wt.wtCitation(r, '2026-11-24'),
    'WikiTree, "Calvin Hartley (Hartley-6084)," (https://www.wikitree.com/wiki/Hartley-6084 : last edited 2023-05-26; ' +
    'accessed 2026-11-24).');
  assert.strictEqual(wt.wtCitation(Object.assign({}, r, { lastEdited: null }), '2026-11-24'),
    'WikiTree, "Calvin Hartley (Hartley-6084)," (https://www.wikitree.com/wiki/Hartley-6084 : accessed 2026-11-24).');
});

test('the live Sources section: each numbered work its own entry, as written, and none of them linked', () => {
  const s = wt.wtSources(wt.wtRecordFromApi(API.profile, wt.wtParsePage(PROFILE)).bio);
  assert.strictEqual(s.length, 4);
  assert.strictEqual(s[0].text, '1. [S362] History of the Hartley Family: An Account of the Descendants of Caleb ' +
    'Hartley of Fairview, 1837-1920., Hartley Society, (Worcester: Commonwealth Press, 1923), 10.');
  assert.ok(s.every((e) => !e.linked && e.links.length === 0 && e.from === 'list'));
  // Not parsed: author, title and year stay in the text (#416's parser reads them later).
  assert.deepStrictEqual(Object.keys(s[0]).sort(), ['from', 'hosted', 'interwiki', 'linked', 'links', 'text']);
});

test('footnotes and a Sources list holding the same work twice give it once, in page order', () => {
  const bio = [
    '== Biography ==',
    'Calvin was born in 1842.<ref name="b">Fairview town record of births, vol. 2, p. 14.</ref>',
    'He married Ethel Cole.<ref>Marriage register, Fairview, 1866. https://www.familysearch.org/ark:/61903/1:1:ABCD-123</ref>',
    'Same birth again.<ref name="b"/>',
    '<!-- <ref>A note an editor hid.</ref> -->',
    '',
    '== Sources ==',
    '<references />',
    '* Fairview town record of births, vol. 2, p. 14',
    '* 1850 census, Fairview: [[FamilySearch:LZ7M-S3K]]',
    '* {{FindAGrave|900000001}}',
    '* Family letters held by Edith Louise Hartley',
    '  continued on a second line',
    'See also:',
    '* [https://archive.org/details/historyoffairview00hart History of Fairview], p. 12.',
    '',
    '== Research Notes ==',
    '* Not a source: a note.',
  ].join('\n');
  const s = wt.wtSources(bio);
  assert.deepStrictEqual(s.map((e) => [e.from, e.linked]), [
    ['ref', false], ['ref', true], ['list', true], ['list', true], ['list', false], ['list', true],
  ]);
  assert.strictEqual(s[0].text, 'Fairview town record of births, vol. 2, p. 14.');
  assert.deepStrictEqual(s[1].links, ['https://www.familysearch.org/ark:/61903/1:1:ABCD-123']);
  assert.deepStrictEqual(s[2].interwiki, ['FamilySearch:LZ7M-S3K']);
  assert.deepStrictEqual(s[2].links, ['https://www.familysearch.org/tree/person/details/LZ7M-S3K']);
  assert.deepStrictEqual(s[3].links, ['https://www.findagrave.com/memorial/900000001']);
  assert.strictEqual(s[4].text, 'Family letters held by Edith Louise Hartley\ncontinued on a second line');
  assert.deepStrictEqual(s[5].links, ['https://archive.org/details/historyoffairview00hart']);
  assert.ok(!s.some((e) => /hid|Not a source/.test(e.text)));
});

test('a profile with no Sources section and no footnotes has an empty list, and a link to another profile is not a source', () => {
  assert.deepStrictEqual(wt.wtSources('== Biography ==\nCalvin, son of [[Hartley-6083|Caleb]].'), []);
  assert.deepStrictEqual(wt.wtSources(''), []);
  const s = wt.wtSources('<ref>See [[Hartley-6083|his father]]\'s profile.</ref>');
  assert.strictEqual(s.length, 1);
  assert.strictEqual(s[0].linked, false);
});

test('files and free-space pages WikiTree holds are found in every way an editor links them, once each', () => {
  const bio = [
    '<ref>Marriage record, [https://www.wikitree.com/photo/jpg/Hartley-6084-2 image on WikiTree].</ref>',
    '== Sources ==',
    '* Marriage record again: [[:Image:Hartley-6084-2.jpg|the same image]]',
    '* Deed: https://www.wikitree.com/photo.php/3/3f/Hartley-Deed-1.pdf',
    '* Bible record: [[Space:Hartley Family Bible|transcription]]',
    '* Probate file, https://www.ancestry.com/discoveryui-content/view/123:9071 (not on WikiTree)',
    '* A thumbnail only: https://www.wikitree.com/photo.php/thumb/9/9a/Hartley-Will.png/300px-Hartley-Will.png',
  ].join('\n');
  const s = wt.wtSources(bio);
  const hosted = wt.wtHostedFiles(s);
  assert.deepStrictEqual(hosted.map((h) => [h.n, h.kind, h.fileName || h.spaceName, h.ext || null, h.pageUrl, h.entries]), [
    [1, 'file', 'Hartley-6084-2.jpg', 'jpg', 'https://www.wikitree.com/photo/jpg/Hartley-6084-2', [0, 1]],
    [2, 'file', 'Hartley-Deed-1.pdf', 'pdf', 'https://www.wikitree.com/photo/pdf/Hartley-Deed-1', [2]],
    [3, 'space', 'Hartley_Family_Bible', null, 'https://www.wikitree.com/wiki/Space:Hartley_Family_Bible', [3]],
    [4, 'file', 'Hartley-Will.png', 'png', 'https://www.wikitree.com/photo/png/Hartley-Will', [5]],
  ]);
  // The Ancestry record stays a link to chase, never a file WikiTree holds.
  assert.deepStrictEqual(s[4].hosted, []);
  assert.deepStrictEqual(s[4].links, ['https://www.ancestry.com/discoveryui-content/view/123:9071']);
  assert.ok(s.every((e) => e.linked));
});

test('a held file\'s own page gives the uploader\'s caption and the original, never a thumbnail', () => {
  const f = wt.wtFileFromPage(parse(PHOTO_PAGE), 'Hartley-6084-2.jpg');
  assert.deepStrictEqual(f, { caption: 'Hartley-Cole marriage record, Fairview',
    originalUrl: 'https://www.wikitree.com/photo.php/7/7a/Hartley-6084-2.jpg' });
  assert.deepStrictEqual(wt.wtFileFromPage(parse(PHOTO_PAGE), 'Other-1.jpg'),
    { caption: 'Hartley-Cole marriage record, Fairview', originalUrl: null });
  assert.deepStrictEqual(wt.wtFileFromPage(parse('<html><body><p>Blocked</p></body></html>'), 'x.jpg'),
    { caption: null, originalUrl: null });
});

test('only an original file on www.wikitree.com is ever downloaded', () => {
  assert.ok(wt.wtOriginalUrlOk('https://www.wikitree.com/photo.php/7/7a/Hartley-6084-2.jpg'));
  assert.ok(wt.wtOriginalUrlOk('https://www.wikitree.com/photo.php/3/3f/Hartley-Deed-1.pdf'));
  for (const u of [
    'http://www.wikitree.com/photo.php/7/7a/Hartley-6084-2.jpg',
    'https://www.wikitree.com/photo.php/thumb/7/7a/Hartley-6084-2.jpg/300px-Hartley-6084-2.jpg',
    'https://www.wikitree.com/photo/jpg/Hartley-6084-2',
    'https://www.wikitree.com/photo.php/7/7a/Hartley-6084-2.jpg?x=1',
    'https://www.wikitree.com.evil.example/photo.php/7/7a/a.jpg',
    'https://a:b@www.wikitree.com/photo.php/7/7a/a.jpg',
    'https://api.wikitree.com/photo.php/7/7a/a.jpg', '', 'nonsense',
  ]) {
    assert.strictEqual(wt.wtOriginalUrlOk(u), false, u);
  }
});

test('a profile\'s photos come with their captions and the original behind each thumbnail', () => {
  assert.deepStrictEqual(wt.wtPhotosFromApi(API.photos), [
    { imageName: 'Hartley-6084.jpg', caption: 'Calvin Hartley about 1900', date: null, place: 'Fairview',
      pageUrl: 'https://www.wikitree.com/photo/jpg/Hartley-6084',
      originalUrl: 'https://www.wikitree.com/photo.php/5/5c/Hartley-6084.jpg' },
    { imageName: 'Hartley-6084-1.jpg', caption: 'Hartley homestead, South street', date: null, place: 'Fairview',
      pageUrl: 'https://www.wikitree.com/photo/jpg/Hartley-6084-1',
      originalUrl: 'https://www.wikitree.com/photo.php/4/47/Hartley-6084-1.jpg' },
  ]);
  assert.deepStrictEqual(wt.wtPhotosFromApi(API.noPhotos), []);
  assert.deepStrictEqual(wt.wtPhotosFromApi(API.refused), []);
});

test('the page itself, when the API refuses: name, dates, last edit, family and the same sources', () => {
  const got = wt.wtRecordFromPage(parse(PAGE), wt.wtParsePage(PROFILE));
  assert.ok(got.ok);
  const r = got.record;
  assert.strictEqual(r.name, 'Calvin Hartley');
  assert.strictEqual(r.from, 'page');
  assert.strictEqual(r.lastEdited, '2023-05-26');
  assert.deepStrictEqual(r.birth, { date: '1842-03-12', place: 'Fairview' });
  assert.deepStrictEqual(r.parents.map((p) => [p.relation, p.wikitreeId, p.name]), [
    ['father', 'Hartley-6083', 'Caleb Comstock Hartley'], ['mother', 'Webb-3', 'Harriet (Webb) Hartley']]);
  assert.deepStrictEqual(r.spouses.map((p) => p.wikitreeId), ['Cole-1234', 'Reed-12']);
  assert.deepStrictEqual(r.children.map((p) => p.wikitreeId), ['Hartley-6090']);
  assert.match(got.bioText, /^\* There is no evidence/);
  assert.match(got.bioText, /History of Fairview \(2\.476\)\. \[1\]$/);
  assert.ok(!/edit/i.test(got.bioText));
  assert.deepStrictEqual(got.sources.map((s) => s.text.slice(0, 9)), ['1. [S362]', '2. [S366]', '3. [S366]', '4. [S366]']);
  assert.ok(got.sources.every((s) => !s.linked));
});

test('the page path reads footnote lists and the links each source shows', () => {
  const html = '<html><body><h1>Calvin Hartley</h1><div class="body-text">' +
    '<h2 id="Biography">Biography</h2><p>Born 1842.<sup><a href="#_note-0">[1]</a></sup></p>' +
    '<h2 id="Sources">Sources</h2>' +
    '<ol class="references"><li><span class="reference-text">Census, <a href="https://www.familysearch.org/ark:/61903/1:1:ABCD-123">FamilySearch</a></span></li></ol>' +
    '<ul><li>Image: <a href="/photo/jpg/Hartley-6084-2">marriage record</a></li><li>Family letters</li>' +
    '<li>See <a href="/wiki/Hartley-6083">his father</a></li></ul></div></body></html>';
  const got = wt.wtRecordFromPage(parse(html), wt.wtParsePage(PROFILE));
  assert.deepStrictEqual(got.sources.map((s) => [s.from, s.text, s.linked]), [
    ['ref', 'Census, FamilySearch', true],
    ['list', 'Image: marriage record', true],
    ['list', 'Family letters', false],
    ['list', 'See his father', false],
  ]);
  assert.deepStrictEqual(got.sources[0].links, ['https://www.familysearch.org/ark:/61903/1:1:ABCD-123']);
  assert.deepStrictEqual(wt.wtHostedFiles(got.sources).map((h) => h.fileName), ['Hartley-6084-2.jpg']);
});

test('the biography as plain text: headings, words of links, no footnotes, templates or categories', () => {
  const text = wt.wtPlainText(API.space[0].profile.bio);
  assert.strictEqual(text, [
    'A transcription of the family record pages in the Hartley family Bible, held by Edith Louise Hartley in 1950.',
    '', '## Births', '', '- Calvin Hartley born 12 March 1842', '- Edith Louise Hartley born 1868',
    '', '## Marriages', '', '- Calvin Hartley and Ethel Cole, 10 June 1866',
  ].join('\n'));
  assert.strictEqual(wt.wtPlainText('Son of [[Hartley-6083|Caleb]]. {{Died Young}}See [https://example.org/x the record].'),
    'Son of Caleb. See the record (https://example.org/x).');
});

test('the cited sources list: linked ones first with where they point and what was saved, then "To find:"', () => {
  const s = wt.wtSources([
    '== Sources ==',
    '* Family letters held by Edith Louise Hartley',
    '* Marriage record: [https://www.wikitree.com/photo/jpg/Hartley-6084-2 image]',
    '* {{FindAGrave|900000001}}',
  ].join('\n'));
  const hosted = wt.wtHostedFiles(s);
  const notes = wt.wtCitedSourcesNotes(s, hosted, { 1: { file: 'source-1.jpg', caption: 'Hartley-Cole marriage record',
    pageUrl: 'https://www.wikitree.com/photo/jpg/Hartley-6084-2' } });
  assert.strictEqual(notes, [
    '### Cited sources',
    '',
    'Linked (open each and check it):',
    '- [ ] Marriage record: image - https://www.wikitree.com/photo/jpg/Hartley-6084-2; saved in this source as ' +
      'source-1.jpg ' +
      '("Hartley-Cole marriage record"), from https://www.wikitree.com/photo/jpg/Hartley-6084-2',
    '- [ ] {{FindAGrave|900000001}} - https://www.findagrave.com/memorial/900000001',
    '',
    'To find:',
    '- [ ] Family letters held by Edith Louise Hartley',
  ].join('\n'));
  assert.strictEqual(wt.wtCitedSourcesNotes([], [], {}), '');
});
