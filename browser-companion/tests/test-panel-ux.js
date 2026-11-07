// The capture panel's everyday handling, driven end to end without a browser
// (tests/panel-harness.js): the two Re-read buttons, the panel staying on its
// own tab, the automatic-saving box and its fetch log, and the FamilySearch
// details that ride with them. Every page here is invented (example.org, the
// fictional Hartleys of Fairview, made-up record and image ids).
//
// What it guards, in her terms:
//   - looking at another tab never changes the form under her; "Re-read page"
//     is the one way the panel moves to the page in front of her, and then it
//     starts clean, with nothing of the last record left to be filed;
//   - "Re-read file" refreshes only the file, keeping what she typed, and
//     only on the page the details describe;
//   - she can see what was fetched, refused or retried, and a picture that is
//     the same as the last capture's is pointed out;
//   - on a page whose file comes automatically there is no address box to
//     puzzle over; on FamilySearch a refused address is retried through
//     FamilySearch itself; and a FamilySearch image that cannot be saved is
//     said in the Notes box before Capture, once.
//
//   node --test browser-companion/tests/test-panel-ux.js

'use strict';

const { test } = require('node:test');
const assert = require('node:assert');

const { openPanel, settle, tick, holdFirst } = require('./panel-harness.js');

const REC_A = 'https://records.example.org/hartley/calvin-1880';
const REC_B = 'https://records.example.org/hartley/harriet-1880';
const VIEWER = 'https://www.ancestry.com/imageviewer/collections/90001/images/4000123_00045';
const VIEWER_2 = 'https://www.ancestry.com/imageviewer/collections/90001/images/4000123_00046';
const FS_PAGE = 'https://www.familysearch.org/records/images/image-details?rmsId=TH-0000-HRT1&imageIndex=3';
const FS_TILE = 'https://sg30p0.familysearch.org/service/records/storage/deepzoomcloud/dz/v1/3:1:HRT1-IMG1/image_files/12/0_0.jpg';

const b64 = (s) => Buffer.from(String(s)).toString('base64');
const picture = (name) => ({ ok: true, base64: b64('picture of ' + name), ext: 'jpg', contentType: 'image/jpeg' });

// A scripted content script. `pages[url]` describes each page: its prefill
// fields (`prefill`), a different title, and `on[action]` answers that replace
// the defaults below. A page that is not listed cannot be read.
function site(pages) {
  return (msg, tab) => {
    const page = pages[String(tab.url).split('#')[0]];
    if (!page) return { ok: false, error: 'this page cannot be read' };
    if (page.on && page.on[msg.action]) return page.on[msg.action](msg, tab);
    switch (msg.action) {
      case 'prefill':
        return {
          ok: true,
          // An ordinary page shows its record as an image (so Capture has a file).
          prefill: Object.assign({ url: tab.url, title: page.title || 'A record', people: page.people || [],
            imageUrl: tab.url + '/image.jpg' }, page.prefill || {}),
        };
      case 'pagehtml': return { ok: true, html: '<html>' + tab.url + '</html>' };
      case 'singlefile': return { ok: true, html: '<html>copy of ' + tab.url + '</html>' };
      case 'fetchAsset': return picture(msg.url);
      case 'ancestryImage': return picture(tab.url);
      default: return { ok: false, error: 'unexpected ' + msg.action };
    }
  };
}

const open = (url, pages, opts = {}) => openPanel(Object.assign({ url, respond: opts.wrap ? opts.wrap(site(pages)) : site(pages) }, opts));
const type = async (p, id, value) => {
  p.$(id).value = value;
  await p.$(id).fire('input');
};
function dropFile(p, name) {
  const file = new Blob(['bytes of ' + name], { type: 'image/jpeg' });
  file.name = name;
  return p.$('dropzone').listeners.drop[0]({ preventDefault() {}, dataTransfer: { files: [file] } });
}
const people = (p) => p.$('people-list').children.map((l) => l.children[1].textContent);
const lastSentTo = (p, action) => p.sentTo[p.sent.map((m) => m.action).lastIndexOf(action)];

// ── the panel stays on its own tab ──────────────────────────────────────────

test('looking at another tab never changes the form; Capture still saves the record it describes', async () => {
  const pages = { [REC_A]: { title: 'Calvin Hartley, 1880' }, [REC_B]: { title: 'Harriet Hartley, 1880' } };
  const p = await open(REC_A, pages);
  await type(p, 'f-title', 'Calvin Hartley in the 1880 census');
  const other = p.openTab(REC_B);
  await p.navigateTab(other, REC_B);
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Calvin Hartley in the 1880 census');
  assert.strictEqual(p.$('f-url').value, REC_A);
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.written[0].captureJson.title, 'Calvin Hartley in the 1880 census');
  assert.strictEqual(p.written[0].captureJson.url, REC_A);
  assert.strictEqual(lastSentTo(p, 'pagehtml'), p.tab.id, 'the page was read from the panel\'s own tab');
});

