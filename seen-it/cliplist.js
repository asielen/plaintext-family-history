// cliplist.js - keep the list of your own Newspapers.com clippings current.
//
// content.js reads the site's clippings list (newest first, 24 at a time) in
// your own signed-in session. Two kinds of pass:
//   incremental  every few minutes: read until a page holds nothing new, add
//                what is new.
//   full         about weekly, or on request: read the whole list again, so a
//                clipping deleted on the site is dropped here too.
// A full pass is capped at MAX_PAGES requests (it pauses between them, so the
// cap is minutes of reading). A long clippings list can need more than one
// pass: when the cap is reached with more still to read, nothing already
// known is dropped, and the next pass resumes where this one stopped
// (meta.clipsFullResume) instead of starting over. Only a full pass that
// reads the whole list from the start may drop anything.
//
// content.js only reads; the background writer (background.js, which loads
// this file too) merges each result into the store as it is at that moment:
// mergeClipList for a list pass, mergePageClips for one newspaper page.
//
// Classic script (window.SeenItClips before content.js, self.SeenItClips in
// the background service worker) and CommonJS (tests).

(function (root) {
  const MAX_PAGES = 60;

  function clipEntry(c, origin) {
    return {
      id: Number(c.clipping_id),
      title: String(c.title || '').trim(),
      url: c.url ? new URL(c.url, origin).href : null,
      created: c.created || null,
    };
  }

  function copyLists(clips) {
    const out = {};
    for (const [pid, list] of Object.entries(clips || {})) out[pid] = (list || []).slice();
    return out;
  }

  function addAll(into, from) {
    for (const [pid, list] of Object.entries(from || {})) {
      const cur = (into[pid] = into[pid] || []);
      for (const c of list || []) if (!cur.some((x) => x.id === c.id)) cur.push(c);
    }
    return into;
  }

  // One pass. `fetchPage(cursor)` returns the site's JSON for one page of the
  // list; `pause()` waits between requests. Returns the new { clips, meta }
  // to store - it writes nothing itself.
  async function refreshClipList(opts) {
    const full = !!opts.full;
    const username = String(opts.username || '');
    const pause = opts.pause || (() => Promise.resolve());
    const maxPages = opts.maxPages || MAX_PAGES;
    const origin = opts.origin || 'https://www.newspapers.com';
    const stored = opts.stored || {};
    const m = Object.assign({}, opts.meta || {});

    const known = new Set();
    for (const list of Object.values(stored)) for (const c of list || []) known.add(c.id);
    const resume = full && m.clipsFullResume != null ? m.clipsFullResume : null;
    // A full pass from the very start rebuilds the list; a resumed or an
    // incremental pass only adds to what is there.
    const fresh = full && resume == null ? {} : copyLists(stored);
    let cursor = resume == null ? 0 : resume;
    let complete = false;
    for (let page = 0; page < maxPages; page++) {
      if (page) await pause();
      const j = await opts.fetchPage(cursor);
      const batch = (j && j.clippings) || [];
      let newHere = 0;
      for (const c of batch) {
        if (c.user_username && c.user_username.toLowerCase() !== username.toLowerCase()) continue;
        const pid = String(c.page_id);
        const e = clipEntry(c, origin);
        const list = (fresh[pid] = fresh[pid] || []);
        if (!list.some((x) => x.id === e.id)) list.push(e);
        if (!known.has(e.id)) newHere++;
      }
      // newest first: an incremental pass stops at the first page with nothing new
      if (!full && newHere < batch.length) {
        complete = true;
        break;
      }
      if (!j || !j.more_results || !batch.length || j.next_cursor_mark == null) {
        complete = true;
        break;
      }
      cursor = j.next_cursor_mark;
    }

    let clips = fresh;
    // `changes` is what the pass decided about the clipping fields of meta -
    // and nothing else. The background writer applies exactly these to the
    // meta as it is THEN (not as it was before minutes of network waits), so
    // a search logged or a research log exported meanwhile is not undone.
    const changes = { set: { user: username, clipsAt: Date.now() }, clear: [] };
    if (full && complete) {
      changes.set.clipsFullAt = Date.now();
      changes.clear.push('fullRefreshRequested', 'clipsFullResume');
    } else if (full) {
      // Stopped at the cap with more to read: keep everything already known
      // (a clipping this pass did not reach is not gone) and carry on from
      // here next time. Not marked as fully refreshed.
      clips = addAll(copyLists(stored), fresh);
      changes.set.clipsFullResume = cursor;
    }
    Object.assign(m, changes.set);
    for (const f of changes.clear) delete m[f];
    m.clipCount = Object.values(clips).reduce((n, l) => n + l.length, 0);
    // `replace`: only a full pass that read the whole list from the start
    // may stand in for the stored list (and so drop a deleted clipping).
    return { clips, meta: m, changes, complete, replace: full && complete && resume == null };
  }

  // The stored list after a pass, merged into the list as it is NOW: other
  // tabs (a newspaper page's own clippings check) may have added to it while
  // the pass was reading.
  function mergeClipList(latest, result) {
    return result.replace ? copyLists(result.clips) : addAll(copyLists(latest), result.clips);
  }

  // A newspaper page's clippings, read in batches of 25 (at most `maxBatches`).
  // `complete` says whether the read reached the end of the page's list.
  async function readPageClips(fetchBatch, pause, maxBatches) {
    const all = [];
    const limit = maxBatches || 4;
    for (let i = 0; i < limit; i++) {
      if (i && pause) await pause();
      const j = await fetchBatch(i * 25);
      const batch = (j && j.clippings) || [];
      all.push(...batch);
      if (batch.length < 25) return { all, complete: true };
    }
    return { all, complete: false };
  }

  // Your clippings on one page, merged into the stored list. Only a complete
  // read may drop one (it was deleted on the site); a read cut short by the
  // batch limit - a page with many other members' clippings - only adds.
  function mergePageClips(clips, pageId, entries, complete) {
    const out = copyLists(clips);
    const pid = String(pageId);
    if (complete) {
      if (entries.length) out[pid] = entries.slice();
      else delete out[pid];
    } else if (entries.length) {
      addAll(out, { [pid]: entries });
    }
    return out;
  }

  const api = { refreshClipList, mergeClipList, readPageClips, mergePageClips, clipEntry, MAX_PAGES };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.SeenItClips = api;
  // window in a content script, self in the background service worker
})(typeof window !== 'undefined' ? window : typeof self !== 'undefined' ? self : null);
