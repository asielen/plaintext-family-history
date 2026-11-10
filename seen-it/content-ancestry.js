// content-ancestry.js - "Seen It" on Ancestry
//
// Every Ancestry record has one identity wherever it appears: {dbId}:{recordId}.
// A tree person's Facts page lists its sources as /search/collections/{dbId}/
// records/{recordId}; search results link to the same address; the record page
// itself lives there (older links: /discoveryui-content/view/{recordId}:{dbId}).
// Images are /imageviewer/collections/{dbId}/images/{imageId}. A photo or
// story someone shared to their tree is a media item,
// /mediaui-viewer/tree/{treeId}/person/{pid}/media/{mediaId} - one id
// wherever that photo is offered (med:{mediaId}).
//
// On a record or image page:
//   • records the look (and how long it was on screen),
//   • shows a badge: in the archive (S-id) / captured, not filed / not in the
//     archive, how often you have opened it, and two buttons - "Not ours"
//     (checked, not our person) and "Later".
// Everywhere else (search results, a person's Facts page, hints…):
//   • puts small pills after every link to a record or image you have opened,
//     marked, captured or filed.
// On a person's Hints page:
//   • opening a hint (its title or Review, which slides in Ancestry's side
//     panel without leaving the page) counts as opening that record or photo,
//     and the badge shows for it while the panel is open. Scrolling past a
//     hint counts for nothing.
//
// It never clicks, saves or changes anything on the site.

