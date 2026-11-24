// wikitree-record.js - the pure, importable core of capturing a WikiTree page:
// which page an address is (a person's profile or a free-space page), what
// WikiTree says about it (names, dates, places, last edit, family as WikiTree
// IDs, the biography's own text), the cited sources as a list, which of those
// sources are files WikiTree itself holds, and the citation.
//
// Why it matters to him: a WikiTree profile is saved with its ID and the day it
// was last edited, and its Sources section becomes a list he can tick through,
// "chase every cited source" as a checklist instead of a re-read. A scan or a
// transcription someone uploaded to WikiTree as a source comes along too.
//
// The browser-side content.js DOES NOT import this file (it is an injected
// classic script that cannot use ES imports); it carries a COPY of the block
// between `FHA-SYNC-BEGIN wikitree` / `FHA-SYNC-END wikitree`, and
// tests/test-sync.js fails if the two drift. Change it here first, then mirror.
//
// What WikiTree gives (checked live 2026-11-24 on a public profile of a man
// born 1681, and on a public free-space page): the public API
// (api.wikitree.com/api.php, action=getProfile, bioFormat=wiki) answers for a
// public profile or free-space page with no login. A profile's answer has
// `Touched` (YYYYMMDDhhmmss), `Parents` keyed by id, `Spouses` keyed 0, 1, …
// with `marriage_date`, `Children`, and the biography's wiki text under the
// lower-case key `bio`. A free-space page answers with `Title.Text` and `bio`.
// action=getPhotos lists a profile's photos (`ImageName`, `Title`, `URL` = the
// photo's own page, `URL_300` = a thumbnail whose path, less `/thumb` and the
// size part, is the original file). A file's own page (/photo/{ext}/{name})
// shows its caption in the <h1> and links the original as
// /photo.php/{h}/{hh}/{name}.
//
// NO LOGIN: every request goes without cookies (`credentials: 'omit'`), so
// only what WikiTree shows anyone is saved. Files are read only from
// www.wikitree.com; a source held elsewhere (Ancestry, FamilySearch, a
// newspaper site) stays a link to chase, never fetched.
//
// Code map:
//   wtParsePage          profile or free-space page, from an address
//   wtApiUrl             the one getProfile (or getPhotos) request
//   wtRecordFromApi      what the API says, as the record the capture keeps
//   wtRecordFromPage     the same from the page itself, when the API refuses
//   wtSources            the cited sources in the wiki text, de-duplicated
//   wtSourcesFromPage    the same from the page's Sources section
//   wtHostedFiles        which cited sources are files WikiTree itself holds
//   wtFileFromPage       a held file's caption and original, from its own page
//   wtPhotosFromApi      a profile's photos, each with its original file
//   wtOriginalUrlOk      is this an original file on www.wikitree.com?
//   wtPlainText          wiki text as plain text (the biography, a transcription)
//   wtCitation           the citation line
//   wtCitedSourcesNotes  the "Cited sources" list for the notes
//   wtFetchFile          (panel side, below the block) download one original
//
// Loaded two ways: content.js carries a copy of the block; panel.html loads
// this whole file as a classic script (`FHA.wikitree`), and node tests
// require it.

'use strict';

// FHA-SYNC-BEGIN wikitree
const WT_HOST = 'www.wikitree.com';
const WT_ORIGIN = 'https://www.wikitree.com';
const WT_API = 'https://api.wikitree.com/api.php';
const WT_APP_ID = 'fha-capture';
const WT_MAX_PHOTOS = 10;
const WT_MAX_HOSTED = 10;
const WT_PROFILE_FIELDS = 'Id,PageId,Name,FirstName,MiddleName,LastNameAtBirth,LastNameCurrent,Nicknames,' +
  'Prefix,Suffix,RealName,BirthDate,DeathDate,BirthLocation,DeathLocation,Gender,IsLiving,Touched,Created,' +
  'Father,Mother,Parents,Spouses,Children,Bio,Title';
// Pages that are WikiTree's own, never a person: [[Space:…]] is a free-space
// page (it may hold a transcription), [[Image:…]] / [[File:…]] an uploaded
// file; the rest are help, categories and the like.
// A sub-heading under Sources that holds thanks or research notes, not sources.
const WT_NOT_SOURCES_RE = /^(acknowledge?ments?|thanks|thank you|credits?|research notes?|see also|disclaimers?)\s*:?$/i;
const WT_NAMESPACES = /^(space|image|file|category|help|project|template|special|docs|wikitree|user|media)$/i;
// The original of an uploaded file: /photo.php/{h}/{hh}/{name}, never a /thumb/.
const WT_ORIGINAL_PATH_RE = /^\/photo\.php\/[0-9a-f]\/[0-9a-f]{2}\/[^/]+\.[A-Za-z0-9]{2,5}$/;

