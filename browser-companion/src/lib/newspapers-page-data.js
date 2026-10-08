// newspapers-page-data.js - the pure, importable core of reading a
// Newspapers.com page's own embedded data (which clipping, which page, how big,
// who clipped it, the site's OCR), so a clipping can be saved at full size.
//
// The browser-side content.js DOES NOT import this file (it is an injected
// classic script that cannot use ES imports); it carries a copy of the block
// between `FHA-SYNC-BEGIN newspapers` / `FHA-SYNC-END newspapers`, and
// tests/test-sync.js drives both through the same battery. This module is the
// canonical, tested reference - change it here first, then mirror the copy.
//
// What the site exposes (verified from live pages, 2026-10-07/08):
//   - the page is a Next.js app; its data rides in `self.__next_f.push([1,"…"])`
//     script strings, which decode to text holding JSON objects;
//   - a clipping page (/article/{slug}/{id}/) carries a `"clipping":{…}` object
//     (id, page id, rectangles in page pixels, clipper, page size, a page pass);
//   - the page viewer (/image/{pageId}/) carries an `"image":{…}` object;
//   - a clipping's OCR is a `"$<id>"` pointer to a text chunk
//     `<id>:T<hex UTF-8 byte length>,<text>` elsewhere in the same data.

'use strict';

// FHA-SYNC-BEGIN newspapers
// Which kind of Newspapers.com address this is: a clipping page (`article`,
// also the older /clip/{id}/) or the page viewer (`image`); null otherwise.
function npPageKind(href) {
  let u;
  try {
    u = new URL(href);
  } catch (e) {
    return null;
  }
  if (!/(^|\.)newspapers\.com$/i.test(u.hostname)) return null;
  let m = u.pathname.match(/^\/article\/[^/]+\/(\d+)\/?/);
  if (m) return { kind: 'article', clippingId: Number(m[1]), pageId: null };
  m = u.pathname.match(/^\/(?:article|clip)\/(\d+)\/?/);
  if (m) return { kind: 'article', clippingId: Number(m[1]), pageId: null };
  m = u.pathname.match(/^\/image\/(\d+)\/?/);
  if (m) {
    return {
      kind: 'image',
      pageId: Number(m[1]),
      clippingId: Number(u.searchParams.get('clipping_id')) || null,
    };
  }
  return null;
}

// The site is a Next.js app: its data rides in `self.__next_f.push([1,"…"])`
// script strings. Decoding those strings yields plain JSON text to search.
function npFlightFrom(scriptTexts) {
  let out = '';
  const re = /self\.__next_f\.push\(\[\d+,("(?:[^"\\]|\\.)*")\]\)/g;
  for (const t of scriptTexts) {
    if (!t || t.indexOf('__next_f') < 0) continue;
    let m;
    re.lastIndex = 0;
    while ((m = re.exec(t))) {
      try {
        out += JSON.parse(m[1]);
      } catch (e) {
        /* skip a chunk that is not a plain string */
      }
    }
  }
  return out;
}

// The JSON object starting at text[start] (a '{'), parsed, or null. Braces
// inside strings do not count, so a title holding "}" cannot cut it short.
function npObjectAt(text, start) {
  let depth = 0;
  let inStr = false;
  for (let k = start; k < text.length; k++) {
    const c = text[k];
    if (inStr) {
      if (c === '\\') k++;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') inStr = true;
    else if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) {
        try {
          return JSON.parse(text.slice(start, k + 1));
        } catch (e) {
          return null;
        }
      }
    }
  }
  return null;
}

function npObjectAfter(text, key, from) {
  const needle = '"' + key + '":{';
  const i = text.indexOf(needle, from || 0);
  return i < 0 ? null : npObjectAt(text, i + needle.length - 1);
}

function npJwtPayload(token) {
  try {
    const part = String(token).split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
    return JSON.parse(atob(part + '==='.slice((part.length + 3) % 4)));
  } catch (e) {
    return null;
  }
}

// One clipping, from the page data or the clipping list, in the shape the
// panel uses. `toAbs` makes the site's relative clipping url absolute.
function npClip(c, toAbs) {
  const raw = Array.isArray(c.rectangles) && c.rectangles.length ? c.rectangles : [c.rectangle];
  const rects = raw
    .filter((r) => r && r.width > 0 && r.height > 0)
    .map((r) => ({
      x: Math.max(0, Math.round(r.x)),
      y: Math.max(0, Math.round(r.y)),
      w: Math.round(r.width),
      h: Math.round(r.height),
      rotation: r.rotation || 0,
    }));
  return {
    id: c.clipping_id,
    pageId: c.page_id,
    title: String(c.title || '').trim(),
    tags: c.tags || '',
    rects,
    userId: c.user_id,
    username: c.user_username || '',
    created: c.created || null,
    pageDate: c.page_date || null,
    pageNo: c.page_title || null,
    pageWidth: c.page_width || null,
    pageHeight: c.page_height || null,
    publication: c.publication
      ? { name: c.publication.name || null, location: c.publication.location || null }
      : null,
    url: c.url ? (toAbs ? toAbs(c.url) : c.url) : null,
    // The site's machine OCR of the clipping: a "$<id>" pointer to a text
    // chunk in the page data (resolved by npTextChunk), or occasionally inline.
    ocrRef: typeof c.ocr === 'string' && c.ocr.charAt(0) === '$' ? c.ocr.slice(1) : null,
    ocr: typeof c.ocr === 'string' && c.ocr.charAt(0) !== '$' ? c.ocr : null,
  };
}

// Resolve a "$<id>" text chunk in the page data: `<id>:T<hex byte length>,<text>`.
// The length counts UTF-8 bytes, so walk characters until the bytes run out.
function npTextChunk(text, ref) {
  if (!ref || !/^[0-9a-z]+$/i.test(ref)) return null;
  const re = new RegExp('(?:^|[^0-9a-z])' + ref + ':T([0-9a-f]+),');
  const m = re.exec(text);
  if (!m) return null;
  let bytes = parseInt(m[1], 16);
  let i = m.index + m[0].length;
  const start = i;
  while (bytes > 0 && i < text.length) {
    const cp = text.codePointAt(i);
    bytes -= cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
    i += cp > 0xffff ? 2 : 1;
  }
  return text.slice(start, i);
}

// Did the signed-in member make this clipping? By user id, else by username.
function npIsMine(c, me) {
  return (
    (!!me.id && c.userId === me.id) ||
    (!!me.username && !!c.username && c.username.toLowerCase() === me.username.toLowerCase())
  );
}

// Which clippings to offer (owner decision, 2026-10-08):
//   - on a clipping page, THAT clipping, whoever made it - opening it is the
//     choice - first, then your own other clippings on the page;
//   - on the page viewer (`opened` null), only your own clippings.
// Returns { offered, otherClipCount, focusOwner } where focusOwner names the
// clipper of an opened clipping that is not yours.
function npOffered(clips, opened, me) {
  const offered = clips.filter((c) => npIsMine(c, me));
  if (opened && !npIsMine(opened, me)) offered.unshift(opened);
  return {
    offered,
    otherClipCount: clips.filter((c) => !offered.some((o) => o.id === c.id)).length,
    focusOwner: opened && !npIsMine(opened, me) ? opened.username || 'another member' : null,
  };
}
// FHA-SYNC-END newspapers

module.exports = {
  npPageKind,
  npFlightFrom,
  npObjectAt,
  npObjectAfter,
  npJwtPayload,
  npClip,
  npTextChunk,
  npIsMine,
  npOffered,
};