test('Re-read page moves the panel to the tab in front, and starts clean there', async () => {
  const pages = {
    [REC_A]: { title: 'Calvin Hartley, 1880', people: ['Calvin Hartley'] },
    [REC_B]: { title: 'Harriet Hartley, 1880', people: ['Harriet Hartley'] },
  };
  const p = await open(REC_A, pages);
  await type(p, 'f-title', 'my title for Calvin');
  await type(p, 'f-date', '1880');
  p.$('f-notes').value = 'a note about Calvin';
  await dropFile(p, 'calvin.jpg');
  const other = p.openTab(REC_B);
  await p.reread();
  assert.strictEqual(p.$('f-title').value, 'Harriet Hartley, 1880', 'what she typed for Calvin does not follow');
  assert.strictEqual(p.$('f-date').value, '');
  assert.strictEqual(p.$('f-notes').value, '');
  assert.strictEqual(p.$('f-url').value, REC_B);
  assert.deepStrictEqual(people(p), ['Harriet Hartley']);
  assert.doesNotMatch(p.$('dropzone').textContent, /calvin/, 'the file dropped for Calvin is let go');
  assert.ok(!p.$('btn-rescrape').disabled);
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.written[0].captureJson.url, REC_B);
  assert.strictEqual(p.written[0].captureJson.title, 'Harriet Hartley, 1880');
  assert.strictEqual(lastSentTo(p, 'pagehtml'), other.id, 'the capture read the tab she moved it to');
  // ...and from now on it follows THAT tab, not the old one.
  pages['https://records.example.org/hartley/ethel-1940'] = { title: 'Ethel Hartley, 1940' };
  await p.navigate(REC_A + '?moved=1');
  await settle();
  assert.strictEqual(p.$('f-url').value, REC_B, 'the old tab\'s navigation is not the form\'s any more');
  await p.navigateTab(other, 'https://records.example.org/hartley/ethel-1940');
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Ethel Hartley, 1940');
});

test('Re-read page on a page that cannot be read leaves nothing of the last record to file with it', async () => {
  const p = await open(REC_A, { [REC_A]: { title: 'Calvin Hartley, 1880', people: ['Calvin Hartley'] } });
  await type(p, 'f-title', 'my title for Calvin');
  p.openTab(REC_B); // not readable
  await p.reread();
  assert.strictEqual(p.$('f-title').value, '');
  assert.deepStrictEqual(people(p), []);
  assert.strictEqual(p.$('f-url').value, REC_B);
  assert.match(p.$('recipe-banner').textContent, /Couldn’t read this page automatically/);
});

test('Re-read page with a browser page in front keeps the panel on its record', async () => {
  const p = await open(REC_A, { [REC_A]: { title: 'Calvin Hartley, 1880' } });
  await type(p, 'f-title', 'my title for Calvin');
  p.openTab('chrome://extensions/');
  await p.reread();
  assert.strictEqual(p.$('f-title').value, 'my title for Calvin');
  assert.strictEqual(p.$('f-url').value, REC_A);
  assert.match(p.$('recipe-banner').textContent, /not a web page the panel can read, so the panel stays/);
  p.activate(p.tab);
  await p.capture();
  assert.strictEqual(p.written[0].captureJson.title, 'my title for Calvin');
});

test('Re-read page on the same record starts it again: what she typed goes, the record stays', async () => {
  const p = await open(REC_A, { [REC_A]: { title: 'Calvin Hartley, 1880' } });
  await type(p, 'f-title', 'a title she typed');
  await p.reread();
  assert.strictEqual(p.$('f-title').value, 'Calvin Hartley, 1880');
  await p.capture();
  assert.strictEqual(p.written[0].captureJson.title, 'Calvin Hartley, 1880');
});

test('Capture waits while the page is re-read; a Re-read waits while a capture saves', async () => {
  let hold = null;
  const pages = { [REC_A]: { title: 'Calvin Hartley, 1880' }, [REC_B]: { title: 'Harriet Hartley, 1880' } };
  const p = await open(REC_A, pages, { wrap: (r) => { hold = holdFirst(r, 'singlefile'); return hold.respond; } });
  const saving = p.capture();
  for (let i = 0; i < 100 && !hold.held; i++) await tick();
  assert.ok(hold.held);
  p.openTab(REC_B);
  await p.$('btn-rescrape').fire('click');
  assert.strictEqual(p.$('f-url').value, REC_A, 'no Re-read while the capture saves');
  hold.release();
  await saving;
  assert.strictEqual(p.written[0].captureJson.url, REC_A);

  const reading = p.reread();
  await tick();
  await p.$('btn-capture').fire('click');
  assert.match(p.result(), /reading the page again - press Capture once it has finished/);
  await reading;
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.$('f-url').value, REC_B);
});

