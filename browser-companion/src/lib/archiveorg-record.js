// archiveorg-record.js - capturing a page of a book on the Internet Archive:
// which page the address is, what the book is (title, author, publisher,
// date), the printed page number for the scan, the page image, the page's
// OCR text, and the citation that leads back to that exact page.
//
// The browser-side content.js DOES NOT import this file (it is an injected
// classic script that cannot use ES imports); it carries a copy of the block
// between `FHA-SYNC-BEGIN archiveorg` / `FHA-SYNC-END archiveorg`, and
// tests/test-sync.js fails if the two drift. This module is the canonical,
// tested reference - change it here first, then mirror the copy. The panel
// (panel.html) loads this same file as a classic script and finds it at
// `FHA.archiveorg`; the network functions below the block run there, in the
// extension page, because the book's files sit on other archive.org hosts.
//
// What archive.org serves (checked on a live item, 2026-11): a book is
// /details/{id}; BookReader writes /page/n{K} for the scan at position K
// counting from 0, and /page/{printed} for a printed page it knows.
//   /metadata/{id}                      the book's own description (JSON)
//   /download/{id}/{id}_page_numbers.json   scan number -> printed page
//   /download/{id}/{id}_hocr_pageindex.json.gz   byte range of each scan in
//   /download/{id}/{id}_hocr.html       the OCR, so one page's text is one
//                                       small range request, not the book
//   iiif.archive.org/iiif/{id}${N}/full/full/0/default.jpg   the scan, full size
// N, the page_numbers.json "leafNum" and the hOCR page number are all the
// same count from 1, so the scan at /page/n20 is leaf 21.
//
// NO LOGIN: every request goes without cookies (`credentials: 'omit'`). A
// lending-library book refuses its images without a borrow; that is said
// plainly and nothing is saved for it.
//
// Code map (inside the block): iaParsePage, iaCleanCreator, iaMetadataFrom,
// iaPageNumbersFrom, iaPrintedFor, iaLeafForPrinted, iaPageIndexRange,
// iaHocrText, iaImageUrl, iaPageUrl, iaCitation, iaTitle, iaHints.
// Below it: iaStatusMessage and the fetchers (iaFetchMetadata,
// iaFetchPageNumbers, iaFetchPageIndex, iaFetchLeafText, iaFetchImage).

'use strict';

// FHA-SYNC-BEGIN archiveorg
const IA_HOST_RE = /(^|\.)archive\.org$/i;
const IA_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$/;

