#!/usr/bin/env python3
"""
find_duplicate_media.py - is this recording already filed, by content not by name?

WHY THIS EXISTS
===============
A real 16-zip phone export held six recordings that were byte-identical to audio
already in the archive. Nothing in the filenames said so: the app had renamed
them to relative weekday labels ("Thursday at 3-11 PM"), so the same afternoon
arrived three times under three different names. Imported blind, one recording
gets two source records and its claims split between them.

`fha` has no content-dedupe verb yet (wanted: `fha media dedupe`, project issue
#43, recorded in this folder's GAP.md). Until it ships, the import-recordings
skill runs this script instead of guessing from filenames or from a full-text
search - a text search finds a transcript that reads alike, which is a different
question and a much weaker answer.

WHAT IT DOES
============
Size first, hash second. Reading every archived recording to hash it is wasteful
and the archive discourages bulk reads of the asset roots (_STANDARD.md §8), so:

  1. Walk the archive's media roots and record each media file's byte size.
     Sizes come from the directory entry - no file is opened.
  2. For an incoming file, look up its exact byte size. No size match means no
     byte-identical twin can exist, and the file is cleared without a single read.
  3. Only on a size collision, SHA-256 both sides and compare. Digests are cached
     per run so a folder of ten incoming files never re-hashes the same archived
     candidate ten times.

Equal size is common and proves nothing; equal SHA-256 over the whole file is
what "byte-identical" means here, and it is the only thing this script will call
a duplicate.

THREE VERDICTS, BECAUSE THE GATE MUST FAIL CLOSED
=================================================
This script is a safety gate: the skill imports what it clears. So "new" is not
the absence of a duplicate, it is a positive statement that every archived file
which could possibly have been a twin was read and was not one. Anything that
stops the script making that statement - an archived candidate on a drive that
just went away, a folder the account cannot list, an `fha.yaml` that will not
parse, a configured documents root that is not mounted - is reported as
`indeterminate` and exits nonzero. It is never rounded down to "new".

That direction matters more than it looks. The cost of a wrong `indeterminate`
is that a human plugs a drive back in and runs one command again. The cost of a
wrong `new` is a second source record for one recording, with that afternoon's
claims split between two S-ids and nothing in either record saying so.

WHAT IT NEVER DOES
==================
Read-only, both sides. It opens files to hash them and nothing else: no rename,
no move, no delete, no write to any archived file. It reports; the human and
`fha process` decide. It also never claims the twin's S-id is correct beyond
what the filename says - `fha find <S-id>` is how you confirm the record.

The one path this script writes is `--json`, and that flag is the only way the
read-only promise could ever be broken: a mistyped or tab-completed report path
that lands on a recording would destroy the file it was reporting on, after
having already hashed it and cleared it as safe to import. So the report path is
canonicalised and compared against every incoming recording, every archived
media file, the archive's own fha.yaml, and the media roots themselves BEFORE
anything is hashed, and the run is refused outright on a collision. See
`report_path_collision`.

WHERE IT LOOKS
==============
The archive root is the folder holding `fha.yaml`, found by walking up from
`--root` (or from the current directory). The media roots come from that file's
`roots:` mapping, because `documents:` and `photos:` are allowed to point outside
the archive entirely - hardcoding `<archive>/documents` silently finds nothing on
those archives. `--media-root` overrides the lot when you want to check against
one folder.

Reading that mapping needs PyYAML, which is the archive tooling's own core
dependency (`tools/requirements.txt`) - without it no `fha` command runs either.
There is deliberately no hand-rolled fallback parser: one that understands part
of YAML cannot tell "this archive configures no roots" from "this archive
configures roots I could not read", and the second one silently searches the
wrong folder. Missing PyYAML is refused with the install command, not guessed
around. See `_roots_from_config`.

Inside a media root, EVERY folder is walked - dot-prefixed ones included. A
`documents/.private/` is the human's own folder and may hold exactly the
recording being checked; `.git/`, `.fha/` and `.cache/` hold no media file, so
walking them changes nothing but a few directory listings. See
`index_sizes_by_root`.

HOW PATHS ARE REPORTED
======================
Every path this script prints or writes is rendered against a NAMED root:

    documents/interviews/hartley-1998-06-14/hartley-1998-06-14_S-wb91h3hjrr.m4a
    FamilyMedia/2019/voice-memo-004.m4a       (a --media-root named FamilyMedia)
    incoming/Thursday at 3-11 PM.m4a          (a file the human handed over)

Two requirements pull against each other here, and only this form satisfies both.
A machine-specific path must never reach a file that gets archived or shared -
neither `/home/alice/FamilyMedia/x.m4a` nor the `../../home/alice/FamilyMedia/
x.m4a` that a plain relative path produces for an external root (AGENTS_TOOLING
§11 privacy, SPEC §12.4 alias-form paths). But a bare filename throws away the
one thing the report is for - WHICH archived file matched, when three folders
each hold a `recording.m4a` and two bundles each hold a `New Recording 4.m4a`.
A path relative to a root the human already has a name for keeps every directory
component that is the archive's own layout (identical on every machine) and drops
every component above the root (this one computer's business). The alias name is
the archive's own `documents:`/`photos:` alias where there is one, the folder's
own basename for an explicit `--media-root`, and `incoming` for the bundle the
human passed in.

Exit codes
==========
  0  every incoming file was checked against every candidate and none matched -
     safe to import
  2  at least one incoming file is byte-identical to a filed recording; the
     skill's rule is to report that item and skip it, never to import it twice
  3  the check could not be completed for at least one file (something could not
     be read). Nothing in the run is cleared for import; fix what is named and
     run the same command again
  1  usage or configuration error (bad path, unreadable archive root, an
     fha.yaml that will not parse, PyYAML not installed)

CODE MAP
========
  Config      find_archive_root / _roots_from_config / resolve_media_roots
  Naming      media_root_label / build_named_roots / portable_path
  Index       is_media / index_sizes_by_root / sha256_file / source_id_in
  Check       expand_inputs / check_one
  Safety      canonical_path / report_path_collision
  CLI         build_parser / fail / main
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

TOOL_NAME = "find_duplicate_media.py"
TOOL_VERSION = "1.3.0"

REPORT_EXAMPLE = "dedupe-report.json"

# Audio and video are the same job here: a video is a recording like any other.
MEDIA_EXTENSIONS = {
    ".aac", ".aif", ".aiff", ".amr", ".flac", ".m4a", ".m4b", ".mp3", ".oga",
    ".ogg", ".opus", ".wav", ".wma",
    ".3gp", ".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm",
    ".wmv",
}

# Roots that can hold a filed recording. `documents` is where interviews live;
# `photos` is included because a phone video can legitimately have been filed
# there before anyone thought of it as an interview.
MEDIA_ROOT_ALIASES = ("documents", "photos")

HASH_CHUNK = 1 << 20      # 1 MiB reads; large enough to be fast, small enough
                          # that hashing a 4 GB video does not sit in memory

EXIT_CLEAR = 0
EXIT_USAGE = 1
EXIT_DUPLICATE = 2
EXIT_INDETERMINATE = 3


class ConfigProblem(Exception):
    """fha.yaml exists but cannot be trusted to say where the media lives.

    Raised rather than returned so that no caller can accidentally carry on with
    an empty roots mapping. Falling back to the built-in `<archive>/documents`
    default when the real root is external is not a safe default: it searches a
    folder that holds nothing, finds no twin, and reports every recording as new.
    """


def fail(msg):
    """One-line plain error on stderr, always naming what to do next."""
    sys.stderr.write("%s: error: %s\n" % (TOOL_NAME, msg))
    return EXIT_USAGE


def _reason(exc):
    """An OSError as one short clause, for a message a genealogist reads."""
    text = getattr(exc, "strerror", None) or str(exc)
    return " ".join(str(text).split())


# ---------------------------------------------------------------------------
# Locating the archive and its media roots
# ---------------------------------------------------------------------------
def find_archive_root(start):
    """Walk up from `start` looking for the folder that holds fha.yaml.

    Same rule every `fha` command uses, restated here rather than imported:
    a skill's script must keep working inside an installed archive, where the
    tools are vendored under `.fha/tools/` and are not importable from here.
    """
    cur = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(cur, "fha.yaml")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _roots_from_config(archive_root):
    """The `roots:` mapping out of fha.yaml, or {} when the file names none.

    Every failure to READ that mapping raises instead of returning {}. An
    archive whose documents root lives on an external drive is entirely normal,
    and for such an archive a silent fallback to the built-in default searches
    an empty folder and clears every incoming recording as new - the exact
    failure this script exists to prevent, arriving as a clean exit 0.

    WHY THERE IS NO HAND-ROLLED YAML FALLBACK
    An earlier version read the `roots:` block line by line when PyYAML was
    absent. It understood `  documents: documents` and nothing else, so a
    perfectly valid archive written as `roots: {documents: /external/media}` -
    or as a quoted key, or an anchor, or a multi-line flow mapping - came back
    empty and the check ran against the archive's own empty `documents/`
    skeleton. Three separate review rounds each found a different YAML spelling
    that fell through it, which is the answer: a parser that understands a
    subset of a format cannot tell "no roots configured" from "roots I could
    not read", and this gate's whole value is that it never confuses the two.

    Requiring PyYAML costs nothing real. It is the archive tooling's own
    declared dependency (`tools/requirements.txt`, "Core (every command)"), so
    an archive without it cannot run `fha` at all - the duplicate check is not
    what the human is missing. And `--media-root <folder>` still checks against
    a named folder with no config read at all, for anyone who truly cannot
    install it.
    """
    path = os.path.join(archive_root, "fha.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as e:
        raise ConfigProblem(
            "the archive's fha.yaml could not be read (%s). It is the file that "
            "says which folders hold your recordings, so the duplicate check "
            "cannot run without it. Fix the file's permissions and run the "
            "command again, or check against one folder with "
            "--media-root <folder>." % _reason(e))
    try:
        import yaml
    except ImportError:
        raise ConfigProblem(
            "the archive's fha.yaml cannot be read here because PyYAML is not "
            "installed. That file is what says which folders hold your "
            "recordings - they are allowed to live on another drive - and "
            "guessing at it would search the wrong folder and call a recording "
            "new when the archive already holds it. Install it with "
            "`python -m pip install pyyaml` (the archive's own `fha` commands "
            "need it too), then run this command again. Or check against one "
            "folder with --media-root <folder>.") from None
    try:
        data = yaml.safe_load(text) or {}
    except Exception as e:
        raise ConfigProblem(
            "the archive's fha.yaml is not valid YAML (%s), so the folders "
            "holding your recordings cannot be read from it. Run "
            "`fha doctor` to see what is wrong with the file, then run this "
            "command again." % _reason(e))
    if not isinstance(data, dict):
        raise ConfigProblem(
            "the archive's fha.yaml does not read as a list of settings, so "
            "the folders holding your recordings cannot be read from it. "
            "Run `fha doctor`, then run this command again.")
    roots = data.get("roots")
    if roots is None:
        return {}
    if not isinstance(roots, dict):
        raise ConfigProblem(
            "the archive's fha.yaml has a `roots:` setting that is not a "
            "list of `name: folder` lines (for example `documents: "
            "documents`). Run `fha doctor`, then run this command again.")
    return {str(k): str(v) for k, v in roots.items() if v}


def resolve_media_roots(archive_root):
    """Named media roots to search, plus any configured root that is not there.

    Returns `(named_roots, missing)`. `named_roots` is a list of
    `(alias, absolute path)` - the alias is the archive's own name for the root
    (`documents`, `photos`), which is what every reported path is rendered
    against.

    Resolved through `roots:` rather than joined onto the archive root, because
    an external documents root is normal (AGENTS.md: "asset roots may live
    OUTSIDE this folder"). A hardcoded `<archive>/documents` on such an archive
    finds zero files and reports every incoming recording as new.

    A root that points OUTSIDE the archive and is not there right now - the
    external drive nobody plugged in, the path that moved - goes into `missing`
    instead of being quietly skipped. Its recordings still exist somewhere; they
    just cannot be read this run, so nothing can be cleared against them and the
    caller refuses rather than reporting every incoming file as new.

    A missing folder INSIDE the archive is a different thing and is ordinary: a
    young archive with no `photos/` yet has no photos anywhere, so there is
    nothing that could have been hidden by not looking. It appears in neither
    list. A configured root that exists but is not a folder is a mistake in
    fha.yaml either way, and goes into `missing` so somebody fixes it.
    """
    roots = _roots_from_config(archive_root)
    named = []
    missing = []
    for alias in MEDIA_ROOT_ALIASES:
        value = roots.get(alias, alias)
        base = value if os.path.isabs(value) else os.path.join(archive_root, value)
        base = os.path.abspath(base)
        if os.path.isdir(base):
            if all(base != existing for _label, existing in named):
                named.append((alias, base))
        elif os.path.exists(base) or not _is_inside(base, archive_root):
            missing.append((alias, value))
    return named, missing


def _is_inside(path, root):
    """Is `path` at or below `root`? Used to tell a young archive from a lost drive."""
    path_key = os.path.normcase(os.path.abspath(path))
    root_key = os.path.normcase(os.path.abspath(root))
    if path_key == root_key:
        return True
    if not root_key.endswith(os.sep):
        root_key += os.sep
    return path_key.startswith(root_key)


# ---------------------------------------------------------------------------
# Naming roots, so no reported path is either absolute or nameless
# ---------------------------------------------------------------------------
def media_root_label(path, taken):
    """A short name for an explicit --media-root folder.

    Its own basename, because that is the word the human used for it and it
    carries no directory structure: `/home/alice/FamilyMedia` reports as
    `FamilyMedia/...`, which says where the twin was found without saying
    anything about this computer. Duplicated basenames get a numeric suffix so
    two roots never render onto each other.
    """
    base = os.path.basename(os.path.normpath(path)) or "media-root"
    label = base
    n = 1
    while label in taken:
        n += 1
        label = "%s-%d" % (base, n)
    return label


def build_named_roots(media_roots, archive_root, incoming_args):
    """The label -> folder table every reported path is rendered against.

    One table for the whole run, so the JSON report, the console lines and any
    future output all spell a path the same way. Longest root first: a media
    root nested inside the archive must win over the archive root itself, or a
    filed recording would render as a bare archive-relative path instead of the
    alias form the rest of the archive uses.

    The archive root carries the empty label, meaning "relative to the archive"
    with no prefix - that is already the archive's own way of writing a path
    (SPEC 12.4), so `inbox/x.m4a` needs no decoration.
    """
    named = list(media_roots)
    if archive_root:
        named.append(("", os.path.abspath(archive_root)))
    seen = set()
    count = 0
    for arg in incoming_args:
        arg_abs = os.path.abspath(arg)
        base = arg_abs if os.path.isdir(arg_abs) else os.path.dirname(arg_abs)
        key = os.path.normcase(base)
        if key in seen:
            continue
        seen.add(key)
        count += 1
        named.append(("incoming" if count == 1 else "incoming-%d" % count, base))
    named.sort(key=lambda pair: len(pair[1]), reverse=True)
    return named


def portable_path(path, named_roots):
    """Render `path` under the name of the root that holds it.

    Never absolute, never a `../..` climb out of the archive, and never reduced
    to a bare filename while a named root still contains it - see "HOW PATHS ARE
    REPORTED" in the module docstring for why both halves of that matter. The
    last-resort basename is for a path under no known root at all, which the
    callers here do not produce.
    """
    target = os.path.abspath(path)
    key = os.path.normcase(target)
    for label, root in named_roots:
        root_abs = os.path.abspath(root)
        prefix = os.path.normcase(root_abs)
        if key == prefix:
            return label or "."
        if not prefix.endswith(os.sep):
            prefix += os.sep
        if key.startswith(prefix):
            rel = target[len(root_abs):].lstrip("\\/").replace("\\", "/")
            return "%s/%s" % (label, rel) if label else rel
    return os.path.basename(target)


# ---------------------------------------------------------------------------
# Size index and hashing
# ---------------------------------------------------------------------------
def is_media(path):
    return os.path.splitext(path)[1].lower() in MEDIA_EXTENSIONS


def index_sizes_by_root(named_roots):
    """Map byte size -> [archived media paths of that size], plus what failed.

    Sizes come from `os.scandir`'s stat, which the directory walk already has,
    so building this index opens no files at all. It is the cheap half of the
    check; hashing only happens for the sizes that actually collide.

    Returns `(by_size, unreadable)`. An archived file or folder that cannot be
    read is NOT the same thing as one that is not a twin: it is a file this run
    never saw. Every such path is returned so the caller can refuse to clear
    anything, because a recording sitting in an unlistable folder is exactly
    where a byte-identical twin would hide.

    NO FOLDER IS SKIPPED, INCLUDING DOT-PREFIXED ONES. An earlier version
    pruned every directory whose name began with a dot. That silently dropped
    `documents/.private/interview.m4a` - a folder a human makes for exactly the
    material he is most careful about - from the walk, and dropped it without
    landing in `unreadable`, so its twin arrived, matched nothing, and was
    cleared as new. There is no third option here: a skipped subtree can only
    be honest as `indeterminate`, and an archive that reports indeterminate on
    every run because it happens to contain a `.git` is a gate nobody obeys.
    So we walk everything. The machine-owned dot-folders an archive actually
    has - `.fha/` (vendored tools), `.cache/` (sqlite), `.git/` - hold no file
    with a media extension, so walking them costs one directory listing each
    and changes no verdict; every other dot-folder in a media root is the
    human's own and may well hold a filed recording.
    """
    by_size = {}
    unreadable = []

    def on_walk_error(err):
        unreadable.append(getattr(err, "filename", None) or "a folder")

    for _label, root in named_roots:
        for dirpath, _dirnames, filenames in os.walk(root, onerror=on_walk_error):
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


def sha256_file(path, cache):
    """SHA-256 of a whole file, memoised per run.

    Whole-file, not a sampled prefix: two recordings of the same conversation
    from the same app share long identical headers, and a prefix hash would call
    them the same file. The cache matters because one incoming folder is often
    checked against the same size-colliding archived candidate many times over.
    """
    key = os.path.abspath(path)
    if key in cache:
        return cache[key]
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    cache[key] = h.hexdigest()
    return cache[key]


def source_id_in(name):
    """The `_S-xxxxxxxxxx` an archived filename carries, if any.

    Documents-root files are renamed `{slug}_{S-id}.{ext}` at processing, so the
    S-id is readable straight off the filename with no index lookup. Photos are
    never renamed and carry theirs as an embedded keyword instead, which needs
    exiftool - out of scope here, so a photo-root twin is reported by path alone
    and `fha find` resolves the rest.
    """
    stem = os.path.splitext(os.path.basename(name))[0]
    for part in reversed(stem.split("_")):
        low = part.lower()
        if len(low) == 12 and low.startswith("s-") and low[2:].isalnum():
            return "S-" + low[2:]
    return None


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def expand_inputs(paths):
    """Flatten files and folders into the media files to check, sorted.

    Returns `(files, unreadable)`. A subfolder of the incoming bundle that
    cannot be listed is reported rather than skipped: the recordings inside it
    were never checked, and a bundle that is imported wholesale would carry them
    past the gate unexamined.

    Dot-prefixed folders are walked, for that same reason and for symmetry with
    `index_sizes_by_root`. A recording under `incoming/.old/` that this function
    never returns is a recording the human's bundle still contains and `fha
    process` will still file, so leaving it out of the list is the gate saying
    nothing about a file it is about to wave through.
    """
    out = []
    unreadable = []

    def on_walk_error(err):
        unreadable.append(getattr(err, "filename", None) or "a folder")

    for p in paths:
        if os.path.isdir(p):
            for dirpath, _dirnames, filenames in os.walk(p, onerror=on_walk_error):
                for name in sorted(filenames):
                    if is_media(name):
                        out.append(os.path.join(dirpath, name))
        elif os.path.isfile(p):
            out.append(p)
    seen = set()
    unique = []
    for p in sorted(out):
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique, unreadable


def check_one(path, by_size, cache):
    """Result dict for one incoming file: duplicate, new, or indeterminate.

    Three verdicts, not two, and the distinction is the whole safety story.
    "new" authorises an import, so it is only ever returned when every same-size
    archived candidate was actually opened and hashed and none of them matched.
    A candidate that could not be read leaves the question open, and an open
    question is `indeterminate`: the unreadable candidates are listed on the
    entry, and the caller exits nonzero rather than clearing the file.

    Rounding an unreadable candidate down to "not a twin" is how a byte-
    identical recording gets imported a second time - one afternoon under two
    S-ids, its claims split between them, and nothing in either record saying so.
    """
    entry = {"file": os.path.basename(path), "verdict": "new",
             "duplicates": [], "unchecked": []}
    try:
        size = os.path.getsize(path)
    except OSError as e:
        entry["verdict"] = "indeterminate"
        entry["detail"] = "this file could not be read (%s)" % _reason(e)
        return entry
    entry["bytes"] = size
    candidates = [c for c in by_size.get(size, [])
                  if os.path.normcase(os.path.abspath(c))
                  != os.path.normcase(os.path.abspath(path))]
    entry["same_size_candidates"] = len(candidates)
    if not candidates:
        return entry
    try:
        digest = sha256_file(path, cache)
    except OSError as e:
        entry["verdict"] = "indeterminate"
        entry["detail"] = "this file could not be read (%s)" % _reason(e)
        return entry
    entry["sha256"] = digest
    for cand in candidates:
        try:
            cand_digest = sha256_file(cand, cache)
        except OSError as e:
            entry["unchecked"].append({"archived_path": cand,
                                       "detail": _reason(e)})
            continue
        if cand_digest != digest:
            continue
        entry["duplicates"].append({
            "archived_path": cand,
            "source_id": source_id_in(cand),
        })
    if entry["duplicates"]:
        entry["verdict"] = "duplicate"
    elif entry["unchecked"]:
        entry["verdict"] = "indeterminate"
        entry["detail"] = (
            "%d archived recording(s) of exactly this size could not be read, "
            "so a byte-identical twin cannot be ruled out"
            % len(entry["unchecked"]))
    return entry


# ---------------------------------------------------------------------------
# Refusing to write onto anything this run reads
# ---------------------------------------------------------------------------
def canonical_path(path):
    """One spelling of a path, so two ways of naming one file compare equal.

    `--json ./report.json` and `--json report.json` are the same file; on Windows
    and macOS so are `Report.json` and `report.json`; and a symlink is whatever
    it points at. A collision check that misses any of those spellings is not a
    check, it is a coin flip - which is why every comparison here goes through
    this function instead of through a bare string equality test.

    Restated rather than imported from the sibling attribute_speakers.py, for the
    same reason `find_archive_root` is restated: each of this skill's scripts has
    to run standalone from any directory, on nothing but the standard library.
    The two spellings must stay identical; if one changes, change both.
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def report_path_collision(report_path, incoming, archived, media_roots,
                          config_path, render):
    """The plain refusal for a --json path that lands on a file this run reads.

    This script's whole contract is that it changes nothing on either side, and
    `--json` is the single place that contract could be broken. The failure is
    silent and total: the report is written last, so the recording it lands on
    has already been hashed, compared and printed as `new` - the run announces
    that a file is safe to import at the moment it destroys it. Pointed at an
    archived candidate instead, the same typo overwrites a filed original.

    Called before the first byte is hashed, so the refusal is instant, and long
    before anything is written, so nothing is half-done when it fires. `render`
    turns an archived path into the named-root form used everywhere else, since
    no message from this script names a machine path.

    Returns the message to refuse with, or None when the path is safe to write.
    """
    if not report_path:
        return None
    target = canonical_path(report_path)
    target_real = os.path.realpath(os.path.abspath(report_path))
    for path in incoming:
        if canonical_path(path) == target:
            return ("--json points at one of the recordings being checked (%s). "
                    "This check only ever reads recordings, and writing the "
                    "report there would destroy that one. Nothing was written. "
                    "Give the report a filename of its own - for example "
                    "--json %s - and run the command again."
                    % (render(path), REPORT_EXAMPLE))
    for path in archived:
        if canonical_path(path) == target:
            return ("--json points at a recording already filed in the archive "
                    "(%s). This check never writes to the archive, and the "
                    "report would replace that recording. Nothing was written. "
                    "Give the report a filename of its own - for example "
                    "--json %s - and run the command again."
                    % (render(path), REPORT_EXAMPLE))
    if config_path and canonical_path(config_path) == target:
        return ("--json points at the archive's fha.yaml, the file that says "
                "which folders hold your recordings. The report would replace "
                "it. Nothing was written. Give the report a filename of its own "
                "- for example --json %s - and run the command again."
                % REPORT_EXAMPLE)
    # The three checks above name the file that would be destroyed, which is the
    # message worth having. This last one is the net under them: a filed
    # transcript or photo is an original too, and it carries no media extension,
    # so it is not in the size index and none of the checks above can see it.
    # Nothing this script produces belongs inside a media root anyway.
    for _label, base in media_roots:
        if _is_inside(target_real, os.path.realpath(base)):
            return ("--json would write into the archive's %s folder, which "
                    "holds your filed recordings and their transcripts. This "
                    "check never writes to the archive. Nothing was written. "
                    "Put the report somewhere of your own - for example "
                    "--json %s - and run the command again."
                    % (render(base), REPORT_EXAMPLE))
    return None


def build_parser():
    p = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Content-hash incoming recordings against what the archive "
                    "already holds. Size first, SHA-256 only on a size collision. "
                    "Read-only: it reports, it never imports or renames anything.")
    p.add_argument("incoming", nargs="+", metavar="FILE_OR_FOLDER",
                   help="the recordings that arrived (files, or folders to walk)")
    p.add_argument("--root", default=None,
                   help="archive root, or any folder inside it "
                        "(default: walk up from the current directory)")
    p.add_argument("--media-root", action="append", default=None, dest="media_roots",
                   metavar="DIR",
                   help="check against this folder instead of the archive's "
                        "configured documents/photos roots (repeatable)")
    p.add_argument("--json", default=None, metavar="PATH",
                   help="also write the findings as JSON (a filename of its own; "
                        "a path landing on a recording is refused)")
    p.add_argument("--quiet", action="store_true", help="suppress the stdout summary")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    for p in args.incoming:
        if not os.path.exists(p):
            return fail("not found: %s - check the path and run the command again" % p)

    if args.media_roots:
        media_roots = []
        taken = set()
        for d in args.media_roots:
            if not os.path.isdir(d):
                return fail("--media-root is not a folder: %s - point it at the "
                            "folder holding the archived recordings" % d)
            base = os.path.abspath(d)
            if any(base == existing for _label, existing in media_roots):
                continue
            label = media_root_label(base, taken)
            taken.add(label)
            media_roots.append((label, base))
        archive_root = None
    else:
        archive_root = find_archive_root(args.root or os.getcwd())
        if archive_root is None:
            return fail(
                "no archive found above %s (nothing named fha.yaml). Run this from "
                "inside the archive, or pass --root <archive folder>, or check "
                "against one folder with --media-root <folder>."
                % os.path.abspath(args.root or os.getcwd()))
        try:
            media_roots, missing_roots = resolve_media_roots(archive_root)
        except ConfigProblem as e:
            return fail(str(e))
        if missing_roots:
            # A configured root that is not mounted is not an empty root. Its
            # recordings are unreadable, so nothing can be cleared against it.
            return fail(
                "the archive's %s folder is not there right now (%s). Recordings "
                "filed in it cannot be read, so this check cannot tell you "
                "whether your new recordings are already in the archive. "
                "Reconnect it (or fix the path in fha.yaml) and run the command "
                "again."
                % (", ".join(label for label, _v in missing_roots),
                   ", ".join(value for _l, value in missing_roots)))
        if not media_roots:
            return fail(
                "the archive at %s has no documents or photos folder to check "
                "against yet. Nothing is filed, so nothing can be a duplicate - "
                "import normally with `fha process`." % archive_root)

    incoming, incoming_unreadable = expand_inputs(args.incoming)
    if not incoming and not incoming_unreadable:
        return fail("none of those paths hold an audio or video file. Supported "
                    "extensions: %s" % ", ".join(sorted(MEDIA_EXTENSIONS)))

    named_roots = build_named_roots(media_roots, archive_root, args.incoming)

    def portable(path):
        return portable_path(path, named_roots)

    by_size, archive_unreadable = index_sizes_by_root(media_roots)

    # Before the first byte is hashed, and long before anything is written: a
    # report path that resolves onto a recording would destroy the file this run
    # exists to protect, and would do it after clearing that file for import.
    # The size index is built from directory entries alone, so it is already in
    # hand here and costs nothing to check against.
    archived_paths = [p for paths in by_size.values() for p in paths]
    collision = report_path_collision(
        args.json, incoming, archived_paths, media_roots,
        os.path.join(archive_root, "fha.yaml") if archive_root else None,
        portable)
    if collision:
        return fail(collision)

    cache = {}
    results = [check_one(p, by_size, cache) for p in incoming]
    # Where each incoming file came from, in the same named-root form as every
    # archived path. The console needs it as much as the JSON does: two bundles
    # in one run can both hold a "New Recording 4.m4a".
    for entry, src in zip(results, incoming):
        entry["path"] = portable(src)

    # An archived recording nobody could read might be the twin of ANY incoming
    # file, not just of one - so it cannot be attached to a single result. It
    # turns every would-be "new" into an open question instead.
    if archive_unreadable:
        for r in results:
            if r["verdict"] == "new":
                r["verdict"] = "indeterminate"
                r["detail"] = (
                    "%d archived recording(s) could not be read at all, so no "
                    "recording can be cleared as new this run"
                    % len(archive_unreadable))

    duplicates = [r for r in results if r["verdict"] == "duplicate"]
    indeterminate = [r for r in results if r["verdict"] == "indeterminate"]

    # Plain sentences about what stopped the check finishing. Named roots only,
    # never a machine path, because this list is written into the JSON report.
    incomplete = []
    for path in archive_unreadable:
        incomplete.append("an archived file or folder could not be read: %s"
                          % portable(path))
    for path in incoming_unreadable:
        incomplete.append("an incoming folder could not be listed, so the "
                          "recordings in it were never checked: %s"
                          % portable(path))

    if args.json:
        payload = {
            "tool": TOOL_NAME,
            "version": TOOL_VERSION,
            "media_roots": [label for label, _root in media_roots],
            "checked": len(results),
            "duplicates": len(duplicates),
            "indeterminate": len(indeterminate),
            "complete": not indeterminate and not incomplete,
            "results": [
                dict(r,
                     duplicates=[{"archived_path": portable(d["archived_path"]),
                                  "source_id": d["source_id"]}
                                 for d in r["duplicates"]],
                     unchecked=[{"archived_path": portable(u["archived_path"]),
                                 "detail": u["detail"]}
                                for u in r["unchecked"]])
                for r in results
            ],
            "could_not_be_read": incomplete,
            "paths_note": "every path is written under the name of the folder it "
                          "sits in - the archive's own documents/photos alias, an "
                          "explicit media root's name, or `incoming` for the "
                          "bundle being checked. Absolute machine paths are "
                          "deliberately not recorded, and no path is shortened to "
                          "a bare filename",
        }
        dest = os.path.abspath(args.json)
        tmp = "%s.tmp-%d" % (dest, os.getpid())
        try:
            parent = os.path.dirname(dest)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2) + "\n")
            os.replace(tmp, dest)
        except OSError as e:
            # A run that could not finish its report must not leave a stray
            # half-written `.tmp-1234` file sitting in the human's folder for
            # him to wonder about later.
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return fail("could not write %s: %s. Pick a --json path in a writable "
                        "folder and run the command again." % (args.json, e))

    if not args.quiet:
        print("checked %d recording(s) against %d archived media file(s)"
              % (len(results), sum(len(v) for v in by_size.values())))
        for r in results:
            if r["verdict"] == "duplicate":
                for d in r["duplicates"]:
                    sid = d["source_id"]
                    print("DUPLICATE  %s is byte-identical to %s%s"
                          % (r["path"], portable(d["archived_path"]),
                             " (%s)" % sid if sid else ""))
            elif r["verdict"] == "indeterminate":
                print("UNCHECKED  %s - %s"
                      % (r["path"], r.get("detail", "the check could not finish")))
            else:
                print("new        %s" % r["path"])
            # Shown for a duplicate too. Its verdict does not change - it is
            # already being skipped - but the human should still see that one
            # of the files this run compared it against could not be read.
            for u in r["unchecked"]:
                print("           could not read %s (%s)"
                      % (portable(u["archived_path"]), u["detail"]))
        if duplicates:
            print("")
            print("Do not import the duplicates: report each one with the path of "
                  "the recording it repeats and leave the bundle untouched. Confirm "
                  "the twin's record with `fha find <S-id>`. If the bundle carries a "
                  "transcript the archive lacks, that is an attach onto the existing "
                  "source with `fha process <filed-primary> --more <file> <role>`, "
                  "not a second import.")
        if indeterminate or incomplete:
            print("")
            print("The duplicate check did not finish. Nothing marked UNCHECKED "
                  "is cleared for import: until every archived recording of the "
                  "same size has been read, one of them may be the same file. "
                  "Reconnect the drive holding the archive's recordings, or fix "
                  "what the lines above name, then run the same command again - "
                  "on the whole bundle, not on the part that happened to work.")

    for note in incomplete:
        sys.stderr.write("%s: warning: %s\n" % (TOOL_NAME, note))

    if indeterminate or incomplete:
        # Ranked above the duplicate code on purpose: exit 2 tells the skill to
        # skip the named items and carry on with the rest, which is precisely
        # what it must not do when some of the rest was never checked.
        return EXIT_INDETERMINATE
    return EXIT_DUPLICATE if duplicates else EXIT_CLEAR


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\n%s: stopped. Nothing was written.\n" % TOOL_NAME)
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
    except OSError as exc:
        sys.exit(fail("the filesystem refused an operation: %s. Check the paths you "
                      "passed and run the command again." % exc))
