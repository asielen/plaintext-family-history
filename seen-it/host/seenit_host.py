#!/usr/bin/env python3
"""seenit_host.py - the optional local helper for the Seen It browser add-on.

Seen It works on its own, with everything kept in the browser. This helper is
only for its "Advanced mode", and it does two jobs the browser cannot do by
itself:

1. **Mark what your family archive already holds.** It reads (never writes) a
   Plaintext Family History archive and answers which Newspapers.com clippings,
   Ancestry and FamilySearch records and Find a Grave memorials are already
   filed as sources, or captured and still waiting in the inbox.
2. **Keep the add-on's data in a real folder.** Chrome only lets an add-on save
   files into Downloads, which gets tidied away. The helper keeps one data file
   per computer (seen-it-<device>.json) in a folder you choose, so a sync tool
   can carry it between computers.

Where things live
-----------------
Settings live in one small per-user file, never in the add-on's folder and
never in the archive:

    Windows:        %LOCALAPPDATA%\\SeenIt\\config.json
    macOS / Linux:  ~/.config/seen-it/config.json   ($XDG_CONFIG_HOME is honored)

    {
      "archive":  "<the archive's top folder - the one holding fha.yaml>",
      "data_dir": "<where the seen-it-<device>.json files go>",
      "capture_staging": "<optional: where the capture add-on downloads bundles>"
    }

Every key is optional. With no `data_dir` the data goes to a per-user folder
(%LOCALAPPDATA%\\SeenIt\\data, or ~/.local/share/seen-it). The data folder holds
your browsing history, so the helper refuses one inside a family archive, any
of its roots (photos, documents, inbox - wherever fha.yaml puts them), the
capture download folder, or a git checkout: that history would otherwise be
committed, filed or shared.

The archive is located only through `archive`. From it the helper reads:

- fha.yaml `roots:` `documents` - filed sources (their *site-data*.json files
  carry the S-id in the filename);
- fha.yaml `roots:` `inbox` - captured, not filed yet: bundles, and the
  *.notes.md stubs `fha capture --ingest` writes (a `processed/` holding
  folder counts as processed);
- `sources/` - source records: the page addresses in a record's frontmatter
  (external_links, citation) mark it filed with its S-id; a Find a Grave
  memorial a record cites is noted as fagidx: (linked, photos not captured);
- the capture add-on's download folder: `capture_staging` here, else fha.yaml
  `capture_staging:`, else ~/Downloads/fha-inbox, plus any `<that>-*` sibling
  (chunk folders split off for filing).

A page image (img:, fsimg:, page:) is counted only when a saved image backs
it - the record says `imageSaved: true`, or the image file is beside it - never
from the image id a record links to (_keys_from_site_data, _holds_saved_image).
Likewise a Find a Grave memorial is fag: (captured) only when its photos are
saved, each one fagphoto:{photoId} from its saved file; a memorial's address
alone - a capture's page address, an inbox stub, a source that cites it - is
fagidx: (known, photos still to come) (_holds_saved_file).

Roots resolve exactly as the archive's own tools do (tools/_lib.py
`resolve_path`): an absolute value is used as-is, a relative one is joined to
the archive folder, and a missing one means the folder of that name inside the
archive. fha.yaml is read with PyYAML when it is installed; otherwise a small
built-in reader handles the two keys used here (`_parse_fha_yaml_minimal`).

Extra folders still go in known-config.json inside the data folder, as before:
{"filed": [...], "captured": [...], "sources": [...]} - each list ADDS to what
the archive gives (a `*` glob is allowed in `captured`).

How Chrome talks to it
----------------------
Chrome starts this program fresh for every message. Each message is a 4-byte
little-endian length followed by that many bytes of UTF-8 JSON, both ways, and
a reply is capped at 1 MB, which is why `read` and `known` answer in pages.

Because each message is a fresh start, two small caches sit beside the
settings file (this computer only; never in the archive, never in the synced
data folder): known-cache.json, the last answer to `known`, which its later
pages reuse for 10 minutes; and known-files.json, what each archive file said
the last time it was read, keyed on the file's size and modification time, so
the first page of a visit reads only the files changed since (_KnownFiles).
Deleting either costs one slower scan, nothing else.

The installers run it from the command line instead: `--install` saves the
settings, writes a small launcher beside them, and registers that launcher
with the browser (current user only). `--setup` saves the settings alone.

Requests (all JSON objects with an `op`):
  config     what is set: archive, data folder, settings file, any problem
  setConfig  change `archive` and/or `dataDir` (checked before saving)
  ping       is the helper there; its data folder
  list / read / write   the seen-it-<device>.json files in the data folder
  known      what the archive holds, in pages of 4000 keys

Code map
--------
  where settings live   config_dir, config_path, default_data_dir,
                        load_settings, save_settings, data_dir_problem,
                        resolve_data_dir
  the archive's config  read_fha_yaml, _parse_fha_yaml_minimal, resolve_root,
                        archive_problem, scan_folders
  what the archive has  _FolderFiles (one listing per folder per scan),
                        _holds_saved_image, _holds_saved_file, _keys_from_site_data,
                        _keys_from_url, scan_known,
                        known_items
  remembering files     _KnownFiles (known-files.json: what each file said, by
                        size and mtime), _reader_fingerprint, _replace_with_retry,
                        _source_record_facts, _stub_facts, _capture_json_facts,
                        _site_data_facts, _site_data_still_true
  the message loop      read_message, send, safe_path, describe, _set_config,
                        handle, main
  installer support     host_manifest, write_launcher, browser_manifest_dirs,
                        register_windows, setup, install, cli
"""

import argparse
import functools
import glob
import hashlib
import json
import os
import re
import shlex
import struct
import sys
import tempfile
import time
from pathlib import Path, PurePath

HOST_NAME = 'com.genealogy.seenit'
# The add-on's ID. It is fixed by the public `key` in manifest.json, so it is
# the same on every computer, and the helper answers only this add-on.
EXTENSION_ID = 'cennkaddolfbhiabacldomegncpdphed'

NAME_RE = re.compile(r'^seen-it-[a-z0-9]{1,16}\.json$')
CHUNK_CHARS = 200_000  # at most 4 bytes each in UTF-8: under Chrome's 1 MB cap on a reply
KNOWN_PAGE = 4000
KNOWN_CACHE_SECONDS = 600
DEFAULT_STAGING = '~/Downloads/fha-inbox'

SID_RE = re.compile(r'_(S-[0-9a-z]{10})\.json$', re.I)
NP_ARTICLE_RE = re.compile(r'newspapers\.com/(?:article|clip)/(?:[^/]+/)?(\d+)')
FS_RECORD_RE = re.compile(r'familysearch\.org/(?:[a-z]{2}/)?ark:/61903/1:1:([A-Z0-9-]+)', re.I)
FAG_MEMORIAL_RE = re.compile(r'findagrave\.com/memorial/(\d+)', re.I)
SOURCE_ID_RE = re.compile(r'^id:\s*["\']?(S-[0-9a-z]{10})', re.M)
ANC_RECORD_RE = re.compile(
    r'/(?:search/collections|collections)/(\d+)/records/(\d+)'
    r'|discoveryui-content/view/(\d+):(\d+)'
    r'|/interactive/(\d+)/[^/?#]+/(\d+)')


class SettingsError(Exception):
    """A problem the person can fix; the message says how, in plain words."""


# ── where settings live ─────────────────────────────────────────────────────

