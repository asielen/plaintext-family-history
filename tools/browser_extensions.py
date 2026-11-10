#!/usr/bin/env python3
"""
browser_extensions.py - keep the owner's installed browser add-ons current
from the tools, the way `fha update-tools` keeps the archive's own tools
current (TOOLING §13c, "Browser add-ons").

WHY THIS EXISTS
---------------
The project repo is the source of the Chrome add-ons it ships. A genealogist
loads one with chrome://extensions -> "Load unpacked", from a folder he keeps.
When the repo improves, that folder has to be refreshed IN PLACE and the add-on
reloaded - never removed and loaded again, because Chrome keeps an add-on's
data by its ID, and removing it deletes the data (for Seen It: every page and
record he ever looked at). Doing that by hand means copying the right files,
leaving the rest, and keeping a backup. This file does it.

THE ONE SETTING
---------------
    browser_extensions: extensions                         # inside the archive
    browser_extensions: D:/Genealogy/Tools/Chrome Extensions   # or anywhere
    browser_extensions: none                               # "no thanks"

A folder that holds one subfolder per add-on. Relative values are joined to
the archive root, like `roots:`. When the line is absent, `fha install` and
`fha update-tools` ask once (only when a person is at the keyboard) and write
the answer - including a "no" - into fha.yaml, so the question never comes
back. A value that names an add-on's OWN folder (the one picked in Load
unpacked) is read as its parent: nothing is ever copied inside an add-on.

WHAT A RUN DOES, PER ADD-ON
---------------------------
  1. Find the installed copy by the `key` in its manifest.json (the key fixes
     the add-on's ID, so a folder renamed by hand is still found).
  2. Not there yet: copy it in as `<folder>/<repo folder name>` (built in a
     marked `.part` folder and renamed) and print the Load-unpacked steps.
  3. There, and already the same files: say so and touch nothing.
  4. There, but NEWER than the tools' copy: leave it alone (never downgrade -
     a newer add-on may have already changed its saved data).
  5. Otherwise: copy the whole installed folder to
     `<folder>/_backups/<name>-<old version>-<date>` (never over an existing
     backup, and not again when an identical one is already there), then
     write the tools' files over it in place. If any file cannot be written,
     every file already written is put back, so the add-on is exactly as it
     was. Files the tools do not ship are kept; sibling folders (Seen It's
     `-data` folder) are never opened.
  Then print the one step left: click the reload arrow, do not remove it.

What "the tools' files" are: the files git tracks in the clone's add-on folder
(so a data file or helper setting lying in a developer's clone never ships),
minus its tests and package files.

Only add-ons with a manifest `key` are handled here. The capture add-on has no
key - its ID comes from the folder Chrome loaded it from - and it lives in the
archive at `.fha/browser-companion/`, which update-tools already keeps current;
`capture_reload_note` reminds the owner to reload it when that happens.

CODE MAP
--------
  Setting
    Setting / read_setting   - fha.yaml value -> state, folder, message
    is_extension_manifest    - is this manifest.json a browser add-on's?
    extension_id             - the Chrome ID a manifest `key` fixes
    doctor_check             - the same reading (and refusals), for `fha doctor`
    save_setting             - append the one line to fha.yaml, surgically
    ask_for_setting          - the once-only question at a terminal
  Repo side
    shipped_files            - the files of one add-on that are copied
    read_extension_manifest  - manifest.json of a folder, or None
  Installed side
    find_installed           - folders under the setting with the add-on's ID
    _locate / _refusal_message - where one add-on is; what blocks it (shared
                               by the plan and doctor)
    plan_extensions          - decide what to do for every add-on
    apply_plan               - do it (back up, copy, put back on failure)
  Entry point
    run_extensions           - setting + plan + apply/preview + report
    capture_reload_note      - reload reminder for .fha/browser-companion/
"""

from __future__ import annotations

import base64
import binascii
import datetime
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    ASSET_ROOT_ALIASES,
    FhaConfigError,
    load_fha_yaml,
    pip_command,
    resolve_path,
    yaml_available,
)

SETTING = 'browser_extensions'
DEFAULT_FOLDER = 'extensions'
BACKUPS_DIR = '_backups'

# The add-ons this file keeps current: (repo folder, short name for the
# prompts, what it is for, its helper subfolder or None, its Chrome ID).
# Short names because Chrome's own title for Seen It runs to four site names.
# The ID is what the manifest `key` fixes (seen-it/README.md, "Why the manifest
# has a key"); it is written here so `fha doctor`, which runs inside an archive
# with no tools clone at hand, can recognise the installed copy too. A test
# checks it against the key in the repo's manifest.
EXTENSIONS: tuple[tuple[str, str, str, str | None, str], ...] = (
    ('seen-it', 'Seen It',
     'marks the newspaper pages and records you have already looked at', 'host',
     'cennkaddolfbhiabacldomegncpdphed'),
)

# The capture add-on, named in the question so the owner knows it exists and
# that there is nothing to choose for it.
_CAPTURE_LINE = ('The capture add-on - saves the record you are looking at into '
                 'your inbox. It already lives inside your archive '
                 '(.fha/browser-companion) and is kept up to date there.')

# What stays home: the add-on's own tests and test tooling, git furniture, and
# bytecode. Matched by name at any depth.
_DEV_DIRS = frozenset({'tests', 'test-bundle', 'node_modules', '__pycache__',
                       '.pytest_cache'})
_DEV_FILES = frozenset({'package.json', 'package-lock.json', '.gitignore',
                        '.gitattributes'})
_DEV_SUFFIXES = ('.pyc', '.pyo')

