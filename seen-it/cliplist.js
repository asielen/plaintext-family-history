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
// Classic script (window.SeenItClips, loaded before content.js) and CommonJS (tests).

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
    m.user = username;
    m.clipsAt = Date.now();
    if (full && complete) {
      m.clipsFullAt = Date.now();
      delete m.fullRefreshRequested;
      delete m.clipsFullResume;
    } else if (full) {
      // Stopped at the cap with more to read: keep everything already known
      // (a clipping this pass did not reach is not gone) and carry on from
      // here next time. Not marked as fully refreshed.
      clips = addAll(copyLists(stored), fresh);
      m.clipsFullResume = cursor;
    }
    m.clipCount = Object.values(clips).reduce((n, l) => n + l.length, 0);
    return { clips, meta: m, complete };
  }

  const api = { refreshClipList, clipEntry, MAX_PAGES };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) root.SeenItClips = api;
})(typeof window !== 'undefined' ? window : null);