// The page an address is: { kind: 'profile', wikitreeId, key, url } for
// /wiki/{Surname-123}, { kind: 'space', spaceName, key, url } for
// /wiki/Space:{name}, or null for anything else (a search, a category, a
// photo page, another site).
function wtParsePage(href) {
  let u;
  try {
    u = new URL(href);
  } catch (e) {
    return null;
  }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') return null;
  const host = u.hostname.toLowerCase();
  if ((host !== WT_HOST && host !== 'wikitree.com') || u.port || u.username || u.password) return null;
  const m = /^\/wiki\/([^/]+)\/?$/.exec(u.pathname);
  if (!m) return null;
  let name;
  try {
    name = decodeURIComponent(m[1]).replace(/ /g, '_');
  } catch (e) {
    return null;
  }
  const space = /^Space:(.+)$/i.exec(name);
  if (space) {
    const spaceName = space[1];
    return { kind: 'space', spaceName, key: 'Space:' + spaceName,
      url: WT_ORIGIN + '/wiki/Space:' + encodeURIComponent(spaceName).replace(/%2C/g, ',') };
  }
  const p = /^(?:Profile:)?(\p{L}[^:#?]*-\d+)$/u.exec(name);
  if (!p) return null;
  return { kind: 'profile', wikitreeId: p[1], key: p[1], url: WT_ORIGIN + '/wiki/' + encodeURIComponent(p[1]) };
}

// The one API request: getProfile for the page (`what` omitted) or getPhotos
// for a profile's photos (`what` 'photos'). A GET, so it carries no body.
function wtApiUrl(key, what) {
  const q = what === 'photos'
    ? { action: 'getPhotos', key, limit: String(WT_MAX_PHOTOS + WT_MAX_HOSTED) }
    : { action: 'getProfile', key, fields: WT_PROFILE_FIELDS, bioFormat: 'wiki' };
  q.appId = WT_APP_ID;
  return WT_API + '?' + Object.keys(q).map((k) => k + '=' + encodeURIComponent(q[k])).join('&');
}

// A WikiTree date as the archive writes one: "1681-10-26" stays, "1635-00-00"
// is "1635", "1681-10-00" is "1681-10", "0000-00-00" (not known) is null.
function wtDate(v) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(v || '').trim());
  if (!m || m[1] === '0000') return null;
  if (m[2] === '00') return m[1];
  if (m[3] === '00') return m[1] + '-' + m[2];
  return m[0];
}

// "20230526144512" (WikiTree's Touched and Created) as "2023-05-26".
function wtStamp(v) {
  const m = /^(\d{4})(\d{2})(\d{2})/.exec(String(v || '').trim());
  return m && m[1] !== '0000' ? m[1] + '-' + m[2] + '-' + m[3] : null;
}

function wtStr(v) {
  return typeof v === 'string' || typeof v === 'number' ? String(v).trim() : '';
}

// A person's name the way WikiTree shows it: "Mary (Cole) Hartley".
function wtFullName(p) {
  if (!p || typeof p !== 'object') return null;
  const first = wtStr(p.FirstName) || wtStr(p.RealName);
  const birth = wtStr(p.LastNameAtBirth);
  const current = wtStr(p.LastNameCurrent) || birth;
  const parts = [wtStr(p.Prefix), first, wtStr(p.MiddleName),
    birth && birth !== current ? '(' + birth + ')' : '', current, wtStr(p.Suffix)];
  const name = parts.filter(Boolean).join(' ');
  return name || wtStr(p.LongName) || null;
}

function wtNameParts(p) {
  const out = {};
  for (const [k, key] of [['prefix', 'Prefix'], ['first', 'FirstName'], ['middle', 'MiddleName'],
    ['lastAtBirth', 'LastNameAtBirth'], ['lastCurrent', 'LastNameCurrent'], ['suffix', 'Suffix'],
    ['nicknames', 'Nicknames'], ['realName', 'RealName']]) {
    const v = wtStr(p[key]);
    if (v) out[k] = v;
  }
  return out;
}

// What the API says, as the record the capture keeps:
// { ok: true, record, bio } with `bio` the biography's wiki text (null when
// WikiTree does not share it), or { ok: false, error } when WikiTree did not
// answer for this page (a private profile, a page that is not there).
function wtRecordFromApi(json, parsed) {
  const entry = Array.isArray(json) ? json[0] : null;
  if (!entry || typeof entry !== 'object') return { ok: false, error: 'WikiTree sent an answer the add-on could not read' };
  const p = entry.profile;
  if ((entry.status !== 0 && entry.status !== '0' && entry.status !== undefined) || !p || typeof p !== 'object' ||
      Array.isArray(p) || !Object.keys(p).length) {
    return { ok: false, error: 'WikiTree did not share this page through its public data service' +
      (typeof entry.status === 'string' && entry.status ? ' ("' + entry.status.slice(0, 120) + '")' : '') };
  }
  const bio = typeof p.bio === 'string' ? p.bio : typeof p.Bio === 'string' ? p.Bio : null;
  if (parsed.kind === 'space') {
    const title = p.Title && typeof p.Title === 'object' ? wtStr(p.Title.Text) : wtStr(p.Title);
    return {
      ok: true,
      bio,
      record: {
        kind: 'space', spaceName: parsed.spaceName, pageUrl: parsed.url, pageId: p.PageId || null,
        name: title || parsed.spaceName.replace(/_/g, ' '),
        lastEdited: wtStamp(p.Touched), created: wtStamp(p.Created), from: 'api',
      },
    };
  }
  if (wtStr(p.Name) && wtStr(p.Name).toLowerCase() !== parsed.wikitreeId.toLowerCase()) {
    // WikiTree answers for a merged profile with the one it was merged into.
    parsed = Object.assign({}, parsed, { wikitreeId: wtStr(p.Name), url: WT_ORIGIN + '/wiki/' + encodeURIComponent(wtStr(p.Name)) });
  }
  const kin = (o, extra) => (o && typeof o === 'object' ? Object.values(o) : [])
    .filter((x) => x && typeof x === 'object' && wtStr(x.Name))
    .map((x) => Object.assign({ wikitreeId: wtStr(x.Name), name: wtFullName(x), birthDate: wtDate(x.BirthDate),
      deathDate: wtDate(x.DeathDate) }, extra ? extra(x) : {}));
  const father = wtStr(p.Father);
  const mother = wtStr(p.Mother);
  return {
    ok: true,
    bio,
    record: {
      kind: 'profile',
      wikitreeId: parsed.wikitreeId,
      pageUrl: parsed.url,
      pageId: p.PageId || null,
      name: wtFullName(p) || parsed.wikitreeId,
      nameParts: wtNameParts(p),
      gender: wtStr(p.Gender) || null,
      isLiving: p.IsLiving === 1 || p.IsLiving === '1',
      birth: { date: wtDate(p.BirthDate), place: wtStr(p.BirthLocation) || null },
      death: { date: wtDate(p.DeathDate), place: wtStr(p.DeathLocation) || null },
      lastEdited: wtStamp(p.Touched),
      created: wtStamp(p.Created),
      parents: kin(p.Parents, (x) => ({
        relation: father && wtStr(x.Id) === father ? 'father' : mother && wtStr(x.Id) === mother ? 'mother' : 'parent',
      })),
      spouses: kin(p.Spouses, (x) => ({
        marriageDate: wtDate(x.marriage_date || x.MarriageDate),
        marriagePlace: wtStr(x.marriage_location || x.MarriageLocation) || null,
      })),
      children: kin(p.Children),
      from: 'api',
    },
  };
}