// A book page: /details/{id}, with /page/{p} when a page is open. `p` is
// BookReader's "n{K}" (the scan at position K from 0) or a printed page
// ("11", "xii"). Returns { itemId, leaf (K, or null), printedPage (the
// segment, or null), segment (the page segment as the address wrote it, or
// null) }; null for any other address.
function iaParsePage(href, base) {
  let u;
  try {
    u = new URL(href, base);
  } catch (e) {
    return null;
  }
  if (u.protocol !== 'https:' || !/^(www\.)?archive\.org$/i.test(u.hostname)) return null;
  const m = /^\/details\/([^/?#]+)(?:\/page\/([^/?#]+))?/i.exec(u.pathname);
  if (!m) return null;
  let itemId;
  try {
    itemId = decodeURIComponent(m[1]);
  } catch (e) {
    return null;
  }
  if (!IA_ID_RE.test(itemId)) return null;
  let segment = null;
  if (m[2]) {
    try {
      segment = decodeURIComponent(m[2]);
    } catch (e) {
      segment = null;
    }
  }
  const n = segment ? /^n(\d+)$/i.exec(segment) : null;
  return {
    itemId,
    leaf: n ? parseInt(n[1], 10) : null,
    printedPage: segment && !n ? segment : null,
    segment: segment || null,
  };
}

// The author as archive.org's catalogue gives it ("[Mason, Jesse D] [from old
// catalog]", "Smith, John, 1840-1910") written as a citation names a person:
// "Jesse D. Mason". More than one author: the first two, "and"; more, "et al.".
function iaCleanCreator(raw) {
  const list = (Array.isArray(raw) ? raw : [raw]).map((x) => String(x == null ? '' : x)).filter((x) => x.trim());
  const names = [];
  for (const item of list) {
    let s = item.replace(/\[from old catalog\]/gi, ' ').replace(/[[\]]/g, ' ').replace(/\s+/g, ' ').trim();
    s = s.replace(/,?\s*\d{4}\??\s*-\s*(\d{4})?\??\.?$/, '').replace(/[.,;\s]+$/, '').trim();
    if (!s) continue;
    const parts = s.split(/\s*,\s*/);
    if (parts.length === 2 && parts[0] && parts[1] && !/\b(inc|co|company|society|committee|association)\b/i.test(s)) {
      const given = parts[1].replace(/\b([A-Z])\b(?!\.)/g, '$1.');
      s = given + ' ' + parts[0];
    }
    if (!names.includes(s)) names.push(s);
  }
  if (!names.length) return null;
  if (names.length === 1) return names[0];
  return names.length === 2 ? names[0] + ' and ' + names[1] : names[0] + ' et al.';
}

function iaFirst(v) {
  const x = Array.isArray(v) ? v[0] : v;
  const s = x == null ? '' : String(x).replace(/\s+/g, ' ').trim();
  return s || null;
}

// What /metadata/{id} says about a book: { ok, item: { itemId, title,
// creator, publisher, date, year, volume, imageCount, mediatype }, files:
// [{ name, size }] } or { ok:false, error }. An item that does not exist
// answers with an empty object.
function iaMetadataFrom(json, itemId) {
  const md = json && json.metadata;
  if (!md || typeof md !== 'object' || !Object.keys(md).length) {
    return { ok: false, error: 'the Internet Archive has no item called ' + itemId + ' (it may have been removed or renamed)' };
  }
  const date = iaFirst(md.date);
  // A range ("1890-1895") dates no single year, so it gives none.
  const isRange = /(?<!\d)\d{4}\s*[-/–]\s*\d{2,4}(?!\d)/.test(date || '');
  const year = isRange ? null : (/\b(1[0-9]{3}|20[0-9]{2})\b/.exec(date || '') || [])[1] || null;
  const count = parseInt(iaFirst(md.imagecount), 10);
  return {
    ok: true,
    item: {
      itemId,
      title: iaFirst(md.title),
      creator: iaCleanCreator(md.creator),
      publisher: iaFirst(md.publisher),
      date,
      year,
      volume: iaFirst(md.volume),
      imageCount: isNaN(count) ? null : count,
      mediatype: iaFirst(md.mediatype),
    },
    files: (Array.isArray(json.files) ? json.files : []).map((f) => ({ name: String(f.name || ''), size: Number(f.size) || 0 })),
  };
}

// {id}_page_numbers.json as a list of { leafNum, printed } (printed is null
// for a scan archive.org could not read a page number from).
function iaPageNumbersFrom(json) {
  const pages = json && Array.isArray(json.pages) ? json.pages : null;
  if (!pages) return null;
  return pages.map((p) => ({
    leafNum: Number(p.leafNum),
    printed: p.pageNumber == null || String(p.pageNumber).trim() === '' ? null : String(p.pageNumber).trim(),
  }));
}

// The printed page of the scan `leafNum` (counting from 1), or null.
function iaPrintedFor(pages, leafNum) {
  const p = (pages || []).find((x) => x.leafNum === leafNum);
  return p ? p.printed : null;
}

// The scan (leafNum) that carries printed page `printed`, or null. When
// the number appears twice (a plate and a page) the first one wins.
function iaLeafForPrinted(pages, printed) {
  const want = String(printed || '').trim().toLowerCase();
  if (!want) return null;
  const p = (pages || []).find((x) => x.printed && x.printed.toLowerCase() === want);
  return p ? p.leafNum : null;
}

// The byte range [start, end) of scan `leafNum` in {id}_hocr.html, from the
// parsed {id}_hocr_pageindex.json (entry 0 is the file's header, entry N the
// OCR of leaf N); null when the index has no usable entry.
function iaPageIndexRange(index, leafNum) {
  const e = Array.isArray(index) ? index[leafNum] : null;
  if (!Array.isArray(e) || e.length < 4) return null;
  const start = Number(e[2]);
  const end = Number(e[3]);
  return start >= 0 && end > start ? [start, end] : null;
}

function iaEntities(s) {
  return s.replace(/&(#x[0-9a-f]+|#\d+|amp|lt|gt|quot|apos);/gi, (all, k) => {
    const key = k.toLowerCase();
    if (key === 'amp') return '&';
    if (key === 'lt') return '<';
    if (key === 'gt') return '>';
    if (key === 'quot') return '"';
    if (key === 'apos') return '\'';
    const code = key[1] === 'x' ? parseInt(key.slice(2), 16) : parseInt(key.slice(1), 10);
    try {
      return String.fromCodePoint(code);
    } catch (e) {
      return all;
    }
  });
}

// One page of archive.org's hOCR as plain text: a line for each printed
// line, a blank line between paragraphs. Words stay as the OCR read them.
function iaHocrText(html) {
  const paras = String(html || '').split(/<p\b[^>]*class="[^"]*\bocr_par\b/);
  const out = [];
  for (const para of paras.slice(1)) {
    const lines = [];
    for (const line of para.split(/<span\b[^>]*class="[^"]*\bocr_line\b/).slice(1)) {
      const words = [];
      const re = /<span\b[^>]*class="[^"]*\bocrx_word\b[^"]*"[^>]*>([\s\S]*?)<\/span>/g;
      let w;
      while ((w = re.exec(line))) {
        const t = iaEntities(w[1].replace(/<[^>]*>/g, '')).replace(/\s+/g, ' ').trim();
        if (t) words.push(t);
      }
      if (words.length) lines.push(words.join(' '));
    }
    if (lines.length) out.push(lines.join('\n'));
  }
  return out.join('\n\n');
}

