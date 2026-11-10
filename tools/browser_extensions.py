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
`fha update-tools` ask once (only at a terminal) and write the answer -
including a "no" - into fha.yaml, so the question never comes back.

WHAT A RUN DOES, PER ADD-ON
---------------------------
  1. Find the installed copy by the `key` in its manifest.json (the key fixes
     the add-on's ID, so a folder renamed by hand is still found).
  2. Not there yet: copy it in as `<folder>/<repo folder name>` and print the
     Load-unpacked steps, once.
  3. There, and already the same files: say so and touch nothing.
  4. There, but NEWER than the tools' copy: leave it alone (never downgrade -
     a newer add-on may have already changed its saved data).
  5. Otherwise: copy the whole installed folder to
     `<folder>/_backups/<name>-<old version>-<date>` (never over an existing
     backup), then write the tools' files over it in place. Files the tools do
     not ship (a launcher the helper wrote, old tests) are kept; sibling
     folders (Seen It's `-data` folder) are never opened.
  Then print the one step left: click the reload arrow, do not remove it.

Only add-ons with a manifest `key` are handled here. The capture add-on has no
key - its ID comes from the folder Chrome loaded it from - and it lives in the
archive at `.fha/browser-companion/`, which update-tools already keeps current;
`capture_reload_note` reminds the owner to reload it when that happens.

CODE MAP
--------
  Setting
    read_setting             - fha.yaml value -> (state, folder, problem)
    save_setting             - append the one line to fha.yaml, surgically
    ask_for_setting          - the once-only question at a terminal
  Repo side
    shipped_files            - the files of one add-on that are copied
    read_extension_manifest  - manifest.json of a folder, or None
  Installed side
    find_installed           - folders under the setting whose key matches
    plan_extensions          - decide what to do for every add-on
    apply_plan               - do it (back up, copy)
  Entry point
    run_extensions           - setting + plan + apply/preview + report
    capture_reload_note      - reload reminder for .fha/browser-companion/
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import sys
from pathlib import Path

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
# prompts, what it is for, its helper subfolder or None). Short names because
# Chrome's own title for Seen It runs to four site names.
EXTENSIONS: tuple[tuple[str, str, str, str | None], ...] = (
    ('seen-it', 'Seen It',
     'marks the newspaper pages and records you have already looked at', 'host'),
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

# Top-level archive folders the tools own or read as records. An add-on folder
# inside one of them would be read as records, scanned as photos, or swept up
# by an update - so the setting may not point there.
_RESERVED_ARCHIVE_FOLDERS = frozenset({
    'sources', 'people', 'places', 'notes', 'inbox', 'documents', 'photos',
    'generated', 'docs', 'out', '.cache', '.fha', '.git', '.claude',
    '.plaintext-backup',
})

_DECLINED = frozenset({'none', 'no', 'off', 'false'})


# ── Setting ────────────────────────────────────────────────────────────────────

def _inside(child: Path, parent: Path) -> bool:
    """True when `child` is `parent` or below it, comparing as the OS does.

    normcase folds capitals on Windows, where `Extensions` and `extensions`
    are one folder; elsewhere it is a no-op.
    """
    c = os.path.normcase(str(child))
    p = os.path.normcase(str(parent))
    return c == p or c.startswith(p.rstrip('\\/') + os.sep)


def read_setting(fha_config: dict, archive_root: Path) -> tuple[str, Path | None, str]:
    """Read `browser_extensions:` -> (state, folder, problem).

    state is one of:
      'absent'   - no line at all: ask (at a terminal) or print the hint
      'declined' - `none` (or no/off/false): do nothing, say nothing
      'folder'   - a usable folder; `folder` is its resolved path
      'bad'      - present but unusable; `problem` says why and what to write

    A blank or wrong-shaped value is 'bad', not 'absent': the line is there, so
    asking again would add a second one. A folder inside the archive is fine
    (the default is `extensions`), but not the archive itself, not inside a
    folder the tools own or read as records, and not inside a photos,
    documents or inbox folder wherever those live - an add-on's icons there
    would be catalogued as photos.
    """
    if SETTING not in fha_config:
        return 'absent', None, ''
    raw = fha_config.get(SETTING)
    example = (f'Write one folder, e.g. `{SETTING}: {DEFAULT_FOLDER}` (inside '
               f'the archive) or `{SETTING}: D:/Chrome Extensions`, or '
               f'`{SETTING}: none` if you do not want the add-ons.')
    if raw is False or (isinstance(raw, str) and raw.strip().lower() in _DECLINED):
        return 'declined', None, ''
    if not isinstance(raw, str) or not raw.strip():
        return 'bad', None, f'{SETTING}: in fha.yaml is blank or not a folder. {example}'
    archive = Path(archive_root).resolve()
    text = os.path.expanduser(raw.strip())
    folder = Path(text)
    folder = (folder if folder.is_absolute() else archive / folder).resolve()
    if _inside(archive, folder):
        return 'bad', folder, (
            f'{SETTING}: in fha.yaml points at {folder}, which holds the whole '
            f'archive - the add-ons need a folder of their own. {example}')
    if _inside(folder, archive):
        first = folder.relative_to(archive).parts[0].lower()
        if first in _RESERVED_ARCHIVE_FOLDERS:
            return 'bad', folder, (
                f'{SETTING}: in fha.yaml points inside the archive\'s {first}/ '
                f'folder, where the tools would read the add-on as records. '
                f'{example}')
    for alias in ASSET_ROOT_ALIASES:
        try:
            root = resolve_path(alias, fha_config, archive).resolve()
        except (OSError, TypeError, ValueError):
            continue
        if _inside(folder, root):
            return 'bad', folder, (
                f'{SETTING}: in fha.yaml points inside your {alias} folder '
                f'({root}), where the add-on\'s files would be filed as '
                f'{alias}. {example}')
    return 'folder', folder, ''


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
    """The one line a run that cannot ask prints instead of asking."""
    return (f'Browser add-ons: not set up. To have Seen It kept up to date, add '
            f'`{SETTING}: {DEFAULT_FOLDER}` to fha.yaml (or name another folder); '
            f'add `{SETTING}: none` to stop this note.')


def ask_for_setting(archive_root: Path, fha_config: dict, ask) -> str | None:
    """Ask once whether he wants the add-ons, and where. Returns the value saved.

    `ask` is `input` at a terminal and a stub in tests. Enter on the first
    question means No (the add-ons are optional); Enter on the second takes the
    default folder inside the archive. A "no" is saved too, as `none`, so the
    question never comes back. Ctrl-C or a closed input saves nothing - that is
    not an answer, and the question comes back next time.
    """
    print()
    print('Browser add-ons (optional). These tools come with:')
    for _folder, short, purpose, _helper in EXTENSIONS:
        print(f'  - {short} - {purpose}.')
    print(f'  - {_CAPTURE_LINE}')
    try:
        answer = ask('Keep Seen It up to date in a folder on this computer? [y/N] ')
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if answer.strip().lower() not in ('y', 'yes'):
        value = 'none'
    else:
        default = Path(archive_root).resolve() / DEFAULT_FOLDER
        value = None
        for _attempt in range(3):
            try:
                typed = ask(f'Which folder? Press Enter for {default}, or type '
                            f'(or paste) a folder: ')
            except (EOFError, KeyboardInterrupt):
                print()
                return None
            # File Explorer's "Copy as path" wraps the path in quotes.
            typed = typed.strip().strip('"').strip("'").strip()
            candidate = typed or DEFAULT_FOLDER
            state, _folder, problem = read_setting(
                {**fha_config, SETTING: candidate}, archive_root)
            if state == 'folder':
                value = candidate
                break
            print(problem if state == 'bad' else
                  'That reads as "no" - type a folder, or press Enter for the default.')
        if value is None:
            print('No folder was saved; you will be asked again next time.')
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


def shipped_files(src_dir: Path) -> list[tuple[str, Path]]:
    """The (relative path, file) pairs of one add-on that get copied.

    Everything Chrome loads, the helper, and the README - minus the add-on's
    own test furniture, git files and bytecode (see _DEV_*). Sorted so a
    preview and the real run list files in the same order.
    """
    out: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(src_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in _DEV_DIRS)
        for name in sorted(filenames):
            if name in _DEV_FILES or name.endswith(_DEV_SUFFIXES):
                continue
            p = Path(dirpath) / name
            out.append((p.relative_to(src_dir).as_posix(), p))
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


# ── Installed side ─────────────────────────────────────────────────────────────

def find_installed(ext_dir: Path, key: str) -> list[Path]:
    """Folders directly under `ext_dir` whose manifest carries `key`.

    By key, not by name, because the key IS the add-on's identity to Chrome:
    the owner's Seen It lives in `newspapers-seen-it`, an older name. Only one
    level down - `_backups/` holds older copies with the same key, one level
    deeper, and is skipped by name too.
    """
    found: list[Path] = []
    for child in sorted(ext_dir.iterdir()):
        # `.part` is an interrupted copy of ours, never the one Chrome loads.
        if child.name == BACKUPS_DIR or child.name.endswith('.part') or not child.is_dir():
            continue
        manifest = read_extension_manifest(child)
        if manifest and str(manifest.get('key', '')).strip() == key:
            found.append(child)
    return found


def _backup_path(ext_dir: Path, folder: Path, old_version: str, date_str: str) -> Path:
    """`_backups/<folder>-<version>-<date>`, numbered on so none is overwritten.

    The owner's own naming (`newspapers-seen-it-0.10.1-2026-11-10`). A `.part`
    left by an interrupted run also counts as taken: it is somebody's partial
    copy, and nothing here deletes it.
    """
    base = f'{folder.name}-{old_version}-{date_str}'
    candidate = ext_dir / BACKUPS_DIR / base
    n = 2
    while candidate.exists() or candidate.with_name(candidate.name + '.part').exists():
        candidate = ext_dir / BACKUPS_DIR / f'{base}-{n}'
        n += 1
    return candidate


def plan_extensions(repo_root: Path, ext_dir: Path, date_str: str) -> list[dict]:
    """Decide, without writing anything, what to do for each add-on.

    One plan serves both the preview and the real run, so `--dry-run` can only
    ever describe what the real run does. Each item has `action`:
      install / update / current / newer / ambiguous / blocked / no-source
    """
    items: list[dict] = []
    for folder_name, short, _purpose, helper in EXTENSIONS:
        src = Path(repo_root) / folder_name
        repo_manifest = read_extension_manifest(src)
        item = {'extension': folder_name, 'name': short, 'helper': helper,
                'action': 'no-source', 'target': None, 'backup': None,
                'old_version': None, 'new_version': None, 'copy': [],
                'helper_changed': False, 'others': []}
        items.append(item)
        if not repo_manifest or not str(repo_manifest.get('key', '')).strip():
            continue
        item['title'] = str(repo_manifest.get('name') or short)
        item['new_version'] = str(repo_manifest.get('version', ''))
        files = shipped_files(src)
        key = str(repo_manifest['key']).strip()
        matches = [m for m in (find_installed(ext_dir, key) if ext_dir.is_dir() else [])
                   if m.resolve() != src.resolve()]
        if len(matches) > 1:
            item.update(action='ambiguous', others=matches)
            continue
        if not matches:
            target = ext_dir / folder_name
            item['target'] = target
            if target.exists() and (not target.is_dir() or any(target.iterdir())):
                item['action'] = 'blocked'
                continue
            item.update(action='install', copy=files)
            continue
        target = matches[0]
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
        item.update(action='update', copy=changed,
                    backup=_backup_path(ext_dir, target, old or 'unknown', date_str),
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


def apply_plan(items: list[dict], ext_dir: Path) -> tuple[list[str], list[str]]:
    """Carry out a plan. Returns (changed paths, failures), never raises OSError.

    A fresh copy is built in `<name>.part` and renamed into place, so a failed
    install leaves no half-copied add-on for Chrome to load. An update backs
    the whole installed folder up first (again through `.part`), and does not
    start copying until that backup is complete - no backup, no update.
    """
    changed: list[str] = []
    failures: list[str] = []
    for item in items:
        action, target = item['action'], item['target']
        if action == 'install':
            part = target.with_name(target.name + '.part')
            try:
                if part.exists():
                    raise OSError(f'{part} is in the way (left by an earlier run - '
                                  f'move it out of {ext_dir})')
                for rel, src in item['copy']:
                    _copy_file(src, part / rel)
                if target.is_dir():
                    target.rmdir()      # the empty folder plan_extensions allowed
                part.rename(target)
            except OSError as exc:
                item['error'] = str(exc)
                failures.append(f'{item["name"]} was not copied into {ext_dir} ({exc}). '
                                f'Check that you can write there, then run '
                                f'`fha update-tools` again.')
                continue
            changed.append(str(target))
        elif action == 'update':
            backup = item['backup']
            part = backup.with_name(backup.name + '.part')
            try:
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(target, part, symlinks=True)
                part.rename(backup)
            except OSError as exc:
                item['error'] = str(exc)
                failures.append(f'{item["name"]} was not updated: its backup to {backup} '
                                f'could not be made ({exc}), so nothing was changed. '
                                f'Check the disk has room, then run `fha update-tools` again.')
                continue
            changed.append(str(backup))
            bad: list[str] = []
            for rel, src in item['copy']:
                try:
                    _copy_file(src, target / rel)
                except OSError as exc:
                    bad.append(f'{rel} ({exc})')
            if bad:
                item['error'] = '; '.join(bad)
                failures.append(f'{item["name"]} was only partly updated - {len(bad)} '
                                f'file(s) could not be written: {"; ".join(bad[:3])}. '
                                f'Your earlier copy is safe in {backup}. Close Chrome, '
                                f'then run `fha update-tools` again.')
            changed.append(str(target))
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
        elif action == 'ambiguous':
            listing = ', '.join(str(p) for p in item['others'])
            msg = (f'{name} is in more than one folder in {ext_dir} ({listing}) - '
                   f'left them all alone, because Chrome uses only one. Move the copy '
                   f'Chrome is not using out of {ext_dir} (into {BACKUPS_DIR}, say), '
                   f'then run `fha update-tools` again.')
            print(msg)
            warnings.append(msg)
        elif action == 'blocked':
            msg = (f'{name} was not copied: {target} already exists and is not {name}. '
                   f'Rename that folder, then run `fha update-tools` again.')
            print(msg)
            warnings.append(msg)
        elif action == 'install':
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
            if dry_run:
                print(f'{would}update {name} {versions} in {target} '
                      f'({len(item["copy"])} file(s)), after copying your current '
                      f'one to {item["backup"]}.')
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

    `ask` is the prompt function (`input`) when a person is at the terminal,
    None otherwise - then nothing is asked and an unset setting gets one hint
    line. A dry run never asks either: answering would write fha.yaml.
    Returns {'items', 'changed', 'warnings'}; every warning has already been
    printed, and the caller folds them into its exit status.
    """
    archive_root = Path(archive_root).resolve()
    out = {'items': [], 'changed': [], 'warnings': []}
    print()
    if not yaml_available():
        # A note, not a warning: install treats a missing PyYAML as advisory
        # (it is how a fresh machine bootstraps), and update-tools already
        # reports it as its own warning.
        print(f'Browser add-ons were not checked: reading fha.yaml needs PyYAML. '
              f'Install it with `{pip_command("pyyaml")}`, then run '
              f'`fha update-tools` again.')
        return out
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as exc:
        msg = (f'Browser add-ons were not checked: fha.yaml could not be read. {exc}')
        print(msg)
        out['warnings'].append(msg)
        return out
    state, ext_dir, problem = read_setting(fha_config, archive_root)
    if state == 'absent':
        if ask is None or dry_run:
            print(_hint_line())
            return out
        if ask_for_setting(archive_root, fha_config, ask) is None:
            return out
        fha_config = load_fha_yaml(archive_root, strict=True)
        state, ext_dir, problem = read_setting(fha_config, archive_root)
    if state == 'declined':
        return out
    if state == 'bad':
        print(problem)
        out['warnings'].append(problem)
        return out
    if ext_dir.exists() and not ext_dir.is_dir():
        msg = (f'{SETTING}: in fha.yaml points at {ext_dir}, which is a file, not a '
               f'folder. Point it at a folder, then run `fha update-tools` again.')
        print(msg)
        out['warnings'].append(msg)
        return out
    date_str = date_str or datetime.date.today().isoformat()
    try:
        items = plan_extensions(repo_root, ext_dir, date_str)
    except OSError as exc:
        msg = (f'Browser add-ons were not checked: {ext_dir} could not be read ({exc}). '
               f'Check the folder (is its drive connected?), then run '
               f'`fha update-tools` again.')
        print(msg)
        out['warnings'].append(msg)
        return out
    out['items'] = items
    print(f'Browser add-ons in {ext_dir}:')
    if dry_run:
        if not ext_dir.exists():
            print(f'[dry-run] Would create the folder {ext_dir}.')
    else:
        if any(i['action'] == 'install' for i in items):
            try:
                ext_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                msg = (f'Could not create {ext_dir} ({exc}). Check the folder in '
                       f'`{SETTING}:` in fha.yaml, then run `fha update-tools` again.')
                print(msg)
                out['warnings'].append(msg)
                return out
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