def config_dir():
    """The per-user folder for the helper's own settings.

    Per user and per computer on purpose: it holds this computer's paths, so it
    must never sit in a synced or shared folder. SEENIT_CONFIG_DIR overrides it
    (for tests, or for someone who keeps settings elsewhere).
    """
    override = os.environ.get('SEENIT_CONFIG_DIR')
    if override:
        return Path(override).expanduser()
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/AppData/Local')
        return Path(base) / 'SeenIt'
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return Path(base) / 'seen-it'


def config_path():
    return config_dir() / 'config.json'


def default_data_dir():
    """Where the data files go when no data_dir is set: a per-user folder.

    Never next to the add-on (which may be a git checkout) and never in the
    archive: the files hold your browsing history.
    """
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/AppData/Local')
        return Path(base) / 'SeenIt' / 'data'
    base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    return Path(base) / 'seen-it'


def load_settings(path=None):
    """The settings file as a dict; a missing file is an empty one.

    A file that cannot be read as JSON is an error with the fix named, rather
    than being silently ignored: ignoring it would quietly move the data folder
    back to the default and the add-on would seem to forget everything.
    """
    path = Path(path) if path else config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        raise SettingsError(
            'the settings file %s could not be read (%s) - fix it in a text editor, '
            'or delete it and run the installer again' % (path, e))
    if not isinstance(data, dict):
        raise SettingsError('the settings file %s should hold {"archive": "...", "data_dir": "..."} '
                            '- delete it and run the installer again' % path)
    return data


def save_settings(settings, path=None):
    """Write the settings file atomically (a crash never leaves half a file)."""
    path = Path(path) if path else config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.writing-', dir=str(path.parent))
    with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write('\n')
    os.replace(tmp, path)
    return path


def _expand(p):
    """A folder path as typed or stored: surrounding spaces dropped, `~` expanded.

    Nothing else is expanded: a `$` or `%` in a folder name is part of the
    name, and expanding it as an environment variable would quietly read a
    different folder.
    """
    return Path(os.path.expanduser(str(p).strip()))


def _archive_owned_folders(archive, settings):
    """Every folder that is part of the archive, by a name a person recognizes.

    The archive folder itself, every root its fha.yaml names (photos,
    documents and inbox often live on another drive - and the inbox is listed
    and filed by the archive's tools), and the capture download folder that
    `fha capture --ingest` sweeps into the inbox.
    """
    out = []
    fha_config = {}
    if archive:
        a = _expand(archive)
        out.append(('your family archive', a))
        try:
            fha_config = read_fha_yaml(a) if (a / 'fha.yaml').is_file() else {}
        except (OSError, SettingsError):
            fha_config = {}
        roots = fha_config.get('roots') if isinstance(fha_config.get('roots'), dict) else {}
        for alias in sorted(set(roots) | {'photos', 'documents', 'inbox'}):
            out.append(("your archive's %s folder" % alias, resolve_root(alias, fha_config, a)))
    out.append(('the capture download folder', capture_staging(settings or {}, fha_config)))
    return out


def data_dir_problem(path, archive=None, settings=None):
    """Why `path` must not hold the data files, or None when it is fine.

    The data files are a record of everything you looked at. Inside a family
    archive - including its photos, documents and inbox folders wherever they
    live - or any git checkout they would be committed, filed or shared along
    with it, so all of those are refused, as is any folder above `path` that
    holds an fha.yaml or a .git.
    """
    p = _expand(path).resolve()
    for label, folder in _archive_owned_folders(archive, settings):
        f = Path(folder).resolve()
        if p == f or f in p.parents:
            return ('that folder is inside %s (%s) - the data files hold your '
                    'browsing history, so keep them outside it' % (label, f))
    for d in (p, *p.parents):
        if (d / 'fha.yaml').is_file():
            return ('that folder is inside a family archive (%s) - the data files hold your '
                    'browsing history, so keep them outside it' % d)
        if (d / '.git').exists():
            return ('that folder is inside a git folder (%s) - the data files hold your browsing '
                    'history and would be committed with it; pick a folder outside it' % d)
    return None


def resolve_data_dir(settings):
    """The data folder to use, checked; raises SettingsError when unsafe."""
    raw = settings.get('data_dir')
    path = _expand(raw) if raw else default_data_dir()
    # A relative path would land wherever the browser happened to start the
    # helper - possibly inside the add-on's own folder.
    problem = (None if path.is_absolute() else 'write it as a full folder path, starting from the drive or /')
    problem = problem or data_dir_problem(path, settings.get('archive'), settings)
    if problem:
        raise SettingsError('the data folder %s cannot be used: %s. Set another one under '
                            '"Data folder" in the Seen It window, or in %s' % (path, problem, config_path()))
    return path


# ── the archive's own config ────────────────────────────────────────────────

def _unquote(value):
    """A YAML scalar's text: quotes removed, a trailing # comment dropped."""
    v = value.strip()
    if v[:1] in ('"', "'"):
        q = v[0]
        end = v.find(q, 1)
        while q == "'" and end != -1 and v[end + 1:end + 2] == "'":
            end = v.find(q, end + 2)  # '' is an escaped quote inside '...'
        inner = v[1:end] if end != -1 else v[1:]
        if q == "'":
            return inner.replace("''", "'")
        return inner.replace('\\\\', '\x00').replace('\\"', '"').replace('\x00', '\\')
    m = re.search(r'\s#', v)
    if m:
        v = v[:m.start()]
    return v.strip()


def _parse_fha_yaml_minimal(text):
    """Read just `roots:` and `capture_staging:` from fha.yaml text, no PyYAML.

    The helper runs under whatever Python Chrome finds, which may not have
    PyYAML, and it needs only two keys, so this reads the forms fha.yaml
    actually uses:

        roots:                       # a block of `alias: path` lines, indented
          documents: /Volumes/Docs   # trailing comments dropped
          inbox: "inbox"             # single or double quotes allowed
        roots: {documents: docs}     # the one-line form
        capture_staging: ~/Downloads/fha-inbox

    Anything else in the file (site:, biography:, lists, nested blocks) is
    skipped. Values come back as strings.
    """
    out = {}
    roots = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.lstrip()
        if not stripped or stripped.startswith('#'):
            continue
        indented = line != stripped
        if indented:
            if roots is not None:
                m = re.match(r'([A-Za-z0-9_-]+)\s*:\s*(.*)$', stripped)
                if m and m.group(2).strip():
                    roots[m.group(1)] = _unquote(m.group(2))
            continue
        roots = None
        m = re.match(r'([A-Za-z0-9_-]+)\s*:\s*(.*)$', stripped)
        if not m:
            continue
        key, rest = m.group(1), m.group(2).strip()
        if key == 'roots':
            out['roots'] = {}
            if rest.startswith('{'):
                body = rest[1:rest.rfind('}')] if '}' in rest else rest[1:]
                for part in body.split(','):
                    if ':' in part:
                        k, v = part.split(':', 1)
                        out['roots'][k.strip().strip('\'"')] = _unquote(v)
            elif not rest or rest.startswith('#'):
                roots = out['roots']
        elif key == 'capture_staging' and rest and not rest.startswith('#'):
            out['capture_staging'] = _unquote(rest)
    return out


