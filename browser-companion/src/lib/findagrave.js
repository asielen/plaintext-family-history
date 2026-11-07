// findagrave.js - Find a Grave memorial photos, panel side.
//
// A memorial's photos (the gravestone, the person, the family, documents)
// are full size on Find a Grave's own image host, images.findagrave.com - a
// different host from the memorial page, so the page itself cannot read them
// and they are fetched here, in the extension page. The content script reads
// the memorial and its photo list (lib/findagrave-record.js, copied into
// content.js); this side downloads each photo, one at a time with a pause
// between them, so a memorial with forty photos does not look like a robot.
//
// NO LOGIN: every request goes without cookies (`credentials: 'omit'`) and
// without the extension's address as a referrer, and only to Find a Grave's
// image host over https (photoUrlOk - the same rule as fagPhotoUrlOk in
// lib/findagrave-record.js; tests/test-sync.js runs both through one
// battery). A photo that comes back from anywhere else after a redirect, or
// that is not an image, is refused, never saved.
//
// Loaded as a classic script in panel.html; attaches to the global `FHA`.

(function () {
  const FHA = (window.FHA = window.FHA || {});
  const PHOTO_HOST = 'images.findagrave.com';

  function photoUrlOk(url) {
    let u;
    try {
      u = new URL(url);
    } catch (e) {
      return false;
    }
    return u.protocol === 'https:' && u.hostname.toLowerCase() === PHOTO_HOST && !u.port && !u.username && !u.password &&
      /^\/photos\//.test(u.pathname);
  }

  const EXT = { 'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp' };

  // One photo, full size: { ok, blob, ext } or { ok:false, error, slowDown? }.
  // `fetchFn` is the browser's fetch (a test's stand-in).
  async function fetchPhoto(url, fetchFn) {
    if (!photoUrlOk(url)) {
      return { ok: false, error: 'the photo is not on Find a Grave’s own image host, so it was not downloaded' };
    }
    let r;
    try {
      r = await fetchFn(url, { credentials: 'omit', referrerPolicy: 'no-referrer' });
    } catch (e) {
      return { ok: false, error: 'could not reach Find a Grave’s image host - check your internet connection' };
    }
    if (r.status === 429) {
      return { ok: false, slowDown: true, error: 'Find a Grave asked us to slow down (HTTP 429)' };
    }
    if (!r.ok) {
      return {
        ok: false,
        error: r.status === 404 || r.status === 410
          ? 'Find a Grave no longer has this photo (HTTP ' + r.status + ')'
          : 'Find a Grave refused the photo (HTTP ' + r.status + ')',
      };
    }
    if (r.url && !photoUrlOk(r.url)) {
      return { ok: false, error: 'the photo’s address led away from Find a Grave’s image host, so it was not saved' };
    }
    const type = String((r.headers && r.headers.get && r.headers.get('content-type')) || '').split(';')[0].trim().toLowerCase();
    if (!/^image\//.test(type)) {
      return { ok: false, error: 'Find a Grave sent a page instead of the photo' };
    }
    const blob = await r.blob();
    const fromUrl = (/\.(jpe?g|png|gif|webp)$/i.exec(new URL(url).pathname) || [])[1];
    return { ok: true, blob, ext: EXT[type] || (fromUrl ? fromUrl.toLowerCase().replace('jpeg', 'jpg') : 'jpg') };
  }

  // The pause between two photos, 0.8-2 s.
  function pauseMs() {
    return 800 + Math.floor(Math.random() * 1200);
  }

  FHA.findagrave = { PHOTO_HOST, photoUrlOk, fetchPhoto, pauseMs };
})();
