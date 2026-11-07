// newspapers.js - Newspapers.com page + clipping capture, panel side.
//
// Newspapers.com offers no original file to download. Its "Download PDF" button
// fetches one JPEG fitted to a 2048px box and assembles the PDF in the browser,
// which leaves a broadsheet page at roughly a quarter of its scanned size - too
// coarse to transcribe. Its page viewer, though, draws the page from tiles:
//
//   https://img.newspapers.com/img/img?id={page}&crop={x},{y},{w},{h}
//       &width={out_w}&height={out_h}&iat={page pass}&user={user id}&…
//
// where `crop` is a region in full-size page pixels. Asking for each region at
// 1:1 (out = crop size) returns the scan at full resolution. This module fetches
// the page that way, one tile at a time with randomized pauses, stitches it,
// cuts each of your clippings out of it, and draws the combined view (clipping
// on top, whole page below with the item boxed).
//
// The content script supplies everything this needs (newspapersInfo in
// content.js): page id, page size, the page pass, your clippings and their
// rectangles. Fetching happens here, in the extension page, because the tiles
// live on another host (img.newspapers.com) that a page-side fetch can't read.
//
// Pacing (rate limits): Newspapers.com answered HTTP 429 when three large image
// requests arrived within a second. So: one tile request at a time with a short
// random gap (0.2-0.7 s; owner, 2026-10-08: the site's own viewer loads its
// tiles all at once, so the real pauses belong between captures, not inside a
// page). Only a 429 or 503 - the site's "slow down" and "busy" answers - earns
// a backoff, of about 1, 2, then 4 minutes (Retry-After honored when sent),
// then a plain stop. Any other refusal stops at once with its real cause: a
// missing tile does not get better by waiting seven minutes. A page costs
// about 15 tiles, roughly 15-25 seconds. A stitched page is reused for every
// clipping on it, so several clippings on one page never fetch it twice.
//
// Loaded as a classic script in panel.html; attaches to the global `FHA`.
//
// Code map:
//   tileUrl / tileGrid   the viewer's tile addresses and grid, in page pixels
//   fetchTile            one tile; backoff on 429/503 only, plain stop otherwise
//   fullPage             the whole page stitched at 1:1 (cached per page id)
//   clipCanvas/combined  the clipping cut out; the annotated view
//   words (pure)         shortDate, longDate, title, caption, citations
//   pageCopyFits (pure)  which bundle the tab's page copy belongs in
//   summarizeStaged (pure)  the closing report: where every bundle went

