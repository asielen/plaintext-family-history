// capture-json.js - assemble the §3 staged-bundle metadata.
//
// This is the seam the whole companion exists to fill (TOOLING_INGESTION §3):
// every delivery form converges on one artifact a `<slug>-<timestamp>-<token>/`
// bundle of `page.html` + optional asset files + `capture.json`. This module
// owns the shape of `capture.json` and the bundle's name, so the panel never
// hand-builds either. Keeping it small and pure (no chrome.* calls) makes it
// the one place to audit against the Python `capture._CAPTURE_JSON_SCHEMA`
// contract.
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
  // sortable and readable on the human's own machine; the durable accessed
  // *date* travels separately in capture.json (and overrides the scrape at
  // ingest, §6). Milliseconds keep two captures a moment apart in the order
  // they were made; they are NOT what makes the folder unique - see
  // randomToken below for that.
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

  // Crockford Base32 - the archive's own ID alphabet (SPEC: lowercase, no
  // `ilou`). Nothing in it needs escaping in a path, a shell, or a Windows
  // folder name, and `capture.py`'s `_safe_member_name` passes it through
  // untouched. 32 values is 5 bits, so a random byte masked with 31 is an
  // unbiased draw - no modulo skew to explain away.
  const TOKEN_ALPHABET = '0123456789abcdefghjkmnpqrstvwxyz';
  const TOKEN_LENGTH = 6;

  // Six random characters, appended to every bundle folder name.
  //
  // A clock is not an identity. Two side panels in two browser windows can
  // stage a same-titled capture in the same millisecond, and a clock adjustment
  // can hand the same millisecond out twice - and then both captures compute
  // the same `<slug>-<timestamp>` folder. `chrome.downloads.download` writes
  // with `conflictAction: 'uniquify'`, which renames the FILE, never the
  // folder, so the two captures MERGE into one directory:
  //
  //     page.html   record.jpg   capture.json
  //     page (1).html   record (1).jpg   capture (1).json
  //
  // Nothing about that folder looks wrong. `fha capture --ingest` reads the one
  // file literally named `capture.json` and resolves its `assets[].file` to the
  // one literally named `record.jpg` - and because the two captures' downloads
  // interleave, that record.jpg can belong to the OTHER capture. It files a
  // complete-looking source carrying the wrong evidence, then parks the whole
  // folder in `.ingested/` with the second capture never read at all. Both
  // failures are silent. Making the FOLDER the unique thing is what prevents
  // them; 32^6 is about 1.07e9, so two captures colliding is not a case that
  // has to be handled.
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
    // No WebCrypto (an old embedder, a stripped test host): Math.random is
    // weaker but this is a collision breaker, not a secret, and a predictable
    // token is still enormously better than none.
    for (let i = 0; i < TOKEN_LENGTH; i++) {
      out += TOKEN_ALPHABET[Math.floor(Math.random() * TOKEN_ALPHABET.length)];
    }
    return out;
  }

  // `<slug>-<timestamp>-<token>`. Slug and timestamp are for the human reading
  // his own Downloads folder; the token is what makes the name an identity.
  //
  // `d` and `token` are injectable for the same reason: a test needs an exact
  // string to assert. Every production call passes neither.
  function bundleName(title, d, token) {
    return slugify(title) + '-' + timestamp(d) + '-' + (token || randomToken());
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

  // Where a staged bundle actually landed, read out of the absolute path the
  // downloads API reports for a file inside it (DownloadItem.filename).
  // Returns { bundle, staging }: the bundle's own folder, and the folder above
  // it that HOLDS the bundles - the one `fha capture --ingest` sweeps. Both are
  // '' when the browser gave us nothing to read, which is the only honest
  // answer; a guess is what this replaced.
  //
  // Windows separators are folded to '/' (Python's Path and the shells both
  // take that form, and it keeps the copied command free of backslash
  // escaping). The last two segments are dropped rather than the folder
  // setting being matched off the end, so a nested setting
  // ('genealogy/staging') needs no special case.
  function stagedPaths(filePath) {
    const raw = String(filePath || '').trim().replace(/\\/g, '/').replace(/\/+$/, '');
    const parts = raw ? raw.split('/') : [];
    if (parts.length < 3) return { bundle: '', staging: '' };
    const bundle = parts.slice(0, -1).join('/');
    const staging = parts.slice(0, -2).join('/');
    // Nothing but a filesystem root or a bare drive letter left: not a folder
    // anyone can be sent to.
    if (!staging || /^[A-Za-z]:$/.test(staging)) return { bundle: '', staging: '' };
    return { bundle: bundle, staging: staging };
  }

  // A path safe to paste between double quotes in the shells this command is
  // copied into. `"` ends the quoting; `$` and a backtick still expand inside
  // double quotes in bash/zsh; a newline would split the command. Such a path
  // is reported by ingestHint as plain text instead of being mangled into a
  // command that runs somewhere else.
  const UNQUOTABLE = /["`$\r\n]/;

  // `fha` is a launcher FILE at the archive root, never a program on PATH
  // (AGENTS.md "Execution rules"), so a bare `fha …` is a command-not-found in
  // every shell the archive documents except the Windows Command Prompt. The
  // project spells it per shell and has done since the first owner doc -
  // `./fha <command>` on macOS/Linux, `.\fha <command>` in Windows PowerShell,
  // a bare `fha <command>` in cmd.exe (CHEATSHEET.md "Running fha";
  // GETTING_STARTED.md; docs/SETUP_FROM_ZIP.md). The card renders one of those
  // two prefixed forms rather than inventing a third spelling.
  //
  // ONE string, not the docs' three-line block, because this string is what the
  // Copy button puts on the clipboard: a block would paste into a terminal as
  // three commands, two of which are wrong for the shell that ran them. So the
  // command is rendered for the platform and the shell nuance stays out of it.
  //
  // Windows gets the PowerShell form because it is the one that works in BOTH
  // Windows shells: cmd.exe resolves a path-qualified `.\fha` through PATHEXT
  // to `fha.cmd` exactly as it resolves the bare name, while PowerShell
  // deliberately refuses the bare name. Rendering `.\fha` therefore strands
  // nobody; rendering the bare form would strand every PowerShell user.
  const LAUNCHER_POSIX = './fha';
  const LAUNCHER_WINDOWS = '.\\fha';

  // Which launcher spelling this machine needs.
  //
  // The extension cannot know the human's shell and has no business guessing
  // it, but it CAN know the operating system, which is what decides the path
  // separator. `userAgentData.platform` is the sanctioned low-entropy hint and
  // survives UA reduction; `navigator.platform` and the UA string are the
  // fallbacks for an embedder carrying neither. Pure in its argument so a test
  // hands it a fake instead of needing a browser.
  //
  // Unknown falls to the POSIX form deliberately: `./fha` is correct on
  // macOS/Linux AND accepted by PowerShell (which takes forward slashes as
  // separators), so the unknown case leaves only cmd.exe short - whereas
  // defaulting the other way would break every Mac and Linux user.
  function launcher(nav) {
    const n = nav || (typeof navigator !== 'undefined' ? navigator : null);
    if (!n) return LAUNCHER_POSIX;
    const hinted = n.userAgentData && n.userAgentData.platform;
    if (hinted) {
      return /^windows$/i.test(String(hinted).trim())
        ? LAUNCHER_WINDOWS : LAUNCHER_POSIX;
    }
    // `Win32`/`Win64` - anchored so macOS's `Darwin` cannot match on "win".
    if (n.platform) {
      return /^win/i.test(String(n.platform)) ? LAUNCHER_WINDOWS : LAUNCHER_POSIX;
    }
    return /windows/i.test(String(n.userAgent || ''))
      ? LAUNCHER_WINDOWS : LAUNCHER_POSIX;
  }

  // The exact command the handoff card offers for sweeping staged bundles in.
  //
  // It names a location ONLY when the browser has reported one. The download
  // directory is a browser setting, not a fixed folder under the home
  // directory - it is routinely moved to OneDrive, to another volume, or
  // carries a localized name - so the home-relative path this used to
  // synthesize was a guess presented as fact: the sweep it advertised searched
  // a folder the bundle had never been written to, and reported nothing to
  // ingest on a capture that had just succeeded. With no reported path the
  // bare command stands,
  // and the Python side resolves `capture_staging:` from fha.yaml (else its
  // own default) and says which folder it looked in. "Bare" is about the
  // DIRECTORY argument only - the launcher prefix is always there, because a
  // prefix-less `fha` is not a command anyone can run (see launcher above).
  function ingestCommand(stagingDir, nav) {
    const dir = String(stagingDir || '').trim();
    const cmd = launcher(nav) + ' capture --ingest';
    if (!dir || UNQUOTABLE.test(dir)) return cmd;
    return cmd + ' "' + dir + '"';
  }

  // The plain-language line under that command, for the two cases where the
  // command alone would leave the human hunting: a staging folder renamed but
  // not yet used (no capture, so no reported path), and a real path that
  // cannot be pasted into a shell as it stands. Both name a location he can
  // act on - his browser's own download setting, or fha.yaml's
  // `capture_staging:` - and neither invents one. Empty string when the
  // command already tells the whole truth.
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
    randomToken,
    bundleName,
    accessedDate,
    build,
    sanitizeFolder,
    stagedPaths,
    launcher,
    ingestCommand,
    ingestHint,
  };
})();