(function () {
  if (window.__seenItAncLoaded) return;
  window.__seenItAncLoaded = true;

  const send = (msg) => chrome.runtime.sendMessage(msg).catch(() => null);
  let data = { anc: {}, known: {}, ancPeople: {}, settings: {} };

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

  // ── a tree person's sources: how many are saved ───────────────────────────
  // A person's Facts page (/family-tree/person/tree/{treeId}/person/{pid}/facts)
  // lists their Ancestry sources in #sourcesSection. Seen It remembers that
  // list per person, so the tree view can mark each person it has seen.
  function personFromPath(path) {
    const m = /\/family-tree\/person\/tree\/(\d+)\/person\/(\d+)\/facts/.exec(String(path || ''));
    return m ? { tree: m[1], pid: m[2], key: m[1] + ':' + m[2] } : null;
  }

  function treeFromPath(path) {
    const m = /\/family-tree\/tree\/(\d+)\//.exec(String(path || ''));
    return m ? m[1] : null;
  }

  // The person's sources that can be tracked: Ancestry records (rec:…) and
  // Newspapers.com clippings (clip:…). Web links (archive.org, Google Books…)
  // are only counted - nothing can tell whether one is saved.
  function personSourceKeys() {
    const box = document.getElementById('sourcesSection');
    if (!box) return null;
    const keys = [];
    let webLinks = 0;
    for (const a of box.querySelectorAll('a[href]')) {
      const href = a.getAttribute('href') || '';
      const k = keyFromHref(href);
      if (k && (k.startsWith('rec:') || k.startsWith('clip:') || k.startsWith('fag:'))) {
        if (!keys.includes(k)) keys.push(k);
        continue;
      }
      try {
        const u = new URL(href, location.origin);
        const own = /(^|\.)(ancestry|ancestrycdn|newspapers)\.[a-z.]+$/i.test(u.hostname);
        // Web links: off-site pages, plus pasted Ancestry addresses shown in the
        // Web links list (their link text is the address itself).
        if (/^https?:$/.test(u.protocol) && (!own || /^https?:\/\//.test((a.innerText || '').trim().split(/\s/).pop() || ''))) webLinks++;
      } catch (e) {
        /* not a link */
      }
    }
    keys.webLinks = webLinks;
    return keys;
  }

  // Counts of saved / waiting / missing, overall and by kind.
  function coverage(keys) {
    const c = { total: keys.length, filed: 0, captured: 0, missing: 0, rec: [0, 0], clip: [0, 0], fag: [0, 0], webLinks: keys.webLinks || 0 };
    for (const k of keys) {
      const st = (knownOf(k) || {}).state;
      const kind = k.startsWith('clip:') ? 'clip' : k.startsWith('fag:') ? 'fag' : 'rec';
      c[kind][1]++;
      if (st === 'filed' || st === 'processed') {
        c.filed++;
        c[kind][0]++;
      } else if (st === 'captured') {
        c.captured++;
        c[kind][0]++;
      } else c.missing++;
    }
    return c;
  }

  function coverageText(c) {
    if (!c.total) return 'No Ancestry records or clippings';
    if (!c.missing) return '✓ All ' + c.total + ' sources saved';
    return (c.filed + c.captured) + ' of ' + c.total + ' sources saved';
  }

  function coverageTitle(c) {
    const parts = [];
    if (c.rec[1]) parts.push('Ancestry records: ' + c.rec[0] + ' of ' + c.rec[1] + ' saved');
    if (c.clip[1]) parts.push('Newspapers.com clippings: ' + c.clip[0] + ' of ' + c.clip[1] + ' saved');
    if (c.fag[1]) parts.push('Find a Grave memorials: ' + c.fag[0] + ' of ' + c.fag[1] + ' saved');
    parts.push(c.filed + ' in the archive, ' + c.captured + ' captured and waiting to be filed, ' + c.missing + ' not saved');
    if (c.webLinks) parts.push(c.webLinks + ' web link' + (c.webLinks === 1 ? '' : 's') + ' not tracked (save by hand with the capture panel)');
    return parts.join('.\n') + '.';
  }

  let lastPersonSig = null;
  function trackPerson(person) {
    const keys = personSourceKeys();
    if (!keys) return;
    const sig = person.key + '|' + keys.join(',');
    if (sig === lastPersonSig) return;
    lastPersonSig = sig;
    const h1 = document.querySelector('h1');
    send({ type: 'ancPerson', key: person.key, name: h1 ? h1.innerText.trim() : null, keys });
  }

  function renderPersonBadge(person) {
    let box = document.getElementById('nsi-anc-person');
    const keys = person && personSourceKeys();
    if (!person || !keys) {
      if (box) box.remove();
      return;
    }
    const c = coverage(keys);
    const sig = JSON.stringify(c);
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
    wrap.appendChild(pill(c.missing ? (c.filed + c.captured ? 'nsi-captured' : 'nsi-notyet') : 'nsi-archive', coverageText(c), coverageTitle(c)));
    if (c.total && c.missing) {
      if (c.filed) wrap.appendChild(pill('nsi-archive', '📚 ' + c.filed + ' in archive', 'Filed as sources in the family archive.'));
      if (c.captured) wrap.appendChild(pill('nsi-captured', '📥 ' + c.captured + ' captured', 'Captured into the inbox, waiting to be filed.'));
      wrap.appendChild(pill('nsi-notyet', '○ ' + c.missing + ' not saved', 'Not captured yet - queue Ancestry records with the capture panel’s "Add this person’s sources"; a clipping comes in through the Newspapers.com batch.'));
    }
    if (c.webLinks) {
      wrap.appendChild(pill('nsi-notyet', '🔗 ' + c.webLinks + ' web link' + (c.webLinks === 1 ? '' : 's'),
        'Web links (archive.org, Google Books…) are not tracked. Open one and use the capture panel’s Capture to save it.'));
    }
    box.appendChild(wrap);
  }

  // Tree view: person cards are div.node with id "{pid}:{n}".
  function renderTreeNodes(tree) {
    if (!tree) {
      for (const chip of document.querySelectorAll('.nsi-node-chip')) chip.remove();
      return;
    }
    for (const node of document.querySelectorAll('div.node[id]')) {
      const pid = String(node.id).split(':')[0];
      const p = data.ancPeople[tree + ':' + pid];
      const host = node.querySelector('.vAlignContent') || node.querySelector('.nodeContent');
      if (!host) continue;
      let chip = host.querySelector(':scope > .nsi-node-chip');
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
        host.appendChild(chip);
      }
      chip.className = cls;
      chip.textContent = text;
      chip.title = (p.name || 'This person') + ': ' + coverageTitle(c) + ' (sources as of ' + fmtDate(p.at) + ')';
    }
  }

  // ── identities ────────────────────────────────────────────────────────────
  function keyFromPath(path) {
    const p = String(path || '');
    let m = /\/search\/collections\/(\d+)\/records\/(\d+)/.exec(p);
    if (m) return 'rec:' + m[1] + ':' + m[2];
    m = /\/discoveryui-content\/view\/(\d+):(\d+)/.exec(p);
    if (m) return 'rec:' + m[2] + ':' + m[1];
    // A source that opens straight into the image: /interactive/{dbId}/{imageId}/{recordId}
    // (image-only collections such as state voter registers).
    m = /\/interactive\/(\d+)\/[^/?#]+\/(\d+)/.exec(p);
    if (m) return 'rec:' + m[1] + ':' + m[2];
    m = /\/imageviewer\/collections\/(\d+)\/images\/([^/?#]+)/.exec(p);
    if (m) return 'img:' + m[1] + ':' + m[2];
    // A shared photo or story: the media id is the same in every tree that
    // saved it, so the tree and person in front of it are not part of the key.
    m = /\/mediaui-viewer\/(?:[^?#]*\/)?media\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i.exec(p);
    if (m) return 'med:' + m[1].toLowerCase();
    return null;
  }

  function keyFromHref(href) {
    try {
      const u = new URL(href, location.origin);
      // A Newspapers.com clipping attached to a tree person ("Other sources").
      // A Find a Grave memorial, usually in the person's Web links.
      if (/(^|\.)findagrave\.com$/i.test(u.hostname)) {
        const g = /^\/memorial\/(\d+)/.exec(u.pathname);
        return g ? 'fag:' + g[1] : null;
      }
      if (/(^|\.)newspapers\.com$/i.test(u.hostname)) {
        const c = /\/(?:clip|article)\/(?:[^/]+\/)?(\d+)/.exec(u.pathname);
        return c ? 'clip:' + c[1] : null;
      }
      if (!/(^|\.)ancestry\.[a-z.]+$/i.test(u.hostname)) return null;
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

  // `full`: the badge on the record's own page also says "not in archive".
  function pillsFor(key, full, thisVisit) {
    const wrap = document.createElement('span');
    wrap.className = 'nsi-pills';
    const k = knownOf(key);
    const a = data.anc[key] || {};
    const tree = treeOf(key);
    if (tree) wrap.appendChild(treePill(tree));
    const what = key.startsWith('img:') ? 'image' : key.startsWith('clip:') ? 'clipping' : key.startsWith('med:') ? 'photo' : 'record';
    if (k && k.state === 'filed') {
      wrap.appendChild(pill('nsi-archive', '📚 In archive' + (k.sid ? ' · ' + k.sid : ''),
        'This ' + what + ' is filed in the family archive' + (k.sid ? ' as ' + k.sid : '') + '.'));
    } else if (k && k.state === 'processed') {
      wrap.appendChild(pill('nsi-archive', '📚 Processed', 'This ' + what + ' was processed into the archive.'));
    } else if (k && k.state === 'captured') {
      wrap.appendChild(pill('nsi-captured', '📥 Captured, not filed', 'Captured into the inbox (' + (k.where || '') + '), waiting to be filed.'));
    } else if (full && adv()) {
      wrap.appendChild(pill('nsi-notyet', Object.keys(data.known).length ? '○ Not in archive' : 'Archive list not loaded',
        Object.keys(data.known).length ? 'This ' + what + ' has not been captured into the family archive.'
          : 'The helper has not reported what the archive holds yet.'));
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

  // Pills after every link to a record or image, wherever it appears. Only
  // links with something to say get pills, so pages are not cluttered.
  function renderLinks() {
    const here = keyFromPath(location.pathname);
    // In a hint card the title claims its item before any thumbnail can, and
    // another link claims an item only once its pills have somewhere to go -
    // so a bare thumbnail ahead of the title can never leave a hint without
    // pills (#256 review).
    const claims = new Map();
    const claimsOf = (card) => {
      let m = claims.get(card);
      if (!m) {
        m = new Map();
        const t = card.querySelector(HINT_TITLE);
        const tk = t && keyFromHref(t.getAttribute('href'));
        if (tk) m.set(tk, t);
        claims.set(card, m);
      }
      return m;
    };
    for (const a of document.querySelectorAll('a[href*="/records/"], a[href*="/discoveryui-content/view/"], a[href*="/imageviewer/collections/"], a[href*="/interactive/"], a[href*="/mediaui-viewer/"], a[href*="newspapers.com/clip/"], a[href*="newspapers.com/article/"], a[href*="findagrave.com/memorial/"]')) {
      if (a.closest('#nsi-anc-badge')) continue;
      // A hint card links its title, thumbnail and Review button to the same
      // item: each item gets its pills once (a record hint's scan is its own
      // item, so its thumbnail keeps them), never on the Review button.
      const hintCard = a.closest(HINT_CARD);
      let key = keyFromHref(a.getAttribute('href'));
      const linkKey = key;
      // Where the pills go: right after a link with text; for an icon-only link
      // (a tree person's Facts page lists sources as cards - an empty icon link
      // beside a .userCardContent block with the collection title) inside that
      // neighbouring block; anything else (a bare thumbnail) gets none.
      let place = null;
      if ((a.textContent || '').trim()) {
        place = { existing: a.nextElementSibling, put: (el) => a.insertAdjacentElement('afterend', el) };
      } else {
        const box = (a.closest('.userCard') && a.closest('.userCard').querySelector('.userCardContent')) ||
          (a.nextElementSibling && (a.nextElementSibling.textContent || '').trim() ? a.nextElementSibling : null);
        if (box) place = { existing: box.querySelector(':scope > .nsi-pills[data-nsi-for]'), put: (el) => box.appendChild(el) };
      }
      if (hintCard && key) {
        const m = claimsOf(hintCard);
        const owner = m.get(key);
        if (a.classList.contains('ancBtn') || (owner && owner !== a) || (!owner && !place)) key = null;
        else m.set(key, a);
      }
      const cand = place && place.existing;
      let existing = cand && cand.classList && cand.classList.contains('nsi-pills') && cand.dataset.nsiFor ? cand : null;
      // Inside a hint card a link touches only the pills it put there itself,
      // so the title and the thumbnail never take each other's down.
      if (existing && hintCard && existing.dataset.nsiOwner !== ownerOf(a, linkKey)) existing = null;
      if (!key || key === here || !place) {
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
      if (hintCard) pills.dataset.nsiOwner = ownerOf(a, linkKey);
      place.put(pills);
      const row = a.closest('[class*="result"], [class*="Result"], li, tr');
      if (row) row.classList.toggle('nsi-ruled-out', (data.anc[key] || {}).mark === 'nothing');
    }
  }

  // Whose pills these are inside a hint card: the item and which link (the
  // title, or another link such as a record hint's scan). Said in words, not
  // tied to the link element, so when Ancestry redraws a link the new one
  // picks up its pills instead of adding another set (#256 review).
  function ownerOf(a, key) {
    const isTitle = a.classList.contains('hintCardContentHeader') || a.classList.contains('recordCardV2CollectionTitle');
    return (isTitle ? 'title|' : 'link|') + (key || '');
  }

  let renderTimer = null;
  function scheduleRender() {
    clearTimeout(renderTimer);
    renderTimer = setTimeout(render, 300);
  }

  function render() {
    renderBadge(keyFromPath(location.pathname) || openHint);
    renderLinks();
    const person = personFromPath(location.pathname);
    if (person) trackPerson(person);
    // Saved-sources coverage is about the family archive: advanced mode only.
    renderPersonBadge(adv() ? person : null);
    renderTreeNodes(adv() ? treeFromPath(location.pathname) : null);
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

  function pageInfo() {
    const h1 = document.querySelector('h1');
    return {
      name: h1 ? h1.innerText.trim() : null,
      collection: String(document.title || '').replace(/\s*-\s*Ancestry(\.com)?\s*$/i, '').trim() || null,
    };
  }

  // ── a hint opened in the side panel ───────────────────────────────────────
  // On a person's Hints page a click on a hint's title or Review slides in
  // Ancestry's side panel; the page address does not change. That click is
  // the look (and the reading time runs until the panel closes). A hint only
  // scrolled past is never counted. A click that opens a new tab (Ctrl, ⌘,
  // Shift or the middle button) is left to that tab, which counts the page it
  // opens, so one look is not counted twice.
  const HINT_CARD = 'section.extHintCard';
  // The title link: a photo hint's heading, a record hint's collection title.
  const HINT_TITLE = '.hintCardContentHeader, .recordCardV2CollectionTitle';
  let openHint = null;
  let openHintAt = 0;
  let openHintPanelSeen = false;
  const panelOpen = () => !!document.querySelector('aside.sidebar:not(.noDisplay)');

  function onHintClick(e) {
    if (e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    const a = e.target && e.target.closest && e.target.closest('a[href]');
    const card = a && a.closest(HINT_CARD);
    if (!card) return;
    // Whatever was clicked in the card (title, Review, a record hint's scan),
    // the panel opens that card's hint: count its title's record or photo.
    // Only a link to the record, its scan or the photo opens the hint - not
    // Ignore, a help link or someone's tree page (#256 review).
    const own = keyFromHref(a.getAttribute('href'));
    if (!own) return;
    const header = card.querySelector(HINT_TITLE);
    const key = (header && keyFromHref(header.getAttribute('href'))) || own;
    const title = header ? (header.innerText || header.textContent || '').trim() : '';
    openHint = key;
    openHintAt = Date.now();
    openHintPanelSeen = false;
    dwellFlush();
    dwellKey = key;
    dwellSince = document.visibilityState === 'visible' ? Date.now() : 0;
    send({ type: 'ancLook', key, info: { title: title || null } });
    scheduleRender();
  }
  document.addEventListener('click', onHintClick, true);

  function checkHintPanel() {
    if (!openHint) return;
    if (panelOpen()) {
      openHintPanelSeen = true;
      return;
    }
    // Not open: closed again - or, on a slow connection, not open yet. Wait
    // for it up to 10 seconds before giving up on it (#256 review).
    if (!openHintPanelSeen && Date.now() - openHintAt < 10000) return;
    dwellFlush();
    openHint = null;
    dwellKey = keyFromPath(location.pathname);
    dwellSince = dwellKey && document.visibilityState === 'visible' ? Date.now() : 0;
    scheduleRender();
  }

  let lastPath = null;
  function onLocation() {
    checkHintPanel();
    if (location.pathname === lastPath) return;
    openHint = null;
    lastPath = location.pathname;
    const key = keyFromPath(location.pathname);
    dwellFlush();
    dwellKey = key;
    dwellSince = key && document.visibilityState === 'visible' ? Date.now() : 0;
    if (key) {
      // The heading and title arrive with the page; give them a moment.
      setTimeout(() => send({ type: 'ancLook', key, info: pageInfo() }), 1500);
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