def read_fha_yaml(archive, use_yaml=True):
    """The archive's fha.yaml as a dict ({} when it has none).

    PyYAML when it is installed (the archive's own tools use it), otherwise the
    small reader above. A file PyYAML cannot read is an error with the fix
    named - silently dropping a configured documents root would mark every
    filed source "not in archive".
    """
    path = Path(archive) / 'fha.yaml'
    text = path.read_text(encoding='utf-8-sig')  # Notepad may have added a BOM
    if use_yaml:
        try:
            import yaml
        except ImportError:
            yaml = None
        if yaml is not None:
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as e:
                raise SettingsError('the archive settings file %s has a mistake in it (%s) - '
                                    'run `fha doctor` in the archive to find it' % (path, e))
            return data if isinstance(data, dict) else {}
    return _parse_fha_yaml_minimal(text)


def resolve_root(alias, fha_config, archive):
    """An archive folder by its alias, the way tools/_lib.py resolve_path does.

    Deliberately the same rule, not a friendlier one (no `~` expansion): the
    helper must find a root exactly where the archive's own tools put files,
    or it would mark a filed source "not in archive".
    """
    roots = fha_config.get('roots') if isinstance(fha_config, dict) else None
    roots = roots if isinstance(roots, dict) else {}
    archive = Path(archive)
    if alias in roots:
        value = str(roots[alias])
        return Path(value) if os.path.isabs(value) else archive / value
    return archive / alias


def archive_problem(archive):
    """Why `archive` is not an archive folder, or None when it is."""
    if not archive:
        return 'no archive folder is set'
    p = _expand(archive)
    if not p.is_absolute():
        return 'write the archive folder as a full path, starting from the drive or /'
    if not p.is_dir():
        return 'the archive folder %s was not found (is the drive connected?)' % p
    if not (p / 'fha.yaml').is_file():
        return ('%s has no fha.yaml - pick the archive\'s top folder, the one that holds '
                'fha.yaml, SPEC.md and the sources folder' % p)
    return None


def _globbed(patterns):
    """Expand the `captured` list into real folders.

    Every entry is a glob pattern: the ones the helper builds itself (the
    inbox, the capture download folder) are passed through `glob.escape`
    first, so a folder named `Family Archive [2024]` is read as itself rather
    than as a pattern that matches nothing. Only known-config.json entries are
    the person's own patterns.
    """
    out = []
    for pattern in patterns:
        pattern = os.path.expanduser(str(pattern))
        out.extend(sorted(glob.glob(pattern)))
    return out


def _literal(path):
    """A real folder path, made safe to put in the `captured` pattern list."""
    return glob.escape(str(path))


def capture_staging(settings, fha_config):
    """The capture add-on's download folder, `~` expanded.

    The settings file wins, then fha.yaml `capture_staging:` (the same key
    `fha capture --ingest` reads), then the default ~/Downloads/fha-inbox.
    """
    value = settings.get('capture_staging') or fha_config.get('capture_staging') or DEFAULT_STAGING
    return _expand(value)


