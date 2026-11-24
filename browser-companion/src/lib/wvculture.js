// wvculture.js - a West Virginia Archives record image, panel side.
//
// Why it matters to him: pressing Capture on a Vital Research Records view
// page saves the death certificate or register page itself. The image sits on
// images.wvculture.org, a different host from the view page, and that host
// sends no cross-origin permission, so the page (and the content script, which
// fetches as the page) cannot read it: checked on a live page, the fetch fails.
// So the content script reads which image the page shows
// (lib/wvculture-record.js, copied into content.js) and this side, the
// extension page, downloads it.
//
// NO LOGIN: one request, without cookies (`credentials: 'omit'`) or a
// referrer, only to images.wvculture.org over https (imageUrlOk - the same
// rule as wvImageUrl in lib/wvculture-record.js; tests/test-sync.js runs both
// through one battery). A redirect is never followed (`redirect: 'error'`);
// an answer that is not an image, or under 30 KB, is refused, never saved.
//
// From a FamilySearch record (its "View on wvculture.org" badge), the panel
// first reads the view page the badge opens (badgeImage), under the same
// rules: one request to archive.wvculture.org's view page over https, no
// cookies, no referrer, no redirect. The page's own <img> on the image host
// names the picture (never a built address), and that picture is downloaded as
// above. Two requests in all, both to the state archive, none to FamilySearch.
//
// Loaded as a classic script in panel.html; attaches to the global `FHA`.

(function () {
  const FHA = (window.FHA = window.FHA || {});
  const IMAGE_HOST = 'images.wvculture.org';
  const IMAGE_PATH_RE = /^\/\d+\/\d+\.(gif|jpe?g)$/i;
  const EXT = { 'image/gif': 'gif', 'image/jpeg': 'jpg', 'image/jpg': 'jpg' };
  const VIEW_HOST = 'archive.wvculture.org';
  const VIEW_PATH_RE = /^\/vrr\/va_view2\.aspx$/i;
  // A view page is about 5 KB; anything far larger is not one.
  const VIEW_MAX_CHARS = 1024 * 1024;

  function imageUrlOk(url) {
    let u;
    try {
      u = new URL(url);
    } catch (e) {
      return false;
    }
    return u.protocol === 'https:' && u.hostname.toLowerCase() === IMAGE_HOST && !u.port && !u.username &&
      !u.password && IMAGE_PATH_RE.test(u.pathname);
  }

  // The record image: { ok, blob, ext, contentType } or { ok:false, error }.
  // `fetchFn` is the browser's fetch (a test's stand-in).
  async function fetchImage(url, fetchFn) {
    if (!imageUrlOk(url)) {
      return { ok: false, error: 'the image is not on the West Virginia Archives image host, so it was not downloaded' };
    }
    let r;
    try {
      r = await fetchFn(url, { credentials: 'omit', referrerPolicy: 'no-referrer', redirect: 'error' });
    } catch (e) {
      return { ok: false, error: 'the image could not be reached (the connection dropped, or the image host sent it ' +
        'somewhere else, which is not followed)' };
    }
    if (!r.ok) return { ok: false, error: 'the West Virginia Archives image server refused the picture (HTTP ' + r.status + ')' };
    if (r.url && !imageUrlOk(r.url)) {
      return { ok: false, error: 'the image’s address led away from the West Virginia Archives image host, so it was not saved' };
    }
    const type = String((r.headers && r.headers.get && r.headers.get('content-type')) || '').split(';')[0].trim().toLowerCase();
    if (!/^image\//.test(type)) return { ok: false, error: 'the image server sent a page instead of the picture' };
    let blob;
    try {
      blob = await r.blob();
    } catch (e) {
      return { ok: false, error: 'the picture broke off while downloading' };
    }
    if (blob.size < 30 * 1024) {
      return { ok: false, error: 'the picture is only ' + Math.round(blob.size / 1024) + ' KB, not the full image, so it was not saved' };
    }
    const fromUrl = (IMAGE_PATH_RE.exec(new URL(url).pathname) || [])[1];
    const ext = EXT[type] || (fromUrl ? fromUrl.toLowerCase().replace('jpeg', 'jpg') : 'gif');
    return { ok: true, blob, ext, contentType: type };
  }

  // The view page a badge leads to: https, on the archive's own host, the
  // Vital Research Records view page and nothing else.
  function pageUrlOk(url) {
    let u;
    try {
      u = new URL(url);
    } catch (e) {
      return false;
    }
    return u.protocol === 'https:' && u.hostname.toLowerCase() === VIEW_HOST && !u.port && !u.username &&
      !u.password && VIEW_PATH_RE.test(u.pathname);
  }

  // The picture a view page shows: its first <img> on the image host, as
  // https (the page writes http), or null. `parse(html)` is the browser's
  // HTML parser (DOMParser), so the page is read as a browser reads it.
  function viewPageImage(html, pageUrl, parse) {
    let doc;
    try {
      doc = parse(String(html || ''));
    } catch (e) {
      return null;
    }
    const imgs = (doc && doc.querySelectorAll) ? doc.querySelectorAll('img') : [];
    for (const img of Array.from(imgs)) {
      let u;
      try {
        u = new URL(img.getAttribute('src') || '', pageUrl);
      } catch (e) {
        continue;
      }
      if (u.protocol !== 'http:' && u.protocol !== 'https:') continue;
      u.protocol = 'https:';
      if (imageUrlOk(u.href)) return u.href;
    }
    return null;
  }

  // A FamilySearch record's badge, followed: the view page once, then its
  // picture. { ok, blob, ext, contentType, imageUrl } or { ok:false, error },
  // the error in words he can act on. `fetchFn` is the browser's fetch.
  async function badgeImage(pageUrl, fetchFn, parse) {
    if (!pageUrlOk(pageUrl)) {
      return { ok: false, error: 'the badge does not lead to a West Virginia Archives image page, so it was not followed' };
    }
    let r;
    try {
      r = await fetchFn(pageUrl, { credentials: 'omit', referrerPolicy: 'no-referrer', redirect: 'error' });
    } catch (e) {
      return { ok: false, error: 'the West Virginia Archives page could not be reached (the connection dropped, or ' +
        'the site sent it somewhere else, which is not followed)' };
    }
    if (!r.ok) return { ok: false, error: 'the West Virginia Archives site refused the page (HTTP ' + r.status + ')' };
    if (r.url && !pageUrlOk(r.url)) {
      return { ok: false, error: 'the West Virginia Archives page led somewhere else, so it was not read' };
    }
    const type = String((r.headers && r.headers.get && r.headers.get('content-type')) || '').split(';')[0].trim().toLowerCase();
    if (type && type !== 'text/html') return { ok: false, error: 'the West Virginia Archives site sent something other than its image page' };
    let html;
    try {
      html = await r.text();
    } catch (e) {
      return { ok: false, error: 'the West Virginia Archives page broke off while loading' };
    }
    if (html.length > VIEW_MAX_CHARS) return { ok: false, error: 'the West Virginia Archives site sent something other than its image page' };
    const imageUrl = viewPageImage(html, pageUrl, parse);
    if (!imageUrl) return { ok: false, error: 'the West Virginia Archives page shows no image the panel can save' };
    const image = await fetchImage(imageUrl, fetchFn);
    return image.ok ? Object.assign({ imageUrl }, image) : image;
  }

  FHA.wvculture = { imageUrlOk, fetchImage, pageUrlOk, viewPageImage, badgeImage };
})();
