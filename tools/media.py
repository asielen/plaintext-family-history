#!/usr/bin/env python3
"""
media.py - fha media: content-hash dedupe and container-metadata probe for
incoming recordings (issues #43, #44; TOOLING §6a).

  fha media dedupe FILE [FILE...] [--root PATH] [--json PATH] [--quiet]
  fha media probe  FILE [--root PATH] [--json]

Both verbs are READ-ONLY. Neither renames, moves, deletes, or embeds anything
into an archive record or an original file; they exist to answer a question
before `fha process` files something, which is why they belong beside it
(TOOLING §6: "the 'is this already an S-id' question already lives" there).

WHY THIS FILE EXISTS
=====================
`import-recordings` (the skill that intakes phone-app exports and interview
recordings) needed both capabilities before a real `fha` verb existed for
either, and ran two interim hand-rolled enactments under the owner exception
`_STANDARD.md` §6 records: `.claude/skills/import-recordings/scripts/
find_duplicate_media.py` (a 1560-line, five-review-round-hardened dedupe
script) and a raw `ffprobe` call plus manual arithmetic (documented in
SKILL.md step 4). This module retires both exceptions by giving the tool
suite its own verbs.

`fha media dedupe`'s coverage-walking and dedup LOGIC is a close port of
`find_duplicate_media.py` - every one of its checks exists because a real
review round found a specific fail-open path, and re-deriving "size-then-hash
dedup" from a blank page would almost certainly reintroduce one of them (see
THE COVERAGE INVARIANT below). What did NOT get ported is that script's own
`roots:` reader (`_roots_from_config`/`resolve_media_roots` there) - it
existed only because the script runs standalone, outside the tool suite, with
no access to `_lib`. This module resolves roots through `_lib.get_roots` /
`_lib.resolve_path`, the tool suite's own canonical helper, used everywhere
else `fha.yaml`'s `roots:` mapping matters.

THE COVERAGE INVARIANT (dedupe)
================================
`fha media dedupe` is a safety gate: a skill imports what it clears. The
tempting reading of its job is "did I find a twin?", and that reading is what
failed the interim script, five review rounds running - each round found a
different way to examine less than the whole archive and still answer
confidently: a same-size candidate that could not be hashed, a `roots:`
spelling a hand-rolled parser could not read, a dot-directory pruned from the
walk, a subtree behind a directory symlink, a configured root that was not
there. Patched one at a time they look like five bugs. They are one bug: the
gate was answering "did I find a twin?" when the question it must answer is
"did I examine everything I claim to have examined?"

So `new` (exit 0) is a positive coverage statement, not a search result, and
it needs all five of these to hold - anything short of any one is
`indeterminate` (exit 3), never `new`:

  ROOTS        every media root fha.yaml names was resolved and is readable
               right now (resolve_media_roots).
  ENUMERATION  every file under every root was listed - hidden folders and
               folders behind a directory symlink included, each folder
               visited once (walk_covering's followlinks + visited-set loop
               guard).
  DOMAIN       the same audio/video rule (is_media) applies to both sides,
               and a path the human named that falls outside it is reported
               as not checked, never given a verdict (expand_inputs).
               "Archived" means FILED: the inbox is staging even when SPEC
               §12.4 lets it sit inside a media root, so a file waiting there
               is the opposite of one already imported.
  CANDIDATES   every same-size archived file was opened and hashed; one that
               could not be read leaves the question open (check_one).
  BATCH        the incoming files were compared against each other too, so
               one sitting exported twice under two names does not yield two
               true-but-jointly-wrong `new` verdicts (mark_bundle_repeats).

Two consequences worth keeping visible in the tests as well as the code: a
file handed to the verb that already lives in a media root is the archive's
OWN copy, reported as already filed and never cleared (filed_inside_media_root)
- otherwise the self-exclusion every dedupe check needs ("a file is not its
own duplicate") would turn an archived original into a file with no twin. And
the verb must never be able to answer a SMALLER question in the same words:
narrowing the search to whatever happens to be readable produces the same
clean exit as a complete check, which is the failure this whole contract
exists to close.

EXIT CODES
==========
`fha media dedupe` uses its own four-code ladder - given in GAP.md and
matched exactly to the interim script's - rather than the tool suite's usual
0=clean/1=warnings/2=errors/3=failure meaning. Read the numbers as what they
literally mean here, not as that other ladder:

  0  every incoming file was checked against every candidate and none
     matched - safe to import (DEDUPE_CLEAR)
  1  usage or configuration error: a bad path, an fha.yaml that will not
     parse, PyYAML missing (DEDUPE_USAGE)
  2  at least one incoming file is byte-identical to a filed recording, or to
     another file in the same batch (DEDUPE_DUPLICATE) - a clean "found,
     nothing to do" answer, not a failure
  3  the check could not be completed for at least one file - nothing is
     cleared for import (DEDUPE_INDETERMINATE)

`fha media probe` follows the ordinary ladder, read straight: 0 clean (the
offset was settled, so the derived local start is presented as confident),
1 warnings (duration and creation_time were read but the timezone could not
be settled, so no exact local start is offered), 2 errors (the container
carries no usable timestamp at all), 3 failure (the file could not be probed
- missing/unreadable, or neither ffprobe nor PyAV is available).

CODE MAP
========
  Path identity     canonical_path / _fold / _identity_key /
                     _identity_from_stat / fs_identity / same_file /
                     could_be_same_file / _is_inside / _path_parts /
                     _relative_under
  Roots             resolve_media_roots (via _lib.get_roots / resolve_path)
  Naming            media_root_label / build_named_roots / portable_path
  Dedupe index      is_media / walk_covering / index_sizes_by_root /
                     sha256_file / source_id_in
  Dedupe check      expand_inputs / filed_inside_media_root / check_one /
                     mark_bundle_repeats / apply_archive_coverage
  Dedupe safety     report_path_collision
  Dedupe engine/CLI run_media_dedupe / _rendered_dedupe_entry / _cmd_media_dedupe
  Probe arithmetic  _parse_iso8601 / _parse_filename_clock /
                     _solve_offset_from_filename / _derive_local_start
  Probe backends    _probe_with_ffprobe / _probe_with_pyav
  Probe engine/CLI  run_media_probe / _cmd_media_probe
  CLI wiring        _add_subcommands / register / _standalone_main
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _lib import (
    EXIT_CLEAN,
    EXIT_ERRORS,
    EXIT_FAILURE,
    EXIT_WARNINGS,
    FhaConfigError,
    Result,
    configure_utf8_stdout,
    fmt_id_display,
    format_ffprobe_error,
    get_roots,
    load_fha_yaml,
    parse_filename,
    pip_command,
    resolve_path,
    resolve_root_arg,
)

configure_utf8_stdout()

# Audio and video are the same job here: a video is a recording like any
# other, and whisper (and ffprobe) both read a video file's audio track/
# container the same way. Kept identical to find_duplicate_media.py's set so
# a file that dedupe considered media, probe does too.
MEDIA_EXTENSIONS = {
    '.aac', '.aif', '.aiff', '.amr', '.flac', '.m4a', '.m4b', '.mp3', '.oga',
    '.ogg', '.opus', '.wav', '.wma',
    '.3gp', '.avi', '.m4v', '.mkv', '.mov', '.mp4', '.mpeg', '.mpg', '.webm',
    '.wmv',
}

# Roots that can hold a FILED recording. `documents` is where interviews
# live; `photos` is included because a phone video can legitimately have been
# filed there before anyone thought of it as an interview.
MEDIA_ROOT_ALIASES = ('documents', 'photos')

# Staging, the opposite of filed. SPEC §12.4 lets the inbox live inside a
# media root's own workflow, so a file under it can be inside a media root
# while being exactly the thing the human is asking us to import.
STAGING_ROOT_ALIAS = 'inbox'

HASH_CHUNK = 1 << 20   # 1 MiB reads

# fha media dedupe's own exit-code ladder (GAP.md's contract; NOT the tool
# suite's usual 0/1/2/3 = clean/warnings/errors/failure meaning - see the
# module docstring's EXIT CODES section).
DEDUPE_CLEAR = 0
DEDUPE_USAGE = 1
DEDUPE_DUPLICATE = 2
DEDUPE_INDETERMINATE = 3

REPORT_EXAMPLE = 'dedupe-report.json'

# A "couple of minutes" (SKILL.md step 4) of slack when checking whether a
# filename clock actually predicts the container's creation_time, once
# rounded to the nearest quarter hour. Wider than a couple of minutes and the
# filename clock is not what it was taken for - SKILL.md's own words for when
# to give up on it, restated as a number here.
FILENAME_CLOCK_TOLERANCE_SECONDS = 180
FILENAME_CLOCK_ROUND_SECONDS = 900   # nearest quarter hour

# Real-world UTC offsets run from -12:00 (Baker/Howland Islands) to +14:00
# (Kiribati's Line Islands) - nothing on Earth is further out than that. A
# filename clock that is a whole number of DAYS off from creation_time (the
# wrong year typed into a filename, a camera clock reset to its epoch) still
# rounds to an exact multiple of the quarter-hour grid - miss lands at or
# near 0 - so the tolerance check alone would call a wrong-by-months date a
# confidently "solved" offset. Bounding to what a real timezone can be also
# closes the crash `datetime.timezone()` would otherwise raise on an offset
# past +/-24h (its own hard limit).
MIN_PLAUSIBLE_OFFSET_SECONDS = -12 * 3600
MAX_PLAUSIBLE_OFFSET_SECONDS = 14 * 3600


class ConfigProblem(Exception):
    """fha.yaml exists but cannot be trusted to say where the media lives.

    Raised rather than returned so no caller can accidentally carry on with an
    empty roots mapping. Falling back to `<archive>/documents` when the real
    root is external is not a safe default: it searches a folder that holds
    nothing and clears every incoming recording as new.
    """


def _reason(exc: OSError) -> str:
    """An OSError as one short clause, for a message a genealogist reads."""
    text = getattr(exc, 'strerror', None) or str(exc)
    return ' '.join(str(text).split())


# ---------------------------------------------------------------------------
# Path identity: which of two names are one file
#
# Ported near-verbatim from find_duplicate_media.py. This is filesystem
# identity logic, not config plumbing - it does not read fha.yaml and has no
# archive-suite equivalent to defer to (see AGENTS_TOOLING's "porting is
# authoring" note: every ported line is read against the contract again here,
# not merely copied).
# ---------------------------------------------------------------------------

def canonical_path(path: str) -> str:
    """One absolute, symlink-resolved, case-normalized spelling of a path.

    A string, no more: `os.path.normcase` folds case on Windows (where the OS
    itself folds it) and is a no-op elsewhere, so this is a normaliser, never
    an identity test. Identity goes through `same_file` (exact, both paths
    exist) or `could_be_same_file` (blunt, for a path that may not exist yet).
    Still the right comparison in the two places a wrong match would DROP
    something - `expand_inputs` and `check_one` - documented at each site.
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _fold(name: str) -> str:
    """One filename with its case and Unicode spelling flattened away.

    NFC-normalises (so an accent stored NFD by the filesystem folds the same
    as one typed NFC) then casefolds (broader than any single filesystem's own
    folding table). Used only where matching TOO MUCH costs a sentence and
    matching too little costs a recording (the --json collision check).
    """
    return unicodedata.normalize('NFC', name).casefold()