def scan_folders(settings, data_dir=None, use_yaml=True):
    """Which folders to read, by kind: {'filed', 'sources', 'captured', 'required'}.

    'captured' entries are glob patterns (real folders escaped, see _globbed);
    'required' lists the archive folders whose absence is reported as missing.

    The archive gives its documents root (filed), its sources/ folder, and its
    inbox (captured); the capture add-on's download folder is captured too.
    known-config.json in the data folder adds more. Raises SettingsError when
    there is nothing to read at all, so the add-on can say how to fix it
    instead of quietly marking everything "not in archive".
    """
    folders = {'filed': [], 'sources': [], 'captured': []}
    fha_config = {}
    archive = settings.get('archive')
    if archive:
        problem = archive_problem(archive)
        if problem:
            raise SettingsError(problem + '. Set the archive folder in the Seen It window '
                                '(Advanced mode), or run the installer again with it')
        archive = _expand(archive)
        fha_config = read_fha_yaml(archive, use_yaml=use_yaml)
        folders['filed'].append(str(resolve_root('documents', fha_config, archive)))
        folders['sources'].append(str(archive / 'sources'))
        inbox = resolve_root('inbox', fha_config, archive)
        folders['captured'].append(_literal(inbox))
        # The archive's inbox must be there (unlike the download folder, which
        # a computer that never captured does not have): when it is not - an
        # inbox on a drive not connected - say so, rather than calling
        # everything waiting in it "not in archive".
        folders['required'] = [str(inbox)]
    staging = str(capture_staging(settings, fha_config)).rstrip('/\\')
    folders['captured'] += [_literal(staging), _literal(staging) + '-*']

    extra_path = Path(data_dir) / 'known-config.json' if data_dir else None
    if extra_path and extra_path.is_file():
        try:
            extra = json.loads(extra_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            extra = {}
        for kind in ('filed', 'sources', 'captured'):
            more = extra.get(kind) if isinstance(extra, dict) else None
            if isinstance(more, str):
                more = [more]
            for item in more or []:
                if isinstance(item, str) and item not in folders[kind]:
                    folders[kind].append(item)
    if not archive and not folders['filed'] and not folders['sources']:
        raise SettingsError('no archive folder is set yet - paste it under "Archive folder" in the '
                            'Seen It window (Advanced mode), or run the installer again with it')
    return folders


# ── what the archive already holds ──────────────────────────────────────────

# Image files a bundle or a filed source keeps its scans in.
IMAGE_EXTS = frozenset({'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.gif', '.webp', '.jp2'})


class _FolderFiles:
    """What each folder beside a site-data file holds, listed once per scan.

    Whether a record's scan or a memorial's photo is saved is answered from the
    files beside its site-data file (_holds_saved_image, _holds_saved_file).
    A filed census folder can hold a couple of thousand site-data files and
    twice as many scans, and listing that whole folder again for every one of
    them - over 17 million names on one real archive - was most of the minute
    a first page of "already looked at" marks used to take. So each folder is
    listed once per scan, and its files are indexed by every S-id their names
    contain.

    One instance lives for one scan only: a folder listed for an earlier
    request in the same process would be out of date, and a deleted scan must
    stop counting as saved on the very next scan.
    """

    def __init__(self):
        self._listed = {}
        self._capture = {}

    def files(self, folder):
        """`(all files, files by S-id)` in `folder`, or None when it cannot be listed.

        Each file is `(lower name, lower stem, lower suffix)`, split the way
        pathlib splits it (a name like `.jpg` has no suffix). By S-id means:
        under every `s-` + 10 characters slice of the lower-cased name, so
        `sid in name.lower()` becomes one dictionary lookup.
        """
        key = str(folder)
        if key not in self._listed:
            rows, by_sid = [], {}
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        try:
                            if not entry.is_file():   # follows links, as Path.is_file does
                                continue
                        except OSError:
                            continue
                        name = entry.name.lower()
                        pure = PurePath(entry.name)
                        row = (name, pure.stem.lower(), pure.suffix.lower())
                        rows.append(row)
                        for i in range(len(name) - 11):
                            if name.startswith('s-', i):
                                by_sid.setdefault(name[i:i + 12], []).append(row)
                self._listed[key] = (rows, by_sid)
            except OSError:
                self._listed[key] = None
        return self._listed[key]

    def capture_assets(self, folder):
        """`(has capture.json, its assets list or None)`, read once per folder."""
        key = str(folder)
        if key not in self._capture:
            cap = Path(folder) / 'capture.json'
            assets = None
            present = cap.is_file()
            if present:
                try:
                    assets = json.loads(cap.read_text(encoding='utf-8')).get('assets')
                except (OSError, ValueError, AttributeError):
                    assets = None
            self._capture[key] = (present, assets)
        return self._capture[key]


def _holds_saved_image(path, roles, folders=None):
    """Does the bundle or source folder beside this site-data file hold a saved
    image - not merely the id of one?

    A staged bundle's capture.json lists its files: a listed, present,
    non-provisional asset with one of `roles` counts (a screen capture is a
    stand-in, not the scan). Without one (an ingested inbox bundle, a filed
    source), an image file in the same folder counts - for a filed source,
    one carrying the same S-id as the site-data file.

    `folders` (a _FolderFiles) shares one listing of each folder across a
    whole scan; without it the folder is listed for this one question.
    """
    folders = folders or _FolderFiles()
    folder = path.parent
    present, assets = folders.capture_assets(folder)
    if present and isinstance(assets, list):
        return any(
            isinstance(a, dict) and a.get('role') in roles and not a.get('provisional')
            and a.get('file') and (folder / str(a['file'])).is_file()
            for a in assets)
    m = SID_RE.search(path.name)
    sid = m.group(1).lower() if m else None
    listed = folders.files(folder)
    if listed is None:
        return False
    rows, by_sid = listed
    return any(suffix in IMAGE_EXTS for _name, _stem, suffix in (rows if sid is None else by_sid.get(sid, ())))


def _holds_saved_file(path, name, folders=None):
    """Does the bundle or source folder beside this site-data file hold the
    file its data names (a memorial photo the capture saved as `name`)?

    A staged bundle's capture.json must list it, present and not provisional.
    Without one, the file itself counts: by its own name in an ingested inbox
    bundle, and in a filed source by the name `fha process` gives it there -
    its old name, a role suffix and the source's S-id
    (`grave-1-grave-1_S-….jpg`), the S-id being the one in this file's name.

    `folders` shares one listing of each folder across a scan, as for
    _holds_saved_image.
    """
    name = Path(str(name or '')).name
    if not name:
        return False
    folders = folders or _FolderFiles()
    folder = path.parent
    present, assets = folders.capture_assets(folder)
    if present and isinstance(assets, list):
        return any(isinstance(a, dict) and str(a.get('file') or '') == name and not a.get('provisional')
                   and (folder / name).is_file() for a in assets)
    m = SID_RE.search(path.name)
    if not m:
        # an ingested inbox bundle: its files keep their names
        return (folder / name).is_file()
    # A filed source: its files carry its S-id. A bare `grave-1.jpg` in the
    # same shared folder is someone else's (or nobody's) - PR #221 review.
    sid = m.group(1).lower()
    stem, suffix = Path(name).stem.lower(), Path(name).suffix.lower()
    listed = folders.files(folder)
    if listed is None:
        return False
    return any(f_suffix == suffix and (f_stem == stem or f_stem.startswith((stem + '-', stem + '_')))
               for _name, f_stem, f_suffix in listed[1].get(sid, ()))


def _keys_from_site_data(data, image_saved=None, file_saved=None):
    """The item keys one site-data file speaks for.

    Capture bundles carry a small JSON describing the item on its site: a
    Newspapers.com clipping and its page, or one or more records (Ancestry
    dbId/recordId, FamilySearch ARK ids, a Find a Grave memorial id).

    A record or clipping key means its index is saved - the file itself is
    that. An IMAGE key (img:, fsimg:, page:) means the scan is saved, and the
    file only names the scan a record links to, so it needs more: the record
    says `imageSaved: true` (the capture companion writes it, false when the
    scan was refused or only a screen capture was kept), or - for files made
    before that field - `image_saved(roles)` finds the saved image beside the
    file. A record whose page sits in another bundle (`same_image_as`) never
    speaks for the image: that bundle's own file does. A list of records
    matched to a source after the fact (`records`) speaks for an image only
    with `imageSaved: true`, since one image file cannot say whose it is.
    Without `image_saved`, only `imageSaved: true` counts.

    A Find a Grave memorial is fag: (captured) only when every one of its
    photos is saved - in this bundle (`file`, and `file_saved(name)` finds
    it beside the data file) or already elsewhere (`same_image_as`) - and
    the gallery was read in full; else fagidx: (known, its photos still to
    come). Each photo saved here is also fagphoto:{photoId}, so a later
    capture does not download it again. Without `file_saved`, a `file` the
    data names counts.
    """
    keys = []
    if not isinstance(data, dict):
        return keys
    check = image_saved or (lambda roles: False)
    has_file = file_saved or (lambda name: True)

    def image_ok(rec, roles, listed):
        if 'imageSaved' in rec:
            return rec.get('imageSaved') is True
        if rec.get('same_image_as') or listed:
            return False
        return check(roles)

    clip = data.get('clipping')
    if isinstance(clip, dict) and clip.get('id'):
        keys.append('clip:%s' % clip['id'])
        page = data.get('page') or {}
        if isinstance(page, dict) and page.get('id') and image_ok(page, ('page',), False):
            keys.append('page:%s' % page['id'])
    recs = []
    if isinstance(data.get('record'), dict):
        recs.append((data['record'], False))
    # a source matched to its Ancestry records after the fact (one census
    # source can hold several years): ancestry-records-site-data_S-….json
    if isinstance(data.get('records'), list):
        recs.extend((r, True) for r in data['records'] if isinstance(r, dict))
    for rec, listed in recs:
        if rec.get('memorialId'):
            # A memorial counts as captured (fag:) only with its photos: the
            # capture lists each photo with the `file` it was saved as (null
            # when it failed, was over the limit, or is saved elsewhere -
            # `same_image_as`) and `photosTotal` (null when the gallery could
            # not be read). Short of all of them it is known but its photos
            # are not captured - fagidx:, as for a memorial a record only
            # links - never fag: from the memorial's facts alone.
            photos = data.get('photos')
            total = data.get('photosTotal')
            # `photosPartial`: the capture read only part of its photo list
            # (a later page failed, the count was not reached) - PR #221 audit.
            complete = isinstance(photos, list) and not listed and data.get('photosPartial') is not True
            saved_here = []
            for ph in photos if isinstance(photos, list) else []:
                if not isinstance(ph, dict):
                    complete = False
                    continue
                if ph.get('file') and has_file(ph['file']):
                    if ph.get('photoId'):
                        saved_here.append('fagphoto:%s' % ph['photoId'])
                elif not ph.get('same_image_as'):
                    complete = False
            if complete and (not isinstance(total, int) or isinstance(total, bool) or total > len(photos)):
                complete = False
            keys.append(('fag:%s' if complete else 'fagidx:%s') % rec['memorialId'])
            keys.extend(saved_here)
            continue
        if rec.get('fsId'):
            keys.append('fsrec:%s' % str(rec['fsId']).upper())
            if rec.get('imageId') and image_ok(rec, ('record',), listed):
                keys.append('fsimg:%s' % str(rec['imageId']).upper())
            continue
        if rec.get('dbId') and rec.get('recordId'):
            keys.append('rec:%s:%s' % (rec['dbId'], rec['recordId']))
            if rec.get('imageId') and image_ok(rec, ('record',), listed):
                keys.append('img:%s:%s' % (rec['dbId'], rec['imageId']))
    return keys


def _keys_from_url(url):
    """The item key a captured page's address names (capture.json `url`)."""
    keys = []
    url = url or ''
    m = NP_ARTICLE_RE.search(url)
    if m:
        keys.append('clip:%s' % m.group(1))
    m = FS_RECORD_RE.search(url)
    if m:
        keys.append('fsrec:%s' % m.group(1).upper())
    m = FAG_MEMORIAL_RE.search(url)
    if m:
        # The address alone says the memorial was seen, never that its
        # photos were saved: fagidx:. Its record-data says fag: when they were.
        keys.append('fagidx:%s' % m.group(1))
    m = ANC_RECORD_RE.search(url)
    if m:
        if m.group(1):
            db, rid = m.group(1), m.group(2)
        elif m.group(3):
            db, rid = m.group(4), m.group(3)
        else:
            db, rid = m.group(5), m.group(6)
        keys.append('rec:%s:%s' % (db, rid))
    return keys


# ── remembering what each file said ─────────────────────────────────────────
#
# Chrome starts a new helper for every message, so nothing kept in memory
# survives from one visit to the next. The first page of "already looked at"
# marks used to read every source record, inbox stub and site-data file in the
# archive; what each file said is now remembered on disk instead, in
# _KnownFiles, and only a file changed since the last scan is read again.

# Bump when the cache file's own layout changes. Changes to how a file is READ
# need no bump: _reader_fingerprint covers those.
KNOWN_FILES_VERSION = 1
# A file must sit unchanged this long before what it says is remembered. A
# second save inside the window could land in the same mtime tick with the
# same size and look untouched (FAT/exFAT drives keep mtimes to 2 s); not
# remembering a just-saved file is git's "racily clean" rule, and costs one
# extra read.
KNOWN_FILES_SETTLE_SECONDS = 2.0
# Windows refuses a rename onto a file another program has open for a moment
# (antivirus, the search indexer). These are tools/_lib.py replace_with_retry's
# numbers: this helper is standalone and imports nothing from the archive's
# tools, so the rule is written twice on purpose - change both together.
REPLACE_ATTEMPTS = 10
REPLACE_DELAY_SECONDS = 0.2


@functools.lru_cache(maxsize=1)
def _reader_fingerprint():
    """A stamp of the code that turns a file into item keys, or None.

    The cache holds what each file said *as this code read it*. A new version
    of the helper may read a file differently - a record address it newly
    recognizes, a fixed rule for when a scan counts as saved - and an answer
    the old code gave must not outlive it: it could keep a record "not in
    archive" that the new code finds, or keep a mark the new code would drop.
    So the stamp is this file's own content hash (every rule that reads a file
    lives in it; it imports nothing but the standard library) plus the Python
    version. Any update to the helper throws the cache away once. None (this
    file will not read) turns the cache off for this process.
    """
    h = hashlib.sha256()
    try:
        h.update(Path(__file__).read_bytes())
    except OSError:
        return None
    h.update(('python %d.%d' % sys.version_info[:2]).encode())
    return h.hexdigest()


def _replace_with_retry(src, dst):
    """`os.replace`, retried a few times while Windows says the file is in use.

    A copy of tools/_lib.py replace_with_retry (this helper is standalone):
    only PermissionError, only on Windows, a bounded number of times; the last
    PermissionError reaches the caller unchanged.
    """
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if os.name != 'nt' or attempt == REPLACE_ATTEMPTS:
                raise
            time.sleep(REPLACE_DELAY_SECONDS)


class _KnownFiles:
    """A disposable cache file remembering what each file the scan reads said.

    One entry per file path: `[mtime_ns, size, facts]`. An entry is used only
    while the file on disk still has exactly that size and modification time,
    so an edit, a new capture or a restored backup is read afresh on the next
    scan, and a file that is gone is never walked, so its entry is never
    consulted. Only a clean read is remembered - a file that would not open
    (held for a moment by a sync tool or a backup), would not decode, or held
    broken JSON is read again every time - and only when what it said survives
    a round trip through JSON unchanged.

    What a site-data file means can also depend on OTHER files: whether the
    scan or memorial photo it names is saved beside it. Those answers are
    never trusted from the cache. The entry records each question reading the
    file asked (`asked`) with its answer; every one is asked again against the
    folder as it is now, and one different answer means the file is read
    afresh. So deleting a scan takes its "saved" mark away on the next scan,
    exactly as before.

    Which way is safe: a stale "in archive" is the dangerous mistake for Seen
    It. It is silent - he skips a record he never saved, and the capture
    add-on's batches skip it too - while a wrong "not in archive" shows itself
    as a second capture of something the archive already holds, which its own
    checks catch. The cache is built to make neither: it can only skip a read
    whose result it already holds for that very file, and anything it cannot
    vouch for is read again. Where it must give up (a cache it cannot read or
    trust), it reads everything, as the helper always did.

    It lives with the helper's settings (this computer only, like
    known-cache.json): never in the archive, which the helper only reads, and
    never in the synced data folder.
    """

    def __init__(self, path):
        self.path = Path(path)
        self.reader = _reader_fingerprint()
        self.entries = self._load()
        self.loaded = dict(self.entries)
        self.walked = set()
        self.kept_roots = []
        self.settled_before = time.time() - KNOWN_FILES_SETTLE_SECONDS

    def _load(self):
        """The stored entries, or {} - missing, damaged, another layout or
        written by other reading code all just mean every file is read once more."""
        if self.reader is None:
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
        if (not isinstance(raw, dict) or raw.get('version') != KNOWN_FILES_VERSION
                or raw.get('reader') != self.reader):
            return {}
        files = raw.get('files')
        return files if isinstance(files, dict) else {}

    def facts(self, p, extract, still_true):
        """What file `p` says: from the cache when it is unchanged, else `extract(p)`.

        `extract(p)` returns `(facts, clean)`; it is the scan's own reading of
        one file, and whatever it raises reaches the caller as before.
        `still_true(facts)` vets a remembered value - its shape (so a cache
        damaged by hand costs a read, never a crash or a wrong answer) and, for
        a site-data file, every answer it depends on, asked again now.
        """
        key = str(p)
        self.walked.add(key)
        try:
            st = p.stat()   # stat BEFORE reading: a save landing in between
        except OSError:     # reads as changed next time, never as remembered
            st = None
        entry = self.entries.get(key)
        if (st is not None and isinstance(entry, list) and len(entry) == 3
                and entry[0] == st.st_mtime_ns and entry[1] == st.st_size
                and still_true(entry[2])):
            return entry[2]
        self.entries.pop(key, None)
        facts, clean = extract(p)
        if clean and st is not None and st.st_mtime < self.settled_before:
            try:
                same = json.loads(json.dumps(facts)) == facts
            except (TypeError, ValueError):
                same = False
            if same:
                self.entries[key] = [st.st_mtime_ns, st.st_size, facts]
        return facts

    def keep_under(self, root):
        """Keep what is remembered under a folder that could not be reached this
        time (a drive not plugged in), so plugging it back in is not a full re-read."""
        self.kept_roots.append(os.path.join(str(root), ''))

    def save(self):
        """Write the cache back after a complete scan; a failure is ignored on purpose.

        Entries for files the scan no longer found (deleted or moved) are
        dropped. The cache only buys speed: a read-only settings folder, or
        two helpers racing to replace the file, costs the next scan a re-read,
        never a wrong answer, so there is nothing worth telling the human about.
        """
        kept = tuple(self.kept_roots)
        self.entries = {k: v for k, v in self.entries.items()
                        if k in self.walked or (kept and k.startswith(kept))}
        if self.reader is None or self.entries == self.loaded:
            return
        tmp = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix='.writing-', dir=str(self.path.parent))
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({'version': KNOWN_FILES_VERSION, 'reader': self.reader,
                           'files': self.entries}, f)
            _replace_with_retry(tmp, self.path)
            tmp = None
        except OSError:
            pass
        finally:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def _no_cache(p, extract, still_true):
    """Read the file, as a scan without a cache always has."""
    return extract(p)[0]