test('a navigation answer for the old tab that lands after a Re-read is dropped, and Capture is not stuck', async () => {
  const REC_A2 = REC_A + '/page-2';
  const pages = {
    [REC_A]: { title: 'Calvin Hartley, 1880' },
    [REC_A2]: { title: 'Calvin Hartley, page 2' },
    [REC_B]: { title: 'Harriet Hartley, 1880' },
  };
  let held = null;
  let release = null;
  const p = await open(REC_A, pages, {
    wrap: (r) => (msg, tab) => {
      if (msg.action === 'prefill' && tab.url === REC_A2 && !held) {
        held = true;
        return new Promise((res) => { release = () => res(r(msg, tab)); });
      }
      return r(msg, tab);
    },
  });
  const nav = p.navigate(REC_A2); // the panel's tab moves; its answer is held back
  for (let i = 0; i < 100 && !release; i++) await tick();
  p.openTab(REC_B);
  await p.reread();
  assert.strictEqual(p.$('f-url').value, REC_B);
  release();
  await nav;
  await settle();
  assert.strictEqual(p.$('f-url').value, REC_B, 'the old tab\'s late answer does not overwrite the form');
  assert.strictEqual(p.$('f-title').value, 'Harriet Hartley, 1880');
  await p.capture();
  assert.strictEqual(p.written.length, 1, 'Capture is not left waiting for a read that was dropped');
  assert.strictEqual(p.written[0].captureJson.url, REC_B);
});

test('the page finishing loading while it is re-read does not leave the form empty', async () => {
  const pages = { [REC_A]: { title: 'Calvin Hartley, 1880' }, [REC_B]: { title: 'Harriet Hartley, 1880' } };
  const p = await open(REC_A, pages);
  const other = p.openTab(REC_B);
  const reading = p.reread();
  await tick();
  await p.navigateTab(other, REC_B); // the same address, done loading
  await reading;
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Harriet Hartley, 1880');
  await p.capture();
  assert.strictEqual(p.written[0].captureJson.url, REC_B);
});

// ── Re-read file ─────────────────────────────────────────────────────────────

test('Re-read file refreshes only the file: what she typed, the people and the notes stay', async () => {
  const page = { title: 'Calvin Hartley, 1880', people: ['Calvin Hartley'], prefill: { imageUrl: 'https://records.example.org/img/first.jpg' } };
  const p = await open(REC_A, { [REC_A]: page });
  assert.strictEqual(p.$('f-asset-url').value, 'https://records.example.org/img/first.jpg');
  await type(p, 'f-title', 'Calvin in the census');
  await type(p, 'f-date', 'June 1880');
  p.$('f-notes').value = 'the second page of the household';
  await dropFile(p, 'old-image.jpg');
  // The viewer turned to the next image without changing its address.
  page.prefill = { imageUrl: 'https://records.example.org/img/second.jpg' };
  page.title = 'Some other title the page shows now';
  await p.rereadFile();
  assert.strictEqual(p.$('f-asset-url').value, 'https://records.example.org/img/second.jpg');
  assert.strictEqual(p.$('f-title').value, 'Calvin in the census');
  assert.strictEqual(p.$('f-date').value, 'June 1880');
  assert.strictEqual(p.$('f-notes').value, 'the second page of the household');
  assert.deepStrictEqual(people(p), ['Calvin Hartley']);
  assert.doesNotMatch(p.$('dropzone').textContent, /old-image/, 'a file dropped for the previous image is let go');
  await p.capture();
  const cap = p.written[0].captureJson;
  assert.strictEqual(cap.title, 'Calvin in the census');
  assert.strictEqual(cap.source_date || cap.sourceDate || cap.date, 'June 1880');
  const fetched = p.sent.filter((m) => m.action === 'fetchAsset').map((m) => m.url);
  assert.deepStrictEqual(fetched, ['https://records.example.org/img/second.jpg']);
});

test('Re-read file on another tab points her to Re-read page and changes nothing', async () => {
  const pages = {
    [REC_A]: { title: 'Calvin Hartley, 1880', prefill: { imageUrl: 'https://records.example.org/img/calvin.jpg' } },
    [REC_B]: { title: 'Harriet Hartley, 1880', prefill: { imageUrl: 'https://records.example.org/img/harriet.jpg' } },
  };
  const p = await open(REC_A, pages);
  await type(p, 'f-title', 'Calvin in the census');
  p.openTab(REC_B);
  await p.rereadFile();
  assert.match(p.result(), /not the one these details describe\. Press ↻ Re-read page/);
  assert.strictEqual(p.$('f-asset-url').value, 'https://records.example.org/img/calvin.jpg');
  assert.strictEqual(p.$('f-title').value, 'Calvin in the census');
});

// ── the automatic-saving box and its log ────────────────────────────────────

test('on an Ancestry viewer the box says the image is saved automatically, and the log shows what came', async () => {
  const p = await open(VIEWER, { [VIEWER]: { prefill: { ancestryImageViewer: true, recipeHint: 'ancestry' } } });
  assert.deepStrictEqual(p.autoBox(), { cls: 'auto-box ready',
    text: 'Saved automatically when you press Capture: the full record image (Ancestry).' });
  assert.strictEqual(p.$('asset-url-row').style.display, 'none', 'no address box to puzzle over');
  await p.capture();
  assert.strictEqual(p.autoBox().cls, 'auto-box done');
  assert.strictEqual(p.autoBox().text, 'Saved automatically.');
  assert.deepStrictEqual(p.log(), ['✓ Ancestry full record image (1 KB)']);
});

