// ancestry-record.js - the pure, importable core of reading an Ancestry record
// page: which record it is, its labelled index fields, the household table,
// the site's own source citation, the linked scan, and the plain hints a
// record gives about itself (what kind of source, when, original or index).
//
// The browser-side content.js DOES NOT import this file (it is an injected
// classic script that cannot use ES imports); it carries a copy of the block
// between `FHA-SYNC-BEGIN ancestry` / `FHA-SYNC-END ancestry`, and
// tests/test-sync.js fails if the two drift. This module is the canonical,
// tested reference - change it here first, then mirror the copy.
//
// The readers walk the page through childNodes, nodeType, tagName, id,
// className and getAttribute only - no selectors, no layout - so the same code
// runs on the live page and on the small stand-in trees the tests build.
//
// It also reads a photo someone shared to their tree, in Ancestry's media
// viewer (/mediaui-viewer/…/media/{mediaId}): its identity, its image and
// its Details, so Capture can save it as a photo (the panel's
// captureAncestryPhoto).
//
// What the site shows (today's record layout, /search/collections/{dbId}/records/{recordId}):
//   - <title>: "{collection} - Ancestry.com"; <h1>: the person's name;
//   - #recordFieldsContainer > dl: dt/dd pairs, the record's index fields;
//   - #recordData tables: the household ("Household Members (Name)", "Age");
//   - #sourceCitation: <section>s headed by <h3> ("Citation", "Information",
//     "Collection description");
//   - the scan: a link to /imageviewer/collections/{dbId}/images/{imageId}, or
//     in image-only collections /interactive/{dbId}/{imageId}/{recordId}.
//
// The Seen It add-on (seen-it/ in this repo) injects its own badges into these
// same pages - after record links in the household, a badge in the corner.
// They are not the site's index, so every reader here skips them (TI-067).
//
// Code map:
//   ancParseRecordPage   the record's identity from an address
//   ancImageFromHref     the scan's image id from a link, for this collection
//   ancIsSeenItNode      is this element one Seen It put on the page?
//   ancText              an element's text, without Seen It's badges or hidden parts
//   ancRecordFrom        the whole record from a page: fields, household, citation
//   ancDateToEdtf        "7 Jan 1950" -> "1950-01-07"; "abt 1845" -> "1845~"
//   recordHints          source type, date and class from a record's own events (every site)
//   ancHints             recordHints over an Ancestry record's labelled fields
//   ancTitle             "{name} - {collection}", unless the human typed a title
//   ancParseMediaPage    a shared photo's identity (its media id) and the address to keep
//   ancMediaImageUrl     the photo's own image, as the viewer shows it, on the page's own site
//   ancMediaDetails      the photo's Details: title, date, place, description, who added it, who it shows
//   ancLinkedNames       the names a photo is linked to, without their years or "Private"
//   ancMediaMemberNodes  the "Saved by" lists of other members, for the page copy to leave out
//   ancMediaFrom         the whole shared photo from its viewer page
//   ancMediaHints        a photo, dated by its Details' date when the archive can read it
//   ancPhotoFileStem     the name the photo keeps in the photo library

'use strict';

// FHA-SYNC-BEGIN ancestry
// A record has one identity wherever it appears: {dbId}:{recordId}, at
// /search/collections/{dbId}/records/{recordId} (older links:
// /discoveryui-content/view/{recordId}:{dbId}). null for anything else.
function ancParseRecordPage(href) {
  let u;
  try {
    u = new URL(href);
  } catch (e) {
    return null;
  }
  if (!/(^|\.)ancestry\.[a-z.]+$/i.test(u.hostname)) return null;
  let m = /\/search\/collections\/(\d+)\/records\/(\d+)/.exec(u.pathname);
  if (m) return { dbId: m[1], recordId: m[2] };
  m = /\/discoveryui-content\/view\/(\d+):(\d+)/.exec(u.pathname);
  if (m) return { dbId: m[2], recordId: m[1] };
  return null;
}