def _is_keys(value):
    return isinstance(value, list) and all(isinstance(k, str) for k in value)


def _source_record_facts(p):
    """The keys one source record names, and its S-id, from its own text."""
    try:
        text = p.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None, False
    sm = SOURCE_ID_RE.search(_frontmatter(text) or text)
    keys = [k for url in _frontmatter_urls(text) for k in _keys_from_url(url) if not k.startswith('fag:')]
    keys += ['fagidx:%s' % mid for mid in sorted(set(FAG_MEMORIAL_RE.findall(text)))]
    return {'sid': sm.group(1) if sm else None, 'keys': keys}, True


def _is_source_facts(facts):
    return (isinstance(facts, dict) and set(facts) == {'sid', 'keys'} and _is_keys(facts['keys'])
            and (facts['sid'] is None or isinstance(facts['sid'], str)))


def _stub_facts(p):
    """The keys an inbox stub names: the page addresses in its frontmatter."""
    try:
        raw = p.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return None, False
    return [k for url in _frontmatter_urls(raw) for k in _keys_from_url(url)], True


def _capture_json_facts(p):
    """The keys a bundle's capture.json names: its page address."""
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, False
    return (_keys_from_url(data.get('url')) if isinstance(data, dict) else []), True


def _site_data_facts(p, folders):
    """The keys one site-data file speaks for, plus each question about the
    files beside it that deciding them asked, with its answer (_KnownFiles)."""
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None, False
    asked = []

    def image_saved(roles):
        answer = _holds_saved_image(p, roles, folders)
        asked.append(['image', list(roles), answer])
        return answer

    def file_saved(name):
        answer = _holds_saved_file(p, name, folders)
        asked.append(['file', name, answer])
        return answer

    return {'keys': _keys_from_site_data(data, image_saved, file_saved), 'asked': asked}, True


