// content-familysearch.js - "Seen It" on FamilySearch
//
// A FamilySearch record entry has one identity wherever it appears: its ARK,
// ark:/61903/1:1:{id} (key fsrec:{id}); its image is ark:/61903/3:1:{id}
// (fsimg:{id}); a tree person is /tree/person/…/{PID}.
//
// On a record or image page: records the look, and shows a badge - in the
// archive (S-id) / captured, not filed / not in the archive, how often you have
// opened it, and "Not ours" / "Later".
// Everywhere else (search results, other records' "people on this record"…):
// small pills after every link to a record you have opened, marked, captured or
// filed.
// On a tree person's page: how many of their FamilySearch sources are saved
// (read through FamilySearch's own data service - the Sources tab shows no
// links), remembered so the tree view can show a chip on each person's card.
//
// It never clicks, saves or changes anything on the site.

(function () {
  if (window.__seenItFsLoaded) return;
  window.__seenItFsLoaded = true;

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
  // Without advanced mode the add-on stands alone: nothing about the family
  // archive is shown, only your own looks, marks and tree.
  const adv = () => !!(data.settings && data.settings.advanced);
  const knownOf = (key) => (adv() ? data.known[key] : null);

  // Which tree people each record is attached to, from every person whose page
  // you have opened (Ancestry Facts page, FamilySearch person page) and from a
  // GEDCOM file loaded in the toolbar window.
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

  function treePill(names) {
    const more = names.length > 1 ? ' +' + (names.length - 1) : '';
    return pill('nsi-tree', '🌳 ' + names[0] + more,
      'Attached to ' + (names.length === 1 ? 'this person' : 'these people') + ' in your tree:\n' +
      names.map((n) => '• ' + n).join('\n') +
      '\n(Known from the person pages you have opened, and any tree file loaded in Seen It.)');
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
    const p = String(path || '');
    let m = /\/ark:\/61903\/1:1:([A-Z0-9-]+)/i.exec(p);
    if (m) return 'fsrec:' + m[1].toUpperCase();
    m = /\/ark:\/61903\/3:1:([A-Z0-9-]+)/i.exec(p);
    if (m) return 'fsimg:' + m[1].toUpperCase();
    return null;
  }

  function keyFromHref(href) {
    try {
      const u = new URL(href, location.origin);
      if (!/(^|\.)familysearch\.org$/i.test(u.hostname)) return null;
      return keyFromPath(u.pathname);
    } catch (e) {
      return null;
    }
  }

  function personFromPath(path) {
    const m = /\/tree\/person\/(?:[a-z-]+\/)?([A-Z0-9]{4}-[A-Z0-9]{2,4})\b/i.exec(String(path || ''));
    return m ? { pid: m[1].toUpperCase(), key: 'fs:' + m[1].toUpperCase() } : null;
  }

  function looksOf(a) {
    return a && a.t ? a.t.length : 0;
  }

  // ── FamilySearch's own data service, in the human's session ───────────────
  function fsToken() {
    const m = /(?:^|;\s*)fssessionid=([^;]+)/.exec(document.cookie || '');
    return m ? decodeURIComponent(m[1]) : null;
  }

  async function fsJson(path) {
    const tok = fsToken();
    const headers = { Accept: 'application/x-fs-v1+json' };
    if (tok) headers.Authorization = 'Bearer ' + tok;
    const r = await fetch(path, { credentials: 'include', headers });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    // 204 No Content: nothing there (a person with no sources at all).
    if (r.status === 204) return {};
    const text = await r.text();
    return text ? JSON.parse(text) : {};
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

  function pillsFor(key, full, thisVisit) {
    const wrap = document.createElement('span');
    wrap.className = 'nsi-pills';
    const k = knownOf(key);
    const a = data.anc[key] || {};
    const tree = treeOf(key);
    if (tree) wrap.appendChild(treePill(tree));
    const what = key.startsWith('fsimg:') ? 'image' : 'record';
    if (k && k.state === 'filed') {
      wrap.appendChild(pill('nsi-archive', '📚 In archive' + (k.sid ? ' · ' + k.sid : ''),
        'This ' + what + ' is filed in the family archive' + (k.sid ? ' as ' + k.sid : '') + '.'));
    } else if (k && k.state === 'processed') {
      wrap.appendChild(pill('nsi-archive', '📚 Processed', 'This ' + what + ' was processed into the archive.'));
    } else if (k && k.state === 'captured') {
      wrap.appendChild(pill('nsi-captured', '📥 Captured, not filed', 'Captured into the inbox (' + (k.where || '') + '), waiting to be filed.'));
    } else if (full && adv()) {
      const loaded = Object.keys(data.known).length > 0;
      wrap.appendChild(pill('nsi-notyet', loaded ? '○ Not in archive' : 'Archive list not loaded',
        loaded ? 'This ' + what + ' has not been captured into the family archive.' : 'The helper has not reported what the archive holds yet.'));
    }
    if (a.mark === 'nothing') wrap.appendChild(pill('nsi-nothing', '✗ Not ours', 'You marked this ' + what + ': not our person' + (a.markAt ? ' (' + fmtDate(a.markAt) + ')' : '') + '.'));
    if (a.mark === 'later') wrap.appendChild(pill('nsi-later', '↻ Later', 'You marked this ' + what + ' to come back to' + (a.markAt ? ' (' + fmtDate(a.markAt) + ')' : '') + '.'));
    const looks = looksOf(a);
    if (looks) {
      const prev = thisVisit ? (a.t.length > 1 ? a.t[a.t.length - 2] : 0) : a.last;
      wrap.appendChild(pill('nsi-seen', (thisVisit ? 'Opened ' : 'Seen ') + looks + '×' + (prev ? ' · last ' + fmtDate(prev) : ''),
        'You have opened this ' + what + ' ' + looks + ' time' + (looks === 1 ? '' : 's') +
        (a.dwellMs ? '; about ' + Math.max(1, Math.round(a.dwellMs / 60000)) + ' min on it in all' : '') + '.'));
    }
    return wrap;
  }

  function sigFor(key) {
    return key + '|' + JSON.stringify(knownOf(key) || 0) + '|' + JSON.stringify(data.anc[key] || 0) + '|' + (treeOf(key) || []).join(',');
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

  function renderLinks() {
    const here = keyFromPath(location.pathname);
    for (const a of document.querySelectorAll('a[href*="ark:/61903/"]')) {
      if (a.closest('#nsi-anc-badge, #nsi-anc-person')) continue;
      const key = keyFromHref(a.getAttribute('href'));
      const next = a.nextElementSibling;
      const existing = next && next.classList && next.classList.contains('nsi-pills') && next.dataset.nsiFor ? next : null;
      if (!key || key === here || !(a.textContent || '').trim()) {
        if (existing) existing.remove();
        continue;
      }
      const has = knownOf(key) || treeOf(key) || looksOf(data.anc[key]) || (data.anc[key] || {}).mark;
      const sig = sigFor(key);
      if (existing && existing.dataset.key === sig) continue;
      if (existing) existing.remove();
      if (!has) continue;
      const pills = pillsFor(key, false, false);
      pills.dataset.key = sig;
      pills.dataset.nsiFor = key;
      a.insertAdjacentElement('afterend', pills);
    }
  }

  // ── a tree person's sources: how many are saved ───────────────────────────
  function coverage(keys) {
    keys = keys.filter((k) => k.startsWith('fsrec:'));
    const c = { total: keys.length, filed: 0, captured: 0, missing: 0 };
    for (const k of keys) {
      const st = (knownOf(k) || {}).state;
      if (st === 'filed' || st === 'processed') c.filed++;
      else if (st === 'captured') c.captured++;
      else c.missing++;
    }
    return c;
  }

  function coverageText(c) {
    if (!c.total) return 'No FamilySearch records attached';  // the "other" pill says what there is instead
    if (!c.missing) return '✓ All ' + c.total + ' sources saved';
    return (c.filed + c.captured) + ' of ' + c.total + ' sources saved';
  }

  function coverageTitle(c, other) {
    return c.total + ' FamilySearch record' + (c.total === 1 ? '' : 's') + ': ' + c.filed + ' in the archive, ' +
      c.captured + ' captured and waiting to be filed, ' + c.missing + ' not saved.' +
      (other ? '\n' + other + ' other source' + (other === 1 ? '' : 's') + ' (web pages, notes) are not tracked.' : '');
  }

  const personRead = {}; // PID -> { at, keys, other } read this page visit
  async function readPerson(person) {
    const prev = personRead[person.pid];
    if (prev && Date.now() - prev.at < 60 * 1000) return prev;
    personRead[person.pid] = { at: Date.now(), keys: prev ? prev.keys : null, other: 0 };
    try {
      const j = await fsJson('/platform/tree/persons/' + person.pid + '/sources');
      const keys = [];
      let other = 0;
      for (const s of j.sourceDescriptions || []) {
        const k = keyFromHref(s.about || '');
        const g = /findagrave\.com\/memorial\/(\d+)/i.exec(s.about || '');
        if (k && k.startsWith('fsrec:')) {
          if (!keys.includes(k)) keys.push(k);
        } else if (g) {
          // a Find a Grave memorial: remembered for the 🌳 pill on findagrave.com
          if (!keys.includes('fag:' + g[1])) keys.push('fag:' + g[1]);
          other++;
        } else other++;
      }
      personRead[person.pid] = { at: Date.now(), keys, other };
      const h1 = document.querySelector('h1');
      send({ type: 'ancPerson', key: person.key, name: h1 ? h1.innerText.trim().split('\n')[0] : null, keys });
      scheduleRender();
    } catch (e) {
      /* not signed in, or the person is private: show nothing */
    }
    return personRead[person.pid];
  }

  function renderPersonBadge(person) {
    let box = document.getElementById('nsi-anc-person');
    const read = person && personRead[person.pid];
    if (!person || !read || !read.keys) {
      if (box) box.remove();
      return;
    }
    const c = coverage(read.keys);
    const sig = JSON.stringify(c) + '|' + read.other;
    if (box && box.dataset.key === sig) return;
    if (!box) {
      box = document.createElement('div');
      box.id = 'nsi-anc-person';
      document.body.appendChild(box);
    }
    box.dataset.key = sig;
    box.textContent = '';
    const wrap = document.createElement('span');
    wrap.className = 'nsi-pills';
    wrap.appendChild(pill(c.missing ? (c.filed + c.captured ? 'nsi-captured' : 'nsi-notyet') : 'nsi-archive', coverageText(c), coverageTitle(c, read.other)));
    if (c.total && c.missing) {
      if (c.filed) wrap.appendChild(pill('nsi-archive', '📚 ' + c.filed + ' in archive', 'Filed as sources in the family archive.'));
      if (c.captured) wrap.appendChild(pill('nsi-captured', '📥 ' + c.captured + ' captured', 'Captured into the inbox, waiting to be filed.'));
      wrap.appendChild(pill('nsi-notyet', '○ ' + c.missing + ' not saved', 'Not captured yet - queue them with the capture panel’s "Add this person’s sources" (FamilySearch batch).'));
    }
    if (read.other) wrap.appendChild(pill('nsi-notyet', '🔗 ' + read.other + ' other', 'Sources that are not FamilySearch records (web pages, notes) are not tracked.'));
    box.appendChild(wrap);
  }

  // Tree view: each person card shows its PID (like "ABCD-123") as text.
  function renderTreeChips() {
    if (!/\/tree\/(pedigree|fan|descendancy|portrait|landscape)/i.test(location.pathname) && !/\/tree\/?$/.test(location.pathname)) return;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const hits = [];
    let n;
    while ((n = walker.nextNode())) {
      const t = (n.nodeValue || '').trim();
      if (/^[A-Z0-9]{4}-[A-Z0-9]{2,4}$/.test(t) && n.parentElement && !n.parentElement.closest('.nsi-pills')) hits.push(n.parentElement);
    }
    for (const el of hits) {
      const pid = el.textContent.trim();
      const p = data.ancPeople['fs:' + pid];
      let chip = el.nextElementSibling && el.nextElementSibling.classList.contains('nsi-node-chip') ? el.nextElementSibling : null;
      if (!p) {
        if (chip) chip.remove();
        continue;
      }
      const c = coverage(p.keys || []);
      const text = c.total ? (c.missing ? (c.filed + c.captured) + '/' + c.total : '✓ ' + c.total + '/' + c.total) : '–';
      const cls = 'nsi-node-chip ' + (c.missing ? (c.filed + c.captured ? 'nsi-node-part' : 'nsi-node-none') : 'nsi-node-all');
      if (chip && chip.textContent === text && chip.className === cls) continue;
      if (!chip) {
        chip = document.createElement('span');
        el.insertAdjacentElement('afterend', chip);
      }
      chip.className = cls;
      chip.textContent = text;
      chip.title = (p.name || 'This person') + ': ' + coverageTitle(c) + ' (sources as of ' + fmtDate(p.at) + ')';
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
    const person = personFromPath(location.pathname);
    if (person) readPerson(person);
    // Saved-sources coverage is about the family archive: advanced mode only.
    renderPersonBadge(adv() ? person : null);
    if (adv()) renderTreeChips();
    else for (const chip of document.querySelectorAll('.nsi-node-chip')) chip.remove();
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

  let lastPath = null;
  function onLocation() {
    if (location.pathname === lastPath) return;
    lastPath = location.pathname;
    const key = keyFromPath(location.pathname);
    dwellFlush();
    dwellKey = key;
    dwellSince = key && document.visibilityState === 'visible' ? Date.now() : 0;
    if (key) {
      setTimeout(() => {
        const h1 = document.querySelector('h1');
        const title = String(document.title || '');
        const coll = /"([^"]+)"/.exec(title);
        send({ type: 'ancLook', key, info: { name: h1 ? h1.innerText.trim() : null, collection: coll ? coll[1] : null } });
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