// ── reading the page itself (a node tree: the page's document in the
// browser, tests/mini-dom.js in tests) ─────────────────────────────────────

function wtAttr(el, name) {
  return el && el.getAttribute ? el.getAttribute(name) : null;
}

function wtTag(el) {
  return el && el.nodeType === 1 ? String(el.tagName || '').toUpperCase() : '';
}

function wtElements(root, pred, out) {
  out = out || [];
  for (const c of (root && root.childNodes) || []) {
    if (c.nodeType !== 1) continue;
    if (pred(c)) out.push(c);
    wtElements(c, pred, out);
  }
  return out;
}

const WT_BLOCK = /^(P|DIV|LI|UL|OL|H[1-6]|TR|TD|DD|DT|BLOCKQUOTE|SECTION|TABLE|PRE)$/;

// An element's text as a reader sees it: a line per block and per <br>, no
// scripts, no edit links (WikiTree's "edit" and pencil links are not page text).
function wtText(node) {
  const parts = [];
  const walk = (n) => {
    if (n.nodeType === 3) {
      parts.push(String(n.nodeValue || '').replace(/\s+/g, ' '));
      return;
    }
    if (n.nodeType !== 1) return;
    const tag = wtTag(n);
    if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' || tag === 'BUTTON') return;
    const cls = String(wtAttr(n, 'class') || '');
    if (/(^|\s)(EDIT|icon--edit)(\s|$)/.test(cls)) return;
    if (tag === 'A' && /Special:Edit|action=edit/i.test(wtAttr(n, 'href') || '')) return;
    if (tag === 'BR') {
      parts.push('\n');
      return;
    }
    const block = WT_BLOCK.test(tag);
    if (block) parts.push('\n');
    for (const c of n.childNodes || []) walk(c);
    if (block) parts.push('\n');
  };
  if (node) walk(node);
  return parts.join('').split('\n').map((l) => l.replace(/[ \t ]+/g, ' ').trim()).filter(Boolean).join('\n');
}

function wtLinksIn(node) {
  return wtElements(node, (e) => wtTag(e) === 'A' && !!wtAttr(e, 'href')).map((a) => wtAttr(a, 'href'));
}

// The text of the page's biography box (`.body-text`) from the element after
// `from` up to the element `to` (a heading), or the whole box.
function wtBoxText(box, from, to) {
  const kids = Array.from((box && box.childNodes) || []);
  const start = from ? kids.indexOf(from) + 1 : 0;
  const end = to ? kids.indexOf(to) : kids.length;
  const holder = { nodeType: 1, tagName: 'DIV', childNodes: kids.slice(start, end < 0 ? kids.length : end),
    getAttribute: () => null };
  return wtText(holder);
}

// The page's biography box: the element holding the "Biography" heading (a
// profile), or the first .body-text (a free-space page).
function wtBodyBox(root) {
  const bioHead = wtElements(root, (e) => /^H[23]$/.test(wtTag(e)) && wtAttr(e, 'id') === 'Biography')[0];
  if (bioHead) return { box: bioHead.parentNode, bioHead };
  const box = wtElements(root, (e) => /(^|\s)body-text(\s|$)/.test(wtAttr(e, 'class') || ''))[0];
  return { box: box || null, bioHead: null };
}