// The scan at `leafNum` (counting from 1) at full size, from archive.org's
// IIIF image service.
function iaImageUrl(itemId, leafNum) {
  return 'https://iiif.archive.org/iiif/' + itemId + '$' + leafNum + '/full/full/0/default.jpg';
}

// The address of a page, the way the page segment was written: n20 stays
// n20 and 11 stays 11.
function iaPageUrl(itemId, segment) {
  return 'https://archive.org/details/' + itemId + (segment ? '/page/' + encodeURIComponent(segment) : '');
}

function iaTitle(item) {
  const i = item || {};
  return (i.title || i.itemId || 'Internet Archive book') + (i.volume ? ', vol. ' + i.volume : '');
}

// The citation: "{author}, {title} ({publisher}, {date}), p. {printed};
// digital images, Internet Archive ({url} : accessed {date})." `pages`:
// [{ leaf (K), printed }] in order; `url`: the link to the first page, in the
// form the human was looking at. A scan whose printed page is not known is
// named by its place in the address (n20) and said so.
function iaCitation(item, pages, url, accessed) {
  const i = item || {};
  const who = i.creator ? i.creator + ', ' : '';
  const pub = [i.publisher, i.date].filter(Boolean).join(', ');
  const head = who + iaTitle(i) + (pub ? ' (' + pub + ')' : '');
  const list = pages || [];
  const known = list.length > 0 && list.every((p) => p.printed);
  let where;
  if (!list.length) where = '';
  else if (known) {
    const nums = list.map((p) => p.printed);
    where = (nums.length === 1 ? 'p. ' : 'pp. ') + (nums.length === 1 ? nums[0] : nums[0] + '-' + nums[nums.length - 1]);
  } else {
    const names = list.map((p) => (p.printed ? 'p. ' + p.printed : 'scan n' + p.leaf));
    where = names.join(', ') + ' (printed page not known)';
  }
  return head + (where ? ', ' + where : '') + '; digital images, Internet Archive (' + url + ' : accessed ' + accessed + ').';
}

// What the book says about itself for filing: a printed book, so `book`;
// a digitised copy of a published work is `derivative`; dated by its
// publication year when it names one.
function iaHints(item) {
  return { sourceType: 'book', sourceClass: 'derivative', sourceDate: (item && item.year) || null };
}
// FHA-SYNC-END archiveorg

