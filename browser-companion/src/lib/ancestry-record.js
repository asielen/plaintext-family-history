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
//   ancHints             source type, date and class from the record itself
//   ancTitle             "{name} - {collection}", unless the human typed a title

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

// What a record says about itself, for filing (TI-066). From the collection
// name and the record's own index fields - never from the rest of the page:
//   sourceType  census for a census; vital-record for a birth, marriage,
//               death, burial, baptism or divorce collection; newspaper for
//               an obituary or newspaper index; else null (let filing decide);
//   sourceDate  the record's own event date ("Death Date" in a death index;
//               "Burial Date", else "Death Date", for a burial, cemetery or
//               funeral record; "Death Date" for an obituary), else a census's
//               year, else the collection's one year when it names exactly one;
//               never a year of a range, closed or open ("Death Index,
//               1940-1997" or "Obituary Collection, 1930-Current" says when
//               the collection runs, not when she died);
//   sourceClass 'derivative' for an index (a compiled list, not the record
//               itself), else null (filing's default, original).
function ancHints(record, fields) {
  const collection = String((record && record.collection) || '');
  const list = Array.isArray(fields) ? fields : [];
  const census = /\bcensus\b/i.test(collection);
  const event = census ? null : (ANC_EVENTS.find(([re]) => re.test(collection)) || [])[1] || null;
  const dateWords = event || (!census && ANC_OBITUARY_RE.test(collection) ? ['Death'] : []);

  let sourceType = null;
  if (census) sourceType = 'census';
  else if (event) sourceType = 'vital-record';
  else if (/\bnewspapers?\b|\bobituar(y|ies)\b/i.test(collection)) sourceType = 'newspaper';

  let sourceDate = null;
  for (const word of dateWords) {
    const f = list.find((x) => new RegExp('^' + word + ' (date|year)$', 'i').test(String(x.label || '').trim()));
    sourceDate = f ? ancDateToEdtf(f.value) : null;
    if (sourceDate) break;
  }
  const years = collection.match(/\b(1[5-9]\d{2}|20\d{2})\b/g) || [];
  const isRange = ANC_RANGE_RE.test(collection);
  if (!sourceDate && census) {
    // A state census collection spans years; the record's own "Residence
    // Date" is the enumeration it is from.
    const f = list.find((x) => /^residence (date|year)$/i.test(String(x.label || '').trim()));
    if (f) sourceDate = ancDateToEdtf(f.value);
  }
  if (!sourceDate && census) {
    const lead = /^\s*(\d{4})\b/.exec(collection);
    if (lead) sourceDate = lead[1];
    else if (years.length === 1) sourceDate = years[0];
  }
  if (!sourceDate && !census && years.length === 1 && !isRange) sourceDate = years[0];

  const sourceClass = /\bindex(es)?\b/i.test(collection) ? 'derivative' : null;
  return { sourceType, sourceDate, sourceClass };
}

// The bundle's title: what the human typed, unless the form still holds the
// tab's own title ("1920 United States Federal Census - Ancestry.com", which
// names only the collection; on any Ancestry site, ANC_TITLE_SUFFIX_RE) or
// nothing; then "{name} - {collection}".
function ancTitle(record, formTitle) {
  const r = record || {};
  const auto = [r.name, r.collection].filter(Boolean).join(' - ') ||
    'Ancestry record ' + r.dbId + ':' + r.recordId;
  const typed = String(formTitle || '').trim();
  return !typed || ANC_TITLE_SUFFIX_RE.test(typed) ? auto : typed;
}
// FHA-SYNC-END ancestry

module.exports = {
  ANC_SEEN_IT_CLASSES,
  ancParseRecordPage,
  ancImageFromHref,
  ancIsSeenItNode,
  ancText,
  ancRecordFrom,
  ancDateToEdtf,
  ancHints,
  ancTitle,
};