// What the page itself says, when the API will not: { ok, record, bioText,
// sources } (sources from wtSourcesFromPage). `root` is the page's document.
function wtRecordFromPage(root, parsed) {
  const h1 = wtElements(root, (e) => wtTag(e) === 'H1')[0];
  const name = h1 ? wtText(h1).split('\n')[0] : '';
  if (!name) return { ok: false, error: 'the page shows no WikiTree profile or free-space page to save' };
  const { box, bioHead } = wtBodyBox(root);
  const srcHead = box ? Array.from(box.childNodes || []).find((e) => /^H[23]$/.test(wtTag(e)) &&
    /^sources?$/i.test(wtText(e))) : null;
  const bioText = box ? wtBoxText(box, bioHead, srcHead) : '';
  const sources = wtSourcesFromPage(root);
  const edits = wtElements(root, (e) => wtAttr(e, 'id') === 'Edits')[0];
  const editTime = edits ? wtElements(edits, (e) => wtTag(e) === 'TIME')[0] : null;
  const modified = wtElements(root, (e) => wtAttr(e, 'itemprop') === 'dateModified')[0];
  const lastEdited = wtDate(wtAttr(editTime, 'datetime')) || wtDate(wtAttr(modified, 'content'));
  if (parsed.kind === 'space') {
    return { ok: true, bioText, sources,
      record: { kind: 'space', spaceName: parsed.spaceName, pageUrl: parsed.url, pageId: null, name,
        lastEdited, created: null, from: 'page' } };
  }
  const prop = (p) => wtElements(root, (e) => wtAttr(e, 'itemprop') === p)[0];
  const vital = (dateProp, placeProp) => {
    const t = prop(dateProp);
    const place = prop(placeProp);
    return { date: wtDate(wtAttr(t, 'datetime')) || null, place: place ? wtText(place) || null : null };
  };
  const kin = { parents: [], spouses: [], children: [] };
  const seen = new Set();
  for (const el of wtElements(root, (e) => /^(parent|spouse|children|child)$/.test(wtAttr(e, 'itemprop') || ''))) {
    const a = wtElements(el, (e) => wtTag(e) === 'A' && !!wtAttr(e, 'href'))[0];
    let id = null;
    try {
      id = a ? wtParsePage(new URL(wtAttr(a, 'href'), WT_ORIGIN).href) : null;
    } catch (e) {
      id = null;
    }
    if (!id || id.kind !== 'profile') continue;
    const p = wtAttr(el, 'itemprop');
    const key = p + ':' + id.wikitreeId.toLowerCase();
    if (seen.has(key)) continue;
    seen.add(key);
    const who = { wikitreeId: id.wikitreeId, name: wtText(a) || null };
    if (p === 'parent') {
      const box2 = wtAttr(el, 'id');
      who.relation = box2 === 'Father' ? 'father' : box2 === 'Mother' ? 'mother' : 'parent';
      kin.parents.push(who);
    } else if (p === 'spouse') {
      kin.spouses.push(who);
    } else {
      kin.children.push(who);
    }
  }
  return {
    ok: true,
    bioText,
    sources,
    record: Object.assign({
      kind: 'profile', wikitreeId: parsed.wikitreeId, pageUrl: parsed.url, pageId: null, name,
      birth: vital('birthDate', 'birthPlace'), death: vital('deathDate', 'deathPlace'),
      lastEdited, created: null, from: 'page',
    }, kin),
  };
}

// ── the cited sources ───────────────────────────────────────────────────────

// A few templates WikiTree editors use for a record on another site, and the
// address each names. Any other template is not taken as a link.
function wtTemplateLink(name, args) {
  const n = name.trim().toLowerCase().replace(/[_\s]+/g, ' ');
  const a = args.map((x) => x.trim());
  const ok = (v) => /^[A-Za-z0-9:-]+$/.test(v || '');
  if (n === 'findagrave' && /^\d+$/.test(a[0] || '')) return 'https://www.findagrave.com/memorial/' + a[0];
  if (n === 'familysearch record' && ok(a[0])) return 'https://www.familysearch.org/ark:/61903/1:1:' + a[0];
  if (n === 'familysearch image' && ok(a[0])) return 'https://www.familysearch.org/ark:/61903/3:1:' + a[0];
  if (n === 'familysearch' && ok(a[0])) return 'https://www.familysearch.org/tree/person/details/' + a[0];
  if (n === 'ancestry record' && /^\d+$/.test(a[0] || '') && /^\d+$/.test(a[1] || '')) {
    return 'https://www.ancestry.com/discoveryui-content/view/' + a[1] + ':' + a[0];
  }
  return null;
}

// The interwiki prefixes WikiTree turns into an address on another site.
function wtInterwikiLink(prefix, target) {
  const p = prefix.toLowerCase();
  const t = target.trim();
  if (p === 'findagrave' && /^\d+$/.test(t)) return 'https://www.findagrave.com/memorial/' + t;
  if (p === 'familysearch' && /^[A-Za-z0-9-]+$/.test(t)) return 'https://www.familysearch.org/tree/person/details/' + t;
  if (p === 'wikipedia' && t) return 'https://en.wikipedia.org/wiki/' + encodeURIComponent(t.replace(/ /g, '_'));
  return null;
}

function wtTrimUrl(u) {
  return u.replace(/[.,;:!?)\]}'"]+$/, '');
}