test('a refused image is a cross with Ancestry\'s reason, and the box says what to do', async () => {
  const p = await open(VIEWER, {
    [VIEWER]: {
      prefill: { ancestryImageViewer: true },
      on: { ancestryImage: () => ({ ok: false, error: 'Ancestry asked you to sign in' }) },
    },
    [VIEWER_2]: { prefill: { ancestryImageViewer: true } },
  });
  assert.strictEqual(p.$('asset-url-row').style.display, 'none');
  await p.capture();
  assert.deepStrictEqual(p.log(), ['✗ Ancestry full record image - Ancestry asked you to sign in']);
  // The viewer's own image did not come: she may now give its address too (F9).
  assert.strictEqual(p.$('asset-url-row').style.display, '');
  assert.deepStrictEqual(p.autoBox(), { cls: 'auto-box failed',
    text: 'Automatic saving did not work - the lines below say why. Drop the file in, or paste its address.' });
  assert.strictEqual(p.written.length, 1, 'the page copy is still saved');
  // A new record starts with a clean log, and its own automatic file.
  await p.navigate(VIEWER_2);
  await settle();
  assert.deepStrictEqual(p.log(), []);
  assert.strictEqual(p.autoBox().cls, 'auto-box ready');
  assert.strictEqual(p.$('asset-url-row').style.display, 'none');
});

test('an address she gives after the automatic image failed is the one saved next', async () => {
  const p = await open(VIEWER, { [VIEWER]: {
    prefill: { ancestryImageViewer: true },
    on: { ancestryImage: () => ({ ok: false, error: 'Ancestry asked you to sign in' }) },
  } });
  await p.capture();
  await type(p, 'f-asset-url', 'https://records.example.org/img/my-download.jpg');
  await p.capture();
  assert.strictEqual(p.written.length, 2);
  assert.ok(p.sent.some((m) => m.action === 'fetchAsset' && m.url === 'https://records.example.org/img/my-download.jpg'));
  assert.deepStrictEqual(p.written[1].captureJson.assets.map((a) => a.mode), ['singlefile', 'fetch']);
});

test('fetched is not saved: a capture that then fails says nothing was saved (F6/F11)', async () => {
  const p = await open(VIEWER, { [VIEWER]: { prefill: { ancestryImageViewer: true } } });
  p.writeFails.add(0); // the download of the bundle is blocked
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /^Could not capture: the download was blocked/);
  assert.deepStrictEqual(p.log(), ['✓ Ancestry full record image (1 KB)']);
  assert.deepStrictEqual(p.autoBox(), { cls: 'auto-box failed',
    text: 'The file was fetched, but nothing was saved - the message under Capture says why. Press Capture again.' });
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.deepStrictEqual(p.autoBox(), { cls: 'auto-box done', text: 'Saved automatically.' });
});

test('the same picture as the last capture, on a new record, is pointed out', async () => {
  const same = picture('the page the tab has not moved off');
  const pages = {
    [VIEWER]: { prefill: { ancestryImageViewer: true }, on: { ancestryImage: () => same } },
    [VIEWER_2]: { prefill: { ancestryImageViewer: true }, on: { ancestryImage: () => same } },
  };
  const p = await open(VIEWER, pages);
  await p.capture();
  assert.doesNotMatch(p.log()[0], /same picture/);
  await p.navigate(VIEWER_2);
  await settle();
  await p.capture();
  assert.match(p.log()[0], /^✓ Ancestry full record image \(1 KB - the same picture as your last capture: check the tab shows the record you mean\)$/);
});

test('a different picture is not called the same; a capture that saved nothing is not "the last capture"', async () => {
  const pages = {
    [VIEWER]: { prefill: { ancestryImageViewer: true }, on: { ancestryImage: () => picture('one') } },
    [VIEWER_2]: { prefill: { ancestryImageViewer: true }, on: { ancestryImage: () => picture('two') } },
  };
  const p = await open(VIEWER, pages);
  p.writeFails.add(0); // the first capture's download is blocked: nothing saved
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  await p.capture();
  assert.doesNotMatch(p.log()[0], /same picture/, 'the blocked capture saved nothing to compare with');
  await p.navigate(VIEWER_2);
  await settle();
  await p.capture();
  assert.doesNotMatch(p.log()[0], /same picture/);
});

test('page text in the log is shown as text, never as markup', async () => {
  const p = await open(VIEWER, { [VIEWER]: {
    prefill: { ancestryImageViewer: true },
    on: { ancestryImage: () => ({ ok: false, error: '<img src=x onerror=alert(1)>' }) },
  } });
  await p.capture();
  const li = p.$('auto-log').children[0];
  assert.strictEqual(li.children[2].textContent, '- <img src=x onerror=alert(1)>');
});