def _site_data_still_true(p, facts, folders):
    """Is a remembered site-data reading still right: does every question it
    asked get the same answer from the folder as it is now?

    `_keys_from_site_data` decides only from the file's own data and these
    answers, so the same data (same size and mtime) and the same answers, in
    the same order, can only give the same keys.
    """
    if not (isinstance(facts, dict) and set(facts) == {'keys', 'asked'} and _is_keys(facts['keys'])
            and isinstance(facts['asked'], list)):
        return False
    for q in facts['asked']:
        if not (isinstance(q, list) and len(q) == 3 and isinstance(q[2], bool)):
            return False
        kind, arg, answer = q
        if kind == 'image' and _is_keys(arg):
            now = _holds_saved_image(p, tuple(arg), folders)
        elif kind == 'file':
            now = _holds_saved_file(p, arg, folders)
        else:
            return False
        if now is not answer:
            return False
    return True


def scan_known(folders, cache=None):
    """{key: {state, sid?, where}} for everything the folders hold.

    'filed' beats 'processed' beats 'captured': an item that is both waiting in
    the inbox and already filed shows as filed. `where` is only the name of the
    folder the file sits in, never a full path. Also returns the folders that
    could not be reached, so the add-on can say so.

    `cache` (a _KnownFiles) remembers what each file said between scans; the
    walk, its order and the answers are the same with it or without it.
    """
    out = {}
    missing = []
    rank = {'captured': 1, 'processed': 2, 'filed': 3}
    beside = _FolderFiles()
    facts = cache.facts if cache is not None else _no_cache

    def put(key, state, where, sid=None):
        old = out.get(key)
        if old and rank[old['state']] >= rank[state]:
            return
        entry = {'state': state, 'where': where}
        if sid:
            entry['sid'] = sid
        out[key] = entry

    def site_data_keys(p):
        found = facts(p, lambda p: _site_data_facts(p, beside),
                      lambda f: _site_data_still_true(p, f, beside))
        return found['keys'] if found is not None else []

    def unreachable(root):
        missing.append(str(root))
        if cache is not None:
            cache.keep_under(root)

    for root in folders.get('filed', []):
        root = _expand(root)
        if not root.is_dir():
            unreachable(root)
            continue
        for p in root.rglob('*site-data*.json'):
            m = SID_RE.search(p.name)
            for k in site_data_keys(p):
                put(k, 'filed', p.parent.name, m.group(1) if m else None)

    # Source records. The page address a capture came from is kept in the
    # record's frontmatter (`external_links:`, written by `fha capture` and
    # `fha process`; sometimes in `citation:` too), so a record filed with the
    # archive's own tools is recognized even with no site-data file beside it.
    # A Find a Grave memorial is the exception: one a record cites or links is
    # fagidx: (its photos are not captured yet), never fag:, so the capture
    # add-on's Find a Grave batch still fetches it.
    for root in folders.get('sources', []):
        root = _expand(root)
        if not root.is_dir():
            unreachable(root)
            continue
        for p in root.rglob('*.md'):
            record = facts(p, _source_record_facts, _is_source_facts)
            if record is None:
                continue
            for k in record['keys']:
                put(k, 'filed', p.parent.name, record['sid'])

    for root in folders.get('required', []):
        if not _expand(root).is_dir():
            unreachable(_expand(root))
    for root in _globbed(folders.get('captured', [])):
        root = Path(root)
        if not root.is_dir():
            continue  # no download folder yet is normal: nothing captured on this computer
        for p in root.rglob('*'):
            name = p.name.lower()
            is_stub = name.endswith('.notes.md') or name == 'notes.md'
            is_data = name.endswith('.json') and (
                name == 'capture.json' or 'site-data' in name
                or name.startswith(('clipping-data', 'record-data')))
            if not (is_stub or is_data) or not p.is_file():
                continue
            # A bundle's data file counts once the bundle is whole: the
            # companion writes capture.json last, and an ingested inbox
            # bundle has its notes.md. A download that stopped part-way has
            # neither, and nothing in it is captured yet.
            if is_data and name != 'capture.json' and not (
                    (p.parent / 'capture.json').is_file() or (p.parent / 'notes.md').is_file()):
                continue
            # an item in the inbox's processed/ holding folder has been filed
            # (or set aside as a duplicate). Only folders INSIDE the inbox
            # count: an inbox that itself lives under a folder called
            # "processed" is not all done.
            inside = [x.lower() for x in p.relative_to(root).parts[:-1]]
            state = 'processed' if 'processed' in inside else 'captured'
            if is_stub:
                # `fha capture --ingest` turns a bundle into an inbox stub
                # whose frontmatter carries the page address.
                keys = facts(p, _stub_facts, _is_keys)
            elif name == 'capture.json':
                keys = facts(p, _capture_json_facts, _is_keys)
            else:
                keys = site_data_keys(p)
            for k in keys or []:
                put(k, state, p.parent.name)
    if cache is not None:
        cache.save()
    return out, missing


URL_RE = re.compile(r'https?://[^\s\'"<>\])]+', re.I)


def _frontmatter(text):
    """The YAML block between the first two `---` lines, or '' when there is none."""
    lines = text.lstrip('﻿').splitlines()
    if not lines or lines[0].strip() != '---':
        return ''
    for i, line in enumerate(lines[1:], 1):
        if line.strip() in ('---', '...'):
            return '\n'.join(lines[1:i])
    return ''


