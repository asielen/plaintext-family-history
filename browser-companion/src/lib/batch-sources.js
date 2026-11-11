// batch-sources.js - the pure, importable core of what fills a batch
// download's queue, read from a page the owner has open: an Ancestry tree
// person's sources and the photos, clippings and documents in their
// gallery, a FamilySearch tree person's sources, the Find a Grave
// memorials a page links to, and one page of the signed-in member's own
// Newspapers.com clippings list. Batch downloads are switched off unless the
// browser has been told otherwise (src/batch.js); content.js answers these
// questions only then.
//
// The browser-side content.js DOES NOT import this file (it is an injected
// classic script that cannot use ES imports); it carries a copy of the block
// between `FHA-SYNC-BEGIN batch-sources` / `FHA-SYNC-END batch-sources`, and
// tests/test-sync.js fails if the two drift. This module is the canonical,
// tested reference - change it here first, then mirror the copy. Inside the
// block, the ancestry, familysearch, findagrave and newspapers blocks'
// readers are used as they are: content.js holds every block in one scope.
//
// What each reader may send:
//   - Ancestry and Find a Grave pages are read from the page itself: no
//     request at all;
//   - a FamilySearch tree person is read from FamilySearch's own data
//     service (fsJson), under its login rule: the Bearer token goes only to
//     https familysearch.org or a subdomain of it (fsAuthHeaders);
//   - the Newspapers.com list is one same-origin GET per page of 24, on
//     https newspapers.com only, as the site's own "My Clippings" page asks.
//
// Code map:
//   batchDescribeOthers     "links to example.org (2), a note (1)"
//   batchAncPersonSources   an Ancestry tree person's record links (no request)
//   batchAncGallery         the items on an Ancestry tree person's Gallery page (no request)
//   batchFsPersonPid        the tree person a FamilySearch address names
//   batchFsPersonSources    a FamilySearch tree person's record ids (data service)
//   batchFagLinks           the Find a Grave memorials a page links to
//   batchNpUsername         who is signed in, from the page's own data
//   batchNpClippingsList    one page of the member's own clippings list

'use strict';

const { ancElements, ancText, ancTag, ancById, ancParseRecordPage, ancParseMediaPage } = require('./ancestry-record.js');
const { fsJson } = require('./familysearch-record.js');
const { fagParseMemorial } = require('./findagrave-record.js');
const { npObjectAt } = require('./newspapers-page-data.js');

// FHA-SYNC-BEGIN batch-sources
const BATCH_FS_PERSON_RE = /\/tree\/person\/(?:[a-z-]+\/)?([A-Z0-9]{4}-[A-Z0-9]{2,4})(?![A-Z0-9])/i;
const BATCH_ANC_HOST_RE = /(^|\.)ancestry\.[a-z.]+$/i;
const BATCH_FS_HOST_RE = /(^|\.)familysearch\.org$/i;
const BATCH_NP_HOST_RE = /(^|\.)newspapers\.com$/i;

// What a person's other sources are, counted: "links to example.org (2),
// a note (1)". Labels only, for the panel to say what it did not queue.
function batchDescribeOthers(list) {
  const n = {};
  for (const x of list) n[x] = (n[x] || 0) + 1;
  return Object.keys(n).map((k) => k + (n[k] > 1 ? ' (' + n[k] + ')' : '')).join(', ');
}

function batchFirstLine(el) {
  return el ? (ancText(el).split('\n')[0] || '').slice(0, 200) : '';
}