// What one cited entry links to: { links: [url…], interwiki: ['FamilySearch:…'],
// hosted: [{ kind: 'file'|'space', … }] }. `hosted` names files and free-space
// pages on WikiTree itself; their addresses are in `links` too.
function wtEntryLinks(text) {
  const links = [];
  const interwiki = [];
  const hosted = [];
  const addLink = (u) => {
    if (u && links.indexOf(u) < 0) links.push(u);
  };
  const addHosted = (h) => {
    if (!h) return;
    const key = h.kind + ':' + (h.fileName || h.spaceName).toLowerCase();
    if (!hosted.some((x) => x.kind + ':' + (x.fileName || x.spaceName).toLowerCase() === key)) hosted.push(h);
    addLink(h.pageUrl);
  };
  // [[Prefix:Target|label]] and [[:Image:Name-1.jpg]]
  const wikiRe = /\[\[\s*:?\s*([^\]|]+?)\s*(?:\|[^\]]*)?\]\]/g;
  let m;
  while ((m = wikiRe.exec(text))) {
    const target = m[1];
    const c = target.indexOf(':');
    if (c < 0) continue; // another WikiTree profile, not a source elsewhere
    const prefix = target.slice(0, c).trim();
    const rest = target.slice(c + 1).trim();
    if (/^(image|file)$/i.test(prefix)) {
      addHosted(wtHostedFile(rest));
    } else if (/^space$/i.test(prefix)) {
      addHosted(wtHostedSpace(rest));
    } else if (!WT_NAMESPACES.test(prefix) && rest) {
      const url = wtInterwikiLink(prefix, rest);
      if (url) addLink(url);
      if (interwiki.indexOf(prefix + ':' + rest) < 0) interwiki.push(prefix + ':' + rest);
    }
  }
  // {{FindAGrave|123}}, {{FamilySearch Record|…}}, …
  const tplRe = /\{\{\s*([^{}|]+?)\s*((?:\|[^{}|]*)*)\}\}/g;
  while ((m = tplRe.exec(text))) {
    const url = wtTemplateLink(m[1], m[2].split('|').slice(1));
    if (url) addLink(url);
  }
  // Every web address, bare or in [https://… label].
  const urlRe = /https?:\/\/[^\s\]|<>"{}]+/gi;
  while ((m = urlRe.exec(text))) {
    const url = wtTrimUrl(m[0]);
    const h = wtHostedFromUrl(url);
    if (h) addHosted(h);
    else if (!(wtParsePage(url) && wtParsePage(url).kind === 'profile')) addLink(url); // another profile is not a source elsewhere
  }
  return { links, interwiki, hosted };
}

// An uploaded file by its name ("Hartley-6084-2.jpg"): its own page is
// /photo/{ext}/{name less ext}.
function wtHostedFile(name) {
  const file = String(name || '').trim().replace(/ /g, '_');
  const m = /^([^/\\]+)\.([A-Za-z0-9]{2,5})$/.exec(file);
  if (!m) return null;
  return { kind: 'file', fileName: file, ext: m[2].toLowerCase(),
    pageUrl: WT_ORIGIN + '/photo/' + m[2].toLowerCase() + '/' + encodeURIComponent(m[1]) };
}

