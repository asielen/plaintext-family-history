// familysearch-record.js - the pure, importable core of capturing a
// FamilySearch record: which record a page is, FamilySearch's own data for it
// (GEDCOM X, through its data service, with the signed-in session), its image,
// and what the record says about itself for filing.
//
// The browser-side content.js DOES NOT import this file (it is an injected
// classic script that cannot use ES imports); it carries a copy of the block
// between `FHA-SYNC-BEGIN familysearch` / `FHA-SYNC-END familysearch`, and
// tests/test-sync.js fails if the two drift. This module is the canonical,
// tested reference - change it here first, then mirror the copy. Inside the
// block, `recordHints` and `ancDateToEdtf` are lib/ancestry-record.js's (the
// one hint rule for every site); content.js holds both blocks in one scope.
//
// What FamilySearch exposes (verified on live pages, 2026-10):
//   - a record entry ("persona") is ark:/61903/1:1:{id}; its image is
//     ark:/61903/3:1:{id}; the image viewer opened from a record names the
//     record in its query (personArk=…1:1:{id});
//   - the pages are drawn by script, so the record is read from FamilySearch's
//     data service, as the site's own pages do, with the signed-in session
//     (the fssessionid cookie, sent as a Bearer token):
//       GET /ark:/61903/1:1:{id}   (GEDCOM X JSON)  -> the record
//       GET /ark:/61903/3:1:{id}   (GEDCOM X JSON)  -> the image's links
//       GET {its image-stream-image-dist link}      -> the full image (dist.jpg)
//   - many images are restricted to FamilySearch centers and affiliate
//     libraries: the image description has no download link, or the image
//     answers 401/403 to a signed-in session.
//
// THE LOGIN (`fsAuthHeaders`): the Bearer token is the researcher's
// FamilySearch login. It goes only to an https address on familysearch.org or
// one of its subdomains - never to an address FamilySearch's reply names
// without that check (a moved CDN, a tampered answer). It is read from whoever
// is signed in on the page at the moment of the request, passed in, and never
// stored, logged or written into anything this module returns.
//
// The network functions take `deps` - { fetch, base, token, toBase64 } - so
// tests drive every answer without a network or a login.
//
// Code map:
//   fsParseRecordPage    the record's identity from an address (record, or viewer + personArk)
//   fsImageIdFromHref    an image id (3:1) named in an address
//   fsAuthHeaders        the login header, for FamilySearch https addresses only
//   fsStatusMessage      what an HTTP answer means, and what to do next
//   fsFormalToEdtf       GEDCOM X "+1957-01-07" / "A+1887" -> EDTF
//   fsOpenedPerson       the person this record entry is (by its ark), not just a principal
//   fsRecordFrom         the record, citation and title from FamilySearch's data
//   fsHints              recordHints over the record's own GEDCOM X facts
//   fsTitle              "{name} - {collection}", unless the human typed a title
//   fsImageLink          the full-image link in an image description
//   fsJson / fsRecordInfo / fsImageCheck / fsImage   the data-service calls

'use strict';

const { recordHints, ancDateToEdtf } = require('./ancestry-record.js');

// FHA-SYNC-BEGIN familysearch
const FS_HOST_RE = /(^|\.)familysearch\.org$/i;

// A FamilySearch record: ark:/61903/1:1:{id}, or the image viewer opened from
// one (ark:/61903/3:1:{image}?…personArk=…1:1:{id}) - captured as the record
// it was opened from, with this image. null for anything else.
function fsParseRecordPage(href) {
  let u;
  try {
    u = new URL(href);
  } catch (e) {
    return null;
  }
  if (!FS_HOST_RE.test(u.hostname)) return null;
  const m = /\/ark:\/61903\/1:1:([A-Z0-9-]+)/i.exec(u.pathname);
  if (m) return { fsId: m[1].toUpperCase(), viewerImageId: null };
  const img = /\/ark:\/61903\/3:1:([A-Z0-9-]+)/i.exec(u.pathname);
  if (img) {
    const p = /1:1:([A-Z0-9-]+)/i.exec(u.searchParams.get('personArk') || '');
    if (p) return { fsId: p[1].toUpperCase(), viewerImageId: img[1].toUpperCase() };
  }
  return null;
}