# Written first into a fresh copy's `.part` folder and removed just before it
# is renamed into place. A `.part` holding it is this tool's own unfinished
# copy, safe to clear; a `.part` without it is somebody's, and is left alone.
_PART_MARKER = '.fha-unfinished-copy'

# Top-level archive folders the tools own or read as records. An add-on folder
# inside one of them would be read as records, scanned as photos, or swept up
# by an update - so the setting may not point there.
_RESERVED_ARCHIVE_FOLDERS = frozenset({
    'sources', 'people', 'places', 'notes', 'inbox', 'documents', 'photos',
    'generated', 'docs', 'out', '.cache', '.fha', '.git', '.claude',
    '.plaintext-backup',
})

_DECLINED = frozenset({'none', 'no', 'off', 'false'})

# Characters a folder name cannot hold. `:` everywhere, except right after a
# leading drive letter (`D:/…`) - a sentence like "Yes, D:\Chrome Extensions"
# saved whole would otherwise fail every later run with WinError 123. The rest
# only on Windows, where they are forbidden and the archive most often lives.
_BAD_NAME_CHARS = '<>"|?*' if os.name == 'nt' else ''

# The first question's answers. A person types what he would say out loud, so
# "ok" and "sure" are yes; anything not clearly yes or no is asked again
# rather than saved as a permanent "no".
_YES_WORDS = frozenset({'y', 'yes', 'ok', 'okay', 'sure', 'yeah', 'yep'})
_NO_WORDS = frozenset({'n', 'no', 'nope'})
_ATTEMPTS = 3


# ── Setting ────────────────────────────────────────────────────────────────────

class Setting(NamedTuple):
    """What `browser_extensions:` says, read once and shared by every caller.

    state:   'absent' (no line: ask or hint), 'declined' (`none`),
             'folder' (usable; `folder` is the resolved path), or
             'bad' (present but unusable; `message` says why and what to write)
    message: for 'bad', the problem; for 'folder', a note when the value named
             an add-on's own folder and its parent is used instead ('' if not)
    addon:   that add-on's name, when the value named its folder ('' if not)
    """
    state: str
    folder: Path | None = None
    message: str = ''
    addon: str = ''