function wtHostedSpace(name) {
  const spaceName = String(name || '').trim().replace(/ /g, '_').replace(/#.*$/, '');
  if (!spaceName) return null;
  const p = wtParsePage(WT_ORIGIN + '/wiki/Space:' + encodeURIComponent(spaceName));
  return p ? { kind: 'space', spaceName: p.spaceName, pageUrl: p.url } : null;
}

// A WikiTree address that is a file it holds or a free-space page, or null.
function wtHostedFromUrl(url) {
  let u;
  try {
    u = new URL(url);
  } catch (e) {
    return null;
  }
  const host = u.hostname.toLowerCase();
  if (host !== WT_HOST && host !== 'wikitree.com') return null;
  let path;
  try {
    path = decodeURIComponent(u.pathname);
  } catch (e) {
    return null;
  }
  let m = /^\/photo\/([A-Za-z0-9]{2,5})\/([^/]+)$/.exec(path);
  if (m) return wtHostedFile(m[2] + '.' + m[1]);
  m = /^\/photo\.php\/(?:thumb\/)?[0-9a-f]\/[0-9a-f]{2}\/([^/]+\.[A-Za-z0-9]{2,5})(?:\/.*)?$/.exec(path);
  if (m) return wtHostedFile(m[1]);
  const p = wtParsePage(u.href);
  if (p && p.kind === 'space') return { kind: 'space', spaceName: p.spaceName, pageUrl: p.url };
  return null;
}

// One entry as the archive keeps it: the text as written, what it links to,
// and whether it links anywhere at all (`linked`: false is a source to find).
function wtEntry(text, from) {
  const l = wtEntryLinks(text);
  return { text, from, links: l.links, interwiki: l.interwiki, hosted: l.hosted,
    linked: l.links.length > 0 || l.interwiki.length > 0 };
}

// Two entries are one work cited twice when their words match, whatever the
// list numbering, spacing, case or closing full stop.
function wtEntryKey(text) {
  return String(text || '').replace(/^\s*\d+\.\s*/, '').replace(/<[^>]+>/g, ' ').replace(/'{2,}/g, '')
    .replace(/\s+/g, ' ').trim().replace(/[.\s]+$/, '').toLowerCase();
}

function wtDedupe(entries) {
  const seen = new Set();
  const out = [];
  for (const e of entries) {
    const key = wtEntryKey(e.text);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    out.push(e);
  }
  return out;
}

// Every cited source in the biography's wiki text, in page order: each
// <ref>…</ref> as it first appears, then each entry of the Sources section,
// one work once. The text is kept as written: author, title and year are not
// read out here (#416's citation parser does that later, from this text).
function wtSources(wikitext) {
  let t = String(wikitext || '').replace(/\r\n?/g, '\n').replace(/<!--[\s\S]*?-->/g, '');
  const entries = [];
  t = t.replace(/<ref\b[^>]*\/\s*>/gi, '');
  t = t.replace(/<ref\b[^>]*>([\s\S]*?)<\/ref\s*>/gi, (all, inner) => {
    const text = inner.trim();
    if (text) entries.push(wtEntry(text, 'ref'));
    return '';
  });
  const lines = t.split('\n');
  const head = lines.findIndex((l) => /^\s*(={2,6})\s*'*\s*sources?\s*'*\s*\1\s*$/i.test(l) ||
    /^\s*={2,6}\s*sources?\s*={2,6}\s*$/i.test(l));
  if (head >= 0) {
    const level = (/^\s*(=+)/.exec(lines[head]) || ['', '=='])[1].length;
    let cur = null;
    let skip = false;
    for (let i = head + 1; i < lines.length; i++) {
      const line = lines[i];
      const h = /^\s*(={2,6})\s*([^=].*?)\s*={2,6}\s*$/.exec(line);
      // A heading at the Sources level ends the list. A sub-heading such as
      // Primary sources continues it; Acknowledgements (thanks, the import
      // note) is skipped up to the next heading. The page path does the same.
      if (h && h[1].length <= level) break;
      if (h) skip = WT_NOT_SOURCES_RE.test(h[2].replace(/'{2,}/g, '').trim());
      const s = line.replace(/<\/?references\b[^>]*>/gi, '').trim();
      if (!s || h || skip) {
        cur = null;
        continue;
      }
      const bullet = /^[*#:]+\s*/.exec(s);
      const body = bullet ? s.slice(bullet[0].length).trim() : s;
      if (!body) continue;
      // "See also:" and the like label the list; they cite nothing.
      if (/^(see also|sources?|references?|notes?|research notes?|footnotes?)\s*:?\s*$/i.test(body.replace(/'{2,}/g, ''))) {
        cur = null;
        continue;
      }
      if (bullet || !cur) {
        cur = wtEntry(body, 'list');
        entries.push(cur);
      } else {
        const joined = wtEntry(cur.text + '\n' + body, 'list');
        entries[entries.indexOf(cur)] = joined;
        cur = joined;
      }
    }
  }
  return wtDedupe(entries);
}

// The same list from the page itself (when the API refuses): the footnotes
// (ol.references) first, then each paragraph or item under the Sources heading.
// Links are the page's own <a href>s; the text is what the page shows.
function wtSourcesFromPage(root) {
  const entries = [];
  const fromNode = (node, from) => {
    const text = wtText(node);
    if (!text || /^(see also|sources?|references?|notes?)\s*:?$/i.test(text)) return;
    const hrefs = wtLinksIn(node).map((h) => {
      try {
        return new URL(h, WT_ORIGIN).href;
      } catch (e) {
        return null;
      }
    }).filter((h) => h && /^https?:/.test(h));
    const e = wtEntry(text + (hrefs.length ? '\n' + hrefs.join('\n') : ''), from);
    e.text = text;
    // A link to another WikiTree profile is not a source elsewhere.
    e.links = e.links.filter((u) => !(wtParsePage(u) && wtParsePage(u).kind === 'profile'));
    e.linked = e.links.length > 0 || e.interwiki.length > 0;
    entries.push(e);
  };
  for (const ol of wtElements(root, (e) => wtTag(e) === 'OL' && /(^|\s)references(\s|$)/.test(wtAttr(e, 'class') || ''))) {
    for (const li of (ol.childNodes || []).filter((c) => wtTag(c) === 'LI')) {
      const ref = wtElements(li, (e) => /(^|\s)reference-text(\s|$)/.test(wtAttr(e, 'class') || ''))[0];
      fromNode(ref || li, 'ref');
    }
  }
  const head = wtElements(root, (e) => /^H[23]$/.test(wtTag(e)) && /^sources?$/i.test(wtText(e)))[0];
  if (head && head.parentNode) {
    const kids = Array.from(head.parentNode.childNodes || []);
    const level = Number(wtTag(head).slice(1));
    let skip = false;
    for (let i = kids.indexOf(head) + 1; i < kids.length; i++) {
      const n = kids[i];
      const hm = /^H([1-6])$/.exec(wtTag(n));
      // Same rule as wtSources: a heading at the Sources level ends the list,
      // a sub-heading of thanks or research notes is skipped.
      if (hm && Number(hm[1]) <= level) break;
      if (hm) {
        skip = WT_NOT_SOURCES_RE.test(wtText(n));
        continue;
      }
      if (skip) continue;
      if (wtTag(n) === 'P') fromNode(n, 'list');
      else if (wtTag(n) === 'UL' || wtTag(n) === 'OL') {
        if (/(^|\s)references(\s|$)/.test(wtAttr(n, 'class') || '')) continue;
        for (const li of (n.childNodes || []).filter((c) => wtTag(c) === 'LI')) fromNode(li, 'list');
      }
    }
  }
  return wtDedupe(entries);
}

// Every file or free-space page WikiTree itself holds that a cited source
// points to, once each, numbered in page order: [{ n, kind, fileName|spaceName,
// ext, pageUrl, entries: [index…] }]. The caller saves at most
// WT_MAX_HOSTED of them.
function wtHostedFiles(sources) {
  const out = [];
  (sources || []).forEach((s, i) => {
    for (const h of s.hosted || []) {
      const key = h.kind + ':' + (h.fileName || h.spaceName).toLowerCase();
      let got = out.find((x) => x.key === key);
      if (!got) {
        got = Object.assign({ key, n: out.length + 1, entries: [] }, h);
        out.push(got);
      }
      if (got.entries.indexOf(i) < 0) got.entries.push(i);
    }
  });
  return out.map((h) => {
    const c = Object.assign({}, h);
    delete c.key;
    return c;
  });
}

// Is this an original uploaded file on www.wikitree.com (never a thumbnail,
// never another host)? The one rule for every file the capture downloads.
function wtOriginalUrlOk(url) {
  let u;
  try {
    u = new URL(url);
  } catch (e) {
    return false;
  }
  return u.protocol === 'https:' && u.hostname.toLowerCase() === WT_HOST && !u.port && !u.username && !u.password &&
    !u.search && WT_ORIGINAL_PATH_RE.test(u.pathname);
}

// A held file's own page (/photo/{ext}/{name}): its caption (the uploader's
// title, in the <h1>) and the address of the original, or null for each that
// the page does not show.
function wtFileFromPage(root, fileName) {
  const h1 = wtElements(root, (e) => wtTag(e) === 'H1')[0];
  const caption = h1 ? wtText(h1).split('\n')[0] || null : null;
  const want = String(fileName || '').toLowerCase();
  let original = null;
  for (const href of wtLinksIn(root)) {
    let abs;
    try {
      abs = new URL(href, WT_ORIGIN);
    } catch (e) {
      continue;
    }
    if (!wtOriginalUrlOk(abs.href)) continue;
    let last;
    try {
      last = decodeURIComponent(abs.pathname.split('/').pop()).toLowerCase();
    } catch (e) {
      continue;
    }
    if (!want || last === want) {
      original = abs.href;
      break;
    }
  }
  return { caption, originalUrl: original };
}

// A thumbnail address (/photo.php/thumb/{h}/{hh}/{name}/{N}px-{name}) as its
// original's (/photo.php/{h}/{hh}/{name}), or null.
function wtOriginalFromThumb(thumb) {
  const m = /^(?:https?:\/\/(?:www\.)?wikitree\.com)?\/photo\.php\/thumb\/([0-9a-f])\/([0-9a-f]{2})\/([^/]+)\/[^/]+$/i
    .exec(String(thumb || ''));
  if (!m) return null;
  const url = WT_ORIGIN + '/photo.php/' + m[1] + '/' + m[2] + '/' + m[3];
  return wtOriginalUrlOk(url) ? url : null;
}

// A profile's photos from getPhotos: [{ imageName, caption, date, place,
// pageUrl, originalUrl }], each with an original on www.wikitree.com.
function wtPhotosFromApi(json) {
  const entry = Array.isArray(json) ? json[0] : null;
  const list = entry && Array.isArray(entry.photos) ? entry.photos : [];
  const out = [];
  for (const p of list) {
    if (!p || typeof p !== 'object') continue;
    const originalUrl = wtOriginalFromThumb(p.URL_300) || wtOriginalFromThumb(p.URL_75);
    if (!originalUrl) continue;
    out.push({
      imageName: wtStr(p.ImageName) || null,
      caption: wtStr(p.Title) || null,
      date: wtDate(p.Date),
      place: wtStr(p.Location) || null,
      pageUrl: /^\/photo\//.test(wtStr(p.URL)) ? WT_ORIGIN + wtStr(p.URL) : null,
      originalUrl,
    });
  }
  return out;
}

// Wiki text as plain text a person reads: headings as "## Heading", links as
// their words, footnotes and templates left out, bold and italics unmarked.
// Used for the biography (page.md) and a free-space page's transcription.
function wtPlainText(wikitext) {
  let t = String(wikitext || '').replace(/\r\n?/g, '\n').replace(/<!--[\s\S]*?-->/g, '');
  t = t.replace(/<ref\b[^>]*\/\s*>/gi, '').replace(/<ref\b[^>]*>[\s\S]*?<\/ref\s*>/gi, '');
  t = t.replace(/<\/?references\b[^>]*>/gi, '');
  t = t.replace(/\[\[\s*:?\s*category\s*:[^\]]*\]\]/gi, '');
  // Templates, innermost first (a few levels deep is all a page uses).
  for (let i = 0; i < 4; i++) t = t.replace(/\{\{[^{}]*\}\}/g, '');
  t = t.replace(/\[\[\s*:?\s*(?:image|file)\s*:([^\]|]+)(?:\|[^\]]*)?\]\]/gi, (a, f) => '[file: ' + f.trim() + ']');
  t = t.replace(/\[\[([^\]|]+)\|([^\]]*)\]\]/g, '$2');
  t = t.replace(/\[\[([^\]]+)\]\]/g, '$1');
  t = t.replace(/\[(https?:\/\/[^\s\]]+)\s+([^\]]+)\]/g, '$2 ($1)');
  t = t.replace(/\[(https?:\/\/[^\s\]]+)\]/g, '$1');
  t = t.replace(/'{2,}/g, '');
  t = t.replace(/<br\s*\/?>/gi, '\n').replace(/<\/?(?:span|div|p|b|i|u|small|big|sup|sub|center|font)\b[^>]*>/gi, '');
  t = t.replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>');
  const out = t.split('\n').map((line) => {
    const h = /^\s*(={2,6})\s*(.*?)\s*={2,6}\s*$/.exec(line);
    if (h) return '\n' + '#'.repeat(Math.min(h[1].length, 6)) + ' ' + h[2] + '\n';
    return line.replace(/^\s*[*#]+\s*/, (b) => (b.trim().startsWith('*') ? '- ' : '')).replace(/[ \t]+$/g, '')
      .replace(/^[ \t]+/, '');
  }).join('\n');
  return out.replace(/\n{3,}/g, '\n\n').trim();
}

// The citation: WikiTree, "{name} ({WikiTree ID})," ({address} : last edited
// {date}; accessed {date}). A free-space page's name stands in for the person.
function wtCitation(record, accessed) {
  const label = record.kind === 'space'
    ? 'Space:' + String(record.name || record.spaceName)
    : String(record.name || record.wikitreeId) + ' (' + record.wikitreeId + ')';
  return 'WikiTree, "' + label + '," (' + record.pageUrl + ' : ' +
    (record.lastEdited ? 'last edited ' + record.lastEdited + '; ' : '') + 'accessed ' + accessed + ').';
}

// One entry on one line, as plain text.
function wtEntryLine(text) {
  return wtPlainText(String(text || '').replace(/\[(https?:\/\/[^\s\]]+)\s+([^\]]+)\]/g, '$2')).replace(/\s*\n\s*/g, ' ').replace(/^\d+\.\s+/, '').trim();
}

