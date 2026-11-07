// popup.js - counts, coverage, research-log export, backups, your tree, and
// (advanced mode) the family-archive integration.
const $ = (id) => document.getElementById(id);
const KEYS = ['views', 'pages', 'passes', 'searches', 'clips', 'meta', 'anc', 'ancPeople', 'settings'];

function ago(ms) {
  if (!ms) return 'never';
  const m = Math.round((Date.now() - ms) / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return m + ' min ago';
  const h = Math.round(m / 60);
  if (h < 48) return h + ' h ago';
  return new Date(ms).toLocaleDateString();
}

function say(t) {
  $('msg').textContent = t;
}

function download(text, name, type) {
  const blob = new Blob([text], { type });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

const num = (n) => (n || 0).toLocaleString();

async function show() {
  const store = await chrome.storage.local.get(KEYS);
  const { views = {}, pages = {}, passes = {}, searches = {}, clips = {}, meta = {}, anc = {}, ancPeople = {}, settings = {} } = store;
  const advanced = !!settings.advanced;
  $('c-adv').checked = advanced;
  $('adv').hidden = !advanced;
  for (const el of document.querySelectorAll('.adv-only')) el.hidden = !advanced;

  const seen = Object.values(views).filter((v) => Math.max(v.t ? v.t.length : 0, v.n || 0) > 0).length;
  const nothing = Object.values(pages).filter((p) => p.mark === 'nothing').length;
  const later = Object.values(pages).filter((p) => p.mark === 'later').length;
  $('n-seen').textContent = num(seen);
  $('n-passed').textContent = num(Object.keys(passes).filter((id) => !views[id]).length);
  $('n-marks').textContent = num(nothing) + ' / ' + num(later);
  $('n-clip-pages').textContent = num(Object.keys(clips).length);
  $('n-searches').textContent = num(Object.keys(searches).length);
  const opened = (prefix) => Object.entries(anc).filter(([k, a]) => k.startsWith(prefix) && a.t && a.t.length).length;
  $('n-anc-seen').textContent = num(opened('rec:'));
  $('n-fs-seen').textContent = num(opened('fsrec:'));
  $('n-fag-seen').textContent = num(opened('fag:'));

  // Your tree: every record attached to a person whose page you opened, or in a loaded GEDCOM.
  const treeKeys = new Set();
  let people = 0;
  for (const p of Object.values(ancPeople)) {
    if ((p.keys || []).length) people++;
    for (const k of p.keys || []) if (!k.startsWith('clip:')) treeKeys.add(k);
  }
  $('n-tree').textContent = num(treeKeys.size);
  const tf = meta.treeFile;
  $('t-tree').textContent = (people ? 'From ' + num(people) + ' tree people' : 'Open people in your tree, or load your tree file, to fill this in') +
    (tf ? ', including the tree file ' + (tf.name ? '"' + tf.name + '" ' : '') + 'loaded ' + ago(tf.at) : '') + '.';
  $('t-log').textContent = 'Research log last exported ' + ago(meta.logExportedAt) + '.';

  const cov = SeenItLog.coverage(store);
  const table = $('cov');
  while (table.rows.length > 1) table.deleteRow(1);
  for (const c of cov.slice(0, 12)) {
    const r = table.insertRow();
    const name = r.insertCell();
    name.textContent = c.paper;
    if (c.place) {
      const s = document.createElement('div');
      s.className = 'place';
      s.textContent = c.place;
      name.appendChild(s);
    }
    for (const v of [c.years, c.pages, c.ruledOut]) {
      const td = r.insertCell();
      td.className = 'n';
      td.textContent = v;
    }
  }
  $('cov-more').textContent = cov.length > 12
    ? '…and ' + (cov.length - 12) + ' more papers (all of them are in the backup file).'
    : cov.length ? '' : 'Fills in as you open newspaper pages.';

  if (!advanced) return;
  const kc = meta.knownCounts || {};
  $('n-k-clip').textContent = num(kc['clip:filed']);
  $('n-k-rec').textContent = num(kc['rec:filed']);
  $('n-k-fs').textContent = num(kc['fsrec:filed']);
  $('n-k-fag').textContent = num(kc['fag:filed']);
  $('n-k-cap').textContent = num((kc['clip:captured'] || 0) + (kc['rec:captured'] || 0) + (kc['fsrec:captured'] || 0) + (kc['fag:captured'] || 0));
  const missing = meta.knownMissing || [];
  $('t-known').textContent = meta.knownError
    ? 'Could not read the archive: ' + meta.knownError + '.'
    : 'Read from the archive ' + ago(meta.knownAt) + ' (every 30 minutes).' +
      (missing.length ? ' Not reachable, so not counted: ' + missing.join('; ') + ' - is that drive connected?' : '');
  $('t-known').className = 'muted' + (meta.knownError || missing.length ? ' warn' : '');
  showFolders();
  const others = Object.keys(meta.merged || {}).filter((n) => n !== 'seen-it-' + meta.device + '.json');
  $('t-backup').textContent = meta.folderError
    ? 'Not connected: ' + meta.folderError + '.'
    : (meta.dataDir || '(checking…)') + '. This computer (' + (meta.device || '…') + ') saved ' + ago(meta.backupAt) + '. ' +
      (others.length
        ? 'Merged from ' + others.length + ' other computer' + (others.length === 1 ? '' : 's') + ', last ' + ago(meta.mergedAt) + '.'
        : 'No other computer\'s file yet.') + ' Checked every 15 minutes.';
  $('t-clips').textContent = 'Clippings list checked ' + (meta.fullRefreshRequested ? '(full re-read pending)' : ago(meta.clipsAt)) + '.';
}

// ── research log ────────────────────────────────────────────────────────────
async function exportLog(all) {
  const store = await chrome.storage.local.get(KEYS);
  const meta = store.meta || {};
  const since = all ? 0 : meta.logExportedAt || 0;
  const { count, text } = SeenItLog.build(store, since);
  if (!count) return say(all ? 'Nothing logged yet.' : 'Nothing new since the last export.');
  download(text, 'newspapers-research-log-' + SeenItLog.day(Date.now()) + '.md', 'text/markdown');
  // Through the background writer: writing back the whole meta read above
  // would undo anything the background recorded meanwhile.
  if (!all) await chrome.runtime.sendMessage({ type: 'logExported', at: Date.now() });
  say(count + ' entr' + (count === 1 ? 'y' : 'ies') + ' saved to your downloads folder' + (all ? '.' : ' (new since the last export).'));
  show();
}

$('b-log').addEventListener('click', () => exportLog(false));
$('b-log-all').addEventListener('click', () => exportLog(true));

// ── backups ─────────────────────────────────────────────────────────────────
$('b-export').addEventListener('click', async () => {
  const r = await chrome.runtime.sendMessage({ type: 'export' });
  if (!r || !r.ok) return say('Could not make the backup.');
  download(JSON.stringify(r.data, null, 1), 'seen-it-backup-' + SeenItLog.day(Date.now()) + '.json', 'application/json');
  say('Backup saved to your downloads folder. Restore merges it in, never counting a look twice.');
});

// A file picker closes the small toolbar window on some systems, so files are
// chosen in a full tab.
const IN_TAB = new URLSearchParams(location.search).has('tab');
if (IN_TAB) document.body.style.width = 'auto';
function pick(input, what) {
  if (!IN_TAB) {
    chrome.tabs.create({ url: chrome.runtime.getURL('popup.html?tab=1&pick=' + what) });
    window.close();
    return;
  }
  input.click();
}
const PICK = new URLSearchParams(location.search).get('pick');
if (PICK === 'gedcom') say('Click "Load your tree (GEDCOM)…" again to choose the file.');
if (PICK === 'restore') say('Click "Restore…" again to choose the backup file(s).');

$('b-import').addEventListener('click', () => pick($('f-import'), 'restore'));
$('f-import').addEventListener('change', async () => {
  const files = [...$('f-import').files];
  if (!files.length) return;
  const done = [];
  for (const f of files) {
    try {
      const data = JSON.parse(await f.text());
      const r = await chrome.runtime.sendMessage({ type: 'import', data });
      done.push(f.name + (r && r.ok ? ': ' + r.pages + ' pages merged' : ': could not load (' + (r && r.error) + ')'));
    } catch (e) {
      done.push(f.name + ': could not be read');
    }
  }
  say(done.join('. ') + '.');
  $('f-import').value = '';
  show();
});

// ── your tree, from a GEDCOM file ───────────────────────────────────────────
// The reading itself is in treefile.js (SeenItTree.parseGedcom), so it can be tested.
const parseGedcom = SeenItTree.parseGedcom;

$('b-gedcom').addEventListener('click', () => pick($('f-gedcom'), 'gedcom'));
$('f-gedcom').addEventListener('change', async () => {
  const f = $('f-gedcom').files[0];
  if (!f) return;
  say('Reading ' + f.name + '…');
  try {
    const people = parseGedcom(await f.text());
    if (!people.length) {
      say(f.name + ' has no Ancestry record or Find a Grave links. Export the tree from Ancestry (Tree settings → Export tree) and load that file.');
    } else {
      const r = await chrome.runtime.sendMessage({ type: 'loadTreeFile', people, file: f.name });
      say(r && r.ok
        ? 'Loaded ' + num(r.people) + ' people with ' + num(r.records) + ' attached records and memorials. Search results now mark them 🌳.'
        : 'Could not load the tree: ' + (r && r.error));
    }
  } catch (e) {
    say('Could not read ' + f.name + '.');
  }
  $('f-gedcom').value = '';
  show();
});

// ── advanced mode ───────────────────────────────────────────────────────────
$('c-adv').addEventListener('change', async () => {
  const on = $('c-adv').checked;
  if (on) {
    // The local helper is a separate program: ask for permission to talk to it
    // only when the integration is wanted.
    let granted = false;
    try {
      granted = await chrome.permissions.request({ permissions: ['nativeMessaging'] });
    } catch (e) {
      granted = false;
    }
    if (!granted) {
      $('c-adv').checked = false;
      return say('Advanced mode needs permission to talk to the local helper.');
    }
    say('Reading what the archive holds…');
  }
  const r = await chrome.runtime.sendMessage({ type: 'setAdvanced', on });
  if (on) {
    say(!r
      ? 'Advanced mode on. The add-on stopped before the archive was read - press "Refresh the archive list" to try again.'
      : r.pending
        ? 'Advanced mode on. Still reading the archive - the count appears below when it is done.'
        : r.ok && !r.error
          ? 'Advanced mode on: ' + num(r.known) + ' archive items known.'
          : 'Advanced mode on, but the archive could not be read: ' + (r.error || 'no answer') + '.');
  } else {
    say('Advanced mode off. Nothing about the archive is shown on the sites.');
  }
  show();
});

// ── advanced mode: the archive folder and the data folder ──────────────────
// Both are kept by the helper in its own settings file on this computer (see
// host/seenit_host.py); the window only shows them and sends changes.
let foldersShown = false;
async function showFolders(force) {
  if (foldersShown && !force) return;
  foldersShown = true;
  const r = await chrome.runtime.sendMessage({ type: 'hostConfig' });
  if (!r || !r.ok) {
    $('t-archive').textContent = 'The helper did not answer: ' + ((r && r.error) || 'no answer') + '.';
    $('t-archive').className = 'muted warn';
    $('t-data').textContent = '';
    return;
  }
  if (document.activeElement !== $('i-archive')) $('i-archive').value = r.archive || '';
  if (document.activeElement !== $('i-data')) $('i-data').value = r.dataDirSet ? r.dataDir : '';
  $('t-archive').textContent = r.archiveProblem
    ? r.archiveProblem + '.'
    : r.archive ? 'Seen It reads this archive (it never changes anything in it).'
      : 'Not set yet. Paste the archive\'s top folder and press Save.';
  $('t-archive').className = 'muted' + (r.archiveProblem || !r.archive ? ' warn' : '');
  $('t-data').textContent = r.dataDirProblem
    ? r.dataDirProblem + '.'
    : (r.dataDirSet ? 'Using ' : 'Using the usual place: ') + r.dataDir +
      '. To share between computers, point every computer at one synced folder. Settings: ' + r.configFile + '.';
  $('t-data').className = 'muted' + (r.dataDirProblem ? ' warn' : '');
}

async function saveFolder(change, what) {
  say('Saving the ' + what + '…');
  const r = await chrome.runtime.sendMessage({ type: 'setHostConfig', change });
  say(r && r.ok
    ? 'Saved the ' + what + '.' + ('archive' in change ? ' Reading what the archive holds…' : ' Syncing the data folder…')
    : 'Not saved: ' + ((r && r.error) || 'the helper did not answer') + '.');
  await showFolders(true);
  setTimeout(show, 3000);
}

$('b-archive').addEventListener('click', () => saveFolder({ archive: $('i-archive').value.trim() }, 'archive folder'));
$('b-data').addEventListener('click', () => saveFolder({ dataDir: $('i-data').value.trim() }, 'data folder'));

$('b-known').addEventListener('click', async () => {
  say('Reading what the archive holds…');
  const r = await chrome.runtime.sendMessage({ type: 'refreshKnown' });
  say(r && r.ok ? 'Done: ' + num(r.total) + ' items known.'
    : 'Could not read the archive: ' + ((r && r.error) || 'the add-on stopped before the archive was read - try again'));
  show();
});

$('b-backup').addEventListener('click', async () => {
  say('Syncing the data folder…');
  const r = await chrome.runtime.sendMessage({ type: 'syncFolder' });
  say(r && r.ok
    ? 'Done. ' + (r.merged && r.merged.length ? 'Merged ' + r.merged.length + ' file(s) from other computers. ' : '') +
      (r.written ? 'Saved this computer\'s file.' : 'This computer\'s file was already up to date.')
    : 'Could not sync: ' + (r && r.error));
  show();
});

$('b-history').addEventListener('click', async () => {
  say('Reading browser history…');
  const r = await chrome.runtime.sendMessage({ type: 'importHistory' });
  say(r && r.ok ? 'Done: ' + r.pages + ' pages found in history (visits already counted are not counted twice).' : 'Could not read history: ' + (r && r.error));
  show();
});

$('b-refresh').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({ type: 'requestFullClipRefresh' });
  say('Your full clippings list will be re-read the next time you open a Newspapers.com search or page.');
  show();
});

show();
