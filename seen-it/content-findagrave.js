// content-findagrave.js - "Seen It" on Find a Grave
//
// A memorial has one identity wherever it appears: its memorial ID,
// /memorial/{id}/{slug} (key fag:{id}).
//
// On a memorial (and its photo pages): records the look, and shows a badge -
// attached to your tree, how often you have opened it, "Not ours" / "Later",
// and (advanced mode) whether the family archive holds it.
// Everywhere else (search results, cemetery listings, a memorial's family
// links): small pills after every link to a memorial you have opened, marked,
// linked from your tree, or (advanced) captured or filed.
//
// It never clicks, saves or changes anything on the site.

(function () {
  if (window.__seenItFagLoaded) return;
  window.__seenItFagLoaded = true;

  const send = (msg) => chrome.runtime.sendMessage(msg).catch(() => null);
  const data = { anc: {}, known: {}, ancPeople: {}, settings: {} };

  async function loadData() {
    const o = await chrome.storage.local.get(['anc', 'known', 'ancPeople', 'settings']);
    data.anc = o.anc || {};
    data.known = o.known || {};
    data.ancPeople = o.ancPeople || {};
    data.settings = o.settings || {};
    treeIdx = null;
  }

  // ── advanced mode and "in your tree" ──────────────────────────────────────
  const adv = () => !!(data.settings && data.settings.advanced);
  const knownOf = (key) => (adv() ? data.known[key] : null);

  // Which tree people link to each memorial: from the person pages you have
  // opened (an Ancestry person's web links, a FamilySearch person's sources)
  // and from a GEDCOM file loaded in the toolbar window.
  let treeIdx = null;
  function treeOf(key) {
    if (!treeIdx) {
      treeIdx = {};
      for (const p of Object.values(data.ancPeople || {})) {
        for (const k of p.keys || []) {
          const list = treeIdx[k] || (treeIdx[k] = []);
          const name = p.name || 'someone in your tree';
          if (!list.includes(name)) list.push(name);
        }
      }
    }
    return treeIdx[key] || null;
  }

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    if (changes.anc) data.anc = changes.anc.newValue || {};
    if (changes.known) data.known = changes.known.newValue || {};
    if (changes.ancPeople) {
      data.ancPeople = changes.ancPeople.newValue || {};
      treeIdx = null;
    }
    if (changes.settings) data.settings = changes.settings.newValue || {};
    if (changes.anc || changes.known || changes.ancPeople || changes.settings) scheduleRender();
  });

  // ── identities ────────────────────────────────────────────────────────────
  function keyFromPath(path) {
    const m = /^\/memorial\/(\d+)/.exec(String(path || ''));
    return m ? 'fag:' + m[1] : null;
  }

  function keyFromHref(href) {
    try {
      const u = new URL(href, location.origin);
      if (!/(^|\.)findagrave\.com$/i.test(u.hostname)) return null;
      return keyFromPath(u.pathname);
    } catch (e) {
      return null;
    }
  }

  function looksOf(a) {
    return a && a.t ? a.t.length : 0;
  }

  // ── rendering ─────────────────────────────────────────────────────────────
  function fmtDate(ms) {
    const d = new Date(ms);
    const sameYear = d.getFullYear() === new Date().getFullYear();
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: sameYear ? undefined : 'numeric' });
  }

  function pill(cls, text, title) {
    const el = document.createElement('span');
    el.className = 'nsi-pill ' + cls;
    el.textContent = text;
    if (title) el.title = title;
    return el;
  }

  function treePill(names) {
    const more = names.length > 1 ? ' +' + (names.length - 1) : '';
    return pill('nsi-tree', '🌳 ' + names[0] + more,
      'Linked from ' + (names.length === 1 ? 'this person' : 'these people') + ' in your tree:\n' +
      names.map((n) => '• ' + n).join('\n') +
      '\n(Known from the person pages you have opened, and any tree file loaded in Seen It.)');
  }

  function pillsFor(key, full, thisVisit) {
    const wrap = document.createElement('span');
    wrap.className = 'nsi-pills';
    const k = knownOf(key);
    const idx = knownOf('fagidx:' + key.slice(4));
    const a = data.anc[key] || {};
    const tree = treeOf(key);
    if (tree) wrap.appendChild(treePill(tree));
    if (k && k.state === 'filed') {
      wrap.appendChild(pill('nsi-archive', '📚 In archive' + (k.sid ? ' · ' + k.sid : ''),
        'This memorial is filed in the family archive' + (k.sid ? ' as ' + k.sid : '') + '.'));
    } else if (k && k.state === 'processed') {
      wrap.appendChild(pill('nsi-archive', '📚 Processed', 'This memorial was processed into the archive.'));
    } else if (k && k.state === 'captured') {
      wrap.appendChild(pill('nsi-captured', '📥 Captured, not filed', 'Captured into the inbox (' + (k.where || '') + '), waiting to be filed.'));
    } else if (idx) {
      wrap.appendChild(pill('nsi-captured', '📑 Index in archive' + (idx.sid ? ' · ' + idx.sid : ''),
        'The archive cites this memorial' + (idx.sid ? ' (' + idx.sid + ')' : '') + ', but the memorial itself (its photos) has not been captured.'));
    } else if (full && adv()) {
      const loaded = Object.keys(data.known).length > 0;
      wrap.appendChild(pill('nsi-notyet', loaded ? '○ Not in archive' : 'Archive list not loaded',
        loaded ? 'This memorial has not been captured into the family archive.' : 'The helper has not reported what the archive holds yet.'));
    }
    if (a.mark === 'nothing') wrap.appendChild(pill('nsi-nothing', '✗ Not ours', 'You marked this memorial: not our person' + (a.markAt ? ' (' + fmtDate(a.markAt) + ')' : '') + '.'));
    if (a.mark === 'later') wrap.appendChild(pill('nsi-later', '↻ Later', 'You marked this memorial to come back to' + (a.markAt ? ' (' + fmtDate(a.markAt) + ')' : '') + '.'));
    const looks = looksOf(a);
    if (looks) {
      const prev = thisVisit ? (a.t.length > 1 ? a.t[a.t.length - 2] : 0) : a.last;
      wrap.appendChild(pill('nsi-seen', (thisVisit ? 'Opened ' : 'Seen ') + looks + '×' + (prev ? ' · last ' + fmtDate(prev) : ''),
        'You have opened this memorial ' + looks + ' time' + (looks === 1 ? '' : 's') +
        (a.dwellMs ? '; about ' + Math.max(1, Math.round(a.dwellMs / 60000)) + ' min on it in all' : '') + '.'));
    }
    return wrap;
  }

  function sigFor(key) {
    return key + '|' + JSON.stringify(knownOf(key) || 0) + '|' + JSON.stringify(knownOf('fagidx:' + key.slice(4)) || 0) +
      '|' + JSON.stringify(data.anc[key] || 0) + '|' + (treeOf(key) || []).join(',');
  }

  function markButtons(key) {
    const a = data.anc[key] || {};
    const box = document.createElement('span');
    box.className = 'nsi-marks';
    const mk = (mark, label, title) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'nsi-mark' + (a.mark === mark ? ' nsi-on' : '');
      b.textContent = label;
      b.title = title;
      b.addEventListener('click', (e) => {
        e.preventDefault();
        e.stopPropagation();
        send({ type: 'ancMark', key, mark: a.mark === mark ? null : mark });
      });
      return b;
    };
    box.appendChild(mk('nothing', '✗ Not ours', 'Checked: this is not our person (click again to clear).'));
    box.appendChild(mk('later', '↻ Later', 'Come back to this one (click again to clear).'));
    return box;
  }

  function renderBadge(key) {
    let box = document.getElementById('nsi-anc-badge');
    if (!key) {
      if (box) box.remove();
      return;
    }
    if (!box) {
      box = document.createElement('div');
      box.id = 'nsi-anc-badge';
      document.body.appendChild(box);
    }
    const sig = sigFor(key);
    if (box.dataset.key === sig) return;
    box.dataset.key = sig;
    box.textContent = '';
    box.appendChild(pillsFor(key, true, true));
    box.appendChild(markButtons(key));
  }

  // Pills go after the memorial's name inside the link card (search results and
  // family lists wrap a whole card in one link), else right after the link.
  function renderLinks() {
    const here = keyFromPath(location.pathname);
    for (const a of document.querySelectorAll('a[href*="/memorial/"]')) {
      if (a.closest('#nsi-anc-badge')) continue;
      const key = keyFromHref(a.getAttribute('href'));
      const nameEl = a.querySelector('[itemprop="name"], h2, h3');
      const existing = nameEl
        ? nameEl.querySelector(':scope > .nsi-pills[data-nsi-for]')
        : a.nextElementSibling && a.nextElementSibling.classList && a.nextElementSibling.classList.contains('nsi-pills') &&
          a.nextElementSibling.dataset.nsiFor ? a.nextElementSibling : null;
      if (!key || key === here || !(a.textContent || '').trim() || /\/(photo|flower|edit)\b/.test(a.getAttribute('href') || '')) {
        if (existing) existing.remove();
        continue;
      }
      const has = knownOf(key) || knownOf('fagidx:' + key.slice(4)) || treeOf(key) || looksOf(data.anc[key]) || (data.anc[key] || {}).mark;
      const sig = sigFor(key);
      if (existing && existing.dataset.key === sig) continue;
      if (existing) existing.remove();
      if (!has) continue;
      const pills = pillsFor(key, false, false);
      pills.dataset.key = sig;
      pills.dataset.nsiFor = key;
      if (nameEl) nameEl.appendChild(pills);
      else a.insertAdjacentElement('afterend', pills);
      const row = a.closest('.memorial-item, li, tr');
      if (row) row.classList.toggle('nsi-ruled-out', (data.anc[key] || {}).mark === 'nothing');
    }
  }

  let renderTimer = null;
  function scheduleRender() {
    clearTimeout(renderTimer);
    renderTimer = setTimeout(render, 300);
  }

  function render() {
    renderBadge(keyFromPath(location.pathname));
    renderLinks();
  }

  // ── looks and reading time ────────────────────────────────────────────────
  let dwellKey = null;
  let dwellSince = 0;
  function dwellFlush() {
    if (dwellKey && dwellSince) {
      const ms = Date.now() - dwellSince;
      if (ms > 500) send({ type: 'ancDwell', key: dwellKey, ms });
    }
    dwellSince = dwellKey && document.visibilityState === 'visible' ? Date.now() : 0;
  }
  document.addEventListener('visibilitychange', dwellFlush);
  window.addEventListener('pagehide', dwellFlush);
  setInterval(dwellFlush, 30000);

  // `undefined` until the first check. (null would match the key of every
  // page that is not a memorial - search results, cemetery listings - so the
  // first check found "nothing changed" and never drew the pills there.)
  let lastPath;
  let lastKey;
  function onLocation() {
    if (location.pathname === lastPath) return;
    lastPath = location.pathname;
    scheduleRender();
    const key = keyFromPath(location.pathname);
    if (key === lastKey) return; // a memorial's photo page: the same memorial
    lastKey = key;
    dwellFlush();
    dwellKey = key;
    dwellSince = key && document.visibilityState === 'visible' ? Date.now() : 0;
    if (key) {
      setTimeout(() => {
        const h1 = document.getElementById('bio-name') || document.querySelector('h1');
        const cem = document.getElementById('cemeteryNameLabel');
        send({ type: 'ancLook', key, info: {
          name: h1 ? h1.innerText.trim() : null,
          collection: 'Find a Grave' + (cem ? ': ' + cem.innerText.trim() : ''),
        } });
      }, 1500);
    }
    scheduleRender();
  }

  (async function start() {
    await loadData();
    onLocation();
    setInterval(onLocation, 1000);
    new MutationObserver(scheduleRender).observe(document.body, { childList: true, subtree: true });
  })();
})();