// The "Cited sources" list for the notes: linked entries first, each with
// where it points (and, for a file WikiTree holds, the file this capture saved
// it as), then "To find:" with the ones that point nowhere. Each is a "- [ ]"
// line he can tick in any Markdown editor. `saved` maps a hosted file's `n`
// to { file, caption, pageUrl } for the ones saved.
function wtCitedSourcesNotes(sources, hosted, saved) {
  if (!sources || !sources.length) return '';
  const savedFor = (i) => (hosted || []).filter((h) => h.entries.indexOf(i) >= 0 && saved && saved[h.n])
    .map((h) => saved[h.n]);
  const linked = [];
  const toFind = [];
  sources.forEach((s, i) => {
    const words = wtEntryLine(s.text) || s.text.replace(/\s+/g, ' ').trim();
    if (!s.linked) {
      toFind.push('- [ ] ' + words);
      return;
    }
    const where = s.links.concat(s.interwiki.filter((w) => !s.links.some((u) => u.indexOf(w.split(':')[1]) >= 0)));
    const keep = savedFor(i).map((f) => 'saved in this source as ' + f.file +
      (f.caption ? ' ("' + f.caption + '")' : '') + ', from ' + f.pageUrl);
    linked.push('- [ ] ' + words + (where.length ? ' - ' + where.join(' ; ') : '') +
      (keep.length ? '; ' + keep.join('; ') : ''));
  });
  const out = ['### Cited sources', ''];
  if (linked.length) out.push('Linked (open each and check it):', ...linked, '');
  if (toFind.length) out.push('To find:', ...toFind, '');
  return out.join('\n').trim();
}
// FHA-SYNC-END wikitree