(function () {
  const FHA = (window.FHA = window.FHA || {});

  // The viewer's own tile grid (1488 x 1448 page pixels); we ask for each at 1:1.
  const TILE_W = 1488;
  const TILE_H = 1448;
  const PAUSE_MS = [200, 700];
  const BACKOFF_S = [60, 120, 240];
  const JPEG_QUALITY = 0.92;

  // The combined view's layout.
  const COMBINED_WIDTH = 2000;
  const MARGIN = 40;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const between = (lo, hi) => lo + Math.random() * (hi - lo);

  // The last stitched page, so sibling clippings on the same page reuse it.
  let cached = null; // { pageId, canvas, reduced }

  function tileUrl(info, x, y, w, h) {
    return (
      'https://img.newspapers.com/img/img?id=' + encodeURIComponent(info.pageId) +
      '&width=' + w + '&height=' + h +
      '&crop=' + [x, y, w, h].join(',') +
      '&brightness=0&contrast=0&invert=0&ts=1&cacheable=1' +
      '&iat=' + encodeURIComponent(info.token) +
      (info.me && info.me.id ? '&user=' + encodeURIComponent(info.me.id) : '')
    );
  }

  // The page cut into the viewer's own tile grid: [x, y, w, h] in page pixels,
  // row by row, the last column and row trimmed to the page edge.
  function tileGrid(W, H) {
    const tiles = [];
    for (let y = 0; y < H; y += TILE_H) {
      for (let x = 0; x < W; x += TILE_W) {
        tiles.push([x, y, Math.min(TILE_W, W - x), Math.min(TILE_H, H - y)]);
      }
    }
    return tiles;
  }

  // One tile, with backoff on the site's "slow down" answers only.
  // onWait(seconds, status) lets the panel say why it has gone quiet. `deps`
  // ({ fetch, sleep }) exists so tests can drive every answer without a network.
  async function fetchTile(url, onWait, deps) {
    const doFetch = (deps && deps.fetch) || fetch;
    const wait = (deps && deps.sleep) || sleep;
    for (let attempt = 0; ; attempt++) {
      let resp;
      try {
        // No cookies: the page pass in the URL is what authorizes a tile (tested
        // 2026-10-08), and leaving cookies out keeps the request cross-origin-clean.
        resp = await doFetch(url, { credentials: 'omit' });
      } catch (e) {
        throw new Error(
          'could not reach Newspapers.com for the page image, so the capture stopped. ' +
            'Nothing was saved for this page. Check your internet connection, then press Capture again'
        );
      }
      if (resp.ok) {
        const blob = await resp.blob();
        if (!/^image\//i.test(blob.type || 'image/jpeg')) {
          throw new Error('Newspapers.com sent something other than an image for a page tile');
        }
        return blob;
      }
      const status = resp.status;
      if (status === 401 || status === 403) {
        throw new Error(
          'Newspapers.com refused the page image (HTTP ' + status + '). Reload the page ' +
            '(its image pass lasts a day) and check you are signed in'
        );
      }
      if (status !== 429 && status !== 503) {
        throw new Error(
          'Newspapers.com could not send part of the page image (HTTP ' + status + '), so the ' +
            'capture stopped. Nothing was saved for this page. Reload the page and press Capture ' +
            'again; if it keeps happening, use the site\'s own download and drop the file in'
        );
      }
      if (attempt >= BACKOFF_S.length) {
        throw new Error(
          'Newspapers.com kept asking us to slow down (HTTP ' + status + '), so the capture ' +
            'stopped. Nothing was saved for this page; try again in a few minutes'
        );
      }
      const retryAfter =
        resp.headers && resp.headers.get ? Number(resp.headers.get('retry-after')) : 0;
      const waitS = (retryAfter > 0 ? retryAfter : BACKOFF_S[attempt]) * between(0.7, 1.3);
      if (onWait) onWait(Math.round(waitS), status);
      await wait(waitS * 1000);
    }
  }

  // The whole page at full resolution, stitched from tiles.
  // onProgress(tileNumber, tileCount, waitSeconds?)
  async function fullPage(info, onProgress) {
    if (cached && cached.pageId === info.pageId) return cached;
    const W = info.pageWidth;
    const H = info.pageHeight;
    const canvas = new OffscreenCanvas(W, H);
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, W, H);

    const tiles = tileGrid(W, H);
    let reduced = 0;
    for (let i = 0; i < tiles.length; i++) {
      if (i) await sleep(between(PAUSE_MS[0], PAUSE_MS[1]));
      const [x, y, w, h] = tiles[i];
      if (onProgress) onProgress(i + 1, tiles.length);
      const blob = await fetchTile(tileUrl(info, x, y, w, h), (s) => {
        if (onProgress) onProgress(i + 1, tiles.length, s);
      });
      const bmp = await createImageBitmap(blob);
      // A tile served smaller than asked would be stretched to fit: count it so
      // the capture can say the page is not at full resolution.
      if (bmp.width < w * 0.98 || bmp.height < h * 0.98) reduced++;
      ctx.drawImage(bmp, x, y, w, h);
      if (bmp.close) bmp.close();
    }
    cached = { pageId: info.pageId, canvas, reduced, tiles: tiles.length };
    return cached;
  }

  // The clipping cut from the full page. Several rectangles (an item continued
  // into the next column) are stacked top to bottom in the site's order.
  function clipCanvas(page, clip) {
    // npOffered never offers such a clipping; this is the last line of defence,
    // so a bad entry names itself instead of failing as a canvas error.
    if (!clip.rects || !clip.rects.length) {
      throw new Error(
        'the clipping "' + (clip.title || clip.id) + '" marks no area on the page, so it cannot be cut out'
      );
    }
    const gap = clip.rects.length > 1 ? 24 : 0;
    const w = Math.max(...clip.rects.map((r) => r.w));
    const h = clip.rects.reduce((sum, r) => sum + r.h, 0) + gap * (clip.rects.length - 1);
    const out = new OffscreenCanvas(w, h);
    const ctx = out.getContext('2d');
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, w, h);
    let y = 0;
    for (const r of clip.rects) {
      ctx.drawImage(page.canvas, r.x, r.y, r.w, r.h, 0, y, r.w, r.h);
      y += r.h + gap;
    }
    return out;
  }

  function fitText(ctx, text, maxWidth) {
    if (ctx.measureText(text).width <= maxWidth) return text;
    let t = text;
    while (t.length > 1 && ctx.measureText(t + '…').width > maxWidth) t = t.slice(0, -1);
    return t + '…';
  }

  // The combined view: a caption, the clipping, then the whole page with this
  // clipping boxed in red and your other clippings on the page dashed in orange.
  // It is a derivative made at capture; the clipping and page files stay unmarked.
  function combined(page, clip, info, otherClips, captionLines) {
    const W = info.pageWidth;
    const H = info.pageHeight;
    const inner = COMBINED_WIDTH - 2 * MARGIN;
    const clipImg = clipCanvas(page, clip);
    const s1 = Math.min(1, inner / clipImg.width);
    const clipW = Math.round(clipImg.width * s1);
    const clipH = Math.round(clipImg.height * s1);
    const s2 = inner / W;
    const pageH = Math.round(H * s2);

    const headerH = 120;
    const labelH = 70;
    const footerH = 70;
    const total = headerH + clipH + labelH + pageH + footerH;
    const out = new OffscreenCanvas(COMBINED_WIDTH, total);
    const ctx = out.getContext('2d');
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, COMBINED_WIDTH, total);

    ctx.fillStyle = '#111';
    ctx.textBaseline = 'top';
    ctx.font = 'bold 34px sans-serif';
    ctx.fillText(fitText(ctx, captionLines[0] || '', inner), MARGIN, 26);
    ctx.font = '24px sans-serif';
    ctx.fillStyle = '#444';
    ctx.fillText(fitText(ctx, captionLines[1] || '', inner), MARGIN, 72);

    let y = headerH;
    const clipX = Math.round((COMBINED_WIDTH - clipW) / 2);
    ctx.drawImage(clipImg, clipX, y, clipW, clipH);
    ctx.strokeStyle = '#999';
    ctx.lineWidth = 1;
    ctx.strokeRect(clipX - 0.5, y - 0.5, clipW + 1, clipH + 1);
    y += clipH;

    ctx.fillStyle = '#444';
    ctx.font = '24px sans-serif';
    ctx.fillText('Where it sits on the page:', MARGIN, y + 28);
    y += labelH;

    const pageY = y;
    ctx.drawImage(page.canvas, 0, 0, W, H, MARGIN, pageY, inner, pageH);
    ctx.strokeStyle = '#999';
    ctx.strokeRect(MARGIN - 0.5, pageY - 0.5, inner + 1, pageH + 1);

    const box = (r) => [MARGIN + r.x * s2, pageY + r.y * s2, r.w * s2, r.h * s2];
    ctx.setLineDash([14, 10]);
    ctx.lineWidth = 4;
    ctx.strokeStyle = '#e08a00';
    for (const o of otherClips) for (const r of o.rects) ctx.strokeRect(...box(r));
    ctx.setLineDash([]);
    ctx.lineWidth = 8;
    ctx.strokeStyle = '#d0021b';
    ctx.fillStyle = 'rgba(208, 2, 27, 0.08)';
    for (const r of clip.rects) {
      ctx.fillRect(...box(r));
      ctx.strokeRect(...box(r));
    }
    y = pageY + pageH;

    ctx.fillStyle = '#666';
    ctx.font = '20px sans-serif';
    const foot =
      'Made at capture from Newspapers.com page ' + info.pageId + ', clipping ' + clip.id +
      '. Red: this clipping. ' + (otherClips.length ? 'Orange dashed: your other clippings on this page. ' : '') +
      'The boxes are not part of the newspaper.';
    ctx.fillText(fitText(ctx, foot, inner), MARGIN, y + 24);
    return out;
  }

  // ── Words: titles, captions, citations ─────────────────────────────────────
  // Pure (no canvas, no network), so tests/test-newspapers.js can drive them.

  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const LONG_MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
    'August', 'September', 'October', 'November', 'December'];

  // "1931-03-03" -> "3 Mar 1931"; anything else comes back as given.
  function shortDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
    return m ? Number(m[3]) + ' ' + MONTHS[Number(m[2]) - 1] + ' ' + m[1] : String(iso || '');
  }

  // "1931-03-03" -> "March 3, 1931", the form the site's citations use.
  function longDate(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ''));
    return m ? LONG_MONTHS[Number(m[2]) - 1] + ' ' + Number(m[3]) + ', ' + m[1] : String(iso || '');
  }

  // The clipping's paper, else the page's, else nothing known.
  function publicationOf(clip, info) {
    return clip.publication || info.publication || { name: null, location: null };
  }

  // "{clip title} - {paper}, {D Mon YYYY}, p. {n}"
  function title(clip, info) {
    const pub = publicationOf(clip, info);
    const bits = [];
    if (pub.name) bits.push(pub.name);
    const d = clip.pageDate || info.pageDate;
    if (d) bits.push(shortDate(d));
    const pg = clip.pageNo || info.pageNo;
    if (pg) bits.push('p. ' + pg);
    return (clip.title || 'Newspapers.com clipping') + (bits.length ? ' - ' + bits.join(', ') : '');
  }

  // The two caption lines drawn on top of the annotated view.
  function caption(clip, info) {
    const pub = publicationOf(clip, info);
    const bits = [];
    if (pub.name) bits.push(pub.name + (pub.location ? ' (' + pub.location + ')' : ''));
    const d = clip.pageDate || info.pageDate;
    if (d) bits.push(shortDate(d));
    const pg = clip.pageNo || info.pageNo;
    if (pg) bits.push('page ' + pg);
    bits.push('Newspapers.com');
    return [clip.title || 'Newspapers.com clipping', bits.join('  ·  ')];
  }

  // The four styles Newspapers.com's own "Source Citation" button offers, in its
  // exact wording (copied from the site's dialog, 2026-10-08). `accessed` is
  // the capture day, YYYY-MM-DD.
  function citations(clip, info, accessed) {
    const pub = publicationOf(clip, info).name || 'Newspapers.com';
    const date = longDate(clip.pageDate || info.pageDate);
    const pg = clip.pageNo || info.pageNo;
    const url = clip.url || '';
    const name = clip.title || 'Clipping';
    const when = longDate(accessed);
    return {
      genealogy:
        pub + ', ' + date + (pg ? ', Page ' + pg : '') + '. via Newspapers.com (' + url +
        ' : accessed ' + when + '), clip page for ' + name +
        (clip.username ? ' by user ' + clip.username : ''),
      apa: pub + '. (' + date + '). ' + name + '. Newspapers.com. Retrieved ' + when + ', from ' + url,
      mla: '"' + name + '" Newspapers.com, ' + pub + ', ' + date + ', ' + url,
      chicago: '"' + name + '" Newspapers.com. ' + pub + ', ' + date + '. ' + url + '.',
    };
  }

  // Which bundle a page copy (the single-file snapshot of the tab) belongs in.
  // On a clipping page the tab shows ONE clipping, so only that clipping's
  // bundle gets it; a sibling ticked alongside would otherwise file a copy of
  // the wrong item's page. On the page viewer the tab shows the newspaper page
  // itself, which every clipping offered there was cut from.
  function pageCopyFits(info, clip) {
    if (info.kind === 'image') return true;
    return info.kind === 'article' && clip.id === info.focusClipId;
  }

  // The panel's closing report for one Capture, which stages one bundle per
  // clipping. `results`: one per clipping, in order - { title, ok, viaHost,
  // where, hostWarning, error }. `warnings`: page-level lines (soft tiles).
  // Every bundle says where it went, a mix of archive and Downloads is never
  // reported as one place, and a clipping that failed after others were saved
  // is named rather than hiding the ones that made it.
  // Returns { text, cls, showHandoff }: cls is 'ok', 'warn' or 'error';
  // showHandoff is true when anything went to Downloads and needs the sweep.
  function summarizeStaged(results, warnings) {
    const saved = results.filter((r) => r.ok);
    const failed = results.filter((r) => !r.ok);
    const lines = [];
    const notes = [];
    for (const w of [...results.map((r) => r.hostWarning), ...(warnings || [])]) {
      if (w && !notes.includes(w)) notes.push(w);
    }
    if (results.length === 1 && saved.length === 1) {
      const r = saved[0];
      lines.push((r.viaHost ? 'Filed straight into your archive: ' : 'Staged to ') + r.where);
    } else {
      lines.push(
        saved.length === results.length
          ? 'Saved all ' + results.length + ' clippings, each as its own source:'
          : saved.length
            ? 'Saved ' + saved.length + ' of ' + results.length + ' clippings, each as its own source:'
            : 'Nothing was saved:'
      );
      for (const r of results) {
        const name = '"' + (r.title || 'Newspapers.com clipping') + '"';
        lines.push(
          '- ' + name + ': ' +
            (!r.ok
              ? 'NOT saved - ' + (r.error || 'unknown problem')
              : r.viaHost
                ? 'filed straight into your archive (' + r.where + ')'
                : 'staged to ' + r.where)
        );
      }
      if (failed.length) {
        lines.push(
          (failed.length === 1
            ? 'The clipping that was not saved is still ticked'
            : 'The ' + failed.length + ' clippings that were not saved are still ticked') +
            ' - press Capture to try again.'
        );
      }
    }
    const text = lines.concat(notes).join('\n');
    const cls = !saved.length ? 'error' : failed.length || notes.length ? 'warn' : 'ok';
    return { text, cls, showHandoff: saved.some((r) => !r.viaHost) };
  }

  function toJpeg(canvas) {
    return canvas.convertToBlob({ type: 'image/jpeg', quality: JPEG_QUALITY });
  }

  FHA.newspapers = {
    fullPage, fetchTile, clipCanvas, combined, toJpeg, tileGrid,
    shortDate, longDate, publicationOf, title, caption, citations,
    pageCopyFits, summarizeStaged,
  };
})();