// The image (ark:/61903/3:1:{id}) an address names, in its path or query.
function fsImageIdFromHref(href) {
  let u;
  try {
    u = new URL(href);
  } catch (e) {
    return null;
  }
  if (!FS_HOST_RE.test(u.hostname)) return null;
  let query = u.search;
  try {
    query = decodeURIComponent(query);
  } catch (e) {
    /* a stray % that is not valid encoding: read the query as it is */
  }
  const m = /ark:\/61903\/3:1:([A-Z0-9-]+)/i.exec(u.pathname + query);
  return m ? m[1].toUpperCase() : null;
}

// The login header for `href` (resolved against `base`, the page's address):
// { Authorization: 'Bearer …' } only for https on familysearch.org or a
// subdomain of it, and only when someone is signed in (`token`); else {}.
function fsAuthHeaders(href, base, token) {
  let u;
  try {
    u = new URL(href, base);
  } catch (e) {
    return {};
  }
  if (u.protocol !== 'https:' || !FS_HOST_RE.test(u.hostname) || u.username || u.password) return {};
  return token ? { Authorization: 'Bearer ' + token } : {};
}

// What an HTTP answer from FamilySearch means, in words that end in what to
// do next. `what` is 'record' or 'image'; `signedIn` whether a login was sent.
// Built from the status alone: no address, header or token ever goes into it.
function fsStatusMessage(status, what, signedIn) {
  const thing = what === 'image' ? 'this image' : 'this record';
  if (status === 401 || (status === 403 && !signedIn)) {
    return 'FamilySearch asked you to sign in (HTTP ' + status + '). Sign in to FamilySearch in this tab, ' +
      'reload the page, then press Capture again';
  }
  if (status === 403) {
    return 'FamilySearch will not show ' + thing + ' here (HTTP 403): it is restricted to a FamilySearch ' +
      'center or affiliate library. It can be saved from a computer there';
  }
  if (status === 404) return 'FamilySearch has no ' + (what === 'image' ? 'image' : 'record') + ' at this address (HTTP 404) - check the page is the one you meant';
  if (status === 429) return 'FamilySearch asked us to slow down (HTTP 429). Wait a few minutes, then press Capture again';
  if (status >= 500) return 'FamilySearch is having trouble (HTTP ' + status + '). Try again in a little while';
  return 'FamilySearch answered HTTP ' + status + ' for ' + thing + '. Reload the page and press Capture again';
}

// GEDCOM X's formal date in the archive's form (EDTF): "+1957-01-07" ->
// "1957-01-07", "+1957-01" -> "1957-01", "+1957", and "A+1887" (about) ->
// "1887~". A range or open date ("+1880/+1881", "/+1900") is null: no date
// beats a wrong one.
function fsFormalToEdtf(formal) {
  const m = /^(A)?\+(\d{4})(-\d{2})?(-\d{2})?$/.exec(String(formal || '').trim());
  return m ? m[2] + (m[3] || '') + (m[4] || '') + (m[1] ? '~' : '') : null;
}