def _identity_key(path: str) -> str:
    """A deliberately blunt key: any two names for one file should share it."""
    return _fold(canonical_path(path))


def _identity_from_stat(st: os.stat_result, path: str) -> tuple[int, int] | str:
    """(device, inode) for a stat already taken, or the canonical string.

    Some network/Windows filesystems report inode 0 for everything; a key that
    collides for every file would prune an entire walk, so the canonical
    string - which never collides with a real (device, inode) pair in the same
    set - is the fallback.
    """
    return (st.st_dev, st.st_ino) if st.st_ino else canonical_path(path)


def fs_identity(path: str) -> tuple[int, int] | str:
    """The filesystem's own name for a path, for use as a dict key."""
    try:
        st = os.stat(path)
    except OSError:
        return canonical_path(path)
    return _identity_from_stat(st, path)


def same_file(a: str, b: str) -> bool:
    """Do these two names point at one file, as the filesystem sees it?

    `os.path.samefile` is correct through a symlink, a hard link, a case
    variant on a folding volume, and two mounts of one disk - none of which a
    string comparison can see. Used where a wrong TRUE is the dangerous
    direction and both paths exist.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return canonical_path(a) == canonical_path(b)


def could_be_same_file(a: str, b: str) -> bool:
    """Could writing to one of these land on the other? For the --json guard.

    A report path usually does not exist yet, so `samefile` cannot answer.
    Falls back to the blunt folded key (`_identity_key`) when one side is
    absent. Deliberately over-eager: a wrong match here costs one word on the
    command line, a missed one costs a recording.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return _identity_key(a) == _identity_key(b)


def _is_inside(path: str, root: str) -> bool:
    """Is `path` at or below `root`, as the filesystem sees it (not by string prefix)?

    Containment decides real things here - whether a handed-over recording is
    the archive's own copy, whether a folder is the inbox, whether a --json
    path would land inside a media root - so it is answered by identity. A
    string-prefix test reads two spellings of one folder as two folders and
    the verdict flips with it.
    """
    target = os.path.realpath(os.path.abspath(path))
    base = os.path.realpath(os.path.abspath(root))
    if target == base or target.startswith(base.rstrip(os.sep) + os.sep):
        return True
    cur = target
    while True:
        if same_file(cur, base):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def _path_parts(path: str) -> list[str]:
    return [part for part in os.path.abspath(path).replace('\\', '/').split('/') if part]


def _relative_under(target: str, root: str, fold: bool = False) -> str | None:
    """`target` written relative to `root`, or None when it is not below it.

    Compared component by component (never by string-slicing) because folding
    can change a string's length, so the reported path stays spelled the way
    the file on disk is spelled.
    """
    parts = _path_parts(target)
    base = _path_parts(root)
    if len(parts) < len(base):
        return None
    for part, base_part in zip(parts, base):
        if part != base_part and not (fold and _fold(part) == _fold(base_part)):
            return None
    return '/'.join(parts[len(base):])


# ---------------------------------------------------------------------------
# Media roots: resolved through `_lib.get_roots` / `_lib.resolve_path`, the
# tool suite's own canonical roots helper - NOT find_duplicate_media.py's
# hand-rolled `_roots_from_config`/`resolve_media_roots`, which existed only
# because that script has no `_lib` to import (see the module docstring).
# ---------------------------------------------------------------------------

def resolve_media_roots(
    archive_root: Path, fha_config: dict,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], str]:
    """Named media roots to search, plus any configured root that is not there.

    Returns `(named_roots, missing, staging)` - `named_roots` is
    `[(alias, absolute_path_str), ...]`; `staging` is the resolved inbox path.
    This is the ROOTS half of the coverage invariant: once this returns with
    an empty `missing`, every folder the archive says holds recordings is a
    folder this run can walk.

    WHAT COUNTS AS MISSING, AND THE ONE CASE THAT DOES NOT (ported reasoning
    from find_duplicate_media.py's `resolve_media_roots`): a root goes into
    `missing` when fha.yaml NAMES it and it is not a readable folder right
    now - an unplugged external drive, a renamed internal folder, a file where
    a folder should be, an alias with no value at all. The one case that is
    ordinary: an alias fha.yaml does NOT mention, whose built-in default
    folder does not exist internally yet (a young archive with no `photos/`).
    That one is silently skipped, never "missing" - refusing the run for it
    would refuse it forever.
    """
    roots_map = get_roots(fha_config)
    if not isinstance(roots_map, dict):
        raise ConfigProblem(
            "the archive's fha.yaml has a `roots:` setting that is not a "
            'list of `name: folder` lines (for example `documents: '
            'documents`). Run `fha doctor`, then run this command again.'
        )
    named: list[tuple[str, str]] = []
    missing: list[tuple[str, str]] = []
    for alias in MEDIA_ROOT_ALIASES:
        configured = alias in roots_map
        raw_value = '' if not configured else str(roots_map.get(alias) or '')
        if configured and not raw_value.strip():
            missing.append((alias, 'no folder named in fha.yaml'))
            continue
        base = str(resolve_path(alias, fha_config, archive_root).resolve())
        if os.path.isdir(base):
            # Both folders exist by here, so `same_file` is exact: one folder
            # configured twice under two aliases is one root.
            if not any(same_file(base, existing) for _label, existing in named):
                named.append((alias, base))
        elif configured or os.path.exists(base) or not _is_inside(base, str(archive_root)):
            missing.append((alias, raw_value or base))
    staging = str(resolve_path(STAGING_ROOT_ALIAS, fha_config, archive_root).resolve())
    return named, missing, staging