def _frontmatter_urls(text):
    """Every web address in a record's frontmatter (external_links, citation, …).

    Only the frontmatter: it describes THIS source. The notes below it may
    mention other records ("see also her sister's census entry"), which would
    otherwise be marked as filed when they are not.
    """
    return URL_RE.findall(_frontmatter(text))


def known_items(offset, settings, data_dir, cache_path=None, files_cache_path=None):
    """The scan for page 0; the later pages of the same answer reuse it.

    Chrome starts a new helper for every message, so without a cache each page
    would re-scan the whole archive (seconds warm, minutes on a cold disk) and
    the add-on could be stopped before the last page came back. The cache sits
    with the settings (this computer only), not in the synced data folder.

    Page 0 scans afresh - the first page of every visit - but through
    _KnownFiles (known-files.json, beside it), so only files changed since the
    last scan are read again.
    """
    cache_path = Path(cache_path) if cache_path else config_dir() / 'known-cache.json'
    files_cache_path = Path(files_cache_path) if files_cache_path else config_dir() / 'known-files.json'
    if offset:
        try:
            if cache_path.stat().st_mtime > time.time() - KNOWN_CACHE_SECONDS:
                cached = json.loads(cache_path.read_text(encoding='utf-8'))
                return cached['items'], cached.get('missing', [])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    items, missing = scan_known(scan_folders(settings, data_dir), _KnownFiles(files_cache_path))
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.writing-', dir=str(cache_path.parent))
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'items': items, 'missing': missing}, f)
        os.replace(tmp, cache_path)
    except OSError:
        pass
    return items, missing


# ── the message loop ────────────────────────────────────────────────────────

def read_message(stream=None):
    stream = stream or sys.stdin.buffer
    raw = stream.read(4)
    if len(raw) < 4:
        return None
    (length,) = struct.unpack('<I', raw)
    return json.loads(stream.read(length).decode('utf-8'))


def send(obj, stream=None):
    stream = stream or sys.stdout.buffer
    data = json.dumps(obj, ensure_ascii=False).encode('utf-8')
    stream.write(struct.pack('<I', len(data)))
    stream.write(data)
    stream.flush()


def safe_path(data_dir, name):
    """A data file's path; only seen-it-<id>.json names, only in the data folder."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError('not a Seen It data file name: %r' % (name,))
    return Path(data_dir) / name


def describe(settings):
    """What the Seen It window shows about the helper's settings."""
    archive = settings.get('archive') or ''
    out = {
        'ok': True,
        'configFile': str(config_path()),
        'archive': archive,
        'archiveProblem': archive_problem(archive) if archive else None,
        'dataDirDefault': str(default_data_dir()),
        'dataDirSet': bool(settings.get('data_dir')),
    }
    try:
        out['dataDir'] = str(resolve_data_dir(settings))
        out['dataDirProblem'] = None
    except SettingsError as e:
        out['dataDir'] = str(settings.get('data_dir') or '')
        out['dataDirProblem'] = str(e)
    return out


def clean_typed_path(text, posix=None):
    """A folder as a person pasted or dragged it, turned back into the folder.

    Each place a path can come from wraps it differently:
      Windows "Copy as path"      "C:\\Users\\you\\Hartley Family Archive"
      GNOME Terminal drag-drop    '/home/you/Hartley Family Archive'   (+ a space)
      macOS Terminal drag-drop    /Users/you/Mom\\'s\\ Archive          (+ a space)
    On macOS and Linux the wrapping is shell quoting, so it is undone with the
    shell's own rules (shlex, which only parses - it never runs anything). A
    path typed plainly, spaces and all, is not shell-quoted; when the quoting
    rules do not give back exactly one folder, the text is used as typed.
    On Windows a backslash is part of the path, so only the quotes go - all
    of them, since no Windows name can hold a `"`, and a folder ending in `\\`
    handed over as "C:\\Users\\you\\Archive\\" reaches Python as
    C:\\Users\\you\\Archive" (the `\\"` reads as an escaped quote).
    """
    posix = (os.name != 'nt') if posix is None else posix
    t = str(text or '').strip()
    if not posix:
        return t.replace('"', '').strip()
    try:
        parts = shlex.split(t)
    except ValueError:  # an unmatched quote: an apostrophe in a typed name
        parts = []
    if len(parts) == 1:
        return parts[0]
    if len(t) >= 2 and t[0] == t[-1] and t[0] in '"\'':
        t = t[1:-1].strip()
    return t


def _set_config(msg, settings):
    """Change the archive and/or data folder from the Seen It window.

    Each value is checked before anything is saved; an empty value clears the
    setting (the data folder goes back to the per-user default).
    """
    new = dict(settings)
    if 'archive' in msg:
        archive = clean_typed_path(msg.get('archive'))
        if archive:
            archive = str(_expand(archive).resolve())  # a path typed in a terminal: from there
            problem = archive_problem(archive)
            if problem:
                raise SettingsError(problem)
            new['archive'] = archive
        else:
            new.pop('archive', None)
    if 'dataDir' in msg:
        data = clean_typed_path(msg.get('dataDir'))
        if data:
            new['data_dir'] = str(_expand(data).resolve())
        else:
            new.pop('data_dir', None)
    data_dir = resolve_data_dir(new)  # refuses an unsafe folder before saving
    data_dir.mkdir(parents=True, exist_ok=True)
    save_settings(new)
    return describe(new)


def handle(msg, settings=None):
    """Answer one request from the add-on."""
    if settings is None:
        settings = load_settings()
    op = msg.get('op')
    if op == 'config':
        return describe(settings)
    if op == 'setConfig':
        return _set_config(msg, settings)
    data_dir = resolve_data_dir(settings)
    if op == 'known':
        offset = int(msg.get('offset') or 0)
        items, missing = known_items(offset, settings, data_dir)
        # Chrome caps a helper's reply at 1 MB: send it in key chunks
        keys = sorted(items)
        part = keys[offset:offset + KNOWN_PAGE]
        return {'ok': True, 'items': {k: items[k] for k in part}, 'total': len(keys),
                'next': offset + len(part), 'missing': missing}
    if op == 'ping':
        data_dir.mkdir(parents=True, exist_ok=True)
        return {'ok': True, 'dir': str(data_dir), 'archive': settings.get('archive') or ''}
    if op == 'list':
        data_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for p in sorted(data_dir.iterdir()):
            if p.is_file() and NAME_RE.match(p.name):
                st = p.stat()
                files.append({'name': p.name, 'size': st.st_size, 'mtime': int(st.st_mtime * 1000)})
        return {'ok': True, 'dir': str(data_dir), 'files': files}
    if op == 'read':
        text = safe_path(data_dir, msg.get('name')).read_text(encoding='utf-8')
        offset = int(msg.get('offset') or 0)
        part = text[offset:offset + CHUNK_CHARS]
        return {'ok': True, 'text': part, 'total': len(text), 'next': offset + len(part)}
    if op == 'write':
        path = safe_path(data_dir, msg.get('name'))
        data_dir.mkdir(parents=True, exist_ok=True)
        text = msg.get('text')
        if not isinstance(text, str):
            raise ValueError('nothing to write')
        # Write beside the target, then swap it in: a sync tool or a crash never
        # sees a half-written file.
        fd, tmp = tempfile.mkstemp(prefix='.writing-', dir=str(data_dir))
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.replace(tmp, path)
        return {'ok': True, 'path': str(path), 'size': path.stat().st_size}
    raise ValueError('unknown request %r' % (op,))


