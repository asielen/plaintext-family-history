// wvculture-record.js - the pure, importable core of saving a West Virginia
// Archives image: which view page an address is (archive.wvculture.org's
// Vital Research Records, va_view2.aspx), the image that page shows, the size
// check and the citation.
//
// Why it matters to him: a death certificate or marriage register page on the
// state archive's free site is saved as a source of its own, with its film and
// image number, so anyone can open that exact page again.
//
// The browser-side content.js DOES NOT import this file (it is an injected
// classic script that cannot use ES imports); it carries a COPY of the block
// between `FHA-SYNC-BEGIN wvculture` / `FHA-SYNC-END wvculture`, and
// tests/test-sync.js fails if the two drift. Change it here first, then mirror.
//
// NO LOGIN: the view page and its image are public. The image is read from the
// page's own <img> (the padding and extension vary by film, so the URL is never
// built), and only from images.wvculture.org.
//
// Code map:
//   wvParsePage       the film and image numbers from a view-page address
//   wvImageUrl        the page's image (its img src on the image host), https
//   wvExtension       'gif' or 'jpg' from that image address
//   wvCitation        the citation line for the source
//   wvPartnerPage     a FamilySearch record's "View on wvculture.org" address, as a page to follow

'use strict';

// FHA-SYNC-BEGIN wvculture
const WV_VIEW_HOST = 'archive.wvculture.org';
const WV_IMAGE_HOST = 'images.wvculture.org';
// A death certificate image is a 7-digit film path, a register page 5-digit;
// the extension is .gif or .jpg. Anything else is not an image of a record.
const WV_IMAGE_PATH_RE = /^\/\d+\/\d+\.(gif|jpe?g)$/i;

// The film and image numbers of a view page (va_view2.aspx), or null for any
// other address or a view page with no usable image number.
function wvParsePage(href) {
  let u;
  try {
    u = new URL(href);
  } catch (e) {
    return null;
  }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
  if (u.hostname.toLowerCase() !== WV_VIEW_HOST) return null;
  if (!/^\/vrr\/va_view2\.aspx$/i.test(u.pathname)) return null;
  let filmNumber = null;
  let imageNumber = null;
  for (const [k, v] of u.searchParams) {
    const key = k.toLowerCase();
    if (key === 'filmnumber') filmNumber = v;
    else if (key === 'imagenumber') imageNumber = v;
  }
  if (!/^\d+$/.test(filmNumber || '') || !/^\d+$/.test(imageNumber || '')) return null;
  return { filmNumber, imageNumber };
}

// The page's image: the first img src that is an image on images.wvculture.org,
// upgraded to https. `srcs` is the list of the page's img src values. Null when
// none is one (the page then gives nothing to save).
function wvImageUrl(srcs) {
  for (const src of srcs || []) {
    let u;
    try {
      u = new URL(String(src));
    } catch (e) {
      continue;
    }
    if (u.protocol !== 'http:' && u.protocol !== 'https:') continue;
    if (u.hostname.toLowerCase() !== WV_IMAGE_HOST || u.port || u.username || u.password) continue;
    if (!WV_IMAGE_PATH_RE.test(u.pathname)) continue;
    return 'https://' + WV_IMAGE_HOST + u.pathname;
  }
  return null;
}

// 'gif' or 'jpg' from an image address, or null.
function wvExtension(url) {
  const m = /\.(gif|jpe?g)$/i.exec(String(url || ''));
  if (!m) return null;
  return m[1].toLowerCase() === 'gif' ? 'gif' : 'jpg';
}

// The citation line: the archive, the page as the human opened it, the film and
// the image. `accessed` is YYYY-MM-DD.
function wvCitation(info, accessed) {
  return 'West Virginia Archives and History, Vital Research Records (' + info.pageUrl +
    ' : accessed ' + accessed + '), film ' + info.filmNumber + ', image ' + info.imageNumber + '.';
}

// A FamilySearch West Virginia index entry has no image of its own; its
// "View on wvculture.org" badge leads to the certificate or register page on
// the state archive's site, and Capture follows it so the record itself is
// saved beside the index. `url` is the address the badge opens (FamilySearch's
// own data names it: lib/familysearch-record.js fsPartnerImageUrl). The page
// to fetch (always https), its film and image number, and the citation part
// naming where the original is; null when the address is not a view page.
function wvPartnerPage(url) {
  const parsed = wvParsePage(url);
  if (!parsed) return null;
  const u = new URL(url);
  // Fetched by the panel, so only the archive's own page: no other port, no
  // name or password in the address.
  if (u.port || u.username || u.password) return null;
  u.protocol = 'https:';
  return {
    site: 'wvculture',
    archive: 'West Virginia Archives and History',
    filmNumber: parsed.filmNumber,
    imageNumber: parsed.imageNumber,
    pageUrl: u.href,
    cite: 'image at West Virginia Archives and History, film ' + parsed.filmNumber + ', image ' + parsed.imageNumber,
  };
}
// FHA-SYNC-END wvculture

module.exports = {
  wvParsePage,
  wvImageUrl,
  wvExtension,
  wvCitation,
  wvPartnerPage,
};
