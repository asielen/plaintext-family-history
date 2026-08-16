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

function bundleName(title, d) {
  return slugify(title) + '-' + timestamp(d);
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

function ingestCommand(folder) {
  const f = sanitizeFolder(folder);
  if (f === DEFAULT_FOLDER) return 'fha capture --ingest';
  return 'fha capture --ingest "~/Downloads/' + f + '"';
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
  slugify, timestamp, bundleName, accessedDate, build,
  sanitizeFolder, ingestCommand,
};