// ── the address box ──────────────────────────────────────────────────────────

test('the address box is hidden where the file comes automatically, and back on an ordinary page', async () => {
  const pages = {
    [VIEWER]: { prefill: { ancestryImageViewer: true } },
    'https://www.ancestry.com/search/collections/90001/records/1000001': { prefill: { ancestryRecord: true } },
    'https://www.familysearch.org/ark:/61903/1:1:HRT1-CEN1': { prefill: { familysearchRecord: true } },
    'https://www.familysearch.org/ark:/61903/3:1:HRT1-IMG1': { prefill: { familysearchImageViewer: true, familysearchImageId: 'HRT1-IMG1' } },
    'https://www.findagrave.com/memorial/1000001/calvin-hartley': { prefill: { findagraveMemorial: true } },
    'https://iiif.example.org/item/1': { prefill: { iiif: true } },
    // A Newspapers.com page with none of her clippings: nothing comes by
    // itself there, so the box is back (F9; the page WITH her clippings is
    // in test-panel-newspapers.js).
    'https://www.newspapers.com/article/the-fairview-gazette/100001/': { prefill: { newspapers: true } },
    [REC_A]: { prefill: { imageUrl: 'https://records.example.org/img/a.jpg' } },
  };
  const shown = new Set([REC_A, 'https://www.newspapers.com/article/the-fairview-gazette/100001/']);
  const p = await open(VIEWER, pages);
  for (const url of Object.keys(pages)) {
    await p.navigate(url);
    await settle();
    assert.strictEqual(p.$('asset-url-row').style.display, shown.has(url) ? '' : 'none', url);
  }
  assert.strictEqual(p.$('f-asset-url').value, 'https://records.example.org/img/a.jpg');
  assert.strictEqual(p.autoBox().text, 'Using the file at the address below.');
});

// ── FamilySearch: a refused address retried through FamilySearch ────────────

function fsPage(over = {}) {
  return Object.assign({
    prefill: { imageUrl: FS_TILE, recipeHint: 'familysearch' },
    on: {
      fetchAsset: () => ({ ok: false, error: 'this image would not come through (often a protected viewer), try dropping the file in instead' }),
      familysearchImage: (msg) => (msg.fromUrl === FS_TILE ? picture('HRT1-IMG1 in full') : { ok: false, error: 'no image named' }),
    },
  }, over);
}

test('on FamilySearch a refused file address is retried through FamilySearch, and the log shows both tries', async () => {
  const p = await open(FS_PAGE, { [FS_PAGE]: fsPage() });
  assert.strictEqual(p.$('asset-url-row').style.display, '', 'not a page the panel fetches by itself');
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  const cap = p.written[0].captureJson;
  assert.deepStrictEqual(cap.assets.map((a) => [a.file, a.role, a.mode]),
    [['page-snapshot.html', 'webpage', 'singlefile'], ['record.jpg', 'record', 'familysearch-api']]);
  assert.deepStrictEqual(p.log(), [
    '✗ File from the page address - this image would not come through (often a protected viewer), try dropping the file in instead',
    '✓ The same image through FamilySearch (a second try) (1 KB)',
  ]);
  assert.strictEqual(p.autoBox().text, 'Saved automatically, but not every try worked - see below.');
  const retry = p.sent.find((m) => m.action === 'familysearchImage');
  assert.deepStrictEqual(retry, { action: 'familysearchImage', fromUrl: FS_TILE });
});

test('the FamilySearch retry is never tried on another site', async () => {
  const p = await open(REC_A, { [REC_A]: fsPage({ prefill: { imageUrl: FS_TILE } }) });
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /^Could not capture: this image would not come through/);
  assert.ok(!p.sent.some((m) => m.action === 'familysearchImage'));
});

test('a FamilySearch retry that also fails gives the address\'s own reason', async () => {
  const page = fsPage();
  page.on.familysearchImage = () => ({ ok: false, error: 'no single image was found on this page' });
  const p = await open(FS_PAGE, { [FS_PAGE]: page });
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /^Could not capture: this image would not come through/);
  assert.match(p.log()[1], /^✗ The same image through FamilySearch \(a second try\) - no single image/);
});

test('the tab moving while the retry reads the page: nothing is saved', async () => {
  const page = fsPage();
  let p = null;
  page.on.familysearchImage = async () => {
    p.tab.url = REC_B; // she clicked on while it was fetched
    return picture('HRT1-IMG1 in full');
  };
  p = await open(FS_PAGE, { [FS_PAGE]: page, [REC_B]: { title: 'Harriet' } });
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.match(p.result(), /the page in the tab changed while this record was being saved/);
});

// ── PR #223 audit: who may fill the form, and a screen she can always use ────

const UNREADABLE = 'https://records.example.org/hartley/not-readable';
const until = async (cond) => {
  for (let i = 0; i < 200 && !cond(); i++) await tick();
  assert.ok(cond(), 'the awaited moment never came');
};

