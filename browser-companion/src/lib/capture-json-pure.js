// capture-json-pure.js — pure, importable subset of capture-json.js.
//
// capture-json.js is a classic browser script that attaches to window.FHA.
// This file re-exports the same pure functions (slugify, bundleName, build, …)
// as a CommonJS module so they can be unit-tested under `node --test` without
// any browser globals.
//
// The browser still loads src/lib/capture-json.js (unchanged).  This module
// is for the test harness only; it must be kept in sync with capture-json.js.

'use strict';

const CAPTURE_JSON_SCHEMA = 2;

function slugify(text) {
  const slug = String(text || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
  return slug || 'capture';
}

function pad(n) {
  return String(n).padStart(2, '0');
}

function timestamp(d) {
  d = d || new Date();
  return (
    d.getFullYear() +
    pad(d.getMonth() + 1) +
    pad(d.getDate()) +
    '-' +
    pad(d.getHours()) +
    pad(d.getMinutes()) +
    pad(d.getSeconds()) +
    '-' +
    String(d.getMilliseconds()).padStart(3, '0')
  );
}

// Crockford Base32 - the archive's own ID alphabet (lowercase, no `ilou`).
// Path-safe, shell-safe, and `capture.py`'s `_safe_member_name` leaves it
// alone. 32 values is 5 bits, so `byte & 31` is an unbiased draw.
const TOKEN_ALPHABET = '0123456789abcdefghjkmnpqrstvwxyz';
const TOKEN_LENGTH = 6;

// Six random characters appended to every bundle folder name. A clock is not
// an identity: two side panels (or a clock adjustment) can produce the same
// `<slug>-<timestamp>`, and `conflictAction: 'uniquify'` renames the FILE, not
// the folder - so the two captures merge into one directory whose capture.json
// then names assets that may belong to the other capture, with the second
// capture parked unread. See capture-json.js for the full note.
function randomToken() {
  const source = (typeof crypto !== 'undefined' && crypto.getRandomValues)
    ? crypto : null;
  let out = '';
  if (source) {
    const bytes = new Uint8Array(TOKEN_LENGTH);
    source.getRandomValues(bytes);
    for (let i = 0; i < TOKEN_LENGTH; i++) out += TOKEN_ALPHABET[bytes[i] & 31];
    return out;
  }
  for (let i = 0; i < TOKEN_LENGTH; i++) {
    out += TOKEN_ALPHABET[Math.floor(Math.random() * TOKEN_ALPHABET.length)];
  }
  return out;
}

// `d` and `token` are injectable so a test can assert an exact string; every
// production call passes neither.
function bundleName(title, d, token) {
  return slugify(title) + '-' + timestamp(d) + '-' + (token || randomToken());
}

const DEFAULT_FOLDER = 'fha-inbox';

const BAD_CHARS = /[<>:"|?*$`!\u0000-\u001f]/g;
function sanitizeFolder(folder) {
  const segs = String(folder || '')
    .replace(/\\/g, '/')
    .split('/')
    .map((s) => s.replace(BAD_CHARS, '').replace(/[. ]+$/, '').trim())
    .filter((s) => s && s !== '.' && s !== '..');
  return segs.length ? segs.join('/') : DEFAULT_FOLDER;
}

function stagedPaths(filePath) {
  const raw = String(filePath || '').trim().replace(/\\/g, '/').replace(/\/+$/, '');
  const parts = raw ? raw.split('/') : [];
  if (parts.length < 3) return { bundle: '', staging: '' };
  const bundle = parts.slice(0, -1).join('/');
  const staging = parts.slice(0, -2).join('/');
  if (!staging || /^[A-Za-z]:$/.test(staging)) return { bundle: '', staging: '' };
  return { bundle: bundle, staging: staging };
}

const UNQUOTABLE = /["`$\r\n]/;

// `fha` is a launcher file at the archive root, not a program on PATH, so the
// card renders the prefixed form the shell needs. Rationale, and why Windows
// gets the PowerShell spelling, in capture-json.js.
const LAUNCHER_POSIX = './fha';
const LAUNCHER_WINDOWS = '.\\fha';

function launcher(nav) {
  const n = nav || (typeof navigator !== 'undefined' ? navigator : null);
  if (!n) return LAUNCHER_POSIX;
  const hinted = n.userAgentData && n.userAgentData.platform;
  if (hinted) {
    return /^windows$/i.test(String(hinted).trim())
      ? LAUNCHER_WINDOWS : LAUNCHER_POSIX;
  }
  if (n.platform) {
    return /^win/i.test(String(n.platform)) ? LAUNCHER_WINDOWS : LAUNCHER_POSIX;
  }
  return /windows/i.test(String(n.userAgent || ''))
    ? LAUNCHER_WINDOWS : LAUNCHER_POSIX;
}

function ingestCommand(stagingDir, nav) {
  const dir = String(stagingDir || '').trim();
  const cmd = launcher(nav) + ' capture --ingest';
  if (!dir || UNQUOTABLE.test(dir)) return cmd;
  return cmd + ' "' + dir + '"';
}

function ingestHint(folder, stagingDir) {
  const dir = String(stagingDir || '').trim();
  if (dir) {
    if (!UNQUOTABLE.test(dir)) return '';
    return 'Your captures are staged in ' + dir + '. That path cannot be '
      + 'pasted into a command as it stands, so point --ingest at it '
      + "yourself, or set capture_staging: to it in your archive's fha.yaml.";
  }
  const f = sanitizeFolder(folder);
  if (f === DEFAULT_FOLDER) return '';
  return 'Captures stage to a folder named "' + f + '" inside your '
    + "browser's download folder (Chrome: Settings > Downloads). If the "
    + "command finds nothing, add that folder's full path after --ingest, "
    + "or set capture_staging: to it in your archive's fha.yaml.";
}

function accessedDate(d) {
  d = d || new Date();
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

function build(fields) {
  const out = { schema: CAPTURE_JSON_SCHEMA };
  if (fields.url) out.url = fields.url;
  if (fields.title) out.title = fields.title;
  out.accessed = fields.accessed || accessedDate();
  if (fields.sourceDate) out.source_date = fields.sourceDate;
  if (fields.sourceType) out.source_type = fields.sourceType;
  if (fields.repository && fields.repository.trim()) out.repository = fields.repository.trim();

  const assets = (fields.assets || [])
    .filter((a) => a && a.file)
    .map((a) => {
      const entry = { file: String(a.file) };
      if (a.role) entry.role = String(a.role);
      if (a.mode) entry.mode = String(a.mode);
      if (a.provisional) entry.provisional = true;
      return entry;
    });
  out.assets = assets;

  const people = (fields.people || [])
    .map((p) => String(p || '').trim())
    .filter(Boolean);
  if (people.length) out.people = people;
  if (fields.notes && fields.notes.trim()) out.notes = fields.notes;
  if (fields.recipeHint) out.recipe_hint = fields.recipeHint;
  return out;
}

module.exports = {
  CAPTURE_JSON_SCHEMA, DEFAULT_FOLDER,
  slugify, timestamp, randomToken, bundleName, accessedDate, build,
  sanitizeFolder, stagedPaths, launcher, ingestCommand, ingestHint,
};
