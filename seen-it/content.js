// content.js - "Newspapers.com: Seen It"
//
// On a search results page:
//   • logs the search (its terms and filters, and the result count),
//   • shows "You ran this search before…" above the results, with what you
//     opened and ruled out from it,
//   • puts pills beside each result: your clippings, how often you have seen
//     the page, your verdict ("Nothing here" / "Later"), another member's
//     clipping that overlaps one of yours, how often it scrolled past without
//     being opened, or "New",
//   • records results that sat on screen (60%+ visible for 1.5 s) without
//     being opened - "passed over", usually judged from the snippet.
// On a newspaper page (/image/{pageId}/):
//   • records the look, what the page is (paper, date, page, place), which
//     search led to it, and how long it was on screen,
//   • shows a badge with the pills and two buttons: "Nothing here" and
//     "Come back later",
//   • checks your clippings on that page, and notes another member's clipping
//     only where it overlaps one of yours.
// It also keeps your own clippings list current (which pages you have clipped).
//
// It never clicks, clips, saves or changes anything on the site.

(function () {
  if (window.__seenItLoaded) return;
  window.__seenItLoaded = true;

  const LIST_INCREMENTAL_EVERY = 3 * 60 * 1000;
  const LIST_FULL_EVERY = 7 * 24 * 60 * 60 * 1000;
  const PAGE_CLIPS_EVERY = 2 * 60 * 1000;    // re-opening the same page
  const PAGE_CLIPS_RETURN = 60 * 1000;       // coming back to the tab or window
  const PAGE_CLIPS_FORCED_MIN = 5 * 1000;    // a save, or the Check button
  // The site's own notice when a clipping is saved (its text is "Clipping saved").
  const CLIP_SAVED_RE = /\bclip(?:ping)? saved\b|saved to (?:your )?clippings/i;
  const OVERLAP_MIN = 0.5; // share of the smaller clipping covered by the other
  // Search parameters that do not change WHICH results a search finds.
  const VOLATILE_PARAMS = new Set(['page', 'search-id', 'searchId', 'offset', 'sort', 'match', 'start']);

  let data = { views: {}, pages: {}, passes: {}, searches: {}, clips: {}, meta: {}, known: {}, settings: {} };
  // What the family archive holds - shown only in advanced mode.
  const archiveKnown = () => (data.settings && data.settings.advanced ? data.known || {} : {});
  const PASS_VISIBLE = 0.6;
  const PASS_MS = 1500;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const between = (a, b) => a + Math.random() * (b - a);
  const send = (msg) => chrome.runtime.sendMessage(msg).catch(() => null);

  // ── data ──────────────────────────────────────────────────────────────────
  async function loadData() {
    const o = await chrome.storage.local.get(['views', 'pages', 'passes', 'searches', 'clips', 'meta', 'known', 'settings']);
    for (const k of Object.keys(data)) data[k] = o[k] || {};
  }

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    for (const k of Object.keys(data)) if (changes[k]) data[k] = changes[k].newValue || {};
    scheduleRender();
  });

  function looksOf(v) {
    return Math.max(v && v.t ? v.t.length : 0, (v && v.n) || 0);
  }

  function pageIdFromHref(href) {
    const m = /\/image\/(\d+)/.exec(String(href || ''));
    return m ? m[1] : null;
  }

  // ── the page's own embedded data ──────────────────────────────────────────
  function flightText() {
    let out = '';
    const re = /self\.__next_f\.push\(\[\d+,("(?:[^"\\]|\\.)*")\]\)/g;
    for (const s of document.scripts) {
      const t = s.textContent || '';
      if (t.indexOf('__next_f') < 0) continue;
      let m;
      re.lastIndex = 0;
      while ((m = re.exec(t))) {
        try {
          out += JSON.parse(m[1]);
        } catch (e) {
          /* skip */
        }
      }
    }
    return out;
  }

  function objectAt(text, start) {
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
      else if (c === '}' && --depth === 0) {
        try {
          return JSON.parse(text.slice(start, k + 1));
        } catch (e) {
          return null;
        }
      }
    }
    return null;
  }

  function signedInUser() {
    const m = flightText().match(/"user":\{"id":(\d+)[^{}]*?"username":"([^"]+)"/);
    return m ? { id: Number(m[1]), username: m[2] } : null;
  }

  // What a newspaper page is. The page data describes the page first loaded, so
  // it is used only when its id matches; otherwise the tab title is read
  // ("Jun 14, 1923, page 4 - The Fairview Courier at Newspapers.com").
  function pageInfo(pageId) {
    const text = flightText();
    for (let at = text.indexOf('"image":{'); at >= 0; at = text.indexOf('"image":{', at + 1)) {
      const o = objectAt(text, at + '"image":'.length);
      if (o && String(o.imageId) === String(pageId)) {
        return {
          paper: o.publicationTitle || null,
          date: o.date || null,
          pageNo: o.title ? String(o.title).replace(/^(?:page\s+|p\.\s*)/i, '') : null,
          place: o.location || null,
        };
      }
    }
    const t = /^([A-Z][a-z]{2} \d{1,2}, \d{4}), page (\S+) - (.+?) at Newspapers\.com/.exec(document.title || '');
    if (t) {
      const d = new Date(t[1] + ' 12:00');
      return { paper: t[3], date: isNaN(d) ? t[1] : d.toISOString().slice(0, 10), pageNo: t[2], place: null };
    }
    return null;
  }

  // ── your clippings ────────────────────────────────────────────────────────
  // The list logic itself is in cliplist.js (window.SeenItClips), so it can be tested.
  const clipEntry = (c) => SeenItClips.clipEntry(c, location.origin);

  // Every write goes through the background writer (background.js): a tab
  // that read the store, waited minutes on the network and wrote it all back
  // would erase whatever was recorded in between.
  async function withLock(fn) {
    const lock = await send({ type: 'clipLock' });
    if (!lock || !lock.ok || !lock.got) return;
    try {
      await fn();
    } finally {
      await send({ type: 'clipUnlock' });
    }
  }

  async function fetchJson(url) {
    const r = await fetch(url, { credentials: 'include', headers: { accept: 'application/json' } });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  async function maybeRefreshClips() {
    const meta = data.meta || {};
    const now = Date.now();
    const full = meta.fullRefreshRequested || !meta.clipsFullAt || now - meta.clipsFullAt > LIST_FULL_EVERY;
    if (!full && meta.clipsAt && now - meta.clipsAt < LIST_INCREMENTAL_EVERY) return;
    const user = signedInUser() || (meta.user ? { username: meta.user } : null);
    if (!user || !user.username) return;

    await withLock(async () => {
      const { clips: stored = {}, meta: m = {} } = await chrome.storage.local.get(['clips', 'meta']);
      const r = await SeenItClips.refreshClipList({
        full,
        stored,
        meta: m,
        username: user.username,
        origin: location.origin,
        pause: () => sleep(between(2000, 5000)),
        fetchPage: (cursor) => fetchJson('/api/clipping/list?user=' + encodeURIComponent(user.username) +
          '&visibility=all&count=24&cursor_mark=' + encodeURIComponent(cursor) + '&product_id=1'),
      });
      await send({ type: 'clipList', result: { clips: r.clips, changes: r.changes, replace: r.replace } });
    }).catch((e) => console.warn('seen-it: clippings refresh', e));
  }

  // Overlap between two rectangles, as a share of the smaller one.
  function overlapShare(a, b) {
    const w = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
    const h = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
    if (w <= 0 || h <= 0) return 0;
    return (w * h) / Math.min(a.width * a.height, b.width * b.height);
  }

  // On a newspaper page: your clippings here (the list the viewer itself loads),
  // and other members' clippings ONLY where they overlap one of yours.
  const pageClipsChecked = {};
  // `minGap`: how long since this page's last check before checking again -
  // long for an ordinary re-open, short when a clipping was probably just made.
  async function refreshPageClips(pageId, minGap) {
    const gap = minGap == null ? PAGE_CLIPS_EVERY : minGap;
    if (pageClipsChecked[pageId] && Date.now() - pageClipsChecked[pageId] < gap) return;
    pageClipsChecked[pageId] = Date.now();
    const user = signedInUser() || (data.meta.user ? { username: data.meta.user } : null);
    if (!user) return;
    try {
      const { all, complete } = await SeenItClips.readPageClips(
        (start) => fetchJson('/api/clipping/page?page_id=' + pageId + '&start=' + start + '&count=25'),
        () => sleep(between(800, 2000)));
      const isMine = (c) => (c.user_username || '').toLowerCase() === user.username.toLowerCase();
      const mine = all.filter(isMine);
      const overlaps = [];
      for (const o of all.filter((c) => !isMine(c) && c.rectangle)) {
        const hit = mine.find((m) => m.rectangle && overlapShare(o.rectangle, m.rectangle) >= OVERLAP_MIN);
        if (hit) {
          overlaps.push({
            id: o.clipping_id,
            user: o.user_username || null,
            title: String(o.title || '').trim(),
            url: o.url ? new URL(o.url, location.origin).href : null,
            mine: hit.clipping_id,
          });
        }
      }
      // Only a read that reached the end of the page's list may drop one of
      // your clippings; a page crowded with other members' clippings is read
      // in part, and a part adds without removing.
      await send({ type: 'pageClips', pageId, entries: mine.map(clipEntry), complete });
      await send({ type: 'overlaps', pageId, overlaps });
    } catch (e) {
      /* the badge shows what is already known */
    }
  }

  // ── searches ──────────────────────────────────────────────────────────────
  function searchKey() {
    const params = {};
    for (const [k, v] of new URLSearchParams(location.search)) {
      if (!VOLATILE_PARAMS.has(k) && v !== '') params[k] = v;
    }
    const keys = Object.keys(params).sort();
    if (!keys.length) return null;
    const sorted = {};
    for (const k of keys) sorted[k] = params[k];
    return { key: JSON.stringify(sorted), params: sorted };
  }

  function resultCount() {
    for (const el of document.querySelectorAll('div, span, p, h1, h2, h3')) {
      if (el.children.length) continue;
      const m = /^\s*([\d,]+)\s+match(?:es)?\s*$/i.exec(el.textContent || '');
      if (m) return Number(m[1].replace(/,/g, ''));
      if (/^\s*no good matches/i.test(el.textContent || '')) return 0;
    }
    return null;
  }

  let priorRuns = [];
  let currentSearchKey = null;
  let searchLogged = null;
  async function logSearch() {
    const s = searchKey();
    if (!s) return;
    // This run's banner compares with runs BEFORE it, so remember them first.
    if (currentSearchKey !== s.key) {
      const runs = ((data.searches[s.key] || {}).runs || []).slice();
      const last = runs[runs.length - 1];
      // A run in the last 30 minutes is this same sitting, not an earlier one.
      priorRuns = last && Date.now() - last.at < 30 * 60 * 1000 ? runs.slice(0, -1) : runs;
      currentSearchKey = s.key;
    }
    // Give the results a moment to arrive, so the count is recorded with the run.
    let count = null;
    for (let i = 0; i < 20 && count == null; i++) {
      count = resultCount();
      if (count == null) await sleep(500);
    }
    // Not twice in one sitting - but the same search run again later is a new run.
    if (SeenItSearch.isRepeat(searchLogged, s.key, count, Date.now())) return;
    searchLogged = { key: s.key, count, at: Date.now() };
    await send({ type: 'search', key: s.key, params: s.params, url: location.href.split('#')[0], count });
  }

  // ── reading time on a newspaper page ──────────────────────────────────────
  let dwellPage = null;
  let dwellSince = 0;
  function dwellFlush() {
    if (dwellPage && dwellSince) {
      const ms = Date.now() - dwellSince;
      if (ms > 500) send({ type: 'dwell', pageId: dwellPage, ms });
    }
    dwellSince = dwellPage && document.visibilityState === 'visible' ? Date.now() : 0;
  }
  function dwellStart(pageId) {
    dwellFlush();
    dwellPage = pageId;
    dwellSince = pageId && document.visibilityState === 'visible' ? Date.now() : 0;
  }
  document.addEventListener('visibilitychange', dwellFlush);
  window.addEventListener('pagehide', dwellFlush);
  setInterval(dwellFlush, 30000);

  // ── rendering ─────────────────────────────────────────────────────────────
  function fmtDate(ms) {
    const d = new Date(ms);
    const sameYear = d.getFullYear() === new Date().getFullYear();
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: sameYear ? undefined : 'numeric' });
  }

  function fmtMinutes(ms) {
    const m = Math.round((ms || 0) / 60000);
    return m < 1 ? 'under a minute' : m + ' min';
  }

  function pill(cls, text, title, href) {
    const el = document.createElement(href ? 'a' : 'span');
    el.className = 'nsi-pill ' + cls;
    el.textContent = text;
    if (title) el.title = title;
    if (href) {
      el.href = href;
      el.target = '_blank';
      el.rel = 'noopener';
      el.addEventListener('click', (e) => e.stopPropagation());
    }
    return el;
  }

  // What the family archive holds for this page: your clippings on it that are
  // filed as sources (with their S-ids), captured but not yet filed, or neither.
  function archivePills(wrap, pageId, clips) {
    const known = archiveKnown();
    const filed = [];
    const captured = [];
    const missing = [];
    for (const c of clips) {
      const k = known['clip:' + c.id];
      if (k && (k.state === 'filed' || k.state === 'processed')) filed.push({ c, k });
      else if (k && k.state === 'captured') captured.push(c);
      else missing.push(c);
    }
    if (filed.length) {
      wrap.appendChild(pill('nsi-archive', '📚 ' + filed.length + ' in archive',
        'Already in the family archive:\n' + filed.map(({ c, k }) =>
          '• ' + (k.sid || (k.state === 'processed' ? 'removed as a duplicate' : '')) + '  ' + (c.title || 'Untitled clipping')).join('\n')));
    }
    if (captured.length) {
      wrap.appendChild(pill('nsi-captured', '📥 ' + captured.length + ' captured, not filed',
        'Captured into the inbox, waiting to be filed:\n' + captured.map((c) => '• ' + (c.title || 'Untitled clipping')).join('\n')));
    }
    if (missing.length && (filed.length || captured.length || Object.keys(known).length)) {
      wrap.appendChild(pill('nsi-notyet', '○ ' + missing.length + ' not in archive',
        'Clipped, but not captured into the archive yet:\n' + missing.map((c) => '• ' + (c.title || 'Untitled clipping')).join('\n')));
    }
    // Someone's clipping of this page is filed, though none of yours are listed here.
    const pk = known['page:' + pageId];
    if (!clips.length && pk && pk.sid) {
      wrap.appendChild(pill('nsi-archive', '📚 Page in archive', 'A clipping from this page is in the archive as ' + pk.sid + '.'));
    }
  }

  function pillsFor(pageId, opts) {
    const here = !!(opts && opts.thisVisit);
    const wrap = document.createElement('span');
    wrap.className = 'nsi-pills';
    const v = data.views[pageId];
    const p = data.pages[pageId] || {};
    const looks = looksOf(v);
    const clips = data.clips[pageId] || [];
    if (clips.length) {
      wrap.appendChild(pill('nsi-clip',
        '✂ ' + clips.length + (clips.length === 1 ? ' clipping of yours' : ' clippings of yours'),
        'Your clippings on this page:\n' + clips.map((c) => '• ' + (c.title || 'Untitled clipping')).join('\n'),
        clips[0].url));
    }
    archivePills(wrap, pageId, clips);
    if (p.mark === 'nothing') {
      wrap.appendChild(pill('nsi-nothing', '✓ Nothing here',
        'You marked this page: nothing here' + (p.markAt ? ' (' + fmtDate(p.markAt) + ')' : '') + '.'));
    }
    if (p.mark === 'later') {
      wrap.appendChild(pill('nsi-later', '↻ Come back later',
        'You marked this page to come back to' + (p.markAt ? ' (' + fmtDate(p.markAt) + ')' : '') + '.'));
    }
    if (looks) {
      const prev = here ? (v.t && v.t.length > 1 ? v.t[v.t.length - 2] : 0) : v.last;
      const text = (here ? 'Opened ' : 'Seen ') + looks + '×' + (prev ? ' · last ' + fmtDate(prev) : '');
      wrap.appendChild(pill('nsi-seen', text,
        'You have opened this page ' + looks + ' time' + (looks === 1 ? '' : 's') + (here ? ', counting this visit' : '') +
        (p.dwellMs ? '; about ' + fmtMinutes(p.dwellMs) + ' on it in all' : '') + '.'));
    }
    if (p.overlaps && p.overlaps.length) {
      wrap.appendChild(pill('nsi-overlap',
        '👥 ' + p.overlaps.length + (p.overlaps.length === 1 ? ' other member clipped your item' : ' other members clipped your item'),
        p.overlaps.map((o) => '• ' + (o.user || 'someone') + ': ' + (o.title || 'untitled')).join('\n'),
        p.overlaps[0].url));
    }
    const ps = data.passes[pageId];
    if (!looks && !clips.length && ps && ps.t && ps.t.length && !here) {
      const terms = (ps.keys || [])
        .map((k) => { try { return JSON.parse(k).keyword; } catch (e) { return null; } })
        .filter(Boolean);
      wrap.appendChild(pill('nsi-passed', 'Passed over ' + ps.t.length + '× · last ' + fmtDate(ps.last),
        'This result was on your screen ' + ps.t.length + ' time' + (ps.t.length === 1 ? '' : 's') +
        ' and you did not open it' + (terms.length ? ' (searches: ' + [...new Set(terms)].join('; ') + ')' : '') + '.'));
    } else if (!clips.length && !looks && !p.mark) {
      wrap.appendChild(pill('nsi-new', 'New', 'You have not opened this page or passed over it before.'));
    }
    return wrap;
  }

  function markButtons(pageId) {
    const p = data.pages[pageId] || {};
    const box = document.createElement('span');
    box.className = 'nsi-marks';
    const mk = (mark, label, title) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'nsi-mark' + (p.mark === mark ? ' nsi-on' : '');
      b.textContent = label;
      b.title = title;
      b.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        send({ type: 'mark', pageId, mark: p.mark === mark ? null : mark });
      });
      return b;
    };
    box.appendChild(mk('nothing', '✓ Nothing here', 'Mark this page as checked, with nothing for you (click again to clear).'));
    box.appendChild(mk('later', '↻ Later', 'Mark this page to come back to (click again to clear).'));
    const check = document.createElement('button');
    check.type = 'button';
    check.className = 'nsi-mark';
    check.textContent = '⟳ Check';
    check.title = 'Check now for clippings you just made on this page.';
    check.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      check.textContent = '⟳ Checking…';
      refreshPageClips(pageId, PAGE_CLIPS_FORCED_MIN).finally(() => { check.textContent = '⟳ Check'; });
    });
    box.appendChild(check);
    return box;
  }

  function renderSearchResults() {
    for (const root of document.querySelectorAll('[class*="SearchResult_SearchResult__"]')) {
      const titleBox = root.querySelector('[class*="SearchResult_Title__"]');
      const link = (titleBox && titleBox.querySelector('a[href*="/image/"]')) || root.querySelector('a[href*="/image/"]');
      const pageId = link && pageIdFromHref(link.getAttribute('href'));
      if (!pageId) continue;
      const p = data.pages[pageId] || {};
      // Pills go right under the title link (paper, date, place), above the
      // match arrows, where the eye lands first.
      const key = pageId + '|' + JSON.stringify(data.views[pageId] || 0) + '|' + (data.clips[pageId] || []).length +
        '|' + knownSig(pageId) +
        '|' + (p.mark || '') + '|' + (p.overlaps || []).length + '|' + JSON.stringify((data.passes[pageId] || {}).t || 0);
      const next = link.nextElementSibling;
      const existing = next && next.classList.contains('nsi-pills') ? next : null;
      if (existing && existing.dataset.key === key) continue;
      if (existing) existing.remove();
      const pills = pillsFor(pageId);
      pills.dataset.key = key;
      link.insertAdjacentElement('afterend', pills);
      root.classList.toggle('nsi-has-clip', !!(data.clips[pageId] || []).length);
      root.classList.toggle('nsi-was-seen', looksOf(data.views[pageId]) > 0);
      root.classList.toggle('nsi-ruled-out', p.mark === 'nothing');
      watchForPass(root, pageId);
    }
  }

  // ── passed over: on screen long enough to be read, never opened ───────────
  // Each result is reported at most once per results page; the background
  // counts one pass per 30-minute sitting, and opening the page later shows
  // "Seen" instead.
  const passReported = new Set();
  const passTimers = new Map();
  const passObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const pageId = e.target.dataset.nsiPage;
      if (!pageId || passReported.has(pageId)) continue;
      if (e.isIntersecting && e.intersectionRatio >= PASS_VISIBLE) {
        if (!passTimers.has(pageId)) {
          passTimers.set(pageId, setTimeout(() => {
            passTimers.delete(pageId);
            if (passReported.has(pageId) || document.visibilityState !== 'visible') return;
            passReported.add(pageId);
            send({ type: 'passed', pageId, key: currentSearchKey });
          }, PASS_MS));
        }
      } else if (passTimers.has(pageId)) {
        clearTimeout(passTimers.get(pageId));
        passTimers.delete(pageId);
      }
    }
  }, { threshold: [0, PASS_VISIBLE, 1] });

  function watchForPass(root, pageId) {
    if (root.dataset.nsiPage === pageId) return;
    if (root.dataset.nsiPage) passObserver.unobserve(root);
    root.dataset.nsiPage = pageId;
    passObserver.observe(root);
  }

  function renderSearchBanner() {
    const old = document.getElementById('nsi-search-banner');
    const s = currentSearchKey && data.searches[currentSearchKey];
    const list = document.querySelector('[class*="SearchResultPage_Results__"]');
    if (!s || !list || !priorRuns.length) {
      if (old) old.remove();
      return;
    }
    const last = priorRuns[priorRuns.length - 1];
    const opened = s.opened || [];
    const ruledOut = opened.filter((id) => (data.pages[id] || {}).mark === 'nothing').length;
    const clipped = opened.filter((id) => (data.clips[id] || []).length).length;
    const text = 'You ran this search ' + priorRuns.length + '× before (last ' + fmtDate(last.at) +
      (last.count != null ? ', ' + last.count.toLocaleString() + ' results' : '') + '). ' +
      'From it you opened ' + opened.length + ' page' + (opened.length === 1 ? '' : 's') +
      (clipped ? ', clipped on ' + clipped : '') + (ruledOut ? ', ruled out ' + ruledOut : '') + '.';
    if (old && old.textContent === text) return;
    const b = old || document.createElement('div');
    b.id = 'nsi-search-banner';
    b.textContent = text;
    if (!old) list.insertAdjacentElement('beforebegin', b);
  }

  function renderPageBadge(pageId) {
    let box = document.getElementById('nsi-page-badge');
    if (!pageId) {
      if (box) box.remove();
      return;
    }
    if (!box) {
      box = document.createElement('div');
      box.id = 'nsi-page-badge';
      document.body.appendChild(box);
    }
    const key = pageId + '|' + JSON.stringify(data.views[pageId] || 0) + '|' + JSON.stringify(data.clips[pageId] || []) +
      '|' + JSON.stringify(data.pages[pageId] || {}) + '|' + knownSig(pageId);
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.textContent = '';
    box.appendChild(pillsFor(pageId, { thisVisit: true }));
    box.appendChild(markButtons(pageId));
  }

  function knownSig(pageId) {
    const known = archiveKnown();
    const parts = (data.clips[pageId] || []).map((c) => (known['clip:' + c.id] || {}).state || '-');
    return parts.join(',') + '/' + ((known['page:' + pageId] || {}).sid || '');
  }

  // On a clipping's own page (/article/{slug}/{id}/): is it in the archive?
  function articleIdFromPath(path) {
    const m = /^\/(?:article|clip)\/(?:[^/]+\/)?(\d+)\/?/.exec(String(path || ''));
    return m ? m[1] : null;
  }

  function renderArticleBadge(clipId) {
    let box = document.getElementById('nsi-article-badge');
    if (!clipId) {
      if (box) box.remove();
      return;
    }
    const advanced = !!(data.settings && data.settings.advanced);
    if (!advanced) {
      if (box) box.remove();
      return;
    }
    const k = archiveKnown()['clip:' + clipId];
    const hasKnown = Object.keys(archiveKnown()).length > 0;
    const sig = clipId + '|' + JSON.stringify(k || null) + '|' + hasKnown;
    if (box && box.dataset.key === sig) return;
    if (!box) {
      box = document.createElement('div');
      box.id = 'nsi-article-badge';
      document.body.appendChild(box);
    }
    box.dataset.key = sig;
    box.textContent = '';
    const wrap = document.createElement('span');
    wrap.className = 'nsi-pills';
    if (k && k.state === 'filed') wrap.appendChild(pill('nsi-archive', '📚 In archive · ' + (k.sid || ''), 'This clipping is filed in the family archive as ' + (k.sid || 'a source') + '.'));
    else if (k && k.state === 'processed') wrap.appendChild(pill('nsi-archive', '📚 Processed', 'This clipping was processed (filed, or removed as a duplicate of one already filed).'));
    else if (k && k.state === 'captured') wrap.appendChild(pill('nsi-captured', '📥 Captured, not filed', 'Captured into the inbox, waiting to be filed.'));
    else if (hasKnown) wrap.appendChild(pill('nsi-notyet', '○ Not in archive', 'This clipping has not been captured into the family archive.'));
    else wrap.appendChild(pill('nsi-notyet', 'Archive list not loaded', 'The helper has not reported what the archive holds yet (open the add-on and press "Refresh the archive list").'));
    box.appendChild(wrap);
  }

  let renderTimer = null;
  function scheduleRender() {
    clearTimeout(renderTimer);
    renderTimer = setTimeout(render, 250);
  }

  function render() {
    renderSearchResults();
    renderSearchBanner();
    renderPageBadge(pageIdFromHref(location.pathname));
    renderArticleBadge(articleIdFromPath(location.pathname));
  }

  // ── page tracking (the site changes pages without reloading) ──────────────
  let lastHref = null;
  function onLocation() {
    const href = location.pathname + location.search;
    if (href === lastHref) return;
    const prevPath = lastHref && lastHref.split('?')[0];
    lastHref = href;
    const pageId = pageIdFromHref(location.pathname);
    const isSearch = /^\/search\//.test(location.pathname);
    if (pageId && prevPath !== location.pathname) {
      const q = new URLSearchParams(location.search);
      const info = Object.assign({}, pageInfo(pageId) || {}, {
        fromSearch: q.has('search-id') || q.has('terms'),
        terms: q.get('terms') || null,
      });
      send({ type: 'look', pageId, info });
      refreshPageClips(pageId);
    }
    if (prevPath !== location.pathname) dwellStart(pageId);
    // Same page, new address: saving a clipping can add ?clipping_id=… to it.
    if (pageId && prevPath === location.pathname && new URLSearchParams(location.search).has('clipping_id')) {
      refreshPageClips(pageId, PAGE_CLIPS_FORCED_MIN);
    }
    if (isSearch) {
      passReported.clear(); // a new results page or a new search: count passes afresh
      logSearch();
    } else {
      currentSearchKey = null;
      priorRuns = [];
      searchLogged = null; // left the results: coming back is a new run
    }
    if (pageId || isSearch) maybeRefreshClips();
    scheduleRender();
  }

  // The site's "Clipping saved" notice: check this page (and the newest of
  // your clippings list) a moment later, once the save has gone through.
  let clipSavedTimer = null;
  function watchForClipSaved(records) {
    const pid = pageIdFromHref(location.pathname);
    if (!pid && !/^\/article\//.test(location.pathname)) return;
    for (const r of records) {
      for (const n of r.addedNodes) {
        const t = n.nodeType === 1 || n.nodeType === 3 ? n.textContent || '' : '';
        if (t.length < 200 && CLIP_SAVED_RE.test(t)) {
          clearTimeout(clipSavedTimer);
          clipSavedTimer = setTimeout(() => {
            if (pid) refreshPageClips(pid, PAGE_CLIPS_FORCED_MIN);
            data.meta = Object.assign({}, data.meta, { clipsAt: 0 }); // let the list check run now
            maybeRefreshClips();
          }, 1500);
          return;
        }
      }
    }
  }

  (async function start() {
    await loadData();
    onLocation();
    setInterval(onLocation, 1000);
    new MutationObserver((records) => {
      scheduleRender();
      watchForClipSaved(records);
    }).observe(document.body, { childList: true, subtree: true });
    // Back to the tab or window: a clipping may have been made meanwhile.
    const onReturn = () => {
      const pid = pageIdFromHref(location.pathname);
      if (pid && document.visibilityState === 'visible') refreshPageClips(pid, PAGE_CLIPS_RETURN);
    };
    document.addEventListener('visibilitychange', onReturn);
    window.addEventListener('focus', onReturn);
  })();
})();