test('F4/F10: the panel\'s first read, answering late, never overwrites a Re-read of another tab', async () => {
  const pages = { [REC_A]: { title: 'Calvin Hartley, 1880' }, [REC_B]: { title: 'Harriet Hartley, 1880' } };
  let hold = null;
  const p = await open(REC_A, pages, {
    waitReady: false,
    wrap: (r) => { hold = holdFirst(r, 'prefill'); return hold.respond; },
  });
  await until(() => hold.held); // tab A is still loading
  const other = p.openTab(REC_B);
  await p.reread();
  assert.strictEqual(p.$('f-title').value, 'Harriet Hartley, 1880');
  hold.release(); // A answers at last
  await p.ready;
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Harriet Hartley, 1880');
  assert.strictEqual(p.$('f-url').value, REC_B);
  await p.capture();
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.written[0].captureJson.url, REC_B);
  assert.strictEqual(p.written[0].captureJson.title, 'Harriet Hartley, 1880');
  assert.ok(!p.sent.some((m) => m.action === 'fetchAsset' && m.url === REC_A + '/image.jpg'), 'never A\'s file');
  assert.strictEqual(lastSentTo(p, 'pagehtml'), other.id);
});

test('F4/F10: a form filled for one page is never saved with another page in the tab', async () => {
  const pages = { [REC_A]: { title: 'Calvin Hartley, 1880' }, [REC_B]: { title: 'Harriet Hartley, 1880' } };
  const p = await open(REC_A, pages);
  await type(p, 'f-title', 'Calvin in the census');
  p.tab.url = REC_B; // the tab moved on, and the panel was not told
  await p.capture();
  assert.strictEqual(p.written.length, 0, 'nothing saved under the wrong page');
  assert.match(p.result(), /^Could not capture: the page in the tab is not the one these details were filled in for, so nothing was saved/);
  await settle();
  // ...and the panel reads the page that is there.
  assert.strictEqual(p.$('f-url').value, REC_B);
  assert.strictEqual(p.$('f-title').value, 'Harriet Hartley, 1880');
});

test('F1: a Re-read that loses to the tab moving to an unreadable page leaves a usable form', async () => {
  const pages = { [REC_A]: { title: 'Calvin Hartley, 1880' }, [REC_B]: { title: 'Harriet Hartley, 1880' } };
  const p = await open(REC_A, pages);
  const other = p.openTab(REC_B);
  const reading = p.reread();
  await tick();
  await p.navigateTab(other, UNREADABLE);
  await reading;
  await settle();
  assert.match(p.$('recipe-banner').textContent, /^Couldn’t read this page automatically/);
  assert.strictEqual(p.$('f-url').value, UNREADABLE);
  assert.strictEqual(p.$('f-title').value, '');
  assert.strictEqual(p.$('btn-capture').disabled, false);
});

test('F1: opened on a browser page, a Re-read that a navigation overtakes still turns Capture on', async () => {
  const REC_A2 = REC_A + '/page-2';
  const pages = { [REC_A]: { title: 'Calvin Hartley, 1880' }, [REC_A2]: { title: 'Calvin, page 2' } };
  const p = await open('chrome://newtab/', pages);
  assert.strictEqual(p.$('btn-capture').disabled, true);
  const rec = p.openTab(REC_A);
  const reading = p.reread();
  await tick();
  await p.navigateTab(rec, REC_A2);
  await reading;
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Calvin, page 2');
  assert.strictEqual(p.$('btn-capture').disabled, false);
  assert.doesNotMatch(p.$('recipe-banner').textContent, /Reading the page/);
  await p.capture();
  assert.strictEqual(p.written[0].captureJson.url, REC_A2);
});

test('Codex 4502593873: an old tab\'s navigation still waiting for its tab does not cancel the Re-read', async () => {
  const REC_A2 = REC_A + '/page-2';
  const pages = {
    [REC_A]: { title: 'Calvin Hartley, 1880' }, [REC_A2]: { title: 'Calvin, page 2' },
    [REC_B]: { title: 'Harriet Hartley, 1880' },
  };
  const p = await open(REC_A, pages);
  const releaseGet = p.holdNextGet(p.tab.id);
  const nav = p.navigate(REC_A2); // waits on its tabs.get
  p.openTab(REC_B);
  const reading = p.reread();
  await tick();
  releaseGet(); // the old tab's navigation resumes while the Re-read reads
  await nav;
  await reading;
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Harriet Hartley, 1880');
  assert.strictEqual(p.$('f-url').value, REC_B);
  assert.doesNotMatch(p.$('recipe-banner').textContent, /Reading the page/);
});

