// capture-json.js - assemble the §3 staged-bundle metadata.
//
// This is the seam the whole companion exists to fill (TOOLING_INGESTION §3):
// every delivery form converges on one artifact a `<slug>-<timestamp>/` bundle
// of `page.html` + optional asset files + `capture.json`. This module owns the
// shape of `capture.json` and the bundle's name, so the panel never hand-builds
// either. Keeping it small and pure (no chrome.* calls) makes it the one place
// to audit against the Python `capture._CAPTURE_JSON_SCHEMA` contract.
//
// Schema 2 (this build): the single `asset_mode`/`asset_file` pair becomes an
// `assets: [{file, role, mode, provisional?}]` LIST, so one capture can carry
// BOTH a self-contained page copy (role `webpage`) AND a separate evidence file
// (role `record`) - the "both" case the panel's design enables. The raw
// `page.html` is still ALWAYS saved as the scrape source, separate from the
// listed assets. Ingest is forgiving and back-compatible: it reads schema 1's
// `asset_mode`/`asset_file` too (§3).
//
// Loaded as a classic script in panel.html; attaches to the global `FHA`.

(function () {
  const FHA = (window.FHA = window.FHA || {});

  // Must equal tools/capture.py `_CAPTURE_JSON_SCHEMA`. Ingest is forgiving about
  // this (absent = current, newer = read shared fields + warn), but we emit the
  // exact current version so a stub processes cleanly with no warning (§3).
  const CAPTURE_JSON_SCHEMA = 2;

  // Match tools/capture.py `_slugify` so the browser-made bundle name lines up
  // with what the engine would have chosen from the same title: lowercase, every
  // run of non-alphanumerics → a single hyphen, trimmed, never empty.
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

  // Local-time stamp `YYYYMMDD-HHMMSS-mmm`. The folder name only has to be
  // unique and sortable on the human's own machine; the durable accessed *date*
  // travels separately in capture.json (and overrides the scrape at ingest, §6).
  // Milliseconds close the same-second collision: two same-title captures in one
  // second would otherwise share a folder, and the downloads API's `uniquify`
  // renames the FILES inside it (`capture (1).json`) - one mixed folder ingest
  // half-reads - rather than the folder itself.
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

  // The staging folder Chrome writes under Downloads. The default must match
  // capture.py `_DEFAULT_STAGING`'s folder name, so the bare `fha capture
  // --ingest` sweeps exactly where a default-settings panel stages.
  const DEFAULT_FOLDER = 'fha-inbox';

  // Normalize a human-typed staging-folder setting into something the
  // downloads API will accept as a Downloads-relative subpath: backslashes
  // become slashes, empty/'.'/'..' segments are dropped (the API rejects
  // escapes anyway, but at capture time with a raw browser error - this makes
  // the setting self-correcting at typing time instead). An input with nothing
  // left falls back to the default rather than staging into Downloads' root.
  // Characters the downloads API rejects in a filename (`<>:"|?*`, controls),
  // plus the ones that break the copied `fha capture --ingest "..."` command
  // inside double quotes on some shell (`$`, backtick, `!` under interactive
  // bash) - dropped rather than escaped, because the command is pasted into
  // whichever shell the human has, and one escaping cannot fit them all. A
  // segment's trailing dots/spaces go too (invalid on Windows), and a bare
  // drive segment (`C:`) is not a Downloads subfolder.
  const BAD_CHARS = /[<>:"|?*$`!\u0000-\u001f]/g;
  function sanitizeFolder(folder) {
    const segs = String(folder || '')
      .replace(/\\/g, '/')
      .split('/')
      .map((s) => s.replace(BAD_CHARS, '').replace(/[. ]+$/, '').trim())
      .filter((s) => s && s !== '.' && s !== '..');
    return segs.length ? segs.join('/') : DEFAULT_FOLDER;
  }

  // The exact command the handoff card offers for sweeping staged bundles in.
  // A renamed staging folder MUST surface here: the bare `fha capture --ingest`
  // only sweeps `capture_staging:`/`~/Downloads/fha-inbox`, so copying it after
  // staging into a custom folder finds nothing - the DIR argument (which the
  // Python side `~`-expands on every OS) points the sweep at the right place.
  function ingestCommand(folder) {
    const f = sanitizeFolder(folder);
    if (f === DEFAULT_FOLDER) return 'fha capture --ingest';
    return 'fha capture --ingest "~/Downloads/' + f + '"';
  }

  // ISO `YYYY-MM-DD` for the `accessed` field - the date the human actually
  // viewed the page. capture.py uses it as the search-log date and the
  // external_links accessed-date, so it is a real durable field, not cosmetic.
  function accessedDate(d) {
    d = d || new Date();
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  // Build the capture.json object from the panel's collected fields. Every field
  // except `url` is optional (§3); we OMIT empties rather than emit nulls so the
  // file stays clean and the engine's "absent" and "blank" paths agree.
  //
  // `assets` is the schema-2 list the caller assembles - one entry per staged
  // asset file, each with its `role` (`webpage` for the page copy, `record` for
  // the evidence), `mode` (singlefile/fetch/manual), and optional `provisional`
  // flag. An EMPTY list is the pointer-only case (§13b case (c)): no asset, which
  // ingest turns into `asset_elsewhere: true`.
  //
  // fields: { url, title, accessed, sourceDate, sourceType, people[], notes,
  //           recipeHint, assets: [{file, role, mode, provisional?}] }
  function build(fields) {
    const out = { schema: CAPTURE_JSON_SCHEMA };
    if (fields.url) out.url = fields.url;
    if (fields.title) out.title = fields.title;
    out.accessed = fields.accessed || accessedDate();
    if (fields.sourceDate) out.source_date = fields.sourceDate;
    if (fields.sourceType) out.source_type = fields.sourceType;
    if (fields.repository && fields.repository.trim()) out.repository = fields.repository.trim();

    // Normalize the asset list: drop entries with no file, keep role/mode and a
    // provisional flag only when true (omit the noise). The list is always
    // emitted (even empty) so a schema-2 reader can tell "no assets" (case (c))
    // from "field missing" unambiguously.
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

  FHA.captureJson = {
    SCHEMA: CAPTURE_JSON_SCHEMA,
    DEFAULT_FOLDER,
    slugify,
    timestamp,
    bundleName,
    accessedDate,
    build,
    sanitizeFolder,
    ingestCommand,
  };
})();