// ── the panel side: requests ─────────────────────────────────────────────────
// Each takes `d` = { fetch } (the extension page's fetch; a test's stand-in)
// and sends no cookies and no referrer. Every answer is { ok, ... } or
// { ok:false, error, status?, slowDown?, transient? }.

const IA_OPTS = { credentials: 'omit', referrerPolicy: 'no-referrer' };

// What an HTTP answer means, in words that end in what to do.
// `what`: 'details', 'numbers', 'index', 'text' or 'image'.
function iaStatusMessage(status, what) {
  if (status === 429 || status === 503) {
    return 'the Internet Archive asked us to slow down (HTTP ' + status + '). Wait a few minutes, then press Capture again';
  }
  if (what === 'image' && (status === 401 || status === 403)) {
    return 'the Internet Archive does not show this book’s page images without borrowing it (HTTP ' + status +
      '). Borrow it on archive.org, then press Capture again, or save the page by hand and drop the file in';
  }
  if (status === 404 || status === 410) {
    return what === 'details'
      ? 'the Internet Archive has no item at this address (HTTP ' + status + '): it may have been removed or renamed'
      : 'the Internet Archive has no ' + (what === 'image' ? 'image' : 'file') + ' for this page (HTTP ' + status + ')';
  }
  if (status >= 500) return 'the Internet Archive is having trouble (HTTP ' + status + '). Try again in a little while';
  return 'the Internet Archive answered HTTP ' + status + ' for the ' + what + '. Reload the page and press Capture again';
}

function iaHostOk(url) {
  if (!url) return true; // a stand-in with no address: nothing to check
  try {
    const u = new URL(url);
    return u.protocol === 'https:' && IA_HOST_RE.test(u.hostname) && !u.username && !u.password;
  } catch (e) {
    return false;
  }
}

async function iaGet(url, d, what, extra) {
  let r;
  try {
    r = await d.fetch(url, Object.assign({}, IA_OPTS, extra || {}));
  } catch (e) {
    return { ok: false, transient: true, error: 'could not reach the Internet Archive - check your internet connection' };
  }
  if (!iaHostOk(r.url)) {
    return { ok: false, error: 'the answer came from outside archive.org, so it was not used' };
  }
  if (!r.ok && r.status !== 206) {
    return { ok: false, status: r.status, slowDown: r.status === 429 || r.status === 503,
      transient: r.status >= 500, error: iaStatusMessage(r.status, what) };
  }
  return { ok: true, r };
}

// The book's description and file list.
async function iaFetchMetadata(itemId, d) {
  if (!IA_ID_RE.test(String(itemId || ''))) return { ok: false, error: 'this is not an Internet Archive item address' };
  const got = await iaGet('https://archive.org/metadata/' + encodeURIComponent(itemId), d, 'details');
  if (!got.ok) return got;
  let json;
  try {
    json = await got.r.json();
  } catch (e) {
    return { ok: false, transient: true, error: 'the Internet Archive’s description of this book was not readable' };
  }
  const meta = iaMetadataFrom(json, itemId);
  if (!meta.ok) return meta;
  if (meta.item.mediatype && meta.item.mediatype !== 'texts') {
    return { ok: false, error: 'this Internet Archive item is not a book or other text (it is "' + meta.item.mediatype + '")' };
  }
  return meta;
}

function iaHasFile(files, name) {
  return (files || []).some((f) => f.name === name);
}

// scan -> printed page. null (ok) when the item has no such file: the
// printed page is then "not known" and the scan is named by its position.
async function iaFetchPageNumbers(itemId, files, d) {
  const name = itemId + '_page_numbers.json';
  if (!iaHasFile(files, name)) return { ok: true, pages: null };
  const got = await iaGet('https://archive.org/download/' + itemId + '/' + name, d, 'numbers');
  if (!got.ok) return got;
  try {
    return { ok: true, pages: iaPageNumbersFrom(await got.r.json()) };
  } catch (e) {
    return { ok: false, transient: true, error: 'the Internet Archive’s page-number list was not readable' };
  }
}