def main():
    while True:
        msg = read_message()
        if msg is None:
            return
        try:
            send(handle(msg))
        except Exception as e:  # report, never crash: Chrome shows nothing useful otherwise
            send({'ok': False, 'error': str(e)})


# ── installer support ───────────────────────────────────────────────────────
# install.cmd (Windows) and install.sh (macOS / Linux) only find Python and ask
# for the archive folder; everything else happens here, in one place, so it
# can be tested and so a folder name with spaces, quotes or an `&` cannot break
# a file the way an `echo` in a batch file would.

def host_manifest(launcher):
    """The file that tells Chrome where the helper is and who may use it.

    `allowed_origins` names only the Seen It add-on, by the ID its manifest key
    fixes, so no other add-on can start the helper.
    """
    return {
        'name': HOST_NAME,
        'description': 'Seen It helper: reads your family archive and keeps the add-on data folder',
        'path': str(Path(launcher).resolve()),
        'type': 'stdio',
        'allowed_origins': ['chrome-extension://%s/' % EXTENSION_ID],
    }


def write_launcher(folder, python=None, script=None, windows=None):
    """Write the small program Chrome starts, naming Python by its full path.

    Chrome starts helpers with a bare environment (on macOS, almost no PATH),
    so the launcher names the exact Python the installer ran under instead of
    hoping `python` is found. Kept with the settings, not in the add-on folder,
    because it holds this computer's paths.
    """
    python = str(Path(python or sys.executable).resolve())
    script = str(Path(script or __file__).resolve())
    windows = (os.name == 'nt') if windows is None else windows
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if windows:
        path = folder / 'seenit_host.cmd'
        esc = lambda s: s.replace('%', '%%')  # noqa: E731 - cmd.exe expands %
        # cmd.exe reads a batch file in the console's code page, not UTF-8, so
        # a folder named Müller would be misread and "not found".
        # Switching the code page first makes the next line read as written.
        text = '@echo off\r\nchcp 65001 >nul 2>nul\r\n"%s" -u "%s" %%*\r\n' % (esc(python), esc(script))
        path.write_bytes(text.encode('utf-8'))
    else:
        path = folder / 'seenit_host'
        q = lambda s: "'" + s.replace("'", "'\\''") + "'"  # noqa: E731 - sh single quotes
        text = '#!/bin/sh\nexec %s -u %s "$@"\n' % (q(python), q(script))
        path.write_bytes(text.encode('utf-8'))
        path.chmod(0o755)
    return path


def browser_manifest_dirs(home=None, platform=None):
    """Where each Chromium-family browser looks for per-user helper files.

    Chrome always (it is the browser Seen It is made for); Chromium, Edge and
    Brave only when they are installed for this user. On Windows the browsers
    find the file through the registry instead (register_windows).
    """
    home = Path(home or os.path.expanduser('~'))
    platform = platform or sys.platform
    if platform == 'darwin':
        base = home / 'Library' / 'Application Support'
        browsers = ['Google/Chrome', 'Chromium', 'Microsoft Edge', 'BraveSoftware/Brave-Browser']
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config')
        browsers = ['google-chrome', 'chromium', 'microsoft-edge', 'BraveSoftware/Brave-Browser']
    out = []
    for i, b in enumerate(browsers):
        d = base / b
        if i == 0 or d.is_dir():
            out.append(d / 'NativeMessagingHosts')
    return out


def register_windows(manifest_path):
    """Point Chrome (and Edge, if present) at the helper file: current user only."""
    import winreg
    keys = [r'Software\Google\Chrome\NativeMessagingHosts\%s' % HOST_NAME]
    if (Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'Edge').is_dir():
        keys.append(r'Software\Microsoft\Edge\NativeMessagingHosts\%s' % HOST_NAME)
    for key in keys:
        winreg.SetValue(winreg.HKEY_CURRENT_USER, key, winreg.REG_SZ, str(manifest_path))
    return keys


def setup(archive=None, data_dir=None, out=None):
    """Record the archive and data folder in the settings file, then report.

    Keeps whatever is already set unless a new value is given, so running the
    installer again (after moving a folder) never forgets the other setting.
    Returns a process exit code: 0 done, 2 a value that cannot be used.
    """
    out = out or sys.stdout
    try:
        settings = load_settings()
        msg = {}
        # A blank answer (Enter, or only spaces) keeps what is set: the
        # installer never clears a setting; the Seen It window can.
        if archive is not None and clean_typed_path(archive):
            msg['archive'] = archive
        if data_dir is not None and clean_typed_path(data_dir):
            msg['dataDir'] = data_dir
        info = _set_config(msg, settings)
    except SettingsError as e:
        out.write('Could not save the Seen It settings: %s.\n' % e)
        return 2
    out.write('Seen It settings saved in %s\n' % info['configFile'])
    out.write('  Data folder:    %s\n' % info['dataDir'])
    if info['archive']:
        out.write('  Family archive: %s\n' % info['archive'])
    else:
        out.write('  Family archive: not set yet (Seen It still works; set it later in the\n'
                  '                  Seen It window under Advanced mode)\n')
    return 0


def install(archive=None, data_dir=None, out=None):
    """The whole installer: settings first, then the launcher and Chrome's file.

    Settings come first because they are the step that can refuse (an archive
    folder with no fha.yaml, a data folder inside a git checkout); nothing is
    registered with the browser until they are good.
    """
    out = out or sys.stdout
    code = setup(archive, data_dir, out)
    if code:
        return code
    launcher = write_launcher(config_dir())
    manifest = host_manifest(launcher)
    text = json.dumps(manifest, indent=2) + '\n'
    if os.name == 'nt':
        target = config_dir() / ('%s.json' % HOST_NAME)
        target.write_bytes(text.encode('utf-8'))
        try:
            register_windows(target)
        except OSError as e:
            out.write('Could not register the helper with Chrome (%s). Run install.cmd again; '
                      'if it keeps failing, ask for help with "reg add HKCU".\n' % e)
            return 3
        out.write('Registered with Chrome for this Windows user only.\n')
    else:
        for d in browser_manifest_dirs():
            d.mkdir(parents=True, exist_ok=True)
            (d / ('%s.json' % HOST_NAME)).write_bytes(text.encode('utf-8'))
            out.write('Registered with the browser: %s\n' % d)
    out.write('Now open chrome://extensions and press the reload arrow on Seen It, then turn on\n'
              'Advanced mode at the bottom of the Seen It window.\n')
    return 0


def cli(argv=None):
    parser = argparse.ArgumentParser(
        description='The Seen It helper. Chrome runs it with no options; the installers use these.')
    parser.add_argument('--install', action='store_true',
                        help='save the settings and register the helper with the browser')
    parser.add_argument('--setup', action='store_true',
                        help='only save the settings (archive, data folder) and report them')
    parser.add_argument('--archive', help="the family archive's top folder (the one holding fha.yaml)")
    parser.add_argument('--data-dir', help='where to keep the seen-it-<device>.json data files')
    args = parser.parse_args(argv)
    if args.install:
        return install(args.archive, args.data_dir)
    if args.setup:
        return setup(args.archive, args.data_dir)
    parser.print_help()
    return 0


if __name__ == '__main__':
    # Chrome starts the helper with the caller's origin as its first argument
    # (chrome-extension://…/), and on Windows a --parent-window handle after it,
    # so an option FIRST means a person (or an installer) is running it.
    if len(sys.argv) > 1 and sys.argv[1].startswith('-') and not sys.argv[1].startswith('--parent-window'):
        sys.exit(cli())
    main()