test('F12: after a Re-read, Capture does not wait for the old tab\'s navigation to be read', async () => {
  const REC_A2 = REC_A + '/page-2';
  const pages = {
    [REC_A]: { title: 'Calvin Hartley, 1880' }, [REC_A2]: { title: 'Calvin, page 2' },
    [REC_B]: { title: 'Harriet Hartley, 1880' },
  };
  let release = null;
  const p = await open(REC_A, pages, {
    wrap: (r) => (msg, tab) => {
      if (msg.action === 'prefill' && tab.url === REC_A2 && !release) {
        return new Promise((res) => { release = () => res(r(msg, tab)); });
      }
      return r(msg, tab);
    },
  });
  const nav = p.navigate(REC_A2);
  await until(() => release);
  p.openTab(REC_B);
  await p.reread();
  await p.capture(); // the old tab's read is still out
  assert.strictEqual(p.written.length, 1);
  assert.strictEqual(p.written[0].captureJson.url, REC_B);
  release();
  await nav;
  await settle();
  assert.strictEqual(p.$('f-url').value, REC_B);
});

test('F13: Re-read buttons are off while a capture saves, and a press says to wait', async () => {
  let hold = null;
  const pages = { [REC_A]: { title: 'Calvin Hartley, 1880' } };
  const p = await open(REC_A, pages, { wrap: (r) => { hold = holdFirst(r, 'singlefile'); return hold.respond; } });
  const saving = p.capture();
  await until(() => hold.held);
  assert.strictEqual(p.$('btn-rescrape').disabled, true);
  assert.strictEqual(p.$('btn-rescrape-file').disabled, true);
  await p.$('btn-rescrape').fire('click');
  assert.strictEqual(p.result(), 'Saving the capture - press Re-read once it has finished.');
  hold.release();
  await saving;
  await settle();
  assert.strictEqual(p.$('btn-rescrape').disabled, false);
  assert.strictEqual(p.$('btn-rescrape-file').disabled, false);
  assert.match(p.result(), /^Staged to /);
});

test('F7: on FamilySearch, an address elsewhere that refuses is not tried again through FamilySearch', async () => {
  const p = await open(FS_PAGE, { [FS_PAGE]: fsPage({ prefill: { imageUrl: 'https://county.example.gov/records/hartley.jpg' } }) });
  await p.capture();
  assert.strictEqual(p.written.length, 0);
  assert.ok(!p.sent.some((m) => m.action === 'familysearchImage'), 'no second try for an address off FamilySearch');
  assert.deepStrictEqual(p.log().length, 1);
  assert.match(p.result(), /^Could not capture: this image would not come through/);
});

test('Codex 4502752672: a new page that could not be read yet is read again when it finishes loading', async () => {
  const pages = { [REC_A]: { title: 'Calvin Hartley, 1880' }, [REC_B]: { title: 'Harriet Hartley, 1880' } };
  let ready = false; // B is still loading at first
  const p = await open(REC_A, pages, {
    wrap: (r) => (msg, tab) => (msg.action === 'prefill' && tab.url === REC_B && !ready
      ? { ok: false, error: 'the page is still loading' } : r(msg, tab)),
  });
  await type(p, 'f-title', 'Calvin in the census');
  await p.navigate(REC_B);
  await settle();
  assert.strictEqual(p.$('f-title').value, '', 'the last record\'s details go at once');
  assert.strictEqual(p.$('f-url').value, REC_B);
  await type(p, 'f-date', '1880'); // she starts typing meanwhile
  ready = true;
  await p.navigate(REC_B); // the page finishes loading: another 'complete'
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Harriet Hartley, 1880');
  assert.strictEqual(p.$('f-date').value, '1880', 'what she typed into the empty form stays');
  assert.doesNotMatch(p.$('recipe-banner').textContent, /Couldn’t read/);
  // ...and once read, another 'complete' for it reads nothing more.
  const reads = p.sent.filter((m) => m.action === 'prefill').length;
  await p.navigate(REC_B);
  await settle();
  assert.strictEqual(p.sent.filter((m) => m.action === 'prefill').length, reads);
});

test('Codex 4502845504: a second failed read of the same page keeps what she typed', async () => {
  const pages = { [REC_A]: { title: 'Calvin Hartley, 1880' }, [REC_B]: { title: 'Harriet Hartley, 1880' } };
  let ready = false;
  const p = await open(REC_A, pages, {
    wrap: (r) => (msg, tab) => (msg.action === 'prefill' && tab.url === REC_B && !ready
      ? { ok: false, error: 'the page is still loading' } : r(msg, tab)),
  });
  await p.navigate(REC_B); // first read of B fails
  await settle();
  await type(p, 'f-title', 'Harriet in the census');
  await type(p, 'f-date', 'June 1880');
  await type(p, 'f-notes', 'the farm household');
  await p.navigate(REC_B); // B still loading: the retry fails too
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Harriet in the census');
  assert.strictEqual(p.$('f-date').value, 'June 1880');
  assert.strictEqual(p.$('f-notes').value, 'the farm household');
  assert.strictEqual(p.$('f-url').value, REC_B);
  ready = true;
  await p.navigate(REC_B); // it loads at last: the rest fills in around her typing
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Harriet in the census');
  assert.strictEqual(p.$('f-notes').value, 'the farm household');
  await p.capture();
  assert.strictEqual(p.written[0].captureJson.title, 'Harriet in the census');
});

