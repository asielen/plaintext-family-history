// findagrave-record.js - the pure, importable core of capturing a Find a
// Grave memorial: which memorial a page is, what it says (names, dates,
// places, cemetery, plot, inscription, family links, bio, its own citation),
// its photos, and what it says about itself for filing.
//
// The browser-side content.js DOES NOT import this file (it is an injected
// classic script that cannot use ES imports); it carries a copy of the block
// between `FHA-SYNC-BEGIN findagrave` / `FHA-SYNC-END findagrave`, and
// tests/test-sync.js fails if the two drift. This module is the canonical,
// tested reference - change it here first, then mirror the copy. Inside the
// block, ancText / ancElements / ancById / ancTag / recordHints /
// ancDateToEdtf are lib/ancestry-record.js's (one DOM walker that skips the
// Seen It add-on's badges, and one hint rule for every site); content.js holds
// both blocks in one scope.
//
// What Find a Grave shows (the readers the owner's archive copy used on live
// memorial pages, 2026-10): a memorial is /memorial/{id}/{slug}; the page is
// drawn on the server, its facts in labelled elements (#bio-name,
// #birthDateLabel, #deathDateLabel, #cemeteryNameLabel, #plotValueLabel, …),
// the family in #family-grid, the bio in #fullBio, Find a Grave's own citation
// in #citationInfo. Its photos are on the gallery page /memorial/{id}/{slug}/photo
// (.viewer-item[data-photo-id], full size on images.findagrave.com).
//
// NO LOGIN: a memorial and its photos are public. Every request this module
// makes goes without cookies (`credentials: 'omit'`), and a photo is taken
// only from Find a Grave's own image host (fagPhotoUrlOk).
//
// The network function takes `deps` - { fetch, parse, base } - so tests drive
// every answer without a network.
//
// Code map:
//   fagParseMemorial    a memorial's id and slug from an address
//   fagPhotoUrlOk       is this a full-size photo on Find a Grave's image host?
//   fagStatusMessage    what an HTTP answer means, and what to do next
//   fagMemorialFrom     the memorial from its page
//   fagPhotosFrom       the photos listed on one gallery page
//   fagGallery          every photo, page by page
//   fagHints            type, date (its own death, never invented) and class
//   fagTitle            "{name} ({born}-{died}) - Find a Grave memorial"
//   fagPhotoRole        "grave-1", "person-1", … for each photo

'use strict';

const { ancText, ancElements, ancById, ancTag, recordHints, ancDateToEdtf } = require('./ancestry-record.js');

// FHA-SYNC-BEGIN findagrave
const FAG_HOST_RE = /(^|\.)findagrave\.com$/i;
// Find a Grave serves its photos, full size, from this one host.
const FAG_PHOTO_HOST = 'images.findagrave.com';