// The scan a record page links to, for THIS collection only (a household
// member's link points at another record, never at this record's image):
// /imageviewer/collections/{dbId}/images/{imageId}, or in image-only
// collections /interactive/{dbId}/{imageId}/{recordId}. null otherwise.
function ancImageFromHref(href, dbId) {
  const s = String(href || '');
  const m = /\/imageviewer\/collections\/(\d+)\/images\/([^/?#]+)/.exec(s) ||
    /\/interactive\/(\d+)\/([^/?#]+)\/\d+/.exec(s);
  if (!m || m[1] !== String(dbId)) return null;
  try {
    return decodeURIComponent(m[2]);
  } catch (e) {
    return m[2]; // a stray % that is not valid encoding: keep the raw segment
  }
}

// Seen It's own elements (seen-it/content*.js): its pills, marks and tree
// chips, and its badges and banners, whose ids all start "nsi-". Seen It also
// toggles a few "nsi-" modifier classes (nsi-ruled-out, nsi-was-seen,
// nsi-has-clip) on the SITE's own rows; those rows are the site's index and
// are kept, which is why this is a list and not every "nsi-" class.
// tests/test-ancestry.js reads seen-it/ and fails if it creates a class
// this list does not know.
const ANC_SEEN_IT_CLASSES = ['nsi-pills', 'nsi-pill', 'nsi-marks', 'nsi-mark', 'nsi-node-chip'];

function ancIsSeenItNode(el) {
  if (!el || el.nodeType !== 1) return false;
  if (/^nsi-/.test(String(el.id || ''))) return true;
  const cls = String(el.className && el.className.baseVal !== undefined ? el.className.baseVal : el.className || '');
  return cls.split(/\s+/).some((c) => ANC_SEEN_IT_CLASSES.indexOf(c) >= 0);
}

const ANC_SKIP_TAGS = ['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'BUTTON', 'svg', 'SVG'];
const ANC_BLOCK_TAGS = ['ADDRESS', 'ARTICLE', 'ASIDE', 'BLOCKQUOTE', 'DD', 'DIV', 'DL', 'DT',
  'FIGCAPTION', 'FIGURE', 'FOOTER', 'FORM', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'HEADER',
  'HR', 'LI', 'MAIN', 'NAV', 'OL', 'P', 'PRE', 'SECTION', 'TABLE', 'TBODY', 'TD', 'TFOOT',
  'TH', 'THEAD', 'TR', 'UL'];

// An element's text as a reader sees it: line breaks between blocks, spaces
// tidied, and nothing from Seen It, scripts, buttons ("Show more", "Save") or
// anything `opts.hidden(el)` says is not shown. `opts.block(el, tag)` says
// whether an element starts a new line (on the live page, its CSS display;
// without one, the usual block tags). `opts.skip` is one more element to
// leave out (a section's own heading). Not innerText: that would read Seen
// It's badges as if they were the record.
function ancText(node, opts) {
  const o = opts || {};
  const parts = [];
  (function walk(n) {
    if (!n) return;
    if (n.nodeType === 3) {
      parts.push(String(n.nodeValue || '').replace(/\s+/g, ' '));
      return;
    }
    if (n.nodeType !== 1 && n.nodeType !== 9 && n.nodeType !== 11) return;
    if (n.nodeType === 1) {
      const tag = String(n.tagName || '').toUpperCase();
      if (n === o.skip || ANC_SKIP_TAGS.indexOf(tag) >= 0 || ancIsSeenItNode(n)) return;
      if (o.hidden && o.hidden(n)) return;
      if (tag === 'BR') {
        parts.push('\n');
        return;
      }
      const block = o.block ? o.block(n, tag) : ANC_BLOCK_TAGS.indexOf(tag) >= 0;
      if (block) parts.push('\n');
      for (const c of n.childNodes || []) walk(c);
      if (block) parts.push('\n');
      return;
    }
    for (const c of n.childNodes || []) walk(c);
  })(node);
  return parts.join('')
    .split('\n')
    .map((l) => l.replace(/[ \t ]+/g, ' ').trim())
    .filter(Boolean)
    .join('\n');
}

function ancElements(root, pred, out) {
  const list = out || [];
  for (const c of (root && root.childNodes) || []) {
    if (c.nodeType !== 1) continue;
    if (pred(c)) list.push(c);
    ancElements(c, pred, list);
  }
  return list;
}

function ancTag(el) {
  return String(el.tagName || '').toUpperCase();
}

function ancById(root, id) {
  return ancElements(root, (e) => e.id === id)[0] || null;
}

// A table's own rows and cells - never those of a table nested inside it.
function ancTableRows(table, text) {
  const rows = [];
  (function walk(n) {
    for (const c of n.childNodes || []) {
      if (c.nodeType !== 1) continue;
      const tag = ancTag(c);
      if (tag === 'TABLE') continue;
      if (tag === 'TR') {
        const cells = (c.childNodes ? [...c.childNodes] : [])
          .filter((td) => td.nodeType === 1 && (ancTag(td) === 'TD' || ancTag(td) === 'TH'))
          .map((td) => text(td).replace(/\n+/g, ' '));
        if (cells.some(Boolean)) rows.push(cells);
      } else {
        walk(c);
      }
    }
  })(table);
  return rows;
}

// The tree-hint prompt Ancestry can put in an <h1>; it is not the record.
const ANC_HINT_RE = /match the person in your tree|^does\b.+\?$/i;

// The site's name at the end of a tab title, on every Ancestry site and in
// both forms it uses: "… - Ancestry.com", "… - Ancestry.co.uk", "… | Ancestry".
const ANC_TITLE_SUFFIX_RE = /\s*[-|\u2013]\s*Ancestry(\.com)?(\.[a-z.]+)?\s*$/i;

// The whole record from a page (`root`: the document, or a stand-in tree):
// { ok, record: {dbId, recordId, imageId, url, name, collection}, fields:
// [{label, value}], household: [[cells]...] per table, citation: {heading:
// text} }. `opts.title` is the document title; `opts.origin` the site origin;
// `opts.hidden` / `opts.block` are ancText's (the live page passes its CSS).
function ancRecordFrom(root, href, opts) {
  const o = opts || {};
  const id = ancParseRecordPage(href);
  if (!id) return { ok: false, error: 'this is not an Ancestry record page' };
  const text = (el, skip) => (el ? ancText(el, { hidden: o.hidden, block: o.block, skip }) : '');

  const fields = [];
  const box = ancById(root, 'recordFieldsContainer') || ancById(root, 'recordData');
  const dl = box ? ancElements(box, (e) => ancTag(e) === 'DL')[0] : null;
  if (dl) {
    let cur = null;
    for (const c of dl.childNodes || []) {
      if (c.nodeType !== 1 || ancIsSeenItNode(c)) continue;
      if (ancTag(c) === 'DT') {
        cur = { label: text(c), value: '' };
        fields.push(cur);
      } else if (ancTag(c) === 'DD' && cur) {
        cur.value = text(c);
      }
    }
  }

  // The household: every table under #recordData that holds rows of its own
  // (the site wraps the household table in a layout table; that wrapper's
  // rows are only the inner table again, so tables holding tables are passed).
  const household = [];
  const data = ancById(root, 'recordData');
  if (data) {
    for (const table of ancElements(data, (e) => ancTag(e) === 'TABLE')) {
      if (ancElements(table, (e) => ancTag(e) === 'TABLE').length) continue;
      const rows = ancTableRows(table, text);
      if (rows.length) household.push(rows);
    }
  }

  const citation = {};
  const sc = ancById(root, 'sourceCitation');
  if (sc) {
    for (const sec of ancElements(sc, (e) => ancTag(e) === 'SECTION')) {
      const h = ancElements(sec, (e) => ancTag(e) === 'H3')[0];
      if (!h || ancElements(sec, (e) => ancTag(e) === 'SECTION').length) continue;
      const heading = text(h);
      const body = text(sec, h);
      if (heading && body) citation[heading] = body;
    }
  }

  let imageId = null;
  for (const a of ancElements(root, (e) => ancTag(e) === 'A')) {
    if (ancIsSeenItNode(a)) continue;
    imageId = ancImageFromHref(a.getAttribute ? a.getAttribute('href') : '', id.dbId);
    if (imageId) break;
  }

  const h1 = ancElements(root, (e) => ancTag(e) === 'H1').map((e) => text(e)).find((t) => t && !ANC_HINT_RE.test(t));
  const collection = String(o.title || '').replace(ANC_TITLE_SUFFIX_RE, '').trim();
  const origin = o.origin || 'https://www.ancestry.com';
  return {
    ok: true,
    record: {
      dbId: id.dbId,
      recordId: id.recordId,
      imageId,
      url: origin + '/search/collections/' + id.dbId + '/records/' + id.recordId,
      name: (h1 && h1.split('\n')[0]) || null,
      collection: collection || null,
    },
    fields,
    household,
    citation,
  };
}

const ANC_MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, sept: 9, oct: 10, nov: 11, dec: 12 };

function ancMonth(word) {
  const w = String(word || '').toLowerCase().replace(/\.$/, '');
  const key = w === 'sept' ? 'sept' : w.slice(0, 3);
  const n = ANC_MONTHS[key];
  if (!n) return null;
  // A full name must really be that month ("Marchant" is not March).
  const full = ['january', 'february', 'march', 'april', 'may', 'june', 'july', 'august',
    'september', 'october', 'november', 'december'][n - 1];
  return w.length <= 4 || full === w ? n : null;
}

const pad2 = (n) => (n < 10 ? '0' : '') + n;

// An Ancestry index date in the archive's form (EDTF), or null when it is not
// one of the plain shapes: "7 Jan 1950" -> "1950-01-07", "Jan 1950" ->
// "1950-01", "1950" -> "1950", and "abt 1845" (about) -> "1845~". Anything
// else - "bef 1900", a range, a place by mistake - is null: no date is better
// than a wrong one, and the reviewer adds what the record supports.
function ancDateToEdtf(value) {
  let v = String(value || '').trim().replace(/\s+/g, ' ');
  let approx = '';
  const a = /^(?:abt\.?|about|circa|ca\.?|c\.)\s+(.+)$/i.exec(v);
  if (a) {
    v = a[1];
    approx = '~';
  }
  let m = /^(\d{1,2}) ([A-Za-z]{3,9}\.?),? (\d{4})$/.exec(v);
  if (m) {
    const mo = ancMonth(m[2]);
    const d = Number(m[1]);
    return mo && d >= 1 && d <= 31 ? m[3] + '-' + pad2(mo) + '-' + pad2(d) + approx : null;
  }
  m = /^([A-Za-z]{3,9}\.?) (\d{1,2}),? (\d{4})$/.exec(v);
  if (m) {
    const mo = ancMonth(m[1]);
    const d = Number(m[2]);
    return mo && d >= 1 && d <= 31 ? m[3] + '-' + pad2(mo) + '-' + pad2(d) + approx : null;
  }
  m = /^([A-Za-z]{3,9}\.?) (\d{4})$/.exec(v);
  if (m) {
    const mo = ancMonth(m[1]);
    return mo ? m[2] + '-' + pad2(mo) + approx : null;
  }
  m = /^(\d{4})$/.exec(v);
  return m ? m[1] + approx : null;
}

// The event a collection is about, as the words its date fields start with,
// in the order to try them. A burial, cemetery or funeral-home record often
// gives only the death date; an obituary's event is the death.
const ANC_EVENTS = [
  [/\bdeaths?\b|\bdied\b|\bmortality\b/i, ['Death']],
  [/\bburials?\b|\bgraves?\b|\bcemeter(y|ies)\b|\bfunerals?\b/i, ['Burial', 'Death']],
  [/\bmarriages?\b|\bmarried\b/i, ['Marriage']],
  [/\bbirths?\b|\bborn\b/i, ['Birth']],
  [/\bbaptisms?\b/i, ['Baptism']],
  [/\bchristenings?\b/i, ['Christening']],
  [/\bdivorces?\b/i, ['Divorce']],
];
const ANC_OBITUARY_RE = /\bobituar(y|ies)\b/i;

// A collection that runs over years - "1940-1997", "1930-Current",
// "1800s-present", "1847-Current" - names when it was gathered, not when the
// record's event happened, so its years never date the record.
const ANC_RANGE_RE = /\b\d{4}s?\s*[-\u2013/]\s*(current|present|\d{2,4}s?)\b/i;

// A Find a Grave memorial, or an index of them ("U.S., Find a Grave Index"):
// a volunteer's page built from the stone, records and family knowledge. It
// dates like a burial record but is a website, not a vital record - filed as
// one, its claims would default to high confidence (SPEC §8.5) on a
// stranger's transcription.
const ANC_FINDAGRAVE_RE = /\bfind\s*a\s*grave\b/i;

// What a record says about itself, for filing (TI-066), on any site: from
// the collection name and `dateOf(event)` - the record's own date for "Death",
// "Marriage", "Census"… as EDTF, or null - never from the rest of the page:
//   sourceType  census for a census; website for Find a Grave; vital-record
//               for a birth, marriage, death, burial, baptism or divorce
//               collection; newspaper for an obituary or newspaper index;
//               else null (let filing decide);
//   sourceDate  the record's own event date ("Death Date" in a death index;
//               "Burial Date", else "Death Date", for a burial, cemetery or
//               funeral record; "Death Date" for an obituary), else a census's
//               year, else the collection's one year when it names exactly one;
//               never a year of a range, closed or open ("Death Index,
//               1940-1997" or "Obituary Collection, 1930-Current" says when
//               the collection runs, not when she died);
//   sourceClass 'derivative' for an index (a compiled list, not the record
//               itself) or Find a Grave, else null (filing's default, original).
function recordHints(collection, dateOf) {
  const c = String(collection || '');
  const ask = typeof dateOf === 'function' ? dateOf : () => null;
  const census = /\bcensus\b/i.test(c);
  // Every event the collection names, in the order ANC_EVENTS gives them:
  // a "Birth and Death Records" entry is a death record when it has a death
  // date, else a birth record.
  const events = [];
  if (!census) {
    for (const [re, words] of ANC_EVENTS) {
      if (re.test(c)) for (const w of words) if (events.indexOf(w) < 0) events.push(w);
    }
  }
  const dateWords = census ? ['Census', 'Residence']
    : events.length ? events : ANC_OBITUARY_RE.test(c) ? ['Death'] : [];

  const findagrave = ANC_FINDAGRAVE_RE.test(c);
  let sourceType = null;
  if (census) sourceType = 'census';
  else if (findagrave) sourceType = 'website';
  else if (events.length) sourceType = 'vital-record';
  else if (/\bnewspapers?\b|\bobituar(y|ies)\b/i.test(c)) sourceType = 'newspaper';

  let sourceDate = null;
  for (const word of dateWords) {
    sourceDate = ask(word) || null;
    if (sourceDate) break;
  }
  const years = c.match(/\b(1[5-9]\d{2}|20\d{2})\b/g) || [];
  const isRange = ANC_RANGE_RE.test(c);
  if (!sourceDate && census) {
    const lead = /^\s*(\d{4})\b/.exec(c);
    if (lead) sourceDate = lead[1];
    else if (years.length === 1) sourceDate = years[0];
  }
  if (!sourceDate && !census && years.length === 1 && !isRange) sourceDate = years[0];

  const sourceClass = findagrave || /\bindex(es)?\b/i.test(c) ? 'derivative' : null;
  return { sourceType, sourceDate, sourceClass };
}

// ancHints for an Ancestry record: its labelled fields answer "{Event} Date"
// (or "{Event} Year") in Ancestry's own date words.
function ancHints(record, fields) {
  const list = Array.isArray(fields) ? fields : [];
  return recordHints((record && record.collection) || '', (word) => {
    const re = new RegExp('^' + word + ' (date|year)$', 'i');
    const f = list.find((x) => re.test(String(x.label || '').trim()));
    return f ? ancDateToEdtf(f.value) : null;
  });
}

// The bundle's title from the form's UNTOUCHED prefill (a title she typed
// never comes here - the panel uses it as typed, PR #214 review): that title,
// unless the form still holds the tab's own title ("1920 United States Federal Census - Ancestry.com", which
// names only the collection; on any Ancestry site, ANC_TITLE_SUFFIX_RE) or
// nothing; then "{name} - {collection}".
function ancTitle(record, formTitle) {
  const r = record || {};
  const auto = [r.name, r.collection].filter(Boolean).join(' - ') ||
    'Ancestry record ' + r.dbId + ':' + r.recordId;
  const typed = String(formTitle || '').trim();
  return !typed || ANC_TITLE_SUFFIX_RE.test(typed) ? auto : typed;
}

// ── shared photos (the media viewer) ──
// A photo or story another member shared to their tree opens at
// /mediaui-viewer/tree/{treeId}/person/{personId}/media/{mediaId}, with
// query parameters (destTreeId, hid, …) that only say how she got there.
// The media id - a GUID - is the photo's identity: the same in every tree
// that saved it. Seen It keys it med:{mediaId}, lowercased
// (seen-it/host/seenit_host.py), from the address this returns as `url`:
// the viewer's own tree/person/media path, without the query.
const ANC_GUID = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
const ANC_MEDIA_PAGE_RE = new RegExp('^/mediaui-viewer/((?:[^/?#]+/)*?)media/(' + ANC_GUID + ')/?$', 'i');

// The shared photo a viewer address names: { mediaId, treeId, personId, url },
// or null for anything else.
function ancParseMediaPage(href) {
  let u;
  try {
    u = new URL(href);
  } catch (e) {
    return null;
  }
  if (!/(^|\.)ancestry\.[a-z.]+$/i.test(u.hostname)) return null;
  const m = ANC_MEDIA_PAGE_RE.exec(u.pathname);
  if (!m) return null;
  const mediaId = m[2].toLowerCase();
  const tree = /(?:^|\/)tree\/(\d+)\//.exec(m[1]);
  const person = /(?:^|\/)person\/(\d+)\//.exec(m[1]);
  return {
    mediaId,
    treeId: tree ? tree[1] : null,
    personId: person ? person[1] : null,
    url: u.origin + '/mediaui-viewer/' + m[1] + 'media/' + mediaId,
  };
}

// The photo's image as the viewer shows it: Ancestry's media service at
// /api/media/retrieval/v2/image/namespaces/{ns}/media/{mediaId}, asked for
// its "hq" size (1200x1920 on the page this was built from). The page's
// thumbnails of the same photo (…/media/{mediaId}.jpg?MaxSide=…) name the
// namespace too, so either will do; another photo's image never does. The
// address is always on the page's own site (`origin`), so the signed-in
// session the fetch carries goes nowhere else. null while no image of THIS
// photo is on the page. Whether Ancestry keeps a larger original behind the
// page's own Download menu is not known; this is the size the page shows.
const ANC_MEDIA_IMG_RE = new RegExp('/api/media/retrieval/v2/image/namespaces/(\\d+)/media/(' + ANC_GUID +
  ')(\\.jpg)?(?=[?#]|$)', 'i');

function ancMediaImageUrl(root, mediaId, origin) {
  const want = String(mediaId || '').toLowerCase();
  if (!want) return null;
  let full = null; // the viewer's own picture names the namespace best
  let thumb = null;
  for (const img of ancElements(root, (e) => ancTag(e) === 'IMG')) {
    if (ancIsSeenItNode(img) || !img.getAttribute) continue;
    for (const attr of ['src', 'data-src']) {
      const m = ANC_MEDIA_IMG_RE.exec(String(img.getAttribute(attr) || ''));
      if (!m || m[2].toLowerCase() !== want) continue;
      if (m[3]) thumb = thumb || m[1];
      else full = full || m[1];
    }
  }
  const ns = full || thumb;
  if (!ns) return null;
  return String(origin || 'https://www.ancestry.com') + '/api/media/retrieval/v2/image/namespaces/' + ns +
    '/media/' + want + '?client=trees-mediaservice&imageQuality=hq';
}

// The labels of the photo's Details panel, as the page shows them.
const ANC_MEDIA_LABELS = [
  ['title', /^title$/i],
  ['date', /^(date|date taken)$/i],
  ['place', /^(place|location)$/i],
  ['description', /^description$/i],
  ['originallyAddedBy', /^(originally )?(added|uploaded|shared) by$/i],
  ['linkedTo', /^(linked|attached) to$/i],
];
// The lists of OTHER members a photo page shows - who saved it, and their
// comments. Never kept: their names are not the family's. Recognised only
// where they are shaped like a heading or a label (ancMediaLabel), never
// from words in a sentence: "Saved by Aunt Ethel from the 1938 farmhouse
// fire" in a description is the family's story, and is kept.
//   "Saved by", "Saved by 2 members", "Saved by (2)", "Also saved by",
//   "Saved to 3 trees", "Members who saved this", "Comments (4)"
const ANC_MEDIA_SAVED_HEADING_RE = /^((also\s+)?saved\s+(by|to)|(\d+\s+)?(other\s+)?(members?|people|users)\s+(who|that)\s+(also\s+)?saved(\s+this(\s+photo)?)?)(\s*\(?\d+\)?)?(\s+(other\s+)?(members?|people|users|others|trees?))?$/i;
const ANC_MEDIA_COMMENTS_RE = /^comments?(\s*\(?\d+\)?)?$/i;
// The one-line form, the names in links: "Saved by <a>name</a> and 3 others".
const ANC_MEDIA_SAVED_INLINE_RE = /^(also\s+)?saved\s+by(\s*,|\s+and\s+\d+\s+others?|\s+and)*$/i;
// ...and in plain text, a member's username (one word with a digit, "_" or
// ".") or one name and "and N others": "Saved by cousin_member_one". Never a
// sentence: "Saved by Aunt Ethel from the 1938 farmhouse fire." is a story.
const ANC_MEDIA_SAVED_NAMED_RE = /^(also\s+)?saved\s+by\s+([a-z0-9._-]*[0-9_.][a-z0-9._-]*(\s*,\s*[a-z0-9._-]+)*(\s+and\s+\d+\s+others?)?|[^\s,.]+\s+and\s+\d+\s+others?)$/i;

// The one-line form of a members list - its names in links, or a username.
function ancMediaSavedInline(el, own) {
  return (ANC_MEDIA_SAVED_INLINE_RE.test(own) && ancElements(el, (e) => ancTag(e) === 'A').length > 0) ||
    ANC_MEDIA_SAVED_NAMED_RE.test(own);
}
const ANC_MEDIA_HEADING_TAGS = ['H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'DT', 'TH', 'LEGEND', 'LABEL', 'SUMMARY'];
// "Originally added by hartley_genealogy on 3 Mar 2015", on one line.
const ANC_MEDIA_INLINE_RE = /^((?:originally )?(?:added|uploaded|shared) by)\s*:?\s+(.+)$/i;
const ANC_MEDIA_INLINE_START_RE = /^((?:originally )?(?:added|uploaded|shared) by)\b/i;

// The text an element holds directly (not in its child elements). A live
// NodeList has no filter, so the children are copied first.
function ancOwnText(el) {
  return [...(el.childNodes || [])].filter((c) => c.nodeType === 3)
    .map((c) => String(c.nodeValue || '')).join(' ').replace(/\s+/g, ' ').trim();
}

// Which Details label an element is - its own words, a colon allowed - or
// null: a key of ANC_MEDIA_LABELS, or 'savedBy' / 'comments' for the lists
// of other members. A members list is a heading element starting "Saved
// by", a label in one of the forms above, or the one-line form whose names
// are links - never a paragraph of words.
function ancMediaLabel(el) {
  const own = ancOwnText(el).replace(/\s*:$/, '');
  if (!own) return null;
  const heading = ANC_MEDIA_HEADING_TAGS.indexOf(ancTag(el)) >= 0;
  if (ANC_MEDIA_SAVED_HEADING_RE.test(own) || (heading && /^(also\s+)?saved\s+(by|to)\b/i.test(own))) return 'savedBy';
  if (ancMediaSavedInline(el, own)) return 'savedBy';
  if (ANC_MEDIA_COMMENTS_RE.test(own)) return 'comments';
  for (const [key, re] of ANC_MEDIA_LABELS) if (re.test(own)) return key;
  return null;
}

// Where a label's value is: what the label holds besides its own words, else
// what follows it - its next siblings, up to the next label - or, for a
// label alone in its box, what follows that box. { box, nodes, text }: `box`
// is the label or the box it sits alone in, `nodes` the siblings after it
// that hold the value. `text(n)` reads a node.
function ancMediaValue(el, text) {
  const tidy = (parts) => parts.join('\n').split('\n').map((l) => l.replace(/\s+/g, ' ').trim())
    .filter(Boolean).join('\n');
  const holdsLabel = (n) => n.nodeType === 1 && (!!ancMediaLabel(n) || ancElements(n, ancMediaLabel).length > 0);
  const inside = tidy([...(el.childNodes || [])].filter((c) => c.nodeType === 1).map((c) => text(c)));
  if (inside) return { box: el, nodes: [], text: inside };
  let node = el;
  for (let up = 0; up < 3 && node && node.parentNode; up++) {
    const sibs = [...(node.parentNode.childNodes || [])]; // a live NodeList has no indexOf
    const nodes = [];
    for (let i = sibs.indexOf(node) + 1; i < sibs.length; i++) {
      if (holdsLabel(sibs[i])) break;
      nodes.push(sibs[i]);
    }
    const v = tidy(nodes.map((s) => (s.nodeType === 3 ? String(s.nodeValue || '') : s.nodeType === 1 ? text(s) : '')));
    if (v) return { box: node, nodes, text: v };
    node = node.parentNode; // alone in its box: read on after the box
  }
  return { box: el, nodes: [], text: '' };
}

// The photo's own Details box: an element whose own label says "Details" -
// its aria-label, or the first heading inside it. Read first, so a label
// elsewhere on the page (a search form's "Location") never stands in for
// it. A class or id with "detail" in it is not enough: a linked person's
// "person-details" card is not the photo's (PR #258 review).
function ancMediaDetailsBoxes(root) {
  const DETAILS = /^(photo\s+|media\s+)?details$/i;
  const named = (e) => {
    const aria = e.getAttribute ? String(e.getAttribute('aria-label') || '').trim() : '';
    if (DETAILS.test(aria)) return true;
    const h = ancElements(e, (x) => /^H[1-6]$/.test(ancTag(x)))[0];
    return !!h && DETAILS.test(ancOwnText(h));
  };
  return ancElements(root, (e) => ['ASIDE', 'SECTION', 'DIV', 'ARTICLE', 'DIALOG', 'FORM'].indexOf(ancTag(e)) >= 0 &&
    named(e) && ancElements(e, ancMediaLabel).length > 0);
}

// The photo's Details as the page shows them: { title, date, place,
// description, originallyAddedBy, linkedTo: [names] } - each only when the
// page shows it (the Details panel may be closed until she opens it). A
// label's value is ancMediaValue's. A label claims its key only with a value,
// the photo's own Details box first, then the rest of the page; the first
// such value wins. `opts.hidden` / `opts.block` are ancText's (the live page
// passes its CSS).
function ancMediaDetails(root, opts) {
  const o = opts || {};
  const text = (n) => ancText(n, { hidden: o.hidden, block: o.block });
  const shown = (e) => {
    for (let n = e; n && n.nodeType === 1; n = n.parentNode) if (o.hidden && o.hidden(n)) return false;
    return true;
  };
  const found = {};
  const claim = (key, value) => {
    if (value && !(key in found)) found[key] = value;
  };
  const walk = (n) => {
    for (const c of (n && n.childNodes) || []) {
      if (c.nodeType !== 1) continue;
      if (ANC_SKIP_TAGS.indexOf(ancTag(c)) >= 0 || ancIsSeenItNode(c)) continue;
      if (o.hidden && o.hidden(c)) continue;
      const key = ancMediaLabel(c);
      if (key) {
        claim(key, ancMediaValue(c, text).text);
        continue;
      }
      // "Originally added by hartley_genealogy", in the one element whose own
      // words start with it (the name may sit in a link inside).
      const inline = ANC_MEDIA_INLINE_START_RE.test(ancOwnText(c))
        ? ANC_MEDIA_INLINE_RE.exec(text(c).split('\n')[0]) : null;
      if (inline) {
        claim('originallyAddedBy', inline[2].trim());
        continue;
      }
      walk(c);
    }
  };
  for (const box of ancMediaDetailsBoxes(root)) if (shown(box)) walk(box);
  walk(root);
  const details = {};
  for (const [key] of ANC_MEDIA_LABELS) {
    // "Saved by" and comments are never kept: other members' names.
    if (!found[key]) continue;
    if (key === 'linkedTo') {
      const names = ancLinkedNames(found[key]);
      if (names.length) details.linkedTo = names;
    } else {
      details[key] = key === 'description' ? found[key] : found[key].replace(/\n+/g, ' ');
    }
  }
  return details;
}

// Where a "Saved by" section ends, for ancMediaMemberNodes: a heading or a
// label, or a box of its own (a section, a story, a note on the back).
const ANC_MEDIA_SECTION_TAGS = ['SECTION', 'ASIDE', 'ARTICLE', 'FIGURE', 'FOOTER', 'HEADER', 'NAV', 'MAIN', 'FORM',
  'BODY', 'DIALOG', 'DETAILS'];
const ANC_MEDIA_LIST_TAGS = ['UL', 'OL', 'DL', 'TABLE'];

// The parts of a photo page (or of a copy of it) that list the other members
// who saved the photo, for the saved copy of the page to leave out: each
// "Saved by" heading and every member row after it, up to the next heading,
// label or box of its own - never past the heading's own section. A list
// after the heading is taken whole and ends the section (an empty one, a list
// not loaded yet, is taken alone); single rows are short lines, one name
// each, so a paragraph after them - a story, a note from the back of the
// photo - is never a row. The one-line form ("Saved by <a>name</a> and 3
// others") goes whole. Hidden or not: a closed Details panel is still in the
// copy.
function ancMediaMemberNodes(root) {
  const out = [];
  const text = (n) => ancText(n, {});
  const boundary = (n) => n.nodeType === 1 && (ANC_MEDIA_SECTION_TAGS.indexOf(ancTag(n)) >= 0 ||
    ANC_MEDIA_HEADING_TAGS.indexOf(ancTag(n)) >= 0 || !!ancMediaLabel(n) ||
    ancElements(n, (e) => !!ancMediaLabel(e) || /^H[1-6]$/.test(ancTag(e))).length > 0);
  const row = (n) => {
    const t = text(n);
    return t.indexOf('\n') < 0 && t.length <= 60 && !/[.!?]["')”]?$/.test(t);
  };
  const rowsAfter = (node) => {
    const sibs = [...(node.parentNode.childNodes || [])];
    const rows = [];
    for (let i = sibs.indexOf(node) + 1; i < sibs.length; i++) {
      const s = sibs[i];
      if (s.nodeType === 3) {
        if (String(s.nodeValue || '').trim()) break; // words in the box itself: not a list of names
        rows.push(s);
        continue;
      }
      if (s.nodeType !== 1 || boundary(s)) break;
      if (ANC_MEDIA_LIST_TAGS.indexOf(ancTag(s)) >= 0) {
        rows.push(s);
        break;
      }
      if (!row(s)) break;
      rows.push(s);
    }
    return rows;
  };
  (function walk(n) {
    for (const c of (n && n.childNodes) || []) {
      if (c.nodeType !== 1) continue;
      if (ancMediaLabel(c) !== 'savedBy') {
        walk(c);
        continue;
      }
      const own = ancOwnText(c).replace(/\s*:$/, '');
      if (ancMediaSavedInline(c, own) || !c.parentNode) {
        out.push(c); // "Saved by <a>cousin_member_one</a> and 3 others"
        continue;
      }
      // The heading alone in a box that is not a section of its own (a
      // <div><span>Saved by</span></div>): the rows follow the box.
      let head = c;
      const meaningful = (p) => [...(p.childNodes || [])].filter((x) => x.nodeType === 1 ||
        (x.nodeType === 3 && String(x.nodeValue || '').trim()));
      while (head.parentNode && head.parentNode.parentNode && head.parentNode.nodeType === 1 &&
        ANC_MEDIA_SECTION_TAGS.indexOf(ancTag(head.parentNode)) < 0 &&
        meaningful(head.parentNode).length === 1 && !rowsAfter(head).length) {
        head = head.parentNode;
      }
      out.push(head);
      for (const r of rowsAfter(head)) out.push(r);
    }
  })(root);
  return out;
}

// The people a shared photo is linked to, as names: one per line, without
// the years Ancestry puts beside them ("Thomas Hartley 1840–1910", "abt
// 1840–1910"); a living person Ancestry shows as "Private" or "Living …" is
// no name.
function ancLinkedNames(value) {
  const names = [];
  for (const line of String(value || '').split(/\n|;/)) {
    const n = line
      .replace(/\(?\s*((abt|about|bef|aft|circa|ca|c)\.?\s+)?(\d{4}|\?{1,4})?\s*[-–—]\s*((abt|about|bef|aft|circa|ca|c)\.?\s+)?(\d{4}|\?{1,4})?\s*\)?\s*$/i, '')
      .replace(/\s+/g, ' ').trim();
    if (n.length >= 2 && n.length <= 80 && !/^(private|living)\b/i.test(n) && names.indexOf(n) < 0) names.push(n);
  }
  return names;
}

// The whole shared photo from its viewer page (`root`: the document, or a
// stand-in tree): { ok, media: { mediaId, treeId, personId, url, title,
// imageUrl, fileStem }, details }. `opts.title` is the document title (the photo's
// title, as whoever shared it wrote it); `opts.origin` the site origin.
function ancMediaFrom(root, href, opts) {
  const o = opts || {};
  const id = ancParseMediaPage(href);
  if (!id) return { ok: false, error: 'this is not a photo shared on Ancestry' };
  const details = ancMediaDetails(root, o);
  const docTitle = String(o.title || '').replace(ANC_TITLE_SUFFIX_RE, '').trim();
  let origin = 'https://www.ancestry.com';
  try {
    origin = new URL(href).origin;
  } catch (e) {
    /* parsed above */
  }
  return {
    ok: true,
    media: Object.assign({}, id, {
      title: details.title || docTitle || null,
      imageUrl: ancMediaImageUrl(root, id.mediaId, o.origin || origin),
      fileStem: ancPhotoFileStem(id.mediaId),
    }),
    details,
  };
}

// What a shared photo says about itself, for filing: a photo, dated by the
// date its Details give when that is one the archive can read ("1925",
// "abt 1925", "12 Jun 1925"), else no date - never a guess.
function ancMediaHints(details) {
  return { sourceType: 'photo', sourceDate: ancDateToEdtf((details || {}).date) || null, sourceClass: null };
}

// The name the photo is filed under, without its extension. It lands in the
// photo library and is never renamed there (SPEC §12.1), so it is unique to
// the photo - its media id, letters, digits and dashes only, never the
// free-text title - and ends in a word, never in what the archive reads as a
// print letter ("…6f") or a back ("…-back"). null for anything not a media id.
function ancPhotoFileStem(mediaId) {
  const id = String(mediaId || '').toLowerCase();
  return new RegExp('^' + ANC_GUID + '$').test(id) ? 'ancestry-' + id + '-photo' : null;
}
// FHA-SYNC-END ancestry

module.exports = {
  ANC_SEEN_IT_CLASSES,
  ancParseRecordPage,
  ancImageFromHref,
  ancIsSeenItNode,
  ancText,
  ancElements,
  ancById,
  ancTag,
  ancRecordFrom,
  ancDateToEdtf,
  recordHints,
  ancHints,
  ancTitle,
  ancParseMediaPage,
  ancMediaImageUrl,
  ancMediaDetails,
  ancLinkedNames,
  ancMediaMemberNodes,
  ancMediaLabel,
  ancMediaFrom,
  ancMediaHints,
  ancPhotoFileStem,
};