# ---------------------------------------------------------------------------
# Naming roots, so no reported path is either absolute or a bare filename
# (ported from find_duplicate_media.py; presentation logic, not config
# plumbing, so it is not affected by the roots-reader swap above)
# ---------------------------------------------------------------------------

def media_root_label(path: str, taken: set) -> str:
    """A short name for a folder outside the archive (an incoming bundle)."""
    base = os.path.basename(os.path.normpath(path)) or 'media-root'
    label = base
    n = 1
    while label in taken:
        n += 1
        label = '%s-%d' % (base, n)
    return label


def build_named_roots(
    media_roots: list[tuple[str, str]], archive_root: str, incoming_args: list[str],
) -> list[tuple[str, str]]:
    """The label -> folder table every reported path is rendered against.

    Longest root first, so a media root nested inside the archive wins over
    the archive root itself. The archive root carries the empty label ("no
    prefix"), matching the archive's own alias-form path convention (SPEC
    §12.4). An incoming argument that is itself inside a media root gets no
    `incoming` label - see `portable_path`.
    """
    named = list(media_roots)
    if archive_root:
        named.append(('', os.path.abspath(archive_root)))
    seen: list[str] = []
    count = 0
    for arg in incoming_args:
        arg_abs = os.path.abspath(arg)
        base = arg_abs if os.path.isdir(arg_abs) else os.path.dirname(arg_abs)
        if any(same_file(base, done) for done in seen):
            continue
        seen.append(base)
        if any(_is_inside(base, root) for _label, root in media_roots):
            continue
        count += 1
        named.append(('incoming' if count == 1 else 'incoming-%d' % count, base))
    named.sort(key=lambda pair: len(pair[1]), reverse=True)
    return named


def portable_path(path: str, named_roots: list[tuple[str, str]]) -> str:
    """Render `path` under the name of the root that holds it.

    Never absolute, never a `../..` climb, and never reduced to a bare
    filename while a named root still contains it (AGENTS_TOOLING §11:
    runtime output that can land in an artifact carries alias-form paths).
    Three passes, most trustworthy spelling first: exact, folded (for a path
    the human typed), resolved (for a path reached through a directory link).
    """
    target = os.path.abspath(path)
    target_real = os.path.realpath(target)
    for resolved, fold in ((False, False), (False, True), (True, True)):
        for label, root in named_roots:
            base = os.path.realpath(root) if resolved else os.path.abspath(root)
            rel = _relative_under(target_real if resolved else target, base, fold)
            if rel is None:
                continue
            if not rel:
                return label or '.'
            return '%s/%s' % (label, rel) if label else rel
    return os.path.basename(target)


# ---------------------------------------------------------------------------
# Dedupe index and hashing
# ---------------------------------------------------------------------------

def is_media(path: str) -> bool:
    """The one media rule, applied identically to both sides of the check."""
    return os.path.splitext(path)[1].lower() in MEDIA_EXTENSIONS


def walk_covering(root: str, unreadable: list, visited: set):
    """Yield `(dirpath, filenames)` for every folder at or below `root`.

    The ENUMERATION half of the coverage invariant, ported from
    find_duplicate_media.py's `walk_covering` (chosen over `_lib.walk_files`
    because that helper does not follow directory symlinks - exactly the gap
    this function exists to close). Three things a plain `os.walk` does not
    do:

    * FOLLOWS DIRECTORY SYMLINKS (`followlinks=True`) - a plain walk skips
      such a subtree in total silence, so an archived recording reached
      through `documents/interviews -> /Volumes/Audio/interviews` would not
      exist as far as the gate could tell.
    * ENUMERATES EACH FOLDER ONCE - following links means loops and diamonds
      are possible; a folder already visited this run is pruned by its own
      (device, inode), which ends the loop without losing coverage.
    * REPORTS WHAT IT COULD NOT ENTER - both `os.walk`'s own `onerror` and a
      folder that cannot be stat'd land in `unreadable`, turning a partial
      walk into `indeterminate` instead of a short list nobody notices.
    """
    def on_walk_error(err: OSError) -> None:
        unreadable.append(getattr(err, 'filename', None) or 'a folder')

    for dirpath, dirnames, filenames in os.walk(root, onerror=on_walk_error, followlinks=True):
        try:
            st = os.stat(dirpath)
        except OSError:
            unreadable.append(dirpath)
            dirnames[:] = []
            continue
        key = _identity_from_stat(st, dirpath)
        if key in visited:
            dirnames[:] = []
            continue
        visited.add(key)
        yield dirpath, filenames


def index_sizes_by_root(
    named_roots: list[tuple[str, str]], staging: str | None = None,
) -> tuple[dict[int, list[str]], list[str]]:
    """Map byte size -> [archived media paths of that size], plus what failed.

    Sizes come straight from the directory-walk's own stat, so this opens no
    files - the cheap half of the check; hashing happens only on a size
    collision. Every folder is walked, dot-prefixed and symlinked ones
    included (see `walk_covering`'s docstring): pruning either dropped real
    material in the interim script's own history and is not repeated here.

    `staging` (the inbox) is excluded: this index answers "what is already
    FILED", and a recording waiting in the inbox has no source record yet -
    calling it a twin would tell the human his own not-yet-imported file is
    already archived, stopping the import that would file it.

    `photos_ignore:` is deliberately NOT honoured here, on purpose and not by
    oversight: it tells the photo catalog what is not the archive's subject,
    it does not unfile anything - a recording under an ignored folder is
    still on disk, still attached to a source record, still exactly the twin
    an incoming file might be.
    """
    by_size: dict[int, list[str]] = {}
    unreadable: list[str] = []
    visited: set = set()
    if staging:
        visited.add(fs_identity(staging))

    for _label, root in named_roots:
        for dirpath, filenames in walk_covering(root, unreadable, visited):
            if staging and _is_inside(dirpath, staging):
                continue
            for name in filenames:
                if not is_media(name):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    unreadable.append(full)
                    continue
                by_size.setdefault(size, []).append(full)
    return by_size, unreadable


def sha256_file(path: str, cache: dict) -> str:
    """SHA-256 of a whole file, memoised per run by filesystem identity."""
    key = fs_identity(path)
    if key in cache:
        return cache[key]
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        while True:
            chunk = fh.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    cache[key] = h.hexdigest()
    return cache[key]