def _inside(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or below it, comparing as the OS does.

    normcase folds capitals on Windows, where `Extensions` and `extensions`
    are one folder; elsewhere it is a no-op.
    """
    c = os.path.normcase(str(child))
    p = os.path.normcase(str(parent))
    return c == p or c.startswith(p.rstrip('\\/') + os.sep)


_EXTENSION_PARTS = ('background', 'content_scripts', 'action', 'browser_action',
                    'permissions')


def is_extension_manifest(manifest) -> bool:
    """Whether a parsed manifest.json is a browser add-on's, not some other file's.

    Other folders carry a manifest.json too - the tools clone's own packing
    list has a `manifest_version` (the string "1") - so the name alone proves
    nothing. A browser add-on's has `manifest_version` 2 or 3 as a number, a
    `name`, and at least one of the parts an add-on is made of.
    """
    if not isinstance(manifest, dict):
        return False
    version = manifest.get('manifest_version')
    return (type(version) is int and version in (2, 3)
            and isinstance(manifest.get('name'), str) and bool(manifest['name'].strip())
            and any(part in manifest for part in _EXTENSION_PARTS))


def _addon_name(folder: Path) -> str:
    """The add-on whose own folder this is, or '' when it is not one.

    Seen It's title runs to four site names; the part before the colon is the
    name a person knows it by.
    """
    manifest = read_extension_manifest(folder)
    if not is_extension_manifest(manifest):
        return ''
    return manifest['name'].split(':')[0].strip() or manifest['name'].strip()


def extension_id(key) -> str | None:
    """The Chrome ID a manifest `key` fixes, or None for a missing or broken key.

    Chrome's own rule: the SHA-256 of the decoded public key, first 32 hex
    digits, each written as a letter a-p.
    """
    if not isinstance(key, str) or not key.strip():
        return None
    try:
        der = base64.b64decode(key.strip(), validate=True)
    except (binascii.Error, ValueError):
        return None
    digest = hashlib.sha256(der).hexdigest()[:32]
    return ''.join(chr(ord('a') + int(c, 16)) for c in digest)


def _example() -> str:
    return (f'Write one folder, e.g. `{SETTING}: {DEFAULT_FOLDER}` (inside the '
            f'archive) or `{SETTING}: D:/Chrome Extensions`, or `{SETTING}: none` '
            f'if you do not want the add-ons.')


def _folder_problem(folder: Path, fha_config: dict, archive: Path) -> str:
    """Why `folder` cannot hold the add-ons, or '' when it can."""
    anchor = folder.anchor
    if anchor and not Path(anchor).exists():
        return (f'{SETTING}: in fha.yaml points at {folder}, on a drive that is not '
                f'connected. Connect the drive, or change the line, then run '
                f'`fha update-tools` again.')
    if _inside(archive, folder):
        return (f'{SETTING}: in fha.yaml points at {folder}, which holds the whole '
                f'archive - the add-ons need a folder of their own. {_example()}')
    if _inside(folder, archive):
        first = folder.relative_to(archive).parts[0].lower()
        if first in _RESERVED_ARCHIVE_FOLDERS:
            return (f'{SETTING}: in fha.yaml points inside the archive\'s {first}/ '
                    f'folder, where the tools would read the add-on as records. '
                    f'{_example()}')
    for alias in ASSET_ROOT_ALIASES:
        try:
            root = resolve_path(alias, fha_config, archive).resolve()
        except (OSError, TypeError, ValueError):
            continue
        if _inside(folder, root):
            return (f'{SETTING}: in fha.yaml points inside your {alias} folder '
                    f'({root}), where the add-on\'s files would be filed as '
                    f'{alias}. {_example()}')
    if folder.exists() and not folder.is_dir():
        return (f'{SETTING}: in fha.yaml points at {folder}, which is a file, not a '
                f'folder. Point it at a folder, then run `fha update-tools` again.')
    return ''


def read_setting(fha_config: dict, archive_root: Path) -> Setting:
    """Read `browser_extensions:` from fha.yaml (see `Setting` for the states).

    A blank or wrong-shaped value is 'bad', not 'absent': the line is there, so
    asking again would add a second one. A folder inside the archive is fine
    (the default is `extensions`), but not the archive itself, not inside a
    folder the tools own or read as records, not inside a photos, documents
    or inbox folder wherever those live (an add-on's icons there would be
    catalogued as photos), and not on a drive that is not connected.

    A value naming an add-on's own folder - what a person picked in Load
    unpacked, so the natural thing to paste - is read as its parent, with a
    note: copying an add-on inside another add-on's folder would leave the
    real one never updated again.
    """
    if SETTING not in fha_config:
        return Setting('absent')
    raw = fha_config.get(SETTING)
    if raw is False or (isinstance(raw, str) and raw.strip().lower() in _DECLINED):
        return Setting('declined')
    if not isinstance(raw, str) or not raw.strip():
        return Setting('bad', None, f'{SETTING}: in fha.yaml is blank or not a folder. '
                                    f'{_example()}')
    text = raw.strip()
    stray = [c for c in text if c in _BAD_NAME_CHARS]
    if ':' in (text[2:] if re.match(r'^[A-Za-z]:', text) else text) or stray:
        bad = ':' if not stray else stray[0]
        return Setting('bad', None, (
            f'{SETTING}: in fha.yaml reads `{text}`, and a folder name cannot hold '
            f'a `{bad}` - the line should name only the folder. Change it to, for '
            f'example, `{SETTING}: D:/Chrome Extensions`, or `{SETTING}: none`.'))
    archive = Path(archive_root).resolve()
    folder = Path(os.path.expanduser(text))
    folder = (folder if folder.is_absolute() else archive / folder).resolve()
    problem = _folder_problem(folder, fha_config, archive)
    if problem:
        return Setting('bad', folder, problem)
    addon = _addon_name(folder)
    if not addon:
        return Setting('folder', folder)
    parent = folder.parent
    problem = _folder_problem(parent, fha_config, archive)
    if problem:
        return Setting('bad', parent, problem, addon)
    return Setting('folder', parent, (
        f'{SETTING}: in fha.yaml points at {folder}, which is the folder of the '
        f'{addon} add-on itself - the add-ons are kept in its parent folder, {parent}. Change '
        f'the line to `{SETTING}: {parent.as_posix()}` to stop this note.'), addon)


def doctor_check(archive_root: Path, fha_config: dict) -> tuple[str, str, str] | None:
    """`fha doctor`'s reading of the setting: (status, detail, next step), or None.

    The same `read_setting` update-tools uses, so the two can never disagree
    about what is wrong. No line at all when the setting is absent or `none`:
    the add-ons are optional, and update-tools asks about them itself.
    """
    setting = read_setting(fha_config, archive_root)
    where = Path(archive_root) / 'fha.yaml'
    if setting.state in ('absent', 'declined'):
        return None
    if setting.state == 'bad':
        return ('warn', setting.message, f'fix the {SETTING}: line in {where}')
    if setting.message:
        return ('warn', setting.message,
                f'change the line in {where} to `{SETTING}: {setting.folder.as_posix()}`')
    if not setting.folder.exists():
        return ('ok', f'kept in {setting.folder} (not made yet - the next '
                      f'`fha update-tools` creates it)', '')
    # What update-tools would refuse to touch, found the same read-only way.
    refusals = []
    try:
        for folder_name, short, _purpose, _helper, ident in EXTENSIONS:
            found = _locate(setting.folder, folder_name, ident)
            if found['action'] in ('ambiguous', 'blocked'):
                refusals.append(_refusal_message(short, setting.folder, found))
    except OSError as exc:
        return ('warn', f'{setting.folder} could not be read ({exc})',
                'check the folder (is its drive connected?)')
    if refusals:
        return ('warn', ' '.join(refusals), 'do what the message says, then run '
                                            '`fha update-tools`')
    return ('ok', f'kept up to date in {setting.folder} by `fha update-tools`', '')


def save_setting(archive_root: Path, value: str) -> str | None:
    """Append `browser_extensions: <value>` to fha.yaml. Returns a problem or None.

    Surgical by construction: the existing text is kept byte for byte and two
    comment lines plus the setting go on the end, so the owner's comments and
    layout survive. The new text is parsed before it is written and must read
    back the intended value; the write goes through a temp file and a rename,
    so an interrupted run leaves the old fha.yaml, never half of a new one.
    """
    import yaml  # only reached when yaml_available() said yes
    path = Path(archive_root) / 'fha.yaml'
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return f'could not read {path} ({exc})'
    text = raw.decode('utf-8-sig')
    newline = '\r\n' if '\r\n' in text else '\n'
    rendered = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True,
                              width=10 ** 9).strip()
    if rendered.endswith('...'):
        rendered = rendered[:-3].strip()
    block = [
        '',
        '# Browser add-ons (Seen It): the folder `fha update-tools` keeps them',
        '# up to date in. `none` = not wanted. Change this line any time.',
        f'{SETTING}: {rendered}',
        '',
    ]
    if text and not text.endswith(('\n', '\r')):
        text += newline
    new_text = text + newline.join(block)
    try:
        parsed = yaml.safe_load(new_text)
    except yaml.YAMLError:
        parsed = None
    if not isinstance(parsed, dict) or parsed.get(SETTING) != yaml.safe_load(rendered):
        return (f'{path} could not take the new line safely, so it was left '
                f'unchanged')
    prefix = raw[:3] if raw.startswith(b'\xef\xbb\xbf') else b''
    tmp = path.with_name(path.name + '.fha-tmp')
    try:
        tmp.write_bytes(prefix + new_text.encode('utf-8'))
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return f'could not write {path} ({exc})'
    return None


def _hint_line() -> str:
    """The one line a run that cannot (or did not get to) ask prints instead."""
    return (f'Browser add-ons: not set up. To have Seen It kept up to date, add '
            f'`{SETTING}: {DEFAULT_FOLDER}` to fha.yaml (or name another folder); '
            f'add `{SETTING}: none` to stop this note.')


def _is_absolute_path(text: str) -> bool:
    """A folder someone pasted: a drive and separator, a network share, / or ~.

    Only these count at the yes/no question - "n/a" or "y/n" has a slash in
    it and means no (or nothing), never a folder named `n/a` in the archive.
    """
    return (re.match(r'^[A-Za-z]:[\\/]', text) is not None
            or text.startswith(('\\\\', '//', '/', '~')))


def _first_answer(text: str) -> tuple[str, str]:
    """Classify the yes/no answer -> ('yes'|'no'|'path'|'unclear', path).

    Enter alone is the default, No. A pasted folder is a yes that already
    says where. Only the leading word counts, so "Yes please" is a yes and
    "no thanks" a no.
    """
    cleaned = text.strip().strip('"\'').strip()
    if not cleaned:
        return 'no', ''
    if _is_absolute_path(cleaned):
        return 'path', cleaned
    # "Yes, D:\Genealogy\Chrome Extensions": a yes that already says where.
    said = re.match(r'^(\w+)[\s,.;:!-]+(.+)$', cleaned)
    if said and said.group(1).lower() in _YES_WORDS:
        rest = said.group(2).strip().strip('"\'').strip()
        if _is_absolute_path(rest):
            return 'path', rest
    if '/' in cleaned or '\\' in cleaned:
        return 'unclear', ''
    word = cleaned.split()[0].lower().strip('.,!')
    if word in _YES_WORDS:
        return 'yes', ''
    if word in _NO_WORDS:
        return 'no', ''
    return 'unclear', ''


def _gave_up() -> None:
    """Ctrl-C, closed input or no clear answer: nothing saved, and say what to add."""
    print()
    print(_hint_line())


def _ask_folder(archive_root: Path, fha_config: dict, ask, first: str) -> str | None:
    """The 'which folder?' half. Returns the value to save, or None (nothing saved)."""
    default = Path(archive_root).resolve() / DEFAULT_FOLDER
    candidates_tried = 0
    pending = first
    while candidates_tried < _ATTEMPTS:
        if pending:
            typed, pending = pending, ''
        else:
            try:
                typed = ask(f'Which folder? Press Enter for {default}, or type '
                            f'(or paste) a folder: ')
            except (EOFError, KeyboardInterrupt):
                _gave_up()
                return None
        candidates_tried += 1
        # File Explorer's "Copy as path" wraps the path in quotes.
        typed = typed.strip().strip('"').strip("'").strip()
        candidate = typed or DEFAULT_FOLDER
        setting = read_setting({**fha_config, SETTING: candidate}, archive_root)
        if setting.state == 'folder':
            if setting.addon:
                print(f'That is the {setting.addon} folder itself; the add-ons will '
                      f'be kept in the folder it sits in, {setting.folder}.')
                return setting.folder.as_posix()
            return candidate
        print(setting.message if setting.state == 'bad' else
              'That reads as "no" - type a folder, or press Enter for the default.')
    print('No folder was saved.')
    _gave_up()
    return None


def ask_for_setting(archive_root: Path, fha_config: dict, ask) -> str | None:
    """Ask once whether he wants the add-ons, and where. Returns the value saved.

    `ask` is `input` at a terminal and a stub in tests. Enter, n, no or nope
    on the first question means No (the add-ons are optional) and is saved as
    `none`, so the question never comes back; y/yes/ok/sure/yeah/yep means
    yes; a pasted folder means yes, there; anything else is asked again, and
    after three tries nothing is saved. Ctrl-C or a closed input saves
    nothing either. Every way of saving nothing ends with the hint line, so
    he still learns which line to add.
    """
    print()
    print('Browser add-ons (optional). These tools come with:')
    for _folder, short, purpose, _helper, _id in EXTENSIONS:
        print(f'  - {short} - {purpose}.')
    print(f'  - {_CAPTURE_LINE}')
    kind, path = 'unclear', ''
    for _attempt in range(_ATTEMPTS):
        try:
            answer = ask('Keep Seen It up to date in a folder on this computer? [y/N] ')
        except (EOFError, KeyboardInterrupt):
            _gave_up()
            return None
        kind, path = _first_answer(answer)
        if kind != 'unclear':
            break
        print('Please answer y (yes) or n (no) - or paste the folder to keep them in.')
    if kind == 'unclear':
        _gave_up()
        return None
    if kind == 'no':
        value = 'none'
    else:
        value = _ask_folder(archive_root, fha_config, ask, path)
        if value is None:
            return None
    problem = save_setting(archive_root, value)
    if problem:
        print(f'Could not save your answer: {problem}. Add the line '
              f'`{SETTING}: {value}` to fha.yaml yourself.')
        return None
    if value == 'none':
        print(f'Saved `{SETTING}: none` in fha.yaml - you will not be asked again. '
              f'To change your mind, put a folder on that line instead.')
    else:
        print(f'Saved `{SETTING}: {value}` in fha.yaml. To move the add-ons '
              f'later, change that line.')
    return value


# ── Repo side ──────────────────────────────────────────────────────────────────

def read_extension_manifest(folder: Path) -> dict | None:
    """An add-on folder's manifest.json as a dict, or None if it has none we can read."""
    try:
        data = json.loads((folder / 'manifest.json').read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _git_tracked(repo_root: Path, folder_name: str) -> list[str] | None:
    """Files git tracks under `folder_name` in the clone, relative to it; None if unknown.

    None (not []) whenever git cannot answer - not installed, not a git
    checkout (an unzipped download), or a folder git knows nothing about (a
    download unzipped inside some other repository) - so the caller falls back
    to walking the folder.
    """
    try:
        proc = subprocess.run(
            ['git', '-C', str(repo_root), 'ls-files', '-z', '--', folder_name],
            capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    prefix = folder_name + '/'
    rels = [p[len(prefix):] for p in proc.stdout.decode('utf-8', 'replace').split('\0')
            if p.startswith(prefix)]
    return rels or None


def _gitignore_patterns(src_dir: Path) -> list[str]:
    """The add-on's own .gitignore lines (comments, blanks and negations dropped)."""
    try:
        text = (src_dir / '.gitignore').read_text(encoding='utf-8')
    except OSError:
        return []
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(('#', '!'))]


def _ignored(rel: str, patterns: list[str]) -> bool:
    """Whether .gitignore-style `patterns` exclude the file at `rel`.

    Enough of git's rules for the patterns an add-on's .gitignore uses:
    `name/` matches a folder of that name at any depth, `*.json` a file name
    at any depth, and a pattern with a slash a path from the add-on folder.
    """
    parts = rel.split('/')
    for pattern in patterns:
        if pattern.endswith('/'):
            if any(fnmatch.fnmatch(d, pattern[:-1]) for d in parts[:-1]):
                return True
        elif '/' in pattern.strip('/'):
            if fnmatch.fnmatch(rel, pattern.lstrip('/')):
                return True
        elif fnmatch.fnmatch(parts[-1], pattern.lstrip('/')):
            return True
    return False


def shipped_files(repo_root: Path, folder_name: str) -> list[tuple[str, Path]]:
    """The (relative path, file) pairs of one add-on that get copied.

    The files git tracks in the clone's add-on folder - so a data file, a
    helper settings file or a cache lying in a developer's clone (exactly
    what the add-on's .gitignore exists to keep out) is never shipped into
    someone's archive. Without git, the folder is walked and its .gitignore
    applied instead. Either way the add-on's own tests, package files and
    bytecode stay home (_DEV_*). Sorted so a preview and the real run list
    files in the same order.
    """
    src_dir = Path(repo_root) / folder_name
    tracked = _git_tracked(Path(repo_root), folder_name)
    if tracked is None:
        patterns = _gitignore_patterns(src_dir)
        tracked = []
        for dirpath, dirnames, filenames in os.walk(src_dir):
            dirnames[:] = [d for d in dirnames if d != '.git']
            for name in filenames:
                rel = (Path(dirpath) / name).relative_to(src_dir).as_posix()
                if not _ignored(rel, patterns):
                    tracked.append(rel)
    out: list[tuple[str, Path]] = []
    for rel in sorted(tracked):
        parts = rel.split('/')
        if (_DEV_DIRS.intersection(parts[:-1]) or parts[-1] in _DEV_FILES
                or parts[-1].endswith(_DEV_SUFFIXES)):
            continue
        p = src_dir / rel
        if p.is_file():
            out.append((rel, p))
    return out


def _version_tuple(text) -> tuple[int, ...] | None:
    """Chrome's version format (1-4 dot-separated integers) as a tuple, or None."""
    try:
        parts = tuple(int(x) for x in str(text).split('.'))
    except ValueError:
        return None
    return parts if 1 <= len(parts) <= 4 else None


def _same_bytes(a: Path, b: Path) -> bool:
    try:
        return b.is_file() and a.stat().st_size == b.stat().st_size \
            and a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _file_map(folder: Path) -> dict[str, Path]:
    return {p.relative_to(folder).as_posix(): p
            for p in folder.rglob('*') if p.is_file()}


def _same_tree(a: Path, b: Path) -> bool:
    """Whether two folders hold the same files with the same bytes."""
    try:
        fa, fb = _file_map(a), _file_map(b)
    except OSError:
        return False
    return fa.keys() == fb.keys() and all(_same_bytes(fa[k], fb[k]) for k in fa)


# ── Installed side ─────────────────────────────────────────────────────────────

def find_installed(ext_dir: Path, ident: str) -> list[Path]:
    """Folders directly under `ext_dir` holding the add-on with Chrome ID `ident`.

    By the ID its manifest `key` fixes, not by name, because that IS the
    add-on's identity to Chrome:
    the owner's Seen It lives in `newspapers-seen-it`, an older name. Only one
    level down - `_backups/` holds older copies with the same key, one level
    deeper, and is skipped by name too.
    """
    found: list[Path] = []
    for child in sorted(ext_dir.iterdir()):
        # `.part` is an unfinished copy, never the one Chrome loads.
        if child.name == BACKUPS_DIR or child.name.endswith('.part') or not child.is_dir():
            continue
        manifest = read_extension_manifest(child)
        if manifest and extension_id(manifest.get('key')) == ident:
            found.append(child)
    return found


def _backup_path(ext_dir: Path, folder: Path, old_version: str,
                 date_str: str) -> tuple[Path, bool]:
    """Where to back `folder` up -> (path, already there).

    `_backups/<folder>-<version>-<date>`, the owner's own naming
    (`newspapers-seen-it-0.10.1-2026-11-10`), numbered on so none is ever
    overwritten. When one of those already holds exactly what the folder
    holds now - the same day's run, retried after a failure put everything
    back - it IS the backup, and no second identical copy is made. A `.part`
    left by an interrupted backup counts as taken and is never reused.
    """
    base = f'{folder.name}-{old_version}-{date_str}'
    candidate = ext_dir / BACKUPS_DIR / base
    n = 2
    while candidate.exists() or candidate.with_name(candidate.name + '.part').exists():
        if candidate.is_dir() and _same_tree(candidate, folder):
            return candidate, True
        candidate = ext_dir / BACKUPS_DIR / f'{base}-{n}'
        n += 1
    return candidate, False


def _locate(ext_dir: Path, folder_name: str, ident: str, src: Path | None = None) -> dict:
    """Where one add-on is in `ext_dir`, read only -> {'action', 'target', ...}.

    'found' (target = the installed copy), 'install' (not there; target is
    where it would go, clear_part our own leftover `.part` if any),
    'ambiguous' (others = every copy), or 'blocked' (blocked_by = the folder
    in the way). Shared by the update plan and `fha doctor`, so doctor reports
    exactly the refusals update-tools would make. `src` is the clone's own
    copy, never counted as installed.
    """
    matches = []
    if ext_dir.is_dir():
        matches = [m for m in find_installed(ext_dir, ident)
                   if src is None or m.resolve() != src.resolve()]
    if len(matches) > 1:
        return {'action': 'ambiguous', 'target': None, 'others': matches}
    if matches:
        return {'action': 'found', 'target': matches[0]}
    target = ext_dir / folder_name
    part = target.with_name(target.name + '.part')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        return {'action': 'blocked', 'target': target, 'blocked_by': target}
    if part.exists():
        if not (part / _PART_MARKER).is_file():
            return {'action': 'blocked', 'target': target, 'blocked_by': part}
        return {'action': 'install', 'target': target, 'clear_part': part}
    return {'action': 'install', 'target': target, 'clear_part': None}


def _refusal_message(name: str, ext_dir: Path, found: dict) -> str:
    """The plain message (and recovery step) for an add-on update-tools will not touch."""
    if found['action'] == 'ambiguous':
        listing = ', '.join(str(p) for p in found['others'])
        return (f'{name} is in more than one folder in {ext_dir} ({listing}) - '
                f'left them all alone, because Chrome uses only one. Move the copy '
                f'Chrome is not using out of {ext_dir} (into {BACKUPS_DIR}, say), '
                f'then run `fha update-tools` again.')
    blocker, target = found['blocked_by'], found['target']
    if blocker != target:
        return (f'{name} was not copied: {blocker} is in the way and was not '
                f'made by these tools. Move or delete that folder, then run '
                f'`fha update-tools` again.')
    return (f'{name} was not copied: {target} already exists and is not '
            f'{name}. Rename that folder, then run `fha update-tools` again.')


def plan_extensions(repo_root: Path, ext_dir: Path, date_str: str) -> list[dict]:
    """Decide, without writing anything, what to do for each add-on.

    One plan serves both the preview and the real run, so `--dry-run` can only
    ever describe what the real run does. Each item has `action`:
      install / update / current / newer / ambiguous / blocked / no-source
    """
    items: list[dict] = []
    for folder_name, short, _purpose, helper, _pinned_id in EXTENSIONS:
        src = Path(repo_root) / folder_name
        repo_manifest = read_extension_manifest(src)
        item = {'extension': folder_name, 'name': short, 'helper': helper,
                'action': 'no-source', 'target': None, 'backup': None,
                'backup_exists': False, 'clear_part': None,
                'old_version': None, 'new_version': None, 'copy': [],
                'helper_changed': False, 'others': [], 'blocked_by': None}
        items.append(item)
        if not repo_manifest or not str(repo_manifest.get('key', '')).strip():
            continue
        item['title'] = str(repo_manifest.get('name') or short)
        item['new_version'] = str(repo_manifest.get('version', ''))
        ident = extension_id(repo_manifest['key'])
        if ident is None:
            continue
        files = shipped_files(repo_root, folder_name)
        found = _locate(ext_dir, folder_name, ident, src)
        if found['action'] != 'found':
            item.update(found)
            if found['action'] == 'install':
                item['copy'] = files
            continue
        target = found['target']
        installed = read_extension_manifest(target) or {}
        old = str(installed.get('version', ''))
        item.update(target=target, old_version=old)
        old_t, new_t = _version_tuple(old), _version_tuple(item['new_version'])
        if old_t and new_t and old_t > new_t:
            item['action'] = 'newer'
            continue
        changed = [(rel, p) for rel, p in files if not _same_bytes(p, target / rel)]
        if not changed:
            item['action'] = 'current'
            continue
        backup, exists = _backup_path(ext_dir, target, old or 'unknown', date_str)
        item.update(action='update', copy=changed, backup=backup, backup_exists=exists,
                    helper_changed=bool(helper) and any(
                        rel.startswith(f'{helper}/') for rel, _p in changed))
    return items


def _copy_file(src: Path, dest: Path) -> None:
    """Copy one file into place through a temp sibling, so a failure leaves the old one."""
    if dest.is_dir() and not dest.is_symlink():
        raise OSError(f'{dest} is a folder where a file belongs')
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + '.fha-tmp')
    if tmp.is_symlink() or tmp.is_file():
        tmp.unlink()
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _install(item: dict, ext_dir: Path) -> str | None:
    """A first copy, built in a marked `.part` folder and renamed into place.

    Returns a failure message, or None. A failed copy removes its own `.part`
    at once; if even that fails, the marker inside lets the next run clear it.
    """
    target = item['target']
    part = target.with_name(target.name + '.part')
    created = False
    try:
        if item['clear_part'] is not None:
            shutil.rmtree(part)
        part.mkdir(parents=True)
        created = True
        (part / _PART_MARKER).write_text(
            'An unfinished copy made by fha update-tools; the next run replaces it.\n',
            encoding='utf-8')
        for rel, src in item['copy']:
            _copy_file(src, part / rel)
        (part / _PART_MARKER).unlink()
        if target.is_dir():
            target.rmdir()      # the empty folder plan_extensions allowed
        part.rename(target)
    except OSError as exc:
        if created:
            shutil.rmtree(part, ignore_errors=True)
        return (f'{item["name"]} was not copied into {ext_dir} ({exc}). Check that '
                f'you can write there, then run `fha update-tools` again.')
    return None


def _update(item: dict) -> str | None:
    """Back up, then copy the new files over the installed add-on - all or nothing.

    Returns a failure message, or None. If any file cannot be written (Chrome
    or a virus scanner holding it, a read-only file), every file already
    written this run is put back from the backup and every file it added is
    removed, so the add-on ends exactly as it was: never a mix of two versions
    that Chrome would load. Only if putting back fails too does the message
    point at the backup to restore by hand.
    """
    target, backup = item['target'], item['backup']
    if not item['backup_exists']:
        part = backup.with_name(backup.name + '.part')
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(target, part, symlinks=True)
            part.rename(backup)
        except OSError as exc:
            return (f'{item["name"]} was not updated: its backup to {backup} could '
                    f'not be made ({exc}), so nothing was changed. Check the disk '
                    f'has room, then run `fha update-tools` again.')
    new_dirs: set[Path] = set()
    for rel, _src in item['copy']:
        d = (target / rel).parent
        while d != target and not d.exists():
            new_dirs.add(d)
            d = d.parent
    written: list[tuple[str, bool]] = []
    for rel, src in item['copy']:
        dest = target / rel
        existed = dest.exists()
        try:
            _copy_file(src, dest)
        except OSError as exc:
            failed, cause = rel, exc
            break
        written.append((rel, existed))
    else:
        return None
    not_restored: list[str] = []
    for rel, existed in reversed(written):
        try:
            if existed:
                _copy_file(backup / rel, target / rel)
            else:
                (target / rel).unlink()
        except OSError as exc:
            not_restored.append(f'{rel} ({exc})')
    for d in sorted(new_dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    if not_restored:
        return (f'{item["name"]} could not be updated ({failed}: {cause}), and '
                f'{len(not_restored)} file(s) could not be put back as they were: '
                f'{"; ".join(not_restored[:3])}. Your copy from before the update is '
                f'in {backup} - close Chrome, copy everything in it back over '
                f'{target}, then run `fha update-tools` again.')
    return (f'{item["name"]} was not updated - nothing changed in {target}. '
            f'{failed} could not be written ({cause}). A copy of it as it is now is '
            f'in {backup}. Close Chrome (or any program using that folder) and check '
            f'the file is not marked read-only, then run `fha update-tools` again.')


def apply_plan(items: list[dict], ext_dir: Path) -> tuple[list[str], list[str]]:
    """Carry out a plan. Returns (changed paths, failures), never raises OSError."""
    changed: list[str] = []
    failures: list[str] = []
    for item in items:
        if item['action'] == 'install':
            problem = _install(item, ext_dir)
            if problem is None:
                changed.append(str(item['target']))
        elif item['action'] == 'update':
            had_backup = item['backup_exists']
            problem = _update(item)
            if not had_backup and (problem is None or item['backup'].is_dir()):
                changed.append(str(item['backup']))
            if problem is None:
                changed.append(str(item['target']))
        else:
            continue
        if problem:
            item['error'] = problem
            failures.append(problem)
    return changed, failures


def _report(items: list[dict], ext_dir: Path, dry_run: bool) -> list[str]:
    """Print one plain paragraph per add-on. Returns the warnings among them."""
    would = '[dry-run] Would ' if dry_run else ''
    warnings: list[str] = []
    reload_line = (('  Afterwards' if dry_run else '  One step left') +
                   ': in chrome://extensions click the reload arrow on '
                   '"{title}" (do not remove it - that would delete its data).')
    for item in items:
        name, action, target = item['name'], item['action'], item['target']
        if action == 'no-source' or item.get('error'):
            continue
        if action == 'current':
            print(f'{name} is up to date (version {item["old_version"]}) in {target}.')
        elif action == 'newer':
            print(f'{name} in {target} is version {item["old_version"]}, newer than the '
                  f'copy these tools carry ({item["new_version"]}) - left as it is.')
        elif action in ('ambiguous', 'blocked'):
            msg = _refusal_message(name, ext_dir, item)
            print(msg)
            warnings.append(msg)
        elif action == 'install':
            if item['clear_part'] is not None:
                print(f'{would}{"replace" if dry_run else "Replaced"} the unfinished '
                      f'copy an earlier run left at {item["clear_part"]}.')
            verb = 'copy' if dry_run else 'Copied'
            print(f'{would}{verb} {name} {item["new_version"]} into {target}.')
            print(f'  To start using it (once): open chrome://extensions, turn on '
                  f'Developer mode (top right), click "Load unpacked" and choose {target}.')
            if item['helper']:
                print(f'  Only if you use {name}\'s Advanced mode: double-click '
                      f'{Path(item["helper"]) / "install.cmd"} in that folder once '
                      f'(Mac or Linux: run `sh install.sh` there).')
        elif action == 'update':
            versions = f'{item["old_version"]} -> {item["new_version"]}'
            kept = ('your current one is already copied in' if item['backup_exists']
                    else 'after copying your current one to')
            if dry_run:
                print(f'{would}update {name} {versions} in {target} '
                      f'({len(item["copy"])} file(s)), {kept} {item["backup"]}.')
            else:
                print(f'Updated {name} {versions} in {target} ({len(item["copy"])} '
                      f'file(s)). Your earlier copy is in {item["backup"]}.')
            if item['helper_changed']:
                print(f'  Its helper ({item["helper"]} folder) changed too. Nothing to '
                      f're-run: the helper is started from this folder, so it is '
                      f'already the new one. (If {name}\'s window ever says it cannot '
                      f'reach the helper, run {Path(item["helper"]) / "install.cmd"} '
                      f'again - it is safe to repeat.)')
            print(reload_line.format(title=item['title']))
    return warnings


# ── Entry point ────────────────────────────────────────────────────────────────

def run_extensions(archive_root: Path, repo_root: Path, *, dry_run: bool = False,
                   ask=None, date_str: str | None = None) -> dict:
    """Keep the owner's add-ons current. Called by install and update-tools.

    `ask` is the prompt function (`input`) when a person is at the keyboard,
    None otherwise - then nothing is asked and an unset setting gets one hint
    line. A dry run never asks either: answering would write fha.yaml.
    Prints nothing at all when the setting is `none`, so that run's output is
    exactly what it was before this feature.
    Returns {'items', 'changed', 'warnings', 'printed'}; every warning has
    already been printed, and the caller folds them into its exit status.
    """
    archive_root = Path(archive_root).resolve()
    out = {'items': [], 'changed': [], 'warnings': [], 'printed': False}

    def say(msg: str, *, warn: bool = False) -> dict:
        if not out['printed']:
            print()
            out['printed'] = True
        print(msg)
        if warn:
            out['warnings'].append(msg)
        return out

    if not yaml_available():
        # A note, not a warning: install treats a missing PyYAML as advisory
        # (it is how a fresh machine bootstraps), and update-tools already
        # reports it as its own warning.
        return say(f'Browser add-ons were not checked: reading fha.yaml needs PyYAML. '
                   f'Install it with `{pip_command("pyyaml")}`, then run '
                   f'`fha update-tools` again.')
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as exc:
        return say(f'Browser add-ons were not checked: fha.yaml could not be read. {exc}',
                   warn=True)
    setting = read_setting(fha_config, archive_root)
    if setting.state == 'absent':
        if ask is None or dry_run:
            return say(_hint_line())
        out['printed'] = True       # the question prints its own blank line
        if ask_for_setting(archive_root, fha_config, ask) is None:
            return out
        fha_config = load_fha_yaml(archive_root, strict=True)
        setting = read_setting(fha_config, archive_root)
    if setting.state == 'declined':
        return out
    if setting.state == 'bad':
        return say(setting.message, warn=True)
    ext_dir = setting.folder
    date_str = date_str or datetime.date.today().isoformat()
    try:
        items = plan_extensions(repo_root, ext_dir, date_str)
    except OSError as exc:
        return say(f'Browser add-ons were not checked: {ext_dir} could not be read '
                   f'({exc}). Check the folder (is its drive connected?), then run '
                   f'`fha update-tools` again.', warn=True)
    out['items'] = items
    say(f'Browser add-ons in {ext_dir}:')
    if setting.message:
        print(setting.message)
    if dry_run:
        if not ext_dir.exists():
            print(f'[dry-run] Would create the folder {ext_dir}.')
    else:
        if any(i['action'] == 'install' for i in items):
            try:
                ext_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return say(f'Could not create {ext_dir} ({exc}). Check the folder in '
                           f'`{SETTING}:` in fha.yaml, then run `fha update-tools` '
                           f'again.', warn=True)
        out['changed'], failures = apply_plan(items, ext_dir)
        for f in failures:
            print(f)
        out['warnings'].extend(failures)
    out['warnings'].extend(_report(items, ext_dir, dry_run))
    return out


def capture_reload_note(archive_paths, repo_root: Path, *, dry_run: bool = False) -> str | None:
    """The reload reminder for the capture add-on, when this run changes it.

    Its files live in the archive (.fha/browser-companion/) and update-tools
    refreshes them with the rest of the tools - but Chrome keeps running the
    old copy until the owner clicks reload, and nothing said so.
    """
    if not any(str(p).startswith('.fha/browser-companion/') for p in archive_paths):
        return None
    manifest = read_extension_manifest(Path(repo_root) / 'browser-companion') or {}
    title = manifest.get('name') or 'Plaintext Family History (Capture)'
    lead = ('[dry-run] The capture add-on would change. Afterwards, in'
            if dry_run else 'The capture add-on was updated. In')
    return (f'{lead} chrome://extensions click the reload arrow on "{title}" '
            f'(do not remove it - that would delete its settings).')