// An Ancestry tree person's Facts page (`root`, at `href`): the Ancestry
// records its sources link to, as { dbId, recordId, title }, plus a count of
// the sources that are something else (a clipping, a web link), so the panel
// can say what it left out rather than "no sources". Read from the person's
// own sources list (#sourcesSection) and nowhere else on the page: a record
// page links its household's records, which are not this person's sources
// (PR #224 Codex 4504294850). No such list: nothing (`sectionFound: false`).
// Seen It's badges are skipped (ancText), and nothing is fetched.
function batchAncPersonSources(root, href) {
  const seen = new Set();
  const sources = [];
  const box = ancById(root, 'sourcesSection');
  const h1 = ancElements(root, (e) => ancTag(e) === 'H1')[0];
  if (!box) {
    return { ok: true, person: batchFirstLine(h1) || null, sources, sectionFound: false, total: 0, other: 0, otherWhat: '' };
  }
  for (const a of ancElements(box, (e) => ancTag(e) === 'A' && !!e.getAttribute('href'))) {
    let u;
    try {
      u = new URL(a.getAttribute('href'), href);
    } catch (e) {
      continue;
    }
    // An image-only collection's source opens into its image:
    // /interactive/{dbId}/{imageId}/{recordId} is that record.
    const inter = BATCH_ANC_HOST_RE.test(u.hostname) && /\/interactive\/(\d+)\/[^/?#]+\/(\d+)/.exec(u.pathname);
    const id = inter ? { dbId: inter[1], recordId: inter[2] } : ancParseRecordPage(u.href);
    if (!id) continue;
    const key = id.dbId + ':' + id.recordId;
    if (seen.has(key)) continue;
    seen.add(key);
    sources.push({ dbId: id.dbId, recordId: id.recordId, title: batchFirstLine(a) });
  }
  const others = [];
  for (const a of ancElements(box, (e) => ancTag(e) === 'A' && !!e.getAttribute('href'))) {
    let u;
    try {
      u = new URL(a.getAttribute('href'), href);
    } catch (e) {
      continue;
    }
    if (!/^https?:$/.test(u.protocol)) continue;
    if (BATCH_NP_HOST_RE.test(u.hostname) && /\/(clip|article)\//.test(u.pathname)) others.push('Newspapers.com clippings');
    else if (!/(^|\.)(ancestry|ancestrycdn)\.[a-z.]+$/i.test(u.hostname)) others.push('links to ' + u.hostname.replace(/^www\./, ''));
  }
  return {
    ok: true,
    person: batchFirstLine(h1) || null,
    sources,
    sectionFound: true,
    total: sources.length + others.length,
    other: others.length,
    otherWhat: batchDescribeOthers(others),
  };
}

// An Ancestry tree person's Gallery page (`root`, at `href`:
// /family-tree/person/tree/{treeId}/person/{personId}/gallery): the photos,
// clippings and documents its tiles open, as { mediaId, url, title } - the
// media viewer's address without its query, which is how Seen It knows a
// shared item (med:{mediaId}), and the tile's caption. Read from the tiles
// (.mediaGalleryItem) and nowhere else on the page, once each, on the
// page's own site; Seen It's badges are skipped (ancText), and nothing is
// fetched. The gallery shows so many tiles a page: `more` says this page
// links to the next one, which she opens herself - the batch never pages
// through a gallery on its own. Not a gallery page: nothing
// (`sectionFound: false`).
const BATCH_ANC_GALLERY_RE = /^\/family-tree\/person\/tree\/\d+\/person\/\d+\/gallery\/?$/i;

function batchAncGallery(root, href) {
  const person = batchFirstLine(ancElements(root, (e) => ancTag(e) === 'H1')[0]) || null;
  const sources = [];
  let here = null;
  try {
    here = new URL(href);
  } catch (e) {
    here = null;
  }
  if (!here || !BATCH_ANC_HOST_RE.test(here.hostname) || !BATCH_ANC_GALLERY_RE.test(here.pathname)) {
    return { ok: true, person, sources, sectionFound: false, total: 0, other: 0, otherWhat: '', more: false, page: 1 };
  }
  const hasClass = (e, c) => String(e.className || '').split(/\s+/).indexOf(c) >= 0;
  const link = (a) => {
    try {
      return new URL(a.getAttribute('href'), href);
    } catch (e) {
      return null;
    }
  };
  const seen = new Set();
  for (const tile of ancElements(root, (e) => hasClass(e, 'mediaGalleryItem'))) {
    const caption = ancElements(tile, (e) => hasClass(e, 'mediaCardCaption'))[0];
    const titled = ancElements(tile, (e) => !!(e.getAttribute && e.getAttribute('title')))[0];
    for (const a of ancElements(tile, (e) => ancTag(e) === 'A' && !!e.getAttribute('href'))) {
      const u = link(a);
      if (!u || u.origin !== here.origin) continue;
      const id = ancParseMediaPage(u.href);
      if (!id || !id.treeId || !id.personId || seen.has(id.mediaId)) continue;
      seen.add(id.mediaId);
      const title = batchFirstLine(caption) || String((titled && titled.getAttribute('title')) || '').slice(0, 200) ||
        batchFirstLine(a);
      sources.push({ mediaId: id.mediaId, url: id.url, title });
    }
  }
  // The next page: a link to this gallery's galleryPage={this one + 1}.
  const page = Math.max(1, Number(here.searchParams.get('galleryPage')) || 1);
  const more = ancElements(root, (e) => {
    if (ancTag(e) !== 'A' || !e.getAttribute('href')) return false;
    const u = link(e);
    return !!u && u.origin === here.origin && u.pathname === here.pathname &&
      Number(u.searchParams.get('galleryPage')) === page + 1;
  }).length > 0;
  return { ok: true, person, sources, sectionFound: true, total: sources.length, other: 0, otherWhat: '', more, page };
}

// The FamilySearch tree person an address names (/tree/person/details/{PID}),
// or null.
function batchFsPersonPid(href) {
  let u;
  try {
    u = new URL(href);
  } catch (e) {
    return null;
  }
  if (!BATCH_FS_HOST_RE.test(u.hostname)) return null;
  const m = BATCH_FS_PERSON_RE.exec(u.pathname);
  return m ? m[1].toUpperCase() : null;
}

// A FamilySearch tree person's sources (the Sources tab shows no links, so
// FamilySearch's own data service is asked, with `d` - { fetch, base, token }
// - as fsJson takes it): the FamilySearch records among them as { fsId,
// title }, and a count of the rest. Two requests, one after the other.
async function batchFsPersonSources(href, d) {
  const pid = batchFsPersonPid(href);
  if (!pid) return { ok: false, error: 'open a person in the FamilySearch Family Tree, then press the button again' };
  let j;
  try {
    j = await fsJson('/platform/tree/persons/' + pid + '/sources', 'application/x-fs-v1+json', d);
  } catch (e) {
    return { ok: false, error: e.message, slowDown: !!e.slowDown };
  }
  const sources = [];
  const others = [];
  const seen = new Set();
  const all = Array.isArray(j && j.sourceDescriptions) ? j.sourceDescriptions : [];
  for (const s of all) {
    const r = /ark:\/61903\/1:1:([A-Z0-9-]+)/i.exec(String((s && s.about) || ''));
    if (!r) {
      let host = null;
      try {
        host = s && s.about ? new URL(s.about).hostname.replace(/^www\./, '') : null;
      } catch (e) {
        /* not a web address */
      }
      others.push(host ? 'links to ' + host : 'a note or citation with no link');
      continue;
    }
    const fsId = r[1].toUpperCase();
    if (seen.has(fsId)) continue;
    seen.add(fsId);
    sources.push({ fsId, title: String((s.titles && s.titles[0] && s.titles[0].value) || '').slice(0, 200) });
  }
  let person = null;
  let slowDown = false;
  try {
    const p = await fsJson('/platform/tree/persons/' + pid, 'application/x-fs-v1+json', d);
    const disp = p && p.persons && p.persons[0] && p.persons[0].display;
    person = (disp && disp.name) || null;
  } catch (e) {
    // The name is a label only - but a "slow down" is still said.
    slowDown = !!e.slowDown;
  }
  return { ok: true, person, pid, sources, total: all.length, other: others.length, otherWhat: batchDescribeOthers(others),
    slowDown };
}

// The Find a Grave memorials a page names, each only from where it means
// "these memorials" (PR #224 Codex 4504294850):
//   - on Find a Grave, every memorial the page links to (a memorial's own
//     family, a search or cemetery listing), the memorial itself first;
//   - on an Ancestry tree person, the person's own sources list
//     (#sourcesSection) only - never the hints and records elsewhere on it;
//   - on a FamilySearch tree person (`d` given), the person's own sources,
//     from FamilySearch's data, never the page's links.
// Anywhere else, nothing. [{ memorialId, slug, title }].
async function batchFagLinks(root, href, d) {
  const out = [];
  const add = (m, name) => {
    if (m && !out.some((x) => x.memorialId === m.memorialId)) {
      out.push({ memorialId: m.memorialId, slug: m.slug, title: String(name || '').slice(0, 200) });
    }
  };
  let person = null;
  let host = '';
  try {
    host = new URL(href).hostname;
  } catch (e) {
    /* no host: nothing below matches */
  }
  let scope = null; // the part of the page whose links are its memorials
  let sectionFound = true;
  let slowDown = false;
  if (/(^|\.)findagrave\.com$/i.test(host)) {
    scope = root;
    const here = fagParseMemorial(href);
    if (here) {
      person = batchFirstLine(ancById(root, 'bio-name')) || null;
      add(here, person);
    }
  } else if (BATCH_ANC_HOST_RE.test(host)) {
    scope = ancById(root, 'sourcesSection');
    sectionFound = !!scope;
  }
  for (const a of scope ? ancElements(scope, (e) => ancTag(e) === 'A' && /\/memorial\//.test(e.getAttribute('href') || '')) : []) {
    add(fagParseMemorial(a.getAttribute('href'), href), batchFirstLine(a));
  }
  if (BATCH_FS_HOST_RE.test(host)) {
    sectionFound = !!batchFsPersonPid(href);
    if (d && sectionFound) {
      try {
        const j = await fsJson('/platform/tree/persons/' + batchFsPersonPid(href) + '/sources', 'application/x-fs-v1+json', d);
        for (const s of (j && j.sourceDescriptions) || []) {
          add(fagParseMemorial(String((s && s.about) || '')), s && s.titles && s.titles[0] && s.titles[0].value);
        }
      } catch (e) {
        if (e.slowDown) slowDown = true;
        else return { ok: false, error: e.message };
      }
    }
  }
  if (!person) person = batchFirstLine(ancElements(root, (e) => ancTag(e) === 'H1')[0]) || null;
  return { ok: true, person, sources: out, total: out.length, other: 0, sectionFound, slowDown };
}

// Who is signed in to Newspapers.com, from the page's own data (`text`, the
// decoded page data): the site's `"user":{"id":…,"username":…}` object.
function batchNpUsername(text) {
  const t = String(text || '');
  const um = /"user":\{"id":(\d+)/.exec(t);
  if (!um) return null;
  const o = npObjectAt(t, um.index + '"user":'.length);
  return (o && typeof o.username === 'string' && o.username) || null;
}

// One page of the signed-in member's own clippings list (24 at a time, from
// `cursor`): { ok, user, clippings, nextCursor, more } or { ok:false, error }.
// `d` = { fetch, base }; `base` must be an https Newspapers.com page.
async function batchNpClippingsList(cursor, username, d) {
  let base;
  try {
    base = new URL(d.base);
  } catch (e) {
    base = null;
  }
  if (!base || base.protocol !== 'https:' || !BATCH_NP_HOST_RE.test(base.hostname)) {
    return { ok: false, error: 'the tab is not on Newspapers.com, so your clippings list was not read' };
  }
  if (!username) {
    return { ok: false, error: 'could not tell who is signed in to Newspapers.com - sign in, reload the page, then press Start again' };
  }
  const path = '/api/clipping/list?user=' + encodeURIComponent(username) + '&visibility=all&count=24' +
    '&cursor_mark=' + encodeURIComponent(String(cursor == null ? 0 : cursor)) + '&product_id=1';
  let r;
  try {
    r = await d.fetch(new URL(path, base).href, { credentials: 'same-origin', headers: { accept: 'application/json' } });
  } catch (e) {
    return { ok: false, error: 'could not reach Newspapers.com for your clippings list - check your internet connection' };
  }
  if (r.status === 429 || r.status === 503) {
    return { ok: false, error: 'Newspapers.com asked us to slow down (HTTP ' + r.status + ')' };
  }
  if (r.status === 401 || r.status === 403) {
    return { ok: false, error: 'Newspapers.com refused your clippings list (HTTP ' + r.status + ') - sign in and reload the page' };
  }
  if (!r.ok) return { ok: false, error: 'Newspapers.com would not send your clippings list (HTTP ' + r.status + ')' };
  let j;
  try {
    j = await r.json();
  } catch (e) {
    // Not JSON is something else; anything else is the answer breaking off.
    return e instanceof SyntaxError
      ? { ok: false, error: 'Newspapers.com sent something other than your clippings list' }
      : { ok: false, transient: true, error: 'the connection to Newspapers.com broke off while your clippings list was coming down' };
  }
  return {
    ok: true,
    user: username,
    clippings: Array.isArray(j && j.clippings) ? j.clippings : [],
    nextCursor: j && j.next_cursor_mark != null ? j.next_cursor_mark : null,
    more: !!(j && j.more_results),
  };
}
// FHA-SYNC-END batch-sources

module.exports = {
  batchDescribeOthers,
  batchAncPersonSources,
  batchAncGallery,
  batchFsPersonPid,
  batchFsPersonSources,
  batchFagLinks,
  batchNpUsername,
  batchNpClippingsList,
};