def source_id_in(name: str) -> str | None:
    """The `S-...` id a processed documents-root filename carries, if any.

    Reuses `_lib.parse_filename` (the same parser every record-filename
    reader in the suite uses) rather than find_duplicate_media.py's own
    hand-rolled suffix scan - one canonical filename grammar, not two. Photos
    are never renamed, so a photo-root twin has no id in its filename; `fha
    find` resolves those from the embedded keyword.
    """
    info = parse_filename(name)
    if info and info.get('id_type') == 'S':
        return fmt_id_display(info['id_str'])
    return None


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def expand_inputs(paths: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Flatten files and folders into the media files to check, sorted.

    Returns `(files, unreadable, not_media)` - the DOMAIN half of the
    coverage invariant: every path the human named is accounted for in
    exactly one of the three. `not_media` is reported, never silently
    dropped, because silence about a file named on the command line reads as
    clearance.
    """
    out: list[str] = []
    unreadable: list[str] = []
    not_media: list[str] = []
    visited: set = set()

    for p in paths:
        if os.path.isdir(p):
            for dirpath, filenames in walk_covering(p, unreadable, visited):
                for name in sorted(filenames):
                    if is_media(name):
                        out.append(os.path.join(dirpath, name))
        elif os.path.isfile(p):
            if is_media(p):
                out.append(p)
            else:
                not_media.append(p)
        else:
            unreadable.append(p)
    seen: set = set()
    unique: list[str] = []
    for p in sorted(out):
        # Canonical (not (device, inode)): two hard links in one bundle are
        # two files the human is about to import and stay two entries here
        # (mark_bundle_repeats reports them as the same recording); a folded
        # key would drop one unexamined, which costs more than one duplicated
        # line ever could.
        key = canonical_path(p)
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique, unreadable, not_media


def filed_inside_media_root(
    path: str, media_roots: list[tuple[str, str]], staging: str | None = None,
) -> str | None:
    """The archived path of an incoming file that already lives in the archive.

    Returns None for anything under `staging` (the inbox): a recording
    waiting there has NOT been imported, so "already filed" would be the
    wrong answer for the very workflow this check exists to serve.
    """
    target = os.path.realpath(os.path.abspath(path))
    if staging and _is_inside(target, staging):
        return None
    for _label, base in media_roots:
        base_real = os.path.realpath(os.path.abspath(base))
        if target != base_real and _is_inside(target, base_real):
            return target
    return None


def check_one(
    path: str, by_size: dict[int, list[str]], cache: dict,
    media_roots: list[tuple[str, str]] = (), staging: str | None = None,
) -> dict:
    """Result dict for one incoming file: duplicate, new, or indeterminate.

    `new` is only ever returned when every same-size archived candidate was
    actually opened and hashed and none matched (the CANDIDATES half of the
    coverage invariant). A candidate that could not be read leaves the
    question open - rounding it down to "not a twin" is exactly how a
    byte-identical recording gets imported a second time.
    """
    entry = {'file': os.path.basename(path), 'verdict': 'new', 'duplicates': [], 'unchecked': []}
    filed = filed_inside_media_root(path, media_roots, staging)
    if filed is not None:
        entry['verdict'] = 'duplicate'
        entry['already_filed'] = True
        entry['duplicates'] = [{'archived_path': filed, 'source_id': source_id_in(filed)}]
        entry['detail'] = ('this file is the archive\'s own copy - it is already filed, so '
                           'there is nothing to import')
        return entry
    try:
        size = os.path.getsize(path)
    except OSError as e:
        entry['verdict'] = 'indeterminate'
        entry['detail'] = 'this file could not be read (%s)' % _reason(e)
        return entry
    entry['bytes'] = size
    # A file is not its own duplicate - compared canonically (not by
    # (device, inode)) because a hard link is one inode with two directory
    # entries, and one of them can be the archive's filed copy; `samefile`
    # would drop the filed recording from the candidate list and clear a
    # recording the archive already holds.
    candidates = [c for c in by_size.get(size, []) if canonical_path(c) != canonical_path(path)]
    entry['same_size_candidates'] = len(candidates)
    if not candidates:
        return entry
    try:
        digest = sha256_file(path, cache)
    except OSError as e:
        entry['verdict'] = 'indeterminate'
        entry['detail'] = 'this file could not be read (%s)' % _reason(e)
        return entry
    entry['sha256'] = digest
    for cand in candidates:
        try:
            cand_digest = sha256_file(cand, cache)
        except OSError as e:
            entry['unchecked'].append({'archived_path': cand, 'detail': _reason(e)})
            continue
        if cand_digest != digest:
            continue
        entry['duplicates'].append({'archived_path': cand, 'source_id': source_id_in(cand)})
    if entry['duplicates']:
        entry['verdict'] = 'duplicate'
    elif entry['unchecked']:
        entry['verdict'] = 'indeterminate'
        entry['detail'] = (
            '%d archived recording(s) of exactly this size could not be read, so a '
            'byte-identical twin cannot be ruled out' % len(entry['unchecked']))
    return entry


def mark_bundle_repeats(results: list[dict], paths: list[str], cache: dict) -> None:
    """Catch the same recording arriving twice in ONE batch - the BATCH invariant.

    Every honestly `new` file of a size shared with another `new` file in the
    batch is hashed and compared; the first of each byte-identical group keeps
    `new`, the rest become `duplicate` carrying `repeat_of`. Only `new`
    entries are grouped: a file already a duplicate of something filed is
    being skipped anyway, and one that could not be checked keeps its more
    specific reason.
    """
    by_size: dict[int, list[tuple[dict, str]]] = {}
    for entry, path in zip(results, paths):
        if entry['verdict'] == 'new' and entry.get('bytes') is not None:
            by_size.setdefault(entry['bytes'], []).append((entry, path))
    for group in by_size.values():
        if len(group) < 2:
            continue
        first_seen: dict[str, str] = {}
        for entry, path in group:
            try:
                digest = sha256_file(path, cache)
            except OSError as e:
                entry['verdict'] = 'indeterminate'
                entry['detail'] = 'this file could not be read (%s)' % _reason(e)
                continue
            entry['sha256'] = digest
            if digest in first_seen:
                entry['verdict'] = 'duplicate'
                entry['repeat_of'] = first_seen[digest]
                entry['detail'] = 'this is the same recording as another file in the same batch'
            else:
                first_seen[digest] = path


def apply_archive_coverage(results: list[dict], archive_unreadable: list[str]) -> None:
    """Hold every verdict to what the archive side actually managed to examine.

    An archived file or folder nobody could read might hold the twin of ANY
    incoming file, so it turns every would-be `new` into `indeterminate`
    rather than being attached to one result. `duplicate` stands (finding a
    twin never depended on seeing the rest); an already-`indeterminate` entry
    keeps its more specific reason.
    """
    if not archive_unreadable:
        return
    for entry in results:
        if entry['verdict'] == 'new':
            entry['verdict'] = 'indeterminate'
            entry['detail'] = (
                '%d archived recording(s) could not be read at all, so no recording can be '
                'cleared as new this run' % len(archive_unreadable))


# ---------------------------------------------------------------------------
# Refusing to write onto anything this run reads (the --json safety rule)
# ---------------------------------------------------------------------------

def report_path_collision(
    report_path: str | None, incoming: list[str], archived: list[str],
    media_roots: list[tuple[str, str]], config_path: str | None, render,
) -> str | None:
    """The plain refusal for a --json path that lands on a file this run reads.

    Called before the first byte is hashed. A read-only tool's whole promise
    is that it changes nothing, and `--json` is the one place that could be
    broken: the report is written LAST, so a recording it lands on would
    already have been hashed and cleared as `new` when it is destroyed. Every
    comparison is `could_be_same_file`, never a string equality test, because
    the report path usually does not exist yet.
    """
    if not report_path:
        return None
    for path in incoming:
        if could_be_same_file(path, report_path):
            return ('--json points at one of the recordings being checked (%s). This '
                    'check only ever reads recordings, and writing the report there would '
                    'destroy that one. Nothing was written. Give the report a filename of '
                    'its own - for example --json %s - and run the command again.'
                    % (render(path), REPORT_EXAMPLE))
    for path in archived:
        if could_be_same_file(path, report_path):
            return ('--json points at a recording already filed in the archive (%s). This '
                    'check never writes to the archive, and the report would replace that '
                    'recording. Nothing was written. Give the report a filename of its own - '
                    'for example --json %s - and run the command again.'
                    % (render(path), REPORT_EXAMPLE))
    if config_path and could_be_same_file(config_path, report_path):
        return ('--json points at the archive\'s fha.yaml, the file that says which folders '
                'hold your recordings. The report would replace it. Nothing was written. '
                'Give the report a filename of its own - for example --json %s - and run the '
                'command again.' % REPORT_EXAMPLE)
    for _label, base in media_roots:
        if _is_inside(report_path, base):
            return ('--json would write into the archive\'s %s folder, which holds your filed '
                    'recordings and their transcripts. This check never writes to the '
                    'archive. Nothing was written. Put the report somewhere of your own - for '
                    'example --json %s - and run the command again.'
                    % (render(base), REPORT_EXAMPLE))
    return None


# ---------------------------------------------------------------------------
# Dedupe: engine (run_media_dedupe) and interface (_cmd_media_dedupe)
# ---------------------------------------------------------------------------

def _rendered_dedupe_entry(entry: dict, render) -> dict:
    """One check_one/mark_bundle_repeats result with every path in alias form."""
    out = dict(entry,
               duplicates=[{'archived_path': render(d['archived_path']), 'source_id': d['source_id']}
                          for d in entry['duplicates']],
               unchecked=[{'archived_path': render(u['archived_path']), 'detail': u['detail']}
                         for u in entry['unchecked']])
    if 'repeat_of' in out:
        out['repeat_of'] = render(out['repeat_of'])
    return out


def run_media_dedupe(
    archive_root: Path, fha_config: dict, *, incoming_args: list[str],
    json_path: str | None = None,
) -> Result:
    """Content-hash every incoming file against everything already filed.

    `data` carries the same shape find_duplicate_media.py's `--json` report
    did (`results`, `duplicates`, `indeterminate`, `complete`,
    `could_not_be_read`, `not_checked`, `media_roots`) so a caller reading the
    Result gets the identical picture the interim script's JSON gave. Every
    path in `data` is alias-form (`documents/...`) or `incoming/...` - never
    an absolute machine path (AGENTS_TOOLING §11).

    `result.exit_code` is DEDUPE_CLEAR/USAGE/DUPLICATE/INDETERMINATE - see the
    module docstring's EXIT CODES section, a deliberately different ladder
    from the tool suite's usual one. `result.ok` is True for CLEAR and
    DUPLICATE (the check ran to completion and produced a real answer, a
    found duplicate being exactly as legitimate an answer as "new") and False
    for USAGE and INDETERMINATE (the check could not be trusted).
    """
    result = Result(data={
        'status': None, 'media_roots': [], 'checked': 0, 'duplicates': 0,
        'indeterminate': 0, 'complete': False, 'results': [],
        'could_not_be_read': [], 'not_checked': [],
    })

    for p in incoming_args:
        if not os.path.exists(p):
            result.data['status'] = 'usage'
            result.exit_code = DEDUPE_USAGE
            result.ok = False
            result.add('error', 'not found: %s - check the path and run the command again' % p)
            return result

    try:
        media_roots, missing_roots, staging = resolve_media_roots(archive_root, fha_config)
    except ConfigProblem as e:
        result.data['status'] = 'usage'
        result.exit_code = DEDUPE_USAGE
        result.ok = False
        result.add('error', str(e))
        return result
    if missing_roots:
        result.data['status'] = 'usage'
        result.exit_code = DEDUPE_USAGE
        result.ok = False
        result.add('error',
                   'the archive\'s fha.yaml says your recordings are kept in %s, and that '
                   'folder is not there right now. Recordings filed in it cannot be read, so '
                   'this check cannot tell you whether your new recordings are already in the '
                   'archive. Reconnect the drive, create the folder, or fix the path in '
                   'fha.yaml, then run the command again.'
                   % ', '.join('%s (%s)' % (label, value or 'no folder named')
                               for label, value in missing_roots))
        return result
    if not media_roots:
        result.data['status'] = 'usage'
        result.exit_code = DEDUPE_USAGE
        result.ok = False
        result.add('error',
                   'this archive has no documents or photos folder to check against yet. '
                   'Nothing is filed, so nothing can be a duplicate - import normally with '
                   '`fha process`.')
        return result

    incoming, incoming_unreadable, not_media = expand_inputs(incoming_args)
    if not incoming and not incoming_unreadable:
        result.data['status'] = 'usage'
        result.exit_code = DEDUPE_USAGE
        result.ok = False
        result.add('error',
                   'none of those paths hold an audio or video file. Supported extensions: %s'
                   % ', '.join(sorted(MEDIA_EXTENSIONS)))
        return result

    named_roots = build_named_roots(media_roots, str(archive_root), incoming_args)

    def portable(path: str) -> str:
        return portable_path(path, named_roots)

    by_size, archive_unreadable = index_sizes_by_root(media_roots, staging)

    archived_paths = [p for paths in by_size.values() for p in paths]
    config_path = str(archive_root / 'fha.yaml')
    collision = report_path_collision(json_path, incoming, archived_paths, media_roots,
                                      config_path, portable)
    if collision:
        result.data['status'] = 'usage'
        result.exit_code = DEDUPE_USAGE
        result.ok = False
        result.add('error', collision)
        return result

    cache: dict = {}
    results = [check_one(p, by_size, cache, media_roots, staging) for p in incoming]
    for entry, src in zip(results, incoming):
        entry['path'] = portable(src)

    mark_bundle_repeats(results, incoming, cache)
    apply_archive_coverage(results, archive_unreadable)

    duplicates = [r for r in results if r['verdict'] == 'duplicate']
    indeterminate = [r for r in results if r['verdict'] == 'indeterminate']

    incomplete: list[str] = []
    for path in archive_unreadable:
        incomplete.append('an archived file or folder could not be read: %s' % portable(path))
    for path in incoming_unreadable:
        incomplete.append('an incoming folder could not be listed, so the recordings in it '
                          'were never checked: %s' % portable(path))
    not_checked = ['not an audio or video file, so nothing was checked for it: %s' % portable(path)
                   for path in not_media]

    result.data['media_roots'] = [label for label, _root in media_roots]
    result.data['checked'] = len(results)
    result.data['duplicates'] = len(duplicates)
    result.data['indeterminate'] = len(indeterminate)
    result.data['complete'] = not indeterminate and not incomplete
    result.data['results'] = [_rendered_dedupe_entry(r, portable) for r in results]
    result.data['could_not_be_read'] = incomplete
    result.data['not_checked'] = not_checked

    for r in results:
        if r.get('already_filed'):
            sid = r['duplicates'][0]['source_id']
            result.add('info',
                       'DUPLICATE  %s is already filed in the archive%s - nothing to import'
                       % (r['path'], ' (%s)' % sid if sid else ''), path=r['path'])
        elif r.get('repeat_of'):
            result.add('info',
                       'DUPLICATE  %s is byte-identical to %s in the same batch - import one of '
                       'them, not both' % (r['path'], portable(r['repeat_of'])), path=r['path'])
        elif r['verdict'] == 'duplicate':
            for d in r['duplicates']:
                sid = d['source_id']
                result.add('info', 'DUPLICATE  %s is byte-identical to %s%s'
                           % (r['path'], portable(d['archived_path']),
                              ' (%s)' % sid if sid else ''), path=r['path'])
        elif r['verdict'] == 'indeterminate':
            result.add('warning', 'UNCHECKED  %s - %s'
                       % (r['path'], r.get('detail', 'the check could not finish')), path=r['path'])
        else:
            result.add('info', 'new        %s' % r['path'], path=r['path'])
        for u in r['unchecked']:
            result.add('warning', '           could not read %s (%s)'
                       % (portable(u['archived_path']), u['detail']))
    for path in not_media:
        result.add('info', 'SKIPPED    %s - not an audio or video file, so nothing was checked '
                   'for it' % portable(path))

    if indeterminate or incomplete:
        result.data['status'] = 'indeterminate'
        result.exit_code = DEDUPE_INDETERMINATE
        result.ok = False
        result.add('warning',
                   'The duplicate check did not finish. Nothing marked UNCHECKED is cleared for '
                   'import: until every archived recording of the same size has been read, one '
                   'of them may be the same file. Reconnect the drive holding the archive\'s '
                   'recordings, or fix what is named above, then run the same command again - on '
                   'the whole batch, not on the part that happened to work.')
    elif duplicates:
        result.data['status'] = 'duplicate'
        result.exit_code = DEDUPE_DUPLICATE
        result.ok = True
        result.add('info',
                   'Do not import the duplicates: report each one with the path of the '
                   'recording it repeats and leave the bundle untouched. Confirm the twin\'s '
                   'record with `fha find <S-id>`. Where the twin is another file in the same '
                   'batch, import the one named and skip the other - one sitting is one source '
                   'record.', next_step='fha find <S-id>')
    else:
        result.data['status'] = 'new'
        result.exit_code = DEDUPE_CLEAR
        result.ok = True

    if json_path:
        payload = {
            'tool': 'fha media dedupe', 'checked': result.data['checked'],
            'duplicates': result.data['duplicates'], 'indeterminate': result.data['indeterminate'],
            'complete': result.data['complete'], 'media_roots': result.data['media_roots'],
            'results': result.data['results'], 'could_not_be_read': incomplete,
            'not_checked': not_checked,
            'paths_note': 'every path is written under the name of the folder it sits in - the '
                          'archive\'s own documents/photos alias, or `incoming` for the bundle '
                          'being checked. Absolute machine paths are deliberately not recorded.',
        }
        dest = os.path.abspath(json_path)
        tmp = '%s.tmp-%d' % (dest, os.getpid())
        try:
            parent = os.path.dirname(dest)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as fh:
                fh.write(json.dumps(payload, indent=2) + '\n')
            os.replace(tmp, dest)
        except OSError as e:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            result.data['status'] = 'usage'
            result.exit_code = DEDUPE_USAGE
            result.ok = False
            result.add('error', 'could not write %s: %s. Pick a --json path in a writable '
                       'folder and run the command again.' % (json_path, e))
            return result
        result.note_changed(dest)

    return result


def _cmd_media_dedupe(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha media dedupe')
    if archive_root is None:
        return DEDUPE_USAGE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print('ERROR: %s' % e, file=sys.stderr)
        return DEDUPE_USAGE
    result = run_media_dedupe(archive_root, fha_config, incoming_args=args.incoming,
                              json_path=getattr(args, 'json', None))
    if not getattr(args, 'quiet', False):
        for m in result.messages:
            stream = sys.stderr if m.level == 'error' else sys.stdout
            print(m.text, file=stream)
        if result.data.get('checked'):
            print('checked %d recording(s)' % result.data['checked'])
    else:
        for m in result.messages:
            if m.level == 'error':
                print(m.text, file=sys.stderr)
    return result.exit_code


# ---------------------------------------------------------------------------
# Probe arithmetic: duration + creation_time -> derived local start
#
# The one part of this module SKILL.md flags for extra scrutiny (the build
# plan's "Opus reviews E2's arithmetic" note). The formula is SKILL.md step
# 4's, restated in code rather than re-derived: QuickTime/MP4 writes
# `creation_time` in UTC at the moment the recording STOPPED; an app-written
# filename clock names local time at the moment it STARTED. So:
#
#     local_stop  = filename_time + duration      (if a filename clock exists)
#     utc_stop    = creation_time
#     offset      = local_stop - utc_stop          (rounded to nearest 15 min)
#     local_start = (creation_time + offset) - duration
#
# `com.apple.quicktime.creationdate`, when present, gives the offset directly
# (it is local time WITH its offset, naming the same instant as
# `creation_time`) and settles the question with no filename arithmetic at
# all - checked first, since it is the stronger source (a device's own clock,
# not an inference from a name an app chose).
# ---------------------------------------------------------------------------

_ISO_RE = re.compile(
    r'^(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})[T ]'
    r'(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})(?:\.(?P<frac>\d+))?'
    r'(?P<tz>Z|[+-]\d{2}:?\d{2})?\s*$'
)


def _parse_iso8601(value: str) -> datetime.datetime | None:
    """Parse an ISO-8601-ish timestamp as ffprobe/QuickTime write it.

    Handles both spellings this module actually sees: `creation_time`
    (`2020-06-14T20:15:00.000000Z`, always UTC, the `Z` explicit) and
    `com.apple.quicktime.creationdate` (`1998-06-14T20:15:00-0500`, local
    time with its own offset, no colon in the offset). Returns an AWARE
    datetime, or None when the string does not parse - never a silent
    fallback to "assume UTC" or "assume local", both of which would be
    exactly the guess this verb exists to refuse.
    """
    if not value:
        return None
    m = _ISO_RE.match(value.strip())
    if not m:
        return None
    frac = m.group('frac') or '0'
    microsecond = int((frac + '000000')[:6])
    tz = m.group('tz')
    try:
        if tz is None:
            tzinfo = None
        elif tz == 'Z':
            tzinfo = datetime.timezone.utc
        else:
            sign = 1 if tz[0] == '+' else -1
            digits = tz[1:].replace(':', '')
            hh, mm = int(digits[:2]), int(digits[2:4] or '0')
            # `datetime.timezone` refuses an offset outside +/-24h. The regex
            # accepts any two digits (00-99) for hh, so a corrupt or
            # malformed tag (a bit-flipped byte, an encoder bug) can spell an
            # offset like '+99:00' - this is untrusted data straight out of
            # the media file's own container metadata, not a value this
            # module produced, so it is treated as an unparseable string
            # (return None) rather than crashing the whole probe.
            tzinfo = datetime.timezone(sign * datetime.timedelta(hours=hh, minutes=mm))
        return datetime.datetime(
            int(m.group('y')), int(m.group('mo')), int(m.group('d')),
            int(m.group('h')), int(m.group('mi')), int(m.group('s')),
            microsecond, tzinfo=tzinfo)
    except ValueError:
        return None


_FILENAME_CLOCK_RE = re.compile(
    r'(?P<y>\d{4})[-_]?(?P<mo>\d{2})[-_]?(?P<d>\d{2})'
    r'[ _T-](?P<h>\d{2})[.:_-]?(?P<mi>\d{2})[.:_-]?(?P<s>\d{2})'
)


def _parse_filename_clock(name: str) -> datetime.datetime | None:
    """Pull a naive local wall-clock date+time out of a recording's filename.

    Matches the handful of shapes an app or a camera actually writes
    (`2020-06-14 15.30.00`, `2020-06-14_15-30-00`, `20200614_153000`,
    `hartley-1998-06-14T20-15-00`). Deliberately does NOT try to solve a
    relative label like "Thursday at 3-11 PM" - GAP.md's own example of a
    filename clock that carries no absolute date, and the reason this
    function returning None (offset unsolved) is a correct, expected outcome
    for exactly that case, not a bug to chase.
    """
    m = _FILENAME_CLOCK_RE.search(name)
    if not m:
        return None
    try:
        return datetime.datetime(
            int(m.group('y')), int(m.group('mo')), int(m.group('d')),
            int(m.group('h')), int(m.group('mi')), int(m.group('s')))
    except ValueError:
        return None


def _solve_offset_from_filename(
    filename_dt: datetime.datetime, duration_seconds: float, creation_time: datetime.datetime,
) -> tuple[datetime.timedelta | None, float, float]:
    """Solve for the recording's UTC offset from its filename clock.

    Returns `(offset, raw_seconds, miss_seconds)`: `offset` is the timedelta
    once rounded to the nearest quarter hour, or None when the fit misses by
    more than `FILENAME_CLOCK_TOLERANCE_SECONDS` - SKILL.md's "a fit that
    misses by more than a couple of minutes means the filename clock is not
    what you took it for" - OR when the rounded offset falls outside
    `MIN_PLAUSIBLE_OFFSET_SECONDS`/`MAX_PLAUSIBLE_OFFSET_SECONDS`. That second
    guard matters because a filename clock a whole number of days off from
    `creation_time` still rounds to an exact quarter hour (miss lands near
    0) - a wrong year in the filename would otherwise "solve" a many-day
    offset with full confidence, and one past +/-24h would crash
    `_derive_local_start`'s `datetime.timezone()` outright. `raw_seconds` is
    the unrounded offset implied by this one file (useful for the data
    payload); `miss_seconds` is the distance from that raw value to the
    nearest quarter hour - the number a human-facing message wants, since
    "misses by 18300s" (the raw offset
    itself, which is mostly just this recording's real timezone) reads as a
    much bigger problem than "misses by 300s" (how far off the QUARTER-HOUR
    FIT is) actually is.
    """
    creation_utc_naive = creation_time.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    local_stop = filename_dt + datetime.timedelta(seconds=duration_seconds)
    raw = (local_stop - creation_utc_naive).total_seconds()
    rounded = round(raw / FILENAME_CLOCK_ROUND_SECONDS) * FILENAME_CLOCK_ROUND_SECONDS
    miss = abs(raw - rounded)
    if miss > FILENAME_CLOCK_TOLERANCE_SECONDS:
        return None, raw, miss
    if not (MIN_PLAUSIBLE_OFFSET_SECONDS <= rounded <= MAX_PLAUSIBLE_OFFSET_SECONDS):
        return None, raw, miss
    return datetime.timedelta(seconds=rounded), raw, miss


def _derive_local_start(
    creation_time: datetime.datetime, duration_seconds: float, offset: datetime.timedelta,
) -> datetime.datetime:
    """local_start = (creation_time + offset) - duration, in the derived zone.

    `creation_time` is the container's UTC-at-stop instant; adding `offset`
    converts that same instant to local time, and subtracting the recording's
    own length walks it back from stop to start - the arithmetic SKILL.md step
    4 states in prose, here as one line so it can be tested.
    """
    utc_naive = creation_time.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    local_naive = utc_naive + offset - datetime.timedelta(seconds=duration_seconds)
    return local_naive.replace(tzinfo=datetime.timezone(offset))


def _fmt_offset(offset: datetime.timedelta) -> str:
    total_minutes = int(offset.total_seconds() // 60)
    sign = '+' if total_minutes >= 0 else '-'
    total_minutes = abs(total_minutes)
    return '%s%02d:%02d' % (sign, total_minutes // 60, total_minutes % 60)


# ---------------------------------------------------------------------------
# Probe backends
# ---------------------------------------------------------------------------

def _probe_with_ffprobe(file_path: Path) -> dict:
    """Run ffprobe and return `{'duration': float|None, 'tags': {lower_key: value}}`.

    Raises RuntimeError (missing binary) or ValueError (bad/unreadable file,
    unparseable JSON) - the caller translates both into a plain message, never
    a traceback.
    """
    cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(file_path)]
    try:
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, encoding='utf-8')
    except FileNotFoundError as e:
        raise RuntimeError(format_ffprobe_error('fha media probe')) from e
    if proc.returncode != 0:
        raise ValueError('ffprobe could not read %s (%s)'
                         % (file_path.name, (proc.stderr or '').strip() or 'unknown error'))
    try:
        data = json.loads(proc.stdout or '{}')
    except json.JSONDecodeError as e:
        raise ValueError('ffprobe\'s output for %s could not be read: %s' % (file_path.name, e)) from e
    fmt = data.get('format') or {}
    duration = None
    raw_duration = fmt.get('duration')
    if raw_duration not in (None, ''):
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = None
    tags = {str(k).lower(): v for k, v in (fmt.get('tags') or {}).items()}
    return {'duration': duration, 'tags': tags}


def _probe_with_pyav(file_path: Path) -> dict:
    """PyAV fallback, matching `transcribe_audio.py`'s own use of PyAV.

    Used only when ffprobe is not on PATH. `container.duration` is in
    microseconds (PyAV/ffmpeg's AV_TIME_BASE convention); `container.metadata`
    carries whatever container-level tags the format exposes (a narrower set
    than ffprobe's for some containers, which is why ffprobe is preferred
    whenever it is available).
    """
    try:
        import av
    except ImportError as e:
        raise RuntimeError(
            'fha media probe needs either ffprobe or the PyAV Python package to read a '
            'recording\'s container metadata. Install ffmpeg (which includes ffprobe), or '
            'install PyAV with `%s`, then run this command again.' % pip_command('av')) from e
    try:
        container = av.open(str(file_path))
    except Exception as e:  # noqa: BLE001 - PyAV raises its own family of decode errors
        raise ValueError('the recording %s could not be opened (%s)' % (file_path.name, e)) from e
    try:
        duration = None
        # `is not None`, not truthiness: PyAV reports an unknown duration as
        # None, but a genuine (if degenerate) zero-length container is a real
        # 0, and `if container.duration:` would silently read that as
        # "unknown" too - the same class of bug as treating an empty string
        # or a zero count as "absent" elsewhere in this codebase.
        if container.duration is not None:
            duration = container.duration / 1_000_000
        tags = {str(k).lower(): v for k, v in (container.metadata or {}).items()}
    finally:
        container.close()
    return {'duration': duration, 'tags': tags}


# ---------------------------------------------------------------------------
# Probe: engine (run_media_probe) and interface (_cmd_media_probe)
# ---------------------------------------------------------------------------

def _resolve_probe_input(raw: str, archive_root: Path) -> Path | None:
    """Path resolution, forgiving like `fha process` (TOOLING §6): as typed
    first, then retried under the archive root."""
    p = Path(raw)
    if p.is_file():
        return p
    retry = archive_root / raw
    if retry.is_file():
        return retry
    return None


def run_media_probe(archive_root: Path, fha_config: dict, *, file_arg: str) -> Result:
    """Read a recording's true duration and creation_time, and derive its
    local start - never silently applying this machine's own timezone.

    `data`: `{file, backend, duration_seconds, creation_time_utc, offset,
    offset_source, local_start, local_start_date, utc_date, crosses_midnight,
    filename_clock}`. `offset_source` is one of `'quicktime_creationdate'`,
    `'filename_clock'`, or `None` (never determined) - GAP.md's own
    requirement that the verb say WHERE the offset came from, not just what
    it computed. See the module docstring's EXIT CODES section for the ladder.
    """
    result = Result(data={
        'file': None, 'backend': None, 'duration_seconds': None,
        'creation_time_utc': None, 'offset': None, 'offset_source': None,
        'local_start': None, 'local_start_date': None, 'utc_date': None,
        'crosses_midnight': False, 'filename_clock': None,
    })

    path = _resolve_probe_input(file_arg, archive_root)
    if path is None:
        result.exit_code = EXIT_FAILURE
        result.ok = False
        result.add('error',
                   'not found: %s - checked it as given and under %s. Check the path and run '
                   'the command again.' % (file_arg, archive_root))
        return result
    result.data['file'] = path.name

    have_ffprobe = shutil.which('ffprobe') is not None
    try:
        if have_ffprobe:
            probed = _probe_with_ffprobe(path)
            result.data['backend'] = 'ffprobe'
        else:
            probed = _probe_with_pyav(path)
            result.data['backend'] = 'pyav'
    except RuntimeError as e:
        result.exit_code = EXIT_FAILURE
        result.ok = False
        result.add('error', str(e), next_step='fha doctor')
        return result
    except ValueError as e:
        result.exit_code = EXIT_FAILURE
        result.ok = False
        result.add('error', str(e))
        return result

    duration = probed['duration']
    tags = probed['tags']
    result.data['duration_seconds'] = duration
    if duration is not None:
        result.add('info', 'duration: %s' % _fmt_duration(duration))
    else:
        result.add('warning',
                   'the container does not report a duration, so a local start time cannot '
                   'be derived even if a creation timestamp is present.')

    creation_raw = tags.get('creation_time')
    creation_time = _parse_iso8601(creation_raw) if creation_raw else None
    # A parsed-but-naive creation_time (no 'Z'/offset marker) is treated as
    # UNUSABLE, not as UTC: `creation_time` is documented (TOOLING §6a) to
    # always be UTC, but a naive datetime's `.astimezone()` presumes THIS
    # MACHINE's own local timezone (Python's own documented behavior for a
    # naive value) - exactly the guess this verb exists to refuse, and the
    # one further down this function that a disagreeing-but-aware
    # `com.apple.quicktime.creationdate` tag would turn into an unhandled
    # `TypeError` (naive minus aware) instead of a plain message.
    if creation_time is not None and creation_time.tzinfo is None:
        creation_time = None
    if creation_time is None:
        result.exit_code = EXIT_ERRORS
        result.ok = False
        result.add('warning',
                   'this recording\'s container carries no usable creation timestamp. '
                   'Falling back to the file\'s own modified-on-disk time would be a guess '
                   'about the FILE, not the recording, so this verb reports none rather than '
                   'one - ask when the recording was made, or read it off the app that made '
                   'it.')
        return result
    result.data['creation_time_utc'] = creation_time.astimezone(datetime.timezone.utc).isoformat()
    result.add('info', 'container creation_time (UTC, at the moment recording stopped): %s'
               % result.data['creation_time_utc'])

    if duration is None:
        result.exit_code = EXIT_ERRORS
        result.ok = False
        result.add('warning',
                   'a creation timestamp is present but the duration is not, so the local '
                   'start (creation_time minus duration) cannot be computed.')
        return result

    offset = None
    offset_source = None
    qt_raw = tags.get('com.apple.quicktime.creationdate')
    qt_dt = _parse_iso8601(qt_raw) if qt_raw else None
    if qt_dt is not None and qt_dt.utcoffset() is not None:
        offset = qt_dt.utcoffset()
        offset_source = 'quicktime_creationdate'
        drift = abs((qt_dt.astimezone(datetime.timezone.utc) - creation_time).total_seconds())
        if drift > 60:
            result.add('warning',
                       'com.apple.quicktime.creationdate (%s) and creation_time (%s) do not '
                       'name the same instant (off by %.0fs) - using the offset anyway, but '
                       'this container\'s timestamps disagree with each other and are worth a '
                       'second look.' % (qt_raw, creation_raw, drift))
        result.add('info', 'timezone settled from com.apple.quicktime.creationdate: %s'
                   % _fmt_offset(offset))
    else:
        filename_dt = _parse_filename_clock(path.name)
        if filename_dt is not None:
            solved, raw_seconds, miss_seconds = _solve_offset_from_filename(
                filename_dt, duration, creation_time)
            result.data['filename_clock'] = {
                'filename_time': filename_dt.isoformat(),
                'raw_offset_seconds': raw_seconds,
                'miss_seconds': miss_seconds,
                'solved': solved is not None,
            }
            if solved is not None:
                offset = solved
                offset_source = 'filename_clock'
                result.add('info',
                           'timezone solved from the filename clock (%s): offset %s, confirmed '
                           'by filename_time + duration matching creation_time to within a few '
                           'minutes.' % (filename_dt.isoformat(timespec='seconds'), _fmt_offset(offset)))
            elif miss_seconds <= FILENAME_CLOCK_TOLERANCE_SECONDS:
                # A good quarter-hour fit that still got rejected: the only
                # other reason is an implausible magnitude (MIN/MAX_PLAUSIBLE_
                # OFFSET_SECONDS) - a whole number of days off from
                # creation_time rounds cleanly too, so a fit alone cannot
                # tell a wrong year in the filename from a real timezone.
                result.add('warning',
                           'the filename carries a clock (%s) that fits creation_time almost '
                           'exactly, but only by implying an offset around %.1f hours - no real '
                           'timezone reaches that far, so this points to a wrong date somewhere '
                           '(the filename, or the container), not a solved timezone.'
                           % (filename_dt.isoformat(timespec='seconds'), raw_seconds / 3600.0))
            else:
                result.add('warning',
                           'the filename carries a clock (%s), but filename_time + duration '
                           'misses the nearest quarter-hour fit to creation_time by %.0fs - too '
                           'far off to trust as this recording\'s timezone.'
                           % (filename_dt.isoformat(timespec='seconds'), miss_seconds))

    if offset is None:
        result.data['offset_source'] = None
        result.exit_code = EXIT_WARNINGS
        result.ok = False
        result.add('warning',
                   'this recording\'s timezone could not be established - not from the '
                   'container\'s own local timestamp, and not from a filename clock. No exact '
                   'local start is offered; record the recording date as an interval spanning '
                   'the candidate days, and say why in the source\'s notes (SKILL.md step 4).')
        return result

    result.data['offset'] = _fmt_offset(offset)
    result.data['offset_source'] = offset_source
    local_start = _derive_local_start(creation_time, duration, offset)
    result.data['local_start'] = local_start.isoformat()
    result.data['local_start_date'] = local_start.date().isoformat()
    utc_date = creation_time.astimezone(datetime.timezone.utc).date()
    result.data['utc_date'] = utc_date.isoformat()
    result.data['crosses_midnight'] = local_start.date() != utc_date
    result.add('info', 'derived local start: %s' % local_start.isoformat())
    if result.data['crosses_midnight']:
        result.add('warning',
                   'the derived local start date (%s) differs from the UTC date (%s) - this '
                   'recording straddles midnight once converted to its own timezone.'
                   % (result.data['local_start_date'], result.data['utc_date']))

    result.exit_code = EXIT_CLEAN
    result.ok = True
    return result


def _fmt_duration(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return '%d:%02d:%02d' % (h, m, s) if h else '%d:%02d' % (m, s)


def _cmd_media_probe(args: argparse.Namespace) -> int:
    archive_root = resolve_root_arg(args, command='fha media probe')
    if archive_root is None:
        return EXIT_FAILURE
    try:
        fha_config = load_fha_yaml(archive_root, strict=True)
    except FhaConfigError as e:
        print('ERROR: %s' % e, file=sys.stderr)
        return EXIT_FAILURE
    result = run_media_probe(archive_root, fha_config, file_arg=args.file)
    if getattr(args, 'use_json', False):
        print(json.dumps(result.as_dict(), indent=2))
    else:
        for m in result.messages:
            stream = sys.stderr if m.level in ('error', 'warning') else sys.stdout
            print(m.text, file=stream)
    return result.exit_code


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def _add_subcommands(subs: argparse._SubParsersAction, *, suppress_root: bool) -> None:
    def root_arg(p: argparse.ArgumentParser) -> None:
        if suppress_root:
            p.add_argument('--root', metavar='PATH', default=argparse.SUPPRESS,
                           help='Archive root (auto-detected if omitted).')
        else:
            p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')

    dd_p = subs.add_parser(
        'dedupe', help='Content-hash incoming recording(s) against what is already filed')
    dd_p.add_argument('incoming', metavar='FILE_OR_FOLDER', nargs='+',
                      help='the recording(s) that arrived (files, or folders to walk)')
    dd_p.add_argument('--json', metavar='PATH', default=None,
                      help='also write the findings as JSON (a filename of its own - a path '
                           'landing on a recording, an archived file, or fha.yaml is refused)')
    dd_p.add_argument('--quiet', action='store_true', help='print only warnings/errors')
    root_arg(dd_p)
    dd_p.set_defaults(func=_cmd_media_dedupe)

    pr_p = subs.add_parser(
        'probe', help='Read a recording\'s true duration and container creation timestamp')
    pr_p.add_argument('file', metavar='FILE', help='the recording to probe')
    pr_p.add_argument('--json', action='store_true', dest='use_json',
                      help='machine-readable JSON instead of plain-language lines')
    root_arg(pr_p)
    pr_p.set_defaults(func=_cmd_media_probe)


_CLI_DESCRIPTION = """\
Answer a question about an incoming recording before it is filed.

  fha media dedupe <file...> [--json PATH]
  fha media probe  <file> [--json]

Both are read-only. `dedupe` content-hashes incoming media against everything
already filed (size first, SHA-256 only on a size collision) so a phone
export renamed by the app never becomes a second source record for one
recording. `probe` reads a recording's true duration and container
creation_time, and derives its local start time - never guessing the
timezone the recording was made in."""


def register(subs: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register 'media' onto the main fha parser."""
    p = subs.add_parser(
        'media', help='Content-hash dedupe and container-metadata probe for recordings',
        description=_CLI_DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument('--root', metavar='PATH', help='Archive root (auto-detected if omitted).')
    sub = p.add_subparsers(dest='media_command', metavar='SUBCOMMAND')
    _add_subcommands(sub, suppress_root=True)
    # Bare `fha media` (no verb) is a usage error, matching `fha confirm`/`fha person`.
    p.set_defaults(func=lambda a: p.print_help() or EXIT_ERRORS)
    return p


def _standalone_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='fha media', description=_CLI_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest='media_command', metavar='SUBCOMMAND')
    _add_subcommands(sub, suppress_root=False)
    args = parser.parse_args(argv)
    if not getattr(args, 'func', None):
        parser.print_help()
        return EXIT_ERRORS
    return args.func(args)


if __name__ == '__main__':
    sys.exit(_standalone_main())