const fsTypeName = (t) => String(t || '').replace(/^.*[/#]/, '');
const fsFirstTitle = (x) => (x && x.titles && x.titles[0] && x.titles[0].value) || null;

// The person this record entry IS: the one whose persistent identifier names
// ark 1:1:{fsId}. A marriage record has two principals - the bride's entry and
// the groom's - so "the first principal" would title the bride's record with
// the groom's name (PR #214 review). Failing an identifier, the principal,
// else the first person.
function fsOpenedPerson(g, fsId) {
  const persons = (g && Array.isArray(g.persons)) ? g.persons : [];
  const want = '1:1:' + String(fsId || '').toUpperCase();
  const names = (p) => {
    const ids = (p && p.identifiers) || {};
    return [].concat(...Object.values(ids).map((v) => (Array.isArray(v) ? v : [v])))
      .map((x) => String(x || '').toUpperCase());
  };
  return (fsId && persons.find((p) => names(p).some((x) => x.indexOf(want) >= 0))) ||
    persons.find((p) => p && p.principal) || persons[0] || null;
}

function fsPersonName(p) {
  const n = p && p.names && p.names[0];
  const f = n && n.nameForms && n.nameForms[0];
  return (f && String(f.fullText || '').trim()) || null;
}

// The record from FamilySearch's data for it (`g`, GEDCOM X) and its identity
// (`ids`, from fsParseRecordPage). Everything comes from the data, not the
// page: a single-page site's title tags can still describe an earlier record.
function fsRecordFrom(g, ids) {
  const sds = (g && Array.isArray(g.sourceDescriptions)) ? g.sourceDescriptions : [];
  const about = (s) => String((s && s.about) || '');
  const persona = sds.find((s) => /ark:\/61903\/1:1:/i.test(about(s)) &&
    about(s).toUpperCase().indexOf('1:1:' + ids.fsId) >= 0) || {};
  const image = sds.find((s) => /DigitalArtifact/.test(String(s.resourceType || '')) && /3:1:/i.test(about(s)));
  const coll = sds.find((s) => /Collection/.test(String(s.resourceType || '')) && /\/collections\/\d+$/.test(about(s)));
  const rec = sds.find((s) => /\/Record$/.test(String(s.resourceType || '')));
  const imageId = (image && ((/3:1:([A-Z0-9-]+)/i.exec(about(image)) || [])[1] || '').toUpperCase()) ||
    ids.viewerImageId || null;
  const cite = persona.citations && persona.citations[0] && persona.citations[0].value;
  return {
    record: {
      fsId: ids.fsId,
      url: 'https://www.familysearch.org/ark:/61903/1:1:' + ids.fsId,
      imageId,
      recordArk: rec ? (/1:2:[A-Z0-9-]+/i.exec(about(rec)) || [])[0] || null : null,
      collectionId: coll ? (/collections\/(\d+)$/.exec(about(coll)) || [])[1] || null : null,
      collection: fsFirstTitle(coll),
      name: fsPersonName(fsOpenedPerson(g, ids.fsId)),
    },
    citation: cite ? String(cite).replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim() || null : null,
    title: fsFirstTitle(persona),
  };
}

// recordHints over the record's own events: the opened person's facts (and
// the couple's, for a marriage), by GEDCOM X type ("http://gedcomx.org/Death").
function fsHints(record, g) {
  const main = fsOpenedPerson(g, record && record.fsId);
  const facts = [].concat((main && main.facts) || []);
  for (const r of (g && g.relationships) || []) {
    const ids = [r.person1, r.person2].map((x) => String((x && (x.resource || x.resourceId)) || '').replace(/^#/, ''));
    if (!main || !main.id || ids.indexOf(main.id) >= 0) facts.push(...(r.facts || []));
  }
  return recordHints((record && record.collection) || '', (word) => {
    for (const f of facts) {
      if (fsTypeName(f.type).toLowerCase() !== word.toLowerCase()) continue;
      const d = f.date || {};
      const v = fsFormalToEdtf(d.formal) || ancDateToEdtf(d.original);
      if (v) return v;
    }
    return null;
  });
}

// The bundle's title: what the human typed, unless the form still holds the
// page's own title (`pageTitle`, the tab's - on FamilySearch often an earlier
// record's) or nothing; then "{name} - {collection}" from FamilySearch's data.
function fsTitle(record, formTitle, pageTitle) {
  const r = record || {};
  const auto = [r.name, r.collection].filter(Boolean).join(' - ') || 'FamilySearch record ' + r.fsId;
  const typed = String(formTitle || '').trim();
  const page = String(pageTitle || '').trim();
  return !typed || typed === page || /\bFamilySearch\s*$|•/.test(typed) ? auto : typed;
}

// The full-image link in an image description; null when there is none
// (a restricted image often has none).
function fsImageLink(j) {
  const links = (j && j.links) || {};
  const l = links['image-stream-image-dist'] || links.image;
  return l && typeof l.href === 'string' && l.href ? l.href : null;
}

// One GET of FamilySearch's data service. Throws an Error with
// fsStatusMessage's words (never the token or the address). `what` is what
// she is told was refused: 'record' (the default) or 'image' (an image's
// description), so a refused image is never reported as a refused record.
async function fsJson(path, accept, d, what) {
  const headers = Object.assign({ Accept: accept || 'application/x-gedcomx-v1+json' },
    fsAuthHeaders(path, d.base, d.token));
  let r;
  try {
    r = await d.fetch(new URL(path, d.base).href, { credentials: 'include', headers });
  } catch (e) {
    throw new Error('could not reach FamilySearch - check your internet connection, then press Capture again');
  }
  if (!r.ok) throw new Error(fsStatusMessage(r.status, what || 'record', !!d.token));
  if (r.status === 204) return {};
  const text = await r.text();
  try {
    return text ? JSON.parse(text) : {};
  } catch (e) {
    throw new Error('FamilySearch sent something other than ' +
      (what === 'image' ? 'the image\u2019s description' : 'its record data') +
      ' - reload the page and press Capture again');
  }
}

// The record on the page at `href`: { ok, record, citation, title, hints,
// gedcomx } or { ok:false, error }.
async function fsRecordInfo(href, d) {
  const ids = fsParseRecordPage(href);
  if (!ids) return { ok: false, error: 'this is not a FamilySearch record page' };
  try {
    const g = await fsJson('/ark:/61903/1:1:' + ids.fsId, null, d);
    const info = fsRecordFrom(g, ids);
    return Object.assign({ ok: true }, info, { hints: fsHints(info.record, g), gedcomx: g });
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

// Before Capture: can this page's image be saved from here? Reads the image's
// description and asks for its first kilobyte, never the whole picture.
// `imageId` is the record's or the viewer's. { ok, imageId, available, reason? }.
async function fsImageCheck(imageId, d) {
  if (!imageId) return { ok: true, imageId: null, available: false, reason: 'there is no image for it on FamilySearch' };
  let link;
  try {
    link = fsImageLink(await fsJson('/ark:/61903/3:1:' + imageId, null, d, 'image'));
  } catch (e) {
    return { ok: true, imageId, available: false, reason: e.message };
  }
  if (!link) {
    return { ok: true, imageId, available: false,
      reason: 'it is restricted: FamilySearch shows it only at a FamilySearch center or affiliate library' };
  }
  let r;
  try {
    r = await d.fetch(new URL(link, d.base).href.split('?')[0], {
      headers: Object.assign({ Accept: 'image/jpeg', Range: 'bytes=0-1023' }, fsAuthHeaders(link, d.base, d.token)),
    });
  } catch (e) {
    return { ok: true, imageId, available: false, reason: 'could not reach FamilySearch for the image' };
  }
  try {
    if (r.body && r.body.cancel) r.body.cancel();
  } catch (e) {
    /* nothing to stop */
  }
  return r.ok ? { ok: true, imageId, available: true }
    : { ok: true, imageId, available: false, reason: fsStatusMessage(r.status, 'image', !!d.token) };
}

// The full image behind ark:/61903/3:1:{imageId}: { ok, base64, ext,
// contentType } or { ok:false, error }.
async function fsImage(imageId, d) {
  if (!imageId) return { ok: false, error: 'no image named' };
  let link;
  try {
    link = fsImageLink(await fsJson('/ark:/61903/3:1:' + imageId, null, d, 'image'));
  } catch (e) {
    return { ok: false, error: e.message };
  }
  if (!link) {
    return { ok: false, error: 'FamilySearch offers no download of this image: it is restricted to a ' +
      'FamilySearch center or affiliate library' };
  }
  let r;
  try {
    r = await d.fetch(new URL(link, d.base).href.split('?')[0], {
      headers: Object.assign({ Accept: 'image/jpeg' }, fsAuthHeaders(link, d.base, d.token)),
    });
  } catch (e) {
    return { ok: false, error: 'could not reach FamilySearch for the image - check your internet connection' };
  }
  if (!r.ok) return { ok: false, error: fsStatusMessage(r.status, 'image', !!d.token) };
  const blob = await r.blob();
  if (!/^image\//.test(blob.type || '') || blob.size < 30 * 1024) {
    return { ok: false, error: 'the image came back too small to be the full record (' +
      Math.round((blob.size || 0) / 1024) + ' KB) - use FamilySearch’s Download button and drop the file in' };
  }
  return { ok: true, base64: await d.toBase64(blob), ext: 'jpg', contentType: blob.type || 'image/jpeg' };
}
// FHA-SYNC-END familysearch

module.exports = {
  fsParseRecordPage,
  fsImageIdFromHref,
  fsAuthHeaders,
  fsStatusMessage,
  fsFormalToEdtf,
  fsRecordFrom,
  fsHints,
  fsTitle,
  fsImageLink,
  fsJson,
  fsRecordInfo,
  fsImageCheck,
  fsImage,
};