// ── panel side: downloading the files (not copied into content.js) ─────────
// The content script says where each original is; the panel downloads it, one
// at a time with a pause between them. NO LOGIN: no cookies, no referrer, only
// an original on www.wikitree.com over https (wtOriginalUrlOk), never a
// redirect; an answer that is not a picture or a PDF is refused, never saved.
const WT_EXT = { 'image/jpeg': 'jpg', 'image/jpg': 'jpg', 'image/png': 'png', 'image/gif': 'gif', 'image/webp': 'webp',
  'image/tiff': 'tif', 'application/pdf': 'pdf' };

// One original file: { ok, blob, ext } or { ok: false, error, slowDown? }.
// `slowDown`: WikiTree said slow down or busy (429/503), so nothing more is
// asked for now. `fetchFn` is the browser's fetch (a test's stand-in).
async function wtFetchFile(url, fetchFn) {
  if (!wtOriginalUrlOk(url)) return { ok: false, error: 'the file is not an original on WikiTree, so it was not downloaded' };
  let r;
  try {
    r = await fetchFn(url, { credentials: 'omit', referrerPolicy: 'no-referrer', redirect: 'error' });
  } catch (e) {
    return { ok: false, error: 'could not download it from WikiTree (the connection dropped, or WikiTree sent it ' +
      'somewhere else, which is not followed) - press Capture again later' };
  }
  if (r.status === 429 || r.status === 503) {
    return { ok: false, slowDown: true, error: 'WikiTree asked us to slow down (HTTP ' + r.status + ')' };
  }
  if (!r.ok) return { ok: false, error: 'WikiTree refused the file (HTTP ' + r.status + ')' };
  if (r.url && !wtOriginalUrlOk(r.url)) return { ok: false, error: 'the file’s address led away from WikiTree, so it was not saved' };
  const type = String((r.headers && r.headers.get && r.headers.get('content-type')) || '').split(';')[0].trim().toLowerCase();
  if (!/^image\//.test(type) && type !== 'application/pdf') return { ok: false, error: 'WikiTree sent a page instead of the file' };
  let blob;
  try {
    blob = await r.blob();
  } catch (e) {
    return { ok: false, error: 'the file broke off while downloading - press Capture again later' };
  }
  if (!blob || !blob.size) return { ok: false, error: 'WikiTree sent an empty file' };
  const named = (/\.([A-Za-z0-9]{2,5})$/.exec(new URL(url).pathname) || [])[1];
  return { ok: true, blob, ext: named ? named.toLowerCase().replace('jpeg', 'jpg') : WT_EXT[type] || 'bin' };
}

const wikitree = {
  WT_MAX_PHOTOS,
  WT_MAX_HOSTED,
  wtParsePage,
  wtApiUrl,
  wtDate,
  wtStamp,
  wtFullName,
  wtRecordFromApi,
  wtRecordFromPage,
  wtSources,
  wtSourcesFromPage,
  wtHostedFiles,
  wtOriginalUrlOk,
  wtFileFromPage,
  wtOriginalFromThumb,
  wtPhotosFromApi,
  wtPlainText,
  wtCitation,
  wtCitedSourcesNotes,
  wtFetchFile,
  // The pause between two downloads, 0.8-2 s.
  pauseMs: () => 800 + Math.floor(Math.random() * 1200),
};

if (typeof module !== 'undefined' && module.exports) {
  module.exports = wikitree;
} else if (typeof window !== 'undefined') {
  window.FHA = window.FHA || {};
  window.FHA.wikitree = wikitree;
}