// A memorial: /memorial/{id}/{slug} (also its /photo and /flower pages).
function fagParseMemorial(href, base) {
  let u;
  try {
    u = new URL(href, base);
  } catch (e) {
    return null;
  }
  if (!FAG_HOST_RE.test(u.hostname)) return null;
  const m = /^\/(?:[a-z]{2}\/)?memorial\/(\d+)(?:\/([^/?#]+))?/i.exec(u.pathname);
  if (!m) return null;
  const slug = m[2] && !/^(photo|photos|flower|flowers|edit)$/i.test(m[2]) ? m[2] : null;
  return { memorialId: m[1], slug };
}

// A photo is taken only from Find a Grave's own image host, over https:
// a gallery entry naming anywhere else is skipped, never fetched.
function fagPhotoUrlOk(url) {
  let u;
  try {
    u = new URL(url);
  } catch (e) {
    return false;
  }
  return u.protocol === 'https:' && u.hostname.toLowerCase() === FAG_PHOTO_HOST && !u.port && !u.username && !u.password &&
    /^\/photos\//.test(u.pathname);
}

// What an HTTP answer from Find a Grave means, in words that end in what to do.
// `what`: 'memorial', 'gallery' or 'photo'.
function fagStatusMessage(status, what) {
  if (status === 404 || status === 410) {
    return what === 'photo'
      ? 'Find a Grave no longer has this photo (HTTP ' + status + ')'
      : 'Find a Grave has no memorial at this address (HTTP ' + status + '): it may have been removed or ' +
        'merged into another. Search Find a Grave for the person and open the memorial it shows now';
  }
  if (status === 429) return 'Find a Grave asked us to slow down (HTTP 429). Wait a few minutes, then press Capture again';
  if (status === 401 || status === 403) {
    return 'Find a Grave refused the ' + (what === 'photo' ? 'photo' : what === 'gallery' ? 'photo list' : 'memorial') +
      ' (HTTP ' + status + '). Reload the page, and try again later if it keeps happening';
  }
  if (status >= 500) return 'Find a Grave is having trouble (HTTP ' + status + '). Try again in a little while';
  return 'Find a Grave answered HTTP ' + status + ' for the ' + (what === 'photo' ? 'photo' : what) +
    '. Reload the page and press Capture again';
}

function fagClassHas(el, name) {
  return String((el && el.className) || '').split(/\s+/).indexOf(name) >= 0;
}

function fagAttr(el, name) {
  return el && el.getAttribute ? el.getAttribute(name) : null;
}

// The memorial on a page (`root`: the document, or a stand-in tree) at
// `href`. `opts`: { title (the document's), hidden / block (ancText's: the
// live page passes its CSS), today (YYYY-MM-DD, for the citation's "accessed") }.
// { ok, record, citation } or { ok:false, error }.
function fagMemorialFrom(root, href, opts) {
  const o = opts || {};
  const id = fagParseMemorial(href);
  if (!id) return { ok: false, error: 'this is not a Find a Grave memorial page' };
  const text = (el, skip) => (el ? ancText(el, { hidden: o.hidden, block: o.block, skip }) || null : null);
  const byId = (x) => text(ancById(root, x));
  const name = byId('bio-name');
  if (!name) {
    return { ok: false, error: 'this page shows no memorial - it may have been removed or merged into another. ' +
      'Search Find a Grave for the person and open the memorial it shows now' };
  }
  const canon = ancElements(root, (e) => ancTag(e) === 'LINK' && /canonical/i.test(fagAttr(e, 'rel') || ''))[0];
  const cid = canon ? fagParseMemorial(fagAttr(canon, 'href') || '', href) : null;
  const slug = (cid && cid.memorialId === id.memorialId && cid.slug) || id.slug;

  // The value under a section heading ("Inscription", "Gravesite Details"):
  // the heading's own box, less the heading itself - or, when the heading
  // sits alone in a box of its own, the box that follows it.
  const section = (label) => {
    for (const h of ancElements(root, (e) => /^H[234]$/.test(ancTag(e)))) {
      if ((text(h) || '').toLowerCase() !== label.toLowerCase()) continue;
      let t = text(h.parentNode, h);
      const box = h.parentNode;
      const around = box && box.parentNode ? Array.from(box.parentNode.childNodes || []) : [];
      if (!t) t = text(around.slice(around.indexOf(box) + 1).find((n) => n.nodeType === 1));
      if (t) return t.slice(0, 4000);
    }
    return null;
  };
  // A date as Find a Grave shows it, less the age it adds ("4 Jan 1918 (aged 75)").
  const dateOf = (x) => {
    const v = byId(x);
    return v ? v.replace(/\s*\(aged[^)]*\)\s*$/i, '').trim() || null : null;
  };
  const ageAt = (x) => {
    const m = /\(aged\s+([^)]*)\)\s*$/i.exec(byId(x) || '');
    return m ? m[1].trim() : null;
  };

  const cemLabel = ancById(root, 'cemeteryNameLabel');
  let cemA = cemLabel;
  while (cemA && cemA.nodeType === 1 && ancTag(cemA) !== 'A') cemA = cemA.parentNode;
  const cemM = cemA && cemA.nodeType === 1 ? /\/cemetery\/(\d+)/.exec(fagAttr(cemA, 'href') || '') : null;

  const family = [];
  const grid = ancById(root, 'family-grid');
  for (const ul of grid ? ancElements(grid, (e) => ancTag(e) === 'UL' && fagClassHas(e, 'member-family')) : []) {
    const lid = fagAttr(ul, 'aria-labelledby');
    const relation = lid ? byId(lid) : null;
    for (const a of ancElements(ul, (e) => ancTag(e) === 'A' && /\/memorial\//.test(fagAttr(e, 'href') || ''))) {
      const mm = fagParseMemorial(fagAttr(a, 'href'), href);
      if (!mm || mm.memorialId === id.memorialId || family.some((f) => f.memorialId === mm.memorialId)) continue;
      const nameEl = ancElements(a, (e) => fagAttr(e, 'itemprop') === 'name')[0];
      const lifeEl = ancElements(a, (e) => fagClassHas(e, 'life'))[0];
      const lines = (text(a) || '').split('\n').map((x) => x.trim()).filter(Boolean);
      family.push({
        relation, memorialId: mm.memorialId, slug: mm.slug,
        name: text(nameEl) || lines[0] || null,
        dates: text(lifeEl) || lines.slice(1).join(' ') || null,
      });
    }
  }

  let citation = byId('citationInfo');
  if (citation && o.today) citation = citation.replace(/accessed\s*\)/, 'accessed ' + o.today + ')');
  const inputValue = (x) => {
    const el = ancById(root, x);
    return el ? String(fagAttr(el, 'value') || text(el) || '').trim() || null : null;
  };
  return {
    ok: true,
    record: {
      memorialId: id.memorialId,
      slug,
      url: 'https://www.findagrave.com/memorial/' + id.memorialId + (slug ? '/' + slug : ''),
      name,
      birth: { date: dateOf('birthDateLabel'), place: byId('birthLocationLabel') },
      death: { date: dateOf('deathDateLabel'), age: ageAt('deathDateLabel'), place: byId('deathLocationLabel') },
      burial: {
        cemetery: byId('cemeteryNameLabel'),
        cemeteryId: cemM ? cemM[1] : null,
        city: byId('cemeteryCityName'),
        county: byId('cemeteryCountyName'),
        state: byId('cemeteryStateName'),
        country: byId('cemeteryCountryName'),
        plot: byId('plotValueLabel'),
      },
      inscription: section('Inscription'),
      gravesiteDetails: section('Gravesite Details'),
      bio: byId('fullBio') || byId('partBio'),
      family,
      createdBy: (inputValue('createdBy') || '').replace(/^Created by:\s*/i, '') || null,
      added: (inputValue('addedDate') || '').replace(/^Added:\s*/i, '') || null,
      collection: 'Find a Grave',
    },
    citation,
  };
}

// The photos on one gallery page: { photos: [{ photoId, url, caption,
// addedBy, type }], total }. Only full-size photos on Find a Grave's own image
// host are listed (fagPhotoUrlOk); the address's query is dropped.
function fagPhotosFrom(root, base) {
  const photos = [];
  for (const item of ancElements(root, (e) => fagClassHas(e, 'viewer-item') && fagAttr(e, 'data-photo-id'))) {
    const photoId = fagAttr(item, 'data-photo-id');
    if (photos.some((p) => p.photoId === photoId)) continue;
    const img = ancElements(item, (e) => ancTag(e) === 'IMG')[0];
    let src = img ? fagAttr(img, 'data-src') || fagAttr(img, 'src') : null;
    try {
      src = src ? new URL(src, base).href.replace(/\?.*$/, '') : null;
    } catch (e) {
      src = null;
    }
    if (!src || !fagPhotoUrlOk(src)) continue;
    const typeP = ancElements(item, (e) => ancTag(e) === 'P' && /Photo type:/i.test(ancText(e)))[0];
    const caption = ancElements(item, (e) => fagClassHas(e, 'photo-text'))[0];
    const added = ancElements(item, (e) => fagClassHas(e, 'added-by'))[0];
    photos.push({
      photoId,
      url: src,
      caption: caption ? ancText(caption) || null : null,
      addedBy: added ? ancText(added).replace(/^Added by:\s*/i, '') || null : null,
      type: typeP ? ancText(typeP).replace(/^[\s\S]*Photo type:\s*/i, '').trim() || null : null,
    });
  }
  const cnt = ancElements(root, (e) => fagClassHas(e, 'photosCount'))[0];
  const total = cnt ? parseInt(ancText(cnt), 10) : NaN;
  return { photos, total: isNaN(total) ? null : total };
}

// Every photo on the memorial, from its gallery pages (at most 20), without
// cookies. { ok, photos, total } or { ok:false, error }.
async function fagGallery(memorialId, slug, d) {
  const path = '/memorial/' + memorialId + (slug ? '/' + slug : '') + '/photo';
  const photos = [];
  let total = null;
  for (let page = 1; page <= 20; page++) {
    let r;
    try {
      r = await d.fetch(new URL(path + (page > 1 ? '?page=' + page : ''), d.base).href, { credentials: 'omit' });
    } catch (e) {
      if (page === 1) return { ok: false, error: 'could not reach Find a Grave for the photos - check your internet connection' };
      break;
    }
    if (!r.ok) {
      if (page === 1) return { ok: false, error: fagStatusMessage(r.status, 'gallery') };
      break;
    }
    const one = fagPhotosFrom(d.parse(await r.text()), d.base);
    if (total == null) total = one.total;
    let added = 0;
    for (const p of one.photos) {
      if (photos.some((x) => x.photoId === p.photoId)) continue;
      photos.push(p);
      added++;
    }
    if (!added || (total != null && photos.length >= total)) break;
  }
  return { ok: true, photos, total: total == null ? photos.length : total };
}

// What the memorial says about itself (TI-066): the shared rule over the
// memorial's own death - Find a Grave gives no burial date, and a burial is
// known only to be "on or after the death", so none is invented (TI-068) -
// and always `derivative`: a memorial is a volunteer's compilation from the
// stone, records and family knowledge, not the original record.
function fagHints(record) {
  const r = record || {};
  const h = recordHints(r.collection || 'Find a Grave', (word) =>
    (word === 'Death' ? ancDateToEdtf(r.death && r.death.date) : null));
  return { sourceType: h.sourceType, sourceDate: h.sourceDate, sourceClass: 'derivative' };
}

// "{name} ({born}-{died}) - Find a Grave memorial", the years from its own dates.
function fagTitle(record) {
  const r = record || {};
  const year = (d) => (/(\d{4})/.exec(String(d || '')) || [])[1] || '?';
  const life = [year(r.birth && r.birth.date), year(r.death && r.death.date)];
  return (r.name || 'Memorial ' + r.memorialId) +
    (life.some((y) => y !== '?') ? ' (' + life.join('-') + ')' : '') + ' - Find a Grave memorial';
}

// A photo's role, from Find a Grave's photo type: "grave-1", "person-1",
// "family-1", "document-1", … and "photo-1" for a type it does not name (or
// none) - never a word the archive already uses for another kind of file
// ("record", "page", "back"). `counts` numbers each kind.
const FAG_PHOTO_KINDS = ['grave', 'person', 'family', 'document', 'flowers', 'cemetery', 'other'];

function fagPhotoRole(type, counts) {
  const word = String(type || '').trim().toLowerCase();
  const kind = FAG_PHOTO_KINDS.indexOf(word) >= 0 ? word : 'photo';
  counts[kind] = (counts[kind] || 0) + 1;
  return kind + '-' + counts[kind];
}
// FHA-SYNC-END findagrave

module.exports = {
  FAG_PHOTO_HOST,
  fagParseMemorial,
  fagPhotoUrlOk,
  fagStatusMessage,
  fagMemorialFrom,
  fagPhotosFrom,
  fagGallery,
  fagHints,
  fagTitle,
  fagPhotoRole,
};
