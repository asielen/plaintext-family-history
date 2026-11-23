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
// Loaded as a classic script in panel.html; attaches to the global `FHA`.

(function () {
  const FHA = (window.FHA = window.FHA || {});
  const IMAGE_HOST = 'images.wvculture.org';
  const IMAGE_PATH_RE = /^\/\d+\/\d+\.(gif|jpe?g)$/i;
  const EXT = { 'image/gif': 'gif', 'image/jpeg': 'jpg', 'image/jpg': 'jpg' };

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

  FHA.wvculture = { imageUrlOk, fetchImage };
})();