test('control: a failed read of a DIFFERENT record still clears the last one\'s details at once', async () => {
  const pages = { [REC_A]: { title: 'Calvin Hartley, 1880' } }; // B cannot be read
  const p = await open(REC_A, pages);
  await type(p, 'f-title', 'Calvin in the census');
  p.$('f-notes').value = 'about Calvin';
  await p.navigate(REC_B);
  await settle();
  assert.strictEqual(p.$('f-title').value, '');
  assert.strictEqual(p.$('f-notes').value, '');
  assert.strictEqual(p.$('f-url').value, REC_B);
});

test('Codex 4502845504: typing while the panel\'s first read is out survives that read failing', async () => {
  let hold = null;
  const p = await open(REC_B, {}, { // B cannot be read
    waitReady: false,
    wrap: (r) => { hold = holdFirst(r, 'prefill'); return hold.respond; },
  });
  await until(() => hold.held);
  await type(p, 'f-title', 'Harriet in the census');
  hold.release();
  await p.ready;
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Harriet in the census');
  assert.strictEqual(p.$('f-url').value, REC_B);
});

// ── PR #223 delta review: a retry fills only what she has not touched ───────

// B cannot be read at first; `state.ready = true` lets the next read work.
function loadingB(extra = {}) {
  const st = { ready: false, holdNext: false, release: null };
  const pages = {
    [REC_A]: { title: 'Calvin Hartley, 1880' },
    [REC_B]: Object.assign({ title: 'Harriet Hartley, 1880', people: ['Harriet Hartley'],
      prefill: { imageUrl: REC_B + '/guess.jpg', sourceDate: '1880', repository: 'records.example.org' } }, extra),
  };
  const wrap = (r) => (msg, tab) => {
    if (msg.action !== 'prefill' || tab.url !== REC_B) return r(msg, tab);
    if (!st.ready) return { ok: false, error: 'the page is still loading' };
    if (st.holdNext) {
      st.holdNext = false;
      return new Promise((res) => { st.release = () => res(r(msg, tab)); });
    }
    return r(msg, tab);
  };
  return { st, pages, wrap };
}

test('D1: a retry that works keeps the file address she typed, and Capture saves that file', async () => {
  const { st, pages, wrap } = loadingB();
  const p = await open(REC_A, pages, { wrap });
  await p.navigate(REC_B);
  await settle();
  await type(p, 'f-asset-url', 'https://records.example.org/img/the-real-scan.jpg');
  st.ready = true;
  await p.navigate(REC_B);
  await settle();
  assert.strictEqual(p.$('f-asset-url').value, 'https://records.example.org/img/the-real-scan.jpg');
  await p.capture();
  const fetched = p.sent.filter((m) => m.action === 'fetchAsset').map((m) => m.url);
  assert.deepStrictEqual(fetched, ['https://records.example.org/img/the-real-scan.jpg']);
});

test('D2: people she added to the empty form stay when the retry works', async () => {
  const { st, pages, wrap } = loadingB();
  const p = await open(REC_A, pages, { wrap });
  await p.navigate(REC_B);
  await settle();
  p.$('f-add-person').value = 'Calvin Hartley';
  await p.$('btn-add-person').fire('click');
  st.ready = true;
  await p.navigate(REC_B);
  await settle();
  assert.deepStrictEqual(people(p), ['Calvin Hartley']);
  await p.capture();
  assert.deepStrictEqual(p.written[0].captureJson.people, ['Calvin Hartley']);
});

test('D3: what she types while the retry is reading is kept', async () => {
  const { st, pages, wrap } = loadingB();
  const p = await open(REC_A, pages, { wrap });
  await p.navigate(REC_B);
  await settle();
  await type(p, 'f-title', 'Harr');
  st.ready = true;
  st.holdNext = true;
  const nav = p.navigate(REC_B);
  await until(() => st.release);
  await type(p, 'f-title', 'Harriet in the census');
  await type(p, 'f-notes', 'typed while it read');
  st.release();
  await nav;
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Harriet in the census');
  assert.strictEqual(p.$('f-notes').value, 'typed while it read');
});

test('control: a retry that works DOES fill every field she has not touched', async () => {
  const { st, pages, wrap } = loadingB();
  const p = await open(REC_A, pages, { wrap });
  await p.navigate(REC_B);
  await settle();
  await type(p, 'f-title', 'Harriet in the census');
  st.ready = true;
  await p.navigate(REC_B);
  await settle();
  assert.strictEqual(p.$('f-title').value, 'Harriet in the census');
  assert.strictEqual(p.$('f-date').value, '1880');
  assert.strictEqual(p.$('f-repo').value, 'records.example.org');
  assert.deepStrictEqual(people(p), ['Harriet Hartley']);
  assert.strictEqual(p.$('f-asset-url').value, REC_B + '/guess.jpg');
  assert.match(p.$('recipe-banner').textContent, /^Generic capture/);
});