async function iaGunzip(buf) {
  const stream = new Blob([buf]).stream().pipeThrough(new DecompressionStream('gzip'));
  return new Response(stream).text();
}

// The OCR page index: { ok, index } or { ok:true, index:null } when the item
// has none.
async function iaFetchPageIndex(itemId, files, d) {
  const name = itemId + '_hocr_pageindex.json.gz';
  if (!iaHasFile(files, name) || !iaHasFile(files, itemId + '_hocr.html')) return { ok: true, index: null };
  const got = await iaGet('https://archive.org/download/' + itemId + '/' + name, d, 'index');
  if (!got.ok) return got;
  try {
    return { ok: true, index: JSON.parse(await (d.gunzip || iaGunzip)(await got.r.arrayBuffer())) };
  } catch (e) {
    return { ok: false, transient: true, error: 'the Internet Archive’s text index for this book was not readable' };
  }
}

// One scan's OCR text by a range request into the book's hOCR file - never
// the whole book. { ok, text } or { ok:false, error }. A server that ignores
// the range (answers 200 with the whole file) is not read.
async function iaFetchLeafText(itemId, index, leafNum, d) {
  const range = iaPageIndexRange(index, leafNum);
  if (!range) return { ok: false, error: 'the Internet Archive has no text for this page' };
  if (range[1] - range[0] > 3000000) return { ok: false, error: 'the page’s text is unusually large, so it was not fetched' };
  const got = await iaGet('https://archive.org/download/' + itemId + '/' + itemId + '_hocr.html', d, 'text',
    { headers: { Range: 'bytes=' + range[0] + '-' + (range[1] - 1) } });
  if (!got.ok) return got;
  if (got.r.status !== 206) {
    try {
      if (got.r.body && got.r.body.cancel) got.r.body.cancel();
    } catch (e) {
      /* nothing to cancel */
    }
    return { ok: false, error: 'the Internet Archive would not send just this page’s text, so the text was not saved' };
  }
  let html;
  try {
    html = await got.r.text();
  } catch (e) {
    return { ok: false, transient: true, error: 'the page’s text broke off while downloading' };
  }
  return { ok: true, text: iaHocrText(html) };
}

const IA_MIN_IMAGE = 30 * 1024;
const IA_EXT = { 'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp' };

// One scan at full size: { ok, blob, ext } or { ok:false, error }. The
// address is archive.org's own image service; redirects stay on archive.org;
// an answer that is not an image, or is smaller than a real page, is refused.
async function iaFetchImage(itemId, leafNum, d) {
  const url = iaImageUrl(itemId, leafNum);
  const got = await iaGet(url, d, 'image');
  if (!got.ok) return got;
  const type = String((got.r.headers && got.r.headers.get && got.r.headers.get('content-type')) || '')
    .split(';')[0].trim().toLowerCase();
  if (!/^image\//.test(type)) return { ok: false, error: 'the Internet Archive sent a page instead of the image' };
  let blob;
  try {
    blob = await got.r.blob();
  } catch (e) {
    return { ok: false, transient: true, error: 'the image broke off while downloading - press Capture again' };
  }
  if (blob.size < IA_MIN_IMAGE) {
    return { ok: false, error: 'the Internet Archive sent only a small picture (' + blob.size + ' bytes), not the full page' };
  }
  return { ok: true, blob, ext: IA_EXT[type] || 'jpg' };
}

const archiveorg = {
  iaParsePage, iaCleanCreator, iaMetadataFrom, iaPageNumbersFrom, iaPrintedFor, iaLeafForPrinted,
  iaPageIndexRange, iaHocrText, iaImageUrl, iaPageUrl, iaTitle, iaCitation, iaHints,
  iaStatusMessage, iaFetchMetadata, iaFetchPageNumbers, iaFetchPageIndex, iaFetchLeafText, iaFetchImage,
  IA_MAX_LEAVES: 4,
  // The pause between two page images, 0.8-2 s.
  pauseMs: () => 800 + Math.floor(Math.random() * 1200),
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = archiveorg;
} else if (typeof window !== 'undefined') {
  window.FHA = window.FHA || {};
  window.FHA.archiveorg = archiveorg;
}
